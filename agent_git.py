"""
agent_git.py — Git-validated execution for SuperAgent.

When a project is git-enabled, each task runs on its own branch so the AI's
file edits become a reviewable, revertible unit:

    run_task   →  start_task_branch()   create/checkout  task/<id>-<slug>
                                          off a clean default branch
    (AI edits files in project_path)
    run_task   →  commit_task()         git add -A && commit on the branch;
                                          returns commit SHA + diffstat
    Approve    →  merge_task_branch()   --no-ff merge into the default branch
    Reject     →  discard_task_branch() delete the branch (tree restored)

Design rules:
  • Operates on an absolute project_path; shells out to the `git` binary.
  • Defensive: every function returns a structured dict and never raises into
    the executor/API. A git failure degrades to {'ok': False, ...} so task
    execution is never broken by a git problem.
  • The default branch is resolved per-repo (main/master), not assumed.
  • Concurrency caveat: a git working tree is shared, so two tasks in the SAME
    project must not run simultaneously. Different projects are independent.
"""
import os
import re
import subprocess

DEFAULT_BRANCH = 'main'
GIT_TIMEOUT = 120  # seconds

# Files SuperAgent manages itself — kept out of the project's *code* history so
# the diff a user reviews is the AI's actual code change, not bookkeeping.
# Artifacts/ bookkeeping (exec logs, chat logs, memories) is appended to even
# *after* the task commit, so it stays ignored. Task deliverables are the
# exception: every task writes them to Artifacts/outputs/<task-slug>/ and they
# must be versioned like any other task change (review diff, reject discards,
# Files > Deliverables attribution, tombstone commit gate).
ARTIFACTS_IGNORE_RULES = (
    'Artifacts/*',
    '!Artifacts/outputs/',
    'Artifacts/outputs/**/exec-*-output.md',
)
GITIGNORE_TEMPLATE = """\
# ── Managed by SuperAgent (execution bookkeeping, not project code) ──
# Task deliverables in Artifacts/outputs/<task>/ are versioned; exec logs are not.
""" + '\n'.join(ARTIFACTS_IGNORE_RULES) + """
*.memory.md
# Live task DB — the source of truth, must never be versioned. Tracking it
# makes branch checkouts revert task state (task/<id> checkout during a run).
# The glob also covers sidecars (project.db-wal) and manual backups
# (project.db.bak-*), which carry the same rows and the same hazard.
project.db*

# ── Secrets ──
.env
.env.*
!.env.example

# ── Python ──
__pycache__/
*.py[cod]
.venv/
venv/

# ── Node ──
node_modules/

# ── OS / editor ──
.DS_Store
*.swp

# ── Soft-delete trash (WebDAV/agent_files.delete) ──
# Files "deleted" via Y: are moved here, not hard-deleted. Tracking them made
# every delete a git rename, so the AI's bash tool could resurrect them and
# they leaked into exec diffstats. Never versioned.
.trash/
"""


# Runtime state git must never own. Tracking any of these makes a branch
# checkout revert live state — see _untrack_runtime_files and the `checkout -f`
# in discard_task_branch.
_RUNTIME_IGNORE_RULES = ('project.db*', 'Artifacts/eu-audit.log', '.trash/')
_RUNTIME_PATHSPECS    = ('project.db', 'project.db.*', 'project.db-*',
                         'Artifacts/eu-audit.log', '.trash')


# ── low-level ───────────────────────────────────────────────────────────────

def _run(project_path, *args, timeout=GIT_TIMEOUT):
    """Run `git <args>` in project_path. Returns (ok, stdout, stderr)."""
    try:
        p = subprocess.run(
            ['git', *args],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (p.returncode == 0, (p.stdout or '').strip(), (p.stderr or '').strip())
    except Exception as e:  # FileNotFoundError (no git), TimeoutExpired, …
        return (False, '', str(e))


def slugify(text, maxlen=40):
    s = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')
    s = s[:maxlen].rstrip('-')
    return s or 'task'


def branch_name(task_id, title):
    return f'task/{task_id}-{slugify(title)}'


# ── software detection / enable resolution ─────────────────────────────────────

CODE_EXTS = {
    '.py', '.js', '.ts', '.jsx', '.tsx', '.go', '.rs', '.java', '.rb', '.php',
    '.c', '.cpp', '.cc', '.h', '.hpp', '.cs', '.swift', '.kt', '.scala', '.sh',
    '.html', '.css', '.scss', '.vue', '.svelte', '.sql', '.dart', '.lua',
}
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', 'Artifacts', '.venv', 'venv',
             'Working Documents', 'Working Docs'}


