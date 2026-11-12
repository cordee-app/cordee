"""Dependency gate — backend tests (Plans/Cordee_Dependency_Gate_Plan_2026-10-17).

Incident: RP-096 (created 2026-10-17 depending on the pending RP-077) was
skipped twice by the slot-2 scheduler with log-only feedback — no execution
row, no Guide notification, nothing on the card. These tests pin:

  * agent_contract.check        — deps-done rule, fail-open on a garbage dep
  * the run_task entry gate     — dep_blocked refusal with zero side effects
  * resume bypass               — a resumed run is never re-gated
  * H2 step 0                   — deterministic hold, no _ask LLM call
  * the save-time acknowledge   — 409 without acknowledged:true
  * PATCH slot-assign warning   — assignment proceeds, response warns
  * the Guide 🔔 notice          — posted, no execution row for the chat
  * _run_seq's TOCTOU backstop  — a dep_blocked result never reaches
                                  generate_queue_handoff
  * flag_stale_dependents       — emits task_changed for changed dependents

Run:  python3 -m unittest test_agent_contract -v
"""
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import agent_chats
import agent_contract
import agent_db
import agent_events
import agent_executor
import agent_overseer

# Real Flask test-client seam for the endpoint tests (the same mixin
# test_agent_api_authz.py builds its two-tenant fixtures on).
from test_agent_api_authz import _TwoTenantApp


