"""
File classification engine for the Files browser.

Every file in a project is categorized into one of nine buckets so the UI can
group, filter, and hide intelligently. Categories (in priority order):

    system        — bookkeeping (hidden by default; toggled by "Show system
                    files")
    definition    — project definition layer (READMEFIRST.md / CLAUDE.md,
                    GUIDE.md, Skills.md, master-spec.json)
    memory        — accumulated knowledge (*.memory.md in Artifacts/)
    chat          — chat transcripts (Artifacts/chats/*.chat.md)
    output        — raw per-execution output (Artifacts/outputs/exec-N-output.md)
    reference     — user reference materials (any Working-Docs folder variant)
    code          — code that was produced by a task execution (Code category
                    shows *only* task outputs, never the user's pre-existing
                    source tree)
    deliverable   — files produced by task executions (anything in a task's
                    git_diffstat that isn't code or in the buckets above —
                    reports, spreadsheets, scripts, data, …)
    archive       — legacy files whose role is unclear (memory/ folders from
                    pre-chats consolidation, session-*.chat.md at Artifacts
                    root, stray TASKS.md / SETUP.md). Always visible.

Hidden rules live in HIDDEN_RULES; everything matching is 'system'. Code and
Deliverable attribution is sourced from git_diffstat via attribution_map().
"""

import os
import re
from datetime import datetime


# ── Shared constants ──────────────────────────────────────────────────────────

WORKING_DOC_FOLDERS = ('My Docs', 'Working Docs', 'Working Documents',
                       'working-docs', 'docs')

DEFINITION_FILES = {
    'READMEFIRST.md', 'CLAUDE.md', 'GUIDE.md', 'Skills.md',
    'master-spec.json',  # canonical path: Artifacts/master-spec.json
}

# Browser-native renderable extensions (raw /files/ stream is fine).
RENDERABLE_EXTS = {
    '.md', '.markdown', '.txt', '.log', '.html', '.htm',
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp',
    '.pdf', '.csv', '.json', '.xml', '.yaml', '.yml',
    '.css', '.js', '.ts', '.py', '.sh',  # code in browser is fine for quick view
}

# Office formats that need server-side text extraction via /api/preview.
NON_RENDERABLE_EXTS = {
    '.docx', '.doc', '.xlsx', '.xls', '.pptx', '.ppt',
    '.odt', '.ods', '.odp',
}

# Extensions of "code" files (used by Code category + agent_git.
# looks_like_software). Mirrors agent_git.CODE_EXTS but lives here so the
# frontend/backend share the same definition structurally.
CODE_EXTS = {
    '.py', '.js', '.ts', '.jsx', '.tsx', '.go', '.rs', '.java', '.rb', '.php',
    '.c', '.cpp', '.cc', '.h', '.hpp', '.cs', '.swift', '.kt', '.scala', '.sh',
    '.html', '.css', '.scss', '.vue', '.svelte', '.sql', '.dart', '.lua',
}

# Legacy file patterns that go to Archive (always visible).
ARCHIVE_RELPATTERNS = (
    re.compile(r'^memory(/|$)'),                         # legacy memory/ folders
    re.compile(r'^Artifacts/session-.*\.chat\.md$'),     # legacy chats at Artifacts root
    re.compile(r'^TASKS\.md$'),
    re.compile(r'^SETUP\.md$'),
    re.compile(r'^project_instructions\.md$'),
)

# ── Hidden / System rules ─────────────────────────────────────────────────────
# Anything matching ANY of these is 'system' (hidden by default). Match by
# basename, extension, or directory prefix.

