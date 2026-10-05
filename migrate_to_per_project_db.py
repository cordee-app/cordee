#!/usr/bin/env python3
"""
Phase 2 migration: copy per-project data from aingel.db → <project_path>/project.db

Run ONCE after stopping the service:
    sudo systemctl stop aingel
    python3 migrate_to_per_project_db.py
    sudo systemctl start aingel

Does NOT rename superagent.db → aingel.db (that is done by agent_config.py pointing
CENTRAL_DB_PATH at aingel.db; rename the file manually before running this script if
the file still exists as superagent.db).
"""
import os
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

# Ensure the local package is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_config import DB_PATH  # aingel.db
import agent_db as db


def _central_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def migrate():
    print(f'Central DB: {DB_PATH}')
    if not os.path.exists(DB_PATH):
        # Try old name
        old = os.path.join(os.path.dirname(DB_PATH), 'superagent.db')
        if os.path.exists(old):
            print(f'Renaming {old} → {DB_PATH}')
            shutil.move(old, DB_PATH)
        else:
            print('ERROR: neither aingel.db nor superagent.db found.')
            sys.exit(1)

    # Ensure registry tables exist
    db.init_db()

    # Backup
    bak = DB_PATH + f'.pre-phase2-{datetime.now().strftime("%Y%m%d-%H%M%S")}.bak'
    shutil.copy2(DB_PATH, bak)
    print(f'Backup: {bak}')

    central = _central_conn()
    projects = central.execute('SELECT * FROM projects').fetchall()
    print(f'Projects to migrate: {len(projects)}')

    total_tasks = total_execs = total_chats = total_skills = total_perms = 0
    errors = []

    for proj in projects:
        pid = proj['id']
        pname = proj['name']
        ppath = proj['path']

        if not ppath or not os.path.isdir(ppath):
            print(f'  [{pid}] {pname} — SKIP (path missing or not a dir: {ppath!r})')
            continue

        print(f'  [{pid}] {pname} → {ppath}')

        try:
            pconn = db.get_project_db(ppath)  # auto-inits schema
            pconn.execute('PRAGMA foreign_keys = OFF')  # allow migrating orphan rows

            # ── Tasks ────────────────────────────────────────────────────────
            tasks = central.execute(
                'SELECT * FROM tasks WHERE project_id=?', (pid,)
            ).fetchall()
            target_task_cols = {r[1] for r in pconn.execute('PRAGMA table_info(tasks)').fetchall()}
            for t in tasks:
                td = dict(t)
                existing = pconn.execute('SELECT id FROM tasks WHERE id=?', (td['id'],)).fetchone()
                if existing:
                    continue
                # Only insert columns that exist in the target schema
                cols = [c for c in td.keys() if c in target_task_cols]
                placeholders = ','.join('?' * len(cols))
                pconn.execute(
                    f'INSERT OR IGNORE INTO tasks ({",".join(cols)}) VALUES ({placeholders})',
                    [td[c] for c in cols]
                )
                # Register in aingel.db
                existing_reg = central.execute(
                    'SELECT id FROM task_registry WHERE id=?', (td['id'],)
                ).fetchone()
                if not existing_reg:
                    central.execute(
                        'INSERT INTO task_registry (id, project_id, project_path) VALUES (?,?,?)',
                        (td['id'], pid, ppath)
                    )
            pconn.commit()
            total_tasks += len(tasks)
            print(f'    tasks:       {len(tasks)}')

            # ── Executions ───────────────────────────────────────────────────
            task_ids = [t['id'] for t in tasks]
            if task_ids:
                placeholders = ','.join('?' * len(task_ids))
                execs = central.execute(
                    f'SELECT * FROM executions WHERE task_id IN ({placeholders})',
                    task_ids
                ).fetchall()
            else:
                execs = []
            # Also grab chat executions for chats belonging to this project
            chats_central = central.execute(
                'SELECT id FROM chats WHERE project_id=?', (pid,)
            ).fetchall()
            chat_ids_central = [c['id'] for c in chats_central]
            if chat_ids_central:
                placeholders = ','.join('?' * len(chat_ids_central))
                chat_execs = central.execute(
                    f'SELECT * FROM executions WHERE chat_id IN ({placeholders})',
                    chat_ids_central
                ).fetchall()
                # merge, dedup by id
                seen_eids = {e['id'] for e in execs}
                for ce in chat_execs:
                    if ce['id'] not in seen_eids:
                        execs.append(ce)
                        seen_eids.add(ce['id'])

            target_exec_cols = {r[1] for r in pconn.execute('PRAGMA table_info(executions)').fetchall()}
            for e in execs:
                ed = dict(e)
                existing = pconn.execute('SELECT id FROM executions WHERE id=?', (ed['id'],)).fetchone()
                if existing:
                    continue
                exec_proj_id = pid
                exec_proj_path = ppath
                cols = [c for c in ed.keys() if c in target_exec_cols]
                placeholders_e = ','.join('?' * len(cols))
                pconn.execute(
                    f'INSERT OR IGNORE INTO executions ({",".join(cols)}) VALUES ({placeholders_e})',
                    [ed[c] for c in cols]
                )
                existing_reg = central.execute(
                    'SELECT id FROM exec_registry WHERE id=?', (ed['id'],)
                ).fetchone()
                if not existing_reg:
                    central.execute(
                        'INSERT INTO exec_registry (id, project_id, project_path) VALUES (?,?,?)',
                        (ed['id'], exec_proj_id, exec_proj_path)
                    )
            pconn.commit()
            total_execs += len(execs)
            print(f'    executions:  {len(execs)}')

            # ── Chats ────────────────────────────────────────────────────────
            chats = central.execute(
                'SELECT * FROM chats WHERE project_id=?', (pid,)
            ).fetchall()
            target_chat_cols = {r[1] for r in pconn.execute('PRAGMA table_info(chats)').fetchall()}
            for c in chats:
                cd = dict(c)
                existing = pconn.execute('SELECT id FROM chats WHERE id=?', (cd['id'],)).fetchone()
                if existing:
                    continue
                cols = [col for col in cd.keys() if col in target_chat_cols]
                placeholders_c = ','.join('?' * len(cols))
                pconn.execute(
                    f'INSERT OR IGNORE INTO chats ({",".join(cols)}) VALUES ({placeholders_c})',
                    [cd[col] for col in cols]
                )
                existing_reg = central.execute(
                    'SELECT id FROM chat_registry WHERE id=?', (cd['id'],)
                ).fetchone()
                if not existing_reg:
                    central.execute(
                        'INSERT INTO chat_registry (id, project_id, project_path) VALUES (?,?,?)',
                        (cd['id'], pid, ppath)
                    )
            pconn.commit()
            total_chats += len(chats)
            print(f'    chats:       {len(chats)}')

            # ── Project skills ────────────────────────────────────────────────
            skills = central.execute(
                'SELECT * FROM project_skills WHERE project_id=?', (pid,)
            ).fetchall()
            for s in skills:
                sd = dict(s)
                pconn.execute(
                    'INSERT OR IGNORE INTO project_skills (project_id, category, name, auto_detected, detected_at) '
                    'VALUES (?,?,?,?,?)',
                    (sd['project_id'], sd['category'], sd['name'],
                     sd.get('auto_detected', 1), sd.get('detected_at'))
                )
            pconn.commit()
            total_skills += len(skills)
            print(f'    skills:      {len(skills)}')

            # ── Project permissions ───────────────────────────────────────────
            perms = central.execute(
                'SELECT * FROM project_permissions WHERE project_id=?', (pid,)
            ).fetchall()
            for p in perms:
                pd = dict(p)
                pconn.execute(
                    'INSERT OR IGNORE INTO project_permissions (project_id, group_key, rule, enabled, auto_added, added_at) '
                    'VALUES (?,?,?,?,?,?)',
                    (pd['project_id'], pd['group_key'], pd['rule'],
                     pd.get('enabled', 1), pd.get('auto_added', 0), pd.get('added_at'))
                )
            pconn.commit()
            total_perms += len(perms)
            print(f'    permissions: {len(perms)}')

            pconn.close()

        except Exception as exc:
            errors.append((pname, str(exc)))
            print(f'    ERROR: {exc}')
            import traceback; traceback.print_exc()
            continue

    central.commit()
    central.close()

    print()
    print('─' * 60)
    print(f'Migration complete.')
    print(f'  Tasks:       {total_tasks}')
    print(f'  Executions:  {total_execs}')
    print(f'  Chats:       {total_chats}')
    print(f'  Skills:      {total_skills}')
    print(f'  Permissions: {total_perms}')
    if errors:
        print(f'  ERRORS ({len(errors)}):')
        for name, msg in errors:
            print(f'    {name}: {msg}')
    else:
        print('  No errors.')
    print()
    print('Next steps:')
    print('  1. sudo systemctl start aingel')
    print('  2. Open the UI and verify tasks / executions / chats load correctly')
    print('  3. DONE 2026-07-17: legacy tables dropped from aingel.db after split verified stable')


