#!/usr/bin/env python3
"""Seed a minimal demo database: one project, four tasks — one per board status.

Enough data to render every column of the Kanban board, so a fresh clone has
something to look at without shipping real task or chat content in git.

    python3 seed_demo.py              # writes to demo/ — live install untouched
    python3 seed_demo.py --clean      # rebuild the demo from scratch
    python3 seed_demo.py --db aingel.db --projects-root ~/Projects   # a real install

Defaults are deliberately self-contained. agent_config.DB_PATH points at the
repo's own aingel.db, so a script that just imported agent_db and started
writing would inject demo rows into a running system. Everything below is
redirected to --db/--projects-root before the first connection is opened.
"""

import argparse
import os
import shutil
import sys

REPO = os.path.dirname(os.path.abspath(__file__))

# One task per board column. Slots mirror the real invariant: a task may not be
# `confirmed` without a provider slot (see update_task in agent_api), and
# anything that has run belongs to the lane it ran in. Only `pending` sits
# unassigned.
DEMO_TASKS = [
    {
        'title': 'Add a health-check endpoint',
        'description': (
            'Expose GET /health returning {"status": "ok"} plus the app version.\n'
            'No auth. Used by the systemd watchdog.'
        ),
        'status': 'pending',
        'slot': None,
        'model': 'claude-sonnet-4-6',
        'priority': 5,
    },
    {
        'title': 'Cache the project list',
        'description': (
            'get_projects() re-reads every project.db on each call. Memoise for\n'
            '30s and invalidate on project create/delete.'
        ),
        'status': 'confirmed',
        'slot': 1,
        'model': 'claude-sonnet-4-6',
        'priority': 3,
    },
    {
        'title': 'Write tests for the event bus',
        'description': (
            'Cover subscribe/unsubscribe/emit, project isolation, and the\n'
            'full-queue drop path in agent_events.'
        ),
        'status': 'running',
        'slot': 2,
        'model': 'claude-haiku-4-5-20251001',
        'priority': 4,
    },
    {
        'title': 'Document the setup steps',
        'description': (
            'SETUP.md drifted from reality: the frontend build step was missing\n'
            'and the systemd unit path was stale.'
        ),
        'status': 'done',
        'slot': 1,
        'model': 'claude-sonnet-4-6',
        'priority': 6,
        'actual_cost': 0.0142,
    },
]

DEMO_README = """# Demo Project

Sample project created by `seed_demo.py`. Four tasks, one per board status,
so the Kanban board renders every column.

Safe to delete — nothing else references it.
"""


def parse_args():
    p = argparse.ArgumentParser(
        description='Seed a minimal demo database (1 project, 4 tasks).',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--db', default=os.path.join(REPO, 'demo', 'aingel.db'),
                   help='central DB path (default: demo/aingel.db)')
    p.add_argument('--projects-root', default=os.path.join(REPO, 'demo', 'projects'),
                   help='where the demo project folder is created '
                        '(default: demo/projects)')
    p.add_argument('--slug', default='demo-project', help='project slug')
    p.add_argument('--name', default='Demo Project', help='project display name')
    p.add_argument('--clean', action='store_true',
                   help='delete an existing demo at these paths first')
    p.add_argument('--force', action='store_true',
                   help='seed even if the target DB already has projects')
    return p.parse_args()


def main():
    args = parse_args()
    db_path = os.path.abspath(args.db)
    projects_root = os.path.abspath(args.projects_root)
    project_path = os.path.join(projects_root, args.slug)

    if args.clean:
        for target in (db_path, project_path):
            if os.path.isdir(target):
                shutil.rmtree(target)
            elif os.path.exists(target):
                os.remove(target)
        print(f'cleaned {db_path} and {project_path}')

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    os.makedirs(project_path, exist_ok=True)

    # Redirect the DB *before* importing anything that opens a connection.
    # agent_db binds DB_PATH at import time (`from agent_config import DB_PATH`),
    # so rebinding the module global is what actually takes effect.
    import agent_db as db
    db.DB_PATH = db_path

    db.init_db()

    existing = db.get_projects()
    if existing and not args.force:
        print(f'refusing to seed: {db_path} already has {len(existing)} project(s).',
              file=sys.stderr)
        print('This looks like a real install. Use --force to seed anyway, '
              '--clean to rebuild the demo, or point --db somewhere empty.',
              file=sys.stderr)
        return 1

    readme = os.path.join(project_path, 'READMEFIRST.md')
    if not os.path.exists(readme):
        with open(readme, 'w') as f:
            f.write(DEMO_README)

    project = db.upsert_project(
        name=args.name,
        slug=args.slug,
        path=project_path,
        project_type='software',
    )
    pid = project['id']
    print(f'project #{pid}  {project["name"]}  -> {project_path}')

    for spec in DEMO_TASKS:
        # create_task always lands in `pending`; the status/slot move is a
        # separate update, exactly as the UI does it.
        task_id = db.create_task(
            project_id=pid,
            title=spec['title'],
            description=spec['description'],
            model=spec['model'],
            priority=spec['priority'],
            project_path=project_path,
        )
        fields = {'status': spec['status']}
        if spec['slot'] is not None:
            fields['work_session_slot'] = spec['slot']
        if spec.get('actual_cost') is not None:
            fields['actual_cost'] = spec['actual_cost']
        db.update_task(task_id, project_path=project_path, **fields)

        slot = f'slot {spec["slot"]}' if spec['slot'] else 'unassigned'
        print(f'  task #{task_id:<4} {spec["status"]:<10} {slot:<12} {spec["title"]}')

    print(f'\ndone. {len(DEMO_TASKS)} tasks in {db_path}')
    print(f'point the app at it with:  AINGEL_PROJECTS_ROOT={projects_root}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
