"""
agent_tools.py — SuperAgent-aware file tools shared between:
  • call_scaleway() agentic loop  (Scaleway function calling)
  • agent_mcp.py MCP server       (Claude Code, Family Assistant, future clients)

File tools are scoped to a project path — no access outside the project folder.
Stateless tools (web_fetch, rag_query) are NOT scoped — they reach external
services and do not check _safe_join. rag_query is the shared RAG factory
tool (1:N, any project can query any corpus via corpora/_registry.yaml).
"""
import contextlib
import contextvars
import json
import logging
import os
import re

import agent_files
import agent_config

_log = logging.getLogger(__name__)

# Folders recognised as "Working Docs". 'My Docs' is the default per
# READMEFIRST.md's Three-Layer Project Structure — it was missing here, which
# silently broke prompt_builder's file auto-catch (and H2's file-existence
# check) for every project using the documented default name instead of one
# of the accepted aliases.
_WORKING_DOC_FOLDERS = ('My Docs', 'Working Docs', 'Working Documents', 'working-docs', 'docs')

# Task outputs never go in the project root or Working Docs (the user's
# reference material): they go in Artifacts/outputs/<task-slug>/. The executor
# sets the running task's folder for the duration of the run; without one
# (MCP clients, chats) new files land in OUTPUTS_ROOT itself.
OUTPUTS_ROOT = os.path.join('Artifacts', 'outputs')
_output_dir = contextvars.ContextVar('aingel_output_dir', default=None)


@contextlib.contextmanager
def output_dir_scope(rel_dir):
    """Route new files written by tools to ``rel_dir`` (project-relative)."""
    token = _output_dir.set(rel_dir)
    try:
        yield
    finally:
        _output_dir.reset(token)


def current_output_dir():
    return _output_dir.get() or OUTPUTS_ROOT


def _is_agent_writable_rel(rel: str) -> bool:
    """Agent tools may write in Working Docs (existing files) and Artifacts/outputs.

    Users stay limited to Working Docs (agent_files._is_writable_rel, used by
    WebDAV and the Files UI); outputs are read-only for them by design.
    """
    if agent_files._is_writable_rel(rel):
        return True
    rel = (rel or '').strip().replace(os.sep, '/')
    prefix = OUTPUTS_ROOT.replace(os.sep, '/') + '/'
    if not rel.startswith(prefix) or rel.startswith('/'):
        return False
    for p in rel.split('/'):
        if not p or p in ('.', '..') or agent_files._is_forbidden_path(p):
            return False
    return True


def _route_new_output(project_path: str, path: str) -> str:
    """Where a write to ``path`` should land under the output rules.

    Bare filenames and *new* files under Working Docs go to the task's output
    folder (Working Docs sub-paths are kept, e.g. ``Working Docs/a/b.md`` ->
    ``<out>/a/b.md``). Existing Working Docs files are edited in place, and
    paths already under Artifacts/outputs are left alone.
    """
    norm = (path or '').strip().replace('\\', '/')
    if not norm:
        return path
    if '/' not in norm:
        from agent_config import DEFINITION_FILE, _LEGACY_DEF_FILE
        if norm in (DEFINITION_FILE, _LEGACY_DEF_FILE, 'GUIDE.md', 'Skills.md'):
            return path
        return os.path.join(current_output_dir(), norm)
    top, rest = norm.split('/', 1)
    if top in _WORKING_DOC_FOLDERS and not os.path.exists(os.path.join(project_path, norm)):
        return os.path.join(current_output_dir(), rest)
    return path

# Directories always excluded from listings
_SKIP_DIRS = {
    '__pycache__', 'node_modules', '.git', '.venv', 'venv',
    'Artifacts', '.claude', '.vibe',
}


def _extract_pdf_text(filepath, max_chars=200_000):
    """Extract text from a .pdf file using pdfplumber.

    Returns the extracted text, or None if extraction fails (e.g. a scanned
    image PDF with no text layer).
    """
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ''
                if text.strip():
                    parts.append(text)
        result = '\n'.join(parts)
        if len(result) > max_chars:
            result = result[:max_chars] + '\n...[truncated]'
        return result if result.strip() else None
    except Exception:
        return None


def _extract_docx_text(filepath, max_chars=200_000):
    """Extract text from a .docx file using python-docx.

    Returns the extracted text, or None if extraction fails. Caps at max_chars
    to avoid overwhelming the prompt.
    """
    try:
        import docx
        doc = docx.Document(filepath)
        parts = []
        for para in doc.paragraphs:
            text = para.text.strip()
            if text:
                parts.append(text)
        for table in doc.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    parts.append(' | '.join(cells))
        result = '\n'.join(parts)
        if len(result) > max_chars:
            result = result[:max_chars] + '\n...[truncated]'
        return result if result.strip() else None
    except Exception:
        return None


def _extract_xlsx_text(filepath, max_chars=200_000):
    """Extract text from an .xlsx file using openpyxl.

    Returns the extracted text, or None if extraction fails. Caps at max_chars
    to avoid overwhelming the prompt.
    """
    try:
        import openpyxl
        wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    parts.append(' | '.join(cells))
        wb.close()
        result = '\n'.join(parts)
        if len(result) > max_chars:
            result = result[:max_chars] + '\n...[truncated]'
        return result if result.strip() else None
    except Exception:
        return None