class _TempDbCase(unittest.TestCase):
    """Shared seam (test_agent_executor.py:23-52): temp central DB + project."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self._db_path_patch = patch.object(
            agent_db, 'DB_PATH', os.path.join(self.td.name, 'aingel.db'))
        self._db_path_patch.start()
        agent_db.init_db()
        self.path = os.path.join(self.td.name, 'proj')
        os.makedirs(self.path, exist_ok=True)
        proj = agent_db.upsert_project('Proj', 'proj', self.path)
        self.proj_id = proj['id']

    def tearDown(self):
        self._db_path_patch.stop()
        self.td.cleanup()

    def _task_with_dep(self, dep_status='pending'):
        """A confirmed task depending on one dep in the given status."""
        dep_id = agent_db.create_task(self.proj_id, 'Dep task',
                                      description='upstream',
                                      project_path=self.path)
        if dep_status != 'pending':
            agent_db.update_task(dep_id, status=dep_status, project_path=self.path)
        task_id = agent_db.create_task(self.proj_id, 'Blocked task',
                                       description='downstream',
                                       project_path=self.path)
        agent_db.update_task(task_id, status='confirmed', project_path=self.path)
        agent_db.add_dependency(task_id, dep_id)
        return task_id, dep_id


class AgentContractCheckTests(_TempDbCase):
    """agent_contract.check — the deterministic deps-done rule."""

    def test_unmet_cross_project_dep_detected(self):
        # Dependencies are central (aingel.db) and cross-project by design:
        # the dep may live in another project entirely.
        path_b = os.path.join(self.td.name, 'proj-b')
        os.makedirs(path_b, exist_ok=True)
        proj_b = agent_db.upsert_project('Other', 'other', path_b)
        dep_id = agent_db.create_task(proj_b['id'], 'Cross dep',
                                      project_path=path_b)
        task_id = agent_db.create_task(self.proj_id, 'Local',
                                       project_path=self.path)
        agent_db.add_dependency(task_id, dep_id)

        result = agent_contract.check({'id': task_id})

        self.assertFalse(result['ok'])
        self.assertEqual(len(result['unmet']), 1)
        entry = result['unmet'][0]
        # Frozen entry shape — trimmed to the four contract fields so a full
        # task row never leaks into an API payload.
        self.assertEqual(set(entry), {'id', 'display_code', 'title', 'status'})
        self.assertEqual(entry['id'], dep_id)
        self.assertEqual(entry['title'], 'Cross dep')
        self.assertEqual(entry['status'], 'pending')

    def test_done_dep_passes(self):
        task_id, _dep = self._task_with_dep(dep_status='done')
        result = agent_contract.check({'id': task_id})
        self.assertTrue(result['ok'])
        self.assertEqual(result['unmet'], [])

    def test_garbage_dep_project_fail_open(self):
        # A dep whose project folder is gone: get_project_db cannot even open
        # a database file there → sqlite3.OperationalError inside
        # get_dependencies. The checker must swallow it — a broken dep record
        # must never hard-block run_task on a task with no other problem.
        task_id, dep_id = self._task_with_dep()
        gone = os.path.join(self.td.name, 'no', 'such', 'dir')
        conn = agent_db.get_db()
        conn.execute('UPDATE task_registry SET project_path=? WHERE id=?',
                     (gone, dep_id))
        conn.commit()
        conn.close()

        result = agent_contract.check({'id': task_id})

        self.assertTrue(result['ok'])
        self.assertEqual(result['unmet'], [])


class EntryGateTests(_TempDbCase):
    """run_task refuses a dep-blocked task before any state write."""

    def test_unmet_dep_refuses_without_side_effects(self):
        # Quotas must be live for the "run not consumed" assertion: auth on,
        # project owned by a free-tier member (the first-ever user is the
        # admin, so the owner is the second one created).
        saved_auth = os.environ.get('AINGEL_AUTH')
        os.environ['AINGEL_AUTH'] = 'oidc'
        try:
            agent_db.get_or_create_user('sub-admin', 'admin@test', 'Admin')
            owner = agent_db.get_or_create_user('sub-free', 'free@test', 'Free')
            agent_db.set_project_owner(self.proj_id, owner['id'])
            task_id, dep_id = self._task_with_dep()
            agent_db.update_task(task_id, created_by=owner['id'],
                                 project_path=self.path)

            # emit_global_safe reaches every subscriber queue.
            q = agent_events.subscribe(0)
            try:
                result = agent_executor.run_task(task_id)
            finally:
                agent_events.unsubscribe(0, q)
        finally:
            if saved_auth is None:
                os.environ.pop('AINGEL_AUTH', None)
            else:
                os.environ['AINGEL_AUTH'] = saved_auth

        dep = agent_db.get_task(dep_id, project_path=self.path)
        # Frozen refusal dict (contract item 1).
        self.assertEqual(result.get('status'), 'dep_blocked')
        self.assertEqual(result.get('task_id'), task_id)
        self.assertEqual(result.get('error'),
                         f"Blocked by unmet dependencies: {dep['display_code']} (pending)")
        unmet = result.get('unmet')
        self.assertEqual(len(unmet), 1)
        self.assertEqual(unmet[0]['id'], dep_id)
        self.assertEqual(unmet[0]['status'], 'pending')
        # SSE global event (contract item 3).
        event = q.get(timeout=5)
        self.assertEqual(event.get('type'), 'dep_blocked')
        self.assertEqual(event.get('task_id'), task_id)
        self.assertEqual(event.get('unmet'), unmet)
        # Zero side effects: task stays confirmed, no execution row, and the
        # free-tier run counter was never touched (check_and_increment_run
        # sits ~90 lines below the gate).
        self.assertEqual(agent_db.get_task(task_id, project_path=self.path)['status'],
                         'confirmed')
        self.assertEqual(agent_db.get_executions(), [])
        import agent_quotas
        self.assertEqual(agent_db.get_usage(owner['id'],
                                            agent_quotas._current_month())['runs'], 0)

    def test_resume_bypasses_the_gate(self):
        # Decision 5 (2026-10-17): deps were validated when the run started —
        # a resumed run must finish, not re-litigate admission.
        task_id, _dep = self._task_with_dep()
        agent_db.update_task(task_id, status='paused', project_path=self.path)
        with patch.object(agent_contract, 'check',
                          side_effect=AssertionError('gate must not run on resume')) as m:
            result = agent_executor.run_task(task_id, resume={
                'exec_id': 999999, 'checkpoint': {'transcript': []},
                'finalize': True})
        m.assert_not_called()
        # The run reached the resume machinery (past the gate) and failed on
        # the nonexistent paused execution — proving the gate was skipped,
        # not that the run never started.
        self.assertEqual(result.get('status'), 'failed')
        self.assertIn('no longer paused', result.get('error', ''))

    def test_all_deps_done_passes_the_gate(self):
        # No regression: a green task runs the normal path (mocked model).
        task_id, _dep = self._task_with_dep(dep_status='done')
        built = {'mode': 'inline', 'prompt': 'do it', 'max_tokens': 100}
        with patch.object(agent_executor.agit, 'resolve_enabled', return_value=False), \
             patch('agent_scw_session.sync_bucket_to_working_docs', return_value=None), \
             patch('prompt_builder.build', return_value=built), \
             patch.object(agent_executor, 'route',
                          return_value=('Deliverable text', 1, 2, 0.001)), \
             patch.object(agent_executor, '_generate_brief_async',
                          lambda *a, **k: None):
            result = agent_executor.run_task(task_id)
        self.assertEqual(result.get('status'), 'done')
        execs = agent_db.get_executions()
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0]['status'], 'done')
        self.assertEqual(agent_db.get_task(task_id, project_path=self.path)['status'],
                         'done')


class H2StepZeroTests(unittest.TestCase):
    """pre_run_check(dep_unmet=...) — deterministic hold, no LLM call."""

    DEP_UNMET = [
        {'id': 11, 'display_code': 'RP-077', 'title': 'Transport plan',
         'status': 'pending'},
        # No display_code → the warning falls back to the title.
        {'id': 12, 'display_code': None, 'title': 'Untitled dep',
         'status': 'failed'},
    ]

    def test_dep_unmet_holds_immediately_without_llm(self):
        # _ask must not even be reachable: no model call may get the chance
        # to wave a deterministically-blocked task through.
        with patch.object(agent_overseer, '_ask',
                          side_effect=AssertionError('_ask must not be called')) as m:
            h2 = agent_overseer.pre_run_check(
                {'id': 1, 'title': 't', 'description': 'x'},
                {'name': 'P', 'path': '.', 'aingel_model': 'mistral-medium-latest'},
                [], [], {'file_access': 'native'}, [],
                dep_unmet=self.DEP_UNMET)
        m.assert_not_called()
        self.assertEqual(h2.get('gate'), 'hold')
        self.assertEqual(h2.get('completeness'), 'incomplete')
        self.assertEqual(h2.get('reason'),
                         'Dependency RP-077 (pending) is not done')
        self.assertEqual(h2.get('capability_warnings'), [
            'Dependency RP-077 is not done',
            'Dependency Untitled dep is not done',
        ])
        self.assertEqual(h2.get('dep_status'), self.DEP_UNMET)
        # Every key callers read on the normal path stays present
        # (run_task dumps this dict into gate_report_h2_json).
        self.assertEqual(h2.get('missing_files'), [])
        self.assertEqual(h2.get('binary_unparsable'), [])
        self.assertEqual(h2.get('dep_blockers'), [])
        self.assertEqual(h2.get('self_serve'), [])
        self.assertEqual(h2.get('output_targets'), [])
        self.assertEqual(h2.get('strategic_advice'), '')


class GuideNoticeTests(_TempDbCase):
    """The refusal posts a 🔔 Notice into the project's Guide chat."""

    def test_refusal_posts_notice_and_creates_no_execution(self):
        task_id, dep_id = self._task_with_dep()
        chat = agent_db.create_chat(self.proj_id, '❆ Guide',
                                    model='mistral-small-latest',
                                    project_path=self.path)
        conn = agent_db.get_db()
        conn.execute('UPDATE projects SET aingel_chat_id=? WHERE id=?',
                     (chat['id'], self.proj_id))
        conn.commit()
        conn.close()

        result = agent_executor.run_task(task_id)
        self.assertEqual(result.get('status'), 'dep_blocked')

        # The notice landed in the chat file as a 🔔 Notice (never as a
        # 👤 User turn — see agent_chats.NOTICE_HEADER).
        full = agent_db.get_chat(chat['id'])
        chat_path = full.get('file_path')
        self.assertTrue(chat_path and os.path.exists(chat_path))
        with open(chat_path, encoding='utf-8') as f:
            content = f.read()
        self.assertIn(agent_chats.NOTICE_HEADER, content)
        self.assertIn('was not run', content)
        dep = agent_db.get_task(dep_id, project_path=self.path)
        self.assertIn(f"blocked by dependency {dep['display_code']}", content)
        self.assertIn('Run the dependency first, or remove the dependency.',
                      content)

        # No execution row at all — neither for the task nor for the chat
        # (chat_id IS NULL invariant: a posted notice must not create one).
        pconn = agent_db.get_project_db(self.path)
        n = pconn.execute('SELECT COUNT(*) FROM executions').fetchone()[0]
        pconn.close()
        self.assertEqual(n, 0)
        self.assertEqual(agent_db.get_executions(), [])


