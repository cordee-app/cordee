#!/usr/bin/env python3
"""ExecStop guard for superagent-vault.service.

Fails all running executions across every project DB BEFORE the main
agent_api.py process is killed by systemd. This prevents orphaned
'running' rows when the service is restarted mid-task (e.g. the
15:05:43 restart that orphaned exec #20000781).

No grace period — the unit uses KillMode=control-group, so a stop kills
every agent process and no execution can survive it. The next startup
sweep (reset_orphaned_executions) is a backup, not the primary recovery,
and it also covers a crash, where ExecStop does not run.

History: this was wired as ``ExecStopPre=``, which is not a systemd
directive, so the guard never ran until it became ``ExecStop=`` (2026-09-24).

Always exits 0 so it never blocks shutdown. A guard failure just means
the startup sweep will catch the orphans on next boot instead.

Usage: invoked by systemd as ExecStop. Can also be run manually:
    python3 ops/stop_guard.py
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone

# Central DB beside the code (one level up from ops/), as in agent_config.
AINGEL_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'aingel.db')


def main():
    now = datetime.now(timezone.utc).isoformat()
    total_execs = 0
    total_tasks = 0
    try:
        conn = sqlite3.connect(AINGEL_DB)
        conn.row_factory = sqlite3.Row
        projects = conn.execute('SELECT path FROM projects WHERE path IS NOT NULL').fetchall()
        conn.close()
    except Exception as e:
        print(f'[stop_guard] could not read projects: {e}', file=sys.stderr)
        sys.exit(0)  # never block shutdown

    for proj in projects:
        proj_path = proj['path']
        db_path = f'{proj_path}/project.db'
        try:
            pconn = sqlite3.connect(db_path)
            pconn.row_factory = sqlite3.Row
            # Fail all running executions (no grace period)
            running = pconn.execute(
                "SELECT id, task_id FROM executions WHERE status='running'"
            ).fetchall()
            for row in running:
                pconn.execute(
                    "UPDATE executions SET status='failed', "
                    "error_message='Service stopped (ExecStop guard)', "
                    "finished_at=? WHERE id=?",
                    (now, row['id'])
                )
                if row['task_id']:
                    pconn.execute(
                        "UPDATE tasks SET status='pending' "
                        "WHERE id=? AND status='running'",
                        (row['task_id'],)
                    )
                    total_tasks += 1
            total_execs += len(running)
            pconn.commit()
            pconn.close()
        except Exception as e:
            print(f'[stop_guard] {proj_path}: {e}', file=sys.stderr)
            continue

    if total_execs:
        print(f'[stop_guard] failed {total_execs} running execution(s), '
              f'reset {total_tasks} task(s) to pending')
    sys.exit(0)


if __name__ == '__main__':
    main()