# ── Path safety ────────────────────────────────────────────────────────────────

def resolve_project_path(project_id: int) -> str:
    """Return the filesystem path for a project, or raise ValueError."""
    import agent_db
    proj = agent_db.get_project(project_id)
    if not proj:
        raise ValueError(f'Project {project_id} not found')
    path = proj.get('path', '')
    if not path or not os.path.isdir(path):
        raise ValueError(f'Project {project_id} path not found on disk: {path!r}')
    return path


def _safe_join(project_path: str, relative: str) -> str:
    """
    Join project_path + relative and verify the result stays inside project_path.
    Raises ValueError on path traversal attempts.

    Legacy helper — kept for backward compat. New code should use:
      * agent_files._safe_resolve for writes (writable + forbidden + symlink walk)
      * _safe_join_for_read for reads (forbidden + symlink walk, no writable)
    """
    base = os.path.realpath(project_path)
    target = os.path.realpath(os.path.join(base, relative))
    if not target.startswith(base + os.sep) and target != base:
        raise ValueError(f'Path traversal blocked: {relative!r} escapes project root')
    return target


def _forbidden_for_read(name: str) -> bool:
    """Read-grade forbidden check: .db/.env/.git-hidden etc.

    Reuses agent_files._is_forbidden_path and also hides HIDDEN_DIRS from
    agent_filecat so GET/PROPFIND/list never leaks system files.
    """
    try:
        if agent_files._is_forbidden_path(name):
            return True
    except Exception:
        pass
    try:
        import agent_filecat as _fcat
        if name in _fcat.HIDDEN_DIRS:
            return True
    except Exception:
        pass
    return False


def _safe_join_for_read(project_path: str, relative: str) -> str:
    """Resolve relative inside project_path for read operations.

    Security: realpath containment + forbidden + lexists/islink walk.
    Unlike agent_files._safe_resolve this does NOT require the path to be
    under a writable Working-Docs variant — reads may access any
    non-forbidden file inside the project (READMEFIRST.md, Artifacts/*, etc).
    Mirrors agent_webdav._safe_join_for_read.
    """
    if not relative:
        return os.path.realpath(project_path)
    rel = relative.strip().replace(os.sep, '/')
    if rel.startswith('/') or rel.startswith('\\'):
        raise ValueError('absolute path not allowed')
    parts = rel.split('/')
    if '..' in parts:
        raise ValueError('path traversal not allowed')
    for part in parts:
        if part and _forbidden_for_read(part):
            low = part.lower()
            # Only block truly sensitive leaves — _forbidden_for_read is strict
            # (e.g. any dotfile), but we narrow to .db/.env/.bak/hidden to avoid
            # false-positives on legitimate names while still blocking secrets.
            if low in ('.env', '.env.example', 'aingel.db') or low.endswith(('.db', '.db-journal', '.db-wal', '.db-shm', '.bak')) or part.startswith('.'):
                raise ValueError(f'forbidden path component: {part}')
            # Also block forbidden dirs like .git, .venv etc.
            if part in ('.git', '.venv', 'venv', '__pycache__', '.claude', '.vibe', 'node_modules', '.agents', '.codex'):
                raise ValueError(f'forbidden path component: {part}')
    # Reject any existing symlink component along the lexical path
    lex_parts = rel.split('/')
    for i in range(1, len(lex_parts) + 1):
        prefix = '/'.join(lex_parts[:i])
        lex_abs = os.path.join(project_path, prefix)
        if os.path.lexists(lex_abs) and os.path.islink(lex_abs):
            raise ValueError(f'symlink not allowed: {prefix}')
    lex_abs = os.path.join(project_path, rel)
    candidate_real = os.path.realpath(lex_abs)
    root_real = os.path.realpath(project_path)
    if not (candidate_real == root_real or candidate_real.startswith(root_real + os.sep)):
        raise ValueError('path escapes project')
    # Block resolved forbidden basename even on read (symlink target name)
    base = os.path.basename(candidate_real)
    if base and _forbidden_for_read(base):
        low = base.lower()
        if low in ('.env', '.env.example', 'aingel.db') or low.endswith(('.db', '.db-journal', '.db-wal', '.db-shm', '.bak')) or base.startswith('.'):
            raise ValueError(f'forbidden resolved path: {base}')
    return lex_abs


# ── Tool implementations ───────────────────────────────────────────────────────

_TRASH_REF = re.compile(r'(^|[^A-Za-z0-9_])\.trash([^A-Za-z0-9_]|$)', re.IGNORECASE)
# Out-of-tree trash root (agent_config.TRASH_ROOT). The agent's shell runs as
# the same user as the service so it can read this too — the guard now blocks both
# the legacy in-tree .trash and the relocated vault-trash root (C6).
_TRASH_ROOT_NAMES = ('vault-trash', 'aingel-trash', 'cordee-trash')


