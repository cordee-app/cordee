"""Regression tests for atomic project creation with Scaleway sessions.

``POST /api/projects`` may provision a Scaleway session (``scw_session: true``).
Provisioning happens after the folder/row/owner exist but *before* the
scaffolding chat, the progress queue and the scaffolding thread, so a failure
can undo the whole project (502 + no row + no folder). These tests pin:

* the no-session path is unchanged (201, thread started, no ``session`` key);
* ``{'ok': False}`` and a raised exception both roll back atomically, write a
  central DENY audit line and never start scaffolding;
* success persists the ``scw_*`` fields, rewrites ``aingel.json`` and returns a
  secret-free ``session`` object;
* the ``IS_VAULT``/``eu_only`` guard still refuses before anything is created;
* a persistence failure shreds the live Scaleway resources before rolling back.

Isolation follows ``test_agent_archive.py``: fresh temp dir per test, central
DB path patched to ``<tmp>/aingel.db``, ``PROJECTS_ROOT`` patched on
``agent_db``, ``agent_config`` **and** ``agent_api`` (the last one copies the
value at import), auth forced off before ``agent_api`` is imported, and the
per-process project-DB schema cache cleared between tests.

Run:  venv/bin/python3 -m unittest test_agent_api_projects.py -v
"""
import contextlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import agent_config
import agent_db
import agent_scw_session


SESSION_OK = {
    'ok': True,
    'scw_project_id': 'p-1',
    'scw_session_bucket': 'aingel-x',
    'scw_kms_key_id': 'k-1',
    'scw_session_region': 'fr-par',
    'scw_session_created_at': '2026-10-21T00:00:00Z',
    # create_session also returns the IAM key pair; the endpoint must never
    # echo or persist it.
    'scw_access_key': 'SCWACCESSKEYSECRET',
    'scw_secret_key': 'scw-super-secret',
}


def payload(**over):
    """A valid create-project body (EU-only scaffolding model by default)."""
    data = {
        'name': 'Atomic Project',
        'pitch': 'Ship the atomic project-creation flow.',
        'eu_only': True,
    }
    data.update(over)
    return data


class _ProjectCreateTestCase(unittest.TestCase):
    """Temp central DB + temp PROJECTS_ROOT + auth off + Flask test client."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tmp = self.td.name
        self.central = os.path.join(self.tmp, 'aingel.db')
        self.root = os.path.join(self.tmp, 'projects')
        os.makedirs(self.root, exist_ok=True)

        # The vault .env sets AINGEL_AUTH=oidc; agent_config loads .env at
        # import, so force the mode off before touching agent_api.
        self._saved_auth = os.environ.get('AINGEL_AUTH')
        os.environ['AINGEL_AUTH'] = 'off'

        self._patches = [
            patch.object(agent_db, 'DB_PATH', self.central),
            patch.object(agent_db, 'PROJECTS_ROOT', self.root),
            patch.object(agent_config, 'PROJECTS_ROOT', self.root),
        ]
        for p in self._patches:
            p.start()
        agent_db.init_db()

        import agent_api
        self.agent_api = agent_api
        # agent_api imported PROJECTS_ROOT by value: without this patch the
        # request would scaffold into this host's live projects root.
        self._api_patches = [
            patch.object(agent_api, 'PROJECTS_ROOT', self.root),
        ]
        for p in self._api_patches:
            p.start()

        self._queues_before = dict(agent_api._scaffold_progress_queues)
        agent_api.app.config['TESTING'] = True
        self.client = agent_api.app.test_client()

    def tearDown(self):
        # Restore the module-global progress queues this test may have touched.
        with self.agent_api._scaffold_progress_lock:
            self.agent_api._scaffold_progress_queues.clear()
            self.agent_api._scaffold_progress_queues.update(self._queues_before)
        for p in reversed(self._api_patches):
            p.stop()
        with agent_db._PROJECT_DB_READY_LOCK:
            agent_db._PROJECT_DB_READY.clear()
        for p in reversed(self._patches):
            p.stop()
        if self._saved_auth is None:
            os.environ.pop('AINGEL_AUTH', None)
        else:
            os.environ['AINGEL_AUTH'] = self._saved_auth
        self.td.cleanup()

    # ── helpers ──────────────────────────────────────────────────────────────

    @contextlib.contextmanager
    def _request_mocks(self):
        """Neutralise the scaffolding thread and the model call behind it."""
        with patch('threading.Thread') as thread_cls, \
                patch.object(self.agent_api, 'route',
                             return_value=('draft', 1, 2, 0.0)):
            yield thread_cls

    @contextlib.contextmanager
    def _capture_project(self):
        """Spy on upsert_project so a rolled-back run's pid is still known."""
        real = agent_db.upsert_project
        seen = {}

        def _spy(*args, **kwargs):
            row = real(*args, **kwargs)
            seen['project'] = row
            return row

        with patch.object(agent_db, 'upsert_project', side_effect=_spy):
            yield seen

    def _folder(self, name):
        return os.path.join(self.root, name)

    def _assert_rolled_back(self, resp, seen, name, slug):
        body = resp.get_json()
        self.assertEqual(resp.status_code, 502, resp.get_data(as_text=True))
        self.assertIn('Scaleway session provisioning failed', body['error'])
        pid = seen['project']['id']
        self.assertIsNone(agent_db.get_project(pid), 'central row must be gone')
        self.assertEqual(
            [p for p in agent_db.get_projects() if p['slug'] == slug], [],
            'rolled-back project must not be listed')
        self.assertFalse(os.path.exists(self._folder(name)),
                         'project folder must be removed')
        return pid