def verify():
    """Quick verification pass — count rows per project in both DBs."""
    central = _central_conn()
    projects = central.execute('SELECT id, name, path FROM projects').fetchall()
    print(f'{"ID":>4}  {"Project":<40}  {"Cent.Tasks":>10}  {"Proj.Tasks":>10}  {"OK?":>5}')
    for proj in projects:
        pid, pname, ppath = proj['id'], proj['name'], proj['path']
        cent_count = central.execute(
            'SELECT COUNT(*) FROM tasks WHERE project_id=?', (pid,)
        ).fetchone()[0]
        proj_db = os.path.join(ppath or '', 'project.db') if ppath else ''
        if ppath and os.path.exists(proj_db):
            pconn = sqlite3.connect(proj_db)
            proj_count = pconn.execute(
                'SELECT COUNT(*) FROM tasks WHERE project_id=?', (pid,)
            ).fetchone()[0]
            pconn.close()
            ok = '✅' if proj_count == cent_count else '❌'
        else:
            proj_count = -1
            ok = '—'
        print(f'{pid:>4}  {pname:<40}  {cent_count:>10}  {proj_count:>10}  {ok:>5}')
    central.close()


if __name__ == '__main__':
    if '--verify' in sys.argv:
        verify()
    else:
        migrate()
        print()
        verify()
