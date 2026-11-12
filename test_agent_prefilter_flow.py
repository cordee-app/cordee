"""Tests for the local decision-model pre-filter wiring (plan v1.2).

Covers the two integration points owned by implementer C:

  • ``agent_overseer.pre_run_check`` (H2) — inject the rendered pre-filter
    block right after the Description and before the gate rules. Fresh,
    stale, unknown/NULL and flag-off payloads each keep their contract;
    H2a (``pre_check_questions``) never sees the block.
  • ``agent_executor.run_task`` — the bounded wait happens BEFORE the
    project lock, is skipped for dependency-blocked tasks, and the run path
    never computes.

``agent_decision.py`` is implemented in parallel, so every test installs a
stub module in ``sys.modules`` (``patch.dict``) instead of importing it.
No network and no live DB: reviewer calls go through a fake ``_ask`` and
``agent_executor.db`` is patched out.
"""
import json
import sys
import types
import unittest
from unittest.mock import patch

import agent_contract
import agent_overseer

import agent_executor

H2_REPLY = json.dumps({'completeness': 'complete', 'missing_files': [],
                       'binary_unparsable': [], 'capability_warnings': [],
                       'strategic_advice': 'clear', 'gate': 'run',
                       'reason': 'clear'})
H2A_REPLY = json.dumps({'questions': [], 'reason': 'clear'})

PREFILTER_BLOCK = ('- flags: requires_web_research, asks_delete_move\n'
                   '- review: route has no web access but the task cites a URL')
HEADER = agent_overseer._PREFILTER_H2_HEADER
NOT_AUTHORITY = agent_overseer._PREFILTER_H2_NOT_AUTHORITY


def _stub_decision(enabled=True, block=PREFILTER_BLOCK, stale=False):
    """Minimal stand-in for agent_decision (see /tmp/opencode spec)."""
    mod = types.ModuleType('agent_decision')
    mod.enabled = enabled
    mod.block = block
    mod.stale = stale
    mod.is_stale_calls = []
    mod.wait_calls = []

    mod.prefilter_enabled = lambda project_path=None: enabled

    def _render(payload):
        return block

    def _is_stale(payload, task, project=None, route_facts=None):
        mod.is_stale_calls.append(payload)
        return stale

    def _wait(task_id, max_s=20):
        mod.wait_calls.append((task_id, max_s))

    mod.render_h2_block = _render
    mod.is_stale = _is_stale
    mod.wait_for_prefilter = _wait
    return mod


def _task(**kw):
    base = {'id': 10001300, 'title': 'RP-130', 'model': 'm',
            'project_id': 7, 'path': '/tmp/aingel-prefilter-test',
            'description': 'Summarise the contract and name the output file.'}
    base.update(kw)
    return base


FRESH_PAYLOAD = json.dumps({
    'status': 'ok',
    'model_fast': 'tev1:0.8b',
    'flags': ['requires_web_research', 'asks_delete_move'],
    'llm_review': [],
    'state_hash': 'abc123',
    'qset_version': 1,
})