def _trash_command_blocked(project_path: str, command: str) -> bool:
    """True if a bash command references the project's soft-delete trash dir
    (either the legacy in-tree `.trash` or the relocated `TRASH_ROOT`).

    .trash is the agent's soft-delete destination (recoverable) and is hidden
    from every read tool. Bash is the one escape hatch the model could use to
    resurrect deleted files (e.g. `cp .trash/files/... Working Documents/`), so
    we refuse any command that mentions the trash. This is intentionally strict —
    legitimate commands never need to touch the trash.

    The guard is advisory, not a sandbox: a determined model could still alias
    or obfuscate the path. It stops the common, accidental resurrection path.
    """
    if not command:
        return False
    low = command.lower()
    if '.trash' in low and _TRASH_REF.search(command):
        return True
    for tok in _TRASH_ROOT_NAMES:
        if tok in low:
            return True
    # The configured root itself, whatever it is called (e.g. <install>/trash).
    root = (getattr(agent_config, 'TRASH_ROOT', '') or '').rstrip('/').lower()
    if root and root in low:
        return True
    return False


def list_files(project_path: str, directory: str = '') -> dict:
    """
    List files and subdirectories inside the project folder.
    directory: relative path within the project (empty = project root).
    Returns {files: [...], directories: [...], path: str}.
    """
    if directory:
        try:
            target = _safe_join_for_read(project_path, directory)
        except ValueError as e:
            return {'error': str(e)}
    else:
        target = project_path
    if not os.path.isdir(target):
        return {'error': f'Directory not found: {directory!r}'}

    files, dirs = [], []
    try:
        for entry in sorted(os.scandir(target), key=lambda e: e.name):
            if entry.name.startswith('.'):
                continue
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS:
                    dirs.append(entry.name)
            else:
                files.append(entry.name)
    except PermissionError as e:
        return {'error': str(e)}

    return {
        'path': directory or '.',
        'directories': dirs,
        'files': files,
    }


def list_working_docs(project_path: str) -> dict:
    """
    List files in the project's Working Docs folders (checks all known folder
    names, up to two subdirectory levels deep, e.g. ``CUPT/II Etap/file.pdf``).
    Returns {folder: str, files: [...]} or {error: str} if none found.

    ``files`` entries are paths relative to the project root (e.g.
    ``Working Documents/CUPT/II Etap/foo.pdf``), not bare basenames — that's
    what ``read_file``'s ``path`` argument expects, and it lets a nested file
    be told apart from a same-named one elsewhere (regression: task #10001145,
    where a bare-basename list couldn't confirm a nested-path reference).
    """
    found_folder = None
    files = []
    try:
        for folder in _WORKING_DOC_FOLDERS:
            wd_path = os.path.join(project_path, folder)
            if os.path.isdir(wd_path):
                if found_folder is None:
                    found_folder = folder
                for _fname, full in agent_files.list_files_under(wd_path, max_depth=3):
                    rel = os.path.relpath(full, project_path)
                    if rel not in files:
                        files.append(rel)
        # Earlier task outputs are readable inputs too.
        out_path = os.path.join(project_path, OUTPUTS_ROOT)
        if os.path.isdir(out_path):
            if found_folder is None:
                found_folder = OUTPUTS_ROOT
            for _fname, full in agent_files.list_files_under(out_path, max_depth=3):
                rel = os.path.relpath(full, project_path)
                if rel not in files:
                    files.append(rel)
    except PermissionError as e:
        return {'error': str(e)}
    if found_folder is None:
        return {'folder': None, 'files': [], 'error': 'No Working Docs folder found in project'}
    return {'folder': found_folder, 'files': sorted(files)}


def read_file(project_path: str, path: str, max_chars: int = 20_000,
               start_line: int = None, end_line: int = None) -> dict:
    """
    Read a file from the project folder.
    path: relative to project root. Returns {content: str} or {error: str}.
    start_line/end_line: 1-based, inclusive. Use to read a specific section of a large file.
    This function now also supports common binary formats (e.g., .pdf, .db, .xlsx, .docx) by returning
    a base64‑encoded string with a ``binary`` flag.
    """
    try:
        full = _safe_join_for_read(project_path, path)
    except ValueError as e:
        return {'error': str(e)}

    if not os.path.isfile(full):
        return {'error': f'File not found: {path!r}'}

    # Determine if the file is likely binary based on extension
    _binary_exts = {'.pdf', '.db', '.sqlite', '.xlsx', '.xls', '.docx', '.pptx', '.odt', '.ods', '.odp'}
    _, ext = os.path.splitext(full)
    is_binary = ext.lower() in _binary_exts

    try:
        if is_binary:
            # PDFs/DOCX: extract the text layer so the model can actually read
            # them (scanned/image PDFs with no text layer fall back to base64).
            if ext.lower() == '.pdf':
                text = _extract_pdf_text(full, max_chars=max_chars)
                if text is not None:
                    result = {'path': path, 'content': text, 'binary': False, 'encoding': 'text'}
                    if os.path.getsize(full) > max_chars:
                        result['truncated'] = True
                        result['note'] = f'PDF text truncated at {max_chars} chars — use start_line/end_line (unsupported for PDFs)'
                    return result
            if ext.lower() == '.docx':
                text = _extract_docx_text(full, max_chars=max_chars)
                if text is not None:
                    result = {'path': path, 'content': text, 'binary': False, 'encoding': 'text'}
                    if os.path.getsize(full) > max_chars:
                        result['truncated'] = True
                        result['note'] = f'DOCX text truncated at {max_chars} chars — use start_line/end_line (unsupported for DOCX)'
                    return result
            if ext.lower() == '.xlsx':
                text = _extract_xlsx_text(full, max_chars=max_chars)
                if text is not None:
                    result = {'path': path, 'content': text, 'binary': False, 'encoding': 'text'}
                    if os.path.getsize(full) > max_chars:
                        result['truncated'] = True
                        result['note'] = f'Spreadsheet text truncated at {max_chars} chars — use start_line/end_line (unsupported for spreadsheets)'
                    return result
            # Other binaries (or PDFs with no text layer): read raw bytes,
            # limit to max_chars bytes, then base64‑encode for safe transport.
            with open(full, 'rb') as f:
                data = f.read(max_chars)
            import base64
            encoded = base64.b64encode(data).decode('ascii')
            result = {'path': path, 'content': encoded, 'binary': True, 'encoding': 'base64'}
            if os.path.getsize(full) > max_chars:
                result['truncated'] = True
                result['note'] = f'Binary file truncated at {max_chars} bytes — use start_line/end_line (unsupported for binaries)'
            return result
        else:
            with open(full, 'r', encoding='utf-8', errors='replace') as f:
                if start_line is not None or end_line is not None:
                    lines = f.readlines()
                    total_lines = len(lines)
                    s = max(1, start_line or 1)
                    e = min(total_lines, end_line or total_lines)
                    selected = lines[s - 1:e]
                    content = ''.join(selected)
                    result = {'path': path, 'content': content,
                              'start_line': s, 'end_line': e, 'total_lines': total_lines}
                    if len(content) > max_chars:
                        content = content[:max_chars]
                        result['content'] = content
                        result['truncated'] = True
                        result['note'] = f'Section truncated at {max_chars} chars'
                    return result
                content = f.read(max_chars)
            truncated = os.path.getsize(full) > max_chars
            result = {'path': path, 'content': content}
            if truncated:
                result['truncated'] = True
                result['note'] = f'File truncated at {max_chars} chars — use start_line/end_line to read specific sections'
            return result
    except Exception as e:
        return {'error': str(e)}


