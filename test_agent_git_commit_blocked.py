"""Tests for commit-blocked bookkeeping (pre-commit hook rejection).

When a project's pre-commit hook rejects `git commit`, nothing was persisted:
the Exec Log showed "Branch … · ready" and stayed empty, and the deliverable
was stranded. Now the failure carries the staged-files diffstat, the error is
stored on the execution row (``git_commit_error``), and ``start_task_branch``
surfaces checkpoint-commit failures instead of swallowing them.

Hermetic: throwaway git repos in temp dirs, no network, no project DB touched
by the git tests. ``_active_tombstone_hashes`` is stubbed so the tombstone
gate never touches the central DB (same pattern as test_agent_git_diffstat).
"""
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import agent_db
import agent_git


def _git_available():
    return shutil.which('git') is not None


def _init_git_repo(path):
    """Init a bare-bones repo with a local identity; returns the git runner."""
    try:
        _git(path, 'init', '-q', '-b', 'main')
    except subprocess.CalledProcessError:
        # Older git without `init -b`.
        _git(path, 'init', '-q')
        _git(path, 'checkout', '-q', '-b', 'main')
    _git(path, 'config', 'user.email', 'test@example.invalid')
    _git(path, 'config', 'user.name', 'AIngel Test')
    _git(path, 'config', 'commit.gpgsign', 'false')
    return path


def _git(path, *args):
    return subprocess.run(
        ['git', *args], cwd=path, check=True,
        capture_output=True, text=True,
    )


def _write(path, rel, content):
    full = os.path.join(path, rel)
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(full, 'w', encoding='utf-8') as f:
        f.write(content)
    return full


def _add_rejecting_hook(repo):
    """Install a pre-commit hook that always rejects."""
    hooks = os.path.join(repo, '.git', 'hooks')
    os.makedirs(hooks, exist_ok=True)
    hook = os.path.join(hooks, 'pre-commit')
    with open(hook, 'w', encoding='utf-8') as f:
        f.write('#!/bin/sh\necho blocked\nexit 1\n')
    os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@unittest.skipUnless(_git_available(), 'git binary not installed')
class CommitTaskBlockedTest(unittest.TestCase):
    """Case A — commit_task on a repo whose pre-commit hook rejects."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='aingel-blocked-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _init_git_repo(self.tmp)
        _write(self.tmp, 'base.txt', 'base\n')
        _git(self.tmp, 'add', '-A')
        _git(self.tmp, 'commit', '-q', '-m', 'base')

        # Install the rejecting hook AFTER the base commit, so the base itself
        # could be created and the hook only guards the task's commit.
        _add_rejecting_hook(self.tmp)

        # No DB access from the tombstone gate.
        patcher = mock.patch.object(
            agent_git, '_active_tombstone_hashes', return_value=set())
        patcher.start()
        self.addCleanup(patcher.stop)

        # The AI created the deliverable and staged it
        # (commit_task's own `git add -A` would do this for dirty trees).
        _write(self.tmp, 'deliverables/Example_Client_plan.docx', 'content\n')
        _git(self.tmp, 'add', '-A')

    def test_commit_rejected_keeps_staged_diffstat(self):
        result = agent_git.commit_task(self.tmp, 7, 'example client plan')

        # Failure surfaced, with the hook's own message in the error.
        self.assertFalse(result.get('ok'), result)
        self.assertFalse(result.get('changed'), result)
        self.assertTrue(result.get('error'), result)
        self.assertIn('git commit failed', result['error'])
        self.assertIn('blocked', result['error'])

        # The staged diffstat lets the Exec Log still link the deliverable.
        diffstat = result.get('diffstat', '')
        self.assertTrue(diffstat, 'diffstat missing on commit-failure result')
        self.assertIn('Example_Client_plan.docx', diffstat)

        # Branch identity preserved for the recover/review flow.
        self.assertEqual(result.get('branch'), 'main')

        # The file must STILL be staged after the call — nothing discarded.
        porcelain = subprocess.run(
            ['git', 'status', '--porcelain'], cwd=self.tmp,
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn('A  deliverables/Example_Client_plan.docx', porcelain)


@unittest.skipUnless(_git_available(), 'git binary not installed')
class CommitTaskOkTest(unittest.TestCase):
    """Case B — commit_task without a hook keeps succeeding as before."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='aingel-ok-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _init_git_repo(self.tmp)
        _write(self.tmp, 'base.txt', 'base\n')
        _git(self.tmp, 'add', '-A')
        _git(self.tmp, 'commit', '-q', '-m', 'base')

        patcher = mock.patch.object(
            agent_git, '_active_tombstone_hashes', return_value=set())
        patcher.start()
        self.addCleanup(patcher.stop)

        # Real flow: the task runs on its own branch created off base.
        _git(self.tmp, 'checkout', '-q', '-b', 'task/7-report')
        _write(self.tmp, 'deliverables/report.md', 'content\n')
        _git(self.tmp, 'add', '-A')

    def test_commit_succeeds_with_diffstat(self):
        result = agent_git.commit_task(self.tmp, 7, 'report')

        self.assertTrue(result.get('ok'), result)
        self.assertTrue(result.get('changed'), result)
        self.assertIn('deliverables/report.md', result.get('diffstat', ''))
        # The failure-only keys must not appear on success paths.
        self.assertNotIn('error', result)