def looks_like_software(project_path, max_scan=3000):
    """Heuristic: does the project tree contain source code? Drives the 'auto' default."""
    if not project_path or not os.path.isdir(project_path):
        return False
    seen = 0
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            seen += 1
            if seen > max_scan:
                return False
            if os.path.splitext(fn)[1].lower() in CODE_EXTS:
                return True
    return False


def resolve_enabled(git_enabled_col, project_path):
    """
    Map the projects.git_enabled column to a bool.
      None → auto: enabled iff the project looks like software
      0    → explicitly off
      1    → explicitly on
    """
    if git_enabled_col in (0, 1):
        return bool(git_enabled_col)
    return looks_like_software(project_path)


# ── inspection ────────────────────────────────────────────────────────────────

def is_repo(project_path):
    if not project_path or not os.path.isdir(project_path):
        return False
    ok, out, _ = _run(project_path, 'rev-parse', '--is-inside-work-tree')
    return ok and out == 'true'


def default_branch(project_path):
    """Resolve the repo's primary branch (main/master), falling back to DEFAULT_BRANCH."""
    for name in (DEFAULT_BRANCH, 'master'):
        ok, _, _ = _run(project_path, 'rev-parse', '--verify', name)
        if ok:
            return name
    # No commits yet, or unusual setup — use the current symbolic ref or default.
    ok, out, _ = _run(project_path, 'symbolic-ref', '--short', 'HEAD')
    return out if (ok and out) else DEFAULT_BRANCH


def current_branch(project_path):
    ok, out, _ = _run(project_path, 'rev-parse', '--abbrev-ref', 'HEAD')
    return out if ok else ''


def is_clean(project_path):
    ok, out, _ = _run(project_path, 'status', '--porcelain')
    return ok and out == ''


# ── setup ─────────────────────────────────────────────────────────────────────

def ensure_repo(project_path, project_name=''):
    """git init + .gitignore + an initial commit if not already a repo. Idempotent."""
    if not project_path or not os.path.isdir(project_path):
        return {'ok': False, 'error': 'project path does not exist'}
    if is_repo(project_path):
        # Make sure a .gitignore exists even on pre-existing repos.
        _ensure_gitignore(project_path)
        return {'ok': True, 'created': False}

    ok, _, err = _run(project_path, 'init', '-b', DEFAULT_BRANCH)
    if not ok:
        # Older git without -b support: init then rename.
        ok2, _, err2 = _run(project_path, 'init')
        if not ok2:
            return {'ok': False, 'error': f'git init failed: {err or err2}'}
        _run(project_path, 'checkout', '-b', DEFAULT_BRANCH)

    _ensure_gitignore(project_path)
    _run(project_path, 'add', '-A')
    _run(project_path, 'commit', '-m',
         f'Initial commit ({project_name or "project"}) — SuperAgent git-enabled')
    return {'ok': True, 'created': True}


def _ensure_gitignore(project_path):
    gi = os.path.join(project_path, '.gitignore')
    if not os.path.exists(gi):
        try:
            with open(gi, 'w') as f:
                f.write(GITIGNORE_TEMPLATE)
        except OSError:
            pass
    else:
        # Existing repo: make sure every runtime path is ignored. Older repos were
        # created before these rules were in the template; append whatever is
        # missing so branch checkouts stop reverting task state.
        try:
            with open(gi, 'r', encoding='utf-8') as f:
                lines = f.read().splitlines()
            # Older template ignored Artifacts/ wholesale, which hid task
            # deliverables from git. Swap that one line for the narrower rules.
            if 'Artifacts/' in (line.strip() for line in lines):
                new_lines = []
                for line in lines:
                    if line.strip() == 'Artifacts/':
                        new_lines.extend(ARTIFACTS_IGNORE_RULES)
                    else:
                        new_lines.append(line)
                with open(gi, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(new_lines) + '\n')
                lines = new_lines
            present = {line.strip() for line in lines}
            missing = [rule for rule in _RUNTIME_IGNORE_RULES if rule not in present]
            if missing:
                with open(gi, 'a', encoding='utf-8') as f:
                    f.write('\n# Runtime state — source of truth, never versioned.\n'
                            + '\n'.join(missing) + '\n')
        except OSError:
            pass


