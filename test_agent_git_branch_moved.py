"""Tests for the branch-moved diffstat fallback in ``agent_git.commit_task``.

The task agent (Mistral Vibe CLI) has shell access, so it can run
``git checkout main`` in the middle of a run.  ``commit_task`` then commits on
whatever HEAD points at, and the old diffstat range ``{base}...HEAD`` is empty
once HEAD == base — the Exec Log's "File" column went blank (regression: exec
20001389, commit 54d6dc3 landed on main).

These tests are hermetic: a throwaway git repo in a temp dir, no network, no
project DB.  ``_active_tombstone_hashes`` is stubbed so the tombstone gate
never touches the central DB.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import agent_git


# 137 chars — comfortably past the default 80-column stat width, so the
# diffstat also proves the ``--stat=1000`` widening still applies.
LONG_NAME = (
    'Northwind_Logistics_18_Points_CUPT_Bilateral_Meeting_2026-11-05_'
    'with_Regional_Plan_and_Follow_up_Actions_for_Cross_Border_Rail.pptx'
)
LONG_REL = 'deliverables/' + LONG_NAME   # > 80 chars on its own


def _git_available():
    return shutil.which('git') is not None


@unittest.skipUnless(_git_available(), 'git binary not installed')
class CommitTaskBranchMovedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='aingel-branch-moved-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        try:
            self._git('init', '-q', '-b', 'main')
        except subprocess.CalledProcessError:
            # Older git without `init -b`.
            self._git('init', '-q')
            self._git('checkout', '-q', '-b', 'main')
        self._git('config', 'user.email', 'test@example.invalid')
        self._git('config', 'user.name', 'AIngel Test')
        self._git('config', 'commit.gpgsign', 'false')

        self._write('base.txt', 'base\n')
        self._git('add', '-A')
        self._git('commit', '-q', '-m', 'base')

        # No DB access from the tombstone gate.
        patcher = mock.patch.object(
            agent_git, '_active_tombstone_hashes', return_value=set())
        patcher.start()
        self.addCleanup(patcher.stop)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _git(self, *args):
        return subprocess.run(
            ['git', *args], cwd=self.tmp, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def _write(self, rel, content):
        path = os.path.join(self.tmp, rel)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)

    def _switch_before_task_commit(self, base):
        """Simulate the CLI's shell: `git checkout <base>` just before we commit.

        The branch switch has to happen *after* ``commit_task`` captured the
        entry branch — that is exactly what the real run does — so it is
        injected right before the task commit runs.
        """
        real_run = agent_git._run
        switched = []

        def spy(project_path, *args, **kwargs):
            is_task_commit = (
                args[:1] == ('commit',)
                and any(isinstance(a, str) and a.startswith('Task #') for a in args)
            )
            if is_task_commit and not switched:
                # Uncommitted (still staged at this point) files carry over.
                subprocess.run(['git', 'checkout', '-q', base],
                               cwd=self.tmp, check=True)
                switched.append(base)
            return real_run(project_path, *args, **kwargs)

        patcher = mock.patch.object(agent_git, '_run', side_effect=spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return switched

    # ── Case A: regression — normal run on the task branch ────────────────────
    def test_commit_task_on_task_branch_has_no_branch_moved(self):
        self._git('checkout', '-q', '-b', 'task/42-normal')
        self._write(LONG_REL, 'content\n')

        result = agent_git.commit_task(self.tmp, 42, 'title')

        self.assertTrue(result.get('ok'), result)
        self.assertTrue(result.get('changed'), result)
        self.assertEqual(result.get('branch'), 'task/42-normal', result)
        self.assertNotIn('branch_moved', result)

        diffstat = result.get('diffstat', '')
        self.assertTrue(diffstat.strip(), 'empty diffstat on the normal path')
        self.assertIn(LONG_NAME, diffstat)
        self.assertNotIn('...', diffstat)
        # The task commit is the one on the task branch.
        self.assertEqual(self._git('rev-parse', '--short', 'HEAD'),
                         result.get('commit'))

    # ── Case B: the bug — the run switched to the base branch mid-run ─────────
    def test_commit_task_branch_moved_uses_last_commit_diffstat(self):
        self._git('checkout', '-q', '-b', 'task/43-switched')
        self._write(LONG_REL, 'content\n')

        switched = self._switch_before_task_commit('main')

        result = agent_git.commit_task(self.tmp, 43, 'switched')

        self.assertEqual(switched, ['main'], 'the branch switch never happened')

        self.assertTrue(result.get('ok'), result)
        self.assertTrue(result.get('changed'), result)
        # 'branch' stays the branch captured at entry (callers merge that one).
        self.assertEqual(result.get('branch'), 'task/43-switched', result)
        self.assertEqual(result.get('branch_moved'), 'main', result)

        # The commit really did land on the base branch.
        self.assertEqual(self._git('rev-parse', '--abbrev-ref', 'HEAD'), 'main')
        self.assertEqual(self._git('rev-parse', '--short', 'HEAD'),
                         result.get('commit'))
        # Guard: the old range would have been empty here.
        self.assertEqual(self._git('diff', '--stat', 'main...HEAD'), '')

        diffstat = result.get('diffstat', '')
        self.assertTrue(diffstat.strip(), 'diffstat empty after a branch move')
        self.assertIn(LONG_NAME, diffstat)


if __name__ == '__main__':
    unittest.main()