class H2PrefilterInjectionTests(unittest.TestCase):
    def setUp(self):
        self.prompts = []
        self.project = {'name': 'P', 'path': '/tmp/aingel-prefilter-test',
                        'aingel_model': 'm'}

    def _h2(self, task, stub):
        def fake_ask(model, prompt, budget, path, caller):
            self.prompts.append(prompt)
            return H2_REPLY, None

        with patch.dict(sys.modules, {'agent_decision': stub}), \
                patch.object(agent_overseer, '_ask', side_effect=fake_ask):
            result = agent_overseer.pre_run_check(
                task, self.project, caught_files=[], noted_binary=[],
                model_caps={}, dep_titles=[])
        return self.prompts[-1], result

    # ── fresh payload ──────────────────────────────────────────────────────
    def test_fresh_payload_injected_after_description_before_rules(self):
        task = _task(prefilter_json=FRESH_PAYLOAD)
        prompt, result = self._h2(task, _stub_decision())

        self.assertIn(PREFILTER_BLOCK, prompt)
        self.assertIn(HEADER, prompt)
        self.assertIn(NOT_AUTHORITY, prompt)
        # Immediately after the Description block, before the gate rules.
        self.assertLess(prompt.index('Description:'), prompt.index(HEADER))
        self.assertLess(prompt.index(HEADER), prompt.index('Rules for `gate`:'))
        self.assertEqual(result['gate'], 'run')
        # 'Stale dependencies:' is pre-existing prompt text — the pre-filter's
        # own stale marker must be absent.
        self.assertNotIn('[stale:', prompt.lower())

    def test_payload_as_dict_is_accepted(self):
        task = _task(prefilter_json=json.loads(FRESH_PAYLOAD))
        prompt, _ = self._h2(task, _stub_decision())
        self.assertIn(PREFILTER_BLOCK, prompt)

    # ── stale payload ──────────────────────────────────────────────────────
    def test_hash_drift_renders_stale(self):
        task = _task(prefilter_json=FRESH_PAYLOAD)
        prompt, _ = self._h2(task, _stub_decision(stale=True))
        self.assertIn(PREFILTER_BLOCK, prompt)   # still injected…
        self.assertIn('stale', prompt.lower())    # …but labelled

    def test_payload_marked_stale_renders_stale(self):
        payload = json.loads(FRESH_PAYLOAD)
        payload['stale'] = True
        task = _task(prefilter_json=json.dumps(payload))
        prompt, _ = self._h2(task, _stub_decision(stale=False))
        self.assertIn('stale', prompt.lower())

    # ── nothing appended: prompt byte-identical ────────────────────────────
    def test_absent_unknown_and_flag_off_leave_prompt_byte_identical(self):
        baseline, _ = self._h2(_task(), _stub_decision(enabled=True))

        variants = [
            # absent / NULL / blank
            _task(prefilter_json=None),
            _task(prefilter_json=''),
            _task(prefilter_json='null'),
            # stored failure payloads are not rendered
            _task(prefilter_json=json.dumps({'status': 'unknown',
                                             'error': 'ollama down'})),
            _task(prefilter_json=json.dumps({'status': 'skipped_trivial'})),
            # unparseable JSON
            _task(prefilter_json='{not valid json'),
        ]
        for task in variants:
            prompt, _ = self._h2(task, _stub_decision(enabled=True))
            self.assertEqual(prompt, baseline, msg=f'variant: {task!r}')

        # Valid fresh payload, but the project is not enabled.
        prompt, _ = self._h2(_task(prefilter_json=FRESH_PAYLOAD),
                             _stub_decision(enabled=False))
        self.assertEqual(prompt, baseline)

    def test_empty_render_appends_nothing(self):
        baseline, _ = self._h2(_task(), _stub_decision())
        prompt, _ = self._h2(_task(prefilter_json=FRESH_PAYLOAD),
                             _stub_decision(block=''))
        self.assertEqual(prompt, baseline)

    def test_decision_module_import_failure_is_silent(self):
        # No stub: a real agent_decision must not be required for H2 to run.
        baseline, _ = self._h2(_task(), _stub_decision())

        def fake_ask(model, prompt, budget, path, caller):
            self.prompts.append(prompt)
            return H2_REPLY, None

        with patch.dict(sys.modules, {'agent_decision': None}), \
                patch.object(agent_overseer, '_ask', side_effect=fake_ask):
            agent_overseer.pre_run_check(
                _task(prefilter_json=FRESH_PAYLOAD), self.project,
                caught_files=[], noted_binary=[], model_caps={},
                dep_titles=[])
        self.assertEqual(self.prompts[-1], baseline)

    # ── H2a stays untouched ────────────────────────────────────────────────
    def test_h2a_prompt_unchanged(self):
        def fake_ask(model, prompt, budget, path, caller):
            self.prompts.append(prompt)
            return H2A_REPLY, None

        task = _task(prefilter_json=FRESH_PAYLOAD)
        with patch.dict(sys.modules, {'agent_decision': _stub_decision()}), \
                patch.object(agent_overseer, '_ask', side_effect=fake_ask):
            result = agent_overseer.pre_check_questions(task, self.project, [])
        prompt = self.prompts[-1]
        self.assertNotIn(HEADER, prompt)
        self.assertNotIn(PREFILTER_BLOCK, prompt)
        self.assertNotIn('pre-filter', prompt.lower())
        self.assertEqual(result['gate_state'], 'open')

    # ── dep gate still short-circuits before any pre-filter work ───────────
    def test_dep_unmet_early_return_does_not_render(self):
        stub = _stub_decision()
        task = _task(prefilter_json=FRESH_PAYLOAD)
        with patch.dict(sys.modules, {'agent_decision': stub}), \
                patch.object(agent_overseer, '_ask',
                             side_effect=AssertionError('H2 must not call LLM')):
            result = agent_overseer.pre_run_check(
                task, self.project, caught_files=[], noted_binary=[],
                model_caps={}, dep_titles=[],
                dep_unmet=[{'id': 2, 'display_code': 'RP-2',
                            'title': 'dep', 'status': 'pending'}])
        self.assertEqual(result['gate'], 'hold')
        self.assertEqual(stub.is_stale_calls, [])


