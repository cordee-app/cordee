"""
Unit tests for Phase 1 unified file manager — agent_files + agent_db.file_tags.

Covers: _is_writable_rel 16-case matrix, _is_forbidden_path, _safe_resolve
(symlink containment), mkdir/move/copy/delete happy paths, tag migration via
agent_db (needs a temp project dir with a real project.db), and .trash edges.

Run:  venv/bin/python -m unittest test_agent_files -v
"""
import os
import json
import shutil
import tempfile
import unittest

import agent_files
import agent_db


class _TempProjectMixin:
    """Create a temp project dir with a real project.db for file_tags tests."""
    def setUp(self):
        from unittest import mock
        # Unregistered temp projects use the in-tree trash layout, which delete()
        # only allows when the vault trash root is explicitly disabled (B4).
        self._trash_off = mock.patch('agent_config.TRASH_ROOT', '')
        self._trash_off.start()
        self.addCleanup(self._trash_off.stop)
        self.td = tempfile.TemporaryDirectory()
        self.project = self.td.name
        # create canonical writable dir
        os.makedirs(os.path.join(self.project, 'Working Documents'), exist_ok=True)
        # ensure project.db exists
        conn = agent_db.get_project_db(self.project)
        conn.close()

    def tearDown(self):
        self.td.cleanup()


class IsWritableMatrixTests(unittest.TestCase):
    # 16 representative cases — maps rel -> expected bool
    CASES = [
        ('Working Documents/a.txt',           True),
        ('Working Documents/sub/a.txt',       True),
        ('Working Docs/x',                    True),
        ('My Docs/x',                         True),
        ('docs/x',                            True),
        ('working-docs/x',                    True),
        ('Working Documents',                 True),   # bare prefix is writable
        ('Artifacts/x',                       False),
        ('READMEFIRST.md',                    False),
        ('project.db',                        False),  # .db suffix
        ('a.txt',                             False),  # no prefix
        ('.env',                              False),
        ('.git/x',                            False),
        ('Working Documents/../x',            False),  # traversal
        ('/abs',                              False),
        ('Working Documents/a.bak',           False),  # .bak suffix
    ]

    def test_matrix(self):
        for rel, expected in self.CASES:
            with self.subTest(rel=rel):
                self.assertEqual(agent_files._is_writable_rel(rel), expected,
                                 f"_is_writable_rel({rel!r}) expected {expected}")

    def test_extra_edges(self):
        # case-insensitive suffix, dotfile, double slash
        self.assertFalse(agent_files._is_writable_rel('Working Documents/PROJECT.DB'))
        self.assertFalse(agent_files._is_writable_rel('Working Documents/.hidden'))
        self.assertFalse(agent_files._is_writable_rel('Working Documents//a.txt'))
        # backslash absolute
        self.assertFalse(agent_files._is_writable_rel('\\abs'))


class IsForbiddenTests(unittest.TestCase):
    def test_forbidden(self):
        self.assertTrue(agent_files._is_forbidden_path('.env'))
        self.assertTrue(agent_files._is_forbidden_path('.env.example'))
        self.assertTrue(agent_files._is_forbidden_path('project.db'))
        self.assertTrue(agent_files._is_forbidden_path('a.bak'))
        self.assertTrue(agent_files._is_forbidden_path('.hidden'))
        self.assertTrue(agent_files._is_forbidden_path('.git'))
        self.assertTrue(agent_files._is_forbidden_path('node_modules'))
        self.assertFalse(agent_files._is_forbidden_path('normal.txt'))
        self.assertFalse(agent_files._is_forbidden_path('My Docs'))


