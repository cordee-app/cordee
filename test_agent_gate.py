"""D-list tombstone-gate tests (agent_git.gate_tombstones).

Covers:
  - B2: a deletion + a copy under a new name in the SAME commit is NOT collapsed
        into a rename entry (which the gate would skip) — the copy is stripped.
  - B3: the precommitted / range scope parses unicode (Polish) filenames via -z
        instead of C-quoting them.
  - the staged gate re-trashes a tombstoned file and leaves HEAD clean.
  - A3: a tiny/empty resurrected file is NOT stripped (never tombstoned).
"""
import os
import shutil
import subprocess
import tempfile
import unittest

import agent_files
import agent_db
import agent_git


def _sha(path):
    return agent_git._hash_file(path)


class GateTests(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        self._mp = mock.patch('agent_config.TRASH_ROOT', tempfile.mkdtemp() + '/trashroot')
        self._mp.start()
        self._pid = mock.patch('agent_files._project_id_for', return_value=1)
        self._pid.start()
        self.td = tempfile.TemporaryDirectory()
        self.p = self.td.name
        os.makedirs(os.path.join(self.p, 'Working Documents'))
        agent_db.get_project_db(self.p).close()
        def sh(*a): return subprocess.run(['git', *a], capture_output=True, text=True, cwd=self.p)
        self.sh = sh
        sh('init', '-q', '-b', 'main'); sh('config', 'user.email', 't@t'); sh('config', 'user.name', 't')
        with open(os.path.join(self.p, "Working Documents/base.txt"), "w") as f: f.write("base-content")
        sh('add', '-A'); sh('commit', '-q', '-m', 'base')

    def tearDown(self):
        self._mp.stop()
        self._pid.stop()
        self.td.cleanup()

    def test_staged_rename_matches_tombstone_and_strips(self):
        # B2 + rename: delete victim, re-create the SAME bytes under a new name,
        # both changes staged together. The gate must treat the copy as a
        # resurrection (content-sha match) and strip it from HEAD.
        payload = 'same-commit-rename-payload-' * 10  # >= 64 bytes
        victim = os.path.join(self.p, 'Working Documents/victim.txt')
        open(victim, 'w').write(payload)
        self.sh('add', '-A'); self.sh('commit', '-q', '-m', 'add victim')
        agent_files.delete(self.p, ['Working Documents/victim.txt'])   # records tombstone
        # now re-create same content under a new name, and also drop the deletion
        # + the new file in ONE staged commit (the git rename-detection trap).
        self.sh('add', '-A')  # stage the deletion
        renamed = os.path.join(self.p, 'Working Documents/rebranded.txt')
        open(renamed, 'w').write(payload)
        self.sh('add', 'Working Documents/rebranded.txt')
        g = agent_git.gate_tombstones(self.p, scope='staged')
        self.assertIn('Working Documents/rebranded.txt', g['stripped'])
        self.sh('commit', '-q', '-m', 'task')
        tree = self.sh('ls-tree', '-r', '--name-only', 'HEAD').stdout
        self.assertNotIn('rebranded.txt', tree)
        self.assertFalse(os.path.exists(renamed))

    def test_range_scope_handles_unicode_renames(self):
        # B3: a precommitted (CLI-committed) run restored a tombstoned file under
        # a Polish/unicode filename on a task branch; range-scope gate (base...HEAD)
        # must catch it via -z parsing.
        payload = 'unicode-filename-payload-' * 10
        victim = os.path.join(self.p, 'Working Documents/Załączniki nr 1.pdf')
        open(victim, 'w').write(payload)
        self.sh('add', '-A'); self.sh('commit', '-q', '-m', 'add victim')
        # user deletes it (soft-delete → tombstone) and it leaves git via commit
        agent_files.delete(self.p, ['Working Documents/Załączniki nr 1.pdf'])
        self.sh('add', '-A'); self.sh('commit', '-q', '-m', 'remove victim')
        # CLI work happens on a task branch off main
        self.sh('checkout', '-q', '-b', 'task/1-x')
        open(victim, 'w').write(payload)
        self.sh('add', '-A'); self.sh('commit', '-q', '-m', 'restored by CLI')
        g = agent_git.gate_tombstones(self.p, base='main', scope='range')
        self.assertIn('Working Documents/Załączniki nr 1.pdf', g['stripped'])
        # commit_task's precommitted branch follows up with a removal commit
        self.sh('commit', '-q', '-m', 'remove tombstoned')
        tree = self.sh('ls-tree', '-r', '--name-only', 'HEAD').stdout
        self.assertNotIn('Załączniki nr 1.pdf', tree)

    def test_tiny_file_not_stripped(self):
        # A3: empty/tiny files never tombstone, so a tiny resurrected file must
        # not be stripped (would be a false positive).
        open(os.path.join(self.p, 'Working Documents/empty.txt'), 'w').write('')
        agent_db.record_delete_tombstone(self.p, 'Working Documents/empty.txt',
                                         _sha(os.path.join(self.p, 'Working Documents/empty.txt')))
        self.sh('add', '-A')
        g = agent_git.gate_tombstones(self.p, scope='staged')
        self.assertNotIn('Working Documents/empty.txt', g['stripped'])


    def test_restored_file_survives_the_gate(self):
        # C7: restore clears tombstones by hash, so a file whose bytes were also
        # tombstoned under another name is not re-trashed at the next commit.
        payload = 'restore-then-commit-payload-' * 5
        wd = os.path.join(self.p, 'Working Documents')
        open(os.path.join(wd, 'copy.txt'), 'w').write(payload)
        open(os.path.join(wd, 'keep.txt'), 'w').write(payload)
        self.sh('add', '-A'); self.sh('commit', '-q', '-m', 'two copies')
        agent_files.delete(self.p, ['Working Documents/copy.txt'])
        agent_files.delete(self.p, ['Working Documents/keep.txt'])
        self.sh('add', '-A'); self.sh('commit', '-q', '-m', 'deleted')
        agent_files.restore(self.p, 'Working Documents/keep.txt')
        self.sh('add', '-A')
        g = agent_git.gate_tombstones(self.p, scope='staged')
        self.assertEqual(g['stripped'], [])
        self.assertTrue(os.path.exists(os.path.join(wd, 'keep.txt')))


if __name__ == '__main__':
    unittest.main()