class DependenciesEndpointTests(_TwoTenantApp, unittest.TestCase):
    """POST /api/tasks/<tid>/dependencies — acknowledge-at-save (409)."""

    def test_not_done_dep_without_acknowledge_is_409(self):
        self._login(self.user_a)
        resp = self.client.post(
            f'/api/tasks/{self.task_a}/dependencies',
            json={'depends_on_id': self.task_b})
        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        self.assertEqual(body.get('error'), 'dependency_not_done')
        unmet = body.get('unmet')
        self.assertEqual(len(unmet), 1)
        self.assertEqual(unmet[0]['id'], self.task_b)
        self.assertEqual(unmet[0]['status'], 'pending')
        self.assertEqual(set(unmet[0]), {'id', 'display_code', 'title', 'status'})
        # Nothing was saved — the user must retry with the acknowledge flag.
        self.assertEqual(agent_db.get_dependencies(self.task_a), [])

    def test_not_done_dep_with_acknowledge_creates(self):
        self._login(self.user_a)
        resp = self.client.post(
            f'/api/tasks/{self.task_a}/dependencies',
            json={'depends_on_id': self.task_b, 'acknowledged': True})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json(), {'ok': True})
        self.assertEqual([d['id'] for d in agent_db.get_dependencies(self.task_a)],
                         [self.task_b])

    def test_done_dep_creates_without_acknowledge(self):
        agent_db.update_task(self.task_b, status='done', project_path=self.path_b)
        self._login(self.user_a)
        resp = self.client.post(
            f'/api/tasks/{self.task_a}/dependencies',
            json={'depends_on_id': self.task_b})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual([d['id'] for d in agent_db.get_dependencies(self.task_a)],
                         [self.task_b])


