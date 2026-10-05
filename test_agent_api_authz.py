"""Regression tests for the P0 cross-tenant / arbitrary-file security fixes.

These are end-to-end-ish tests: they build a temp central DB with two projects
owned by two different users, then exercise the real Flask app (agent_api.app)
through its test client with an authenticated session. They cover:

  * P0-1  a PATCHed chat ``file_path`` / task ``project_path`` is ignored
  * P0-2  ids from different projects in one request are rejected (403)
  * P0-2  ``/api/execute`` requires a task_id (no "first confirmed anywhere")
  * P0-2  task attachments cannot read another project's task

Run:  venv/bin/python -m unittest test_agent_api_authz -v
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import agent_db
import agent_config


class _TwoTenantApp:
    """Mixin: temp central DB + two users/projects + agent_api test client."""

    def setUp(self):
        self._saved_env = {
            k: os.environ.get(k) for k in
            ('AINGEL_AUTH', 'AINGEL_SESSION_SECRET', 'AINGEL_PUBLIC_URL')
        }
        os.environ['AINGEL_AUTH'] = 'oidc'
        os.environ['AINGEL_SESSION_SECRET'] = 'test-secret-' + 'x' * 32
        os.environ['AINGEL_PUBLIC_URL'] = 'https://aingel.test'

        self.td = tempfile.TemporaryDirectory()
        self.root = self.td.name
        self.central = os.path.join(self.root, 'aingel.db')

        self._db_path_patch = patch.object(agent_db, 'DB_PATH', self.central)
        self._db_path_patch.start()
        agent_db.init_db()

        # Users: first-ever user becomes admin; the two tenants are members.
        self.admin = agent_db.get_or_create_user('sub-admin', 'admin@test', 'Admin')
        self.user_a = agent_db.get_or_create_user('sub-a', 'a@test', 'Alice')
        self.user_b = agent_db.get_or_create_user('sub-b', 'b@test', 'Bob')

        self.path_a = os.path.join(self.root, 'proj-a')
        self.path_b = os.path.join(self.root, 'proj-b')
        os.makedirs(self.path_a, exist_ok=True)
        os.makedirs(self.path_b, exist_ok=True)

        self.proj_a = agent_db.upsert_project('Proj A', 'proj-a', self.path_a)
        self.proj_b = agent_db.upsert_project('Proj B', 'proj-b', self.path_b)
        agent_db.set_project_owner(self.proj_a['id'], self.user_a['id'])
        agent_db.set_project_owner(self.proj_b['id'], self.user_b['id'])

        self.task_a = agent_db.create_task(
            self.proj_a['id'], 'Task A', project_path=self.path_a)
        self.task_b = agent_db.create_task(
            self.proj_b['id'], 'Secret Task B', description='tenant B secret',
            project_path=self.path_b)
        self.chat_a = agent_db.create_chat(
            self.proj_a['id'], 'Chat A', project_path=self.path_a)['id']
        self.chat_b = agent_db.create_chat(
            self.proj_b['id'], 'Chat B', project_path=self.path_b)['id']

        import agent_api
        self.agent_api = agent_api
        agent_api.app.config['TESTING'] = True
        # The prod cookie is Secure-only; the werkzeug test client speaks http.
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

    def _login(self, user):
        with self.client.session_transaction() as sess:
            sess['uid'] = user['id']


class PatchFieldAllowListTests(_TwoTenantApp, unittest.TestCase):
    def test_chat_file_path_is_ignored(self):
        self._login(self.user_a)
        with patch.object(self.agent_api.db, 'update_chat') as m:
            resp = self.client.patch(
                f'/api/chats/{self.chat_a}',
                json={'name': 'renamed', 'file_path': '/etc/passwd'})
        self.assertEqual(resp.status_code, 200)
        m.assert_called_once()
        args, kwargs = m.call_args
        self.assertEqual(args[0], self.chat_a)
        self.assertEqual(kwargs, {'name': 'renamed'})
        self.assertNotIn('file_path', kwargs)

    def test_chat_unknown_fields_dropped(self):
        self._login(self.user_a)
        with patch.object(self.agent_api.db, 'update_chat') as m:
            self.client.patch(
                f'/api/chats/{self.chat_a}',
                json={'status': 'archived', 'project_path': '/tmp/x',
                      'nonsense': 1})
        _, kwargs = m.call_args
        self.assertEqual(kwargs, {'status': 'archived'})

    def test_task_project_path_is_ignored(self):
        self._login(self.user_a)
        with patch.object(self.agent_api.db, 'update_task') as m:
            resp = self.client.patch(
                f'/api/tasks/{self.task_a}',
                json={'title': 'renamed', 'project_path': self.path_b})
        self.assertEqual(resp.status_code, 200)
        m.assert_called_once()
        args, kwargs = m.call_args
        self.assertEqual(args[0], self.task_a)
        self.assertNotIn('project_path', kwargs)
        self.assertEqual(kwargs, {'title': 'renamed'})


class CrossTenantIdTests(_TwoTenantApp, unittest.TestCase):
    def test_execute_with_mismatched_project_and_task_is_forbidden(self):
        self._login(self.user_a)
        resp = self.client.post('/api/execute', json={
            'project_id': self.proj_a['id'],
            'task_id': self.task_b,   # belongs to tenant B
        })
        self.assertEqual(resp.status_code, 403)

    def test_execute_requires_task_id(self):
        self._login(self.user_a)
        resp = self.client.post('/api/execute', json={'project_id': self.proj_a['id']})
        self.assertEqual(resp.status_code, 400)

    def test_cross_project_chat_patch_is_not_found(self):
        self._login(self.user_a)
        resp = self.client.patch(
            f'/api/chats/{self.chat_b}', json={'name': 'hijack'})
        # Anti-enumeration: no membership -> 404, never 200.
        self.assertEqual(resp.status_code, 404)

    def test_resolve_pid_rejects_mixed_ids(self):
        import agent_auth
        self._login(self.user_a)
        with self.agent_api.app.test_request_context(
                '/x', json={'project_id': self.proj_a['id'],
                            'task_id': self.task_b}):
            with self.assertRaises(agent_auth.ProjectAccessMismatch):
                agent_auth._resolve_pid()

    def test_resolve_pid_accepts_consistent_ids(self):
        import agent_auth
        self._login(self.user_a)
        with self.agent_api.app.test_request_context(
                '/x', json={'project_id': self.proj_a['id'],
                            'task_id': self.task_a}):
            self.assertEqual(agent_auth._resolve_pid(), self.proj_a['id'])


class FileServingHeaderTests(_TwoTenantApp, unittest.TestCase):
    def test_html_is_forced_to_attachment(self):
        with open(os.path.join(self.path_a, 'evil.html'), 'w') as f:
            f.write('<script>alert(1)</script>')
        with patch.object(self.agent_api, 'FILES_ROOT', self.root):
            self._login(self.user_a)
            resp = self.client.get('/files/proj-a/evil.html')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('attachment', resp.headers.get('Content-Disposition', ''))
        self.assertEqual(resp.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertIn('Content-Security-Policy', resp.headers)

    def test_svg_is_forced_to_attachment(self):
        with open(os.path.join(self.path_a, 'evil.svg'), 'w') as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        with patch.object(self.agent_api, 'FILES_ROOT', self.root):
            self._login(self.user_a)
            resp = self.client.get('/files/proj-a/evil.svg')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('attachment', resp.headers.get('Content-Disposition', ''))


    # /files/<pid>/<path> is the route the frontend actually links to (Exec Log,
    # Files browser). It must get the same hardening as /files/<subpath>.
    def test_project_file_route_html_forced_to_attachment(self):
        with open(os.path.join(self.path_a, 'evil.html'), 'w') as f:
            f.write('<script>alert(1)</script>')
        self._login(self.user_a)
        resp = self.client.get(f'/files/{self.proj_a["id"]}/evil.html')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('attachment', resp.headers.get('Content-Disposition', ''))
        self.assertEqual(resp.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertIn('sandbox', resp.headers.get('Content-Security-Policy', ''))

    def test_project_file_route_svg_forced_to_attachment(self):
        with open(os.path.join(self.path_a, 'evil.svg'), 'w') as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        self._login(self.user_a)
        resp = self.client.get(f'/files/{self.proj_a["id"]}/evil.svg')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('attachment', resp.headers.get('Content-Disposition', ''))

    def test_project_file_route_markdown_stays_inline(self):
        with open(os.path.join(self.path_a, 'notes.md'), 'w') as f:
            f.write('# hi')
        self._login(self.user_a)
        resp = self.client.get(f'/files/{self.proj_a["id"]}/notes.md')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('attachment', resp.headers.get('Content-Disposition', ''))
        self.assertEqual(resp.headers.get('X-Content-Type-Options'), 'nosniff')

    def test_project_file_route_other_tenant_denied(self):
        with open(os.path.join(self.path_b, 'secret.md'), 'w') as f:
            f.write('secret')
        self._login(self.user_a)
        resp = self.client.get(f'/files/{self.proj_b["id"]}/secret.md')
        self.assertIn(resp.status_code, (403, 404))


class AttachmentScopingTests(_TwoTenantApp, unittest.TestCase):
    def test_executor_task_attachment_is_project_scoped(self):
        import agent_executor
        att = {'kind': 'task', 'ref': str(self.task_b)}
        # From project A the tenant-B task must not resolve.
        self.assertIsNone(agent_executor._load_attachment(att, self.path_a))
        # From its own project it does.
        loaded = agent_executor._load_attachment(att, self.path_b)
        self.assertIsNotNone(loaded)
        self.assertIn('Secret Task B', loaded[1])

    def test_improve_prompt_attachment_is_project_scoped(self):
        got = self.agent_api._load_attachment_content(
            'task', str(self.task_b), self.path_a)
        self.assertIsNone(got)
        got_own = self.agent_api._load_attachment_content(
            'task', str(self.task_b), self.path_b)
        self.assertEqual(got_own, 'tenant B secret')


class UserDirectoryTests(_TwoTenantApp, unittest.TestCase):
    """GET /api/users is a minimal directory for picking members by name.

    Admin (member of nothing) sees everyone. A tenant sees only themselves and
    the users they share a project with, so the endpoint cannot be used to
    enumerate the whole user base.
    """

    def test_requires_auth(self):
        resp = self.client.get('/api/users')
        self.assertEqual(resp.status_code, 401)

    def test_admin_sees_everyone(self):
        self._login(self.admin)
        resp = self.client.get('/api/users')
        self.assertEqual(resp.status_code, 200)
        ids = {u['id'] for u in resp.get_json()['users']}
        self.assertEqual(ids, {self.admin['id'], self.user_a['id'], self.user_b['id']})

    def test_member_sees_only_self_and_co_members(self):
        # Put Bob on Alice's project, leave the admin off every project.
        agent_db.add_project_member(self.proj_a['id'], self.user_b['id'], 'member')
        self._login(self.user_a)
        resp = self.client.get('/api/users')
        self.assertEqual(resp.status_code, 200)
        ids = {u['id'] for u in resp.get_json()['users']}
        self.assertEqual(ids, {self.user_a['id'], self.user_b['id']})
        self.assertNotIn(self.admin['id'], ids)

    def test_row_shape_is_minimal(self):
        self._login(self.admin)
        rows = self.client.get('/api/users').get_json()['users']
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(set(r.keys()), {'id', 'name', 'email', 'role'})


class UserDeletionTests(_TwoTenantApp, unittest.TestCase):
    """DELETE /api/admin/users/<id> — admin only, with safety guards."""

    def test_requires_admin(self):
        self._login(self.user_a)
        resp = self.client.delete(f'/api/admin/users/{self.user_b["id"]}')
        # require_admin returns 404 (not 403) to avoid role enumeration.
        self.assertEqual(resp.status_code, 404)
        self.assertIsNotNone(agent_db.get_user(self.user_b['id']))

    def test_cannot_delete_self(self):
        self._login(self.admin)
        resp = self.client.delete(f'/api/admin/users/{self.admin["id"]}')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()['error'], 'cannot_modify_self')

    def test_deletes_user_memberships_and_counters(self):
        agent_db.add_project_member(self.proj_a['id'], self.user_b['id'], 'member')
        agent_db.add_project_member(self.proj_b['id'], self.user_b['id'], 'owner')
        self._login(self.admin)
        resp = self.client.delete(f'/api/admin/users/{self.user_b["id"]}')
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(agent_db.get_user(self.user_b['id']))
        # Membership rows are gone (project_b still exists).
        members_b = agent_db.get_project_members(self.proj_b['id'])
        self.assertNotIn(self.user_b['id'], {m['user_id'] for m in members_b})

    def test_owned_project_is_orphaned_not_deleted(self):
        # Bob owns project B; deleting Bob keeps the project but clears owner_id.
        self._login(self.admin)
        resp = self.client.delete(f'/api/admin/users/{self.user_b["id"]}')
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(resp.get_json()['orphaned_projects'], 1)
        proj = agent_db.get_project(self.proj_b['id'])
        self.assertIsNotNone(proj)
        self.assertIsNone(proj.get('owner_id'))

    def test_last_admin_guard_in_db_layer(self):
        # The API always has the calling admin as a second admin, so the
        # last_admin guard is only reachable through the db helper.
        with self.assertRaises(ValueError) as ctx:
            agent_db.delete_user(self.admin['id'])
        self.assertEqual(str(ctx.exception), 'last_admin')
        self.assertIsNotNone(agent_db.get_user(self.admin['id']))

    def test_missing_user_is_404(self):
        self._login(self.admin)
        resp = self.client.delete('/api/admin/users/999999')
        self.assertEqual(resp.status_code, 404)


class OwnerSyncTests(_TwoTenantApp, unittest.TestCase):
    """projects.owner_id must follow membership ownership transfers.

    User report 2026-10-02: the Members modal moved the owner role but
    projects.owner_id stayed on the previous owner — quota resolution,
    storage stock and ownership display kept charging the old owner.
    """

    def setUp(self):
        super().setUp()
        # Seed an owner membership for A, mirroring the live projects where
        # the creator/owner holds both owner_id and an owner membership row.
        agent_db.add_project_member(self.proj_a['id'], self.user_a['id'], 'owner')

    def _owner_id(self):
        return agent_db.get_project(self.proj_a['id'])['owner_id']

    def test_demoting_owner_transfers_owner_id(self):
        self._login(self.admin)
        resp = self.client.post(
            f"/api/projects/{self.proj_a['id']}/members",
            json={'user_id': self.user_b['id'], 'role': 'owner'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._owner_id(), self.user_a['id'])  # A still an owner
        resp = self.client.put(
            f"/api/projects/{self.proj_a['id']}/members/{self.user_a['id']}",
            json={'role': 'member'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._owner_id(), self.user_b['id'],
                         'owner_id must follow the demotion')

    def test_removing_owner_member_transfers_owner_id(self):
        self._login(self.admin)
        self.client.post(
            f"/api/projects/{self.proj_a['id']}/members",
            json={'user_id': self.user_b['id'], 'role': 'owner'})
        resp = self.client.delete(
            f"/api/projects/{self.proj_a['id']}/members/{self.user_a['id']}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._owner_id(), self.user_b['id'],
                         'owner_id must follow the removal')

    def test_last_owner_membership_is_protected(self):
        self._login(self.admin)
        resp = self.client.delete(
            f"/api/projects/{self.proj_a['id']}/members/{self.user_a['id']}")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get('error'), 'last_owner')
        self.assertEqual(self._owner_id(), self.user_a['id'])


if __name__ == '__main__':
    unittest.main()