def write_file(project_path: str, path: str, content: str) -> dict:
    """
    Write (create or overwrite) a file inside the project folder.
    path: relative to project root. Returns {ok: True, path: str} or {error: str}.

    New files go to the task's output folder (Artifacts/outputs/<task-slug>/),
    never the project root or Working Docs — see _route_new_output. The
    returned ``path`` is where the file actually landed.
    """
    path = _route_new_output(project_path, path)

    try:
        if not _is_agent_writable_rel(path):
            raise ValueError(f'path must be under Working Documents or {OUTPUTS_ROOT} and not forbidden: {path}')
        full = agent_files._safe_resolve(project_path, path)
    except ValueError as e:
        return {'error': str(e)}

    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w', encoding='utf-8') as f:
            f.write(content)
        _log.info('write_file: wrote %d chars to %s', len(content), full)
        return {'ok': True, 'path': path, 'bytes': len(content.encode())}
    except Exception as e:
        _log.error('write_file error: %s', e)
        return {'error': str(e)}


def _kill_group(proc, signal=9):
    """Best-effort send a signal to the whole process group of `proc` (children
    included). bash() starts the shell with start_new_session=True, so the group
    id is the shell's pid and stays valid after the shell itself has exited
    (os.getpgid(pid) would fail on the reaped shell and miss the children)."""
    import os as _os
    try:
        _os.killpg(proc.pid, signal)
    except ProcessLookupError:
        pass  # group already empty
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def bash(project_path: str, command: str, timeout: int = 120) -> dict:
    """
    Run a shell command with the project folder as the working directory.
    command: shell command string. Runs with cwd=project_path so relative
    paths resolve against the project root. Output is capped to avoid
    overwhelming the prompt. Returns {stdout, stderr, returncode} or {error}.

    Security note: bash is intentionally NOT gated by agent_files._is_writable_rel
    / _safe_resolve. It is an arbitrary shell whose writes are broader than
    single-file tool calls (pipelines, redirections, git, python scripts).
    Confinement relies on cwd=project_path + realpath containment via the
    process working directory and on project .claude/settings.json permissions,
    not on a per-path writable check. A per-path writable guard is still
    bypassable via shell indirection.

    The one hard line drawn here: the soft-delete trash directory (.trash) is
    NOT a valid bash target for the agent. Read tools already hide .trash from
    the prompt (agent_filecat.HIDDEN_DIRS) and block it on read; bash is the
    only tool that could resurrect soft-deleted files by copying them back out
    of .trash. Any command referencing .trash is rejected outright, so soft
    deletes stay soft-deleted until the 7-day reaper hard-deletes them.
    """
    import subprocess
    if not command or not command.strip():
        return {'error': 'Empty command'}
    if _trash_command_blocked(project_path, command):
        return {'error': 'Command referencing .trash is blocked: the soft-delete '
                         'trash is off-limits to the agent (use delete via the '
                         'file API instead).'}

    # Run in its own process group (start_new_session) so a timeout kills the
    # whole group — background children can't outlive the tool call and re-enable
    # a deferred resurrection after we've returned.
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=project_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            # Scrubbed env: the agent's shell must not inherit the server's
            # secrets (session secret, DAV token, provider keys, DB paths).
            env=agent_config.cli_subprocess_env(),
        )
    except Exception as e:
        return {'error': f'Command failed to start: {e}'}
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill the whole process group (children included), then reap.
        _kill_group(proc)
        proc.communicate()
        return {'error': f'Command timed out after {timeout}s'}
    # C6: also kill the whole group on NORMAL exit so a `cmd &` background job
    # cannot outlive the tool call and re-enable a deferred resurrection after
    # we've returned (only relevant if the child spawned its own children).
    _kill_group(proc, signal=15)
    _MAX_OUT = 20_000
    if len(stdout) > _MAX_OUT:
        stdout = stdout[:_MAX_OUT] + '\n...[stdout truncated]'
    if len(stderr) > _MAX_OUT:
        stderr = stderr[:_MAX_OUT] + '\n...[stderr truncated]'
    return {
        'stdout': stdout,
        'stderr': stderr,
        'returncode': proc.returncode,
    }