class SafeResolveSymlinkTests(unittest.TestCase):
    def test_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, 'Working Documents'))
            with self.assertRaises(ValueError):
                agent_files._safe_resolve(td, 'Working Documents/../x')

    def test_absolute_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, 'Working Documents'))
            with self.assertRaises(ValueError):
                agent_files._safe_resolve(td, '/abs')

    def test_forbidden_component_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, 'Working Documents'))
            with self.assertRaises(ValueError):
                agent_files._safe_resolve(td, 'Working Documents/project.db')

    def test_symlink_file_escape_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, 'Working Documents'))
            secret = os.path.join(td, 'project.db')
            with open(secret, 'w') as f:
                f.write('secret')
            lnk = os.path.join(td, 'Working Documents', 'lnk')
            os.symlink('../project.db', lnk)
            with self.assertRaisesRegex(ValueError, 'symlink not allowed'):
                agent_files._safe_resolve(td, 'Working Documents/lnk')
            self.assertTrue(os.path.exists(secret))

    def test_symlinked_parent_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, 'Working Documents'))
            outside = os.path.join(td, 'outside')
            os.makedirs(outside)
            open(os.path.join(outside, 'file.txt'), 'w').close()
            lnk = os.path.join(td, 'Working Documents', 'esc')
            os.symlink('../outside', lnk)
            with self.assertRaisesRegex(ValueError, 'symlink not allowed'):
                agent_files._safe_resolve(td, 'Working Documents/esc/file.txt')

    def test_chained_symlink_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, 'Working Documents'))
            os.makedirs(os.path.join(td, 'outside2'))
            # Working Documents/a -> ../outside2, and outside2/b -> ../outside3
            a = os.path.join(td, 'Working Documents', 'a')
            os.symlink('../outside2', a)
            with self.assertRaisesRegex(ValueError, 'symlink not allowed'):
                agent_files._safe_resolve(td, 'Working Documents/a/x.txt')

    def test_lexical_path_returned(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, 'Working Documents'))
            out = agent_files._safe_resolve(td, 'Working Documents/sub/file.txt')
            self.assertEqual(out, os.path.join(td, 'Working Documents/sub/file.txt'))
            self.assertFalse(os.path.islink(out))


class HappyOpsTests(_TempProjectMixin, unittest.TestCase):
    def test_mkdir_nested(self):
        p = agent_files.mkdir(self.project, 'Working Documents/a/b/c')
        self.assertTrue(os.path.isdir(p))
        self.assertTrue(os.path.isdir(os.path.join(self.project, 'Working Documents/a/b/c')))

    def test_move_file(self):
        src = os.path.join(self.project, 'Working Documents', 'a.txt')
        with open(src, 'w') as f:
            f.write('hello')
        agent_files.move(self.project, 'Working Documents/a.txt', 'Working Documents/b.txt')
        self.assertFalse(os.path.exists(src))
        self.assertTrue(os.path.exists(os.path.join(self.project, 'Working Documents/b.txt')))

    def test_move_folder(self):
        os.makedirs(os.path.join(self.project, 'Working Documents/src'))
        with open(os.path.join(self.project, 'Working Documents/src/file.txt'), 'w') as f:
            f.write('x')
        agent_files.move(self.project, 'Working Documents/src', 'Working Documents/dst')
        self.assertFalse(os.path.exists(os.path.join(self.project, 'Working Documents/src')))
        self.assertTrue(os.path.exists(os.path.join(self.project, 'Working Documents/dst/file.txt')))

    def test_copy_file(self):
        src = os.path.join(self.project, 'Working Documents', 'a.txt')
        with open(src, 'w') as f:
            f.write('hello')
        agent_files.copy(self.project, 'Working Documents/a.txt', 'Working Documents/b.txt')
        self.assertTrue(os.path.exists(src))
        self.assertTrue(os.path.exists(os.path.join(self.project, 'Working Documents/b.txt')))

    def test_copy_folder(self):
        os.makedirs(os.path.join(self.project, 'Working Documents/src'))
        with open(os.path.join(self.project, 'Working Documents/src/file.txt'), 'w') as f:
            f.write('x')
        agent_files.copy(self.project, 'Working Documents/src', 'Working Documents/dst')
        self.assertTrue(os.path.exists(os.path.join(self.project, 'Working Documents/src/file.txt')))
        self.assertTrue(os.path.exists(os.path.join(self.project, 'Working Documents/dst/file.txt')))

    def test_dst_exists_rejected(self):
        for fn in (agent_files.move, agent_files.copy):
            with self.subTest(fn=fn.__name__):
                td = tempfile.mkdtemp(dir=self.project)
                try:
                    os.makedirs(os.path.join(self.project, 'Working Documents'), exist_ok=True)
                    a = os.path.join(self.project, 'Working Documents', f'a_{fn.__name__}.txt')
                    b = os.path.join(self.project, 'Working Documents', f'b_{fn.__name__}.txt')
                    open(a, 'w').close()
                    open(b, 'w').close()
                    with self.assertRaises(ValueError):
                        fn(self.project, f'Working Documents/a_{fn.__name__}.txt', f'Working Documents/b_{fn.__name__}.txt')
                finally:
                    shutil.rmtree(td, ignore_errors=True)

    def test_delete_to_trash(self):
        src = os.path.join(self.project, 'Working Documents', 'del.txt')
        with open(src, 'w') as f:
            f.write('x')
        trashed = agent_files.delete(self.project, ['Working Documents/del.txt'])
        self.assertEqual(trashed, ['Working Documents/del.txt'])
        self.assertFalse(os.path.exists(src))
        # .trash exists under project
        trash_root = os.path.join(self.project, '.trash', 'files')
        self.assertTrue(os.path.isdir(trash_root))
        # file preserved under trash with original rel prefix
        found = []
        for root, _, files in os.walk(trash_root):
            if 'del.txt' in files:
                found.append(os.path.join(root, 'del.txt'))
        self.assertTrue(found, "trashed file not found under .trash")

    def test_delete_batch(self):
        a = os.path.join(self.project, 'Working Documents', 'a_del.txt')
        b = os.path.join(self.project, 'Working Documents', 'b_del.txt')
        open(a, 'w').close()
        open(b, 'w').close()
        trashed = agent_files.delete(self.project, ['Working Documents/a_del.txt', 'Working Documents/b_del.txt'])
        self.assertEqual(set(trashed), {'Working Documents/a_del.txt', 'Working Documents/b_del.txt'})


