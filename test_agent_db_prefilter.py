"""Tests for the local decision pre-filter DB/API layer.

Covers the storage contract in plan v1.2 §5.3 / §5.7:

* the guarded ALTER adds ``tasks.prefilter_json`` to an existing project.db;
* ``update_task_prefilter`` round-trips, resolves the registry path, does not
  bump ``updated_at``, and emits no SSE / GUIDE.md regeneration;
* ``increment_app_setting`` survives concurrent increments (atomic UPSERT);
* ``list_tasks`` strips the column from the refetched list payload;
* description-change hooks call ``agent_decision.note_description_changed``
  (and tolerate an absent module).

The decision module is created by a parallel implementer, so every touch is a
lazy import and these tests use a stub in ``sys.modules`` where they assert it.
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import agent_db


class _PrefilterDBTestCase(unittest.TestCase):
    """Temp central DB + temp project dir, one task to write against."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self._patch = patch.object(
            agent_db, 'DB_PATH', os.path.join(self.td.name, 'aingel.db'))
        self._patch.start()
        agent_db.init_db()
        self.path = os.path.join(self.td.name, 'proj')
        os.makedirs(self.path, exist_ok=True)
        proj = agent_db.upsert_project('Prefilter', 'prefilter', self.path)
        self.project_id = proj['id']
        self.task_id = agent_db.create_task(
            self.project_id, 'Task', description='A description for the task',
            project_path=self.path)

    def tearDown(self):
        with agent_db._PROJECT_DB_READY_LOCK:
            agent_db._PROJECT_DB_READY.clear()
        self._patch.stop()
        self.td.cleanup()


class MigrationTests(_PrefilterDBTestCase):
    def test_migration_adds_prefilter_json_to_existing_tables(self):
        legacy = os.path.join(self.td.name, 'legacy')
        os.makedirs(legacy, exist_ok=True)
        conn = sqlite3.connect(os.path.join(legacy, 'project.db'))
        conn.executescript('''
            CREATE TABLE tasks (
                id               INTEGER PRIMARY KEY,
                project_id       INTEGER NOT NULL,
                title            TEXT NOT NULL,
                status           TEXT DEFAULT 'pending',
                phase_name       TEXT DEFAULT '',
                archived         INTEGER DEFAULT 0,
                work_session_slot INTEGER DEFAULT NULL
            );
        ''')
        conn.commit()
        conn.close()

        pconn = agent_db.get_project_db(legacy)
        try:
            cols = {r['name'] for r in pconn.execute('PRAGMA table_info(tasks)')}
        finally:
            pconn.close()
        self.assertIn('prefilter_json', cols)

    def test_fresh_schema_has_prefilter_json(self):
        pconn = agent_db.get_project_db(self.path)
        try:
            cols = {r['name'] for r in pconn.execute('PRAGMA table_info(tasks)')}
        finally:
            pconn.close()
        self.assertIn('prefilter_json', cols)


class UpdateTaskPrefilterTests(_PrefilterDBTestCase):
    def test_roundtrip_preserves_updated_at_and_has_no_side_effects(self):
        fixed_updated = '2000-01-01T00:00:00+00:00'
        pconn = agent_db.get_project_db(self.path)
        try:
            pconn.execute('UPDATE tasks SET updated_at=? WHERE id=?',
                          (fixed_updated, self.task_id))
            pconn.commit()
        finally:
            pconn.close()

        payload = {
            'status': 'ok',
            'flags': ['read_scope'],
            'answers': {'P1': 2, 'P4': False},
        }
        # project_path omitted on purpose: the registry must resolve it.
        with patch.object(agent_db, '_emit_safe') as emit, \
                patch('agent_guide_sync.after_task_change') as guide:
            agent_db.update_task_prefilter(self.task_id, payload)

        emit.assert_not_called()
        guide.assert_not_called()

        row = agent_db.get_task(self.task_id, self.path)
        self.assertEqual(json.loads(row['prefilter_json']), payload)
        self.assertEqual(row['updated_at'], fixed_updated)

    def test_swallows_operational_error(self):
        with patch.object(agent_db, 'get_project_db',
                          side_effect=sqlite3.OperationalError('locked')):
            # Must not raise into the worker.
            agent_db.update_task_prefilter(self.task_id, {'status': 'ok'},
                                           project_path=self.path)


class IncrementAppSettingTests(_PrefilterDBTestCase):
    def test_concurrent_increments_are_not_lost(self):
        errors = []
        per_thread = 50

        def worker():
            try:
                for _ in range(per_thread):
                    agent_db.increment_app_setting('prefilter:calls', 1)
            except Exception as e:  # pragma: no cover - failure detail
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertFalse(errors, errors)
        value = agent_db.get_app_settings().get('prefilter:calls')
        self.assertEqual(int(value), 10 * per_thread)

    def test_increment_creates_and_accrues(self):
        agent_db.increment_app_setting('prefilter:errors', 2)
        agent_db.increment_app_setting('prefilter:errors', 3)
        self.assertEqual(
            int(agent_db.get_app_settings()['prefilter:errors']), 5)


def _decision_stub(calls):
    stub = types.ModuleType('agent_decision')

    def note_description_changed(task_id, project_path):
        calls.append((task_id, project_path))

    stub.note_description_changed = note_description_changed
    return stub


class DescriptionHookTests(_PrefilterDBTestCase):
    def test_update_task_calls_hook_only_for_description(self):
        calls = []
        with patch.dict(sys.modules, {'agent_decision': _decision_stub(calls)}):
            agent_db.update_task(self.task_id, project_path=self.path,
                                 title='Renamed')
            self.assertEqual(calls, [])
            agent_db.update_task(self.task_id, project_path=self.path,
                                 description='A changed description')
        self.assertEqual(calls, [(self.task_id, self.path)])

    def test_create_task_calls_hook(self):
        calls = []
        with patch.dict(sys.modules, {'agent_decision': _decision_stub(calls)}):
            tid = agent_db.create_task(
                self.project_id, 'Another', description='New description',
                project_path=self.path)
        self.assertEqual(calls, [(tid, self.path)])

    def test_hook_tolerates_missing_module(self):
        # None in sys.modules makes `import agent_decision` raise ImportError;
        # the DB write must still succeed.
        with patch.dict(sys.modules, {'agent_decision': None}):
            agent_db.update_task(self.task_id, project_path=self.path,
                                 description='Still written')
        row = agent_db.get_task(self.task_id, self.path)
        self.assertEqual(row['description'], 'Still written')


class ListTasksStripTests(_PrefilterDBTestCase):
    def setUp(self):
        super().setUp()
        self._saved_auth = os.environ.get('AINGEL_AUTH')
        os.environ['AINGEL_AUTH'] = 'off'
        import agent_api
        self.api = agent_api
        self.api.app.config['TESTING'] = True
        self.client = self.api.app.test_client()

    def tearDown(self):
        if self._saved_auth is None:
            os.environ.pop('AINGEL_AUTH', None)
        else:
            os.environ['AINGEL_AUTH'] = self._saved_auth
        super().tearDown()

    def test_list_tasks_does_not_leak_prefilter_json(self):
        task = agent_db.get_task(self.task_id, self.path)
        task['prefilter_json'] = json.dumps({'status': 'ok'})
        with patch.object(agent_db, 'get_tasks', return_value=[task]):
            resp = self.client.get('/api/tasks')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        rows = resp.get_json()
        self.assertTrue(rows)
        self.assertNotIn('prefilter_json', rows[0])


if __name__ == '__main__':
    unittest.main()