def patch_file(project_path: str, path: str, old_string: str, new_string: str) -> dict:
    """
    Replace an exact string in a file without rewriting the whole file.
    old_string must match exactly (whitespace, quotes, indentation).
    Returns {ok: True, path: str} or {error: str}.
    """
    try:
        if not _is_agent_writable_rel(path):
            raise ValueError(f'path must be under Working Documents or {OUTPUTS_ROOT} and not forbidden: {path}')
        full = agent_files._safe_resolve(project_path, path)
    except ValueError as e:
        return {'error': str(e)}

    if not os.path.isfile(full):
        return {'error': f'File not found: {path!r}'}

    try:
        with open(full, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except Exception as e:
        return {'error': f'Read error: {e}'}

    count = content.count(old_string)
    if count == 0:
        return {'error': 'old_string not found in file — read the target section first and copy the exact text'}
    if count > 1:
        return {'error': f'old_string matches {count} locations — provide more surrounding context to make it unique'}

    new_content = content.replace(old_string, new_string, 1)
    try:
        with open(full, 'w', encoding='utf-8') as f:
            f.write(new_content)
        _log.info('patch_file: patched %s (%d→%d chars)', full, len(old_string), len(new_string))
        return {'ok': True, 'path': path}
    except Exception as e:
        _log.error('patch_file write error: %s', e)
        return {'error': str(e)}


def get_project_memory(project_path: str, scope: str = "project", phase_name: str = "") -> dict:
    """
    Read a memory file from Artifacts/.
    scope: "project" → project.memory.md, "phase" → phase-<phase_name>.memory.md
    Returns {content: str} or {error: str}.
    """
    artifacts = os.path.join(project_path, "Artifacts")
    if scope == "phase" and phase_name:
        from agent_memory import _phase_slug
        slug = _phase_slug(phase_name)
        fname = f"phase-{slug}.memory.md"
    else:
        fname = "project.memory.md"

    fpath = os.path.join(artifacts, fname)
    
    # Containment check
    artifacts_real = os.path.realpath(artifacts)
    fpath_real = os.path.realpath(fpath)
    if not (fpath_real == artifacts_real or fpath_real.startswith(artifacts_real + os.sep)):
        return {"content": "", "error": "invalid phase_name"}
    
    if not os.path.isfile(fpath):
        return {"content": "", "note": f"{fname} does not exist yet"}

    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"scope": scope, "file": fname, "content": content}
    except Exception as e:
        return {"error": str(e)}


