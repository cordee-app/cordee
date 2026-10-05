"""
Phase parser — reads phase/task status from the database (source of truth).
Falls back to GUIDE.md for projects not yet imported.
"""
import os
import re
from agent_config import PROJECTS_ROOT

SKIP_DIRS = {'Dashboard', 'SuperAgent', '.git', '__pycache__'}

EMOJI_STATUS = {
    '✅': 'done',
    '🔴': 'blocked',
    '⬛': 'todo',
}


def parse_guide(path):
    """Parse GUIDE.md (fallback) into phases with their tasks.
    Used only if TASKS.md doesn't exist."""
    try:
        with open(path, encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception:
        return []

    phases = []
    current_phase = None

    for line in content.splitlines():
        # Phase header: ## Phase N — Title  or  ## Phase N: Title
        ph = re.match(r'^#{1,3}\s+(Phase\s+\d+[^#\n]*)', line, re.IGNORECASE)
        if ph:
            current_phase = {
                'title': ph.group(1).strip(),
                'tasks': [],
            }
            phases.append(current_phase)
            continue

        if current_phase is None:
            continue

        # Task line with emoji marker
        for emoji, status in EMOJI_STATUS.items():
            if emoji in line:
                # Strip leading list marker, emoji and whitespace
                text = re.sub(r'^[\s\-\*]+', '', line)
                text = text.replace(emoji, '').strip()
                # Skip summary lines and empty
                if not text or text.lower().startswith('**summary'):
                    break
                # Truncate sub-bullets (lines starting with spaces after the main item)
                current_phase['tasks'].append({'text': text, 'status': status})
                break
        # Also catch plain checkbox tasks in guide
        else:
            m = re.match(r'\s*-\s+\[([x~\s])\]\s+(.*)', line)
            if m:
                ch = m.group(1)
                text = m.group(2).strip()
                if text:
                    status = 'done' if ch == 'x' else 'blocked' if ch == '~' else 'todo'
                    current_phase['tasks'].append({'text': text, 'status': status})
            else:
                # Catch plain bullet points (no checkbox, no emoji) as pending tasks.
                # The scaffolding AI often emits `- Task description` without `[ ]`.
                m2 = re.match(r'\s*-\s+(.+)', line)
                if m2:
                    text = m2.group(1).strip()
                    # Skip lines that look like metadata, not tasks
                    if text and not text.startswith('**') and not text.startswith('_'):
                        current_phase['tasks'].append({'text': text, 'status': 'todo'})

    # Compute stats per phase
    for ph in phases:
        tasks = ph['tasks']
        done    = sum(1 for t in tasks if t['status'] == 'done')
        blocked = sum(1 for t in tasks if t['status'] == 'blocked')
        todo    = sum(1 for t in tasks if t['status'] == 'todo')
        total   = len(tasks)
        ph['done']    = done
        ph['blocked'] = blocked
        ph['todo']    = todo
        ph['total']   = total
        ph['pct']     = round(done / total * 100) if total else 0
        # Overall phase status
        if total == 0:
            ph['status'] = 'empty'
        elif done == total:
            ph['status'] = 'done'
        elif blocked > 0:
            ph['status'] = 'blocked'
        elif done > 0:
            ph['status'] = 'in_progress'
        else:
            ph['status'] = 'todo'

    return phases


def _compute_phase_stats(phases):
    for ph in phases:
        tasks = ph['tasks']
        done    = sum(1 for t in tasks if t['status'] == 'done')
        blocked = sum(1 for t in tasks if t['status'] in ('blocked', 'failed'))
        todo    = sum(1 for t in tasks if t['status'] in ('todo', 'pending', 'confirmed', 'running'))
        total   = sum(1 for t in tasks if t['status'] not in ('skip', 'archived'))
        ph['done']    = done
        ph['blocked'] = blocked
        ph['todo']    = todo
        ph['total']   = total
        ph['pct']     = round(done / total * 100) if total else 0
        if total == 0:
            ph['status'] = 'empty'
        elif done == total:
            ph['status'] = 'done'
        elif blocked > 0:
            ph['status'] = 'blocked'
        elif done > 0:
            ph['status'] = 'in_progress'
        else:
            ph['status'] = 'todo'
    return phases


def get_all_phases():
    """Return phases for all projects, reading from each project's project.db."""
    import agent_db as db

    results = []
    central = db.get_db()
    projects = central.execute('SELECT * FROM projects ORDER BY name').fetchall()
    central.close()

    for project in projects:
        pid = project['id']
        pname = project['name']
        ppath = project['path']

        # Read tasks from per-project DB (Phase 2 architecture)
        proj_db_path = os.path.join(ppath, 'project.db') if ppath else None
        if proj_db_path and os.path.exists(proj_db_path):
            pconn = db.get_project_db(ppath)
            task_rows = pconn.execute(
                'SELECT external_id, title, phase_name, status, COALESCE(archived, 0) as archived FROM tasks WHERE project_id=? ORDER BY phase_name, created_at',
                (pid,)
            ).fetchall()
            pconn.close()
        else:
            task_rows = []

        if not task_rows:
            # Fall back to GUIDE.md for projects not yet imported
            guide_file = os.path.join(ppath, 'GUIDE.md')
            if os.path.exists(guide_file):
                phases = parse_guide(guide_file)
                if not phases:
                    continue
                phases = _compute_phase_stats(phases)
            else:
                continue
        else:
            phases_dict = {}
            phase_order = []
            for task in task_rows:
                ph = task['phase_name'] or 'Notebook'
                if ph not in phases_dict:
                    phases_dict[ph] = []
                    phase_order.append(ph)
                label = task['title']
                status_map = {
                    'done': 'done', 'failed': 'failed', 'skip': 'skip',
                    'confirmed': 'confirmed', 'running': 'running', 'pending': 'pending',
                }
                if task['archived'] and task['status'] != 'done':
                    status = 'archived'
                else:
                    status = status_map.get(task['status'], 'pending')
                phases_dict[ph].append({'text': label, 'status': status})

            phases = [{'title': ph, 'tasks': phases_dict[ph]} for ph in phase_order]
            phases = _compute_phase_stats(phases)

        total_tasks = sum(p['total'] for p in phases)
        total_done  = sum(p['done']  for p in phases)
        overall_pct = round(total_done / total_tasks * 100) if total_tasks else 0

        results.append({
            'project': pname,
            'path':    ppath,
            'phases':  phases,
            'total_tasks': total_tasks,
            'total_done':  total_done,
            'overall_pct': overall_pct,
        })

    return results
