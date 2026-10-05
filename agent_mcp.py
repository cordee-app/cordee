"""
agent_mcp.py — SuperAgent-aware MCP server

Exposes project file tools (read, write, list) and memory access via the
Model Context Protocol. Consumed by:
  • Claude Code sessions  (stdio transport, configured in .claude/settings.json)
  • Family Assistant       (sse transport, connects to running server instance)
  • Future projects        (any MCP-compatible client)

Usage:
  # stdio (Claude Code) — project locked by path:
  python3 agent_mcp.py --project-path /path/to/project

  # stdio — project locked by DB id:
  python3 agent_mcp.py --project-id 7

  # SSE server (remote clients, runs persistently):
  python3 agent_mcp.py --project-id 7 --transport sse --port 8003
"""
import argparse
import logging
import os
import sys

# Ensure the SuperAgent package directory is on the path when invoked directly
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from mcp.server.fastmcp import FastMCP
import agent_tools

_log = logging.getLogger(__name__)


def _resolve_project_path(project_path: str | None, project_id: int | None) -> str:
    """Return an absolute, verified project path from CLI args."""
    if project_path:
        path = os.path.realpath(project_path)
        if not os.path.isdir(path):
            raise SystemExit(f'ERROR: --project-path does not exist: {path}')
        return path
    if project_id is not None:
        return agent_tools.resolve_project_path(project_id)
    raise SystemExit('ERROR: provide --project-path or --project-id')


def build_server(project_path: str) -> FastMCP:
    """Create and return a configured FastMCP instance for the given project."""
    project_name = os.path.basename(project_path.rstrip('/'))
    mcp = FastMCP(
        name=f'SuperAgent — {project_name}',
        instructions=(
            f'You have file access to the project at {project_path}. '
            'Use list_working_docs to discover reference documents, '
            'read_file to load them, write_file to save your output, '
            'and get_project_memory for accumulated project context.'
        ),
    )

    # ── Tools ────────────────────────────────────────────────────────────────

    @mcp.tool(description='List files and subdirectories in the project folder.')
    def list_files(directory: str = '') -> dict:
        """directory: relative path within the project (empty = project root)."""
        return agent_tools.list_files(project_path, directory)

    @mcp.tool(description='List all files in the project Working Docs folder.')
    def list_working_docs() -> dict:
        return agent_tools.list_working_docs(project_path)

    @mcp.tool(description='Read the contents of a file from the project folder.')
    def read_file(path: str) -> dict:
        """path: relative to the project root, e.g. "Working Docs/spec.md"."""
        return agent_tools.read_file(project_path, path)

    @mcp.tool(
        description=(
            'Write content to a file in the project folder. '
            'Creates the file and any missing parent directories.'
        )
    )
    def write_file(path: str, content: str) -> dict:
        """path: relative to project root. content: full file content."""
        return agent_tools.write_file(project_path, path, content)

    @mcp.tool(
        description=(
            'Read accumulated project or phase memory — '
            'context built up across previous task executions.'
        )
    )
    def get_project_memory(scope: str = 'project', phase_name: str = '') -> dict:
        """scope: "project" or "phase". phase_name required when scope is "phase"."""
        return agent_tools.get_project_memory(project_path, scope, phase_name)

    @mcp.tool(
        description=(
            'Query the shared RAG factory (railway, polish_general_law corpora). '
            'Retrieves top-k legal chunks with Dz.U./ELI/CELEX citations. '
            'Any Vault project can query any corpus — not project-scoped.'
        )
    )
    def rag_query(corpus_id: str, query: str, top_k: int = 8) -> dict:
        """corpus_id: railway | polish_general_law. query: PL/EN/FR question. top_k: 1-20."""
        return agent_tools.rag_query(corpus_id, query, top_k=top_k)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description='SuperAgent MCP server')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--project-path', metavar='PATH',
                       help='Absolute path to the project folder')
    group.add_argument('--project-id', metavar='ID', type=int,
                       help='SuperAgent project ID (looks up path from DB)')
    parser.add_argument('--transport', choices=['stdio', 'sse'], default='stdio',
                        help='Transport protocol (default: stdio)')
    parser.add_argument('--host', default='127.0.0.1',
                        help='Host for SSE transport (default: 127.0.0.1)')
    parser.add_argument('--port', type=int, default=8003,
                        help='Port for SSE transport (default: 8003)')
    parser.add_argument('--log-level', default='WARNING',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                        help='Log level (default: WARNING)')
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')

    project_path = _resolve_project_path(args.project_path, args.project_id)
    _log.info('Starting MCP server for project: %s', project_path)

    mcp = build_server(project_path)

    if args.transport == 'sse':
        # Patch host/port onto the settings object FastMCP reads at run time
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        print(f'SuperAgent MCP server (SSE) listening on {args.host}:{args.port}',
              flush=True)

    mcp.run(transport=args.transport)


if __name__ == '__main__':
    main()