def _untrack_runtime_files(project_path):
    """Stop versioning runtime state in repos that already track it.

    A .gitignore rule does nothing to an already-tracked file, so repos created
    before those rules existed keep the live DB under version control. Every
    `checkout -f` then reverts it, destroying whatever task rows, status changes
    and chat messages were written since the last checkpoint commit — silently,
    because the file still exists and only its contents rolled back.

    The removal is committed immediately. An uncommitted `git rm --cached` would
    be undone by the very checkout it exists to defuse, and the base branch would
    still carry the DB in its tree.

    Call only while on the base branch. Idempotent: a no-op once nothing runtime
    is tracked, which is the steady state for every repo built from the template.
    """
    ok, out, _ = _run(project_path, 'ls-files', '--', *_RUNTIME_PATHSPECS)
    tracked = [line for line in (out or '').splitlines() if line.strip()]
    if not ok or not tracked:
        return {'ok': True, 'untracked': []}

    ok, _, err = _run(project_path, 'rm', '--cached', '-q', '--ignore-unmatch',
                      '--', *tracked)
    if not ok:
        return {'ok': False, 'untracked': [], 'error': err}

    # The ignore rules must land in the same commit as the removal: untracked but
    # un-ignored, the next `add -A` would put the DB straight back under version
    # control and restore the very hazard this removes.
    _run(project_path, 'add', '--', '.gitignore')

    # Commits the staged removals only; unstaged working-tree edits are left for
    # the caller's checkpoint commit.
    ok, _, err = _run(project_path, 'commit', '-m',
                      'chore: stop versioning runtime state\n\n'
                      'Tracking the live DB made every branch checkout revert '
                      'task state written during a run.')
    if not ok:
        return {'ok': False, 'untracked': [], 'error': err}
    return {'ok': True, 'untracked': tracked}


# ── task lifecycle ──────────────────────────────────────────────────────────

def start_task_branch(project_path, task_id, title, project_name=''):
    """
    Prepare a clean per-task branch before the AI runs.

    Steps: ensure repo → checkout default branch → checkpoint any pre-existing
    dirty state onto it (so nothing is lost and the baseline is clean) →
    create/checkout task/<id>-<slug>.

    Returns {'ok', 'active', 'branch', 'base', 'error'}.
    'active' is True only when the task branch is checked out and ready.
    """
    setup = ensure_repo(project_path, project_name)
    if not setup.get('ok'):
        return {'ok': False, 'active': False, 'error': setup.get('error')}

    base = default_branch(project_path)
    # Land on the base branch (force: a prior crashed run may have left us elsewhere).
    _run(project_path, 'checkout', base)

    # Before any branch work: make sure git does not own the live DB. Must happen
    # on the base branch and before the checkpoint below, so the removal lands in
    # base's tree and the discard path can no longer revert task state.
    _untrack_runtime_files(project_path)

    # Checkpoint pre-existing manual/uncommitted edits so the task baseline is clean.
    if not is_clean(project_path):
        _run(project_path, 'add', '-A')
        # Tombstone gate: strip resurrected (deleted-then-re-added) files that
        # arrived between runs (bisync/bucket sync) before they land on base.
        gate_tombstones(project_path, scope='staged')
        _run(project_path, 'commit', '-m', f'Checkpoint before task #{task_id}')

    branch = branch_name(task_id, title)
    # Recreate the branch fresh if a stale one with this name exists.
    _run(project_path, 'branch', '-D', branch)
    ok, _, err = _run(project_path, 'checkout', '-b', branch)
    if not ok:
        # Best effort: get back to base so we don't strand the tree on a half-branch.
        _run(project_path, 'checkout', base)
        return {'ok': False, 'active': False, 'branch': branch, 'base': base, 'error': err}

    return {'ok': True, 'active': True, 'branch': branch, 'base': base}