def rag_query(corpus_id: str, query: str, top_k: int = 8, filters: dict | None = None,
              rerank: bool = False, graph: bool = False) -> dict:
    """Query the shared RAG factory (Qdrant Hybrid) — 1:N, not project-scoped.

    Factory lives at /opt/RAG (outside PROJECTS_ROOT). Any Vault project can
    call rag_query(corpus_id="railway", query="Art. 22b ust.21") — analogous
    to web_fetch, no _safe_join check.
    Tries HTTP RAG API at localhost:8010 (hybrid dense+BM25+article boost+RRF,
    optional cross-encoder rerank + graph expansion), falls back to direct Qdrant.
    """
    import requests as _req
    if not corpus_id or not query:
        return {"error": "corpus_id and query are required"}
    if corpus_id not in ("railway", "polish_general_law"):
        # allow unknown corpora — factory registry is authoritative (corpora/_registry.yaml)
        pass
    # 1. Try RAG HTTP API (preferred — single contract for all consumers)
    rag_url = os.environ.get("RAG_API_URL", "http://localhost:8010/v1/rag/query")
    try:
        resp = _req.post(rag_url, json={
            "corpus_id": corpus_id,
            "query": query,
            "top_k": int(top_k) if top_k else 8,
            "filters": filters or {},
            "rerank": bool(rerank),
            "graph": bool(graph),
        }, timeout=25)
        if resp.status_code == 200:
            data = resp.json()
            # normalize: expect {hits:[{text,dz_u_ref,eli_url,celex,score}], answer?}
            return {"corpus_id": corpus_id, "query": query, "via": "rag_api", **data}
        # non-200: fall through to local attempt, but surface status
        if resp.status_code not in (404, 502, 503):
            return {"error": f"RAG API {resp.status_code}: {resp.text[:500]}", "corpus_id": corpus_id}
    except Exception as e:
        # API not running yet — report but don't crash dispatch
        _log.debug("rag_query API miss %s: %s", rag_url, e)

    # 2. Fallback: direct Qdrant Hybrid search (no rag_server needed for demo)
    try:
        qdrant_url = os.environ.get("QDRANT_URL", "http://localhost:6333")
        # Map corpus_id -> collection (factory registry)
        coll_map = {"railway": "railway_L1L3", "polish_general_law": "polish_general_law"}
        collection = coll_map.get(corpus_id, corpus_id)
        # Probe collections
        probe = _req.get(f"{qdrant_url}/collections", timeout=5)
        cols = []
        if probe.status_code == 200:
            cols = [c.get("name") for c in probe.json().get("result", {}).get("collections", [])]
        if collection not in cols:
            return {"corpus_id": corpus_id, "query": query, "via": "qdrant_direct", "error": f"collection {collection} not in Qdrant {cols}", "hint": "run /opt/RAG pipeline index_qdrant --corpus-id railway"}
        # Build query vector with same BGE-M3 as index (1024) — fallback to 1024-dim hash if model not available
        import re as _re
        qvec = None
        try:
            from sentence_transformers import SentenceTransformer
            _qmodel = SentenceTransformer("BAAI/bge-m3", trust_remote_code=True, device="cpu")
            qvec = _qmodel.encode([query], normalize_embeddings=True)[0].tolist()
        except Exception as e:
            _log.debug("BGE-M3 query embed failed %s — hash fallback 1024", e)
            import hashlib
            h = hashlib.sha256(query.encode()).digest()
            qvec = [h[i % len(h)]/255.0 for i in range(1024)]
        # Try qdrant_client if available, else REST
        try:
            from qdrant_client import QdrantClient
            client = QdrantClient(host="localhost", port=6333, timeout=10)
            # Keyword-filtered path if Art. found (mirrors ask.py) — boosts exact article
            art_m = _re.search(r"Art\.\s*(\d+[a-z]*)", query, _re.IGNORECASE)
            if art_m:
                art = art_m.group(1)
                hits, _ = client.scroll(collection_name=collection, scroll_filter={"must": [{"key": "article", "match": {"value": art}}]}, limit=int(top_k or 8), with_payload=True)
                if hits:
                    citations = [{"dz_u": p.payload.get("dz_u_ref"), "eli": p.payload.get("eli_url"), "article": p.payload.get("article"), "ustep": p.payload.get("ustep"), "extract": (p.payload.get("text") or "")[:350], "score": 1.0, "hash": p.payload.get("hash_verbatim")} for p in hits[: int(top_k or 8)]]
                    return {"corpus_id": corpus_id, "collection": collection, "query": query, "via": "qdrant_direct", "citations": citations, "hits": len(citations)}
            res = client.query_points(collection_name=collection, query=qvec, limit=int(top_k or 8), with_payload=True)
            citations = [{"dz_u": p.payload.get("dz_u_ref"), "eli": p.payload.get("eli_url"), "article": p.payload.get("article"), "ustep": p.payload.get("ustep"), "extract": (p.payload.get("text") or "")[:350], "score": round(p.score,4), "hash": p.payload.get("hash_verbatim")} for p in res.points]
            return {"corpus_id": corpus_id, "collection": collection, "query": query, "via": "qdrant_direct", "citations": citations, "hits": len(citations)}
        except Exception:
            # REST fallback
            r = _req.post(f"{qdrant_url}/collections/{collection}/points/query", json={"query": qvec, "limit": int(top_k or 8), "with_payload": True}, timeout=10)
            if r.status_code == 200:
                data = r.json().get("result", {}).get("points", [])
                citations = [{"dz_u": p.get("payload",{}).get("dz_u_ref"), "eli": p.get("payload",{}).get("eli_url"), "article": p.get("payload",{}).get("article"), "extract": (p.get("payload",{}).get("text") or "")[:350], "score": round(p.get("score",0),4)} for p in data]
                return {"corpus_id": corpus_id, "collection": collection, "query": query, "via": "qdrant_direct_rest", "citations": citations}
            return {"corpus_id": corpus_id, "query": query, "via": "qdrant_direct", "note": "Qdrant collections: " + ", ".join(cols), "collections": cols}
    except Exception as e:
        _log.debug("rag_query qdrant fallback failed: %s", e)

    return {
        "corpus_id": corpus_id,
        "query": query,
        "error": "RAG not yet indexed — run /opt/RAG pipeline (crawl→chunk→index) then start rag_server.py on :8010, or qdrant on :6333",
        "hint": "Factory: /opt/RAG/corpora/_registry.yaml, API: http://localhost:8010/v1/rag/query",
    }