HIDDEN_EXTS = {
    '.bak', '.tmp', '.pyc', '.pyo', '.pyd', '.swp', '.swo',
    '.log.bak', '.lock', '.gitkeep',
}
HIDDEN_BASENAMES = {
    'aingel.json', '.gitignore', '.env', '.env.example',
    '.DS_Store', 'Thumbs.db',
}
HIDDEN_BASENAME_PREFIXES = ('project.db', '.~lock.')
HIDDEN_DIRS = {
    '.git', '.vibe', '.claude', '.agents', '.codex', '.ipynb_checkpoints',
    '__pycache__', 'node_modules', '.venv', 'venv', '.mypy_cache',
    '.pytest_cache', '.ruff_cache', 'dist', 'build', '.next', '.turbo',
    '.trash', '.uploads',
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_hidden(rel_path: str, name: str) -> bool:
    """Return True if the file matches a hidden/system rule."""
    # Directory prefix
    parts = rel_path.split('/')
    for d in parts[:-1]:
        if d in HIDDEN_DIRS:
            return True
    # Basename exactly
    if name in HIDDEN_BASENAMES:
        return True
    # Basename starts with project.db / .~lock.
    for p in HIDDEN_BASENAME_PREFIXES:
        if name.startswith(p):
            return True
    # Extension
    _, ext = os.path.splitext(name)
    if ext.lower() in HIDDEN_EXTS:
        return True
    # Hidden by name (anywhere on disk starts with '.')
    if name.startswith('.') and name not in ('.', '..'):
        return True
    return False


def _relpath(project_path: str, abs_path: str) -> str:
    """Return repo-relative path (forward slashes)."""
    return os.path.relpath(abs_path, project_path).replace(os.sep, '/')


def _is_under(rel: str, folder: str) -> str | None:
    """If rel is directly under folder (or its descendants), return the
    sub-path after folder; else None. Matches literal folder name only so
    'Working Docs' is not ambiguous with 'Working Documents'."""
    if rel == folder:
        return ''
    if rel.startswith(folder + '/'):
        return rel[len(folder) + 1:]
    return None


# ── Attribution map ────────────────────────────────────────────────────────────

_DIFFSTAT_LINE = re.compile(r'^\s*(?:\.\.\./)?(.+?)\s+\|')


def _expand_diffstat_rename(raw: str) -> list[str]:
    """Expand a git diff --stat filename that may contain a rename.

    git --stat abbreviates renames as `old => new` and, when prefix/suffix
    are shared, as `prefix/{old => new}/suffix` (e.g. `{a => b}` or
    `Working Docs/{DU202433000-sig.md => DPP-WOPN.717.10.2024.AC.md}`).
    Returns [new] or [old, new] expanded paths; for non-rename lines returns
    [raw]. The caller should link the NEW name (last element).
    """
    s = (raw or '').strip().replace('.../', '')
    if not s:
        return []
    if ' => ' not in s:
        return [s]
    # Brace expansion: prefix/{old => new}/suffix
    if '{' in s and '}' in s:
        try:
            open_idx = s.index('{')
            close_idx = s.index('}', open_idx)
            inside = s[open_idx + 1:close_idx]
            if ' => ' in inside:
                prefix = s[:open_idx]
                suffix = s[close_idx + 1:]
                old_part, new_part = inside.split(' => ', 1)
                old = (prefix + old_part.strip() + suffix).strip().replace('.../', '')
                new = (prefix + new_part.strip() + suffix).strip().replace('.../', '')
                out: list[str] = []
                if old:
                    out.append(old)
                if new:
                    out.append(new)
                return out if out else [s]
        except Exception:
            pass
    # Plain `old => new` without braces
    try:
        old, new = s.split(' => ', 1)
        old = old.strip().replace('.../', '')
        new = new.strip().replace('.../', '')
        out = []
        if old:
            out.append(old)
        if new:
            out.append(new)
        return out if out else [s]
    except Exception:
        return [s]


def attribution_map(project_path: str, project_id: int) -> dict:
    """Return {rel_path: {'task_id', 'exec_id', 'merged_at'}} by parsing each
    done execution's git_diffstat. Used by classify_file() to mark Code and
    Deliverable files as task outputs.

    Falls back to nothing if the project has no project.db (shouldn't happen).
    """
    import agent_db as _db
    pconn = _db.get_project_db(project_path)
    try:
        rows = pconn.execute(
            "SELECT id, task_id, git_diffstat, finished_at, git_merge_commit "
            "FROM executions "
            "WHERE status='done' AND git_diffstat IS NOT NULL "
            "AND git_diffstat != '' "
            "ORDER BY finished_at DESC"
        ).fetchall()
    except Exception:
        try:
            pconn.close()
        except Exception:
            pass
        return {}

    out: dict = {}
    for r in rows:
        exec_id = r['id']
        task_id = r['task_id']
        finished_at = r['finished_at']
        diffstat = r['git_diffstat'] or ''
        for line in diffstat.split('\n'):
            m = _DIFFSTAT_LINE.match(line)
            if not m:
                continue
            rel = m.group(1).strip()
            rel = rel.replace('.../', '')
            if not rel:
                continue
            # Handle rename lines: expand `{a => b}` / `a => b` into real paths.
            # Attribute the NEW name (last element) and, for completeness, also the old.
            expanded = _expand_diffstat_rename(rel)
            for ep in expanded:
                if not ep:
                    continue
                if ep not in out:
                    out[ep] = {
                        'task_id': task_id,
                        'exec_id': exec_id,
                        'finished_at': finished_at,
                    }
    try:
        pconn.close()
    except Exception:
        pass
    return out


# ── Classification ────────────────────────────────────────────────────────────

_OUTPUT_FOLDER_RE = re.compile(r'^Artifacts/outputs/(\d+)-[^/]+/')


def task_id_from_output_folder(rel: str):
    """Task id owning a file in Artifacts/outputs/<id>-<slug>/, else None.

    Fallback attribution for deliverables no task commit claims (e.g. picked
    up by a checkpoint commit, or git disabled for the project).
    """
    m = _OUTPUT_FOLDER_RE.match(rel or '')
    return int(m.group(1)) if m else None


def classify_file(project_path: str, abs_path: str,
                  attributions: dict | None = None) -> dict | None:
    """Classify a single file. Returns None if the file should not be listed
    (e.g. inside a hidden directory we know to skip), or a dict otherwise.

    Dict shape::

        {
            'name': str,
            'path': str,          # absolute
            'rel': str,           # repo-relative, forward slashes
            'ext': str,           # lowercase, with leading dot, '' if none
            'size': int,
            'modified': iso8601,
            'category': 'memory' | 'code' | ...,
            'hidden': bool,
            'task_id': int | None,
            'exec_id': int | None,
        }
    """
    if not os.path.isfile(abs_path):
        return None
    name = os.path.basename(abs_path)
    rel = _relpath(project_path, abs_path)
    # Hidden files are still listed (they're useful in their own category)
    hidden = _is_hidden(rel, name)
    _, ext_raw = os.path.splitext(name)
    ext = ext_raw.lower()
    try:
        st = os.stat(abs_path)
    except OSError:
        return None

    category = _categorize(rel, name, ext, attributions)
    attr = (attributions or {}).get(rel) or {}
    task_id = attr.get('task_id')
    if task_id is None and category in ('deliverable', 'output'):
        task_id = task_id_from_output_folder(rel)

    return {
        'name': name,
        'path': abs_path,
        'rel': rel,
        'ext': ext,
        'size': st.st_size,
        'modified': datetime.fromtimestamp(st.st_mtime).isoformat(),
        'category': category,
        'hidden': hidden,
        'task_id': task_id,
        'exec_id': attr.get('exec_id'),
    }


def _categorize(rel: str, name: str, ext: str, attributions: dict | None) -> str:
    """Return the category name for a file."""

    # 1. System (hidden) — short-circuit before any other check
    if _is_hidden(rel, name):
        return 'system'

    # 2. Definition
    if rel in ('READMEFIRST.md', 'CLAUDE.md', 'GUIDE.md', 'Skills.md'):
        return 'definition'
    if rel == 'Artifacts/master-spec.json':
        return 'definition'

    # 3. Memory
    if rel.startswith('Artifacts/') and rel.endswith('.memory.md'):
        return 'memory'

    # 4. Chat (canonical)
    if rel.startswith('Artifacts/chats/') and rel.endswith('.chat.md'):
        return 'chat'

    # 5. Output (per-execution raw response)
    if rel.startswith('Artifacts/outputs/') and name.startswith('exec-') \
            and name.endswith('-output.md'):
        return 'output'

    # 6. Reference (any Working-Docs folder variant wins regardless of writer)
    for folder in WORKING_DOC_FOLDERS:
        if _is_under(rel, folder) is not None:
            return 'reference'

    # 7. Code (only if file is a task output AND extension is in CODE_EXTS)
    if attributions and ext in CODE_EXTS and rel in attributions:
        return 'code'

    # 8. Deliverable (task output, anything else)
    if attributions and rel in attributions:
        return 'deliverable'

    # 9. Archive (legacy orphans)
    for pat in ARCHIVE_RELPATTERNS:
        if pat.match(rel):
            return 'archive'

    # 10. Otherwise → unclassified (treat as deliverable so the user sees
    #     it; the UI can offer "hide" if they want)
    return 'deliverable'


# ── Listing ───────────────────────────────────────────────────────────────────

_MAX_DEPTH = 4
_MAX_FILES = 5000


def list_project_files(project_path: str, project_id: int,
                       depth: int = _MAX_DEPTH) -> list:
    """Walk the project tree and return classified files.

    Safety: stays inside project_path, caps depth, skips symlinks pointing
    outside, skips hidden dirs (so we don't enumerate node_modules etc.).
    """
    if not project_path or not os.path.isdir(project_path):
        return []

    attributions = attribution_map(project_path, project_id)
    # File tags enrichment (per-project DB) — best-effort, backward compat
    tags_map = {}
    try:
        import agent_db as _db
        tags_map = _db.get_file_tags(project_path) or {}
    except Exception:
        tags_map = {}
    real_root = os.path.realpath(project_path)
    out: list = []
    skipped_dirs = HIDDEN_DIRS

    def _walk(rel: str, depth_remaining: int) -> None:
        if len(out) >= _MAX_FILES:
            return
        abs_path = os.path.join(real_root, rel.replace('/', os.sep))
        try:
            entries = sorted(os.listdir(abs_path),
                             key=lambda n: (not os.path.isdir(os.path.join(abs_path, n)),
                                            n.lower()))
        except (PermissionError, OSError):
            return
        for entry in entries:
            if len(out) >= _MAX_FILES:
                return
            child_abs = os.path.join(abs_path, entry)
            # Skip anything that resolves outside the project root
            if os.path.islink(child_abs):
                real = os.path.realpath(child_abs)
                if not real.startswith(real_root + os.sep):
                    continue
            if entry.startswith('.') and entry not in ('.', '..'):
                # Always recurse into the well-known Working-Docs variants
                # even if their name starts with a dot (none do, but for
                # forward compat).
                pass
            child_rel = (rel + '/' + entry) if rel else entry
            if os.path.isdir(child_abs):
                if entry in skipped_dirs:
                    continue
                if depth_remaining <= 0:
                    continue
                _walk(child_rel, depth_remaining - 1)
            elif os.path.isfile(child_abs):
                info = classify_file(project_path, child_abs, attributions)
                if info is not None:
                    # Enrich with file_tags if present
                    tag_entry = tags_map.get(child_rel)
                    if tag_entry:
                        info['tags'] = tag_entry.get('tags', [])
                        info['note'] = tag_entry.get('note', '')
                        info['tags_updated_at'] = tag_entry.get('updated_at', '')
                    else:
                        info['tags'] = []
                        info['note'] = ''
                    out.append(info)

    _walk('', depth)
    return out


def list_project_folders(project_path: str, project_id: int,
                         depth: int = _MAX_DEPTH) -> list:
    """Return folder rels that are descendants of WORKING_DOC_FOLDERS variants.

    Used to surface empty folders in the Files browser / move-copy picker.
    Mirrors list_project_files walk but collects dir rels instead of files.
    Only dirs under a WORKING_DOC_FOLDERS variant (including the variant
    itself) are tracked. Skips HIDDEN_DIRS, dot-prefixed entries, and
    symlinked-outside entries, and respects depth cap.
    """
    if not project_path or not os.path.isdir(project_path):
        return []
    real_root = os.path.realpath(project_path)
    skipped_dirs = HIDDEN_DIRS
    dirs_seen: set[str] = set()

    def _is_writable_dir(rel: str) -> bool:
        for v in WORKING_DOC_FOLDERS:
            if _is_under(rel, v) is not None:
                return True
        return False

    def _walk(rel: str, depth_remaining: int) -> None:
        abs_path = os.path.join(real_root, rel.replace('/', os.sep))
        try:
            entries = sorted(os.listdir(abs_path),
                             key=lambda n: (not os.path.isdir(os.path.join(abs_path, n)),
                                            n.lower()))
        except (PermissionError, OSError):
            return
        for entry in entries:
            child_abs = os.path.join(abs_path, entry)
            # Skip symlink that resolves outside project root
            if os.path.islink(child_abs):
                try:
                    real = os.path.realpath(child_abs)
                except Exception:
                    continue
                if not real.startswith(real_root + os.sep) and real != real_root:
                    continue
            # Skip hidden dot-entries (files and dirs). The original file walk
            # has a no-op pass here; for folders we actually skip dot dirs so
            # ".hidden" folders don't appear as writable targets.
            if entry.startswith('.'):
                continue
            child_rel = (rel + '/' + entry) if rel else entry
            if os.path.isdir(child_abs):
                if entry in skipped_dirs:
                    continue
                # Collect before depth check so leaf dirs at depth limit still appear
                if _is_writable_dir(child_rel):
                    dirs_seen.add(child_rel)
                if depth_remaining <= 0:
                    continue
                _walk(child_rel, depth_remaining - 1)

    _walk('', depth)
    return sorted(dirs_seen)


# ── Convenience: group by task ─────────────────────────────────────────────────

def group_deliverables_by_task(files: list) -> list:
    """Return a list of {task_id, exec_id, task_title, finished_at, files:[...]}
    for task-attributable files, recent-first. Files without attribution are
    grouped at the end under task_id=None with title 'Other files'."""
    by_task: dict = {}
    ungrouped: list = []
    for f in files:
        if f['category'] not in ('deliverable', 'code', 'output'):
            continue
        if f.get('task_id') is None:
            ungrouped.append(f)
            continue
        key = f['task_id']
        bucket = by_task.setdefault(key, {
            'task_id': key,
            'exec_id': f.get('exec_id'),
            'task_title': None,
            'finished_at': f.get('modified'),
            'files': [],
        })
        bucket['files'].append(f)

    groups = list(by_task.values())
    groups.sort(key=lambda g: (g['finished_at'] or ''), reverse=True)
    if ungrouped:
        groups.append({
            'task_id': None,
            'exec_id': None,
            'task_title': 'Other files (no task attribution)',
            'finished_at': None,
            'files': ungrouped,
        })
    return groups