def commit_task(project_path, task_id, title):
    """
    Commit the AI's edits on the current task branch.

    Returns {'ok', 'changed', 'commit', 'diffstat', 'branch', 'error'}.
    'changed' is False when the AI produced no tracked file changes (e.g. a
    text-only/research task) — that is the natural software-vs-generic boundary,
    and no commit is made.

    CLI-pre-committed edits: some agentic CLIs (Mistral Vibe) commit files
    themselves during execution with their own message. When that happens the
    working tree is clean by the time we get here, but the task branch is
    ahead of the default branch by the CLI's commit(s). We detect that case
    and treat the CLI's commits as the AI's edits — returning `changed: True`
    with the real diffstat and HEAD sha so the caller merges the branch to
    main and the Exec Log can show the files. Regression: task #10000698
    (a client project) where Vibe committed `whoswho.md` itself, leaving
    `git_commit`/`git_diffstat` empty and the file stranded on an unmerged
    task branch, invisible to the Exec Log and Memory.
    """
    branch = current_branch(project_path)
    base = default_branch(project_path)
    if is_clean(project_path):
        # Tree is clean — but did the agentic CLI already commit on this branch?
        # If we're on a task branch that's ahead of base, treat those commits as
        # the AI's work. This is the Vibe-CLI-pre-committed case.
        if branch and branch != base and branch.startswith('task/'):
            _, ahead, _ = _run(project_path, 'rev-list', '--count', f'{base}..HEAD')
            ahead = (ahead or '').strip()
            if ahead and ahead != '0':
                # Tombstone gate over the already-committed range (bucket-sync /
                # CLI restores may have landed here). Re-trash matches.
                gate = gate_tombstones(project_path, base=base, scope='range')
                if gate.get('trashed'):
                    _run(project_path, 'commit', '-m',
                         f'Task #{task_id}: remove tombstoned (resurrected) files')
                _, sha, _ = _run(project_path, 'rev-parse', '--short', 'HEAD')
                _, diffstat, _ = _run(project_path, 'diff', '--stat', f'{base}...HEAD')
                return {'ok': True, 'changed': True, 'commit': sha,
                        'diffstat': diffstat, 'branch': branch,
                        'precommitted': True, 'tombstone_note': gate.get('note', '')}
        return {'ok': True, 'changed': False, 'commit': '', 'diffstat': '', 'branch': branch}

    ok, _, err = _run(project_path, 'add', '-A')
    if not ok:
        return {'ok': False, 'changed': False, 'branch': branch, 'error': f'git add failed: {err}'}

    # Tombstone gate: strip resurrected (deleted-then-re-added) files before commit.
    gate = gate_tombstones(project_path, base=base, scope='staged')

    ok, _, err = _run(project_path, 'commit', '-m', f'Task #{task_id}: {title}')
    if not ok:
        return {'ok': False, 'changed': False, 'branch': branch, 'error': f'git commit failed: {err}'}

    _, sha, _ = _run(project_path, 'rev-parse', '--short', 'HEAD')
    _, diffstat, _ = _run(project_path, 'diff', '--stat', f'{base}...HEAD')
    result = {'ok': True, 'changed': True, 'commit': sha, 'diffstat': diffstat, 'branch': branch}
    if gate.get('trashed'):
        result['tombstone_note'] = gate.get('note', '')
    return result


def commit_chat(project_path, chat_id, chat_name=''):
    """
    Commit any file changes left by a chat execution directly on the current branch.

    Unlike commit_task there is no branch lifecycle — chats run on whatever
    branch the repo is on (usually main) and we just capture their edits so
    they are not left as uncommitted noise that confuses subsequent task
    branch operations (stash/checkout).

    Returns {'ok', 'changed', 'commit', 'diffstat'}.
    """
    if not is_repo(project_path):
        return {'ok': False, 'changed': False, 'error': 'not a git repo'}

    if is_clean(project_path):
        return {'ok': True, 'changed': False, 'commit': '', 'diffstat': ''}

    ok, _, err = _run(project_path, 'add', '-A')
    if not ok:
        return {'ok': False, 'changed': False, 'error': f'git add failed: {err}'}

    # Tombstone gate: strip resurrected (deleted-then-re-added) files from chats too.
    gate = gate_tombstones(project_path, scope='staged')

    label = f': {chat_name[:60]}' if chat_name else ''
    ok, _, err = _run(project_path, 'commit', '-m', f'Chat #{chat_id}{label}')
    if not ok:
        return {'ok': False, 'changed': False, 'error': f'git commit failed: {err}'}

    _, sha, _ = _run(project_path, 'rev-parse', '--short', 'HEAD')
    _, diffstat, _ = _run(project_path, 'diff', '--stat', 'HEAD~1..HEAD')
    result = {'ok': True, 'changed': True, 'commit': sha.strip(), 'diffstat': diffstat}
    if gate.get('trashed'):
        result['tombstone_note'] = gate.get('note', '')
    return result