class CreateProjectRouteTests(_ProjectCreateTestCase):
    def test_create_without_session_still_works(self):
        data = payload(name='Atomic No Session')
        with self._capture_project() as seen, self._request_mocks() as thread_cls:
            resp = self.client.post('/api/projects', json=data)

        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertNotIn('session', body,
                         'no session block when scw_session is not requested')
        pid = seen['project']['id']
        self.assertEqual(body['project']['id'], pid)

        row = agent_db.get_project(pid)
        self.assertIsNotNone(row)
        self.assertEqual(row['slug'], 'atomic-no-session')
        self.assertFalse(row.get('scw_session_enabled'),
                         'a session-less create must leave the flag off')
        self.assertTrue(os.path.isdir(self._folder('Atomic No Session')))

        # The scaffolding thread is the only Thread spawned by this route.
        self.assertEqual(thread_cls.call_count, 1)
        thread_cls.return_value.start.assert_called_once()
        self.assertEqual(thread_cls.call_args.kwargs.get('target').__name__,
                         '_do_scaffold')

    def test_session_failure_rolls_back(self):
        err = ('project create failed: Scaleway rejected the request: '
               'bad name (HTTP 400)')
        name, slug = 'Atomic Fail', 'atomic-fail'
        with self._capture_project() as seen, \
                patch.object(agent_scw_session, 'create_session',
                             return_value={'ok': False, 'error': err}), \
                patch.object(agent_config, 'eu_audit') as audit, \
                patch.object(agent_db, 'create_chat',
                             wraps=agent_db.create_chat) as chat_mock, \
                self._request_mocks() as thread_cls:
            resp = self.client.post(
                '/api/projects', json=payload(name=name, scw_session=True))

        pid = self._assert_rolled_back(resp, seen, name, slug)
        self.assertEqual(resp.get_json()['error'],
                         f'Scaleway session provisioning failed: {err}')
        chat_mock.assert_not_called()
        self.assertEqual(agent_db.get_chats(project_id=pid), [],
                         'no chat row may survive the rollback')
        self.assertNotIn(pid, self.agent_api._scaffold_progress_queues)
        thread_cls.assert_not_called()

        audit.assert_called()
        self.assertTrue(
            any(c.kwargs.get('allowed') is False
                and c.kwargs.get('caller') == 'create_project'
                for c in audit.call_args_list),
            f'expected a DENY create_project audit line, got {audit.call_args_list}')

    def test_session_exception_rolls_back(self):
        name, slug = 'Atomic Boom', 'atomic-boom'
        with self._capture_project() as seen, \
                patch.object(agent_scw_session, 'create_session',
                             side_effect=RuntimeError('boom')), \
                patch.object(agent_config, 'eu_audit') as audit, \
                patch.object(agent_db, 'create_chat',
                             wraps=agent_db.create_chat) as chat_mock, \
                self._request_mocks() as thread_cls:
            resp = self.client.post(
                '/api/projects', json=payload(name=name, scw_session=True))

        pid = self._assert_rolled_back(resp, seen, name, slug)
        self.assertIn('boom', resp.get_json()['error'])
        chat_mock.assert_not_called()
        self.assertEqual(agent_db.get_chats(project_id=pid), [])
        self.assertNotIn(pid, self.agent_api._scaffold_progress_queues)
        thread_cls.assert_not_called()
        audit.assert_called()
        self.assertTrue(
            any(c.kwargs.get('allowed') is False
                and c.kwargs.get('caller') == 'create_project'
                for c in audit.call_args_list),
            f'expected a DENY create_project audit line, got {audit.call_args_list}')

    def test_session_success_persists_and_returns_session(self):
        name = 'Atomic Session'
        with self._capture_project() as seen, \
                patch.object(agent_scw_session, 'create_session',
                             return_value=dict(SESSION_OK)), \
                self._request_mocks() as thread_cls:
            resp = self.client.post(
                '/api/projects', json=payload(name=name, scw_session=True))

        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        body = resp.get_json()
        pid = seen['project']['id']

        # Persisted row (re-read after the session was applied).
        row = agent_db.get_project(pid)
        self.assertIsNotNone(row)
        self.assertTrue(row.get('scw_session_enabled'))
        self.assertEqual(row.get('scw_project_id'), 'p-1')
        self.assertEqual(row.get('scw_session_bucket'), 'aingel-x')

        # The refreshed project is what the response carries.
        self.assertEqual(body['project']['id'], pid)
        self.assertTrue(body['project'].get('scw_session_enabled'))

        # aingel.json is rewritten with the scw block.
        with open(os.path.join(self._folder(name), 'aingel.json')) as f:
            conf = json.load(f)
        self.assertIn('scw_session', conf)
        self.assertIsNotNone(conf['scw_session'], 'scw block must not be null')
        self.assertTrue(conf['scw_session']['enabled'])
        self.assertEqual(conf['scw_session']['bucket'], 'aingel-x')
        self.assertEqual(conf['scw_session']['project_id'], 'p-1')

        # Secret-free session object.
        session = body['session']
        self.assertEqual(set(session),
                         {'project_id', 'bucket', 'kms_key_id', 'region',
                          'created_at'})
        self.assertEqual(session['bucket'], 'aingel-x')
        self.assertEqual(session['project_id'], 'p-1')
        raw = resp.get_data(as_text=True)
        self.assertNotIn(SESSION_OK['scw_secret_key'], raw)
        self.assertNotIn(SESSION_OK['scw_access_key'], raw)

        self.assertEqual(thread_cls.call_count, 1)
        thread_cls.return_value.start.assert_called_once()

    def test_session_guard_requires_eu_or_vault(self):
        name = 'Atomic Guard'
        with patch.object(agent_config, 'IS_VAULT', False), \
                self._request_mocks() as thread_cls:
            resp = self.client.post(
                '/api/projects',
                json=payload(name=name, eu_only=False, scw_session=True))

        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn('IS_VAULT', resp.get_json()['error'])
        self.assertEqual(agent_db.get_projects(), [], 'nothing may be created')
        self.assertFalse(os.path.exists(os.path.join(self.root, name)))
        thread_cls.assert_not_called()

    def test_persist_failure_shreds_resources(self):
        name, slug = 'Atomic Persist', 'atomic-persist'
        real_set_field = agent_db.set_project_field

        def _set_field(pid, field, value):
            if field == 'scw_session_enabled':
                # Simulate a persistence failure: the row never gets the flag.
                return
            return real_set_field(pid, field, value)

        with self._capture_project() as seen, \
                patch.object(agent_scw_session, 'create_session',
                             return_value=dict(SESSION_OK)), \
                patch.object(agent_db, 'set_project_field',
                             side_effect=_set_field), \
                patch.object(agent_scw_session, 'close_session') as close_mock, \
                patch.object(agent_config, 'eu_audit') as audit, \
                self._request_mocks() as thread_cls:
            resp = self.client.post(
                '/api/projects', json=payload(name=name, scw_session=True))

        pid = self._assert_rolled_back(resp, seen, name, slug)
        self.assertIn('could not persist session fields',
                      resp.get_json()['error'])

        # The live Scaleway resources must be shredded before the rollback.
        close_mock.assert_called_once()
        kwargs = close_mock.call_args.kwargs
        self.assertEqual(kwargs.get('project_id'), pid)
        self.assertEqual(kwargs.get('scw_project_id'), 'p-1')
        self.assertEqual(kwargs.get('bucket_name'), 'aingel-x')
        self.assertEqual(kwargs.get('kms_key_id'), 'k-1')

        thread_cls.assert_not_called()
        audit.assert_called()
        self.assertTrue(
            any(c.kwargs.get('allowed') is False
                and c.kwargs.get('caller') == 'create_project'
                for c in audit.call_args_list),
            f'expected a DENY create_project audit line, got {audit.call_args_list}')


if __name__ == '__main__':
    unittest.main()