class RunTaskPrefilterWaitTests(unittest.TestCase):
    """The bounded wait must be skipped for dep-blocked runs and must happen
    BEFORE the project lock (plan §5.4; 20 s ceiling)."""

    def setUp(self):
        self.stub = _stub_decision()
        self.order = []

    def _fake_lock(self, *args, **kwargs):
        order = self.order

        class _Lock:
            def acquire(self, timeout=None):
                order.append('lock')
                return False

            def release(self):
                pass

        return _Lock()

    def _run_task(self, check_result):
        task = _task(status='confirmed')
        proj = {'id': 7, 'name': 'P', 'path': task['path'],
                'aingel_name': '', 'aingel_model': 'm',
                'aingel_autopilot': 0}

        def _wait(task_id, max_s=20):
            self.order.append('wait')
            self.stub.wait_calls.append((task_id, max_s))

        self.stub.wait_for_prefilter = _wait

        import agent_quotas
        with patch.dict(sys.modules, {'agent_decision': self.stub}), \
                patch.object(agent_executor.db, 'get_task', return_value=task), \
                patch.object(agent_executor.db, 'get_project', return_value=proj), \
                patch.object(agent_executor.db, 'get_project_budget',
                             return_value={}), \
                patch.object(agent_contract, 'check', return_value=check_result), \
                patch.object(agent_quotas, 'resolve_owner_id',
                             return_value=None), \
                patch.object(agent_executor, '_project_lock',
                             side_effect=self._fake_lock):
            return agent_executor.run_task(task_id=task['id'])

    def test_dep_blocked_run_does_not_wait(self):
        unmet = [{'id': 2, 'display_code': 'RP-2', 'title': 'dep',
                  'status': 'pending'}]
        result = self._run_task({'ok': False, 'unmet': unmet})
        self.assertEqual(result['status'], 'dep_blocked')
        self.assertEqual(self.stub.wait_calls, [])
        self.assertNotIn('wait', self.order)

    def test_wait_happens_before_project_lock(self):
        result = self._run_task({'ok': True, 'unmet': []})
        # The fake lock refuses, so the run stops right after the wait.
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.order, ['wait', 'lock'])
        self.assertEqual(self.stub.wait_calls, [(10001300, 20)])

    def test_helper_skips_wait_when_deps_unmet(self):
        # Belt-and-braces: even at the pre-lock point, unmet deps pay nothing.
        unmet = [{'id': 2, 'display_code': 'RP-2', 'title': 'dep',
                  'status': 'pending'}]
        with patch.dict(sys.modules, {'agent_decision': self.stub}), \
                patch.object(agent_contract, 'check',
                             return_value={'ok': False, 'unmet': unmet}):
            agent_executor._prefilter_wait_before_lock(_task(), '/tmp/p')
        self.assertEqual(self.stub.wait_calls, [])

    def test_helper_waits_when_enabled_and_deps_ok(self):
        with patch.dict(sys.modules, {'agent_decision': self.stub}), \
                patch.object(agent_contract, 'check',
                             return_value={'ok': True, 'unmet': []}):
            agent_executor._prefilter_wait_before_lock(_task(), '/tmp/p')
        self.assertEqual(self.stub.wait_calls, [(10001300, 20)])

    def test_helper_flag_off_does_not_wait(self):
        stub = _stub_decision(enabled=False)
        with patch.dict(sys.modules, {'agent_decision': stub}), \
                patch.object(agent_contract, 'check',
                             return_value={'ok': True, 'unmet': []}):
            agent_executor._prefilter_wait_before_lock(_task(), '/tmp/p')
        self.assertEqual(stub.wait_calls, [])


if __name__ == '__main__':
    unittest.main()
