"""
Task executor — Phase 2/3/4.
Real AI calls via agent_router. Memory-aware prompt building.
Two execution paths:
  • run_task()    — full one-shot task execution (legacy)
  • chat_reply()  — single message in a chat (Phase 4)
"""
import json
import logging
import os
from datetime import datetime
import agent_db as db
import agent_files
import agent_config
from agent_config import MODELS, DEFAULT_MODEL, get_pricing, resolve_def_filename, get_def_files_for_project
from agent_router import route, _summarize_permission_denials, ExecutionCancelledError, VIBE_TURN_LIMIT_MARKER
from agent_memory import read_memory, phase_from_task, list_working_docs
import agent_chats as chats
from agent_skills import skills_context_summary
import threading as _threading
import agent_git as agit
import re as _linkify_re
import urllib.parse as _linkify_urlparse


def _start_heartbeat(exec_id, project_path=None):
    """Watchdog thread that touches last_heartbeat_at every 30s for a live execution.
    Returns a stop() closure. Safe to call from run_task / chat_reply."""
    _stop = _threading.Event()

    def _loop():
        while not _stop.wait(30):
            try:
                db.touch_execution_heartbeat(exec_id, project_path=project_path)
            except Exception:
                pass

    _threading.Thread(target=_loop, daemon=True, name=f'hb-{exec_id}').start()

    def _stop_fn():
        _stop.set()

    return _stop_fn

_log = logging.getLogger(__name__)

# One lock per project path. Two runs in the same project must not overlap:
# their git checkpoint/stash/checkout would mix up or discard each other's
# edits. Keyed by realpath so symlinked paths share a lock.
_PROJECT_LOCKS: dict = {}
_PROJECT_LOCKS_GUARD = _threading.Lock()


def _project_lock(project_path):
    key = os.path.realpath(project_path) if project_path else ''
    with _PROJECT_LOCKS_GUARD:
        lock = _PROJECT_LOCKS.get(key)
        if lock is None:
            lock = _threading.Lock()
            _PROJECT_LOCKS[key] = lock
        return lock


ATTACHMENT_CHAR_CAP = 30000  # per-attachment cap to keep prompts sane

# ── F10: linkify exec outputs with clickable file URLs ────────────────────────
_LINKIFY_NON_RENDERABLE_EXTS = {
    '.docx', '.doc', '.xlsx', '.xls', '.pptx', '.ppt',
    '.odt', '.ods', '.odp',
}
# Mirror agent_filecat.NON_RENDERABLE_EXTS if available at runtime
try:
    from agent_filecat import NON_RENDERABLE_EXTS as _FC_NON_RENDER
    if _FC_NON_RENDER:
        _LINKIFY_NON_RENDERABLE_EXTS = set(_FC_NON_RENDER)
except Exception:
    pass

_DIFFSTAT_LINKIFY_LINE = _linkify_re.compile(r'^\s*(?:\.\.\./)?(.+?)\s+\|')


def _expand_diffstat_rename_linkify(raw: str) -> list[str]:
    """Expand git diff --stat rename `old => new` / `{a => b}` into real paths.
    Mirrors agent_filecat._expand_diffstat_rename."""
    s = (raw or '').strip().replace('.../', '')
    if not s:
        return []
    if ' => ' not in s:
        return [s]
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


def _parse_diffstat_rels(diffstat: str) -> list[str]:
    """Parse git_diffstat text into repo-relative paths using _DIFFSTAT_LINKIFY_LINE."""
    if not diffstat:
        return []
    rels: list[str] = []
    for line in diffstat.split('\n'):
        m = _DIFFSTAT_LINKIFY_LINE.match(line)
        if not m:
            continue
        rel = m.group(1).strip()
        # Strip git abbreviation '...' (with or without trailing slash) and any quotes
        rel = rel.lstrip('...').lstrip('.../').strip().strip('"').strip("'")
        # Reject octal-escaped quoted paths (git core.quotepath) — they contain \ooo escapes, not valid FS rels
        if '\\' in rel:
            continue
        if not rel:
            continue
        # Strip leading ... after above (covers bare '...' prefix)
        rel = rel.lstrip('.')
        rel = rel.strip()
        if not rel:
            continue
        expanded = _expand_diffstat_rename_linkify(rel)
        # For renames, keep only the NEW name (last element) — old is 404 and wastes 20-cap
        if len(expanded) == 2:
            rels.append(expanded[-1].strip())
        else:
            for ep in expanded:
                ep = ep.strip().strip('"').strip("'")
                if ep and '\\' not in ep:
                    rels.append(ep)
    return rels


def _scan_created_rels_from_text(text: str, project_path: str) -> list[str]:
    """Scan output text for absolute project_path occurrences and return rels.
    Handles spaces in folder names (e.g. Working Documents) by anchoring to file extensions.
    Uses per-occurrence slicing to avoid spanning across two base paths."""
    if not text or not project_path:
        return []
    base = project_path.rstrip('/')
    if not base:
        return []
    rels: list[str] = []
    _exts = r'(?:csv|md|markdown|txt|log|html?|pdf|json|xml|yaml|yml|css|js|ts|py|sh|png|jpe?g|gif|webp|svg|bmp|docx?|xlsx?|pptx?|odt|ods|odp)'
    # Find each base occurrence and extract rel that follows it
    for m_base in _linkify_re.finditer(_linkify_re.escape(base), text):
        start = m_base.end()
        # require a slash after base
        if start >= len(text) or text[start] != '/':
            continue
        start += 1  # skip '/'
        # Take up to next base occurrence or up to 300 chars / newline/quote boundary
        # Look ahead slice
        slice_end = min(len(text), start + 400)
        # If another base occurs within slice, cut before it
        next_base_idx = text.find(base, start)
        if next_base_idx != -1 and next_base_idx < slice_end:
            slice_end = next_base_idx
        substr = text[start:slice_end]
        # Truncate at newline or closing quote/parens that likely ends the path
        # For file pattern, we want to capture up to extension; for dir, up to whitespace
        # Try file pattern first (allow spaces)
        m_file = _linkify_re.search(r'^([^\n\"\'`\)\]]+?\.' + _exts + r')\b', substr, _linkify_re.I)
        if m_file:
            rel = m_file.group(1).strip()
            rel = rel.rstrip('.,;:)]}"\'`')
            rel = rel.lstrip('./')
            if rel:
                rel = rel.replace('\\', '/')
                if rel not in rels:
                    rels.append(rel)
            continue
        # Fallback for dir or extensionless (e.g. Data/)
        m_dir = _linkify_re.search(r'^([^\s\"\'`\)\]\n]+)', substr)
        if m_dir:
            rel = m_dir.group(1).strip()
            rel = rel.rstrip('.,;:)]}"\'`')
            rel = rel.lstrip('./')
            if rel:
                rel = rel.replace('\\', '/')
                if rel not in rels:
                    rels.append(rel)
    # Deduplicate preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for r in rels:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


def _linkify_output_with_files(content: str, project_path: str, pid: int, created_rels: list[str]) -> str:
    """Append clickable links for created files to exec output markdown.

    Idempotent: if content already contains '## Files created in this run', returns unchanged.
    Caps at 20, deduped, per-segment encodeURIComponent, non-renderable -> /api/preview.
    Validates existence before emitting (bare dirs and .. rejected, must be file on disk).
    """
    if not content or "## Files created in this run" in content:
        return content
    if not created_rels or pid is None:
        return content
    seen: set[str] = set()
    uniq: list[str] = []
    for rel in created_rels:
        if not rel:
            continue
        r = str(rel).strip().replace('\\', '/').lstrip('/')
        # Filter garbage: octal escapes, bare ..., trailing quotes, .., bare dirs
        if not r or r in seen:
            continue
        if '\\' in r or r.startswith('...'):
            continue
        if '..' in r.split('/'):
            continue
        # Reject bare-directory rels (no slash or trailing slash with no file, extensionless dir-like)
        # Require at least one path component that looks like a file (has dot) OR validate existence below
        seen.add(r)
        uniq.append(r)
        if len(uniq) >= 40:  # over-collect, will filter to 20 after isfile check
            break
    # Validate existence on disk (or basename fallback for moved files) before emitting
    validated: list[str] = []
    for r in uniq:
        abs_path = os.path.join(project_path, r) if project_path else None
        if abs_path and os.path.isfile(abs_path):
            validated.append(r)
            if len(validated) >= 20:
                break
            continue
        # Basename fallback for files that were moved (e.g. OCR_mistral/ -> Working Documents/OCR_mistral/)
        base = os.path.basename(r)
        if base and project_path:
            found = None
            for root, _, files in os.walk(project_path):
                if base in files:
                    # Prefer a match under Working Documents or same suffix depth
                    rel_found = os.path.relpath(os.path.join(root, base), project_path).replace(os.sep, '/')
                    if rel_found.endswith(r) or base == rel_found.split('/')[-1]:
                        found = rel_found
                        break
                    if found is None:
                        found = rel_found
            if found:
                # Avoid duplicating if basename already in validated via different rel
                if found not in validated and found not in seen:
                    seen.add(found)
                validated.append(found)
                if len(validated) >= 20:
                    break
                continue
        # Skip rels that don't exist anywhere (404) — do not link garbage
    if not validated:
        return content
    lines: list[str] = []
    for rel in validated[:20]:
        label = rel
        encoded = '/'.join(_linkify_urlparse.quote(seg, safe='') for seg in rel.split('/'))
        ext = os.path.splitext(rel)[1].lower()
        if ext in _LINKIFY_NON_RENDERABLE_EXTS:
            url = f"/api/preview/{pid}/{encoded}"
        else:
            url = f"/files/{pid}/{encoded}"
        lines.append(f"- [{label}]({url})")
    suffix = "\n\n## Files created in this run\n" + "\n".join(lines) + "\n"
    if content.endswith("\n"):
        return content.rstrip("\n") + suffix
    return content + suffix


def _safe_resolve(project_path, *parts):
    """Join path components and verify the result stays inside project_path.
    Returns the resolved absolute path, or None if it escapes the project.
    """
    target = os.path.realpath(os.path.join(project_path, *parts))
    root = os.path.realpath(project_path)
    if not (target == root or target.startswith(root + os.sep)):
        return None
    return target

MAX_TOKENS   = 8192
CONTEXT_LINES = 60
CHAT_CONTEXT_CHARS = 12000
HANDOFF_CONTEXT_CHARS = 12000
HANDOFF_MEMORY_CHARS = 4000
HANDOFF_MAX_TOKENS = 1200

# Scaleway HTTP body cap: devstral (and likely other scw endpoints) reject bodies
# over ~40 kB with a spurious "Unterminated string" 400 error.  Since scw-* models
# have the get_project_memory tool they can fetch memory on demand; keep the injected
# excerpt short so the base body stays well under the limit.
SCW_PHASE_MEM_CAP = 4000   # chars injected into prompt; model calls tool for the rest
SCW_PROJ_MEM_CAP  = 5000

# Substrings that mean "you blew through your subscription window" rather than
# "the task itself is broken". Covers Claude CLI stderr, Anthropic SDK 429s,
# and Vibe CLI quota errors. Match is case-insensitive.
_QUOTA_ERROR_PATTERNS = (
    'rate_limit', 'rate limit', 'ratelimit',
    '429', 'too many requests',
    '5-hour limit', 'usage limit', 'usage_limit',
    'quota exceeded', 'quota_exceeded', 'out of credit',
    'subscription limit', 'plan limit',
    'overloaded_error',  # Anthropic returns this when capacity is exhausted
)


def _looks_like_quota_error(msg):
    if not msg:
        return False
    lo = msg.lower()
    return any(p in lo for p in _QUOTA_ERROR_PATTERNS)