class PatchAssignWarningTests(_TwoTenantApp, unittest.TestCase):
    """PATCH /api/tasks/<tid> slot-assign — warning (contract item 4)."""

    def _slot_assign(self, tid, slot=1):
        self._login(self.user_a)
        return self.client.patch(f'/api/tasks/{tid}',
                                 json={'work_session_slot': slot})

    def test_assigning_blocked_task_returns_warning(self):
        dep_id = agent_db.create_task(
            self.proj_a['id'], 'Pending dep', description='upstream',
            project_path=self.path_a)
        blocked = agent_db.create_task(
            self.proj_a['id'], 'Blocked', description='downstream work',
            project_path=self.path_a)
        agent_db.add_dependency(blocked, dep_id)

        resp = self._slot_assign(blocked)

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        # Assignment proceeds exactly as today: status confirmed, slot set.
        task = agent_db.get_task(blocked, project_path=self.path_a)
        self.assertEqual(task['status'], 'confirmed')
        self.assertEqual(task['work_session_slot'], 1)
        # …and the response carries the dep-blocked warning.
        warning = body.get('warning') or {}
        self.assertEqual(warning.get('type'), 'dep_blocked')
        unmet = warning.get('unmet') or []
        self.assertEqual([u['id'] for u in unmet], [dep_id])
        self.assertEqual(unmet[0]['status'], 'pending')

    def test_assigning_unblocked_task_has_no_warning(self):
        tid = agent_db.create_task(
            self.proj_a['id'], 'Free', description='no deps',
            project_path=self.path_a)
        resp = self._slot_assign(tid)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('warning', resp.get_json())