class TagMigrationTests(_TempProjectMixin, unittest.TestCase):
    def test_move_migrates_tag(self):
        # create file + tag
        path = os.path.join(self.project, 'Working Documents', 'a.txt')
        with open(path, 'w') as f:
            f.write('x')
        agent_db.set_file_tag(self.project, 'Working Documents/a.txt', ['alpha'], 'note1')
        agent_files.move(self.project, 'Working Documents/a.txt', 'Working Documents/b.txt')
        tags = agent_db.get_file_tags(self.project)
        self.assertNotIn('Working Documents/a.txt', tags)
        self.assertIn('Working Documents/b.txt', tags)
        self.assertEqual(tags['Working Documents/b.txt']['tags'], ['alpha'])
        self.assertEqual(tags['Working Documents/b.txt']['note'], 'note1')

    def test_move_folder_migrates_children(self):
        os.makedirs(os.path.join(self.project, 'Working Documents/src'))
        with open(os.path.join(self.project, 'Working Documents/src/file.txt'), 'w') as f:
            f.write('x')
        agent_db.set_file_tag(self.project, 'Working Documents/src/file.txt', ['t'], '')
        agent_files.move(self.project, 'Working Documents/src', 'Working Documents/dst')
        tags = agent_db.get_file_tags(self.project)
        self.assertNotIn('Working Documents/src/file.txt', tags)
        self.assertIn('Working Documents/dst/file.txt', tags)

    def test_copy_duplicates_tag(self):
        path = os.path.join(self.project, 'Working Documents', 'a.txt')
        with open(path, 'w') as f:
            f.write('x')
        agent_db.set_file_tag(self.project, 'Working Documents/a.txt', ['copytag'], 'n')
        agent_files.copy(self.project, 'Working Documents/a.txt', 'Working Documents/b.txt')
        tags = agent_db.get_file_tags(self.project)
        self.assertIn('Working Documents/a.txt', tags)
        self.assertIn('Working Documents/b.txt', tags)
        self.assertEqual(tags['Working Documents/b.txt']['tags'], ['copytag'])

    def test_delete_clears_tag(self):
        path = os.path.join(self.project, 'Working Documents', 'del_tag.txt')
        with open(path, 'w') as f:
            f.write('x')
        agent_db.set_file_tag(self.project, 'Working Documents/del_tag.txt', ['t'], 'note')
        agent_files.delete(self.project, ['Working Documents/del_tag.txt'])
        tags = agent_db.get_file_tags(self.project)
        self.assertNotIn('Working Documents/del_tag.txt', tags)

    def test_delete_folder_clears_children_tags(self):
        os.makedirs(os.path.join(self.project, 'Working Documents/folder'))
        with open(os.path.join(self.project, 'Working Documents/folder/file.txt'), 'w') as f:
            f.write('x')
        agent_db.set_file_tag(self.project, 'Working Documents/folder/file.txt', ['t'], '')
        agent_files.delete(self.project, ['Working Documents/folder'])
        tags = agent_db.get_file_tags(self.project)
        self.assertNotIn('Working Documents/folder/file.txt', tags)

    def test_set_empty_deletes_row(self):
        agent_db.set_file_tag(self.project, 'Working Documents/x.txt', ['a'], 'n')
        # set with empty tags + empty note should delete
        agent_db.set_file_tag(self.project, 'Working Documents/x.txt', [], '')
        self.assertNotIn('Working Documents/x.txt', agent_db.get_file_tags(self.project))

    def test_copy_folder_duplicates_child_tags(self):
        os.makedirs(os.path.join(self.project, 'Working Documents/src2'))
        with open(os.path.join(self.project, 'Working Documents/src2/f.txt'), 'w') as f:
            f.write('x')
        agent_db.set_file_tag(self.project, 'Working Documents/src2/f.txt', ['child'], '')
        agent_files.copy(self.project, 'Working Documents/src2', 'Working Documents/dst2')
        tags = agent_db.get_file_tags(self.project)
        self.assertIn('Working Documents/src2/f.txt', tags)
        self.assertIn('Working Documents/dst2/f.txt', tags)


