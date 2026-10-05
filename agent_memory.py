"""
Memory manager — Phase 3.
Manages the Artifacts/ folder and memory files per project.
(Chat transcripts live in agent_chats; the legacy session-*.chat.md writers
were removed in the sessions→chats consolidation.)
"""
import os
import re
from datetime import datetime

import agent_files

ARTIFACTS_DIR = 'Artifacts'


def _artifacts(project_path):
    d = os.path.join(project_path, ARTIFACTS_DIR)
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(d, 'outputs'), exist_ok=True)
    return d


def _phase_slug(phase_name):
    """'Phase 1 — Setup' → 'phase-1'"""
    m = re.search(r'\d+', phase_name or '')
    return f'phase-{m.group()}' if m else re.sub(r'[^a-z0-9]+', '-', (phase_name or 'misc').lower()).strip('-')


def _memory_path(project_path, level='project', phase_name=None, workflow_id=None):
    d = _artifacts(project_path)
    if level == 'phase' and phase_name:
        return os.path.join(d, f'{_phase_slug(phase_name)}.memory.md')
    if level == 'workflow' and workflow_id:
        return os.path.join(d, f'workflow-{workflow_id}.memory.md')
    return os.path.join(d, 'project.memory.md')


# ── Read ──────────────────────────────────────────────────────────────────────

def read_memory(project_path, level='project', phase_name=None, workflow_id=None):
    path = _memory_path(project_path, level, phase_name, workflow_id)
    if not os.path.exists(path):
        return ''
    with open(path, encoding='utf-8', errors='ignore') as f:
        return f.read().strip()


def list_memory_files(project_path):
    """Return all Artifacts files: memory files + chat session logs + exec outputs."""
    d = os.path.join(project_path, ARTIFACTS_DIR)
    if not os.path.isdir(d):
        return []
    result = []
    # Scan top-level Artifacts/ for memory files
    for fname in sorted(os.listdir(d)):
        fpath = os.path.join(d, fname)
        if not os.path.isfile(fpath):
            continue
        if fname.endswith('.memory.md'):
            stat = os.stat(fpath)
            if fname == 'project.memory.md':
                mem_level = 'project'
            elif fname.startswith('workflow-'):
                mem_level = 'workflow'
            else:
                mem_level = 'phase'
            result.append({
                'name': fname, 'path': fpath,
                'level': mem_level,
                'phase': fname.replace('.memory.md', '') if mem_level == 'phase' else None,
                'size': stat.st_size,
                'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                'file_type': 'memory',
            })
    # Scan Artifacts/chats/ for chat session logs
    chats_dir = os.path.join(d, 'chats')
    if os.path.isdir(chats_dir):
        for fname in sorted(os.listdir(chats_dir)):
            fpath = os.path.join(chats_dir, fname)
            if os.path.isfile(fpath) and fname.endswith('.chat.md'):
                stat = os.stat(fpath)
                result.append({
                    'name': fname, 'path': fpath,
                    'level': 'chat', 'phase': None,
                    'size': stat.st_size,
                    'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    'file_type': 'chat',
                })
    # Scan Artifacts/outputs/ for execution output files — flat (legacy) and
    # inside each task's folder (Artifacts/outputs/<id>-<slug>/exec-N-output.md).
    outputs_dir = os.path.join(d, 'outputs')
    if os.path.isdir(outputs_dir):
        candidates = []
        for fname in sorted(os.listdir(outputs_dir)):
            fpath = os.path.join(outputs_dir, fname)
            if os.path.isdir(fpath) and not fname.startswith('.'):
                for sub in sorted(os.listdir(fpath)):
                    if sub.startswith('exec-') and sub.endswith('-output.md'):
                        candidates.append((sub, os.path.join(fpath, sub)))
            else:
                candidates.append((fname, fpath))
        for fname, fpath in candidates:
            if os.path.isfile(fpath) and not fname.startswith('.'):
                stat = os.stat(fpath)
                result.append({
                    'name': fname, 'path': fpath,
                    'level': 'output', 'phase': None,
                    'size': stat.st_size,
                    'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    'file_type': 'output',
                })
    return result