class RunSeqTOCTOUTests(_TempDbCase):
    """_run_seq: a runtime dep_blocked result must not become a handoff."""

    def test_dep_blocked_result_never_reaches_generate_queue_handoff(self):
        # Two confirmed slot-2 tasks with NO deps in the DB: the pre-skip
        # passes them; the mocked run_task refuses "at runtime" — the
        # redo-during-run race the pre-skip cannot see (a dep was redone
        # while the task waited on the project lock).
        ids = []
        for i in (1, 2):
            tid = agent_db.create_task(self.proj_id, f'T{i}',
                                       description='x', project_path=self.path)
            agent_db.update_task(tid, status='confirmed', work_session_slot=2,
                                 project_path=self.path)
            ids.append(tid)

        dep_blocked = {'status': 'dep_blocked', 'task_id': ids[0],
                       'error': 'Blocked by unmet dependencies: RP-077 (pending)',
                       'unmet': [{'id': 4242, 'display_code': 'RP-077',
                                  'title': 'd', 'status': 'pending'}]}
        # Imported lazily like test_agent_api_authz does — importing the
        # server module at test-module scope has import-time side effects.
        import agent_api
        with patch.object(agent_api, 'run_task', return_value=dep_blocked) as run_mock, \
             patch.object(agent_api, 'generate_queue_handoff') as handoff:
            ok, _msg = agent_api._kick_run_seq(2, ids)
            self.assertTrue(ok)
            # Wait for the runner thread to drain: it pops the slot in its
            # finally block, so the assertions below cannot race the thread.
            deadline = time.time() + 10
            while time.time() < deadline:
                with agent_api._running_slots_lock:
                    running = agent_api._running_slots.get(2)
                if running is None:
                    break
                time.sleep(0.02)
            else:
                self.fail('slot runner thread did not finish within 10s')
        # The refusal is recorded as a skip — feeding it to the handoff
        # generator would make a real mistral-medium-latest call on garbage.
        handoff.assert_not_called()
        # Skip semantics match the pre-skip: the queue CONTINUES past a
        # dep_blocked task instead of breaking out of the column run.
        self.assertEqual(run_mock.call_count, 2)


class FlagStaleDependentsTests(_TempDbCase):
    """flag_stale_dependents pushes task_changed SSE for changed dependents."""

    def test_changed_dependent_gets_task_changed_sse(self):
        dep = agent_db.create_task(self.proj_id, 'Dep', project_path=self.path)
        dependent = agent_db.create_task(self.proj_id, 'Dependent',
                                         project_path=self.path)
        agent_db.update_task(dependent, status='confirmed', project_path=self.path)
        agent_db.add_dependency(dependent, dep)

        q = agent_events.subscribe(self.proj_id)
        try:
            changed = agent_db.flag_stale_dependents(dep)
            self.assertEqual(changed, 1)
            event = q.get(timeout=5)
        finally:
            agent_events.unsubscribe(self.proj_id, q)

        self.assertEqual(event, {'type': 'task_changed', 'task_id': dependent})
        self.assertEqual(
            agent_db.get_task(dependent, project_path=self.path).get('stale'), 1)

    def test_cross_project_dependent_emits_on_its_own_project(self):
        # Deps are cross-project: the dependent may live in another project,
        # whose board must still hear about the stale flag.
        path_b = os.path.join(self.td.name, 'proj-b')
        os.makedirs(path_b, exist_ok=True)
        proj_b = agent_db.upsert_project('Other', 'other', path_b)
        dep = agent_db.create_task(self.proj_id, 'Dep', project_path=self.path)
        dependent = agent_db.create_task(proj_b['id'], 'Dependent',
                                         project_path=path_b)
        agent_db.update_task(dependent, status='confirmed', project_path=path_b)
        agent_db.add_dependency(dependent, dep)

        qb = agent_events.subscribe(proj_b['id'])
        try:
            changed = agent_db.flag_stale_dependents(dep)
            self.assertEqual(changed, 1)
            event = qb.get(timeout=5)
        finally:
            agent_events.unsubscribe(proj_b['id'], qb)

        self.assertEqual(event, {'type': 'task_changed', 'task_id': dependent})


