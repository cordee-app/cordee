"""Tests for the Terms & Conditions versioning + acceptance gate.

Covers the storage helpers (``agent_db``), the boot seeder and the
request-scoped gate (``agent_terms``), and the two HTTP routes
(``/api/terms/current``, ``/api/terms/accept``), through the real Flask app
with an authenticated session. No network / OIDC calls are made.

Run:  python3 -m unittest test_agent_terms -v
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import agent_db
import agent_terms


class _TermsApp:
    """Mixin: temp central DB + two users + agent_api test client (oidc mode)."""

    def setUp(self):
        self._saved_env = {
            k: os.environ.get(k) for k in
            ('AINGEL_AUTH', 'AINGEL_SESSION_SECRET', 'AINGEL_PUBLIC_URL')
        }
        os.environ['AINGEL_AUTH'] = 'oidc'
        os.environ['AINGEL_SESSION_SECRET'] = 'test-secret-' + 'x' * 32
        os.environ.pop('AINGEL_PUBLIC_URL', None)

        self.td = tempfile.TemporaryDirectory()
        self.root = self.td.name
        self.central = os.path.join(self.root, 'aingel.db')
        self.terms_dir = os.path.join(self.root, 'legal-terms')
        os.makedirs(self.terms_dir, exist_ok=True)

        self._db_path_patch = patch.object(agent_db, 'DB_PATH', self.central)
        self._db_path_patch.start()
        agent_db.init_db()

        self.admin = agent_db.get_or_create_user('sub-admin', 'admin@test', 'Admin')
        self.user = agent_db.get_or_create_user('sub-u', 'u@test', 'Uma')

        self.proj_path = os.path.join(self.root, 'proj')
        os.makedirs(self.proj_path, exist_ok=True)
        self.project = agent_db.upsert_project('Proj', 'proj', self.proj_path)

        import agent_api
        self.agent_api = agent_api
        agent_api.app.config['TESTING'] = True
        agent_api.app.config['SESSION_COOKIE_SECURE'] = False
        self.client = agent_api.app.test_client()

    def tearDown(self):
        self._db_path_patch.stop()
        self.td.cleanup()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ── helpers ──────────────────────────────────────────────────────────────
    def _login(self, user=None):
        with self.client.session_transaction() as sess:
            sess['uid'] = (user or self.user)['id']

    def _logout(self):
        with self.client.session_transaction() as sess:
            sess.pop('uid', None)

    def _write_terms(self, tag='2026-11', lang='en', body='# Terms\n\nBody A\n'):
        path = os.path.join(self.terms_dir, f'{tag}.{lang}.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(body)
        return path

    def _seed(self):
        return agent_terms.seed_terms(self.terms_dir)

    def _publish(self, tag='2026-11', lang='en'):
        row = agent_db.get_terms_version_by_tag(tag, lang, 'all')
        agent_db.set_app_meta('terms_current_version_id', row['id'])
        agent_db.set_app_meta('terms_gate_enabled', '1')
        return row


class SeederTests(_TermsApp, unittest.TestCase):
    def test_seed_creates_version_from_markdown(self):
        self._write_terms(body='# Cordée Terms\n\nHello\n')
        summary = self._seed()
        self.assertEqual(summary['seeded'], 1)
        row = agent_db.get_terms_version_by_tag('2026-11', 'en', 'all')
        self.assertIsNotNone(row)
        self.assertEqual(row['title'], 'Cordée Terms')
        self.assertEqual(row['audience'], 'all')
        self.assertIn('Hello', row['content'])
        self.assertEqual(len(row['content_hash']), 64)

    def test_seed_ignores_files_not_matching_convention(self):
        with open(os.path.join(self.terms_dir, 'README.md'), 'w') as f:
            f.write('# not a version\n')
        with open(os.path.join(self.terms_dir, 'notes.txt'), 'w') as f:
            f.write('x')
        summary = self._seed()
        self.assertEqual(summary['seeded'], 0)
        self.assertEqual(agent_db.list_terms_versions(), [])

    def test_seed_rerun_same_content_is_noop(self):
        self._write_terms()
        self._seed()
        first = agent_db.get_terms_version_by_tag('2026-11')
        summary = self._seed()
        self.assertEqual(summary['seeded'], 0)
        self.assertEqual(summary['updated'], 0)
        self.assertEqual(summary['skipped'], 1)
        second = agent_db.get_terms_version_by_tag('2026-11')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['content'], second['content'])
        self.assertEqual(len(agent_db.list_terms_versions()), 1)

    def test_seed_updates_content_before_any_acceptance(self):
        self._write_terms(body='# Terms\n\nOld text\n')
        self._seed()
        v1 = agent_db.get_terms_version_by_tag('2026-11')
        self._write_terms(body='# Terms v2\n\nNew text\n')
        summary = self._seed()
        self.assertEqual(summary['updated'], 1)
        v2 = agent_db.get_terms_version_by_tag('2026-11')
        self.assertEqual(v1['id'], v2['id'])
        self.assertIn('New text', v2['content'])
        self.assertEqual(v2['title'], 'Terms v2')
        self.assertNotEqual(v1['content_hash'], v2['content_hash'])

    def test_seed_does_not_modify_accepted_version(self):
        self._write_terms(body='# Terms\n\nOriginal\n')
        self._seed()
        row = self._publish()
        agent_db.record_terms_acceptance(self.user['id'], row['id'])
        self._write_terms(body='# Terms\n\nRewritten\n')
        summary = self._seed()
        self.assertEqual(summary['updated'], 0)
        self.assertEqual(summary['skipped'], 1)
        after = agent_db.get_terms_version(row['id'])
        self.assertEqual(after['content'], row['content'])
        self.assertEqual(after['content_hash'], row['content_hash'])
        self.assertEqual(after['title'], row['title'])
        # Evidence survives.
        self.assertIsNotNone(
            agent_db.get_terms_acceptance(self.user['id'], row['id']))

    def test_seed_never_deletes_rows(self):
        self._write_terms(tag='2026-11')
        self._write_terms(tag='2027-01')
        self._seed()
        self.assertEqual(len(agent_db.list_terms_versions()), 2)
        os.remove(os.path.join(self.terms_dir, '2026-11.en.md'))
        self._seed()
        tags = {v['version_tag'] for v in agent_db.list_terms_versions()}
        self.assertEqual(tags, {'2026-11', '2027-01'})

    def test_seed_missing_directory_is_harmless(self):
        summary = agent_terms.seed_terms(os.path.join(self.root, 'nope'))
        self.assertEqual(summary['seeded'], 0)
        self.assertEqual(summary['errors'], 0)


class DbHelperTests(_TermsApp, unittest.TestCase):
    def test_app_meta_roundtrip(self):
        self.assertIsNone(agent_db.get_app_meta('nope'))
        self.assertEqual(agent_db.get_app_meta('nope', 'dflt'), 'dflt')
        agent_db.set_app_meta('k', 'v')
        self.assertEqual(agent_db.get_app_meta('k'), 'v')
        agent_db.set_app_meta('k', 'v2')
        self.assertEqual(agent_db.get_app_meta('k'), 'v2')

    def test_record_acceptance_is_idempotent(self):
        self._write_terms()
        self._seed()
        v = agent_db.get_terms_version_by_tag('2026-11')
        first = agent_db.record_terms_acceptance(self.user['id'], v['id'])
        second = agent_db.record_terms_acceptance(self.user['id'], v['id'])
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['accepted_at'], second['accepted_at'])
        self.assertEqual(agent_db.count_terms_acceptances(v['id']), 1)

    def test_record_acceptance_rejects_none(self):
        with self.assertRaises(ValueError):
            agent_db.record_terms_acceptance(None, 1)
        with self.assertRaises(ValueError):
            agent_db.record_terms_acceptance(1, None)

    def test_accepted_version_cannot_be_deleted(self):
        self._write_terms()
        self._seed()
        v = agent_db.get_terms_version_by_tag('2026-11')
        agent_db.record_terms_acceptance(self.user['id'], v['id'])
        conn = sqlite3.connect(self.central)
        try:
            conn.execute('PRAGMA foreign_keys = ON')
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute('DELETE FROM terms_versions WHERE id=?', (v['id'],))
        finally:
            conn.close()

    def test_unaccepted_version_can_be_deleted(self):
        self._write_terms()
        self._seed()
        v = agent_db.get_terms_version_by_tag('2026-11')
        conn = sqlite3.connect(self.central)
        try:
            conn.execute('PRAGMA foreign_keys = ON')
            conn.execute('DELETE FROM terms_versions WHERE id=?', (v['id'],))
            conn.commit()
        finally:
            conn.close()
        self.assertIsNone(agent_db.get_terms_version(v['id']))


class GateTests(_TermsApp, unittest.TestCase):
    def test_off_mode_passthrough(self):
        self._write_terms()
        self._seed()
        self._publish()
        os.environ['AINGEL_AUTH'] = 'off'
        resp = self.client.get(f'/api/memory/{self.project["id"]}')
        self.assertNotEqual(resp.status_code, 403)

    def test_inactive_when_pointer_unset(self):
        self._login()
        resp = self.client.get(f'/api/memory/{self.project["id"]}')
        self.assertNotEqual(resp.status_code, 403)

    def test_inactive_when_flag_off(self):
        self._write_terms()
        self._seed()
        row = self._publish()
        agent_db.set_app_meta('terms_gate_enabled', '0')
        self._login()
        resp = self.client.get(f'/api/memory/{self.project["id"]}')
        self.assertNotEqual(resp.status_code, 403)
        self.assertIsNotNone(agent_db.get_terms_version(row['id']))

    def test_fails_open_when_pointer_dangles(self):
        self._write_terms()
        self._seed()
        self._publish()
        agent_db.set_app_meta('terms_current_version_id', '999999')
        self.assertFalse(agent_db.terms_gate_active())
        self._login()
        resp = self.client.get(f'/api/memory/{self.project["id"]}')
        self.assertNotEqual(resp.status_code, 403)

    def test_active_blocks_until_accepted(self):
        self._write_terms()
        self._seed()
        row = self._publish()
        self._login()
        resp = self.client.get(f'/api/memory/{self.project["id"]}')
        self.assertEqual(resp.status_code, 403)
        body = resp.get_json()
        self.assertEqual(body['error'], 'terms_acceptance_required')
        self.assertEqual(body['terms']['version_id'], row['id'])
        self.assertEqual(body['terms']['tag'], '2026-11')
        self.assertEqual(body['terms']['language'], 'en')

        agent_db.record_terms_acceptance(self.user['id'], row['id'])
        resp = self.client.get(f'/api/memory/{self.project["id"]}')
        self.assertNotEqual(resp.status_code, 403)

    def test_anonymous_is_not_gated(self):
        self._write_terms()
        self._seed()
        self._publish()
        self._logout()
        resp = self.client.get(f'/api/memory/{self.project["id"]}')
        self.assertNotEqual(resp.status_code, 403)

    def test_api_me_exact_match_is_exempt(self):
        self._write_terms()
        self._seed()
        self._publish()
        self._login()
        resp = self.client.get('/api/me')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['terms']['required'])

    def test_api_memory_is_not_exempt(self):
        self._write_terms()
        self._seed()
        self._publish()
        self._login()
        resp = self.client.get(f'/api/memory/{self.project["id"]}')
        self.assertEqual(resp.status_code, 403)

    def test_auth_routes_are_exempt(self):
        self._write_terms()
        self._seed()
        self._publish()
        self._login()
        resp = self.client.post('/api/auth/logout')
        self.assertEqual(resp.status_code, 200)

    def test_terms_routes_are_exempt(self):
        self._write_terms()
        self._seed()
        self._publish()
        self._login()
        resp = self.client.get('/api/terms/current')
        self.assertEqual(resp.status_code, 200)

    def test_health_is_exempt(self):
        self._write_terms()
        self._seed()
        self._publish()
        self._login()
        self.assertEqual(self.client.get('/api/health').status_code, 200)

    def test_files_is_gated(self):
        self._write_terms()
        self._seed()
        self._publish()
        self._login()
        with patch.object(self.agent_api, 'FILES_ROOT', self.root):
            resp = self.client.get('/files')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()['error'], 'terms_acceptance_required')

    def test_dav_is_not_gated_and_does_not_500(self):
        self._write_terms()
        self._seed()
        self._publish()
        self._login()
        resp = self.client.get('/dav/anything')
        # WebDAV keeps its own edge auth; the terms gate must not AttributeError.
        self.assertNotEqual(resp.status_code, 500)

    def test_app_shell_is_not_gated(self):
        self._write_terms()
        self._seed()
        self._publish()
        self._login()
        resp = self.client.get('/favicon.svg')
        self.assertNotEqual(resp.status_code, 403)

    def test_terms_state_for_none_user(self):
        self._write_terms()
        self._seed()
        self._publish()
        state = agent_terms.terms_state_for(None)
        self.assertFalse(state['required'])
        self.assertIsNone(state['version_id'])

    def test_terms_state_for_swallows_db_errors(self):
        with patch.object(agent_db, 'get_current_terms_version',
                          side_effect=RuntimeError('boom')):
            state = agent_terms.terms_state_for(self.user)
        self.assertEqual(state, {'required': False, 'version_id': None,
                                 'tag': None, 'accepted_at': None})


class AcceptEndpointTests(_TermsApp, unittest.TestCase):
    def _setup_published(self):
        self._write_terms()
        self._seed()
        return self._publish()

    def test_current_terms_payload(self):
        row = self._setup_published()
        self._login()
        resp = self.client.get('/api/terms/current')
        self.assertEqual(resp.status_code, 200)
        terms = resp.get_json()['terms']
        self.assertEqual(terms['version_id'], row['id'])
        self.assertEqual(terms['tag'], '2026-11')
        self.assertEqual(terms['content'], row['content'])
        self.assertIsNone(terms['accepted_at'])

    def test_current_terms_none_when_unpublished(self):
        self._login()
        resp = self.client.get('/api/terms/current')
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.get_json()['terms'])

    def test_accept_twice_is_idempotent(self):
        row = self._setup_published()
        self._login()
        first = self.client.post('/api/terms/accept', json={'version_id': row['id']})
        self.assertEqual(first.status_code, 200)
        second = self.client.post('/api/terms/accept', json={'version_id': row['id']})
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()['ok'])
        self.assertEqual(first.get_json()['accepted_at'],
                         second.get_json()['accepted_at'])
        self.assertEqual(agent_db.count_terms_acceptances(row['id']), 1)

    def test_accept_accepts_numeric_string(self):
        row = self._setup_published()
        self._login()
        resp = self.client.post('/api/terms/accept',
                                json={'version_id': str(row['id'])})
        self.assertEqual(resp.status_code, 200)

    def test_accept_version_mismatch(self):
        row = self._setup_published()
        self._login()
        resp = self.client.post('/api/terms/accept', json={'version_id': row['id'] + 99})
        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        self.assertEqual(body['error'], 'terms_version_changed')
        self.assertEqual(body['terms']['version_id'], row['id'])
        self.assertEqual(body['terms']['tag'], '2026-11')

    def test_accept_without_current_terms(self):
        self._login()
        resp = self.client.post('/api/terms/accept', json={'version_id': 1})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()['error'], 'no_current_terms')

    def test_accept_requires_user(self):
        self._setup_published()
        self._logout()
        resp = self.client.post('/api/terms/accept', json={'version_id': 1})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()['error'], 'auth_required')

    def test_accept_unlocks_the_gate(self):
        row = self._setup_published()
        self._login()
        self.assertEqual(
            self.client.get(f'/api/memory/{self.project["id"]}').status_code, 403)
        self.client.post('/api/terms/accept', json={'version_id': row['id']})
        self.assertNotEqual(
            self.client.get(f'/api/memory/{self.project["id"]}').status_code, 403)


class MeEndpointTests(_TermsApp, unittest.TestCase):
    def test_me_terms_required_true(self):
        self._write_terms()
        self._seed()
        self._publish()
        self._login()
        body = self.client.get('/api/me').get_json()
        self.assertIn('terms', body)
        self.assertTrue(body['terms']['required'])
        self.assertEqual(body['terms']['tag'], '2026-11')
        self.assertIsNone(body['terms']['accepted_at'])

    def test_me_terms_required_false_after_accept(self):
        self._write_terms()
        self._seed()
        row = self._publish()
        agent_db.record_terms_acceptance(self.user['id'], row['id'])
        self._login()
        body = self.client.get('/api/me').get_json()
        self.assertFalse(body['terms']['required'])
        self.assertIsNotNone(body['terms']['accepted_at'])

    def test_me_terms_false_when_gate_off(self):
        self._write_terms()
        self._seed()
        self._publish()
        agent_db.set_app_meta('terms_gate_enabled', '0')
        self._login()
        body = self.client.get('/api/me').get_json()
        self.assertFalse(body['terms']['required'])

    def test_me_anonymous_is_401(self):
        # @require_auth short-circuits in oidc mode; the anonymous branch of
        # me() (terms=None) is only reachable in off mode.
        self._logout()
        resp = self.client.get('/api/me')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()['error'], 'auth_required')

    def test_me_off_mode_anonymous_has_terms_key(self):
        os.environ['AINGEL_AUTH'] = 'off'
        self._logout()
        body = self.client.get('/api/me').get_json()
        self.assertIsNone(body['user'])
        self.assertIn('terms', body)
        self.assertIsNone(body['terms'])


if __name__ == '__main__':
    unittest.main()