def get_diffstat(project_path, base, branch):
    """Return the --stat summary between two refs (for display in the UI)."""
    if not is_repo(project_path):
        return ''
    ok, out, _ = _run(project_path, 'diff', '--stat', f'{base}...{branch}')
    return out if ok else ''


def get_diff(project_path, base, branch, context=3):
    """Return the unified diff between two refs.
    Caps output at 5000 lines so the browser doesn't choke."""
    if not is_repo(project_path):
        return {'error': 'not a git repo'}
    ok, out, err = _run(project_path, 'diff', f'--unified={context}', f'{base}...{branch}')
    if not ok:
        return {'error': err or 'git diff failed'}
    lines = out.split('\n')
    total = len(lines)
    if total > 5000:
        out = '\n'.join(lines[:5000]) + f'\n\n... [diff truncated — {total} total lines]'
    return {'diff': out, 'total_lines': total, 'truncated': total > 5000}


def commit_exists(project_path, ref):
    """Return True if ref resolves to a git object (branch, tag, or SHA)."""
    if not is_repo(project_path):
        return False
    ok, _, _ = _run(project_path, 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}')
    return ok


def get_commit_diff(project_path, commit, context=3):
    """Return the diff a single commit introduced (its first-parent change).

    Works for task commits (`git show`) and merge commits (compares the merge
    result against its first parent), which is exactly what the Exec Log needs
    once a task branch has been merged and deleted.
    """
    if not is_repo(project_path):
        return {'error': 'not a git repo'}
    if not commit_exists(project_path, commit):
        return {'error': f'unknown revision: {commit}'}
    # If it's a merge commit, diff against the first parent; otherwise git show
    # of a normal commit already yields its own patch.
    ok, parents, _ = _run(project_path, 'rev-list', '--parents', '-n', '1', commit)
    if not ok:
        return {'error': 'failed to inspect commit'}
    parents = parents.split()
    if len(parents) >= 3:
        args = ['diff', f'--unified={context}', parents[1], commit]
    else:
        args = ['show', f'--unified={context}', '--format=', commit]
    ok, out, err = _run(project_path, *args)
    if not ok:
        return {'error': err or 'git diff failed'}
    lines = out.split('\n')
    total = len(lines)
    if total > 5000:
        out = '\n'.join(lines[:5000]) + f'\n\n... [diff truncated — {total} total lines]'
    return {'diff': out, 'total_lines': total, 'truncated': total > 5000}


def merge_task_branch(project_path, branch, task_id=None):
    """Approve: merge the task branch into the default branch, then delete it."""
    if not branch or not is_repo(project_path):
        return {'ok': False, 'error': 'no branch or not a repo'}
    base = default_branch(project_path)

    # Stash any uncommitted changes so checkout doesn't fail on dirty files.
    stash_ok, stash_out, stash_err = _run(
        project_path, 'stash', 'push', '--include-untracked',
        '-m', 'superagent-merge-stash'
    )
    stashed = stash_ok and 'No local changes' not in (stash_out + stash_err)

    def restore_stash():
        if not stashed:
            return {'ok': True}
        ok, out, err = _run(project_path, 'stash', 'pop')
        if ok:
            return {'ok': True}
        return {'ok': False, 'error': err or out or 'stash pop failed'}

    ok, _, err = _run(project_path, 'checkout', base)
    if not ok:
        restored = restore_stash()
        restore_note = '' if restored.get('ok') else f"; restoring local changes failed: {restored.get('error')}"
        return {'ok': False, 'error': f'checkout {base} failed: {err}{restore_note}'}

    label = f'task #{task_id}' if task_id is not None else branch
    ok, out, err = _run(project_path, 'merge', '--no-ff', branch,
                        '-m', f'Merge {label} (approved)')
    if not ok:
        _run(project_path, 'merge', '--abort')  # leave base pristine on conflict
        restored = restore_stash()
        restore_note = '' if restored.get('ok') else f"; restoring local changes failed: {restored.get('error')}"
        return {'ok': False, 'merged': False, 'base': base,
                'error': f'merge conflict or failure: {err or out}{restore_note}'}

    _, sha, _ = _run(project_path, 'rev-parse', '--short', 'HEAD')
    restored = restore_stash()
    if not restored.get('ok'):
        return {'ok': True, 'merged': True, 'base': base, 'commit': sha,
                'warning': f'merged, but restoring local changes failed: {restored.get("error")}. Run `git stash pop` manually.'}
    _run(project_path, 'branch', '-d', branch)
    return {'ok': True, 'merged': True, 'base': base, 'commit': sha}