class ColumnBusyMessageTests(_TempDbCase):
    """_kick_run_seq: a busy-column refusal names the task holding it,
    falling back to the generic form when the holder can't be resolved."""

    def test_busy_kick_names_holder_task_and_project(self):
        # Imported lazily like test_agent_api_authz does — importing the
        # server module at test-module scope has import-time side effects.
        import agent_api
        ids = []
        for i in (1, 2):
            tid = agent_db.create_task(self.proj_id, f'T{i}',
                                       description='x', project_path=self.path)
            agent_db.update_task(tid, status='confirmed', work_session_slot=4,
                                 project_path=self.path)
            ids.append(tid)
        holder_id, other_id = ids

        release = threading.Event()
        # Hygiene: unblock the daemon runner even if an assertion fails —
        # a stuck thread would hold slot 4 and poison later tests.
        self.addCleanup(release.set)

        def blocked_run(task_id=None, resume=None):
            release.wait(timeout=10)
            return {'status': 'done', 'exec_id': 1, 'task_id': task_id}

        with patch.object(agent_api, 'run_task', side_effect=blocked_run) as run_mock, \
             patch.object(agent_api, 'generate_queue_handoff'):
            ok, _msg = agent_api._kick_run_seq(4, [holder_id])
            self.assertTrue(ok)
            # The runner sets 'current' right before run_task; poll for it so
            # the busy-refusal below deterministically names the holder.
            deadline = time.time() + 5
            while time.time() < deadline:
                with agent_api._running_slots_lock:
                    entry = agent_api._running_slots.get(4) or {}
                if entry.get('current') == holder_id:
                    break
                time.sleep(0.02)
            else:
                self.fail('slot 4 runner never registered the holder task')

            result = agent_api._kick_run_seq(4, [other_id])
            self.assertEqual(len(result), 2)
            ok2, msg = result
            self.assertFalse(ok2)
            holder = agent_db.get_task(holder_id)
            detail = holder.get('display_code') or f"#{holder['id']}"
            self.assertIn(detail, msg)
            self.assertIn('Proj', msg)
            self.assertIn('EU Scaleway', msg)
            self.assertIn('is already running', msg)
            # Assertions done — unblock the runner so the drain below is
            # deterministic (addCleanup only covers the failure paths).
            release.set()
        # Drain the runner thread: it pops the slot in its finally block, so
        # a still-registered slot would leak into later tests.
        deadline = time.time() + 10
        while time.time() < deadline:
            with agent_api._running_slots_lock:
                running = agent_api._running_slots.get(4)
            if running is None:
                break
            time.sleep(0.02)
        else:
            self.fail('slot runner thread did not finish within 10s')
        self.assertEqual(run_mock.call_count, 1)

    def test_busy_kick_generic_when_holder_unresolvable(self):
        import agent_api
        tid = agent_db.create_task(self.proj_id, 'Kicker',
                                   description='x', project_path=self.path)
        # A holder id that cannot be resolved to a task (registry miss /
        # missing project.db) must yield the generic refusal, not a crash.
        with agent_api._running_slots_lock:
            agent_api._running_slots[4] = {
                'thread': agent_api._RESERVED, 'started': time.time(),
                'task_ids': [999999001], 'current': 999999001}
        try:
            result = agent_api._kick_run_seq(4, [tid])
            self.assertEqual(len(result), 2)
            ok, msg = result
            self.assertFalse(ok)
            self.assertEqual(msg, 'Column 4 (EU Scaleway) is already running')
        finally:
            with agent_api._running_slots_lock:
                agent_api._running_slots.pop(4, None)


