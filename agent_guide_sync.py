"""GUIDE.md regeneration from the per-project task table.

GUIDE.md is now a derived view of `<project>/project.db` `tasks` (DB is source of
truth). This module is the single writer for GUIDE.md; the legacy
`agent_memory.update_guide_task` fuzzy-matcher has been retired in favour of full
regeneration on every task change.

Hook points (in agent_db.py):
- create_task
- update_task (covers archive_task, redo_task, save_task_handoff,
  clear_task_handoff, copy_task via create_task)
- delete_task

Bulk imports (agent_importer.import_phase_tasks_for_project) suppress per-row
hooks and call `regenerate_guide` once at the end via the `suppress` context.
"""
import os
import re
import threading
from contextlib import contextmanager

# Status -> markdown marker (mirrors existing emoji convention in
# agent_memory.update_guide_task and the phase-stats logic in agent_phases).
_STATUS_MARKERS = {
    'done':       '- [x] ',     # ✅
    'skip':       '- [x] ',     # ✅ skipped (strikethrough applied to title below)
    'failed':     '- [ ] 🔴 ',
    'blocked':    '- [ ] \U0001f6a7 ',  # 🚧
    'running':    '- [ ] \U0001f504 ',  # 🔄
    'confirmed':  '- [ ] ',
    'pending':    '- [ ] ',
}

_AUTO_GEN_HEADER = (
    "<!-- Auto-generated from the task database. Manual edits will be "
    "overwritten on the next task change. -->\n"
)


_per_project_locks = {}
_locks_mutex = threading.Lock()


def _lock_for(project_path):
    p = os.path.realpath(project_path) if project_path else ''
    with _locks_mutex:
        lock = _per_project_locks.get(p)
        if lock is None:
            lock = threading.Lock()
            _per_project_locks[p] = lock
        return lock


_suppress_local = threading.local()


@contextmanager
def suppress():
    """Disable per-row regeneration inside this block. Use around bulk
    importers; call `regenerate_guide` once at the end of the block.

    Thread-local: one thread's `suppress()` does not affect another thread's
    regeneration, so concurrent bulk imports on different projects can each
    suppress their own per-row hooks without cross-contamination.
    """
    depth = getattr(_suppress_local, 'depth', 0) + 1
    _suppress_local.depth = depth
    try:
        yield
    finally:
        _suppress_local.depth = depth - 1


def _is_suppressed():
    return getattr(_suppress_local, 'depth', 0) > 0


def _read_tasks(project_path, project_id):
    """Return list of task dicts ordered by phase, slot, creation time."""
    import sqlite3
    db_path = os.path.join(project_path, 'project.db')
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            'SELECT title, status, phase_name, COALESCE(archived,0) AS archived, '
            'work_session_slot, slot_position, created_at '
            'FROM tasks WHERE project_id = ? '
            'ORDER BY phase_name, work_session_slot, slot_position, created_at, id',
            (project_id,),
        ).fetchall()
    finally:
        conn.close()
    return rows


def _phase_order(project_path, project_id, rows):
    """Return phases in alphabetical order, with 'Notebook' last.

    `rows` come from `_read_tasks` which orders by `phase_name`, so the input
    is already alphabetical. Empty-string phase is normalised to 'Notebook'
    and pushed to the end of the list regardless of alphabetic position.
    """
    seen = set()
    order = []
    for r in rows:
        ph = (r['phase_name'] or '').strip() or 'Notebook'
        if ph not in seen:
            seen.add(ph)
            order.append(ph)
    if 'Notebook' in order:
        order.remove('Notebook')
        order.append('Notebook')
    return order


def _format_task_line(task):
    status = task['status'] or 'pending'
    archived = bool(task['archived'])
    title = (task['title'] or '').strip().replace('\n', ' ')
    if archived and status != 'done':
        marker = '- [ ] \U0001f4e6 '  # 📦 archived
    else:
        marker = _STATUS_MARKERS.get(status, '- [ ] ')
    line = f"{marker}{title}"
    if status == 'skip':
        line = f"{marker}~~{title}~~"
    return line


def regenerate_guide(project_path, project_id):
    """Regenerate `<project>/GUIDE.md` from the project's tasks.

    No-op if GUIDE.md regeneration is currently suppressed (bulk-import path).
    No-op (returns False) if the project has no `project.db` or no tasks —
    leaves the existing file untouched so `parse_guide` fallback still works.

    All exceptions are swallowed and logged so a GUIDE write failure never
    propagates back into a committed task mutation.

    Returns True if the file was rewritten, False otherwise.
    """
    if not project_path or _is_suppressed():
        return False

    try:
        rows = _read_tasks(project_path, project_id)
        if not rows:
            return False

        project_name = os.path.basename(os.path.normpath(project_path))
        phases = _phase_order(project_path, project_id, rows)

        out = []
        out.append(f"# {project_name} \u2014 Roadmap\n")
        out.append(_AUTO_GEN_HEADER.rstrip())
        out.append('')

        for idx, phase in enumerate(phases, start=1):
            out.append(f"## Phase {idx} \u2014 {phase}\n")
            phase_rows = [r for r in rows if ((r['phase_name'] or '').strip() or 'Notebook') == phase]
            if not phase_rows:
                out.append('_No tasks yet._\n')
            else:
                for r in phase_rows:
                    out.append(_format_task_line(r) + '\n')
            out.append('')

        new_content = ''.join(out)

        guide_path = os.path.join(project_path, 'GUIDE.md')
        with _lock_for(project_path):
            existing = ''
            if os.path.exists(guide_path):
                with open(guide_path, encoding='utf-8') as f:
                    existing = f.read()
            if existing == new_content:
                return False
            tmp = guide_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(new_content)
            os.replace(tmp, guide_path)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            'GUIDE.md regen failed for %s: %s', project_path, exc
        )
        return False
    return True


def after_task_change(project_path, project_id=None, task_id=None):
    """Convenience hook for callers. Resolves project_id if needed."""
    if _is_suppressed() or not project_path:
        return False
    if project_id is None and task_id is not None:
        import agent_db as db
        project_id = db._resolve_project_id(task_id=task_id, project_path=project_path)
    if project_id is None:
        return False
    return regenerate_guide(project_path, project_id)