import os
import re
from pathlib import Path
from agent_config import PROJECTS_ROOT, DEFAULT_MODEL, DEFINITION_FILE, _LEGACY_DEF_FILE, default_model_for
import agent_db as db

SKIP_DIRS = {'Dashboard', 'SuperAgent', '.git', '__pycache__'}


def slugify(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def import_phase_tasks():
    """Create tasks from GUIDE.md phase steps for all projects (title only, deduped by ext_id)."""
    from agent_phases import parse_guide
    import agent_guide_sync
    results = []
    if not os.path.isdir(PROJECTS_ROOT):
        return []
    for entry in sorted(os.scandir(PROJECTS_ROOT), key=lambda e: e.name):
        if not entry.is_dir() or entry.name.startswith('.') or entry.name in SKIP_DIRS:
            continue
        guide = Path(entry.path) / 'GUIDE.md'
        if not guide.exists():
            continue
        project = db.upsert_project(entry.name, slugify(entry.name), entry.path)
        phases = parse_guide(str(guide))
        imported = 0
        model = default_model_for(project)
        with agent_guide_sync.suppress():
            for ph in phases:
                for tk in ph['tasks']:
                    ext_id      = f"ph::{ph['title'][:30]}::{tk['text'][:40]}"
                    task_status = 'done' if tk['status'] == 'done' else 'pending'
                    result = db.upsert_task(project['id'], ext_id, tk['text'],
                                            model=model,
                                            status=task_status, phase_name=ph['title'])
                    if result:
                        imported += 1
        if imported > 0:
            agent_guide_sync.regenerate_guide(entry.path, project['id'])
            results.append({'project': entry.name, 'imported': imported, 'note': 'from phases'})
    return results


def import_all_projects():
    """Import tasks from GUIDE.md for all projects. DB is the source of truth."""
    return import_phase_tasks()


def scaffold_project_folder(path, name, pitch=''):
    """Create the standard SuperAgent project folder layout at `path`.
    Idempotent: existing files are left untouched."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    (p / 'Working Documents').mkdir(exist_ok=True)
    (p / 'Artifacts').mkdir(exist_ok=True)
    (p / 'Artifacts' / 'chats').mkdir(exist_ok=True)
    (p / 'Artifacts' / 'outputs').mkdir(exist_ok=True)
    # Keep empty dirs in git/file listings
    for keep in [p / 'Working Documents' / '.gitkeep',
                 p / 'Artifacts' / 'chats' / '.gitkeep',
                 p / 'Artifacts' / 'outputs' / '.gitkeep']:
        if not keep.exists():
            keep.write_text('')
    # Placeholder definition files — only created if absent.
    # New projects get READMEFIRST.md; legacy CLAUDE.md is kept if it already exists.
    def_file = p / DEFINITION_FILE
    legacy_file = p / _LEGACY_DEF_FILE
    if not def_file.exists() and not legacy_file.exists():
        def_file.write_text(
            f"# {DEFINITION_FILE} — {name}\n\n"
            f"## Pitch\n\n{pitch.strip() or '_TODO: describe the project._'}\n\n"
            f"## Architecture\n\n_TODO: fill in once the design stabilises._\n"
        )
    guide_md = p / 'GUIDE.md'
    if not guide_md.exists():
        guide_md.write_text(
            f"# {name} — Roadmap\n\n"
            "_This file will be filled in by the scaffolding chat._\n"
        )
    skills_md = p / 'Skills.md'
    if not skills_md.exists():
        skills_md.write_text(
            f"# Skills — {name}\n\n"
            "_Capabilities will be auto-detected on first scan._\n\n"
            "## Auto-Detected Capabilities\n"
        )
    return str(p)


def import_phase_tasks_for_project(project_id):
    """Run the phase-task import for a single project. Mirrors
    import_phase_tasks() but scoped. Returns count of imported tasks."""
    from agent_phases import parse_guide
    import agent_guide_sync
    project = db.get_project(project_id)
    if not project:
        return 0
    guide = Path(project['path']) / 'GUIDE.md'
    if not guide.exists():
        return 0
    phases = parse_guide(str(guide))
    imported = 0
    model = default_model_for(project)
    with agent_guide_sync.suppress():
        for ph in phases:
            for tk in ph['tasks']:
                ext_id      = f"ph::{ph['title'][:30]}::{tk['text'][:40]}"
                task_status = 'done' if tk['status'] == 'done' else 'pending'
                result = db.upsert_task(project_id, ext_id, tk['text'],
                                        model=model,
                                        status=task_status, phase_name=ph['title'])
                if result:
                    imported += 1
    # Normalise GUIDE.md to DB-derived form now that the import is committed.
    # Only rewrite if this import actually added rows; otherwise the caller's
    # freshly-written AI draft is preserved (the no-rows no-op handles new
    # projects with no DB yet).
    if imported > 0:
        agent_guide_sync.regenerate_guide(project['path'], project_id)
    return imported