class GateOverrideLatchTests(_TwoTenantApp, unittest.TestCase):
    """POST /api/tasks/<tid>/gate — the one-shot 'user-override' latch.

    The executor's verified-impossibility pre-flight guards bypass once on
    gate_source == 'user-override' and then reset it to 'H2'. These tests pin
    the API side: override stamps the latch, acknowledge stamps a source that
    does NOT bypass, answer clears it, and a content PATCH clears a stale one.
    """

    def _gate(self, tid, **body):
        self._login(self.user_a)
        return self.client.post(f'/api/tasks/{tid}/gate', json=body)

    def _latch_task(self, desc='do the thing'):
        tid = agent_db.create_task(self.proj_a['id'], 'Gated', description=desc,
                                   project_path=self.path_a)
        agent_db.update_task(tid, project_path=self.path_a, gate_state='hold',
                             gate_source='H2', gate_reason='missing context')
        return tid

    def test_override_stamps_consumable_latch(self):
        tid = self._latch_task()
        with patch.object(self.agent_api.db, 'update_task') as m:
            resp = self._gate(tid, action='override')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['gate_state'], 'open')
        self.assertEqual(body['gate_source'], 'user-override')
        _, kwargs = m.call_args
        self.assertEqual(kwargs['gate_state'], 'open')
        self.assertEqual(kwargs['gate_source'], 'user-override')
        self.assertEqual(kwargs['gate_reason'], 'User overrode the pre-run gate')
        self.assertIn('gate_decided_at', kwargs)

    def test_acknowledge_closes_without_bypass_latch(self):
        tid = self._latch_task()
        with patch.object(self.agent_api.db, 'update_task') as m:
            resp = self._gate(tid, action='acknowledge')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['gate_state'], 'closed')
        self.assertEqual(resp.get_json()['gate_source'], 'user-ack')
        _, kwargs = m.call_args
        self.assertEqual(kwargs['gate_state'], 'closed')
        # Only 'user-override' bypasses pre-flight guards.
        self.assertEqual(kwargs['gate_source'], 'user-ack')
        self.assertEqual(kwargs['gate_reason'],
                         'User acknowledged the pre-run warning')

    def test_answer_clears_stale_latch(self):
        tid = self._latch_task()
        agent_db.update_task(tid, project_path=self.path_a,
                             gate_source='user-override')
        resp = self._gate(tid, action='answer',
                          answers={'q1': 'use the second folder'})
        self.assertEqual(resp.status_code, 200)
        task = agent_db.get_task(tid, project_path=self.path_a)
        self.assertEqual(task['gate_state'], 'open')
        self.assertEqual(task['gate_source'], '')
        self.assertIn('use the second folder', task['description'])

    def test_discuss_does_not_touch_the_latch(self):
        tid = self._latch_task()
        resp = self._gate(tid, action='discuss')
        self.assertEqual(resp.status_code, 200)
        task = agent_db.get_task(tid, project_path=self.path_a)
        self.assertEqual(task['gate_state'], 'hold')
        self.assertEqual(task['gate_source'], 'H2')

    def test_content_patch_clears_stale_override_latch(self):
        tid = self._latch_task()
        agent_db.update_task(tid, project_path=self.path_a, gate_state='open',
                             gate_source='user-override',
                             gate_reason='User overrode the pre-run gate')
        self._login(self.user_a)
        resp = self.client.patch(f'/api/tasks/{tid}',
                                 json={'description': 'a new, clearer ask'})
        self.assertEqual(resp.status_code, 200)
        task = agent_db.get_task(tid, project_path=self.path_a)
        self.assertEqual(task['gate_source'], '')
        self.assertEqual(task['gate_reason'], '')
        self.assertEqual(task['description'], 'a new, clearer ask')

    def test_patch_that_sets_gate_columns_is_not_overridden(self):
        tid = self._latch_task()
        self._login(self.user_a)
        resp = self.client.patch(f'/api/tasks/{tid}',
                                 json={'gate_state': 'open',
                                       'gate_source': 'user-override'})
        self.assertEqual(resp.status_code, 200)
        task = agent_db.get_task(tid, project_path=self.path_a)
        self.assertEqual(task['gate_source'], 'user-override')

    def test_h2a_review_patch_is_still_409(self):
        # The latch-clearing must not disturb the H2a 409 guard.
        tid = self._latch_task()
        agent_db.update_task(tid, project_path=self.path_a, gate_state='hold',
                             gate_source='H2a')
        self._login(self.user_a)
        resp = self.client.patch(f'/api/tasks/{tid}',
                                 json={'description': 'sneaky edit'})
        self.assertEqual(resp.status_code, 409)
        task = agent_db.get_task(tid, project_path=self.path_a)
        self.assertEqual(task['gate_source'], 'H2a')