def revert_merge(project_path, commit):
    """
    Undo a previously-approved change by creating an inverse commit on the
    default branch (history-preserving — never rewrites published history).

    `commit` may be the merge commit (preferred) or the task commit. If a later
    task built on these changes, git cannot cleanly invert them — we abort and
    report so, rather than leaving a half-reverted tree.

    Returns {'ok', 'reverted', 'commit', 'base', 'error'}.
    """
    if not commit or not is_repo(project_path):
        return {'ok': False, 'error': 'no commit or not a repo'}
    base = default_branch(project_path)
    ok, _, err = _run(project_path, 'checkout', base)
    if not ok:
        return {'ok': False, 'error': f'checkout {base} failed: {err}'}

    # Is it a merge commit? (more than one parent → need -m to pick the mainline)
    _, parents, _ = _run(project_path, 'rev-list', '--parents', '-n', '1', commit)
    is_merge = len(parents.split()) > 2  # "<sha> <p1> <p2> …"
    args = ['revert', '--no-edit'] + (['-m', '1'] if is_merge else []) + [commit]
    ok, out, err = _run(project_path, *args)
    if not ok:
        _run(project_path, 'revert', '--abort')  # leave base pristine on conflict
        return {'ok': False, 'reverted': False, 'base': base,
                'error': f'cannot auto-revert (a later task likely depends on this change): {err or out}'}

    _, sha, _ = _run(project_path, 'rev-parse', '--short', 'HEAD')
    return {'ok': True, 'reverted': True, 'base': base, 'commit': sha}


def discard_task_branch(project_path, branch):
    """Reject: delete the task branch; the default branch is untouched (zero residue)."""
    if not branch or not is_repo(project_path):
        return {'ok': False, 'error': 'no branch or not a repo'}
    base = default_branch(project_path)
    # Force-leave the branch even if it has uncommitted changes, then hard-delete it.
    _run(project_path, 'checkout', '-f', base)
    ok, _, err = _run(project_path, 'branch', '-D', branch)
    if not ok:
        return {'ok': False, 'discarded': False, 'error': err}
    return {'ok': True, 'discarded': True, 'base': base}