@unittest.skipUnless(_git_available(), 'git binary not installed')
class StartTaskBranchCheckpointTest(unittest.TestCase):
    """Case C — start_task_branch surfaces a rejected checkpoint commit."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='aingel-checkpoint-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _init_git_repo(self.tmp)
        _write(self.tmp, 'base.txt', 'base\n')
        _git(self.tmp, 'add', '-A')
        _git(self.tmp, 'commit', '-q', '-m', 'base')
        _add_rejecting_hook(self.tmp)

        patcher = mock.patch.object(
            agent_git, '_active_tombstone_hashes', return_value=set())
        patcher.start()
        self.addCleanup(patcher.stop)

        # Pre-existing dirty state that the checkpoint commit cannot land.
        _write(self.tmp, 'notes/hand_notes.txt', 'uncommitted\n')

    def test_checkpoint_failure_surfaced_branch_still_active(self):
        result = agent_git.start_task_branch(self.tmp, 8, 'blocked checkpoint')

        # Task branch still created and active (surface, don't abort).
        self.assertTrue(result.get('active'), result)
        self.assertTrue(result.get('ok'), result)
        self.assertTrue(result.get('branch', '').startswith('task/8-'))

        # The checkpoint failure is surfaced with the hook's message.
        self.assertIn('checkpoint_error', result)
        self.assertTrue(result['checkpoint_error'])
        self.assertIn('git commit failed', result['checkpoint_error'])
        self.assertIn('blocked', result['checkpoint_error'])

        # The new task branch really exists in the repo.
        heads = subprocess.run(
            ['git', 'branch', '--list', result['branch']], cwd=self.tmp,
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn(result['branch'], heads)


class UpdateExecutionGitCommitErrorTest(unittest.TestCase):
    """Case D — update_execution_git persists/clears git_commit_error."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.project = self.td.name

        # Seed one execution row (NOT NULL columns satisfied) in its project.db.
        # get_project_db creates the schema (incl. git_commit_error) on first touch.
        agent_db.get_project_db(self.project)
        conn = sqlite3.connect(os.path.join(self.project, 'project.db'))
        conn.execute(
            'INSERT INTO executions (id, model, status) VALUES (1, ?, ?)',
            ('test-model', 'done'),
        )
        conn.commit()
        conn.close()

    def _connect(self):
        conn = sqlite3.connect(os.path.join(self.project, 'project.db'))
        conn.row_factory = sqlite3.Row
        return conn

    def _git_commit_error(self):
        with self._connect() as conn:
            return conn.execute(
                'SELECT git_commit_error FROM executions WHERE id=1'
            ).fetchone()['git_commit_error']

    # None leaves the previous value untouched; a string overwrites it.
    def test_commit_error_is_stored(self):
        agent_db.update_execution_git(
            1, diffstat='staged diff', commit_error='blocked by hook',
            project_path=self.project)
        self.assertEqual(self._git_commit_error(), 'blocked by hook')

    def test_commit_error_empty_string_clears(self):
        agent_db.update_execution_git(
            1, commit_error='blocked', project_path=self.project)
        self.assertEqual(self._git_commit_error(), 'blocked')

        # '' is not None → overwrite semantics: it clears the stored error.
        agent_db.update_execution_git(1, commit_error='', project_path=self.project)
        self.assertEqual(self._git_commit_error(), '')

    def test_commit_error_none_leaves_untouched(self):
        agent_db.update_execution_git(
            1, commit_error='blocked', project_path=self.project)
        self.assertEqual(self._git_commit_error(), 'blocked')

        # None (the default) must never touch the column — e.g. the success
        # path calls update_execution_git(merge_commit=…) with no commit_error.
        agent_db.update_execution_git(
            1, diffstat='normal diff', project_path=self.project)
        self.assertEqual(self._git_commit_error(), 'blocked')

    # The guarded ALTER for project.db files created before the column existed.
    def test_migration_adds_column_to_pre_existing_db(self):
        # Build a project.db with an OLD executions schema (no git_commit_error).
        old = tempfile.mkdtemp(prefix='aingel-oldschema-')
        self.addCleanup(shutil.rmtree, old, ignore_errors=True)
        conn = sqlite3.connect(os.path.join(old, 'project.db'))
        conn.execute('''
            CREATE TABLE executions (
                id                INTEGER PRIMARY KEY,
                task_id           INTEGER REFERENCES tasks(id),
                model             TEXT NOT NULL,
                status            TEXT NOT NULL DEFAULT 'running',
                started_at        TEXT,
                chat_id           INTEGER,
                last_heartbeat_at TEXT,
                git_branch        TEXT DEFAULT '',
                git_commit        TEXT DEFAULT '',
                git_diffstat      TEXT DEFAULT '',
                git_merge_commit  TEXT DEFAULT ''
            )
        ''')
        conn.commit()
        conn.close()

        # touch project.db → get_project_db runs the guarded ALTERs.
        agent_db.get_project_db(old)
        # Query the OLD db itself — asserting against self.project would pass
        # trivially since setUp's schema already has the column.
        old_conn = sqlite3.connect(os.path.join(old, 'project.db'))
        old_conn.row_factory = sqlite3.Row
        try:
            cols = {r['name'] for r in old_conn.execute('PRAGMA table_info(executions)')}
        finally:
            old_conn.close()
        self.assertIn('git_commit_error', cols)


if __name__ == '__main__':
    unittest.main()