class H2aRouteQuestionTests(unittest.TestCase):
    """_h2a_route_questions — deterministic route blindness check (10001296)."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = self.td.name
        os.makedirs(os.path.join(self.path, 'Sources'), exist_ok=True)

    def _ask(self, desc, facts, existing=None):
        import agent_api
        return agent_api._h2a_route_questions(desc, self.path, facts, existing)

    def test_url_with_no_web_access_injects_question(self):
        out = self._ask('Summarise https://example.com/report and cite it.',
                        {'web': False, 'file_tools': True, 'route': 'batch'})
        self.assertEqual(len(out), 1)
        q = out[0]
        self.assertEqual(q['id'], 'q1')
        self.assertIn('https://example.com/report', q['question'])
        self.assertIn('batch', q['question'])
        self.assertIn('Switch to an agentic route', q['options'])
        self.assertTrue(q['why'])

    def test_existing_folder_with_no_file_tools_injects_question(self):
        out = self._ask('Read every PDF in the folder "Sources" and summarise.',
                        {'web': True, 'file_tools': False, 'route': 'batch'})
        self.assertEqual(len(out), 1)
        self.assertIn('folder "Sources"', out[0]['question'])

    def test_absent_folder_is_ignored(self):
        out = self._ask('Read the folder "Nowhere" and summarise.',
                        {'web': False, 'file_tools': False})
        self.assertEqual(out, [])

    def test_capable_route_injects_nothing(self):
        out = self._ask('Summarise https://example.com and the folder "Sources".',
                        {'web': True, 'file_tools': True, 'route': 'inline'})
        self.assertEqual(out, [])

    def test_unknown_route_facts_inject_nothing(self):
        self.assertEqual(self._ask('Summarise https://example.com', None), [])
        self.assertEqual(self._ask('Summarise https://example.com', {}), [])

    def test_existing_question_about_the_url_is_not_duplicated(self):
        existing = [{'id': 'q1', 'question': 'Is https://example.com the right source?',
                     'why': '', 'options': []}]
        out = self._ask('Summarise https://example.com', {'web': False}, existing)
        self.assertEqual(len(out), 1)

    def test_respects_three_question_cap(self):
        existing = [{'id': f'q{i}', 'question': f'q{i}?', 'why': '', 'options': []}
                    for i in (1, 2, 3)]
        out = self._ask('Summarise https://example.com', {'web': False}, existing)
        self.assertEqual(len(out), 3)

    def test_appends_after_existing_questions(self):
        existing = [{'id': 'q1', 'question': 'Which section?', 'why': '', 'options': []}]
        out = self._ask('Summarise https://example.com', {'web': False}, existing)
        self.assertEqual([q['id'] for q in out], ['q1', 'q2'])

    def test_already_answered_route_question_is_not_reasked(self):
        desc = ('Summarise https://example.com\n\n'
                '**User clarifications (Guide pre-run check):**\n'
                '- q1: Switch to an agentic route')
        out = self._ask(desc, {'web': False})
        self.assertEqual(out, [])


class H2aRouteFactsWiringTests(_TempDbCase):
    """_run_h2a_precheck_async passes route facts to the overseer."""

    def test_route_facts_passed_and_injected_question_holds(self):
        import agent_api
        import agent_executor
        import agent_overseer
        agent_db.set_project_field(self.proj_id, 'aingel_autopilot', 1)
        tid = agent_db.create_task(
            self.proj_id, 'Route test',
            description='Summarise https://example.com/report',
            project_path=self.path)
        seen = {}

        def fake_pre_check(task, project, file_names, **kwargs):
            seen.update(kwargs)
            return {'questions': [], 'gate_state': 'open', 'reason': 'clear'}

        facts = {'web': False, 'file_tools': False, 'route': 'batch'}
        with patch.object(agent_executor, '_task_route_facts',
                          return_value=facts) as rf, \
             patch.object(agent_overseer, 'pre_check_questions',
                          side_effect=fake_pre_check):
            agent_api._run_h2a_precheck_async(tid, self.path)

        rf.assert_called_once()
        self.assertEqual(rf.call_args[0][1], 'inline')
        self.assertEqual(seen.get('route_facts'), facts)
        task = agent_db.get_task(tid, project_path=self.path)
        self.assertEqual(task['gate_state'], 'hold')
        self.assertEqual(task['gate_source'], 'H2a')

    def test_missing_route_facts_helper_is_tolerated(self):
        import agent_api
        import agent_executor
        import agent_overseer
        agent_db.set_project_field(self.proj_id, 'aingel_autopilot', 1)
        tid = agent_db.create_task(self.proj_id, 'No facts',
                                   description='plain work',
                                   project_path=self.path)
        seen = {}

        def fake_pre_check(task, project, file_names, **kwargs):
            seen.update(kwargs)
            return {'questions': [], 'gate_state': 'open', 'reason': 'clear'}

        with patch.object(agent_executor, '_task_route_facts',
                          side_effect=ImportError('not yet')), \
             patch.object(agent_overseer, 'pre_check_questions',
                          side_effect=fake_pre_check):
            agent_api._run_h2a_precheck_async(tid, self.path)

        self.assertIn('route_facts', seen)
        self.assertIsNone(seen['route_facts'])
        task = agent_db.get_task(tid, project_path=self.path)
        self.assertEqual(task['gate_state'], 'open')


if __name__ == '__main__':
    unittest.main()