def _ip_is_blocked(ip_str: str) -> bool:
    """True for loopback/private/link-local/reserved addresses."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable → block
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _url_host_is_blocked(url: str) -> bool:
    """True when the URL points at a loopback/private/reserved address.

    Resolves every address the host maps to, so a name that resolves to
    127.0.0.1 or a Docker/LAN IP is refused. This stops the agent's web_fetch
    from reaching internal services (the Flask app itself, cloudflared, the
    RAG server, cloud metadata endpoints, …).
    """
    import socket
    import urllib.parse
    try:
        host = urllib.parse.urlparse(url).hostname
    except Exception:
        return True
    if not host:
        return True
    try:
        import ipaddress
        ipaddress.ip_address(host)
        return _ip_is_blocked(host)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return True
    if not infos:
        return True
    return any(_ip_is_blocked(info[4][0]) for info in infos)


def web_fetch(url: str, max_chars: int = 20_000) -> dict:
    """Fetch a URL and return the content as text (HTML tags stripped).

    Uses requests + BeautifulSoup to extract readable text from any web page.
    Caps at max_chars to avoid overwhelming the prompt. Loopback/private hosts
    are refused, and redirects are followed manually so a public URL cannot
    bounce the fetch onto an internal address.
    """
    import urllib.parse
    import requests
    from bs4 import BeautifulSoup

    if not url or not url.startswith(('http://', 'https://')):
        return {"error": "URL must start with http:// or https://"}

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36',
        }
        # Manual redirect handling: validate every hop's host before fetching.
        resp = None
        for _ in range(6):
            if _url_host_is_blocked(url):
                return {"error": "URL host is not allowed (loopback/private addresses are blocked)"}
            resp = requests.get(url, headers=headers, timeout=30, allow_redirects=False)
            if resp.is_redirect and resp.headers.get('Location'):
                url = urllib.parse.urljoin(url, resp.headers['Location'])
                continue
            break
        resp.raise_for_status()

        content_type = resp.headers.get('Content-Type', '')

        # For non-HTML responses, return raw text (truncated)
        if 'text/html' not in content_type and 'application/xhtml' not in content_type:
            text = resp.text[:max_chars]
            return {
                "url": resp.url,
                "status": resp.status_code,
                "content_type": content_type,
                "content": text,
            }

        # Parse HTML and extract text
        soup = BeautifulSoup(resp.text, 'html.parser')

        # Remove script, style, nav, footer, header elements
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'noscript', 'iframe']):
            tag.decompose()

        # Try to get main content, fall back to body
        main = soup.find('main') or soup.find('article') or soup.body
        if main:
            text = main.get_text(separator='\n', strip=True)
        else:
            text = soup.get_text(separator='\n', strip=True)

        # Collapse excessive blank lines
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        text = '\n'.join(lines)

        if len(text) > max_chars:
            text = text[:max_chars] + '\n...[truncated]'

        return {
            "url": resp.url,
            "status": resp.status_code,
            "content_type": content_type,
            "content": text,
        }
    except requests.exceptions.Timeout:
        return {"error": f"Request timed out (30s) for {url}"}
    except requests.exceptions.ConnectionError as e:
        return {"error": f"Connection error: {e}"}
    except requests.exceptions.HTTPError as e:
        return {"error": f"HTTP error: {e}"}
    except Exception as e:
        return {"error": f"Fetch failed: {e}"}



# ── OpenAI-compatible tool schemas ────────────────────────────────────────────
# Passed to Scaleway (and any other function-calling API) as the `tools` parameter.

TOOL_SCHEMAS = [
    {
        'type': 'function',
        'function': {
            'name': 'list_files',
            'description': 'List files and subdirectories in the project folder. Use this to discover what files are available before reading them.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'directory': {
                        'type': 'string',
                        'description': 'Relative path within the project to list (e.g. "Working Docs"). Leave empty for the project root.',
                    },
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_working_docs',
            'description': 'List all files in the project Working Docs folder. These are the primary reference documents for the project.',
            'parameters': {
                'type': 'object',
                'properties': {},
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'read_file',
            'description': (
                'Read a file from the project folder. '
                'Large files are truncated at 20,000 chars — use start_line/end_line to read specific sections '
                'rather than re-reading the whole file. Prefer targeted reads over reading entire large files.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {
                        'type': 'string',
                        'description': 'Relative path to the file within the project folder (e.g. "agent_dashboard.js").',
                    },
                    'start_line': {
                        'type': 'integer',
                        'description': '1-based line number to start reading from (inclusive). Use with end_line to read a specific section.',
                    },
                    'end_line': {
                        'type': 'integer',
                        'description': '1-based line number to stop reading at (inclusive). Omit to read to end of file (or truncation limit).',
                    },
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'write_file',
            'description': 'Write content to a file in the project folder. Creates the file and any missing parent directories. Use this to save your output: new files are saved in the task\'s output folder under Artifacts/outputs/ (the result gives the final path).',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {
                        'type': 'string',
                        'description': 'File name or relative path (e.g. "report.md"). New files go to the task output folder under Artifacts/outputs/; an existing Working Docs file is edited in place.',
                    },
                    'content': {
                        'type': 'string',
                        'description': 'Full file content to write.',
                    },
                },
                'required': ['path', 'content'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'patch_file',
            'description': (
                'Replace an exact string in an existing file without rewriting the whole file. '
                'Use this instead of write_file when editing large files — you only send the changed lines, not the entire content. '
                'Workflow: read the target section with read_file(start_line=X, end_line=Y), copy the exact text you want to replace as old_string, write the replacement as new_string. '
                'old_string must match exactly (including whitespace and indentation). '
                'Returns an error if old_string is not found or matches multiple locations.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {
                        'type': 'string',
                        'description': 'Relative path to the file within the project folder (e.g. "agent_dashboard.js").',
                    },
                    'old_string': {
                        'type': 'string',
                        'description': 'Exact text to find and replace. Must match character-for-character including indentation.',
                    },
                    'new_string': {
                        'type': 'string',
                        'description': 'Replacement text. May be empty string to delete old_string.',
                    },
                },
                'required': ['path', 'old_string', 'new_string'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_project_memory',
            'description': 'Read accumulated project or phase memory — context built up across previous task executions.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'scope': {
                        'type': 'string',
                        'enum': ['project', 'phase'],
                        'description': '"project" for project-wide memory, "phase" for a specific phase.',
                    },
                    'phase_name': {
                        'type': 'string',
                        'description': 'Required when scope is "phase". The phase name (e.g. "Phase 1").',
                    },
                },
                'required': ['scope'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'web_fetch',
            'description': 'Fetch a web URL and return its text content. Use this to browse websites, check documentation, or read web pages referenced in the task description. HTML is stripped to plain text.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'url': {
                        'type': 'string',
                        'description': 'The full URL to fetch (e.g. "https://example.com/page").',
                    },
                    'max_chars': {
                        'type': 'integer',
                        'description': 'Maximum characters to return. Default 20000.',
                    },
                },
                'required': ['url'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'rag_query',
            'description': (
                'You MUST call rag_query before answering any question about Polish/EU '
                'law when RAG is enabled for this project (Dz.U., ELI, CELEX, Art., ust., '
                'rozporządzenie). Do not answer such questions from memory or local PDFs. '
                'Retrieves top-k legal chunks (Dz.U. + ELI + CELEX + verbatim extract) for a question. '
                'Corpora: railway (L1 EU→L2 Polish Acts→L3 executive/UTK), polish_general_law (KSH — Kodeks spółek handlowych). '
                'Call this tool SEPARATELY for EACH specific article you intend to cite '
                '(e.g. query "art. 116 § 2 Ordynacji podatkowej", then "art. 202 § 6 k.s.h.") '
                'to fetch its verbatim text. Do not rely only on the pre-fetched context block.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'corpus_id': {
                        'type': 'string',
                        'enum': ['railway', 'polish_general_law'],
                        'description': 'Factory corpus from corpora/_registry.yaml (railway = EU→PL railway full picture; polish_general_law = KSH company law).',
                    },
                    'query': {
                        'type': 'string',
                        'description': 'Natural language question in PL/EN/FR (e.g. "Jakie warunki dla świadectwa maszynisty wg art. 22b?").',
                    },
                    'top_k': {
                        'type': 'integer',
                        'description': 'Number of chunks to retrieve (default 8, max 20).',
                    },
                    'filters': {
                        'type': 'object',
                        'description': 'Optional filters: {layer: L1|L2|L3, language: pl|en|fr, celex: 32007L0059}',
                    },
                    'rerank': {
                        'type': 'boolean',
                        'description': 'Cross-encoder rerank (slower, higher precision). Default false.',
                    },
                    'graph': {
                        'type': 'boolean',
                        'description': 'Graph expansion across L1→L2→L3 hierarchy. Default false.',
                    },
                },
                'required': ['corpus_id', 'query'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'bash',
            'description': (
                'Run a shell command with the project folder as the working directory. '
                'Use this to run Python scripts (e.g. `python3 scripts/foo.py`) or other '
                'commands that process project files. Relative paths resolve against the '
                'project root. Output is capped at 20,000 chars.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'command': {
                        'type': 'string',
                        'description': 'Shell command to run (cwd = project root).',
                    },
                    'timeout': {
                        'type': 'integer',
                        'description': 'Timeout in seconds (default 120).',
                    },
                },
                'required': ['command'],
            },
        },
    },
]


# ── Tool dispatcher ────────────────────────────────────────────────────────────

def dispatch(tool_name: str, tool_args: dict, project_path: str) -> str:
    """
    Execute a tool call from the model and return the result as a JSON string.
    project_path is always injected by the caller — never from model args.
    """
    _log.info('tool call: %s args=%s', tool_name, tool_args)
    try:
        if tool_name == 'list_files':
            result = list_files(project_path, tool_args.get('directory', ''))
        elif tool_name == 'list_working_docs':
            result = list_working_docs(project_path)
        elif tool_name == 'read_file':
            result = read_file(project_path, tool_args['path'],
                               start_line=tool_args.get('start_line'),
                               end_line=tool_args.get('end_line'))
        elif tool_name == 'write_file':
            result = write_file(project_path, tool_args['path'], tool_args['content'])
        elif tool_name == 'patch_file':
            result = patch_file(project_path, tool_args['path'],
                                tool_args['old_string'], tool_args['new_string'])
        elif tool_name == 'bash':
            result = bash(project_path, tool_args.get('command', ''),
                          timeout=tool_args.get('timeout', 120))
        elif tool_name == 'get_project_memory':
            result = get_project_memory(
                project_path,
                tool_args.get('scope', 'project'),
                tool_args.get('phase_name', ''),
            )
        elif tool_name == 'web_fetch':
            result = web_fetch(
                tool_args['url'],
                max_chars=tool_args.get('max_chars', 20_000),
            )
        elif tool_name == 'rag_query':
            result = rag_query(
                corpus_id=tool_args.get('corpus_id', ''),
                query=tool_args.get('query', ''),
                top_k=tool_args.get('top_k', 8),
                filters=tool_args.get('filters'),
                rerank=tool_args.get('rerank', False),
                graph=tool_args.get('graph', False),
            )
        else:
            result = {'error': f'Unknown tool: {tool_name!r}'}
    except KeyError as e:
        result = {'error': f'Missing required argument: {e}'}
    except Exception as e:
        _log.error('tool dispatch error: %s', e)
        result = {'error': str(e)}

    return json.dumps(result, ensure_ascii=False)