def _title_words(text):
    """Extract meaningful words from a task title for fuzzy matching."""
    words = re.findall(r'[a-zA-Z0-9]+', text.lower())
    stopwords = {'a', 'an', 'the', 'and', 'or', 'to', 'in', 'for', 'of', 'with', 'on', 'at'}
    return [w for w in words if w not in stopwords and len(w) > 2]


def update_guide_task(project_path, task_title, phase_name=None):
    """Deprecated stub. GUIDE.md is now a derived view regenerated from the
    project DB by `agent_guide_sync.regenerate_guide` on every task change.
    Kept as a no-op for backward compatibility with legacy callers.
    """
    return False


def list_working_docs(project_path):
    """List files across all Working Docs / Working Documents folder variants.

    A project can have more than one variant present at once (e.g. a legacy
    empty "Working Docs" alongside the active "Working Documents") — scanning
    only the first match that exists silently hid files in the others. Scans
    up to two levels of subdirectories so files synced from bucket prefixes
    (e.g. ``202608101109_Specyfikacja/file.pdf``) or nested project folders
    (e.g. ``CUPT/II Etap/file.pdf``) appear in the UI.
    """
    seen = set()
    result = []
    for folder_name in ('Working Docs', 'Working Documents', 'working-docs', 'docs'):
        d = os.path.join(project_path, folder_name)
        if not os.path.isdir(d):
            continue
        for fname, fpath in agent_files.list_files_under(d, max_depth=3):
            if fname in seen:
                continue
            seen.add(fname)
            stat = os.stat(fpath)
            result.append({
                'name': fname, 'path': fpath,
                'size': stat.st_size,
                'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                'ext': os.path.splitext(fname)[1].lower(),
            })
    return result


# ── Append ────────────────────────────────────────────────────────────────────

def append_to_memory(project_path, content, level='project', phase_name=None,
                     workflow_id=None, task_title='', exec_id=None):
    """Append an execution output to the appropriate memory file."""
    path = _memory_path(project_path, level, phase_name, workflow_id)
    ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M')
    header = f'\n\n---\n\n## [{ts}] {task_title or "Task output"}'
    if exec_id:
        header += f' (exec #{exec_id})'
    entry = header + '\n\n' + content.strip()

    # Init file with a header if it doesn't exist
    if not os.path.exists(path):
        if level == 'workflow':
            label = f'Workflow {workflow_id} Memory'
        elif level == 'phase':
            label = f'{phase_name or "Phase"} Memory'
        else:
            label = 'Project Memory'
        init = f'# {label}\n\n_Auto-generated by SuperAgent. Edit freely._\n'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(init)

    with open(path, 'a', encoding='utf-8') as f:
        f.write(entry)

    return path


def remove_memory_entry(project_path, exec_id, level='project', phase_name=None, workflow_id=None):
    """Remove one execution approval entry from the matching memory file."""
    path = _memory_path(project_path, level, phase_name, workflow_id)
    if not os.path.exists(path):
        return {'removed': False, 'path': path, 'error': 'memory file not found'}

    with open(path, encoding='utf-8', errors='ignore') as f:
        text = f.read()

    pattern = re.compile(
        r'\n\n---\n\n## \[[^\]]+\] [^\n]*\(exec #'
        + re.escape(str(exec_id))
        + r'\)\n\n.*?(?=\n\n---\n\n## \[|\Z)',
        re.DOTALL,
    )
    new_text, count = pattern.subn('', text, count=1)
    if count == 0:
        return {'removed': False, 'path': path, 'error': 'entry not found'}

    with open(path, 'w', encoding='utf-8') as f:
        f.write(new_text.rstrip() + '\n')

    return {'removed': True, 'path': path}


# ── Phase detection from task ─────────────────────────────────────────────────

def phase_from_task(task):
    """Return stored phase_name, falling back to external_id (P1-3 → 'Phase 1')."""
    stored = (task.get('phase_name') or '').strip()
    if stored:
        return stored
    ext_id = task.get('external_id') or ''
    if ext_id.startswith('ph::'):
        parts = ext_id.split('::', 2)
        if len(parts) >= 3 and parts[1].strip():
            return parts[1].strip()
    m = re.match(r'^P(\d+)', ext_id.upper())
    if m:
        return f'Phase {m.group(1)}'
    return None
