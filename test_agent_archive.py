"""Regression tests for the project archive / restore feature.

Covers the pure helpers in ``agent_archive`` (build/validate/restore/reap) and
the central-row helpers in ``agent_db`` (export / collision-check / restore).

Isolation style follows the rest of the suite: a fresh temp dir per test, the
central DB path patched to ``<tmp>/aingel.db`` and ``PROJECTS_ROOT`` patched to
``<tmp>/projects`` (both the ``agent_db`` import and the live
``agent_config.PROJECTS_ROOT`` that ``agent_archive`` reads dynamically). The
per-process project-DB schema cache (``agent_db._PROJECT_DB_READY``) is cleared
between tests so a path reused across tests always re-runs migrations.

Run:  venv/bin/python3 -m unittest test_agent_archive.py -v
"""
import io
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

import agent_archive
import agent_config
import agent_db


# Fixed ids so assertions are readable. Fresh DB per test means no collisions.
TASK_ID = 900001
EXEC_ID = 900002
CHAT_ID = 900003


class _ArchiveTestCase(unittest.TestCase):
    """Base fixture: temp central DB + temp PROJECTS_ROOT + helpers."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tmp = self.td.name
        self.central = os.path.join(self.tmp, 'aingel.db')
        self.root = os.path.join(self.tmp, 'projects')
        os.makedirs(self.root, exist_ok=True)

        self._patches = [
            patch.object(agent_db, 'DB_PATH', self.central),
            patch.object(agent_db, 'PROJECTS_ROOT', self.root),
            patch.object(agent_config, 'PROJECTS_ROOT', self.root),
        ]
        for p in self._patches:
            p.start()
        agent_db.init_db()

    def tearDown(self):
        with agent_db._PROJECT_DB_READY_LOCK:
            agent_db._PROJECT_DB_READY.clear()
        for p in reversed(self._patches):
            p.stop()
        self.td.cleanup()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _make_project(self, name='Proj Alpha', slug='proj-alpha'):
        path = os.path.join(self.root, name)
        os.makedirs(path, exist_ok=True)
        project = agent_db.upsert_project(name, slug, path)
        return project, path

    def _seed_registries_and_project_db(self, path, pid):
        """Insert one task/exec/chat into project.db with explicit ids plus the
        matching central registry rows, and write the chat transcript file."""
        pconn = agent_db.get_project_db(path)
        pconn.execute(
            'INSERT INTO tasks (id, project_id, title, status) VALUES (?,?,?,?)',
            (TASK_ID, pid, 'Task One', 'done'))
        pconn.execute(
            'INSERT INTO executions (id, task_id, model, status) VALUES (?,?,?,?)',
            (EXEC_ID, TASK_ID, 'claude-sonnet-4-6', 'done'))
        chat_rel = os.path.join('Artifacts', 'chats', 'Session.chat.md')
        chat_path = os.path.join(path, chat_rel)
        os.makedirs(os.path.dirname(chat_path), exist_ok=True)
        with open(chat_path, 'w') as f:
            f.write('# chat transcript\n')
        pconn.execute(
            'INSERT INTO chats (id, project_id, name, file_path) VALUES (?,?,?,?)',
            (CHAT_ID, pid, 'Session', chat_path))
        pconn.commit()
        pconn.close()

        conn = agent_db.get_db()
        for table, rid in (('task_registry', TASK_ID),
                           ('exec_registry', EXEC_ID),
                           ('chat_registry', CHAT_ID)):
            conn.execute(
                f'INSERT INTO {table} (id, project_id, project_path) VALUES (?,?,?)',
                (rid, pid, path))
        conn.commit()
        conn.close()

    def _write_extra_files(self, path):
        for rel in ('Working Documents/notes.txt',
                    'Artifacts/project.memory.md',
                    '.git/config',
                    '.uploads/junk/chunk'):
            full = os.path.join(path, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'w') as f:
                f.write('payload: ' + rel + '\n')

    def _read_manifest(self, zip_path):
        with zipfile.ZipFile(zip_path) as zf:
            return json.loads(zf.read('manifest.json').decode('utf-8'))

    @staticmethod
    def _zip_names(zip_path):
        with zipfile.ZipFile(zip_path) as zf:
            return zf.namelist()

    def _valid_manifest(self, pid=910001, name='Evil', slug='evil'):
        return {
            'format': agent_archive.ARCHIVE_FORMAT,
            'version': agent_archive.ARCHIVE_VERSION,
            'project': {'id': pid, 'name': name, 'slug': slug},
        }

    def _write_zip(self, zip_path, manifest=..., entries=(), raw_manifest=None):
        """Build a hand-crafted archive. ``manifest=...`` means include a valid
        manifest; pass None to omit it, or ``raw_manifest`` for arbitrary bytes."""
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            if manifest is not ...:
                if raw_manifest is not None:
                    zf.writestr('manifest.json', raw_manifest)
                else:
                    zf.writestr('manifest.json', json.dumps(manifest))
            for entry in entries:
                if isinstance(entry, zipfile.ZipInfo):
                    zf.writestr(entry, b'')
                else:
                    arcname, content = entry
                    zf.writestr(arcname, content)

    def _remake_archive(self, src_zip, dst_zip, manifest=None, replacements=None):
        """Copy `src_zip` to `dst_zip`, optionally replacing the manifest with a
        new dict and/or raw entry bytes (dict arcname -> bytes)."""
        with zipfile.ZipFile(src_zip) as zf:
            items = [(i, zf.read(i)) for i in zf.infolist()]
        with zipfile.ZipFile(dst_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
            for info, data in items:
                if replacements and info.filename in replacements:
                    data = replacements[info.filename]
                if manifest is not None and info.filename == 'manifest.json':
                    data = json.dumps(manifest).encode('utf-8')
                zf.writestr(info, data)
        return dst_zip

    def _add_dependency(self, task_id, depends_on_id, spec_keys=None):
        conn = agent_db.get_db()
        conn.execute(
            'INSERT OR IGNORE INTO task_dependencies (task_id, depends_on_id, spec_keys) '
            'VALUES (?,?,?)',
            (task_id, depends_on_id, json.dumps(spec_keys or [])))
        conn.commit()
        conn.close()

    def _add_task_registry(self, rid, pid, path):
        conn = agent_db.get_db()
        conn.execute(
            'INSERT INTO task_registry (id, project_id, project_path) VALUES (?,?,?)',
            (rid, pid, path))
        conn.commit()
        conn.close()


class FullRoundTripTests(_ArchiveTestCase):
    def test_round_trip_preserves_ids_rows_and_paths(self):
        project, path = self._make_project()
        pid = project['id']
        self._seed_registries_and_project_db(path, pid)
        self._write_extra_files(path)

        res = agent_archive.build_archive(agent_db.get_project(pid))
        self.assertTrue(os.path.exists(res['path']))
        self.assertTrue(res['filename'].endswith('.aingel.zip'))
        self.assertGreater(res['size'], 0)

        manifest = self._read_manifest(res['path'])
        self.assertEqual(manifest['format'], 'aingel-project-archive')
        self.assertEqual(manifest['version'], 1)
        names = self._zip_names(res['path'])
        for expected in ('project/Working Documents/notes.txt',
                         'project/Artifacts/project.memory.md',
                         'project/.git/config',
                         'project/project.db'):
            self.assertIn(expected, names)
        self.assertFalse(any('.uploads' in n for n in names),
                         f'.uploads leaked into archive: {names}')

        # Delete exactly like the delete route: folder gone, central rows gone.
        shutil.rmtree(path)
        agent_db.delete_project(pid)
        self.assertIsNone(agent_db.get_project(pid))

        restored = agent_archive.restore_archive(res['path'], uploader_id=None)
        target = os.path.join(self.root, 'Proj Alpha')
        self.assertEqual(restored['id'], pid)
        self.assertEqual(restored['path'], target)

        # Registries re-inserted with the ORIGINAL global ids, repointed.
        conn = agent_db.get_db()
        try:
            for table, rid in (('task_registry', TASK_ID),
                               ('exec_registry', EXEC_ID),
                               ('chat_registry', CHAT_ID)):
                row = conn.execute(
                    f'SELECT project_id, project_path FROM {table} WHERE id=?',
                    (rid,)).fetchone()
                self.assertIsNotNone(row, f'{table} row {rid} missing')
                self.assertEqual(row['project_id'], pid)
                self.assertEqual(row['project_path'], target)
        finally:
            conn.close()

        # Restored per-project rows are present with original ids.
        rconn = sqlite3.connect(os.path.join(target, 'project.db'))
        rconn.row_factory = sqlite3.Row
        try:
            self.assertIsNotNone(rconn.execute(
                'SELECT 1 FROM tasks WHERE id=?', (TASK_ID,)).fetchone())
            self.assertIsNotNone(rconn.execute(
                'SELECT 1 FROM executions WHERE id=?', (EXEC_ID,)).fetchone())
            chat = rconn.execute(
                'SELECT file_path FROM chats WHERE id=?', (CHAT_ID,)).fetchone()
            self.assertIsNotNone(chat)
            self.assertEqual(
                chat['file_path'],
                os.path.join(target, 'Artifacts', 'chats', 'Session.chat.md'))
        finally:
            rconn.close()

        # Ownership + SCW session must be session-less after restore.
        self.assertIsNone(restored.get('owner_id'))
        for col, val in restored.items():
            if col.startswith('scw_'):
                self.assertIsNone(val, f'{col} should be cleared, got {val!r}')
        self.assertTrue(os.path.exists(
            os.path.join(target, 'Working Documents', 'notes.txt')))


class WalSnapshotTests(_ArchiveTestCase):
    def test_wal_snapshot_includes_uncheckpointed_commits(self):
        project, path = self._make_project(name='Wal Proj', slug='wal-proj')
        pid = project['id']

        # Keep the connection OPEN so committed pages stay in the -wal sidecar;
        # closing it would checkpoint them into the main file and the test would
        # no longer prove the online-backup path reads WAL.
        pconn = agent_db.get_project_db(path)
        pconn.execute(
            'INSERT INTO tasks (id, project_id, title) VALUES (?,?,?)',
            (TASK_ID, pid, 'wal-task'))
        pconn.commit()
        self.assertTrue(os.path.exists(os.path.join(path, 'project.db-wal')))

        res = agent_archive.build_archive(agent_db.get_project(pid))
        pconn.close()

        with zipfile.ZipFile(res['path']) as zf:
            db_bytes = zf.read('project/project.db')
        extracted = os.path.join(self.tmp, 'extracted-project.db')
        with open(extracted, 'wb') as f:
            f.write(db_bytes)
        conn = sqlite3.connect(extracted)
        try:
            row = conn.execute(
                'SELECT title FROM tasks WHERE id=?', (TASK_ID,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, 'uncheckpointed WAL commit missing from snapshot')
        self.assertEqual(row[0], 'wal-task')


class ScwColumnsClearedTests(_ArchiveTestCase):
    def test_scw_columns_cleared_in_manifest_and_after_restore(self):
        import agent_scw_session
        project, path = self._make_project(name='Scw Proj', slug='scw-proj')
        pid = project['id']
        conn = agent_db.get_db()
        conn.execute(
            "UPDATE projects SET scw_session_enabled=1, scw_project_id='p-1', "
            "scw_session_bucket='bucket-1', scw_kms_key_id='kms-1', "
            "scw_session_created_at='2026-01-01T00:00:00Z' WHERE id=?", (pid,))
        conn.commit()
        conn.close()

        # build_archive would otherwise try a real bucket sync when the flag is
        # set; stub it so the test stays offline.
        with patch.object(agent_scw_session, 'sync_bucket_to_working_docs',
                          return_value={'ok': True}):
            res = agent_archive.build_archive(agent_db.get_project(pid))

        manifest = self._read_manifest(res['path'])
        scw_cols = [c for c in manifest['project'] if c.startswith('scw_')]
        self.assertTrue(scw_cols, 'expected scw_* columns on the project row')
        for col in scw_cols:
            self.assertIsNone(manifest['project'][col],
                              f'manifest {col} must be None')

        shutil.rmtree(path)
        agent_db.delete_project(pid)
        restored = agent_archive.restore_archive(res['path'], uploader_id=None)
        for col, val in restored.items():
            if col.startswith('scw_'):
                self.assertIsNone(val, f'restored {col} must be None')


class ValidationRejectionTests(_ArchiveTestCase):
    def _assert_rejected(self, zip_path, name='Evil'):
        with self.assertRaises(agent_archive.ArchiveValidationError):
            agent_archive.restore_archive(zip_path, uploader_id=None)
        self.assertFalse(os.path.exists(os.path.join(self.root, name)),
                         'nothing may be created at the target on rejection')

    def _zip(self, label):
        return os.path.join(self.tmp, f'{label}.zip')

    def test_wrong_format_rejected(self):
        m = self._valid_manifest()
        m['format'] = 'not-aingel'
        p = self._zip('wrong-format')
        self._write_zip(p, manifest=m)
        self._assert_rejected(p)

    def test_newer_version_rejected(self):
        m = self._valid_manifest()
        m['version'] = 2
        p = self._zip('newer-version')
        self._write_zip(p, manifest=m)
        self._assert_rejected(p)

    def test_missing_manifest_rejected(self):
        p = self._zip('missing-manifest')
        self._write_zip(p, manifest=None, entries=[('project/f.txt', b'x')])
        self._assert_rejected(p)

    def test_non_json_manifest_rejected(self):
        p = self._zip('bad-json')
        self._write_zip(p, manifest=None, raw_manifest=b'this is not json')
        self._assert_rejected(p)

    def test_zip_slip_dotdot_rejected(self):
        p = self._zip('dotdot')
        self._write_zip(p, manifest=self._valid_manifest(),
                        entries=[('project/../evil', b'x')])
        self._assert_rejected(p)

    def test_absolute_path_rejected(self):
        p = self._zip('absolute')
        self._write_zip(p, manifest=self._valid_manifest(),
                        entries=[('/tmp/evil', b'x')])
        self._assert_rejected(p)

    def test_symlink_entry_rejected(self):
        info = zipfile.ZipInfo('project/link')
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        p = self._zip('symlink')
        self._write_zip(p, manifest=self._valid_manifest(), entries=[info])
        self._assert_rejected(p)

    def test_entry_outside_project_rejected(self):
        p = self._zip('outside')
        self._write_zip(p, manifest=self._valid_manifest(),
                        entries=[('other/file', b'x')])
        self._assert_rejected(p)


class CollisionRefusalTests(_ArchiveTestCase):
    def test_second_restore_from_same_zip_is_refused(self):
        project, path = self._make_project()
        pid = project['id']
        res = agent_archive.build_archive(agent_db.get_project(pid))

        # Round-trip once.
        shutil.rmtree(path)
        agent_db.delete_project(pid)
        restored = agent_archive.restore_archive(res['path'], uploader_id=None)
        self.assertEqual(restored['id'], pid)

        # A second restore of the same zip must collide (id + slug + name).
        with self.assertRaises(agent_db.ArchiveConflictError):
            agent_archive.restore_archive(res['path'], uploader_id=None)

        projects = agent_db.get_projects()
        matching = [p for p in projects if p['id'] == pid]
        self.assertEqual(len(matching), 1, 'no partial/duplicate project row')
        self.assertEqual(len(projects), 1)

    def test_name_slug_folder_conflict_when_original_remains(self):
        project, path = self._make_project()
        pid = project['id']
        res = agent_archive.build_archive(agent_db.get_project(pid))

        # Original project, folder and registry ids all still present.
        with self.assertRaises(agent_db.ArchiveConflictError):
            agent_archive.restore_archive(res['path'], uploader_id=None)

        projects = agent_db.get_projects()
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]['id'], pid)
        self.assertTrue(os.path.isdir(path))


class ReapArchivesTests(_ArchiveTestCase):
    def test_reaps_old_and_keeps_fresh(self):
        adir = os.path.join(self.tmp, 'archives')
        os.makedirs(adir)
        old = os.path.join(adir, 'old.aingel.zip')
        fresh = os.path.join(adir, 'fresh.aingel.zip')
        for f in (old, fresh):
            with open(f, 'wb') as fh:
                fh.write(b'PK\x03\x04')
        eight_days = time.time() - (8 * 86400)
        os.utime(old, (eight_days, eight_days))

        reaped = agent_archive.reap_archives(adir, 7)
        self.assertEqual(reaped, 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))


class ExportAndCollisionUnitTests(_ArchiveTestCase):
    def test_export_shape_then_collision_detection(self):
        project, path = self._make_project(name='Export Proj', slug='export-proj')
        pid = project['id']
        self._seed_registries_and_project_db(path, pid)

        export = agent_db.export_project_central_rows(pid)
        self.assertEqual(
            set(export.keys()),
            {'project', 'roles', 'project_hf_models', 'project_members',
             'task_dependencies', 'registries', 'scw_deployments',
             'scw_deployment_calls', 'scw_session_costs'})
        self.assertEqual(set(export['registries'].keys()),
                         {'tasks', 'execs', 'chats'})

        # Delete the project so every id in the export is free again → clean.
        shutil.rmtree(path)
        agent_db.delete_project(pid)
        self.assertIsNone(agent_db.check_archive_id_collisions(export))

        # Reuse the project id → collision must be reported.
        conn = agent_db.get_db()
        conn.execute(
            'INSERT INTO projects (id, name, slug, path) VALUES (?,?,?,?)',
            (pid, 'Other', 'other-slug', os.path.join(self.root, 'Other')))
        conn.commit()
        conn.close()

        collisions = agent_db.check_archive_id_collisions(export)
        self.assertIsNotNone(collisions)
        # New dict contract: fatal collisions refuse the restore; skippable ones
        # (shared task_dependencies / scw_deployment_calls) do not.
        self.assertTrue(any('project id' in c for c in collisions['fatal']),
                        f'expected project id collision, got {collisions}')
        self.assertEqual(collisions['skippable'], [],
                         f'nothing shared in this scenario: {collisions}')


class RouteLevelTests(_ArchiveTestCase):
    """End-to-end through the real Flask app with auth off."""

    def setUp(self):
        super().setUp()
        self._saved_auth = os.environ.get('AINGEL_AUTH')
        os.environ['AINGEL_AUTH'] = 'off'
        import agent_api
        self.agent_api = agent_api
        self._archives_patch = patch.object(
            agent_api, '_ARCHIVES_DIR', os.path.join(self.root, '.archives'))
        self._archives_patch.start()
        agent_api.app.config['TESTING'] = True
        self.client = agent_api.app.test_client()

    def tearDown(self):
        self._archives_patch.stop()
        if self._saved_auth is None:
            os.environ.pop('AINGEL_AUTH', None)
        else:
            os.environ['AINGEL_AUTH'] = self._saved_auth
        super().tearDown()

    def test_archive_download_import_round_trip(self):
        project, path = self._make_project()
        pid = project['id']
        with open(os.path.join(path, 'notes.txt'), 'w') as f:
            f.write('hello archive\n')

        resp = self.client.post(f'/api/projects/{pid}/archive')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertTrue(body['filename'].endswith('.aingel.zip'))
        self.assertIn('download_url', body)

        dl = self.client.get(body['download_url'])
        self.assertEqual(dl.status_code, 200)
        zip_bytes = dl.data
        self.assertTrue(zip_bytes.startswith(b'PK'))

        shutil.rmtree(path)
        agent_db.delete_project(pid)

        imp = self.client.post(
            '/api/projects/import',
            data={'file': (io.BytesIO(zip_bytes), body['filename'])},
            content_type='multipart/form-data')
        self.assertEqual(imp.status_code, 200, imp.get_data(as_text=True))
        self.assertEqual(imp.get_json()['project']['id'], pid)


class PathTraversalNameTests(_ArchiveTestCase):
    """A crafted manifest project.name must never escape PROJECTS_ROOT."""

    def _built_zip(self):
        project, path = self._make_project(name='Safe Proj', slug='safe-proj')
        pid = project['id']
        res = agent_archive.build_archive(agent_db.get_project(pid))
        # Remove the original so only the crafted restore could (wrongly) write.
        shutil.rmtree(path)
        agent_db.delete_project(pid)
        return res

    def _evil_zip(self, label, evil_name):
        src = self._built_zip()
        manifest = self._read_manifest(src['path'])
        manifest['project']['name'] = evil_name
        # Keep the slug valid so name validation is what rejects.
        manifest['project']['slug'] = 'evil-slug'
        dst = os.path.join(self.tmp, f'traversal-{label}.zip')
        return self._remake_archive(src['path'], dst, manifest=manifest)

    def _assert_traversal_rejected(self, label, evil_name, escape_target):
        p = self._evil_zip(label, evil_name)
        with self.assertRaises(agent_archive.ArchiveValidationError):
            agent_archive.restore_archive(p, uploader_id=None)
        self.assertFalse(os.path.exists(escape_target),
                         f'{evil_name!r} escaped to {escape_target}')

    def test_relative_dotdot_name_rejected(self):
        escape = os.path.abspath(os.path.join(self.root, '..', 'evil'))
        self._assert_traversal_rejected('dotdot', '../evil', escape)

    def test_absolute_path_name_rejected(self):
        escape = os.path.join(self.tmp, 'aingel-evil')
        self._assert_traversal_rejected(
            'absolute', escape, escape)

    def test_mixed_separators_name_rejected(self):
        escape = os.path.abspath(os.path.join(self.root, 'bad', '..', '..', 'name'))
        self._assert_traversal_rejected('mixed', 'bad/../../name', escape)

    def test_nul_byte_name_rejected(self):
        p = self._evil_zip('nul', 'bad\x00name')
        with self.assertRaises(agent_archive.ArchiveValidationError):
            agent_archive.restore_archive(p, uploader_id=None)


class IdorAndDownloadHardeningTests(_ArchiveTestCase):
    """GET /archive/<filename> is scoped to the requested pid and hardened."""

    def setUp(self):
        super().setUp()
        self._saved_auth = os.environ.get('AINGEL_AUTH')
        os.environ['AINGEL_AUTH'] = 'off'
        import agent_api
        self.agent_api = agent_api
        self._archives_patch = patch.object(
            agent_api, '_ARCHIVES_DIR', os.path.join(self.root, '.archives'))
        self._archives_patch.start()
        agent_api.app.config['TESTING'] = True
        self.client = agent_api.app.test_client()

    def tearDown(self):
        self._archives_patch.stop()
        if self._saved_auth is None:
            os.environ.pop('AINGEL_AUTH', None)
        else:
            os.environ['AINGEL_AUTH'] = self._saved_auth
        super().tearDown()

    def _project_with_archive(self, name, slug):
        project, path = self._make_project(name=name, slug=slug)
        pid = project['id']
        res = agent_archive.build_archive(agent_db.get_project(pid))
        return pid, res

    def test_cross_pid_download_is_404_and_own_is_200(self):
        pid_a, res_a = self._project_with_archive('Idor A', 'idor-a')
        pid_b, _ = self._project_with_archive('Idor B', 'idor-b')

        cross = self.client.get(
            f"/api/projects/{pid_b}/archive/{res_a['filename']}")
        self.assertEqual(cross.status_code, 404, 'cross-pid download must be 404')

        own = self.client.get(
            f"/api/projects/{pid_a}/archive/{res_a['filename']}")
        self.assertEqual(own.status_code, 200)
        self.assertTrue(own.data.startswith(b'PK'))

    def test_download_filename_hardening(self):
        pid, res = self._project_with_archive('Harden', 'harden')
        bad = ['..%2Fetc%2Fpasswd', 'etc/passwd', 'evil%00name.zip',
               'sub/evil.aingel.zip']
        for raw in bad:
            resp = self.client.get(f'/api/projects/{pid}/archive/{raw}')
            self.assertEqual(resp.status_code, 404,
                             f'{raw!r} should 404, got {resp.status_code}')

    def test_archive_post_reports_import_limit(self):
        project, _ = self._make_project(name='Limit Proj', slug='limit-proj')
        pid = project['id']
        resp = self.client.post(f'/api/projects/{pid}/archive')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertIn('import_limit', body)
        self.assertEqual(body['import_limit'], self.agent_api._MAX_IMPORT_BYTES)


class ManifestAndEntryGuardTests(_ArchiveTestCase):
    def _target(self, name='Evil'):
        return os.path.join(self.root, name)

    def test_oversized_manifest_rejected(self):
        p = os.path.join(self.tmp, 'oversized.zip')
        big = json.dumps(self._valid_manifest())
        # Inflate well past the 1 MiB manifest cap.
        big = big.replace('}', ', "pad": "' + ('A' * (2 * 1024 * 1024)) + '"}')
        self._write_zip(p, manifest=None, raw_manifest=big.encode('utf-8'))
        with self.assertRaises(agent_archive.ArchiveValidationError):
            agent_archive.restore_archive(p, uploader_id=None)
        self.assertFalse(os.path.exists(self._target()))

    def test_tree_conflict_zip_is_400(self):
        p = os.path.join(self.tmp, 'tree-conflict.zip')
        self._write_zip(p, manifest=self._valid_manifest(),
                        entries=[('project/x', b'file'),
                                 ('project/x/y', b'nested')])
        with self.assertRaises(agent_archive.ArchiveValidationError):
            agent_archive.restore_archive(p, uploader_id=None)
        self.assertFalse(os.path.exists(self._target()))

    def test_corrupt_project_db_rejected_without_orphan(self):
        project, path = self._make_project(name='Corrupt Proj', slug='corrupt-proj')
        pid = project['id']
        self._seed_registries_and_project_db(path, pid)
        res = agent_archive.build_archive(agent_db.get_project(pid))

        bad = os.path.join(self.tmp, 'corrupt.zip')
        self._remake_archive(res['path'], bad,
                             replacements={'project/project.db': b'\x00' * 4096})

        shutil.rmtree(path)
        agent_db.delete_project(pid)
        self.assertIsNone(agent_db.get_project(pid))

        with self.assertRaises(agent_archive.ArchiveValidationError):
            agent_archive.restore_archive(bad, uploader_id=None)

        self.assertIsNone(agent_db.get_project(pid),
                          'corrupt restore must not commit a central row')
        self.assertFalse(os.path.exists(self._target('Corrupt Proj')),
                         'corrupt restore must not leave a project folder')


class BuildUniquenessAndSpecialFileTests(_ArchiveTestCase):
    def test_same_second_builds_have_distinct_filenames(self):
        project, path = self._make_project(name='Uniq Proj', slug='uniq-proj')
        pid = project['id']
        r1 = agent_archive.build_archive(agent_db.get_project(pid))
        r2 = agent_archive.build_archive(agent_db.get_project(pid))
        self.assertNotEqual(r1['filename'], r2['filename'])
        self.assertTrue(os.path.exists(r1['path']))
        self.assertTrue(os.path.exists(r2['path']))
        self.assertEqual(
            os.path.dirname(r1['path']),
            agent_archive.project_archives_dir(pid))

    def test_fifo_is_skipped(self):
        if not hasattr(os, 'mkfifo'):
            self.skipTest('os.mkfifo unavailable')
        project, path = self._make_project(name='Fifo Proj', slug='fifo-proj')
        pid = project['id']
        fifo = os.path.join(path, 'pipe.fifo')
        try:
            os.mkfifo(fifo)
        except OSError as e:
            self.skipTest(f'cannot create FIFO: {e}')

        res = agent_archive.build_archive(agent_db.get_project(pid))
        names = self._zip_names(res['path'])
        self.assertFalse(any(n.endswith('pipe.fifo') for n in names),
                         f'FIFO leaked into archive: {names}')
        manifest = self._read_manifest(res['path'])
        self.assertIn('pipe.fifo', manifest.get('excluded_specials', []))

    def test_project_db_side_files_are_excluded(self):
        """Every project.db.* side file is scratch: the live project.db is
        replaced by the WAL-safe snapshot, so backups and legacy pre-merge
        snapshots must not ride along. Regression for the .premerge-*.bak
        pattern that the old `project.db.bak-` prefix check missed."""
        project, path = self._make_project(name='Side Proj', slug='side-proj')
        pid = project['id']
        # Materialise the live project.db so the WAL-safe snapshot has a source.
        agent_db.get_project_db(path).close()
        for side in ('project.db.bak-20260101',
                     'project.db.premerge-20260813102459.bak'):
            with open(os.path.join(path, side), 'w') as f:
                f.write('stale backup\n')

        res = agent_archive.build_archive(agent_db.get_project(pid))
        names = self._zip_names(res['path'])
        leaked = [n for n in names if os.path.basename(n).startswith('project.db.')
                  and os.path.basename(n) != 'project.db']
        self.assertEqual(leaked, [], f'project.db.* side files leaked: {leaked}')
        # The WAL-safe snapshot itself is still present.
        self.assertIn('project/project.db', names)


class SharedDependencyRoundTripTests(_ArchiveTestCase):
    def test_shared_dependency_restores_in_both_projects(self):
        pid_a = 930001
        pid_b = 930002
        task_a = 931001
        task_b = 931002

        path_a = os.path.join(self.root, 'Dep A')
        path_b = os.path.join(self.root, 'Dep B')
        os.makedirs(path_a, exist_ok=True)
        os.makedirs(path_b, exist_ok=True)
        conn = agent_db.get_db()
        conn.execute('INSERT INTO projects (id, name, slug, path) VALUES (?,?,?,?)',
                     (pid_a, 'Dep A', 'dep-a', path_a))
        conn.execute('INSERT INTO projects (id, name, slug, path) VALUES (?,?,?,?)',
                     (pid_b, 'Dep B', 'dep-b', path_b))
        conn.execute('INSERT INTO task_registry (id, project_id, project_path) '
                     'VALUES (?,?,?)', (task_a, pid_a, path_a))
        conn.execute('INSERT INTO task_registry (id, project_id, project_path) '
                     'VALUES (?,?,?)', (task_b, pid_b, path_b))
        conn.commit()
        conn.close()
        # A depends-on B: OR-scoped export puts this row in BOTH archives.
        self._add_dependency(task_a, task_b)

        # project.db files must exist for build/restore (schema init is lazy).
        for p in (path_a, path_b):
            pc = agent_db.get_project_db(p)
            pc.commit()
            pc.close()

        res_a = agent_archive.build_archive(agent_db.get_project(pid_a))
        res_b = agent_archive.build_archive(agent_db.get_project(pid_b))
        manifest_a = self._read_manifest(res_a['path'])
        manifest_b = self._read_manifest(res_b['path'])
        self.assertTrue(manifest_a['task_dependencies'], 'A should carry the dep')
        self.assertTrue(manifest_b['task_dependencies'], 'B should carry the dep')

        shutil.rmtree(path_a)
        shutil.rmtree(path_b)
        agent_db.delete_project(pid_a)
        agent_db.delete_project(pid_b)

        # Both must restore without a 409 despite sharing the dependency id.
        restored_a = agent_archive.restore_archive(res_a['path'], uploader_id=None)
        restored_b = agent_archive.restore_archive(res_b['path'], uploader_id=None)
        self.assertEqual(restored_a['id'], pid_a)
        self.assertEqual(restored_b['id'], pid_b)

        conn = agent_db.get_db()
        try:
            rows = conn.execute(
                'SELECT COUNT(*) FROM task_dependencies WHERE task_id=? AND depends_on_id=?',
                (task_a, task_b)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(rows, 1, 'shared dependency must survive exactly once')


class SkippableCollisionUnitTests(_ArchiveTestCase):
    def test_shared_dependency_id_is_skippable_not_fatal(self):
        self._add_task_registry(940001, 940000, os.path.join(self.root, 'X'))
        self._add_dependency(940002, 940003)
        conn = agent_db.get_db()
        dep_id = conn.execute(
            'SELECT id FROM task_dependencies WHERE task_id=940002').fetchone()[0]
        conn.close()

        manifest = {
            'project': {'id': 999999, 'name': 'Free Name', 'slug': 'free-name'},
            'task_dependencies': [{'id': dep_id, 'task_id': 940002,
                                   'depends_on_id': 940003}],
        }
        collisions = agent_db.check_archive_id_collisions(manifest)
        self.assertIsNotNone(collisions)
        self.assertEqual(collisions['fatal'], [])
        self.assertTrue(
            any('task_dependencies' in c for c in collisions['skippable']),
            f'expected skippable dep collision, got {collisions}')

    def test_clean_manifest_returns_none(self):
        manifest = {'project': {'id': 8888888, 'name': 'Nope',
                                'slug': 'nope-nope'}}
        self.assertIsNone(agent_db.check_archive_id_collisions(manifest))


class ChatRepointAcrossRootsTests(_ArchiveTestCase):
    def test_restore_into_second_root_repoints_chat_path(self):
        project, path = self._make_project(name='Repoint', slug='repoint')
        pid = project['id']
        self._seed_registries_and_project_db(path, pid)
        res = agent_archive.build_archive(agent_db.get_project(pid))

        shutil.rmtree(path)
        agent_db.delete_project(pid)

        second = os.path.join(self.tmp, 'projects-2')
        os.makedirs(second, exist_ok=True)
        with patch.object(agent_config, 'PROJECTS_ROOT', second), \
                patch.object(agent_db, 'PROJECTS_ROOT', second):
            restored = agent_archive.restore_archive(res['path'], uploader_id=None)

        target = os.path.join(second, 'Repoint')
        self.assertEqual(restored['path'], target)
        conn = sqlite3.connect(os.path.join(target, 'project.db'))
        conn.row_factory = sqlite3.Row
        try:
            chat = conn.execute(
                'SELECT file_path FROM chats WHERE id=?', (CHAT_ID,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(chat)
        self.assertTrue(chat['file_path'].startswith(second),
                        f'chat path not repointed to second root: {chat["file_path"]}')


class CrossProjectRowInjectionTests(_ArchiveTestCase):
    """A manifest is attacker-controlled. Child rows carrying another project's
    project_id must be refused (fatal), not inserted into that tenant, even when
    the row id itself is free. Regression for the adversarial-review finding."""

    def _manifest_with_role(self, target_pid):
        return {
            'format': agent_archive.ARCHIVE_FORMAT,
            'version': agent_archive.ARCHIVE_VERSION,
            'project': {'id': 960000, 'name': 'Attacker', 'slug': 'attacker'},
            # New row id (free), but pointed at a victim's project.
            'roles': [{'id': 960101, 'project_id': target_pid,
                       'name': 'x', 'system_prompt': 'EXFIL SECRETS'}],
        }

    def test_foreign_project_id_child_row_is_fatal_collision(self):
        victim, _ = self._make_project(name='Victim', slug='victim')
        collisions = agent_db.check_archive_id_collisions(
            self._manifest_with_role(victim['id']))
        self.assertIsNotNone(collisions)
        self.assertEqual(collisions['skippable'], [])
        self.assertTrue(
            any('project_id' in c for c in collisions['fatal']),
            f'expected a project_id mismatch in fatal collisions, got {collisions}')

    def test_restore_refuses_foreign_project_id_row(self):
        victim, _ = self._make_project(name='Victim', slug='victim')
        zip_path = os.path.join(self.tmp, 'inject.zip')
        self._write_zip(zip_path, manifest=self._manifest_with_role(victim['id']))

        with self.assertRaises(agent_db.ArchiveConflictError):
            agent_archive.restore_archive(zip_path, uploader_id=None)

        # The victim project must not have gained the planted role.
        conn = agent_db.get_db()
        try:
            n = conn.execute(
                'SELECT COUNT(*) FROM roles WHERE project_id=?', (victim['id'],)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0, 'foreign role was planted into the victim project')

    def test_own_project_id_row_is_accepted(self):
        """A legitimate archive (row project_id == archive project id) still
        restores and gets exactly its own role."""
        manifest = {
            'format': agent_archive.ARCHIVE_FORMAT,
            'version': agent_archive.ARCHIVE_VERSION,
            'project': {'id': 970000, 'name': 'Legit', 'slug': 'legit'},
            'roles': [{'id': 970101, 'project_id': 970000, 'name': 'r',
                       'system_prompt': 'ok'}],
        }
        zip_path = os.path.join(self.tmp, 'legit.zip')
        self._write_zip(zip_path, manifest=manifest)
        restored = agent_archive.restore_archive(zip_path, uploader_id=None)
        self.assertEqual(restored['id'], 970000)
        conn = agent_db.get_db()
        try:
            row = conn.execute(
                'SELECT project_id FROM roles WHERE id=970101').fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row['project_id'], 970000)


class PostCommitReturnSafetyTests(_ArchiveTestCase):
    """restore_project_central_rows must never raise AFTER its commit: the
    caller rmtree's the folder when the call raises and central_committed is
    still False, which would orphan durable rows and burn global ids."""

    def test_readback_failure_after_commit_still_returns_row(self):
        manifest = {
            'project': {'id': 990000, 'name': 'Readback',
                        'slug': 'readback'},
        }
        # Force the post-commit project read-back to raise, simulating any
        # failure between commit and return.
        real_get_db = agent_db.get_db

        class _Boom(Exception):
            pass

        class _Conn:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *a, **k):
                # Only the post-commit read-back (`SELECT * FROM projects ...`);
                # the pre-insert existence check is `SELECT 1 FROM projects ...`.
                if 'SELECT * FROM projects WHERE id=?' in sql:
                    raise _Boom('read-back exploded')
                return self._inner.execute(sql, *a, **k)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        with patch.object(agent_db, 'get_db',
                          side_effect=lambda: _Conn(real_get_db())):
            # Must NOT raise — the commit already happened.
            out = agent_db.restore_project_central_rows(
                manifest, os.path.join(self.root, 'Readback'))

        self.assertEqual(out['id'], 990000)


if __name__ == '__main__':
    unittest.main()