class TombstoneTests(_TempProjectMixin, unittest.TestCase):
    def test_delete_records_tombstone(self):
        with open(os.path.join(self.project, 'Working Documents/a.txt'), 'w') as f:
            f.write('needle' * 20)  # >= _TOMBSTONE_MIN_BYTES
        agent_files.delete(self.project, ['Working Documents/a.txt'])
        tombstones = agent_db.get_delete_tombstones(self.project)
        self.assertEqual(len(tombstones), 1)
        self.assertEqual(tombstones[0]['rel_path'], 'Working Documents/a.txt')
        self.assertEqual(len(tombstones[0]['sha256']), 64)

    def test_folder_delete_records_child_tombstones(self):
        os.makedirs(os.path.join(self.project, 'Working Documents/folder'))
        with open(os.path.join(self.project, 'Working Documents/folder/f.txt'), 'w') as f:
            f.write('folder-content-payload' * 20)
        agent_files.delete(self.project, ['Working Documents/folder'])
        tombstones = agent_db.get_delete_tombstones(self.project)
        self.assertEqual(len(tombstones), 1)
        self.assertEqual(tombstones[0]['rel_path'], 'Working Documents/folder/f.txt')

    def test_clear_tombstone(self):
        with open(os.path.join(self.project, 'Working Documents/a.txt'), 'w') as f:
            f.write('some-writable-content-of-sufficient-length' * 20)
        agent_files.delete(self.project, ['Working Documents/a.txt'])
        self.assertEqual(len(agent_db.get_delete_tombstones(self.project)), 1)
        removed = agent_db.clear_delete_tombstone(self.project, rel_path='Working Documents/a.txt')
        self.assertGreaterEqual(removed, 1)
        self.assertEqual(len(agent_db.get_delete_tombstones(self.project)), 0)

    def test_tiny_file_no_tombstone(self):
        # A3: a 0-byte / tiny file must NOT record a tombstone.
        with open(os.path.join(self.project, 'Working Documents/tiny.txt'), 'w') as f:
            f.write('x')
        agent_files.delete(self.project, ['Working Documents/tiny.txt'])
        self.assertEqual(len(agent_db.get_delete_tombstones(self.project)), 0)

    def test_reap_old_tombstone(self):
        conn = agent_db.get_project_db(self.project)
        conn.execute(
            "INSERT OR REPLACE INTO delete_tombstones (rel_path, sha256, deleted_at) "
            "VALUES (?, ?, ?)",
            ('Working Documents/old.txt', 'x' * 64, '2020-01-01T00:00:00Z'))
        conn.commit()
        conn.close()
        removed = agent_db.reap_delete_tombstones(self.project, '2021-01-01T00:00:00Z')
        self.assertGreaterEqual(removed, 1)
        self.assertEqual(len(agent_db.get_delete_tombstones(self.project)), 0)

    def test_folder_delete_removes_dir(self):
        # A2: a deleted folder must not leave an empty tree behind.
        os.makedirs(os.path.join(self.project, 'Working Documents/folder'))
        with open(os.path.join(self.project, 'Working Documents/folder/f.txt'), 'w') as f:
            f.write('folder-delete-payload' * 20)
        agent_files.delete(self.project, ['Working Documents/folder'])
        self.assertFalse(os.path.exists(os.path.join(self.project, 'Working Documents/folder')))

    def test_empty_folder_delete_removes_dir(self):
        # A2: an entirely empty folder is removed too.
        os.makedirs(os.path.join(self.project, 'Working Documents/emptydir'))
        agent_files.delete(self.project, ['Working Documents/emptydir'])
        self.assertFalse(os.path.exists(os.path.join(self.project, 'Working Documents/emptydir')))

    def test_delete_overwrite_no_tombstone_no_bucket(self):
        # A1: the MOVE/COPY Overwrite:T destination delete must not tombstone or
        # remove the bucket object (the new content replaces the old).
        with open(os.path.join(self.project, 'Working Documents/x.txt'), 'w') as f:
            f.write('overwrite-destination-payload-abcdefghij' * 5)  # >= 64 bytes
        agent_files.delete(self.project, ['Working Documents/x.txt'],
                           record_tombstone=False, remove_bucket=False)
        self.assertEqual(len(agent_db.get_delete_tombstones(self.project)), 0)

    def test_trash_resolved_outside_working_docs(self):
        # B1: the gate must be able to re-trash a resurrection outside Working Docs.
        from unittest import mock
        with mock.patch('agent_config.TRASH_ROOT', self.td.name + '/trashroot'), \
             mock.patch('agent_files._project_id_for', return_value=42):
            os.makedirs(os.path.join(self.project, 'Legal'))
            with open(os.path.join(self.project, 'Legal/contract.txt'), 'w') as f:
                f.write('legal-contract-payload' * 20)
            trashed = agent_files.trash_resolved(self.project, 'Legal/contract.txt')
            self.assertTrue(trashed)
            # moved OUT of the project (not left on disk, untracked)
            self.assertFalse(os.path.exists(os.path.join(self.project, 'Legal/contract.txt')))
            # and landed somewhere under the vault trash root
            self.assertTrue(any(
                os.path.exists(os.path.join(self.td.name, 'trashroot', '42', 'files', d, 'Legal', 'contract.txt'))
                for d in os.listdir(os.path.join(self.td.name, 'trashroot', '42', 'files'))))

    def test_delete_raises_without_trash_root(self):
        # B4: with AINGEL_TRASH_ROOT unset and an unregistered temp project (no
        # central DB id), delete uses the in-tree fallback path (tests). With a
        # bogus absolute path the project must raise 'project not found'.
        with self.assertRaises(ValueError):
            agent_files.delete('/nonexistent/project', ['Working Documents/x.txt'])