def _hash_file(abs_path):
    """sha256 of a file (None on error)."""
    import hashlib
    try:
        h = hashlib.sha256()
        with open(abs_path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _active_tombstone_hashes(project_path):
    """Set of active delete-tombstone sha256 values for the project."""
    try:
        import agent_db
        tombstones = agent_db.get_delete_tombstones(project_path) or []
        return set(t['sha256'] for t in tombstones if t.get('sha256'))
    except Exception:
        return set()


def _re_trash(project_path, rel):
    """Move rel (a currently-tracked/on-disk file) into the out-of-tree trash
    and record a fresh tombstone. Uses agent_files.trash_resolved (B1) which does
    realpath containment + forbidden checks WITHOUT requiring the writable-root
    check, so a resurrection at the project root / Legal/ / Artifacts/ can also
    be stripped. Returns {'ok', 'trash_rel', 'error'}."""
    try:
        import agent_files
        trashed = agent_files.trash_resolved(project_path, rel)
        return {'ok': bool(trashed)}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def gate_tombstones(project_path, base=None, scope='staged'):
    """Strip files that match a delete-tombstone from an about-to-commit change.

    The "deleted files stay deleted" invariant: any file whose content sha256
    matches an active delete-tombstone is a resurrection and is re-trashed
    before it can land in git. Matches by content hash so renames (cp under a
    new name) and bucket-sync / git-checkout restores are all caught.

    scope='staged'  → hash the currently-staged (git add -A'd) added/modified
                      files in the working tree, re-trash matches, rm --cached.
    scope='range'   → hash files ADDED between base...HEAD (used after a CLI /
                      pre-committed run), re-trash matches and `git rm` them in
                      a follow-up commit.

    Returns {'stripped': [rel...], 'trashed': n, 'note': str}.
    Call BEFORE git commit so the resurrected files are never committed.
    """
    try:
        import agent_files
    except Exception as _e:
        return {'stripped': [], 'trashed': 0, 'note': ''}
    if not is_repo(project_path):
        return {'stripped': [], 'trashed': 0, 'note': ''}

    hashes = _active_tombstone_hashes(project_path)
    if not hashes:
        return {'stripped': [], 'trashed': 0, 'note': ''}

    def _parse_zero(out):
        return [x for x in out.split('\0') if x]

    rels_to_check = []
    if scope == 'range':
        # Files added between base...HEAD (precommitted / CLI-committed case).
        # B3: -z so core.quotePath doesn't C-quote Polish/unicode filenames;
        # base...HEAD (three dots) matches the diffstat used for display.
        base = base or default_branch(project_path)
        ok, out, _ = _run(project_path, 'diff', '--name-only', '-z',
                          '--diff-filter=AC', '--no-renames', f'{base}...HEAD')
        if not ok:
            return {'stripped': [], 'trashed': 0, 'note': ''}
        rels_to_check = _parse_zero(out)
    else:
        # Staged added/modified files in the working tree. B2: --no-renames so a
        # deletion + a copy under a new name in the same commit is NOT collapsed
        # into an R entry (which we would skip).
        ok, out, _ = _run(project_path, 'diff', '--cached', '--name-only', '-z',
                          '--diff-filter=AC', '--no-renames')
        if not ok:
            return {'stripped': [], 'trashed': 0, 'note': ''}
        rels_to_check = _parse_zero(out)

    stripped = []
    failed = []
    for rel in rels_to_check:
        if not rel:
            continue
        abs_path = os.path.join(project_path, rel)
        if not os.path.isfile(abs_path):
            continue
        # A3: skip tiny/empty files (never tombstoned, never a resurrection).
        try:
            if os.path.getsize(abs_path) < agent_files._TOMBSTONE_MIN_BYTES:
                continue
        except Exception:
            pass
        sha = _hash_file(abs_path)
        if not sha or sha not in hashes:
            continue
        # Match → it's a resurrected file. Unstage FIRST (so a re-trash failure
        # does not leave it committed), then re-trash; only count it stripped if
        # the re-trash actually succeeded (otherwise note it as a failure so it
        # is not silently left on disk, untracked).
        _run(project_path, 'rm', '-f', '--cached', '--', rel)
        res = _re_trash(project_path, rel)
        if res.get('ok'):
            stripped.append(rel)
        else:
            failed.append((rel, res.get('error', 're-trash failed')))

    if not stripped and not failed:
        return {'stripped': [], 'trashed': 0, 'note': ''}

    note = ''
    if stripped:
        note += (f"{len(stripped)} deleted file(s) were re-deleted by the tombstone "
                 f"gate (they were resurrected during this run and would otherwise "
                 f"have been committed): {', '.join(stripped[:20])}"
                 + (', … more' if len(stripped) > 20 else ''))
    if failed:
        note += (' ' if note else '') + '; '.join(
            f"could not re-trash {r}: {e}" for r, e in failed)
    try:
        import agent_db as _db
        import agent_events
        proj = _db.get_project_by_path(project_path)
        if proj:
            agent_events.emit(proj.get('id'), {'type': 'files_changed'})
    except Exception:
        pass
    return {'stripped': stripped, 'trashed': len(stripped), 'note': note,
            'failed': [{'rel': r, 'error': e} for r, e in failed]}
