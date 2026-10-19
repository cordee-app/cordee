"""Tests for the widened diffstat used by the stored execution diff (RP-099).

Git abbreviates long paths at the default 80-column diffstat width, so the
Exec Log lost the link to files with long names.  ``agent_git`` now passes
``--stat=1000`` at the three sites that produce a stored diffstat.

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


# 137 chars — comfortably past the default 80-column stat width.
LONG_NAME = (
    'Northwind_Logistics_18_Points_CUPT_Bilateral_Meeting_2026-11-05_'
    'with_Regional_Plan_and_Follow_up_Actions_for_Cross_Border_Rail.pptx'
)


def _git_available():
    return shutil.which('git') is not None


@unittest.skipUnless(_git_available(), 'git binary not installed')
class DiffstatWidthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='aingel-diffstat-')
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
        ).stdout

    def _write(self, rel, content):
        path = os.path.join(self.tmp, rel)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)

    def _capture_run(self):
        """Patch _run to record argv while still executing real git."""
        calls = []
        real_run = agent_git._run

        def spy(project_path, *args, **kwargs):
            calls.append(args)
            return real_run(project_path, *args, **kwargs)

        patcher = mock.patch.object(agent_git, '_run', side_effect=spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def _assert_wide_stat_call(self, calls):
        diff_calls = [c for c in calls if c and c[0] == 'diff']
        self.assertTrue(diff_calls, f'no diff call captured; calls={calls!r}')
        self.assertTrue(
            any(c[1:2] == ('--stat=1000',) for c in diff_calls),
            f'diff call did not use --stat=1000; calls={calls!r}',
        )
        self.assertFalse(
            any(c[1:2] == ('--stat',) for c in diff_calls),
            f'diff call still used bare --stat; calls={calls!r}',
        )

    # ── integration: real repo, full path survives into the diffstat ─────────
    def test_commit_task_diffstat_keeps_full_long_filename(self):
        self.assertEqual(agent_git._DIFFSTAT_FLAG, '--stat=1000')

        self._git('checkout', '-q', '-b', 'task/1-long-name')
        self._write('deliverables/' + LONG_NAME, 'content\n')
        self._git('add', '-A')

        # Guard: with git's default width this exact diff IS abbreviated, so
        # the assertion below would fail without the fix.
        default_stat = self._git('diff', '--stat', '--cached', 'main')
        self.assertIn('...', default_stat)
        self.assertNotIn(LONG_NAME, default_stat)

        result = agent_git.commit_task(self.tmp, 1, 'long name')

        self.assertTrue(result.get('ok'), result)
        self.assertTrue(result.get('changed'), result)
        diffstat = result.get('diffstat', '')
        self.assertIn(LONG_NAME, diffstat)
        self.assertNotIn('...', diffstat)

    # ── all three changed call sites pass the wide flag ─────────────────────
    def test_commit_task_dirty_tree_uses_wide_stat(self):
        calls = self._capture_run()
        self._git('checkout', '-q', '-b', 'task/2-dirty')
        self._write('deliverables/' + LONG_NAME, 'content\n')

        result = agent_git.commit_task(self.tmp, 2, 'dirty')

        self.assertTrue(result.get('ok'), result)
        self._assert_wide_stat_call(calls)

    def test_commit_task_precommitted_uses_wide_stat(self):
        calls = self._capture_run()
        self._git('checkout', '-q', '-b', 'task/3-precommitted')
        self._write('deliverables/' + LONG_NAME, 'content\n')
        self._git('add', '-A')
        self._git('commit', '-q', '-m', 'CLI pre-commit')

        result = agent_git.commit_task(self.tmp, 3, 'precommitted')

        self.assertTrue(result.get('ok'), result)
        self.assertTrue(result.get('changed'), result)
        self.assertTrue(result.get('precommitted'), result)
        self._assert_wide_stat_call(calls)

    def test_commit_chat_uses_wide_stat(self):
        calls = self._capture_run()
        self._write('deliverables/' + LONG_NAME, 'content\n')

        result = agent_git.commit_chat(self.tmp, 7)

        self.assertTrue(result.get('ok'), result)
        self.assertTrue(result.get('changed'), result)
        self.assertIn(LONG_NAME, result.get('diffstat', ''))
        self._assert_wide_stat_call(calls)


if __name__ == '__main__':
    unittest.main()