class DeleteTreeTests(_TempProjectMixin, unittest.TestCase):
    """Folder cleanup after delete: only what was deleted goes away."""

    def _write(self, rel, payload='p' * 100):
        full = os.path.join(self.project, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w') as f:
            f.write(payload)

    def test_nested_folder_delete_removes_whole_tree(self):
        self._write('Working Documents/A/B/f.txt')
        os.makedirs(os.path.join(self.project, 'Working Documents/A/B/empty'))
        agent_files.delete(self.project, ['Working Documents/A'])
        self.assertFalse(os.path.exists(os.path.join(self.project, 'Working Documents/A')))

    def test_deleting_last_file_keeps_its_folder(self):
        self._write('Working Documents/Keep/only.txt')
        agent_files.delete(self.project, ['Working Documents/Keep/only.txt'])
        self.assertTrue(os.path.isdir(os.path.join(self.project, 'Working Documents/Keep')))

    def test_deleting_last_file_keeps_working_documents(self):
        self._write('Working Documents/solo.txt')
        agent_files.delete(self.project, ['Working Documents/solo.txt'])
        self.assertTrue(os.path.isdir(os.path.join(self.project, 'Working Documents')))

    def test_unregistered_project_with_vault_trash_root_raises(self):
        # B4: no silent in-tree fallback when a vault trash root is configured.
        from unittest import mock
        self._write('Working Documents/x.txt')
        with mock.patch('agent_config.TRASH_ROOT', self.td.name + '/trashroot'), \
             mock.patch('agent_files._project_id_for', return_value=None):
            with self.assertRaises(RuntimeError):
                agent_files.delete(self.project, ['Working Documents/x.txt'])
        self.assertTrue(os.path.exists(os.path.join(self.project, 'Working Documents/x.txt')))
        self.assertFalse(os.path.exists(os.path.join(self.project, '.trash')))


class RestoreTests(_TempProjectMixin, unittest.TestCase):
    """C7: restore brings a file back and clears every tombstone for its bytes."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        root = self.td.name + '/trashroot'
        for patcher in (mock.patch('agent_config.TRASH_ROOT', root),
                        mock.patch('agent_files._project_id_for', return_value=7)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.wd = os.path.join(self.project, 'Working Documents')

    def _write(self, name, payload):
        with open(os.path.join(self.wd, name), 'w') as f:
            f.write(payload)

    def test_restore_clears_hash_tombstones_under_other_paths(self):
        payload = 'restore-me-payload-' * 10
        self._write('dup.txt', payload)
        self._write('orig.txt', payload)
        agent_files.delete(self.project, ['Working Documents/dup.txt'])
        agent_files.delete(self.project, ['Working Documents/orig.txt'])
        self.assertEqual(len(agent_db.get_delete_tombstones(self.project)), 2)
        agent_files.restore(self.project, 'Working Documents/orig.txt')
        self.assertTrue(os.path.exists(os.path.join(self.wd, 'orig.txt')))
        # The commit gate matches by hash, so no tombstone for these bytes may remain.
        self.assertEqual(agent_db.get_delete_tombstones(self.project), [])

    def test_restore_picks_most_recent_deletion(self):
        import time as _t
        self._write('v.txt', 'first-version-' * 10)
        os.utime(os.path.join(self.wd, 'v.txt'), (2_000_000_000, 2_000_000_000))  # "newer" mtime
        agent_files.delete(self.project, ['Working Documents/v.txt'])
        _t.sleep(1.1)  # distinct <ts> directory
        self._write('v.txt', 'second-version-' * 10)
        os.utime(os.path.join(self.wd, 'v.txt'), (1_000_000_000, 1_000_000_000))  # "older" mtime
        agent_files.delete(self.project, ['Working Documents/v.txt'])
        agent_files.restore(self.project, 'Working Documents/v.txt')
        with open(os.path.join(self.wd, 'v.txt')) as f:
            self.assertTrue(f.read().startswith('second-version-'))

    def test_restore_refuses_symlinked_destination(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        os.makedirs(os.path.join(self.wd, 'sub'))
        self._write('sub/s.txt', 'symlink-target-payload-' * 5)
        agent_files.delete(self.project, ['Working Documents/sub/s.txt'])
        os.rmdir(os.path.join(self.wd, 'sub'))
        os.symlink(outside, os.path.join(self.wd, 'sub'))
        with self.assertRaises(ValueError):
            agent_files.restore(self.project, 'Working Documents/sub/s.txt')
        self.assertEqual(os.listdir(outside), [])


class ListFilesUnderTests(unittest.TestCase):
    """agent_files.list_files_under is the shared depth-capped scanner behind
    the working-doc listers (agent_api._list_working_doc_names,
    agent_memory.list_working_docs, agent_tools.list_working_docs,
    prompt_builder._resolve_referenced_files). Regression: task #10001145
    (a client project) referenced a file at
    Working Documents/CUPT/II Etap/file.pdf — two subdirectory levels below
    the Working Documents root — which the old 1-level-deep scan never saw.
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = self.td.name
        self.addCleanup(self.td.cleanup)
        # root/top.txt
        # root/CUPT/mid.txt
        # root/CUPT/II Etap/deep.pdf   <- 2 subdir levels deep
        os.makedirs(os.path.join(self.root, 'CUPT', 'II Etap'))
        with open(os.path.join(self.root, 'top.txt'), 'w') as f:
            f.write('top')
        with open(os.path.join(self.root, 'CUPT', 'mid.txt'), 'w') as f:
            f.write('mid')
        with open(os.path.join(self.root, 'CUPT', 'II Etap', 'deep.pdf'), 'w') as f:
            f.write('deep')
        # hidden dir + hidden file must never surface
        os.makedirs(os.path.join(self.root, '.git'))
        with open(os.path.join(self.root, '.git', 'HEAD'), 'w') as f:
            f.write('ref: refs/heads/main')
        with open(os.path.join(self.root, '.hidden.txt'), 'w') as f:
            f.write('hidden')

    def test_default_depth_finds_two_levels_deep(self):
        names = {n for n, _ in agent_files.list_files_under(self.root, max_depth=3)}
        self.assertEqual(names, {'top.txt', 'mid.txt', 'deep.pdf'})

    def test_depth_two_misses_two_levels_deep(self):
        # This is the old, buggy behavior: root + 1 subdir level only.
        names = {n for n, _ in agent_files.list_files_under(self.root, max_depth=2)}
        self.assertEqual(names, {'top.txt', 'mid.txt'})
        self.assertNotIn('deep.pdf', names)

    def test_skips_hidden_dirs_and_files(self):
        names = {n for n, _ in agent_files.list_files_under(self.root, max_depth=3)}
        self.assertNotIn('HEAD', names)
        self.assertNotIn('.hidden.txt', names)

    def test_nonexistent_root_returns_empty(self):
        self.assertEqual(agent_files.list_files_under(os.path.join(self.root, 'nope')), [])


if __name__ == '__main__':
    unittest.main()
