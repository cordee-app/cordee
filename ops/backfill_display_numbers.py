#!/usr/bin/env python3
"""Backfill human display codes ("RP-014") for pre-existing projects and tasks.

Display codes are assigned automatically at creation since the feature landed
(short_code in upsert_project, display_no/display_code in create_task /
upsert_task). Rows created before that have none. This script makes every
existing row match what creation would have produced, deterministically:

  * projects.short_code — generated from the project name (significant-word
    initials, unique per instance) when missing; an existing code is never
    touched.
  * tasks.display_no / display_code — per project, allocated in
    (created_at, id) order starting at 001; existing numbers are never
    reassigned, so the script is idempotent and safe to re-run. Rows that
    already have a number but no code (interrupted earlier run) get their
    code repaired.

Default is a dry run: it prints the planned assignments without writing data.
`--apply` writes. The guarded column migrations (central short_code column +
per-project display columns) are applied by agent_db.init_db() /
get_project_db() either way — schema is not data.

Run while the service is stopped or quiet: a task created concurrently could
race the MAX(display_no)+1 allocation.

Usage:
  venv/bin/python ops/backfill_display_numbers.py            # dry run
  venv/bin/python ops/backfill_display_numbers.py --apply
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import agent_db  # noqa: E402


def project_folder(proj):
    """The project's folder holding project.db: the central row's path first,
    then PROJECTS_ROOT/<name> for rows whose recorded path drifted (archive
    restore into a new location, moved checkout). None when there is no
    project.db to backfill."""
    if proj.get('path') and os.path.isfile(os.path.join(proj['path'], 'project.db')):
        return proj['path']
    cand = os.path.join(agent_db.PROJECTS_ROOT, proj.get('name') or '')
    if os.path.isfile(os.path.join(cand, 'project.db')):
        return cand
    return None


def backfill_project(proj, apply=False, provisional=None):
    """Assign the missing display fields for one project.

    Returns a summary dict: short_code (assigned code or the kept existing
    one), short_code_assigned (code this call would write / wrote, or None),
    numbered (tasks that get a fresh display_no), repaired (tasks that had a
    number but no code), next_no (the next free number afterwards).
    `provisional` is a shared set of codes this run plans to claim, so a dry
    run reports the same suffixes an --apply run would write.
    """
    if provisional is None:
        provisional = set()
    pid = proj['id']
    summary = {
        'name': proj.get('name') or f'project {pid}',
        'short_code': proj.get('short_code'),
        'short_code_assigned': None,
        'numbered': 0,
        'repaired': 0,
        'next_no': None,
        'note': None,
    }

    # 1. short_code (central DB) — only when missing.
    if not summary['short_code']:
        conn = agent_db.get_db()
        try:
            code = agent_db._generate_short_code(
                proj.get('name') or '', conn, extra_taken=provisional)
        finally:
            conn.close()
        summary['short_code'] = code
        summary['short_code_assigned'] = code
        provisional.add(code)
        if apply:
            conn = agent_db.get_db()
            try:
                conn.execute('UPDATE projects SET short_code=? WHERE id=?',
                             (code, pid))
                conn.commit()
            finally:
                conn.close()
    short = summary['short_code']

    # 2. task numbers (project.db) — never reassign an existing number.
    folder = project_folder(proj)
    if folder is None:
        summary['note'] = 'no project.db — tasks skipped'
        return summary
    # get_project_db runs the guarded ALTERs, so the display columns exist.
    pconn = agent_db.get_project_db(folder)
    try:
        next_no = (pconn.execute(
            'SELECT COALESCE(MAX(display_no),0) FROM tasks WHERE project_id=?',
            (pid,)).fetchone()[0] or 0) + 1
        for row in pconn.execute(
                'SELECT id FROM tasks WHERE project_id=? AND display_no IS NULL '
                'ORDER BY created_at, id', (pid,)).fetchall():
            if apply:
                pconn.execute(
                    'UPDATE tasks SET display_no=?, display_code=? WHERE id=?',
                    (next_no, f'{short}-{next_no:03d}', row['id']))
            summary['numbered'] += 1
            next_no += 1
        # Rows numbered by an interrupted earlier run but never coded.
        for row in pconn.execute(
                'SELECT id, display_no FROM tasks WHERE project_id=? '
                'AND display_no IS NOT NULL AND display_code IS NULL',
                (pid,)).fetchall():
            if apply:
                pconn.execute(
                    'UPDATE tasks SET display_code=? WHERE id=?',
                    (f'{short}-{row["display_no"]:03d}', row['id']))
            summary['repaired'] += 1
        if apply:
            pconn.commit()
        summary['next_no'] = next_no
    finally:
        pconn.close()
    return summary


def run(apply=False):
    """Backfill every project in the central DB. Returns per-project summaries."""
    agent_db.init_db()  # guarded migrations: projects.short_code + unique index
    conn = agent_db.get_db()
    try:
        projects = [dict(r) for r in conn.execute(
            'SELECT * FROM projects ORDER BY id').fetchall()]
    finally:
        conn.close()
    provisional = set()
    summaries = []
    for proj in projects:
        try:
            summaries.append(backfill_project(proj, apply=apply,
                                              provisional=provisional))
        except Exception as e:
            summaries.append({'name': proj.get('name') or f"project {proj['id']}",
                              'error': str(e)})
    return summaries


def _print_summaries(summaries, apply):
    verb = 'APPLIED' if apply else 'PLANNED (dry run — no data written)'
    print(f'== display-code backfill: {verb} ==')
    total_numbered = total_repaired = 0
    for s in summaries:
        total_numbered += s.get('numbered', 0) or 0
        total_repaired += s.get('repaired', 0) or 0
        if s.get('error'):
            print(f"{s['name']}: ERROR — {s['error']}")
            continue
        bits = [f"short_code={s.get('short_code') or '-'}"]
        if s.get('short_code_assigned'):
            bits[0] += ' (assign)'
        bits.append(f"tasks numbered={s.get('numbered', 0)}")
        if s.get('repaired'):
            bits.append(f"repaired={s.get('repaired')}")
        if s.get('next_no') is not None:
            bits.append(f"next={s['next_no']:03d}")
        if s.get('note'):
            bits.append(s['note'])
        print(f"{s['name']}: " + ', '.join(bits))
    print(f'== {len(summaries)} project(s), {total_numbered} task(s) numbered, '
          f'{total_repaired} repaired ==')


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Backfill project short_codes and task display numbers '
                    '(human codes like RP-014). Dry run by default.')
    ap.add_argument('--apply', action='store_true',
                    help='write the planned assignments (default: dry run)')
    args = ap.parse_args(argv)
    summaries = run(apply=args.apply)
    _print_summaries(summaries, apply=args.apply)


if __name__ == '__main__':
    main()