def _load_project_context(project_path):
    if not project_path:
        return ''
    def_file = os.path.join(project_path, resolve_def_filename(project_path))
    if not os.path.exists(def_file):
        return ''
    try:
        with open(def_file, encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()[:CONTEXT_LINES]
        return ''.join(lines).strip()
    except Exception:
        return ''


def _find_project_path(task):
    conn = db.get_db()
    row = conn.execute('SELECT path FROM projects WHERE id=?', (task['project_id'],)).fetchone()
    conn.close()
    return row['path'] if row else ''


def _chat_tools_enabled(model_id, project_path):
    """Decide whether the chat path will *actually* give the model real tools.

    The chat prompt builder advertises read_file/list_files/write_file to the
    model based on `model_capabilities(model_id)['file_access']`. That capability
    reflects what the provider *can* do, not what `chat_reply`'s `route()` call
    will actually wire up. Advertising tools that aren't bound to the API call
    makes the model emit the tool invocation as plain text (a "simulation gap")
    and the turn ends with nothing executed — the user sees an apparent hang.

    This helper mirrors the real routing decisions in `agent_router.route()` so
    the prompt and the API call agree:
      - Anthropic: always agentic (claude CLI path, both auth modes have tools).
      - Mistral: agentic only if the effective mode resolves to 'vibe' *and* the
        vibe CLI is installed *and* the model is in VIBE_MODELS. Gracefully
        degrades to text-only otherwise (no tools advertised).
      - Scaleway / Ollama: agentic whenever project_path is set (the OpenAI-
        compatible tool loop binds tools whenever project_path is a real dir).
      - Others (openai, google, scw_deploy): text-only in the chat path.
    """
    if not project_path or not os.path.isdir(project_path):
        return False
    m = MODELS.get(model_id) or {}
    provider = (m.get('provider')
                or (model_id.split('-', 1)[0] if model_id else ''))
    if model_id.startswith('claude-'):
        provider = 'anthropic'
    elif model_id.startswith(('mistral-', 'open-mistral')):
        provider = 'mistral'
    elif model_id.startswith('scw-') and not model_id.startswith('scw-dep-'):
        provider = 'scaleway'
    elif model_id.startswith('oll-'):
        provider = 'ollama'
    elif model_id.startswith('scw-dep-'):
        provider = 'scw_deploy'

    if provider == 'anthropic':
        return True
    if provider == 'mistral':
        try:
            from agent_config import MISTRAL_MODE, VIBE_MODELS
        except Exception:
            return False
        if model_id not in VIBE_MODELS:
            return False
        # Mirrors call_mistral's vibe-CLI presence check (agent_router.py:541-544).
        import shutil
        vibe_bin = shutil.which('vibe') or os.path.join(
            os.path.expanduser('~'), '.local', 'bin', 'vibe')
        if not (os.path.isfile(vibe_bin) or shutil.which('vibe')):
            return False
        return MISTRAL_MODE == 'vibe'
    if provider in ('scaleway', 'ollama'):
        return True
    return False


def _task_slug(title, maxlen=40):
    """Slugify task title for outputs isolation (lowercase, hyphens, truncated)."""
    import re
    s = re.sub(r'[^a-z0-9]+', '-', (title or '').lower()).strip('-')
    s = s[:maxlen].rstrip('-')
    return s or 'task'


def task_output_dir(task, project_path=None):
    """Project-relative folder for a task's deliverables and exec logs.

    Rule: a task's output files never go in the project root or Working Docs
    (the user's reference material) — they go in Artifacts/outputs/<id>-<slug>/,
    which every later task can read. The task number comes first so folders
    sort and identify by task; the folder is found by that number, so renaming
    the task later does not split its outputs across two folders.
    """
    task = task or {}
    tid = task.get('id')
    slug = _task_slug(task.get('title') or '')
    if tid is None:
        return os.path.join('Artifacts', 'outputs', slug)
    if project_path:
        base = os.path.join(project_path, 'Artifacts', 'outputs')
        try:
            for entry in sorted(os.listdir(base)):
                if entry.startswith(f'{tid}-') and os.path.isdir(os.path.join(base, entry)):
                    return os.path.join('Artifacts', 'outputs', entry)
        except OSError:
            pass
    return os.path.join('Artifacts', 'outputs', f'{tid}-{slug}')


# Project-root files a run may legitimately create or rewrite in place.
_ROOT_KEEP = frozenset({'READMEFIRST.md', 'CLAUDE.md', 'GUIDE.md', 'Skills.md',
                        'aingel.json', 'master-spec.json'})


def _root_files_snapshot(project_path):
    """Names of the regular files directly in the project root."""
    try:
        return {e.name for e in os.scandir(project_path) if e.is_file(follow_symlinks=False)}
    except OSError:
        return set()


def _relocate_new_root_files(project_path, before, out_rel):
    """Move files a run created in the project root into its output folder.

    CLI agents (Vibe, Claude Code) write straight to disk, so a task text that
    says "save in the project root" used to leave deliverables where the
    prompt builder, H2a and the Files view don't look (task #10001148).
    Definition files, hidden files and git-ignored files (e.g. a cron's daily
    digest landing mid-run) are left alone. Returns [(old_rel, new_rel)].
    """
    if not project_path or before is None:
        return []
    new = sorted(
        n for n in _root_files_snapshot(project_path) - before
        if n not in _ROOT_KEEP and not n.startswith('.') and not n.startswith('project.db')
    )
    if not new:
        return []
    ok, out, _ = agit._run(project_path, 'check-ignore', '--', *new)
    ignored = set(out.splitlines()) if out else set()
    moved = []
    dest_dir = os.path.join(project_path, out_rel)
    for name in new:
        if name in ignored:
            continue
        stem, ext = os.path.splitext(name)
        dest_name, n = name, 2
        while os.path.exists(os.path.join(dest_dir, dest_name)):
            dest_name = f'{stem}-{n}{ext}'
            n += 1
        try:
            os.makedirs(dest_dir, exist_ok=True)
            os.replace(os.path.join(project_path, name), os.path.join(dest_dir, dest_name))
            moved.append((name, os.path.join(out_rel, dest_name)))
        except OSError as e:
            _log.warning('could not relocate root file %s: %s', name, e)
    return moved


def _rescue_scratchpad_files(task, project_path, out_rel, since):
    """Rescue deliverables a CLI agent wrongly wrote to its scratchpad.

    The Vibe CLI gives the model a /tmp scratchpad; when a task asks to
    "create a file" without naming a project path, the model may write the
    deliverable there instead of the project — invisible to the user, the
    Exec Log, and follow-up tasks, and lost when /tmp is cleaned (task
    #10001176: 'and one more test.md' landed in /tmp/vibe-scratchpad-*/).
    If the task requested file creation but nothing appeared in its output
    folder, move files created in any scratchpad after run start into the
    output folder. Returns the list of new out_rel paths.
    """
    if not task or not project_path or not out_rel or since is None:
        return []
    if not _task_requests_file_creation(task):
        return []
    try:
        if _changed_output_rels(project_path, out_rel, since):
            return []  # deliverables landed correctly — nothing to rescue
    except Exception:
        return []
    import glob as _glob
    import shutil as _shutil
    dest_dir = os.path.join(project_path, out_rel)
    rescued = []
    for pad in _glob.glob('/tmp/vibe-scratchpad-*'):
        if not os.path.isdir(pad):
            continue
        try:
            names = sorted(os.listdir(pad))
        except OSError:
            continue
        for name in names:
            full = os.path.join(pad, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if not os.path.isfile(full) or st.st_mtime < since:
                continue
            stem, ext = os.path.splitext(name)
            dest_name, n = name, 2
            while os.path.exists(os.path.join(dest_dir, dest_name)):
                dest_name = f'{stem}-{n}{ext}'
                n += 1
            try:
                os.makedirs(dest_dir, exist_ok=True)
                _shutil.move(full, os.path.join(dest_dir, dest_name))
                rescued.append(os.path.join(out_rel, dest_name))
            except OSError as e:
                _log.warning('scratchpad rescue: could not move %s: %s', full, e)
    return rescued


def _changed_output_rels(project_path, out_rel, since):
    """Files in the task's output folder modified at/after ``since`` (exec logs excluded)."""
    if not project_path or not out_rel or since is None:
        return []
    base = os.path.join(project_path, out_rel)
    rels = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for name in sorted(files):
            if name.startswith('.') or (name.startswith('exec-') and name.endswith('-output.md')):
                continue
            full = os.path.join(root, name)
            try:
                if os.path.getmtime(full) >= since - 1:
                    rels.append(os.path.relpath(full, project_path).replace(os.sep, '/'))
            except OSError:
                pass
    return rels


def _output_file_path_new(project_path, exec_id, task_slug):
    """Lane B isolated path: Artifacts/outputs/<task folder>/exec-<id>-output.md"""
    slug = (task_slug or 'task').strip().strip('/')
    return os.path.join(project_path, 'Artifacts', 'outputs', slug, f'exec-{exec_id}-output.md')


def _find_output_file(project_path, exec_id):
    """Find exec output, supporting both legacy flat and Lane B slug layouts."""
    legacy = os.path.join(project_path, 'Artifacts', 'outputs', f'exec-{exec_id}-output.md')
    if os.path.exists(legacy):
        return legacy
    base = os.path.join(project_path, 'Artifacts', 'outputs')
    if not os.path.isdir(base):
        return None
    target = f'exec-{exec_id}-output.md'
    # quick one-level scan
    try:
        for entry in os.listdir(base):
            sub = os.path.join(base, entry)
            if os.path.isdir(sub):
                cand = os.path.join(sub, target)
                if os.path.exists(cand):
                    return cand
    except Exception:
        pass
    for root, _, files in os.walk(base):
        if target in files:
            return os.path.join(root, target)
    return None


def _output_file_path(project_path, exec_id):
    # Backward-compatible: if isolated file exists, return it, else legacy
    found = _find_output_file(project_path, exec_id) if project_path else None
    if found:
        return found
    return os.path.join(project_path, 'Artifacts', 'outputs', f'exec-{exec_id}-output.md')


def _read_full_output(project_path, exec_id, cap=HANDOFF_CONTEXT_CHARS):
    if not project_path or not exec_id:
        return ''
    output_file = _output_file_path(project_path, exec_id)
    if not os.path.exists(output_file):
        return ''
    try:
        with open(output_file, encoding='utf-8', errors='ignore') as f:
            text = f.read().strip()
    except Exception:
        return ''
    if len(text) > cap:
        return text[:cap] + f'\n\n[…truncated at {cap} chars…]'
    return text


def _write_full_output(project_path, exec_id, task, text, tok_in, tok_out, stale=False, git_diffstat=None):
    if not project_path:
        return None
    # Lane B: isolated outputs to the task's folder, Artifacts/outputs/<id>-<slug>/
    try:
        folder = os.path.basename(task.get('_output_dir') or task_output_dir(task, project_path))
    except Exception:
        folder = 'task'
    output_file = _output_file_path_new(project_path, exec_id, folder)
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    task_id = task.get('id', '?')
    task_title = task.get('title', '')
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(f'# Task #{task_id}: {task_title}\n\n')
        f.write(f'**Execution:** #{exec_id}  \n')
        f.write(f'**Task:** #{task_id}  \n')
        f.write(f'**Model:** {task["model"]}  \n')
        f.write(f'**Tokens:** {tok_in}↑ {tok_out}↓  \n\n')
        if stale:
            f.write('> ⚠️ **STALE RE-RUN**: A dependency of this task was approved before this execution. '
                    'Output reflects updated context.\n\n')
        f.write('---\n\n')
        f.write(text)
    # F10a — write-time linkify: append clickable file links for created files
    try:
        pid = task.get('project_id')
        if pid is None and project_path:
            try:
                _proj = db.get_project_by_path(project_path)
                if _proj:
                    pid = _proj.get('id')
            except Exception:
                pass
        if pid is not None:
            created_rels: list[str] = []
            if git_diffstat:
                created_rels.extend(_parse_diffstat_rels(git_diffstat))
            # Also scan output text for absolute project_path occurrences
            created_rels.extend(_scan_created_rels_from_text(text, project_path))
            if created_rels:
                with open(output_file, encoding='utf-8', errors='ignore') as _f:
                    cur = _f.read()
                new_cur = _linkify_output_with_files(cur, project_path, int(pid), created_rels)
                if new_cur != cur:
                    with open(output_file, 'w', encoding='utf-8') as _f:
                        _f.write(new_cur)
    except Exception as _e:
        _log.warning("linkify write-time failed for exec %s: %s", exec_id, _e)
    return output_file


def _cap_text(text, cap):
    text = (text or '').strip()
    if len(text) <= cap:
        return text
    return text[:cap] + f'\n\n[…truncated at {cap} chars…]'


def _strip_markdown_fence(text):
    text = (text or '').strip()
    if text.startswith('```'):
        lines = text.splitlines()
        if lines and lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        text = '\n'.join(lines).strip()
    return text


def _model_is_direct_text_api(model_id):
    """Models that always run through direct text APIs in SuperAgent."""
    model_id = (model_id or '').strip()
    if model_id.startswith(('gpt-', 'o1', 'o3', 'o4')) and not model_id.startswith('codex-'):
        return True
    # Scaleway now uses function calling for file access — NOT text-only
    return False


def _mistral_route_is_agentic(model_id, slot=None):
    """True when route() will actually use the Vibe CLI (real tools) for this
    Mistral model.

    This mirrors ``agent_router.call_mistral``'s effective-mode resolution so
    the prompt, the auto-switch and the detector all agree with what the API
    call really does::

        effective = 'vibe' if (MISTRAL_MODE == 'vibe' or slot in (2, 3)) else 'api'
        agentic   = effective == 'vibe' and model_id in VIBE_MODELS and CLI present

    The per-model ``VIBE_MODELS`` allowlist is the piece the executor used to
    ignore: a model outside it falls back to the text-only API *even when the
    CLI is installed*, so advertising tools for it produces fake tool markup
    and an empty deliverable (laptop task 20001131, mistral-medium/small-latest).
    """
    try:
        from agent_config import MISTRAL_MODE, VIBE_MODELS
    except Exception:
        return False
    mode = MISTRAL_MODE
    if slot in (2, 3):
        # run_task forces the Vibe path for the Mistral Pro / PAYG slots.
        mode = 'vibe'
    if mode != 'vibe':
        return False
    if model_id not in VIBE_MODELS:
        return False
    import shutil
    vibe_bin = shutil.which('vibe') or os.path.join(
        os.path.expanduser('~'), '.local', 'bin', 'vibe')
    return bool(os.path.isfile(vibe_bin) or shutil.which('vibe'))


def _task_uses_direct_text_route(task):
    """Return True only if the model truly cannot act on the filesystem.

    ``model_caps.tools`` marks API-level function-calling support, but some
    routes (Claude Code, Mistral Vibe, Codex) reach the filesystem through a
    local CLI and are fully agentic despite ``tools: false``.  The correct
    signal is ``file_access``:
        - "native" → CLI with built-in file/bash access  (agentic)
        - "bash"   → CLI running Bash                     (agentic *only* when the
                     model is actually routable through Vibe — see
                     ``_mistral_route_is_agentic``)
        - "tools"  → API function-calling                 (agentic)
        - missing/other → plain text API, no file access  (text-only)
    """
    model_id = ((task or {}).get('model') or '').strip()
    from model_caps import model_capabilities
    caps = model_capabilities(model_id)
    file_access = (caps.get('file_access') or '').strip().lower()
    if file_access == 'bash':
        return not _mistral_route_is_agentic(
            model_id, (task or {}).get('work_session_slot'))
    return file_access not in ('native', 'bash', 'tools')



import re as _re
_TOOL_MARKUP_PATTERNS = [
    _re.compile(r'<bash>.*?</bash>', _re.S | _re.I),
    _re.compile(r'<tool_code>.*?</tool_code>', _re.S | _re.I),
    _re.compile(r'<function_call>.*?</function_call>', _re.S | _re.I),
    _re.compile(r'<function=write_file>.*?</function>', _re.S | _re.I),
    _re.compile(r'<antml:invoke>.*?</antml:invoke>', _re.S | _re.I),
    _re.compile(r'<write_file>.*?</write_file>', _re.S | _re.I),
    # Pseudo-JSON tool syntax: bash{...} / write_file{...} with a brace body.
    _re.compile(r'\b(?:bash|write_file|read_file|list_files|edit_file)\s*\{[^{}]*\}', _re.S | _re.I),
    # Paired read tag (write tag already covered above; read needed on its own).
    _re.compile(r'<read_file>.*?</read_file>', _re.S | _re.I),
    _re.compile(r'```bash\s*.*?\s*```', _re.S | _re.I),
    _re.compile(r'```tool_code\s*.*?\s*```', _re.S | _re.I),
    _re.compile(r'```json\s*\{.*?"command".*?\}\s*```', _re.S | _re.I),
]
_PSEUDO_JSON_TOOL_RE = _TOOL_MARKUP_PATTERNS[6]


def _looks_like_unexecuted_tool_request(text):
    """Detect tool-call markup emitted as text instead of via real tool calls.

    Requires *paired* tags (open AND close) for each marker family so a stray
    trailing opening fragment -- which some API models emit as an artifact
    even after successfully executing tools -- does not false-positive.
    """
    if not text:
        return False
    lowered = text.lower()
    pairs = (
        ('<bash>', '</bash>'),
        ('<tool_code>', '</tool_code>'),
        ('<function_call>', '</function_call>'),
        ('<function=write_file>', '</function>'),
        ('<antml:invoke>', '</antml:invoke>'),
        # GLM emits <tool_call>...</tool_call> when it still wants a tool but the
        # loop has already forced a final-text call (exec 694 / task #682).
        ('<tool_call>', '</tool_call>'),
        # Mistral text-API dialect observed on laptop task 20001131: paired
        # read/write tags plus pseudo-JSON "bash{...}" blocks.
        ('<read_file>', '</read_file>'),
        ('<write_file>', '</write_file>'),
    )
    if any(o in lowered and c in lowered for o, c in pairs):
        return True
    return bool(_PSEUDO_JSON_TOOL_RE.search(text))


def _strip_tool_markup(text):
    """Remove tool-call markup blocks from text, return the remaining real content."""
    if not text:
        return ''
    t = text
    for pat in _TOOL_MARKUP_PATTERNS:
        t = pat.sub('', t)
    t = _re.sub(r'\n{3,}', '\n\n', t)
    return t.strip()


def _raise_if_batch_all_simulated(text, task):
    """Fail a batch-mode run if its output is almost entirely simulated tool calls.

    The batch route is toolless by design, so the agentic simulation detector
    is bypassed. But an output that is *entirely* fake tool calls (no real
    deliverable) is still a failure — the model did not answer the task.
    Strips tool-call markup and checks the remaining real content; if under
    a threshold, the run is failed with a clear error.
    """
    if not text:
        return
    real = _strip_tool_markup(text)
    if len(real) < 200:
        model_id = ((task or {}).get('model') or '').strip() or 'model'
        raise RuntimeError(
            f'{model_id} produced no real deliverable in batch mode. The output '
            f'is entirely simulated tool-call markup (tool_code/bash/write_file '
            f'blocks as text) with no actual content. Batch mode has no tools — '
            f'the model should have produced a text deliverable directly. '
            f'Rerun the task; if it recurs, switch to an agentic (standard) model.'
        )


def _extract_batch_writes(text, project_path, out_rel=os.path.join('Artifacts', 'outputs')):
    """Extract files the model produced as text in batch mode and persist them.

    Batch mode has no tools, so the constraints block tells the model to output
    the proposed filename on its own line followed by the file contents in a
    markdown code block. This helper finds those patterns, writes the files to
    the task's output folder ``out_rel`` (never Working Docs), and returns a list of
    (filename, path, chars) for logging. Fail-open: any error returns [].

    Patterns matched (case-insensitive, flexible):
      - A line containing a filename (ending in .md/.txt/.json/.csv/.html/.py/.js/.ts/.tsx)
        followed by a ```lang fence with the content.
      - A <write_file>FILENAME\nCONTENT</write_file> block (legacy simulation pattern).
    """
    if not text or not project_path:
        return []
    import re
    written = []

    # Pattern 1: filename line followed by a code fence.
    # Match: a line that is mostly a filename (possibly in backticks), then a
    # ```lang fence, then content up to closing ```.
    _FILE_EXTS = r'\.(md|txt|json|csv|html|py|js|ts|tsx|sh|yaml|yml|xml)'
    # Find fenced code blocks and look for a filename in the preceding non-empty line.
    fence_pat = re.compile(r'```(\w+)?\s*\n(.*?)\n```', re.S)
    lines = text.split('\n')
    for m in fence_pat.finditer(text):
        content = m.group(2)
        if not content or not content.strip():
            continue
        # Find the line index of the fence start.
        start_pos = m.start()
        prefix = text[:start_pos]
        prefix_lines = prefix.rstrip('\n').split('\n')
        if len(prefix_lines) < 2:
            continue
        prev_line = prefix_lines[-1].strip()
        if not prev_line:
            # Try one more line back.
            if len(prefix_lines) >= 2:
                prev_line = prefix_lines[-2].strip()
        if not prev_line:
            continue
        # Extract a filename from the preceding line.
        # Accept: `filename.md`, filename.md, **filename.md**, "filename.md"
        fname_match = re.search(r'`{0,2}\*{0,2}"?([A-Za-z0-9_\-./ ]+' + _FILE_EXTS + r')`{0,2}\*{0,2}"?', prev_line)
        if not fname_match:
            continue
        fname = fname_match.group(1).strip().split('/')[-1]  # basename only
        # Validate filename — no path traversal, no weird chars.
        if not re.match(r'^[A-Za-z0-9_\-.]+' + _FILE_EXTS + r'$', fname, re.I):
            continue
        # Write to the task's output folder (create if missing).
        wd = os.path.join(project_path, out_rel)
        if not os.path.isdir(wd):
            try:
                os.makedirs(wd, exist_ok=True)
            except Exception:
                continue
        dest = os.path.join(wd, fname)
        # Guard against path traversal.
        if os.path.realpath(dest) != dest and not os.path.realpath(dest).startswith(os.path.realpath(wd)):
            continue
        try:
            with open(dest, 'w', encoding='utf-8') as f:
                f.write(content)
            written.append((fname, dest, len(content)))
            _log.info('batch file extracted: %s (%d chars) -> %s', fname, len(content), dest)
        except Exception as e:
            _log.warning('batch file extract failed for %s: %s', fname, e)

    # Pattern 2: <write_file>FILENAME\nCONTENT</write_file> blocks (legacy).
    wf_pat = re.compile(r'<write_file>\s*([A-Za-z0-9_\-.]+\.[A-Za-z0-9]+)\s*\n(.*?)</write_file>', re.S)
    for m in wf_pat.finditer(text):
        fname = m.group(1).strip()
        content = m.group(2).strip()
        if not content or not fname:
            continue
        if not re.match(r'^[A-Za-z0-9_\-.]+' + _FILE_EXTS + r'$', fname, re.I):
            continue
        wd = os.path.join(project_path, out_rel)
        if not os.path.isdir(wd):
            try:
                os.makedirs(wd, exist_ok=True)
            except Exception:
                continue
        dest = os.path.join(wd, fname)
        try:
            with open(dest, 'w', encoding='utf-8') as f:
                f.write(content)
            written.append((fname, dest, len(content)))
            _log.info('batch file extracted (legacy <write_file>): %s (%d chars) -> %s', fname, len(content), dest)
        except Exception as e:
            _log.warning('batch file extract failed for %s: %s', fname, e)

    return written


def _load_result_envelope(text):
    """Return a parsed CLI result envelope if the text contains one."""
    if not text:
        return None
    stripped = text.strip()
    if not stripped or '"type"' not in stripped:
        return None

    candidates = [stripped]
    first = stripped.find('{')
    last = stripped.rfind('}')
    if first != -1 and last > first:
        inner = stripped[first:last + 1].strip()
        if inner and inner not in candidates:
            candidates.append(inner)

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict) and data.get('type') == 'result':
            return data
    return None


def _raise_if_denied_cli_result(text, model_id):
    """Treat empty denied tool requests as failures, not successful replies."""
    data = _load_result_envelope(text)
    if not data:
        return
    denials = data.get('permission_denials') or []
    result = (data.get('result') or '').strip()
    if not denials or result:
        return
    detail = _summarize_permission_denials(denials)
    raise RuntimeError(
        f'{model_id} returned no assistant text because a tool request was denied'
        + (f': {detail}' if detail else '')
    )


def _raise_if_unexecuted_tool_request(text, task):
    """Fail a run if the model emitted tool-call markup instead of using tools.

    Two cases:
      * text-only route  → always a failure (no tools available)
      * agentic route    → failure only when the model simulated a tool call in
        text *and* there is no CLI result envelope proving it actually ran.
    """
    if not text or not _looks_like_unexecuted_tool_request(text):
        return
    # A run that exhausted Vibe's turn budget executed real tools on every
    # turn; report it as incomplete (the marker), not as a simulation.
    if VIBE_TURN_LIMIT_MARKER in text:
        return
    model_id = ((task or {}).get('model') or '').strip() or 'model'
    if _task_uses_direct_text_route(task):
        raise RuntimeError(
            f'{model_id} returned an unexecuted tool command instead of a final deliverable. '
            'This route is a direct text API call and cannot run Bash, read files, or write files. '
            'Rerun with an action-capable CLI model such as codex-chatgpt, Claude Pro, or Mistral Pro, '
            'or rewrite the task so the model returns complete file contents directly.'
        )
    # Agentic route: a CLI result envelope proves the tools actually ran.
    # If one is present, the markers are just quoted inside the result content
    # (e.g. the model showed a snippet) — not a simulation.
    if _load_result_envelope(text) is None:
        raise RuntimeError(
            f'{model_id} simulated a tool call in text instead of executing it. '
            'The model has real tool access but emitted markup rather than acting. '
            'This is the Phase 7.2 simulation gap — rerun the task; if it recurs, '
            'switch to a stronger agentic model or simplify the task prompt.'
        )


_DOC_DELIVERABLE_KEYWORDS = (
    'draft', 'write', 'create', 'prepare', 'produce', 'generate', 'compose',
    'summarise', 'summarize', 'analyze', 'analyse', 'review', 'contract',
    'mou', 'memorandum', 'report', 'document', 'proposal', 'plan', 'brief',
    'guide', 'framework', 'template', 'agreement', 'policy', 'statement',
)

# Creation-intent detection for the text-only auto-switch.
#
# A phrase list ('create a file', 'write a report', …) only matches the exact
# wording someone thought of; real tasks say "Create a plain text file",
# "Write a test file", "make a CSV", "output a JSON file" and slipped through,
# so a text-only model was left to "create" a file it could not write
# (laptop task 20001131).
#
# Instead: a creation verb applied to a *file-like artifact*. Read verbs
# (read/open/list/show/summarise/analyse…) are deliberately excluded so
# analysis tasks are not pushed off their assigned model.
_CREATION_VERBS = (
    r'create|write|make|generate|produce|save|output|export|compose|draft|'
    r'prepare|build|author|record'
)
# Artifact or container nouns. "file"/"files" catch the generic case; the
# explicit extensions catch "test.txt", "report.md", "data.csv".
_ARTIFACT_NOUNS = (
    r'file|files|document|documents|report|reports|folder|directory|'
    r'doc|docs|spreadsheet|worksheet|csv|json|xml|yaml|yml|markdown|md|'
    r'txt|log|script|template|guide|memo|brief|proposal|plan|contract|'
    r'framework|summary|transcript|dataset|config|config file|readme'
)
_ARTIFACT_EXTS = (
    r'txt|md|markdown|csv|tsv|json|xml|yaml|yml|html|htm|pdf|docx?|xlsx?|'
    r'pptx?|log|py|js|ts|sh|sql|ini|toml|cfg|conf|rtf|tex'
)
_CREATION_INTENT_RE = _re.compile(
    rf'\b(?:{_CREATION_VERBS})\b[^.\n]{{0,60}}?'
    rf'(?:\b(?:{_ARTIFACT_NOUNS})\b|\.(?:{_ARTIFACT_EXTS})\b)',
    _re.I | _re.S,
)
# Imperative one-liners with no explicit noun ("Write it to disk", "Save as
# notes.md") — the second alternative catches a bare "save to <name>.<ext>".
_CREATION_INTENT_AUX_RE = _re.compile(
    rf'(?:\b(?:{_CREATION_VERBS})\s+(?:it|them|these|those|this|the\s+result|'
    rf'the\s+output|the\s+content)\b)'
    rf'|\b(?:save|write|output|export)\s+(?:as|to)\b',
    _re.I,
)


def _task_requests_file_creation(task):
    """True when the task asks the model to create/save a file-like artifact.

    Used to auto-switch a text-only route onto a tool-capable model so the
    write actually executes. Read-only tasks (read/open/list/summarise the
    attached file) return False, so they stay on their assigned model.
    """
    text = ((task or {}).get('title') or '') + '\n' + ((task or {}).get('description') or '')
    if not text.strip():
        return False
    if _CREATION_INTENT_RE.search(text):
        return True
    return bool(_CREATION_INTENT_AUX_RE.search(text))


_ACK_PHRASES = (
    'i understand', 'i will draft', 'i will write', 'i will create',
    'i will prepare', 'i will produce', 'i will proceed', 'to begin with',
    'understood', 'certainly', 'sure, i can', 'i can help', 'i am ready',
    'ok, i will',
)


def _task_expects_deliverable(task):
    """True when the task asks the model to author a document/substantive
    deliverable (as opposed to a short factual answer)."""
    t = ((task.get('title') or '') + ' ' + (task.get('description') or '')).lower()
    return any(k in t for k in _DOC_DELIVERABLE_KEYWORDS)


def _looks_like_acknowledgment(text):
    """True when output is a bare 'I will do X' / 'understood' with no actual
    deliverable — short and matching an acknowledgment lead-in."""
    if not text:
        return True
    t = text.strip()
    if len(t) > 400:
        return False
    return any(p in t.lower() for p in _ACK_PHRASES)


def generate_queue_handoff(previous_result, next_task_id):
    """Generate and store a compact handoff for the next queued task.

    The coordinator is fail-open: any error is logged and the next task keeps
    running with its original prompt.
    """
    try:
        if not isinstance(previous_result, dict) or not next_task_id:
            return {'ok': False, 'skipped': True}

        next_task = db.get_task(next_task_id)
        prev_task_id = previous_result.get('task_id')
        prev_task = db.get_task(prev_task_id) if prev_task_id else None
        if not next_task or not prev_task:
            return {'ok': False, 'skipped': True}

        if prev_task.get('project_id') != next_task.get('project_id'):
            _log.warning(
                'cross-project handoff blocked: task %s (project %s) → task %s (project %s)',
                prev_task_id, prev_task.get('project_id'),
                next_task_id, next_task.get('project_id'),
            )
            return {'ok': False, 'skipped': True, 'reason': 'cross_project'}

        prev_project_path = _find_project_path(prev_task)
        next_project_path = _find_project_path(next_task)
        prev_exec_id = previous_result.get('exec_id')
        prev_output = _read_full_output(prev_project_path, prev_exec_id)
        if not prev_output:
            prev_output = previous_result.get('summary') or previous_result.get('error') or ''

        phase_name = phase_from_task(next_task)
        project_memory = read_memory(next_project_path, level='project') if next_project_path else ''
        phase_memory = ''
        if next_project_path and phase_name:
            phase_memory = read_memory(next_project_path, level='phase', phase_name=phase_name)

        prompt = f"""You are SuperAgent's queue coordinator. You run after one queued task finishes and before the next queued task starts.

Produce a concise markdown handoff for the next task. Do not rewrite the next task. Do not invent facts. Focus only on what the next task must know because of the previous task's result.

If the previous task failed, partially completed, hit quota, or made the next prompt likely stale, say that clearly. If there is no useful coordination context, return: No specific handoff from the previous task.

Keep it under 350 words. Return only the handoff markdown body.

Previous task:
Title: {prev_task.get('title') or ''}
Status: {previous_result.get('status') or ''}
Error: {previous_result.get('error') or ''}
Instructions:
{_cap_text(prev_task.get('description') or '', 3000)}

Previous task output:
{_cap_text(prev_output, HANDOFF_CONTEXT_CHARS)}

Next queued task:
Title: {next_task.get('title') or ''}
Phase: {phase_name or ''}
Instructions:
{_cap_text(next_task.get('description') or '', 5000)}

Project memory excerpt:
{_cap_text(project_memory, HANDOFF_MEMORY_CHARS)}

Phase memory excerpt:
{_cap_text(phase_memory, HANDOFF_MEMORY_CHARS)}
"""

        text, tok_in, tok_out, cost = route(
            'mistral-medium-latest',
            prompt,
            HANDOFF_MAX_TOKENS,
            project_path=next_project_path,
            force_mistral_mode='api',
            caller='handoff',
        )
        handoff = _strip_markdown_fence(text).strip()
        if not handoff:
            handoff = 'No specific handoff from the previous task.'
        db.save_task_handoff(next_task_id, handoff, source_exec_id=prev_exec_id)
        return {
            'ok': True,
            'task_id': next_task_id,
            'source_exec_id': prev_exec_id,
            'tokens_in': tok_in,
            'tokens_out': tok_out,
            'cost_usd': cost,
        }
    except Exception as e:
        _log.warning('queue handoff generation failed for next task %s: %s', next_task_id, e)
        return {'ok': False, 'error': str(e)}


def _build_prompt(task, project_path, mode='inline'):
    """Build the execution prompt: role, project context, skills, budget,
    project + phase memory, queue handoff, then the task itself.
    `mode` is 'inline' (standard/agentic) or 'batch' (toolless chunk-and-aggregate).
    In batch mode the prompt emits a text-only constraints block instead of the
    agentic rules block — the model has no tools in batch mode and telling it
    to "use your tools" causes it to emit fake tool-call markup as text.
    (Mirrored by estimate_task_prompt_tokens — keep the two in sync.)"""
    parts = []

    # Stale warning — shown when a dependency has been approved since last run
    if (task or {}).get('stale'):
        parts.append(
            '## ⚠️ Stale Task Warning\n\n'
            'One or more tasks this task depends on have been updated and approved since '
            'this task was last run. The dependency outputs may have changed. '
            'Review all context carefully and produce output that reflects the latest dependency state.'
        )

    # Role / persona — persona identity first, then role domain prompt
    role_id = (task or {}).get('role_id')
    aingel_name = (task or {}).get('_aingel_name', '').strip()
    project_name = (task or {}).get('_project_name', '').strip()
    role_parts = []
    if aingel_name:
        role_parts.append(f'You are {aingel_name}, Guide for the {project_name} project.')
    if role_id:
        try:
            role = db.get_role(role_id)
            if role and role.get('system_prompt', '').strip():
                role_parts.append(role['system_prompt'].strip())
        except Exception:
            pass
    if role_parts:
        parts.append('## Role\n\n' + '\n\n'.join(role_parts))

    # Project context (CLAUDE.md) — always included regardless of level
    context = _load_project_context(project_path)
    if context:
        parts.append(f'## Project context\n\n{context}')

    # Skills summary — compact, always included so the model knows the stack
    if project_path:
        try:
            skills = skills_context_summary(project_path)
            if skills:
                parts.append(f'## Project skills\n\n{skills}')
        except Exception:
            pass

    # Budget context — only when the project has a monthly budget set
    try:
        budget_block = _budget_context(task)
        if budget_block:
            parts.append(f'## Budget Context\n\n{budget_block}')
    except Exception:
        pass

    phase_name = phase_from_task(task)
    model_id = ((task or {}).get('model') or '').strip()
    # Both Scaleway and Ollama Cloud run the OpenAI-compatible function-calling loop
    # and expose get_project_memory, so memory is available on demand — keep the injected
    # excerpt short (also keeps the HTTP body under Scaleway's ~40 kB parser limit).
    is_tool_loop = model_id.startswith('scw-') or model_id.startswith('oll-')
    is_research = (task or {}).get('_execution_type') == 'research'
    if project_path:
        proj_mem = read_memory(project_path, level='project')
        if proj_mem:
            if is_tool_loop and not is_research:
                proj_mem = _cap_text(proj_mem, SCW_PROJ_MEM_CAP)
            parts.append(f'## Project memory\n\n{proj_mem}')
        if is_research:
            # Inject all phase memories for research projects
            artifacts_dir = os.path.join(project_path, 'Artifacts')
            if os.path.isdir(artifacts_dir):
                for fname in sorted(os.listdir(artifacts_dir)):
                    if fname.startswith('phase-') and fname.endswith('.memory.md'):
                        fpath = os.path.join(artifacts_dir, fname)
                        try:
                            with open(fpath, encoding='utf-8', errors='ignore') as _f:
                                pmem = _f.read().strip()
                            if pmem:
                                label = fname.replace('.memory.md', '').replace('phase-', 'Phase ')
                                parts.append(f'## {label} memory\n\n{pmem}')
                        except Exception:
                            pass
        elif phase_name:
            phase_mem = read_memory(project_path, level='phase', phase_name=phase_name)
            if phase_mem:
                if is_tool_loop:
                    phase_mem = _cap_text(phase_mem, SCW_PHASE_MEM_CAP)
                parts.append(f'## {phase_name} memory\n\n{phase_mem}')

    handoff_context = (task.get('handoff_context') or '').strip()
    if handoff_context:
        source = task.get('handoff_source_exec_id')
        label = f' from exec #{source}' if source else ''
        parts.append(f'## Queue Handoff{label}\n\n{handoff_context}')

    # Task
    parts.append(f'## Task: {task["title"]}')
    instructions = (task.get('description') or '').strip()
    if instructions:
        parts.append(f'## Instructions\n\n{instructions}')
    else:
        parts.append(
            'No specific instructions provided. Analyse the task title, '
            'use the project context above, and produce the most useful output you can. '
            'Be concrete and specific.'
        )

    if _task_uses_direct_text_route(task):
        parts.append(
            '## Execution constraints\n\n'
            'You are being called through a direct text API route. You cannot run Bash, '
            'inspect the filesystem, or create/edit files. Do not emit tool-call markup '
            'such as <bash>...</bash>. If the task asks for a file or HTML artifact, '
            'return the complete proposed filename and full file contents in your response.'
        )
    elif mode == 'batch':
        # Batch mode runs without tools (project_path=None) by design — the
        # model receives file contents inlined into the prompt and cannot call
        # read_file/write_file/bash. Telling it to "use your tools" here causes
        # the simulation gap: the model emits <write_file>…</write_file> or
        # ```bash blocks as text, nothing is actually written, and the run is
        # marked done with no deliverable (tasks #43/#44 on the Vault).
        parts.append(
            '## Execution constraints (batch mode)\n\n'
            'You are being called through a text-only API with NO tool access. '
            'All relevant file content has been inlined in the prompt above — do not '
            'attempt to read, list, or write files on disk.\n\n'
            '1. **No simulation.** Do NOT emit tool-call markup such as `<bash>…</bash>`, '
            '`<tool_code>…</tool_code>`, `<write_file>…</write_file>`, `function_call` blocks, '
            'or JSON `{"command":...}` envelopes. You have no tools — these are text and '
            'will not execute.\n\n'
            '2. **Produce the complete deliverable as text.** Answer the task directly in '
            'your response. If the task asks you to "create a file", output the full proposed '
            'filename on its own line followed by the complete file contents in a markdown '
            'code block — the executor will extract and persist it.\n\n'
            '3. **No filesystem references.** Do not refer to paths, `ls`, `cat`, or shell '
            'commands. Work only from the content already inlined above.'
        )
    else:
        # Phase 7.2 — Mandated Agentic Execution
        out_dir = ((task or {}).get('_output_dir') or task_output_dir(task)).replace(os.sep, '/')
        parts.append(
            '## Agentic execution rules (mandatory)\n\n'
            'You are an agentic worker with real filesystem access. Act through your tools — '
            'never describe what you would do.\n\n'
            '1. **No simulation.** Do NOT wrap commands or code in markdown fences expecting '
            'them to run. Do NOT emit `<bash>…</bash>`, `<tool_code>…</tool_code>`, or '
            '`function_call` blocks as text. If a change is needed, use the write/patch/bash '
            'tool to make it for real. A markdown "```bash" block is never an execution.\n\n'
            '2. **ReAct loop.** Think → call a tool → read the observation → decide the next '
            'action. Never report completion based on what you intended to do; only on what '
            'the tool returned.\n\n'
            '3. **Verify after every write/patch.** Immediately after any `write_file` or '
            '`patch_file` call, call `read_file` on the same path to confirm the bytes on disk '
            'match what you intended. If they do not, fix the discrepancy before moving on.\n\n'
            '4. **Read before patch.** Before any `patch_file`, read the target section first '
            'so `old_string` matches the file exactly (whitespace and indentation included).\n\n'
            '5. **Project folder is your sandbox.** All file access — reads, writes, bash '
            'cwd — MUST stay inside the project folder you were launched in. Never read, '
            'list, or write paths outside that folder (no `/home/other`, `/etc`, `/opt`, '
            'parent directories, or absolute paths outside the project). If a task seems to '
            'require external files, refuse and explain in your summary — do not reach out.\n\n'
             '6. **Read every referenced file.** Any file named in the task instructions must '
             'be read with `read_file` before you act on it. Do not guess its contents. If the '
             'file does not exist inside the project folder, report that in your summary and '
             'stop — do not fabricate content for it.\n\n'
             '7. **Deleted files stay deleted.** Files the user deleted must remain deleted. '
             'Never search for, reconstruct, or restore them (no copying from the soft-delete '
             'trash, no `git checkout`/`git restore`/`git show` of deleted history, no re-'
             'creating them from memory or other copies). If a task needs a file that is '
             'missing — whether it was deleted or simply never existed — report it missing '
             'and stop rather than restoring or inventing it. The project may contain '
             'automatic resurrection guards; treat them as enforcement, not obstacles.\n\n'
             '8. **Report what you did, with paths.** Your final summary must list the concrete '
             'file paths you created or modified and the verified result — not the steps you '
             'planned.\n\n'
              f'9. **Where outputs go.** Save every new file you produce in `{out_dir}/` '
              '(create the folder if needed), even if the instructions name another location. '
              'Never create files in the project root or in Working Docs / Working Documents — '
              'those hold the user\'s reference material. Change an existing Working Docs file '
              'only when the instructions explicitly ask you to edit that file. Outputs of '
              'earlier tasks in `Artifacts/outputs/` are readable inputs.\n\n'
              '10. **Never write deliverables to a scratchpad or temp directory.** Files '
              'outside the project folder (your CLI scratchpad, `/tmp`, temp dirs) are '
              'invisible to the user and are lost. When a task asks to "create a file", '
              'that file must land inside the project — in `{out_dir}/` unless the task '
              'names a project path explicitly.'
        )

    parts.append(
        '## Output format\n\n'
        'Provide a clear, structured response. '
        'If you produce files or code, show the full content. '
        'This is a non-interactive run: do not ask the user clarification questions; make a reasonable assumption and state it. '
        'End with a 2-sentence summary of what you did and what the next step should be.'
    )

    return '\n\n---\n\n'.join(parts)


def estimate_cost(model_id, tokens):
    pricing = get_pricing(model_id)
    if not pricing:
        pricing = get_pricing(DEFAULT_MODEL)
    tok_in  = int(tokens * agent_config.ESTIMATE_INPUT_RATIO)
    tok_out = int(tokens * agent_config.ESTIMATE_OUTPUT_RATIO)
    return round((tok_in * pricing['input'] + tok_out * pricing['output']) / 1_000_000, 5)


# ── Pre-run cost estimation ───────────────────────────────────────────────────

def count_chars_as_tokens(text):
    """Approximate token count from character count (chars / 4). Provider-agnostic."""
    return max(0, len(text or '') // 4)


def _output_token_stats(project_id, model, project_path=None):
    """Return (avg, min, max, sample_count) of output tokens for project+model from history."""
    try:
        if project_path:
            pconn = db.get_project_db(project_path)
            row = pconn.execute(
                'SELECT AVG(e.tokens_output), MIN(e.tokens_output), MAX(e.tokens_output), COUNT(*) '
                'FROM executions e LEFT JOIN tasks t ON e.task_id = t.id '
                'WHERE (t.project_id=? OR e.chat_id IS NOT NULL) AND e.model=? AND e.status="done" '
                'ORDER BY e.id DESC LIMIT 20',
                (project_id, model)
            ).fetchone()
            pconn.close()
            if row and row[3] and int(row[3]) >= 3:
                return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0), int(row[3])
        # Global fallback: aggregate across all project DBs for this model
        avgs, mins, maxes, counts = [], [], [], []
        for _pid, pp in db._all_project_paths():
            try:
                pconn = db.get_project_db(pp)
                r = pconn.execute(
                    'SELECT AVG(tokens_output), MIN(tokens_output), MAX(tokens_output), COUNT(*) '
                    'FROM executions WHERE model=? AND status="done" LIMIT 20',
                    (model,)
                ).fetchone()
                pconn.close()
                if r and r[3]:
                    avgs.append(r[0] or 0); mins.append(r[1] or 0)
                    maxes.append(r[2] or 0); counts.append(r[3])
            except Exception:
                pass
        if counts:
            total = sum(counts)
            weighted_avg = sum(a * c for a, c in zip(avgs, counts)) / total
            return int(weighted_avg), int(min(mins)), int(max(maxes)), total
    except Exception:
        pass
    return 0, 0, 0, 0


def _fallback_output_stats(total_input):
    avg = max(512, int(total_input * 0.35))
    mn  = max(256, int(total_input * 0.15))
    mx  = max(1024, int(total_input * 0.60))
    return avg, mn, mx


def _project_for(row):
    """Best-effort project row for a task/chat dict, for EU policy and defaults.
    Returns None when the row has no project — `agent_config.default_model_for`
    and `eu_guard` both accept that."""
    pid = (row or {}).get('project_id')
    if not pid:
        return None
    try:
        return db.get_project(pid)
    except Exception:
        return None


def estimate_chat_prompt_tokens(chat_id, user_message):
    """Estimate input tokens for a chat message without making an AI call."""
    chat = db.get_chat(chat_id)
    if not chat:
        return {'error': f'Chat {chat_id} not found'}

    project_path = chat.get('project_path') or ''
    model = (chat.get('model') or chat.get('task_model')
             or agent_config.default_model_for(_project_for(chat)))

    # Same tool-availability logic the real prompt will use, so the estimate
    # includes the agentic_rules section iff it will actually be in the prompt.
    tools_enabled = _chat_tools_enabled(model, project_path)

    # Same sections the real prompt is built from, so the estimate can't drift.
    breakdown = {}
    for key, text in _chat_prompt_sections(chat, user_message, model, tools_enabled):
        # Two attachments can share a label; sum rather than overwrite.
        breakdown[key] = breakdown.get(key, 0) + count_chars_as_tokens(text)

    total_input = sum(breakdown.values())

    project_id = chat.get('project_id')
    avg_out, min_out, max_out, samples = _output_token_stats(project_id, model, project_path)
    if samples < 3:
        avg_out, min_out, max_out = _fallback_output_stats(total_input)

    pricing = get_pricing(model)
    if pricing:
        cost_min = round((total_input * pricing['input'] + min_out * pricing['output']) / 1_000_000, 6)
        cost_max = round((total_input * pricing['input'] + max_out * pricing['output']) / 1_000_000, 6)
    else:
        cost_min = cost_max = None

    m_info = MODELS.get(model, {})
    return {
        'model':                   model,
        'model_label':             m_info.get('label', model),
        'total_input_tokens':      total_input,
        'estimated_output_tokens': avg_out,
        'output_tokens_min':       min_out,
        'output_tokens_max':       max_out,
        'cost_min_usd':            cost_min,
        'cost_max_usd':            cost_max,
        'breakdown':               breakdown,
        'historical_samples':      samples,
    }


def estimate_task_prompt_tokens(task_id):
    """Estimate input tokens for a task execution without making an AI call."""
    task = db.get_task(task_id)
    if not task:
        return {'error': f'Task {task_id} not found'}

    project_path = task.get('path') or _find_project_path(task)
    model = task.get('model') or agent_config.default_model_for(_project_for(task))
    breakdown = {}

    context = _load_project_context(project_path)
    if context:
        breakdown['project_context'] = count_chars_as_tokens(context)

    if project_path:
        try:
            skills = skills_context_summary(project_path)
            if skills:
                breakdown['skills'] = count_chars_as_tokens(skills)
        except Exception:
            pass

    try:
        budget_block = _budget_context(task)
        if budget_block:
            breakdown['budget_context'] = count_chars_as_tokens(budget_block)
    except Exception:
        pass

    phase_name = phase_from_task(task)
    if project_path:
        proj_mem = read_memory(project_path, level='project')
        if proj_mem:
            breakdown['project_memory'] = count_chars_as_tokens(proj_mem)
        if phase_name:
            phase_mem = read_memory(project_path, level='phase', phase_name=phase_name)
            if phase_mem:
                breakdown['phase_memory'] = count_chars_as_tokens(phase_mem)

    task_text = task.get('title', '')
    desc = (task.get('description') or '').strip()
    if desc:
        task_text += f'\n\n{desc}'
    breakdown['task'] = count_chars_as_tokens(task_text)

    output_fmt = (
        '## Output format\n\nProvide a clear, structured response. '
        'If you produce files or code, show the full content. '
        'End with a 2-sentence summary of what you did and what the next step should be.'
    )
    breakdown['output_format'] = count_chars_as_tokens(output_fmt)
    # Mirror _build_prompt: agentic runs get the 7.2 rules block, text-only runs
    # get the short constraints block. Approximate at ~1500 / ~300 chars.
    if _task_uses_direct_text_route(task):
        breakdown['execution_rules'] = count_chars_as_tokens(
            '## Execution constraints\n\nYou are being called through a direct text API route. '
            'You cannot run Bash, inspect the filesystem, or create/edit files. '
            'Do not emit tool-call markup. Return full file contents in your response.'
        )
    else:
        breakdown['execution_rules'] = count_chars_as_tokens(
            '## Agentic execution rules (mandatory)\n\n'
            'You are an agentic worker with real filesystem access. Act through your tools — '
            'never describe what you would do. No simulation. ReAct loop. '
            'Verify after every write/patch with read_file. Read before patch. '
            'Deleted files stay deleted; never restore from trash or git history. '
            'Report what you did, with paths.'
        )

    total_input = sum(breakdown.values())

    project_id = task.get('project_id')
    project_path_for_stats = task.get('path') or ''
    avg_out, min_out, max_out, samples = _output_token_stats(project_id, model, project_path_for_stats)
    if samples < 3:
        avg_out, min_out, max_out = _fallback_output_stats(total_input)

    pricing = get_pricing(model)
    if pricing:
        cost_min = round((total_input * pricing['input'] + min_out * pricing['output']) / 1_000_000, 6)
        cost_max = round((total_input * pricing['input'] + max_out * pricing['output']) / 1_000_000, 6)
    else:
        cost_min = cost_max = None

    m_info = MODELS.get(model, {})
    return {
        'model':                   model,
        'model_label':             m_info.get('label', model),
        'total_input_tokens':      total_input,
        'estimated_output_tokens': avg_out,
        'output_tokens_min':       min_out,
        'output_tokens_max':       max_out,
        'cost_min_usd':            cost_min,
        'cost_max_usd':            cost_max,
        'breakdown':               breakdown,
        'historical_samples':      samples,
    }


def _budget_context(task):
    """Return a markdown 'Budget Context' block for the prompt, or '' if the
    project has no monthly budget configured."""
    try:
        snap = db.get_project_budget(task['project_id'])
    except Exception:
        return ''
    if not snap:
        return ''
    budget = snap.get('budget_monthly') or 0.0
    if not budget:
        return ''
    spend = snap.get('current_month_spend') or 0.0
    remaining = budget - spend
    est = estimate_cost(task.get('model') or agent_config.default_model_for(_project_for(task)),
                        task.get('estimated_tokens') or 50000)
    lines = [
        f'- Monthly budget: ${budget:.2f}',
        f'- Current month spend: ${spend:.4f}',
        f'- Remaining budget: ${remaining:.4f}',
        f'- This task\'s estimated cost: ${est:.4f}',
        '',
        'Guidelines:',
        '1. Be mindful of remaining budget.',
        '2. For complex tasks, consider breaking into smaller steps.',
        '3. If budget is tight, prioritize essential work.',
    ]
    return '\n'.join(lines)


def _split_text(text, chunk_chars):
    """Split text into <=chunk_chars pieces, preferring newline boundaries."""
    text = text or ''
    if len(text) <= chunk_chars:
        return [text] if text.strip() else []
    chunks, start, n = [], 0, len(text)
    while start < n:
        end = min(start + chunk_chars, n)
        if end < n:
            nl = text.rfind('\n', start, end)
            if nl > start:
                end = nl + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def run_large_file_batch(model, base_prompt, payload, out_max, project_path):
    """Chunk-and-aggregate a payload too large for one context window.

    Map: each chunk is processed with the task instructions to produce a partial
    result. Reduce: partials are synthesised into the final deliverable. Tools are
    disabled (project_path=None) because the file content is supplied directly.
    Returns ``(text, tokens_in, tokens_out, cost)``.
    """
    from agent_router import route

    # Dynamically size chunks based on the model's context window and the base
    # prompt.  Use a very conservative 1.7 chars/token ratio — Polish/Central-
    # European text with diacritics tokenises much denser than English
    # (measured ~1.7 chars/token on Scaleway Qwen models for tender documents).
    try:
        from model_caps import model_capabilities
        caps = model_capabilities(model)
        ctx = caps.get('context_window', 128_000)
    except Exception:
        ctx = 128_000
    base_prompt_chars = len(base_prompt or '')
    # Reserve the per-call output budget plus overhead/markers so a chunk prompt
    # never overflows the context window. A fixed 10k-token reserve would go
    # negative for a 4096-token custom model; scale it from the real output cap.
    out_reserve = min(int(out_max) if out_max else 8192, 8192)
    available_tokens = max(ctx - out_reserve - 2000, 0)
    # Convert available tokens to chars using conservative 1.7 chars/token ratio
    available_chars = int(available_tokens * 1.7)
    chunk_chars = min(available_chars - base_prompt_chars, 280_000)
    chunk_chars = max(chunk_chars, 2000)  # floor, context-aware
    CHUNK_CHARS  = chunk_chars
    # model_caps lists context-window-sized max_tokens; Scaleway caps *output* lower
    # (e.g. 32768). Keep per-call output modest — partial/final summaries don't need more.
    out_max = min(int(out_max) if out_max else 8192, 8192)
    # Reduce budget scales with the model's context window so a small-context
    # model collapses partials before the final synthesis overflows. A fixed
    # 90k-token budget is unreachable for a 4096/32k window, so the final call
    # would always blow past it (regression: small-context batch overflow).
    reduce_budget = max(ctx - out_max - (len(base_prompt or '') // 2) - 2000, 4096)

    chunks = _split_text(payload, CHUNK_CHARS)
    if not chunks:
        # Nothing oversized to chunk — fall back to a single inline call.
        return route(model, base_prompt, out_max, project_path=None,
                     policy_path=project_path, caller='large_file_batch',
                     force_mistral_mode='api')

    n = len(chunks)
    tin = tout = 0
    cost = 0.0
    partials = []
    for i, ch in enumerate(chunks, 1):
        p = (f"{base_prompt}\n\n--- DOCUMENT PART {i} of {n} ---\n{ch}\n\n"
             f"Above is part {i} of {n} of a large document. Extract and summarise everything "
             f"in THIS part that is relevant to the task. Output only the partial result for this part.")
        text, a, b, c = route(model, p, out_max, project_path=None,
                              policy_path=project_path, caller='large_file_batch:map',
                              force_mistral_mode='api')
        partials.append(f"[Part {i}/{n}]\n{text}")
        tin += a or 0; tout += b or 0; cost += c or 0.0
        _log.info("batch map: part %d/%d done (%d tok out)", i, n, b or 0)

    # Reduce — hierarchically collapse if the partials themselves overflow.
    combined = "\n\n".join(partials)
    while len(combined) // 2 > reduce_budget and len(partials) > 1:
        merged = []
        for j in range(0, len(partials), 2):
            grp = "\n\n".join(partials[j:j + 2])
            p = f"{base_prompt}\n\nMerge these partial results into one consolidated partial result:\n\n{grp}"
            text, a, b, c = route(model, p, out_max, project_path=None,
                                  policy_path=project_path, caller='large_file_batch:reduce',
                                  force_mistral_mode='api')
            merged.append(text)
            tin += a or 0; tout += b or 0; cost += c or 0.0
        partials = merged
        combined = "\n\n".join(partials)
        _log.info("batch reduce: collapsed to %d partials", len(partials))

    final_prompt = (
        f"{base_prompt}\n\nThe source document was processed in {n} parts. Below are the partial "
        f"results. Synthesise them into the FINAL deliverable exactly as the task specifies — do not "
        f"mention the chunking process.\n\n{combined}")
    text, a, b, c = route(model, final_prompt, out_max, project_path=None,
                          policy_path=project_path, caller='large_file_batch:final',
                          force_mistral_mode='api')
    tin += a or 0; tout += b or 0; cost += c or 0.0
    return text, tin, tout, round(cost, 6)


# Polish runs denser than English per token; measured on the 840-page OCR corpus
# this file was written for. Used to size a chunk by what the model can *emit*.
_CHARS_PER_TOKEN = 3.2
# Leave the model room to be slightly more verbose than its input without being
# cut off mid-sentence.
_TRANSFORM_SAFETY = 0.8
# A correction should return roughly what it was given. Much shorter means the
# model summarised, refused, or was truncated — all silent failures without this.
_TRUNCATION_RATIO = 0.7
# Character length alone is not sufficient. Measured on the OCR corpus,
# scw-mistral-small-24b returned a length ratio of 0.985 — comfortably inside the
# check above — while dropping 142 of 1 480 lines and reordering table rows. Loss
# spread thinly across a segment hides in a character count but not in a line
# count, so both are checked.
_LINE_LOSS_RATIO = 0.95


def run_large_file_transform(model, base_prompt, payload, out_max, project_path,
                             concurrency=4, caller='large_file_transform'):
    """Apply a 1:1 transformation to a payload too large for one context window.

    Distinct from ``run_large_file_batch`` on purpose. That one is *summarise*-
    shaped: it maps chunks to partial results and then collapses them into a
    single deliverable, discarding the per-chunk text. A transformation — OCR
    correction, translation, redaction — must keep **every chunk's output, in
    order**, so bending the batch function would have meant two incompatible
    behaviours behind one name.

    Three differences that matter:

    * **Chunks are sized by output capacity, not input.** ``run_large_file_batch``
      uses 280 000 chars (~70k tokens) because a summary comes back small. Here
      output ≈ input, so a chunk must fit within ``out_max`` on the way *back*.
    * **Chunks run concurrently.** The batch path is sequential, which is fine
      against serverless but not against a GPU billed by the hour, where
      wall-clock is the cost.
    * **Short output is treated as failure.** A chunk that comes back far shorter
      than it went in was summarised, refused or truncated. Returning it silently
      is how you lose pages out of the middle of a document and never notice.

    Returns ``(text, tokens_in, tokens_out, cost, report)``. ``report`` carries
    ``chunks``, ``suspect`` (indices that tripped the length check) and ``failed``,
    so a caller can refuse to write the result rather than trusting it.
    """
    from concurrent.futures import ThreadPoolExecutor
    from model_caps import get_cap

    # A 1:1 transform must budget for the answer *and* whatever the model spends
    # thinking first. Capping out_max at 8192 like the summarise path does is what
    # broke the first dry-run: 21k-char chunks needed ~6 550 output tokens, leaving
    # ~1 600 for reasoning, and scw-qwen3.6-35b spent the lot — 30 803 output tokens
    # billed for three empty segments. Ask for the model's real ceiling instead
    # (unused tokens are not charged) and size chunks to a fraction of it, so the
    # answer occupies a minority of the budget however verbose the reasoning.
    # `out_max` from the caller describes the *task's* answer (a few thousand
    # tokens of deliverable). A per-segment budget is a different quantity, so the
    # caller's figure is a floor here, not a ceiling — honouring 8192 verbatim is
    # what starved the first dry-run.
    cap = get_cap(model).get('max_tokens') or 8192
    out_max = min(max(int(out_max or 0), cap), cap)
    target_out_tokens = max(512, out_max // 4)
    chunk_chars = max(2000, int(target_out_tokens * _CHARS_PER_TOKEN * _TRANSFORM_SAFETY))
    chunks = _split_text(payload, chunk_chars)
    if not chunks:
        return '', 0, 0, 0.0, {'chunks': 0, 'suspect': [], 'failed': []}

    n = len(chunks)
    _log.info('transform: %d chunks of <=%d chars, concurrency=%d', n, chunk_chars, concurrency)

    def _one(idx_chunk):
        i, ch = idx_chunk
        p = (f"{base_prompt}\n\n--- SEGMENT {i} of {n} ---\n{ch}\n\n"
             f"Return ONLY the processed text for THIS segment. Preserve its length, "
             f"structure, line breaks and ordering. Do not summarise, do not omit "
             f"anything, do not add commentary, headings or explanation.")
        try:
            # project_path=None disables tools — this is a text transformation, not
            # an agentic task; policy_path keeps the EU boundary resolving against
            # the owning project. force_mistral_mode='api' ensures Mistral doesn't
            # try the Vibe CLI path (which requires project_path as cwd).
            text, a, b, c = route(model, p, out_max, project_path=None,
                                  policy_path=project_path, caller=f'{caller}:map',
                                  force_mistral_mode='api')
            return i, text or '', a or 0, b or 0, c or 0.0, None
        except Exception as e:
            _log.warning('transform: segment %d/%d failed: %s', i, n, e)
            return i, '', 0, 0, 0.0, str(e)

    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
        results = list(pool.map(_one, enumerate(chunks, 1)))

    results.sort(key=lambda r: r[0])          # concurrency must not reorder the document
    out_parts, suspect, failed = [], [], []
    tin = tout = 0
    cost = 0.0
    for (i, text, a, b, c, err) in results:
        tin += a; tout += b; cost += c
        if err:
            failed.append({'segment': i, 'error': err})
            # Keep the original text so the document stays complete and the gap is
            # locatable, rather than silently losing a segment.
            out_parts.append(chunks[i - 1])
            continue
        src = chunks[i - 1]
        src_len = len(src)
        src_lines = src.count('\n')
        out_lines = text.count('\n')
        why = None
        if src_len and len(text) < src_len * _TRUNCATION_RATIO:
            why = 'short output'
        elif src_lines and out_lines < src_lines * _LINE_LOSS_RATIO:
            why = 'lines dropped'
        if why:
            suspect.append({'segment': i, 'reason': why,
                            'in_chars': src_len, 'out_chars': len(text),
                            'in_lines': src_lines, 'out_lines': out_lines})
            _log.warning('transform: segment %d/%d suspect (%s) — %d chars/%d lines in, '
                         '%d chars/%d lines out',
                         i, n, why, src_len, src_lines, len(text), out_lines)
        # _split_text cuts on newline boundaries and keeps the newline, so the
        # chunks concatenate back to the original byte for byte. Restore a trailing
        # newline the model dropped, but never add one that was not there — joining
        # with '\n' instead would insert a blank line between every segment.
        if src.endswith('\n') and text and not text.endswith('\n'):
            text += '\n'
        out_parts.append(text)

    report = {'chunks': n, 'chunk_chars': chunk_chars, 'concurrency': concurrency,
              'suspect': suspect, 'failed': failed}
    if suspect or failed:
        _log.warning('transform: %d suspect, %d failed of %d segments',
                     len(suspect), len(failed), n)
    return ''.join(out_parts), tin, tout, round(cost, 6), report


def _generate_brief_async(exec_id, task, project_path,
                         referenced_files=None, caught_files=None,
                         self_serve_files=None, route_agentic=False,
                         rag_label=None, rag_provenance=None):
    """Phase 7: AIngel Autopilot post-run analysis (H3) + overview refresh (H4).

    Replaces the legacy 2-sentence Haiku brief with a structured JSON analysis
    from the project's ``aingel_model``. Stores:
      - ``executions.aingel_review_json`` — full JSON {severity, findings, …}
      - ``executions.aingel_brief``       — the 2-3 sentence ``brief_2s`` field
                                           (backwards-compat with inline UI)

    Phase 7.2: also receives the referenced/caught/self-serve file lists from
    H2 so H3 can verify that referenced files were actually read by the task
    agent (closing the simulation-gap verification loop).

    If the project has an AIngel chat (``aingel_chat_id``) and severity != ok,
    posts the finding into the chat for discussion.

    After H3 completes, fires H4 (overview refresh) in another daemon thread.
    Runs entirely fire-and-forget; never raises into the caller.
    """
    pconn = None
    try:
        import agent_overseer as overseer
        output_path = _output_file_path(project_path, exec_id)
        output_text = ''
        if os.path.exists(output_path):
            with open(output_path, encoding='utf-8', errors='ignore') as f:
                output_text = f.read()

        proj = db.get_project(task.get('project_id')) if task.get('project_id') else None
        if not proj:
            # Fall back to a project lookup by path
            for p in db.get_projects():
                if p.get('path') == project_path:
                    proj = p
                    break
        if not proj or not proj.get('aingel_model'):
            # No AIngel configured — fall back to the legacy Haiku 2-sentence brief
            _legacy_haiku_brief(exec_id, task, project_path, output_text[:4000])
            return

        # Fetch the execution row for H3
        exec_row = db.get_execution(exec_id, project_path=project_path)
        if not exec_row:
            exec_row = {'id': exec_id, 'task_id': task.get('id'),
                        'model': task.get('model'), 'status': 'done',
                        'tokens_input': 0, 'tokens_output': 0, 'finish_reason': ''}

        review = overseer.post_run_analysis(
            task=task,
            execution=exec_row,
            project=proj,
            output_text=output_text,
            blocked_tool_count=0,  # H3 scans the output text itself for blocked-tool hits
            referenced_files=referenced_files,
            caught_files=caught_files,
            self_serve_files=self_serve_files,
            route_agentic=route_agentic,
            rag_label=rag_label,
            rag_provenance=rag_provenance,
        )

        brief_2s = (review.get('brief_2s') or '').strip()
        review_json = json.dumps(review, ensure_ascii=False)
        db.update_execution_review(exec_id, brief=brief_2s, review_json=review_json,
                                   project_path=project_path)

        # Set gate state on the task based on H3's gate_for_next recommendation.
        # Only hold; never skip — the user decides whether to skip the next task.
        gate_for_next = (review.get('gate_for_next') or 'proceed').lower()
        if gate_for_next == 'hold':
            db.update_task(task['id'], project_path=project_path,
                           gate_state='hold', gate_source='H3',
                           gate_reason='; '.join(review.get('findings', [])[:3])
                           or review.get('severity', ''),
                           gate_decided_at=datetime.utcnow().isoformat(),
                           gate_report_json=review_json)
        else:
            # Clear any prior H2 gate now that the run is done and analysed.
            db.update_task(task['id'], project_path=project_path,
                           gate_state='open', gate_source='H3',
                           gate_reason='', gate_decided_at=datetime.utcnow().isoformat(),
                           gate_report_json=review_json)

        # Phase 7.1: always post to AIngel chat — 1-liner on ok, full block on warning/issue.
        severity = (review.get('severity') or 'ok').lower()
        if proj.get('aingel_chat_id'):
            if severity == 'ok':
                post_msg = f"✓ Task #{task.get('id')} completed: {brief_2s}"
            else:
                findings = review.get('findings') or []
                findings_block = '\n'.join(f'  • {f}' for f in findings[:5]) or '  (no specific findings)'
                post_msg = (
                    f"⚠ Post-run review for Task #{task.get('id')} flagged **{severity}**.\n\n"
                    f"**Task:** {task.get('title','')}\n"
                    f"**Recommendation:** {review.get('recommendation','approve')}\n"
                    f"**Gate for next task:** {gate_for_next}\n\n"
                    f"**Findings:**\n{findings_block}\n\n"
                    f"**Summary:** {brief_2s}"
                )
            try:
                chat_reply(proj['aingel_chat_id'], post_msg, model=proj.get('aingel_model'))
            except Exception as _chat_err:
                _log.warning('[overseer H3] exec %s: chat post failed: %s', exec_id, _chat_err)

        # Fire H4 (overview refresh) in another daemon thread — uses last 5 briefs.
        import threading as _threading
        _threading.Thread(target=_aingel_overview_async,
                          args=(proj, project_path),
                          daemon=True).start()
    except Exception as _e:
        _log.warning('[overseer H3] exec %s: %s', exec_id, _e)
        # Fall back to legacy brief on any error so the inline UI still has something.
        try:
            _legacy_haiku_brief(exec_id, task, project_path, '')
        except Exception:
            pass
    finally:
        if pconn:
            try:
                pconn.close()
            except Exception:
                pass


def _legacy_haiku_brief(exec_id, task, project_path, output_snippet):
    """Legacy 2-sentence brief — kept as a fallback when no aingel_model is set
    or when the overseer call fails. Writes only ``executions.aingel_brief``.

    The model is resolved per project: Haiku normally, a cheap Scaleway model on
    EU-only projects and on the Vault. This runs on *every* task, so hardcoding
    Anthropic here sent a slice of every EU-only project's output to a US
    provider."""
    try:
        proj = db.get_project(task.get('project_id')) if task.get('project_id') else None
        brief_model = agent_config.default_model_for(proj, cheap=True)
        if not agent_config.eu_only_for(proj):
            brief_model = 'claude-haiku-4-5-20251001'
        brief_prompt = (
            f'Task: {task.get("title","")}\n\n'
            f'Output:\n{output_snippet}\n\n'
            'In 2-3 sentences: what was accomplished, and what is the natural next step?'
        )
        text, _ti, _to, _c = route(brief_model, brief_prompt, 200,
                                    project_path=project_path, caller='legacy_brief')
        if text and text.strip():
            conn = db.get_project_db(project_path)
            conn.execute('UPDATE executions SET aingel_brief=? WHERE id=?',
                         (text.strip(), exec_id))
            conn.commit()
            conn.close()
    except Exception as _e:
        _log.warning('[brief] exec %s: %s', exec_id, _e)


def _aingel_overview_async(project, project_path):
    """Phase 7 H4: refresh Artifacts/aingel-overview.md from recent activity.

    Gathers the last 5 execution briefs, the phase list, open risks (from H3
    history where severity != ok), and the budget snapshot, then asks the
    project's aingel_model to write a concise rolling overview.
    """
    pconn = None
    try:
        import agent_overseer as overseer
        if not project or not project.get('aingel_model'):
            return

        # Last 5 briefs — read directly from project.db to avoid cross-project
        # aggregation overhead.
        last_5 = []
        try:
            pconn = db.get_project_db(project_path)
            rows = pconn.execute(
                "SELECT e.id, e.task_id, e.aingel_brief, e.aingel_review_json, t.title "
                "FROM executions e LEFT JOIN tasks t ON e.task_id = t.id "
                "WHERE e.aingel_brief IS NOT NULL AND e.aingel_brief != '' "
                "ORDER BY e.id DESC LIMIT 5"
            ).fetchall()
            for r in rows:
                brief_2s = r['aingel_brief'] or ''
                severity = 'ok'
                if r['aingel_review_json']:
                    try:
                        rv = json.loads(r['aingel_review_json'])
                        severity = (rv.get('severity') or 'ok').lower()
                        brief_2s = rv.get('brief_2s') or brief_2s
                    except Exception:
                        pass
                last_5.append({
                    'task_id': r['task_id'],
                    'title': r['title'] or '',
                    'severity': severity,
                    'brief_2s': brief_2s,
                })
        except Exception as _e:
            _log.warning('[overseer H4] last_5 fetch failed: %s', _e)

        # Phase list — read from agent_phases if available
        phase_list = []
        try:
            import agent_phases as aph
            pd = aph.get_project_phases(project.get('id'), project_path)
            if pd:
                for ph in (pd.get('phases') or []):
                    tasks_in = ph.get('tasks') or []
                    done = sum(1 for t in tasks_in if t.get('status') == 'done')
                    phase_list.append({
                        'name': ph.get('title', ''),
                        'done': done,
                        'total': len(tasks_in),
                    })
        except Exception:
            pass

        # Open risks — recent H3 findings where severity != ok
        open_risks = []
        try:
            pconn = db.get_project_db(project_path)
            rows = pconn.execute(
                "SELECT aingel_review_json FROM executions "
                "WHERE aingel_review_json IS NOT NULL "
                "ORDER BY id DESC LIMIT 10"
            ).fetchall()
            pconn.close()
            for r in rows:
                try:
                    rv = json.loads(r['aingel_review_json'])
                    if (rv.get('severity') or 'ok').lower() != 'ok':
                        for f in (rv.get('findings') or [])[:2]:
                            open_risks.append(f)
                except Exception:
                    pass
            open_risks = list(dict.fromkeys(open_risks))[:6]  # dedupe + cap
        except Exception:
            pass

        # Budget snapshot
        budget = None
        try:
            budget = db.get_project_budget(project.get('id'))
        except Exception:
            pass

        overseer.refresh_overview(
            project=project,
            last_5_briefs=last_5,
            phase_list=phase_list,
            open_risks=open_risks,
            budget_snapshot=budget,
        )
    except Exception as _e:
        _log.warning('[overseer H4] project %s: %s', project.get('id') if project else '?', _e)
    finally:
        if pconn:
            try:
                pconn.close()
            except Exception:
                pass


def run_task(task_id=None):
    """Execute a task with a real AI call. Returns result dict."""
    if not task_id:
        # Never fall back to "first confirmed task anywhere": that runs another
        # tenant's task. Callers must name the task they are allowed to run.
        return {'error': 'task_id is required'}
    task = db.get_task(task_id)
    if not task:
        return {'error': f'Task {task_id} not found'}

    if task['status'] not in ('confirmed', 'pending'):
        return {'error': f'Task status is "{task["status"]}" — confirm it first'}

    # Hugging Face Scout: a task assigned to an adopted HF model that has no
    # serving path yet is not runnable — refuse rather than silently running on
    # a fallback engine the user never chose.
    if task.get('awaiting_model'):
        return {'task_id': task['id'], 'status': 'failed',
                'error': ('This task is assigned to a Hugging Face model that is not '
                          'yet served (awaiting self-hosting). It cannot run until the '
                          'model is deployed.')}

    project_path = task.get('path') or _find_project_path(task)
    phase_name   = phase_from_task(task)

    # ── Project + persona resolution (before create_execution so model is recorded correctly) ──
    proj = db.get_project(task['project_id'])
    task = dict(task)  # shallow copy — we inject private keys without mutating the DB row

    # Processing type, set per task: research (all memories in the prompt) or
    # deployment (1:1 large-file transform, no auto-merge). Git does not depend
    # on it — every project is versioned (agent_git.resolve_enabled).
    execution_type = (task.get('execution_type') or '').strip() or 'standard'
    task['_execution_type'] = execution_type
    if execution_type == 'deployment':
        # Phase 6. A 'deployment' project processes a large document 1:1 — OCR
        # correction, translation, redaction — rather than summarising it, so an
        # oversized referenced file goes through run_large_file_transform instead
        # of run_large_file_batch. Wiring it here (rather than at the dedicated-GPU
        # step) means moving to a dedicated deployment later changes only *which
        # endpoint the model id resolves to*, not how the task runs.
        _log.info('[exec] task %s deployment mode — 1:1 transform for oversized files',
                  task['id'])

    # Named persona — injected into prompt by _build_prompt()
    task['_aingel_name'] = (proj or {}).get('aingel_name') or ''
    task['_project_name'] = (proj or {}).get('name') or ''
    # Where this task's deliverables go — told to the model in the prompt and
    # enforced for in-process tools (agent_tools.output_dir_scope).
    task['_output_dir'] = task_output_dir(task, project_path)
    # Lane B: ensure explicit context refs are carried into the prompt builder
    if 'context_refs' not in task or task['context_refs'] is None:
        task['context_refs'] = []
    # if stored as JSON string (legacy), parse
    if isinstance(task['context_refs'], str):
        try:
            import json as _json
            parsed = _json.loads(task['context_refs']) if task['context_refs'].strip() else []
            task['context_refs'] = parsed if isinstance(parsed, list) else []
        except Exception:
            task['context_refs'] = []

    # role.default_model fallback — when task has no explicit model, use the role's preferred model
    explicit_model = bool(task.get('model'))
    if not task.get('model') and task.get('role_id'):
        _role = db.get_role(task['role_id'])
        if _role and _role.get('default_model'):
            task['model'] = _role['default_model']

    # EU boundary: roles are configured globally, so role.default_model bypasses
    # the API-level guard that runs on task create/model change. Anything we
    # filled in ourselves (role default, or nothing at all) is replaced with the
    # project's EU default; a model the user explicitly chose is refused rather
    # than silently swapped underneath them.
    ok_eu, err_eu = agent_config.eu_guard(proj, task.get('model'))
    if not ok_eu and explicit_model:
        agent_config.eu_audit(project_path, model=task['model'],
                              provider=MODELS.get(task['model'], {}).get('provider', ''),
                              allowed=False, caller='run_task', reason=err_eu)
        db.update_task(task['id'], status='failed', project_path=project_path)
        return {'task_id': task['id'], 'status': 'failed', 'error': err_eu}
    if not ok_eu or not task.get('model'):
        fallback = agent_config.default_model_for(proj)
        _log.warning('[exec] task %s: model %r unusable here — falling back to %s',
                     task['id'], task.get('model'), fallback)
        task['model'] = fallback

    # File-creation auto-switch: if the task asks the model to create/write a
    # file but the assigned model is text-only (e.g. Mistral API mode without
    # Vibe CLI), switch to a Scaleway model with tool calling so write_file
    # actually executes instead of being emitted as fake markup.
    if _task_uses_direct_text_route(task):
        if _task_requests_file_creation(task):
            from agent_config import is_eu_model, EU_DEFAULT_MODEL
            _log.info('[exec] task %s: model %r is text-only but task creates files — '
                      'switching to Scaleway tool model', task['id'], task['model'])
            task['model'] = EU_DEFAULT_MODEL

    # Free-tier run + token quota — increment runs and check token budget
    # before create_execution so a 21st run or an over-budget user fails
    # cleanly without creating an orphan execution record.
    try:
        import agent_quotas
        _owner = agent_quotas.resolve_owner_id(task, proj)
        if _owner:
            agent_quotas.check_and_increment_run(_owner)
            agent_quotas.check_token_budget(_owner)
    except agent_quotas.QuotaError as e:
        return {'task_id': task['id'], 'status': 'failed', 'error': str(e),
                'quota': e.field, 'limit': e.limit}

    # Monthly budget gate — refuse to start on a project that already spent its
    # budget for the month. The budget used to be displayed but never enforced.
    try:
        _budget = db.get_project_budget(task['project_id']) or {}
        _limit = _budget.get('budget_monthly') or 0.0
        _spend = _budget.get('current_month_spend') or 0.0
        if _limit and _spend >= _limit:
            return {'task_id': task['id'], 'status': 'failed',
                    'error': f'Monthly budget of ${_limit:.2f} is exhausted '
                             f'(spent ${_spend:.2f}). Raise the budget or wait for the reset.'}
    except Exception:
        pass

    # Serialize runs within a project — overlapping git checkpoint/stash/checkout
    # would mix up or discard another run's edits. The lock is taken BEFORE the
    # task is claimed: while we wait, the task is still pending/confirmed, so a
    # restart during the wait cannot leave it 'running' with no execution row.
    # Released in the finally below (and on the early-exit paths).
    _plock = _project_lock(project_path)
    if not _plock.acquire(timeout=1800):
        return {'task_id': task['id'], 'status': 'failed',
                'error': 'Another run is already in progress for this project'}

    # Atomic claim: only one runner may move this task pending/confirmed →
    # running. A second concurrent run gets False and bails out instead of
    # executing the same task twice.
    try:
        _claimed = db.claim_task(task['id'], project_path=project_path)
    except Exception:
        _plock.release()
        raise
    if not _claimed:
        _plock.release()
        return {'task_id': task['id'], 'status': 'failed',
                'error': 'Task is already running or is no longer runnable'}

    # Create execution record first, then run. The task is already 'running'
    # from the atomic claim above.
    try:
        exec_id = db.create_execution(
            task['id'], task['model'], project_path=project_path,
            created_by=(task.get('created_by')
                        or (proj or {}).get('owner_id')))
    except Exception as e:
        _plock.release()
        db.update_task(task['id'], status='pending', project_path=project_path)
        return {'task_id': task['id'], 'status': 'failed', 'error': f'Failed to create execution: {str(e)}'}

    _hb_stop = _start_heartbeat(exec_id, project_path=project_path)
    try:

        # ── Git-validated execution: start a per-task branch (every project) ──
        # The AI edits files in project_path; isolating those edits on task/<id>-<slug>
        # makes the change reviewable (approve→merge) and revertible (reject→discard).
        # Mistral OCR is data-only (writes to Working Documents/OCR_mistral, not code) —
        # skip git branching to avoid 2-5s git status on large repos and to keep Vault
        # responsive during 41-PDF batches (was causing 49s /api/executions stalls).
        git_active = False
        git_branch = ''
        if task.get('model') == 'mistral-ocr-latest':
            start_git = False
        elif project_path:
            start_git = agit.resolve_enabled((proj or {}).get('git_enabled'), project_path)
        else:
            start_git = False
        if start_git and project_path:
            gstart = agit.start_task_branch(project_path, task['id'], task['title'],
                                            project_name=(proj or {}).get('name', ''))
            if gstart.get('active'):
                git_active = True
                git_branch = gstart['branch']
                db.update_execution_git(exec_id, branch=git_branch, project_path=project_path)
            else:
                _log.warning('git: could not start task branch for task %s: %s',
                             task['id'], gstart.get('error'))

        try:
            import agent_scw_session
            agent_scw_session.sync_bucket_to_working_docs(task['project_id'], project_path)
        except Exception as _sync_err:
            _log.warning('bucket sync failed for task %s: %s', task['id'], _sync_err)

        from prompt_builder import build as build_prompt
        built = build_prompt(task, project_path, project=proj)
        mode = built.get('mode')           # 'inline' or 'batch' (large-file chunk-and-aggregate)
        prompt = built.get('prompt')
        batch_payload = built.get('batch_payload', '')
        max_tokens = built.get('max_tokens', MAX_TOKENS)
        caught_files = built.get('caught_files', [])
        noted_binary = built.get('noted_binary', [])
        rag_label = built.get('rag_label')          # RAG badge (RAG-PREFETCHED etc.)
        rag_provenance = built.get('rag_provenance') or {}   # retrieval detail for UI
        # Lane B isolation fields
        lane_context_refs = built.get('context_refs', [])
        lane_used_refs = built.get('used_context_refs', [])
        lane_review_needed = built.get('context_review_needed', False)
        h2 = {}  # populated by the H2 block when autopilot is on

        # Persist RAG provenance so the UI can show whether this run grounded its
        # answer in the legal library (and which corpus/chunks) vs. model knowledge.
        if rag_label or rag_provenance:
            try:
                db.update_execution_rag(exec_id, label=rag_label,
                                        provenance=rag_provenance or None,
                                        project_path=project_path)
            except Exception as _rag_err:
                _log.warning('[exec] task %s: rag provenance persist failed: %s',
                             task['id'], _rag_err)

        # Persist which Lane B context files were actually injected (used) vs.
        # only noted as binary, so the UI can show the prompt's grounding.
        try:
            db.update_execution_context(exec_id,
                                        used_refs=lane_used_refs or None,
                                        noted_binary=noted_binary or None,
                                        review_needed=lane_review_needed or None,
                                        project_path=project_path)
        except Exception as _ctx_err:
            _log.warning('[exec] task %s: context refs persist failed: %s',
                         task['id'], _ctx_err)

        # Model-fit guard: a small-context model cannot process a corpus many times
        # its window — chunking still "works" but at hundreds of map calls on a GPU
        # billed hourly (regression: task #10001078, 1.7M chars on a 4096-token
        # fine-tune). Fail fast with an actionable hint instead of burning money.
        if mode == 'batch' and batch_payload:
            try:
                from model_caps import model_capabilities
                _caps = model_capabilities(task['model'])
                _ctx = int(_caps.get('context_window', 128000) or 128000)
                _batch_tokens = len(batch_payload) // 2
                if _ctx < 32768 and _batch_tokens > _ctx * 8:
                    _est_calls = (_batch_tokens // max(_ctx, 1)) + 1
                    raise RuntimeError(
                        f"Task references ~{_batch_tokens:,} tokens of file content but "
                        f"{task['model']} has a {_ctx}-token context. Even chunked this would "
                        f"need ~{_est_calls:,} map calls on a GPU billed hourly. "
                        f"Use a large-context model (e.g. scw-qwen3.6-35b, 256k) for this deliverable.")
            except RuntimeError:
                raise
            except Exception:
                pass

        # ── Phase 7: H2 — AIngel pre-run check (only when autopilot on) ──────────
        # One medium-AI call returning both the strategic brief AND a prompt-
        # completeness check (missing files, binary mismatches, capability warnings).
        # Strict mode: gate != 'run' aborts before route() is called.
        # Advisory mode: gate decision is persisted so the UI can show a banner,
        # but the run proceeds (the user sees the banner appear after the run).
        _autopilot_ok = True
        try:
            import agent_quotas
            _owner = agent_quotas.resolve_owner_id(task, proj)
            if _owner and not agent_quotas.can_use_autopilot(_owner):
                _autopilot_ok = False
        except Exception:
            pass
        if _autopilot_ok and (proj or {}).get('aingel_autopilot'):
            try:
                import agent_overseer as overseer
                from model_caps import model_capabilities
                dep_titles = [d.get('title', '') for d in (db.get_dependencies(task['id']) or [])]
                dep_outcomes = db.get_dependency_h3_outcomes(task['id'], project_path)
                h2 = overseer.pre_run_check(
                    task=task,
                    project=proj,
                    caught_files=caught_files,
                    noted_binary=noted_binary,
                    model_caps=model_capabilities(task.get('model')),
                    dep_titles=dep_titles,
                    budget_snapshot=db.get_project_budget(task['project_id']),
                    dep_outcomes=dep_outcomes,
                    rag_provenance=rag_provenance,
                )
                gate = (h2.get('gate') or 'run').lower()
                gate_state_db = 'hold' if gate in ('hold', 'skip') else 'open'
                db.update_task_h2_gate(task['id'],
                                       h2_json=json.dumps(h2, ensure_ascii=False),
                                       gate_state=gate_state_db,
                                       gate_reason=h2.get('reason', ''),
                                       gate_decided_at=datetime.utcnow().isoformat(),
                                       project_path=project_path)
                dep_blockers = h2.get('dep_blockers') or []
                _log.info('[overseer H2] task %s: gate=%s completeness=%s reason=%s dep_blockers=%d',
                          task['id'], gate, h2.get('completeness', '?'),
                          h2.get('reason', ''), len(dep_blockers))
                aingel_mode = (proj or {}).get('aingel_mode') or 'advisory'
                if gate != 'run' and aingel_mode == 'strict':
                    db.finish_execution(exec_id, 'failed',
                        error_message=f'Guide gate ({gate}): {h2.get("reason","")}',
                        project_path=project_path)
                    db.update_task(task['id'], status='pending', project_path=project_path)
                    _log.info('[overseer H2] task %s aborted by strict gate (%s): %s',
                              task['id'], gate, h2.get('reason', ''))
                    return {
                        'task_id': task['id'], 'exec_id': exec_id,
                        'status': 'gate_hold', 'gate': h2,
                        'work_session_slot': task.get('work_session_slot'),
                    }
                if dep_blockers and aingel_mode == 'strict':
                    blocker_list = ', '.join(
                        f"#{b['task_id']} ({b['gate_for_next']})" for b in dep_blockers[:5]
                    )
                    db.finish_execution(exec_id, 'failed',
                        error_message=f'Guide dep_blockers: {blocker_list}',
                        project_path=project_path)
                    db.update_task(task['id'], status='pending', project_path=project_path)
                    _log.info('[overseer H2] task %s aborted: %d dep_blocker(s) — %s',
                              task['id'], len(dep_blockers), blocker_list)
                    return {
                        'task_id': task['id'], 'exec_id': exec_id,
                        'status': 'gate_hold', 'gate': h2,
                        'work_session_slot': task.get('work_session_slot'),
                    }
            except Exception as _h2_err:
                _log.warning('[overseer H2] task %s: %s', task['id'], _h2_err)

        # ── Lane B: deterministic context_refs validation (lexists+readable) ──────
        # Every re-run validates refs; missing → advisory hold banner, strict aborts
        # before route(). This is independent of AIngel autopilot/H2.
        try:
            _crefs = task.get('context_refs') or []
            # handle case where DB still returns JSON string (should be list)
            if isinstance(_crefs, str):
                try:
                    import json as _json2
                    _crefs = _json2.loads(_crefs) if _crefs.strip() else []
                except Exception:
                    _crefs = []
            if _crefs:
                # normalize not needed — already normalized, but be safe
                missing = []
                for rel in _crefs:
                    if not isinstance(rel, str):
                        continue
                    s = rel.strip().replace(os.sep, '/')
                    if not s or os.path.isabs(s) or '..' in s.split('/'):
                        missing.append(rel)
                        continue
                    # ── Lane B security: forbidden path component ──────────
                    _sec_blocked = False
                    for part in s.split('/'):
                        if agent_files._is_forbidden_path(part):
                            missing.append(f"forbidden path: {s}")
                            _log.warning('[Lane B] gate blocked forbidden component %r in %r', part, s)
                            _sec_blocked = True
                            break
                    if _sec_blocked:
                        continue
                    # ── symlink walk on prefix + realpath containment ──────
                    if project_path:
                        parts = s.split('/')
                        for i in range(1, len(parts) + 1):
                            prefix = '/'.join(parts[:i])
                            lex_abs = os.path.join(project_path, prefix)
                            if os.path.lexists(lex_abs) and os.path.islink(lex_abs):
                                missing.append(f"symlink not allowed: {s}")
                                _log.warning('[Lane B] gate blocked symlink component %r in %r', prefix, s)
                                _sec_blocked = True
                                break
                        if _sec_blocked:
                            continue
                        try:
                            candidate_real = os.path.realpath(os.path.join(project_path, s))
                            root_real = os.path.realpath(project_path)
                            if not (candidate_real == root_real or candidate_real.startswith(root_real + os.sep)):
                                missing.append(f"path escapes project: {s}")
                                _log.warning('[Lane B] gate blocked escapes project %r -> %r', s, candidate_real)
                                continue
                            base = os.path.basename(candidate_real)
                            if base and agent_files._is_forbidden_path(base):
                                missing.append(f"forbidden path: {s}")
                                _log.warning('[Lane B] gate blocked forbidden resolved basename %r for %r', base, s)
                                continue
                        except Exception:
                            pass
                    full = os.path.join(project_path, s) if project_path else s
                    # basename-only fallback search in Working Docs + outputs
                    if project_path and not os.path.lexists(full) and '/' not in s:
                        found = False
                        try:
                            from agent_tools import _WORKING_DOC_FOLDERS as _WDF2
                        except Exception:
                            _WDF2 = ('My Docs', 'Working Docs', 'Working Documents', 'working-docs', 'docs')
                        for _fld in list(_WDF2) + [os.path.join('Artifacts', 'outputs')]:
                            cand = os.path.join(project_path, _fld, s)
                            if os.path.lexists(cand) and os.access(cand, os.R_OK):
                                # ── validate fallback candidate security ───
                                _cand_blocked = False
                                _cand_parts = os.path.join(_fld, s).split('/')
                                for i in range(1, len(_cand_parts) + 1):
                                    _pfx = '/'.join(_cand_parts[:i])
                                    _lex = os.path.join(project_path, _pfx)
                                    if os.path.lexists(_lex) and os.path.islink(_lex):
                                        missing.append(f"symlink not allowed: {s}")
                                        _log.warning('[Lane B] gate blocked symlink in fallback %r for %r', _pfx, s)
                                        _cand_blocked = True
                                        break
                                if _cand_blocked:
                                    # mark as blocked; don't treat as found, continue searching other folders
                                    # but ensure we don't later mark as simple missing; already added
                                    found = False
                                    # break outer? keep searching — but symlink block is definitive
                                    break
                                try:
                                    _cand_real = os.path.realpath(cand)
                                    _root_real = os.path.realpath(project_path)
                                    if not (_cand_real == _root_real or _cand_real.startswith(_root_real + os.sep)):
                                        missing.append(f"path escapes project: {s}")
                                        _log.warning('[Lane B] gate blocked fallback escapes %r -> %r', cand, _cand_real)
                                        _cand_blocked = True
                                    else:
                                        _b = os.path.basename(_cand_real)
                                        if _b and agent_files._is_forbidden_path(_b):
                                            missing.append(f"forbidden path: {s}")
                                            _log.warning('[Lane B] gate blocked forbidden fallback basename %r for %r', _b, s)
                                            _cand_blocked = True
                                except Exception:
                                    pass
                                if _cand_blocked:
                                    found = False
                                    break
                                if os.path.isfile(cand):
                                    found = True
                                    break
                                else:
                                    # fallback exists but not a file
                                    missing.append(s)
                                    found = False
                                    break
                        if found:
                            continue
                        # if we already appended a security reason, skip generic missing append
                        if any(s in m for m in missing):
                            continue
                    if not os.path.lexists(full) or not os.access(full, os.R_OK):
                        missing.append(s)
                    elif not os.path.isfile(full):
                        missing.append(s)
                    else:
                        # re-validate realpath for direct full (covers symlink target escape)
                        try:
                            _full_real = os.path.realpath(full)
                            _root2 = os.path.realpath(project_path) if project_path else ''
                            if project_path and not (_full_real == _root2 or _full_real.startswith(_root2 + os.sep)):
                                # replace previous non-missing with security reason
                                if s in missing:
                                    pass
                                else:
                                    missing.append(f"path escapes project: {s}")
                                    _log.warning('[Lane B] gate blocked full escapes %r -> %r', full, _full_real)
                            else:
                                _fb = os.path.basename(_full_real)
                                if _fb and agent_files._is_forbidden_path(_fb):
                                    if s not in missing and f"forbidden path: {s}" not in missing:
                                        missing.append(f"forbidden path: {s}")
                        except Exception:
                            pass
                if missing:
                    reason = f"missing context: {', '.join(missing[:5])}"
                    _log.warning('[Lane B] task %s missing context refs: %s', task['id'], missing)
                    # set hold gate (advisory vs strict decided below)
                    try:
                        db.update_task(task['id'], project_path=project_path,
                                       gate_state='hold', gate_source='H2',
                                       gate_reason=reason,
                                       gate_decided_at=datetime.utcnow().isoformat())
                    except Exception:
                        pass
                    aingel_mode = (proj or {}).get('aingel_mode') or 'advisory'
                    # CRON-recorded tasks are advisory even in strict mode (spec)
                    # Detect via task source or description marker? For now treat source=='cron' or title prefix.
                    is_cron = (task.get('source') == 'cron' or str(task.get('title') or '').startswith('[CRON]'))
                    if is_cron:
                        aingel_mode = 'advisory'
                    if aingel_mode == 'strict':
                        try:
                            db.finish_execution(exec_id, 'failed',
                                                error_message=f'Lane B gate (missing context): {reason}',
                                                project_path=project_path)
                            db.update_task(task['id'], status='pending', project_path=project_path)
                        except Exception:
                            pass
                        _hb_stop()
                        return {
                            'task_id': task['id'], 'exec_id': exec_id,
                            'status': 'gate_hold', 'gate': {'gate': 'hold', 'reason': reason, 'missing': missing},
                            'work_session_slot': task.get('work_session_slot'),
                        }
                    # advisory: proceed but h2 dict will carry warning for UI
                    if not h2:
                        h2 = {}
                    h2.setdefault('capability_warnings', []).append(reason)
                    h2['gate'] = 'hold'
                    h2['reason'] = reason
        except Exception as _lb_err:
            _log.warning('[Lane B] validation failed for task %s: %s', task.get('id'), _lb_err)

        # Kanban column → forced provider mode:
        #   slot 1 (Claude Pro)   → claude-code (Pro subscription, agentic)
        #   slot 2 (Mistral Pro)  → vibe        (Mistral Pro subscription, agentic)
        #   slot 3 (PAYG)         → claude-code / vibe (agentic, billed to subscription)
        #                            GPT/Scaleway fall through to their direct routes
        #   slot 4 (EU Scaleway)  → scaleway    (Scaleway Generative API, function calling)
        #   slot 5 (Ollama Cloud) → ollama      (Ollama Cloud OpenAI-compatible, function calling)
        #                            no forced mode — dispatched by provider, like Scaleway
        #   slot None (Unassigned) is unreachable here — Unassigned tasks have status
        #   pending and the runner only picks up confirmed tasks.
        slot = task.get('work_session_slot')
        force_anthropic_mode = None
        force_mistral_mode = None
        if slot == 1:
            force_anthropic_mode = 'claude-code'
        elif slot == 2:
            force_mistral_mode = 'vibe'
        elif slot == 3:
            force_anthropic_mode = 'claude-code'
            force_mistral_mode = 'vibe'

        # Free-tier batch gate — fail cleanly instead of degrading to a broken
        # inline run (the batch-built prompt has no file content; it lives only
        # in batch_payload which the inline dispatch silently drops).
        try:
            import agent_quotas
            _owner = agent_quotas.resolve_owner_id(task, proj)
            if _owner and not agent_quotas.can_use_batch(_owner) and mode == 'batch':
                db.update_task(task['id'], status='failed', project_path=project_path)
                return {'task_id': task['id'], 'status': 'failed',
                        'error': 'Batch mode (large-file chunking) is not available on the free tier. '
                                 'Upgrade or use a model with a large enough context window to process the file inline.'}
        except Exception:
            pass

        # Software projects (project type) legitimately create files in the
        # repo root; elsewhere stray root files are moved to the task's outputs.
        _is_software_project = ((proj or {}).get('project_type') or '').strip().lower() == 'software'
        _root_before = (_root_files_snapshot(project_path)
                        if project_path and not _is_software_project else None)
        import time as _time
        _run_started_at = _time.time()
        import agent_tools as _agent_tools
        _out_token = _agent_tools._output_dir.set(task['_output_dir'])
        try:
            if mode == 'batch' and execution_type == 'deployment':
                # Phase 6: a 1:1 transformation of the whole file, order preserved —
                # not a summary. The report is recorded on the execution so a caller
                # can see that segments were truncated or failed instead of trusting
                # output far too long for anyone to read end to end.
                text, tok_in, tok_out, cost, _report = run_large_file_transform(
                    task['model'], prompt, batch_payload, max_tokens, project_path)
                if _report.get('suspect') or _report.get('failed'):
                    _log.warning('[exec] task %s transform: %d suspect, %d failed of %d segments',
                                 task['id'], len(_report.get('suspect') or []),
                                 len(_report.get('failed') or []), _report.get('chunks'))
            elif mode == 'batch':
                # Large referenced file(s): chunk-and-aggregate synchronously.
                text, tok_in, tok_out, cost = run_large_file_batch(
                    task['model'], prompt, batch_payload, max_tokens, project_path)
            else:
                web_search = False
                try:
                    if task.get('project_id') and task.get('model'):
                        from agent_config import is_eu_model
                        m = task['model']
                        if (m.startswith(('mistral-', 'open-mistral', 'codestral-', 'devstral-'))
                                and is_eu_model(m)):
                            perms = db.get_project_permissions(task['project_id'])
                            for item in perms.get('F_network', []):
                                if item.get('enabled'):
                                    web_search = True
                                    break
                except Exception:
                    pass
                text, tok_in, tok_out, cost = route(task['model'], prompt, max_tokens,
                                                     project_path=project_path,
                                                     exec_id=exec_id,
                                                     force_anthropic_mode=force_anthropic_mode,
                                                     force_mistral_mode=force_mistral_mode,
                                                     caller=f'run_task:{task["id"]}',
                                                     web_search=web_search)
            _raise_if_denied_cli_result(text, task['model'])
            # Deliverable gate: a text-only route (GPU deployment / direct API) can
            # answer a document task with a bare "I will draft …" and nothing else —
            # the run then ships an empty deliverable marked `done` (regression:
            # task #10001079). Nudge once for the real deliverable; if it still
            # answers with an acknowledgment, fail instead of pretending.
            if mode != 'batch' and _task_uses_direct_text_route(task) and \
                    _task_expects_deliverable(task) and _looks_like_acknowledgment(text):
                _log.warning('task %s: %s returned an acknowledgment, nudging for the deliverable',
                             task['id'], task['model'])
                _nudge = (prompt + '\n\nDo not describe what you will do. Now actually produce '
                          'the complete deliverable, in full, in your response.')
                _text2, _a2, _b2, _c2 = route(task['model'], _nudge, max_tokens,
                                              project_path=None,
                                              policy_path=project_path,
                                              caller=f'run_task:{task["id"]}:deliverable-nudge')
                if not _looks_like_acknowledgment(_text2):
                    text = _text2
                    tok_in += _a2 or 0
                    tok_out += _b2 or 0
                    cost += _c2 or 0.0
                else:
                    raise RuntimeError(
                        f'{task["model"]} answered the task with a bare acknowledgment ("I will …") '
                        'and produced no deliverable even after a nudge. Rerun with an agentic '
                        'model (Claude Pro / Mistral Pro / a Scaleway tool model) or a stronger '
                        'fine-tune.')
            # Batch mode runs without tools (project_path=None) by design;
            # the model may emit incidental tool-call markup as text, which is
            # tolerated. But an output that is *entirely* simulated tool calls
            # (no real deliverable) is still a failure — the model did not answer.
            if mode == 'batch':
                _raise_if_batch_all_simulated(text, task)
                # Batch mode has no tools — if the model produced a file as text
                # (filename line + code block, per the batch constraints), extract
                # and persist it to the task's output folder so downstream tasks can read it.
                try:
                    extracted = _extract_batch_writes(text, project_path, task['_output_dir'])
                    if extracted:
                        _log.info('task %s: extracted %d file(s) from batch output: %s',
                                  task['id'], len(extracted), [f for f, _p, _c in extracted])
                except Exception as _ext_err:
                    _log.warning('batch file extraction failed for task %s: %s', task['id'], _ext_err)
            else:
                _raise_if_unexecuted_tool_request(text, task)

            _relocated = _relocate_new_root_files(project_path, _root_before, task['_output_dir'])
            if _relocated:
                _log.info('task %s: moved %d new root file(s) to %s: %s', task['id'],
                          len(_relocated), task['_output_dir'], _relocated)
                text = (text or '') + (
                    '\n\n---\n\n**Guide:** task outputs are kept in the task\'s output folder, '
                    'not the project root. Moved:\n'
                    + '\n'.join(f'- `{old}` → `{new}`' for old, new in _relocated))

            # Scratchpad rescue: deliverable written to the CLI agent's /tmp
            # scratchpad instead of the project (task #10001176 pattern).
            _rescued = _rescue_scratchpad_files(task, project_path, task['_output_dir'],
                                                _run_started_at)
            if _rescued:
                _log.info('task %s: rescued %d scratchpad file(s) to %s: %s',
                          task['id'], len(_rescued), task['_output_dir'], _rescued)
                text = (text or '') + (
                    '\n\n---\n\n**Guide:** the deliverable was written to the model\'s '
                    'scratchpad; rescued into the task output folder:\n'
                    + '\n'.join(f'- `{r}`' for r in _rescued))

            # Shared GPU window: record a per-call attribution row so the window's
            # accrued cost can be split across the projects that used it.
            if (task.get('model') or '').startswith('scw-dep-'):
                try:
                    import agent_scw_deploy
                    agent_scw_deploy.record_deployment_call(
                        task['model'], project_id=task.get('project_id'),
                        task_id=task['id'], exec_id=exec_id,
                        tokens_in=tok_in, tokens_out=tok_out)
                except Exception as _attr_err:
                    _log.warning('deployment call attribution failed for task %s: %s',
                                 task['id'], _attr_err)

            summary = text[:2000] + ('…' if len(text) > 2000 else '')

            # Save full output to Artifacts/outputs/ so approval can use complete content.
            # Written before the zombie guard so a cancelled-but-completed run's
            # paid-for output is still preserved for inspection.
            output_file = _write_full_output(project_path, exec_id, task, text, tok_in, tok_out,
                                             stale=bool(task.get('stale')))

            # Zombie guard: if the execution was cancelled while the provider call
            # was still in flight (e.g. an in-process tool loop whose cancel check
            # only fires between turns), the /cancel endpoint already marked the row
            # failed and reset the task to pending. finish_execution's
            # `AND status='running'` guard then silently no-ops, but update_task
            # below is unguarded and would flip the user-reset task to 'done' with
            # no live execution (regression: task #10001124 — done task, sole exec
            # failed, slot 409 on re-run). Detect the cancelled state and stop
            # cleanly instead — no update_task, no H3 review, no git commit/merge.
            try:
                from agent_router import _execution_cancelled
                if _execution_cancelled(exec_id, project_path):
                    raise ExecutionCancelledError('execution cancelled during provider call')
            except ExecutionCancelledError:
                raise
            except Exception as _zg_err:
                _log.warning('task %s: zombie guard check failed: %s', task['id'], _zg_err)

            db.finish_execution(exec_id, 'done',
                tokens_input=tok_in, tokens_output=tok_out,
                cost_usd=cost, output_summary=summary,
                project_path=project_path)
            # Clear stale flag: task has been re-run with fresh context.
            db.update_task(task['id'], status='done', actual_cost=cost, stale=0,
                           project_path=project_path)

            # Auto-generate a 2-3 sentence brief (fire-and-forget, does not block result)
            # Phase 7.2: pass referenced/caught/self-serve file lists so H3 can verify
            # that referenced files were actually read by the task agent.
            import threading as _threading
            # Free-tier brief gate
            _briefs_ok = True
            try:
                import agent_quotas
                _owner = agent_quotas.resolve_owner_id(task, proj)
                if _owner and not agent_quotas.can_use_briefs(_owner):
                    _briefs_ok = False
            except Exception:
                pass
            if _briefs_ok:
                try:
                    import agent_overseer as _overseer
                    h3_referenced = _overseer._extract_referenced_files(
                        task.get('description') or '', _overseer.context_ref_names(task))
                except Exception:
                    h3_referenced = []
                h3_caught = caught_files
                h3_self_serve = (h2.get('self_serve') if isinstance(h2, dict) else []) or []
                h3_agentic = not _task_uses_direct_text_route(task)
                _threading.Thread(target=_generate_brief_async,
                                  args=(exec_id, task, project_path),
                                  kwargs={
                                      'referenced_files': h3_referenced,
                                      'caught_files': h3_caught,
                                      'self_serve_files': h3_self_serve,
                                      'route_agentic': h3_agentic,
                                      'rag_label': rag_label,
                                      'rag_provenance': rag_provenance,
                                  },
                                  daemon=True).start()

            # Count actual consumption against the slot's budget.
            task_slot = task.get('work_session_slot')
            if task_slot:
                db.update_work_session_tokens(task_slot, (tok_in or 0) + (tok_out or 0))
            # The queue handoff (if any) was consumed by this run; a later re-run
            # must not inherit it.
            if (task.get('handoff_context') or '').strip():
                db.clear_task_handoff(task['id'], project_path=project_path)

            # Commit the AI's file edits on the task branch (if git-enabled). No diff
            # (text-only/research task) → discard the empty branch; nothing to validate.
            git_info = {'git_branch': '', 'git_commit': '', 'git_diffstat': '', 'git_changed': False}
            if git_active:
                gc = agit.commit_task(project_path, task['id'], task['title'])
                if gc.get('ok') and gc.get('changed'):
                    db.update_execution_git(exec_id, commit=gc['commit'], diffstat=gc['diffstat'],
                                            project_path=project_path)
                    git_info = {'git_branch': git_branch, 'git_commit': gc['commit'],
                                'git_diffstat': gc['diffstat'], 'git_changed': True}
                    if gc.get('tombstone_note'):
                        git_info['tombstone_note'] = gc.get('tombstone_note')

                    # Auto-merge to main for all execution types so output files
                    # appear on disk immediately. For 'software' type, the user can
                    # still Reject (git revert) from the Exec Log if needed.
                    exec_type = task.get('_execution_type') or task.get('execution_type') or 'standard'
                    if exec_type in ('standard', 'research', 'software'):
                        try:
                            git_merge = agit.merge_task_branch(project_path, git_branch, task['id'])
                            if git_merge and git_merge.get('ok') and git_merge.get('commit'):
                                db.update_execution_git(exec_id, merge_commit=git_merge['commit'],
                                                        project_path=project_path)
                                git_info['git_merge_commit'] = git_merge['commit']
                            elif git_merge and git_merge.get('warning'):
                                _log.warning('git merge warning for task %s: %s', task['id'], git_merge['warning'])
                        except Exception as e:
                            _log.warning('git merge failed for task %s: %s', task['id'], e)
                else:
                    # Commit failed — do NOT discard; keep the branch so the user can recover.
                    _log.warning('git commit failed for task %s, branch %s preserved: %s',
                                 task['id'], git_branch, gc.get('error'))
                    git_info = {
                        'git_branch': git_branch, 'git_commit': '', 'git_diffstat': '',
                        'git_changed': False,
                        'git_commit_error': gc.get('error') or 'commit failed',
                    }

            # F10 — post-git linkify: ensure output file contains links for files created in this run
            # (diffstat + absolute path scan). Handles the case where _write_full_output already
            # added a section from scan-only; in that case rebuild with the full combined set.
            try:
                pid_for_link = task.get('project_id')
                if output_file and pid_for_link and os.path.exists(output_file):
                    diffstat = ''
                    if git_active:
                        try:
                            # gc is defined only inside the git_active branch above
                            if 'gc' in locals() and isinstance(gc, dict):
                                diffstat = gc.get('diffstat') or ''
                        except Exception:
                            diffstat = ''
                    diff_rels = _parse_diffstat_rels(diffstat) if diffstat else []
                    scan_rels = _scan_created_rels_from_text(text, project_path) if project_path and text else []
                    # Files written to this task's own output folder during the run —
                    # covers git-disabled projects and replies that give relative paths.
                    scan_rels += _changed_output_rels(project_path, task.get('_output_dir'),
                                                      locals().get('_run_started_at'))
                    # Combine preserving order: diffstat first, then scan
                    combined: list[str] = []
                    seen_c: set[str] = set()
                    for _r in diff_rels + scan_rels:
                        if _r and _r not in seen_c:
                            seen_c.add(_r)
                            combined.append(_r)
                    if combined:
                        with open(output_file, encoding='utf-8', errors='ignore') as _f:
                            cur = _f.read()
                        if "## Files created in this run" in cur:
                            # Strip old section (last occurrence — ours is always at EOF) and regenerate
                            _idx = cur.rfind("## Files created in this run")
                            _base = cur[:_idx].rstrip()
                            _new_cur = _linkify_output_with_files(_base, project_path, int(pid_for_link), combined)
                        else:
                            _new_cur = _linkify_output_with_files(cur, project_path, int(pid_for_link), combined)
                            # also try reading cur as base (cur may be header+text)
                        # Need original cur for comparison; _new_cur derived from base or cur
                        # For the already-linkified branch, compare against original cur
                        _orig = cur
                        # Re-read to ensure comparison correct when we rebuilt from base
                        if _new_cur != _orig:
                            # When we rebuilt from base, _new_cur != _orig by definition; write it
                            with open(output_file, 'w', encoding='utf-8') as _f:
                                _f.write(_new_cur)
                        elif "## Files created in this run" not in cur:
                            # Non-rebuilt path already handled; but keep idempotent
                            pass
            except Exception as _le:
                _log.warning("linkify post-git failed for exec %s: %s", exec_id, _le)

            # C2: append the tombstone note AFTER the linkify rebuild (which
            # truncates from "## Files created in this run" onward), so the note
            # survives and the user always sees why files were re-deleted.
            if git_info.get('tombstone_note'):
                try:
                    if output_file and os.path.exists(output_file):
                        with open(output_file, 'a', encoding='utf-8') as _f:
                            _f.write(f"\n\n> ⚠ **Tombstone gate:** {git_info['tombstone_note']}\n")
                except Exception:
                    pass

            result = {
                'task_id':    task['id'],
                'exec_id':    exec_id,
                'phase_name': phase_name,
                'status':     'done',
                'model':      task['model'],
                'tokens_in':  tok_in,
                'tokens_out': tok_out,
                'cost_usd':   cost,
                'summary':    summary,
                'output_file': output_file,
                'work_session_slot': task.get('work_session_slot'),
            }
            result.update(git_info)
            try:
                _hb_stop()
            except Exception:
                pass
            return result

        except NotImplementedError as e:
            try:
                _hb_stop()
            except Exception:
                pass
            msg = str(e)
            if git_active:
                agit.discard_task_branch(project_path, git_branch)
            db.finish_execution(exec_id, 'failed', error_message=msg, project_path=project_path)
            db.update_task(task['id'], status='failed', project_path=project_path)
            return {'task_id': task['id'], 'exec_id': exec_id, 'status': 'failed', 'error': msg}

        except ExecutionCancelledError as e:
            # The execution was cancelled while the provider subprocess was in
            # flight. The /cancel endpoint already marked the execution failed and
            # reset the task to pending — do not overwrite that with 'failed' or
            # re-run the task. Just stop cleanly.
            try:
                _hb_stop()
            except Exception:
                pass
            if git_active:
                try:
                    agit.discard_task_branch(project_path, git_branch)
                except Exception:
                    pass
            return {
                'task_id': task['id'], 'exec_id': exec_id, 'status': 'cancelled',
                'error': str(e), 'work_session_slot': task.get('work_session_slot'),
            }

        except Exception as e:
            try:
                _hb_stop()
            except Exception:
                pass
            msg = str(e)
            # Quota detection: if Claude Pro / Mistral Pro burned through its rolling
            # window, requeue the task (status=confirmed) rather than marking failed.
            # The slot scheduler (agent_api._slot_scheduler) will resume at the next
            # window. Patterns cover Claude CLI stderr + Anthropic SDK + Vibe CLI.
            if git_active:
                # Discard the branch so a requeued retry (or a clean tree) starts fresh.
                agit.discard_task_branch(project_path, git_branch)
            if _looks_like_quota_error(msg):
                db.finish_execution(exec_id, 'failed', error_message=f'quota_paused: {msg}',
                                    project_path=project_path)
                db.update_task(task['id'], status='confirmed', project_path=project_path)
                return {
                    'task_id': task['id'], 'exec_id': exec_id, 'status': 'quota_paused',
                    'error': msg, 'work_session_slot': task.get('work_session_slot'),
                }
            db.finish_execution(exec_id, 'failed', error_message=msg, project_path=project_path)
            db.update_task(task['id'], status='failed', project_path=project_path)
            return {'task_id': task['id'], 'exec_id': exec_id, 'status': 'failed', 'error': msg}

    except Exception as e:
        # Pre-dispatch failure: prompt build, git setup, guards or a model-fit
        # abort. The dispatch block has its own handlers, so this only fires for
        # failures before it — previously those propagated out of run_task and
        # left the execution row 'running'.
        _log.exception('[exec] task %s failed before dispatch', task.get('id'))
        if locals().get('git_active'):
            try:
                agit.discard_task_branch(project_path, git_branch)
            except Exception:
                pass
        try:
            db.finish_execution(exec_id, 'failed', error_message=str(e),
                                project_path=project_path)
        except Exception:
            pass
        try:
            db.update_task(task['id'], status='failed', project_path=project_path)
        except Exception:
            pass
        return {'task_id': task['id'], 'exec_id': exec_id, 'status': 'failed',
                'error': str(e)}

    finally:
        # Always stop the heartbeat and make sure the execution row leaves
        # the "running" state, whatever path we exited through. The
        # finish_execution status guard makes this a no-op when a handler
        # already wrote a terminal state.
        try:
            _hb_stop()
        except Exception:
            pass
        try:
            _agent_tools._output_dir.reset(_out_token)
        except Exception:  # not set yet (early exit) or already reset
            pass
        # Failed / cancelled / timed-out runs skip the success-path relocation,
        # and discard_task_branch's `checkout -f` keeps untracked files — so the
        # next run's "Checkpoint before task" commit swept them into the default
        # branch (task #10001192: runs 1-2 left 16 scripts in the root). Move
        # them now, before the project lock is released. No-op after a
        # successful run (its root files were already moved).
        try:
            _residue = _relocate_new_root_files(
                project_path, locals().get('_root_before'), task['_output_dir'])
            if _residue:
                _log.info('task %s: moved %d root file(s) left by the run to %s: %s',
                          task['id'], len(_residue), task['_output_dir'], _residue)
        except Exception as _rel_err:
            _log.warning('root residue relocation failed for task %s: %s',
                         task.get('id'), _rel_err)
        try:
            db.finish_execution(exec_id, 'failed',
                error_message='Run ended without a terminal state (interrupted)',
                project_path=project_path)
        except Exception:
            pass
        try:
            _plock.release()
        except Exception:
            pass



# ── Chat replies ──────────────────────────────────────────────────────────────

def _read_capped(path, cap=ATTACHMENT_CHAR_CAP):
    if not path or not os.path.exists(path):
        return ''
    try:
        with open(path, encoding='utf-8', errors='ignore') as f:
            text = f.read()
    except Exception:
        return ''
    if len(text) > cap:
        return text[:cap] + f'\n\n[…truncated at {cap} chars…]'
    return text


def _load_attachment(att, project_path):
    """Resolve one attachment to (header, body) or None if not loadable.
    Attachment shape: {'kind': str, 'ref': str, 'label': str}
      kind='definition'  → ref is filename relative to project root (CLAUDE.md, GUIDE.md, Skills.md)
      kind='memory'      → ref is filename inside Artifacts/  (project.memory.md, phase-1.memory.md…)
      kind='task'        → ref is task id (string or int)
      kind='working_doc' → ref is filename inside Working Docs / Working Documents / etc.
    """
    kind  = (att.get('kind') or '').strip()
    ref   = att.get('ref')
    label = (att.get('label') or '').strip()

    if kind == 'definition':
        path = _safe_resolve(project_path, str(ref))
        if path:
            body = _read_capped(path)
            if body:
                return (f'## {label or ref} (project setup)', body)

    elif kind == 'memory':
        path = _safe_resolve(project_path, 'Artifacts', str(ref))
        if path:
            body = _read_capped(path)
            if body:
                return (f'## Memory: {label or ref}', body)

    elif kind == 'task':
        try:
            tid = int(ref)
        except (TypeError, ValueError):
            return None
        t = db.get_task(tid)
        if not t:
            return None
        # Scope the lookup to the project that owns this prompt: a task id is
        # only unique per project, so an unscoped get_task would let a member
        # of one project read another tenant's task description.
        try:
            proj = db.get_project_by_path(project_path) if project_path else None
        except Exception:
            proj = None
        if proj and t.get('project_id') is not None and t.get('project_id') != proj.get('id'):
            return None
        block = f'**Task:** {t["title"]}'
        td = (t.get('description') or '').strip()
        if td:
            block += f'\n\n**Instructions:**\n\n{td}'
        if t.get('phase_name'):
            block = f'**Phase:** {t["phase_name"]}\n\n' + block
        return (f'## Task: {label or t["title"]}', block)

    elif kind == 'working_doc':
        for folder in ('Working Docs', 'Working Documents', 'working-docs', 'docs'):
            path = _safe_resolve(project_path, folder, str(ref))
            if path and os.path.exists(path):
                body = _read_capped(path)
                if body:
                    return (f'## Working Doc: {label or ref}', body)

    return None


def _parse_attachments(chat):
    """Return chat.attachments as a Python list, tolerating bad JSON."""
    raw = chat.get('attachments')
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


AUTO_INJECT_DEFS = ('READMEFIRST.md', 'GUIDE.md', 'Skills.md')


def _auto_def_sections(project_path, exclude_refs):
    """Return [(header, body)] for project-root Defs that exist and are not in exclude_refs."""
    if not project_path:
        return []
    sections = []
    for fname in get_def_files_for_project(project_path):
        if fname in exclude_refs:
            continue
        body = _read_capped(os.path.join(project_path, fname))
        if body:
            sections.append((f'## {fname} (project setup, auto-loaded)', body))
    return sections


def _build_task_context_block(task_id):
    """Build a '## Task context' block for a task-scoped chat: title, description,
    and the latest done execution's output_summary / aingel_brief / git info.
    Returns a string (without leading '## '), or None if the task can't be loaded.
    """
    try:
        t = db.get_task(task_id)
    except Exception:
        t = None
    if not t:
        return None

    lines = []
    if t.get('phase_name'):
        lines.append(f'**Phase:** {t["phase_name"]}')
    lines.append(f'**Task #{t["id"]}:** {t["title"]}')
    desc = (t.get('description') or '').strip()
    if desc:
        lines.append('')
        lines.append('**Instructions:**')
        lines.append(desc)

    try:
        ex = db.get_latest_done_execution_for_task(task_id)
    except Exception:
        ex = None
    if ex:
        lines.append('')
        lines.append(f'**Latest execution:** #{ex["id"]} (done, {ex.get("started_at", "")})')
        if ex.get('git_branch') or ex.get('git_commit'):
            lines.append(f'- Git: `{ex.get("git_branch") or ""}` @ `{ex.get("git_commit") or ""}`')
        brief = (ex.get('aingel_brief') or '').strip()
        if brief:
            lines.append('')
            lines.append('**Execution summary (Guide brief):**')
            lines.append(brief)
        summary = (ex.get('output_summary') or '').strip()
        if summary and summary != brief:
            lines.append('')
            lines.append('**Execution output:**')
            lines.append(summary)

    body = '\n'.join(lines)
    return f'## Task context (auto-loaded)\n\n{body}' if lines else None


CHAT_OUTPUT_FORMAT = (
    '## Output format\n\n'
    'Reply based only on the context above. '
    'If you need information that is not attached, say so explicitly — do not invent file contents. '
    'If the user asks a question, answer it directly. '
    'If the user requests work, perform it and show full results. '
    'This is a non-interactive run: do not ask the user clarification questions; make a reasonable assumption and state it.'
)


def _task_status_section(project_path):
    """Compact '## Task status' table of every non-archived task in the project.

    Lets the AIngel chat answer task-state questions ("is Task 42 running?")
    from real DB state instead of guessing from file context. Fail-open:
    returns '' on any error so the chat never breaks.
    """
    if not project_path:
        return ''
    try:
        pconn = db.get_project_db(project_path)
        rows = pconn.execute(
            '''
            SELECT t.id, t.title, t.status, t.model, t.work_session_slot,
                   e.id            AS exec_id,
                   e.status        AS exec_status,
                   e.tokens_input,
                   e.tokens_output,
                   e.started_at,
                   e.last_heartbeat_at
            FROM tasks t
            LEFT JOIN executions e ON e.id = (
                SELECT e2.id FROM executions e2
                WHERE e2.task_id = t.id
                ORDER BY e2.started_at DESC, e2.id DESC LIMIT 1
            )
            WHERE COALESCE(t.archived, 0) = 0
            '''
        ).fetchall()
        pconn.close()
    except Exception:
        return ''

    if not rows:
        return ''

    lines = [
        '## Task status',
        '',
        '| ID | Status | Model | Slot | Last exec | Title |',
        '|----|--------|-------|------|-----------|-------|',
    ]
    # Surface problems (running/failed) first, then the rest by id.
    def _sort_key(r):
        st = (r['status'] or '').lower()
        return (0 if st in ('running', 'failed') else 1, r['id'])

    cap = 3000
    body_chars = 0
    for r in sorted(rows, key=_sort_key):
        title = (r['title'] or '').strip()
        if len(title) > 60:
            title = title[:57] + '...'
        slot = r['work_session_slot'] or ''
        if r['exec_id']:
            last_exec = f"#{r['exec_id']} {r['exec_status']} {r['tokens_input']}↑{r['tokens_output']}↓"
            # Show elapsed minutes for running executions so the model can
            # distinguish "just started" from "stuck for 18 min".
            # 2026-08-21 fix (task 10001088): Vibe CLI commits at END, so
            # checking git/filesystem mid-run is meaningless. Guard stuck
            # assessment with elapsed threshold — <10 min is always "in progress".
            # The executor heartbeat (every 30s) is the liveness signal: a fresh
            # heartbeat means the worker is alive however long it has run.
            # (sqlite3.Row has no .get() — that AttributeError used to drop this
            # whole table whenever a task was running; task 10001187.)
            if (r['exec_status'] or '').lower() == 'running' and r['started_at']:
                try:
                    from datetime import datetime as _dt
                    _now = _dt.utcnow()
                    _started = _dt.strptime(r['started_at'][:19], '%Y-%m-%d %H:%M:%S')
                    _age = int((_now - _started).total_seconds() // 60)
                    last_exec += f' · {_age} min'
                    if r['last_heartbeat_at']:
                        _hb = _dt.strptime(r['last_heartbeat_at'][:19], '%Y-%m-%d %H:%M:%S')
                        _hb_age = int((_now - _hb).total_seconds())
                        if _hb_age <= 120:
                            last_exec += f' · heartbeat {_hb_age}s ago – ALIVE, not stuck'
                        else:
                            last_exec += f' · heartbeat {_hb_age // 60} min ago – possibly stalled'
                    elif _age < 10 and (r['tokens_input'] or 0) == 0 and (r['tokens_output'] or 0) == 0:
                        last_exec += ' · in progress (<10 min – not stuck, Vibe batches at end)'
                except Exception:
                    pass
        else:
            last_exec = '—'
        line = f"| #{r['id']} | {r['status']} | {r['model']} | {slot} | {last_exec} | {title} |"
        body_chars += len(line)
        if body_chars > cap:
            lines.append('| _…further tasks omitted…_ |')
            break
        lines.append(line)

    lines.append('')
    lines.append(
        '> **Task completion is determined by the Last exec column above, not by file '
        '> modification times or git history.** If a task shows `running`, it is in '
        '> progress even if output files exist or git shows no commits / clean tree. '
        '> A `running` task whose heartbeat is ≤2 min old is ALIVE — never call it stuck, '
        '> whatever its age, token count, output folder or git state (token counts and '
        '> files are only written when the run ends). Only report a task as stuck when '
        '> its heartbeat is stale (>2 min) or, with no heartbeat, it has been `running` '
        '> with `0↑0↓` for **>10 min**.'
    )
    return '\n'.join(lines)


def _chat_prompt_sections(chat, user_message, chosen_model=None, tools_enabled=None):
    """Ordered [(key, text)] sections that make up a chat prompt:
       - the AIngel role line, when the project has one
       - auto-loaded Defs (READMEFIRST.md / CLAUDE.md legacy, GUIDE.md, Skills.md) at project root,
         unless chat.auto_inject_defs is 0 or the file is already explicitly attached
       - explicit attachments on the chat (definitions, memories, tasks, working docs)
       - task context, for task-scoped chats
       - the chat history
       - the new user message
       - the output-format instructions

    `tools_enabled` overrides the model-capability-based tool detection. When
    provided, it is used instead of `model_capabilities(...)['file_access']` so
    the prompt only advertises tools the chat route will actually bind. Pass
    `None` to keep the legacy capability-based behaviour (used by callers that
    haven't been threaded yet). See `_chat_tools_enabled`.

    Single source of truth for `_build_chat_prompt` (which joins the texts) and
    `estimate_chat_prompt_tokens` (which counts them per key). Keep them sharing
    this function — the two drifting apart is what made task-chat estimates
    understate the real prompt by the whole task-context block.
    """
    project_path = chat.get('project_path') or ''
    sections = []

    proj = db.get_project(chat.get('project_id')) if chat.get('project_id') else None
    aingel_name = ((proj or {}).get('aingel_name') or '').strip()
    proj_name = ((proj or {}).get('name') or '').strip()
    if aingel_name:
        sections.append(('role', f'## Role\n\nYou are {aingel_name}, Guide for the {proj_name} project.'))

    attachments = _parse_attachments(chat)

    auto_inject = chat.get('auto_inject_defs', 1)
    if auto_inject in (None, '', 1, '1', True):
        explicit_def_refs = {
            str(a.get('ref'))
            for a in attachments
            if (a.get('kind') or '').strip() == 'definition'
        }
        for header, body in _auto_def_sections(project_path, explicit_def_refs):
            sections.append((f'auto_def:{header.split()[1]}', f'{header}\n\n{body}'))

    for att in attachments:
        loaded = _load_attachment(att, project_path)
        if loaded:
            header, body = loaded
            label = att.get('label') or att.get('ref') or att.get('kind') or 'attachment'
            sections.append((f'attachment:{label}', f'{header}\n\n{body}'))

    # Auto-inject task context for task-scoped chats (e.g. opened from Execution Log).
    # Skipped if the task is already an explicit attachment.
    task_id = chat.get('task_id')
    if task_id and not any(
        (a.get('kind') or '').strip() == 'task' and str(a.get('ref') or '') == str(task_id)
        for a in attachments
    ):
        task_block = _build_task_context_block(task_id)
        if task_block:
            sections.append(('task_context', task_block))

    # ── Project context: memory, skills, files, H3/H4 analysis ───────────
    # These sections make the AIngel chat context-aware, not just a blind
    # text conversation. They mirror what run_task() injects via _build_prompt.
    if project_path:
        # Project memory — rolling project state
        try:
            proj_mem = read_memory(project_path, level='project')
            if proj_mem:
                is_tool_loop = (chosen_model or '').startswith('scw-') or (chosen_model or '').startswith('oll-')
                if is_tool_loop:
                    proj_mem = _cap_text(proj_mem, SCW_PROJ_MEM_CAP)
                sections.append(('project_memory', f'## Project memory\n\n{proj_mem}'))
        except Exception:
            pass

        # Phase memory — for task-scoped or phase-scoped chats
        phase_name = ''
        if task_id:
            try:
                t = db.get_task(task_id)
                if t:
                    phase_name = phase_from_task(t) or ''
            except Exception:
                pass
        if not phase_name:
            phase_name = chat.get('phase_name') or ''
        if phase_name:
            try:
                phase_mem = read_memory(project_path, level='phase', phase_name=phase_name)
                if phase_mem:
                    is_tool_loop = (chosen_model or '').startswith('scw-') or (chosen_model or '').startswith('oll-')
                    if is_tool_loop:
                        phase_mem = _cap_text(phase_mem, SCW_PHASE_MEM_CAP)
                    sections.append(('phase_memory', f'## {phase_name} memory\n\n{phase_mem}'))
            except Exception:
                pass

        # Skills summary — compact, tells the AIngel what stack the project uses
        try:
            skills = skills_context_summary(project_path)
            if skills:
                sections.append(('skills', f'## Project skills\n\n{skills}'))
        except Exception:
            pass

        # File listing — always inject so the AIngel knows what files exist
        try:
            docs = list_working_docs(project_path)
            if docs:
                file_lines = []
                for f in docs:
                    size_kb = f.get('size', 0) / 1024
                    file_lines.append(f'- {f["name"]} ({size_kb:.0f} KB)')
                sections.append(('file_listing', f'## Project files\n\nThe following files are available in Working Documents:\n\n' + '\n'.join(file_lines)))
        except Exception:
            pass

        # File auto-catch — inline file contents when the user message triggers
        try:
            from prompt_builder import _resolve_referenced_files, _approx_token_count
            from model_caps import model_capabilities
            caps = model_capabilities(chosen_model)
            ctx_window = caps.get('context_window') or 128000
            inline_budget = int(ctx_window * 0.50)
            single_file_max = int(ctx_window * 0.40)
            synthetic_task = {'description': user_message or '', 'title': ''}
            inline_files, batch_files = _resolve_referenced_files(
                synthetic_task, project_path, inline_budget, single_file_max,
                catchall_disabled=True)
            if inline_files:
                file_blocks = []
                for fname, content in inline_files:
                    if content is not None:
                        file_blocks.append(f'**File: {fname}**\n```\n{content}\n```')
                    else:
                        file_blocks.append(f'**File: {fname}** (binary file — content not inlined)')
                if file_blocks:
                    sections.append(('file_contents', f'## Referenced file contents\n\n' + '\n\n'.join(file_blocks)))
            if batch_files:
                batch_names = [f for f, _ in batch_files]
                sections.append(('file_batch_note',
                    f'## Large files noted\n\nThe following files are too large to inline but are available in Working Documents: {", ".join(batch_names)}'))
        except Exception:
            pass

        # H3 review injection — for task-scoped chats, show the AIngel's last review
        if task_id:
            try:
                ex = db.get_latest_done_execution_for_task(task_id)
                if ex and ex.get('aingel_review_json'):
                    try:
                        review = json.loads(ex['aingel_review_json'])
                        review_text = json.dumps(review, indent=2, ensure_ascii=False)[:2000]
                        sections.append(('h3_review', f'## Guide\'s last review of this task\n\n```json\n{review_text}\n```'))
                    except Exception:
                        pass
            except Exception:
                pass

        # H4 overview injection — for project-scoped AIngel chats
        if not task_id:
            try:
                overview_path = os.path.join(project_path, 'Artifacts', 'aingel-overview.md')
                if os.path.exists(overview_path):
                    with open(overview_path, encoding='utf-8', errors='ignore') as f:
                        overview = f.read().strip()
                    if overview:
                        overview = _cap_text(overview, 4000)
                        sections.append(('h4_overview', f'## Project overview\n\n{overview}'))
            except Exception:
                pass

        # Task status — compact table so the AIngel can answer task-state questions
        try:
            status_block = _task_status_section(project_path)
            if status_block:
                sections.append(('task_status', status_block))
        except Exception:
            pass

    # read_chat_file already caps at CHAT_CONTEXT_CHARS (keeping the tail).
    history = chats.read_chat_file(chat.get('file_path') or '')
    if history:
        sections.append(('chat_history', f'## Chat so far\n\n{history}'))

    sections.append(('user_message', f'## New user message\n\n{(user_message or "").strip()}'))

    # Agentic tool instructions — only when the model can actually act on files.
    # `tools_enabled` (when passed) reflects the route `chat_reply` will really
    # take, including the vibe-CLI presence fallback for Mistral. Without it we
    # fall back to the model-capability flag, which over-promises for Mistral
    # API mode and triggers the "simulation gap" (model emits the tool call as
    # plain text, nothing executes, chat appears to hang).
    from model_caps import model_capabilities as _mc
    caps = _mc(chosen_model) if chosen_model else {}
    file_access = caps.get('file_access')
    if tools_enabled is None:
        has_real_tools = file_access in ('native', 'bash', 'tools')
    else:
        has_real_tools = bool(tools_enabled)
    if has_real_tools:
        if file_access == 'bash':
            tool_desc = 'You have access to bash tools including read_file, list_files, and write_file via the Vibe CLI.'
        elif file_access == 'native':
            tool_desc = 'You have native filesystem access via Read, Write, Edit, and Bash tools.'
        else:
            tool_desc = 'You have access to tools: read_file, list_files, write_file.'
        sections.append(('agentic_rules',
            f'## Tool access\n\n'
            f'{tool_desc} '
            'You can read project files, list directories, and create files. '
            'Use these tools when the user asks you to work with files. '
            'Do not simulate tool calls — call them for real.'
        ))
        sections.append(('output_format',
            '## Output format\n\n'
            'Reply using the context above and your tools. '
            'If you need information that is not in the context, use read_file or list_files to get it. '
            'If the user asks a question, answer it directly. '
            'If the user requests work, perform it and show full results. '
            'This is a non-interactive run: do not ask the user clarification questions; make a reasonable assumption and state it.'
        ))
    else:
        sections.append(('output_format', CHAT_OUTPUT_FORMAT))

    return sections


def _build_chat_prompt(chat, user_message, chosen_model=None, tools_enabled=None):
    """Build a prompt for a chat message from `_chat_prompt_sections`."""
    return '\n\n---\n\n'.join(
        text for _key, text in _chat_prompt_sections(chat, user_message, chosen_model, tools_enabled)
    )


def chat_reply(chat_id, user_message, model=None):
    """Send a user message into a chat, get an AI reply.
    Returns dict with exec_id, reply text, tokens, cost.
    """
    chat = db.get_chat(chat_id)
    if not chat:
        return {'error': f'Chat {chat_id} not found'}

    user_message = (user_message or '').strip()
    if not user_message:
        return {'error': 'Empty message'}

    project_path = chat.get('project_path') or ''
    chat_proj = _project_for(chat)
    chosen_model = (model or chat.get('model') or chat.get('task_model')
                    or agent_config.default_model_for(chat_proj))

    # EU boundary: chats reach here from several routes (project chat, task chat,
    # AIngel chat), not all of which pass through the API-level guard.
    ok_eu, err_eu = agent_config.eu_guard(chat_proj, chosen_model)
    if not ok_eu:
        agent_config.eu_audit(project_path, model=chosen_model,
                              provider=MODELS.get(chosen_model, {}).get('provider', ''),
                              allowed=False, caller='chat_reply', reason=err_eu)
        return {'error': err_eu}

    # Ensure chat file exists
    chat_path = chat.get('file_path') or ''
    if not chat_path or not os.path.exists(chat_path):
        chat_path = chats.create_chat_file(project_path, chat)
        db.update_chat(chat_id, file_path=chat_path)
        chat['file_path'] = chat_path

    # Append user message before calling the model so it survives a failed call
    chats.append_user_message(chat_path, user_message)

    # Free-tier pre-flight token check — fail before spending provider credits.
    try:
        import agent_quotas
        _owner = (chat.get('created_by')
                  or (chat_proj or {}).get('owner_id'))
        if _owner:
            agent_quotas.check_token_budget(_owner)
    except agent_quotas.QuotaError as e:
        return {'error': str(e), 'quota': e.field, 'limit': e.limit}

    try:
        exec_id = db.create_execution(
            task_id=chat.get('task_id'),
            model=chosen_model,
            chat_id=chat_id,
            project_path=project_path or None,
            created_by=(chat.get('created_by')
                        or (chat_proj or {}).get('owner_id')),
        )
        _chat_hb = _start_heartbeat(exec_id, project_path=project_path or None)
    except Exception as e:
        return {'error': f'Failed to create execution: {e}'}

    # Decide whether this chat turn will really give the model tools, then build
    # a prompt that only advertises tools when they'll actually be bound. This
    # closes the "simulation gap" where Mistral API mode (text-only) was told it
    # had read_file/list_files and emitted them as plain text, ending the turn
    # with nothing executed. See `_chat_tools_enabled` for the routing mirror.
    try:
        tools_enabled = _chat_tools_enabled(chosen_model, project_path)
        prompt = _build_chat_prompt(chat, user_message, chosen_model, tools_enabled)

        # Dynamic max_tokens — size from context window, floor 2048
        from model_caps import model_capabilities, effective_max_tokens
        caps = model_capabilities(chosen_model)
        ctx_window = caps.get('context_window') or 128000
        prompt_tokens = len(prompt) // 2  # conservative 2 chars/token
        dynamic_max = max(2048, ctx_window - prompt_tokens - 500)
        chat_max_tokens = min(dynamic_max, MAX_TOKENS * 2)  # cap at 2x default to avoid excessive output
    except Exception as e:
        # Prompt construction happens after the execution row exists, so a
        # failure here must still stop the heartbeat and finish the execution
        # instead of leaving it 'running' forever.
        try:
            _chat_hb()
        except Exception:
            pass
        db.finish_execution(exec_id, 'failed',
                            error_message=f'chat prompt build failed: {e}',
                            project_path=project_path or None)
        return {'chat_id': chat_id, 'exec_id': exec_id, 'error': str(e)}

    try:
        # Route through the model's default path — no force_mistral_mode='api'.
        # Previously the chat path hard-forced Mistral onto the text-only API
        # route, which (combined with the prompt advertising tools) produced the
        # simulation gap. Letting Mistral use the global MISTRAL_MODE='vibe'
        # gives the chat the real tool loop (read_file/list_files/write_file
        # via the Vibe CLI). When vibe is unavailable _chat_tools_enabled returns
        # False and the prompt degrades to the text-only format, so the prompt
        # and the route always agree.
        text, tok_in, tok_out, cost = route(chosen_model, prompt, chat_max_tokens,
                                             project_path=project_path,
                                             exec_id=exec_id,
                                             caller='chat_reply')
        _raise_if_denied_cli_result(text, chosen_model)
        summary = text[:2000] + ('…' if len(text) > 2000 else '')
        db.finish_execution(
            exec_id, 'done',
            tokens_input=tok_in, tokens_output=tok_out,
            cost_usd=cost, output_summary=summary,
            project_path=project_path or None,
        )
        try:
            _chat_hb()
        except Exception:
            pass
        chats.append_assistant_message(chat_path, text, chosen_model, tok_in, tok_out, cost, exec_id)
        db.touch_chat(chat_id, project_path=project_path or None)

        # Commit any file changes made by this chat execution so they are not
        # left as uncommitted noise that confuses subsequent task branch operations.
        if project_path:
            # Serialize with a running task's git work (audit P1-2 / C1). This is
            # a NON-BLOCKING acquire: if a task already holds the project lock we
            # SKIP the chat's git commit rather than race the task's branch
            # operations. The next task checkpoint or task commit picks the chat's
            # file changes up, so nothing is lost. C1.
            _chat_lock = _project_lock(project_path)
            _held = bool(_chat_lock.acquire(timeout=0))
            try:
                if not _held:
                    _log.info(
                        'chat %s: project lock held by another run — skipping git '
                        'commit; changes will be captured by the next checkpoint/task',
                        chat_id)
                else:
                    proj = db.get_project(chat['project_id'])
                    git_enabled = agit.resolve_enabled(
                        proj.get('git_enabled') if proj else None,
                        project_path,
                    )
                    if git_enabled:
                        gc = agit.commit_chat(project_path, chat_id, chat.get('name', ''))
                        if gc.get('changed') and gc.get('commit'):
                            db.update_execution_git(exec_id, commit=gc['commit'],
                                                    diffstat=gc.get('diffstat', ''),
                                                    project_path=project_path)
            except Exception as _git_err:
                _log.warning('chat_reply git commit failed for exec %s: %s', exec_id, _git_err)
            finally:
                if _held:
                    try:
                        _chat_lock.release()
                    except Exception:
                        pass

        return {
            'chat_id':  chat_id,
            'exec_id':  exec_id,
            'reply':    text,
            'model':    chosen_model,
            'tokens_in':  tok_in,
            'tokens_out': tok_out,
            'cost_usd':   cost,
        }
    except NotImplementedError as e:
        try:
            _chat_hb()
        except Exception:
            pass
        msg = str(e)
        db.finish_execution(exec_id, 'failed', error_message=msg, project_path=project_path or None)
        chats.append_system_note(chat_path, f'Provider error: {msg}')
        return {'chat_id': chat_id, 'exec_id': exec_id, 'error': msg}
    except Exception as e:
        try:
            _chat_hb()
        except Exception:
            pass
        msg = str(e)
        db.finish_execution(exec_id, 'failed', error_message=msg, project_path=project_path or None)
        chats.append_system_note(chat_path, f'Error: {msg}')
        return {'chat_id': chat_id, 'exec_id': exec_id, 'error': msg}
