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


class CustomModelImportAuthzTests(_TwoTenantApp, unittest.TestCase):
    """/api/hf-models/imports must not leak other projects' imports to members."""

    def setUp(self):
        super().setUp()
        # _register_import_roster may write the shared hf_models.json catalogue —
        # redirect it to a throwaway file so tests never touch the repo data.
        import hf_catalog
        self._catalog_patch = patch.object(
            hf_catalog, '_CATALOG_PATH', os.path.join(self.root, 'hf_models.json'))
        self._catalog_patch.start()
        # Never reach the real Scaleway/HF APIs from route-level tests: stub the
        # library listing (empty) and the existence probe (transient/None), so a
        # live SCW key on the host cannot make these tests non-deterministic.
        self._list_models_patch = patch.object(
            self.agent_api.agent_scw_deploy, 'list_models', return_value=[])
        self._list_models_patch.start()
        self._model_exists_patch = patch.object(
            self.agent_api.agent_scw_deploy, 'model_exists', return_value=None)
        self._model_exists_patch.start()

    def tearDown(self):
        self._model_exists_patch.stop()
        self._list_models_patch.stop()
        self._catalog_patch.stop()
        super().tearDown()

    def _seed(self):
        agent_db.add_model_import(self.proj_a['id'], 'a/repo', 'imp-a',
                                  scw_model_id='ua', status='ready')
        agent_db.add_model_import(self.proj_b['id'], 'b/repo', 'imp-b',
                                  scw_model_id='ub', status='ready')
        agent_db.add_model_import(None, 'org/repo', 'imp-org',
                                  scw_model_id='uo', status='ready')

    def test_member_cannot_read_other_projects_imports(self):
        self._seed()
        self._login(self.user_a)
        resp = self.client.get(
            f"/api/hf-models/imports?project_id={self.proj_b['id']}")
        # Anti-enumeration: non-member project -> 404, never the rows.
        self.assertEqual(resp.status_code, 404)

    def test_member_sees_only_own_project_rows(self):
        self._seed()
        self._login(self.user_a)
        resp = self.client.get('/api/hf-models/imports')
        self.assertEqual(resp.status_code, 200)
        repos = {r['repo_id'] for r in resp.get_json()['imports']}
        self.assertEqual(repos, {'a/repo'})

    def test_admin_sees_all_rows(self):
        self._seed()
        self._login(self.admin)
        resp = self.client.get('/api/hf-models/imports')
        self.assertEqual(resp.status_code, 200)
        repos = {r['repo_id'] for r in resp.get_json()['imports']}
        self.assertEqual(repos, {'a/repo', 'b/repo', 'org/repo'})

    def test_verify_exception_returns_friendly_502(self):
        self._login(self.admin)
        with patch.object(self.agent_api.agent_scw_deploy, 'verify_hf_model',
                          side_effect=RuntimeError('boom')):
            resp = self.client.post('/api/hf-models/verify',
                                    json={'repo_id': 'org/repo'})
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.get_json()['error_code'], 'error')

    def test_non_owner_cannot_import(self):
        self._login(self.user_b)  # not a member of project A
        with patch.object(self.agent_api.agent_scw_deploy, 'import_hf_model') as m:
            resp = self.client.post(
                f"/api/projects/{self.proj_a['id']}/hf-models/import",
                json={'repo_id': 'org/repo'})
        self.assertIn(resp.status_code, (403, 404))
        m.assert_not_called()

    def test_import_reuses_existing_model_and_registers_roster(self):
        self._seed()  # org/repo is ready
        self._login(self.user_a)
        with patch.object(self.agent_api.agent_scw_deploy, 'import_hf_model') as m:
            resp = self.client.post(
                f"/api/projects/{self.proj_a['id']}/hf-models/import",
                json={'repo_id': 'org/repo'})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body.get('reused'))
        self.assertEqual(body['model_id'], 'uo')
        m.assert_not_called()
        # Reuse must adopt the repo into the initiating project's roster so the
        # cross-project task picker lists it.
        repos = {r['repo_id'] for r in agent_db.get_project_hf_models(self.proj_a['id'])}
        self.assertIn('org/repo', repos)

    def test_reuse_resumes_poller_for_inflight_row(self):
        # A row stranded at 'downloading' must re-spawn a poller on re-import
        # (e.g. after a server restart killed the original one).
        agent_db.add_model_import(self.proj_a['id'], 'org/inflight', 'lib-i',
                                  scw_model_id='uuid-inflight', status='downloading')
        self._login(self.user_a)
        with patch.object(self.agent_api, '_launch_import_poll') as lp, \
             patch.object(self.agent_api.agent_scw_deploy, 'import_hf_model') as m:
            resp = self.client.post(
                f"/api/projects/{self.proj_a['id']}/hf-models/import",
                json={'repo_id': 'org/inflight'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get('reused'))
        lp.assert_called_once()
        self.assertEqual(lp.call_args[0][1], 'uuid-inflight')
        m.assert_not_called()

    def test_sync_advances_error_row_when_model_now_present(self):
        # A row marked failed/error whose Scaleway import later completed must be
        # advanced by sync so the user need not create a duplicate import.
        iid = agent_db.add_model_import(self.proj_a['id'], 'late/repo', 'lib-late',
                                        scw_model_id='uuid-late', status='failed')
        deployable = [{'id': 'uuid-late', 'name': 'lib-late', 'custom': True,
                       'status': 'ready'}]
        self.agent_api._sync_library_imports(deployable)
        self.assertEqual(agent_db.get_model_import(iid)['status'], 'ready')

    def test_hf_queue_group_marks_ready_import_servable(self):
        # A queued task whose repo has a ready import row must come back
        # servable (the GPU-window left panel badge/button read this flag).
        self._seed()  # org/repo is ready as imp-org
        agent_db.update_task(self.task_a, project_path=self.path_a,
                             hf_repo_id='org/repo')
        self._login(self.user_a)
        resp = self.client.get('/api/hf-queue')
        self.assertEqual(resp.status_code, 200)
        groups = {g['repo_id']: g for g in resp.get_json()['groups']}
        g = groups['org/repo']
        self.assertTrue(g['servable'])
        self.assertTrue(g['import_ready'])
        self.assertEqual(g['import_status'], 'ready')
        self.assertEqual(g['import_model_name'], 'imp-org')

    def test_hf_queue_group_flags_inflight_import(self):
        agent_db.add_model_import(self.proj_a['id'], 'q/repo', 'imp-q',
                                  scw_model_id='uq', status='downloading')
        agent_db.update_task(self.task_a, project_path=self.path_a,
                             hf_repo_id='q/repo')
        self._login(self.user_a)
        resp = self.client.get('/api/hf-queue')
        self.assertEqual(resp.status_code, 200)
        g = {gr['repo_id']: gr for gr in resp.get_json()['groups']}['q/repo']
        self.assertFalse(g['servable'])
        self.assertFalse(g['import_ready'])
        self.assertEqual(g['import_status'], 'downloading')

    def test_hf_queue_group_without_import_stays_unservable(self):
        agent_db.update_task(self.task_a, project_path=self.path_a,
                             hf_repo_id='plain/repo')
        self._login(self.user_a)
        resp = self.client.get('/api/hf-queue')
        self.assertEqual(resp.status_code, 200)
        g = {gr['repo_id']: gr for gr in resp.get_json()['groups']}['plain/repo']
        self.assertFalse(g['servable'])
        self.assertFalse(g['import_ready'])
        self.assertIsNone(g['import_status'])

    def test_hf_queue_hides_done_and_cancelled_tasks(self):
        # Finished tasks no longer need a GPU window; failed ones stay for retry.
        agent_db.update_task(self.task_a, project_path=self.path_a,
                             hf_repo_id='done/repo', status='done')
        self._login(self.user_a)
        repos = {gr['repo_id'] for gr in self.client.get('/api/hf-queue').get_json()['groups']}
        self.assertNotIn('done/repo', repos)

        agent_db.update_task(self.task_a, project_path=self.path_a, status='failed')
        repos = {gr['repo_id'] for gr in self.client.get('/api/hf-queue').get_json()['groups']}
        self.assertIn('done/repo', repos)

    def test_register_import_roster_is_global_via_adopted(self):
        # A repo adopted by project A shows up in the cross-project adopted list
        # (any project can use it).
        self.agent_api._register_import_roster(self.proj_a['id'], 'org/newrepo',
                                               fetch_meta=False)
        self._login(self.user_a)
        resp = self.client.get('/api/hf/adopted')
        self.assertEqual(resp.status_code, 200)
        repos = {c['repo_id'] for c in resp.get_json()['models']}
        self.assertIn('org/newrepo', repos)

    def test_adopted_candidate_annotated_with_import_status(self):
        agent_db.add_project_hf_model(self.proj_a['id'], 'a/repo', model_id='',
                                      label='a/repo')
        agent_db.add_model_import(self.proj_a['id'], 'a/repo', 'imp-a',
                                  scw_model_id='ua', status='ready')
        self._login(self.user_a)
        resp = self.client.get('/api/hf/adopted')
        self.assertEqual(resp.status_code, 200)
        cand = next(c for c in resp.get_json()['models'] if c['repo_id'] == 'a/repo')
        self.assertTrue(cand['import_ready'])
        self.assertEqual(cand['import_model_name'], 'imp-a')
        self.assertEqual(cand['import_status'], 'ready')

    def test_sync_library_imports_backfills_missing_rows(self):
        # A repo adopted by A, a console-imported custom model matching its name,
        # but no scw_model_imports row yet.
        agent_db.add_project_hf_model(self.proj_a['id'], 'Qwen3-0.6B', model_id='',
                                      label='Qwen3-0.6B')
        deployable = [{
            'id': 'uuid-q', 'name': 'Qwen3-0.6B-abc123', 'custom': True,
            'status': 'ready', 'size_bytes': 100,
        }]
        added = self.agent_api._sync_library_imports(deployable)
        self.assertEqual(added, 1)
        row = agent_db.find_latest_model_import('Qwen3-0.6B')
        self.assertIsNotNone(row)
        self.assertEqual(row['scw_model_id'], 'uuid-q')
        self.assertEqual(row['project_id'], self.proj_a['id'])
        # Idempotent: a second pass adds nothing.
        self.assertEqual(self.agent_api._sync_library_imports(deployable), 0)

    def test_sync_reconciles_stale_ready_row(self):
        # Row says ready, but its Scaleway model is gone (404) -> flip to error.
        agent_db.add_model_import(self.proj_a['id'], 'gone/repo', 'gone-model',
                                  scw_model_id='uuid-gone', status='ready')
        with patch.object(self.agent_api.agent_scw_deploy, 'model_exists',
                          return_value=False):
            self.agent_api._sync_library_imports([])
        row = agent_db.find_latest_model_import('gone/repo')
        self.assertEqual(row['status'], 'error')
        self.assertIn('no longer exists', row['error_message'])

    def test_sync_leaves_live_ready_row_untouched(self):
        agent_db.add_model_import(self.proj_a['id'], 'live/repo', 'live-model',
                                  scw_model_id='uuid-live', status='ready')
        # Model exists but isn't in the (filtered) custom list -> must stay ready.
        with patch.object(self.agent_api.agent_scw_deploy, 'model_exists',
                          return_value=True) as me:
            self.agent_api._sync_library_imports([])
        self.assertEqual(agent_db.find_latest_model_import('live/repo')['status'], 'ready')
        me.assert_called_once()

    def test_sync_advances_inflight_row_from_library(self):
        # Row stranded at 'downloading' after a restart; Scaleway now says ready.
        iid = agent_db.add_model_import(self.proj_a['id'], 'inflight/repo', 'lib-m',
                                        scw_model_id='uuid-inflight', status='downloading')
        deployable = [{'id': 'uuid-inflight', 'name': 'lib-m', 'custom': True,
                       'status': 'ready'}]
        self.agent_api._sync_library_imports(deployable)
        self.assertEqual(agent_db.get_model_import(iid)['status'], 'ready')

    def test_sync_marks_stranded_inflight_row_error(self):
        iid = agent_db.add_model_import(self.proj_a['id'], 'stranded/repo', 'lib-s',
                                        scw_model_id='uuid-stranded', status='preparing')
        with patch.object(self.agent_api.agent_scw_deploy, 'model_exists',
                          return_value=False):
            self.agent_api._sync_library_imports([])
        row = agent_db.get_model_import(iid)
        self.assertEqual(row['status'], 'error')
        self.assertIn('disappeared', row['error_message'])


if __name__ == '__main__':
    unittest.main()
