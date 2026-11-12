"""Tests for the verified-impossibility pre-flight guards (task 10001296).

Covers:
  • ``agent_executor._task_route_facts`` — what each route can do;
  • ``agent_executor._preflight_guards`` — web / folder holds, deliverable warning;
  • the route line + conditional folder rule in the H2 / H2a reviewer prompts;
  • the one-shot ``user-override`` latch.

No network and no live DB: every reviewer call goes through a fake ``_ask``
and ``agent_executor.db.*`` is patched out.
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import agent_config
import agent_db
import agent_executor
import agent_overseer


def _task(**kw):
    base = {'id': 10001296, 'title': 'RP-129-E1', 'project_id': 7,
            'model': 'mistral-large-4', 'work_session_slot': 2,
            'description': ''}
    base.update(kw)
    return base


class TaskRouteFactsTests(unittest.TestCase):
    def test_batch_is_always_toolless(self):
        facts = agent_executor._task_route_facts(
            _task(model='mistral-large-4', work_session_slot=2), 'batch')
        self.assertEqual(facts['mode'], 'batch')
        self.assertFalse(facts['file_tools'])
        self.assertFalse(facts['web'])
        self.assertIn('batch', facts['route'])
        # Even a Scaleway tool model gets no tools in batch mode.
        facts = agent_executor._task_route_facts(
            _task(model='scw-qwen3.8-27b', work_session_slot=4), 'batch')
        self.assertFalse(facts['file_tools'])
        self.assertFalse(facts['web'])

    def test_slot2_mistral_vibe_is_agentic(self):
        with patch.object(agent_config, 'MISTRAL_MODE', 'vibe'), \
                patch.object(agent_config, 'VIBE_MODELS', ['mistral-large-4']):
            facts = agent_executor._task_route_facts(
                _task(model='mistral-large-4', work_session_slot=2), 'inline')
        self.assertTrue(facts['file_tools'])
        self.assertTrue(facts['web'])
        self.assertIn('vibe', facts['route'])

    def test_mistral_api_is_text_only(self):
        with patch.object(agent_config, 'MISTRAL_MODE', 'api'):
            with patch.object(agent_db, 'get_project_permissions', return_value={}):
                facts = agent_executor._task_route_facts(
                    _task(model='mistral-large-4', work_session_slot=None), 'inline')
            self.assertFalse(facts['file_tools'])
            self.assertFalse(facts['web'])
            self.assertIn('Mistral API', facts['route'])
            perms = {'F_network': [{'rule': 'example.com', 'enabled': True}]}
            with patch.object(agent_db, 'get_project_permissions', return_value=perms):
                facts = agent_executor._task_route_facts(
                    _task(model='mistral-large-4', work_session_slot=None), 'inline')
            self.assertFalse(facts['file_tools'])
            self.assertTrue(facts['web'])

    def test_scaleway_slot4_is_agentic(self):
        facts = agent_executor._task_route_facts(
            _task(model='scw-qwen3.8-27b', work_session_slot=4), 'inline')
        self.assertTrue(facts['file_tools'])
        self.assertTrue(facts['web'])

    def test_image_inputs_are_route_specific(self):
        # Batch: no tools at all — never delivers an image.
        facts = agent_executor._task_route_facts(
            _task(model='scw-qwen3.8-27b', work_session_slot=4), 'batch')
        self.assertFalse(facts['image_inputs'])
        # Scaleway/Ollama tool loop: read_file → image part.
        facts = agent_executor._task_route_facts(
            _task(model='scw-qwen3.8-27b', work_session_slot=4), 'inline')
        self.assertTrue(facts['image_inputs'])
        # Mistral/Vibe: file tools, but read_file returns bytes-as-text.
        facts = agent_executor._task_route_facts(
            _task(model='mistral-large-4', work_session_slot=2), 'inline')
        self.assertTrue(facts['file_tools'])
        self.assertFalse(facts['image_inputs'])
        # Mistral API fallback: no tools.
        with patch.object(agent_config, 'MISTRAL_MODE', 'api'):
            with patch.object(agent_db, 'get_project_permissions', return_value={}):
                facts = agent_executor._task_route_facts(
                    _task(model='mistral-large-4', work_session_slot=None), 'inline')
        self.assertFalse(facts['image_inputs'])
        # Claude CLI reads images natively (both auth modes use the CLI).
        facts = agent_executor._task_route_facts(
            _task(model='claude-sonnet-5-5', work_session_slot=1), 'inline')
        self.assertTrue(facts['image_inputs'])

    def test_never_raises(self):
        facts = agent_executor._task_route_facts(None, 'inline')
        self.assertEqual(facts['mode'], 'inline')
        self.assertEqual(facts['description'], '')
        with patch.object(agent_executor.db, 'get_project_permissions',
                          side_effect=RuntimeError('no db')), \
                patch.object(agent_config, 'MISTRAL_MODE', 'api'):
            facts = agent_executor._task_route_facts(
                {'model': 'mistral-large-4', 'project_id': 7}, 'inline')
        self.assertFalse(facts['file_tools'])
        self.assertFalse(facts['web'])


class H2ImageRouteTests(unittest.TestCase):
    """A vision cap is not enough: H2 must flag image inputs when the run's
    route cannot deliver them.

    Regression: CTIH task 10001302 — vision-capable Mistral models on the Vibe
    route received the JPEGs as raw bytes (execs 20001465/20001466) and H2 had
    waved both runs through because it only checked the vision cap.
    """

    REPLY = json.dumps({
        'completeness': 'complete', 'missing_files': [],
        'binary_unparsable': [], 'capability_warnings': [],
        'strategic_advice': '', 'gate': 'run', 'reason': 'clear'})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = {'name': 'P', 'path': self.tmp.name,
                        'aingel_model': 'mistral-large-4'}
        self.task = {'id': 1, 'title': 'Photos',
                     'description': 'Use only this photo: `a.jpg`',
                     'model': 'scw-deepseek-v4.1-flash'}

    def tearDown(self):
        self.tmp.cleanup()

    def _h2(self, model, route_facts, caps, noted_binary=None):
        task = dict(self.task, model=model)
        with patch.object(agent_overseer, '_ask',
                          return_value=(self.REPLY, None)):
            return agent_overseer.pre_run_check(
                task, self.project, caught_files=[],
                noted_binary=noted_binary if noted_binary is not None else ['a.jpg'],
                model_caps=caps, dep_titles=[], route_facts=route_facts)

    def test_vibe_route_vision_model_is_flagged(self):
        caps = {'vision': True, 'file_access': 'bash'}
        out = self._h2('mistral-large-4',
                       {'route': 'vibe CLI (file tools + web)', 'file_tools': True,
                        'web': True, 'image_inputs': False}, caps)
        entries = out.get('binary_unparsable') or []
        self.assertEqual([e['name'] for e in entries], ['a.jpg'])
        self.assertFalse(entries[0]['model_can_parse'])
        self.assertIn('cannot deliver image content', entries[0]['reason'])

    def test_scaleway_tool_loop_vision_model_passes(self):
        # The working path (exec 20001467) must keep passing untouched.
        caps = {'vision': True, 'file_access': 'tools'}
        out = self._h2('scw-deepseek-v4.1-flash',
                       {'route': 'scaleway tool loop (file tools + web)',
                        'file_tools': True, 'web': True, 'image_inputs': True}, caps)
        self.assertEqual(out.get('binary_unparsable'), [])

    def test_batch_run_is_flagged(self):
        caps = {'vision': True, 'file_access': 'tools'}
        out = self._h2('scw-deepseek-v4.1-flash',
                       {'route': 'batch (toolless chunk-and-aggregate)',
                        'file_tools': False, 'web': False, 'image_inputs': False}, caps)
        self.assertEqual([e['name'] for e in out.get('binary_unparsable') or []],
                         ['a.jpg'])

    def test_text_only_model_keeps_existing_reason(self):
        caps = {'vision': False, 'file_access': 'tools'}
        out = self._h2('scw-deepseek-v4-flash',
                       {'route': 'scaleway tool loop', 'file_tools': True,
                        'web': True, 'image_inputs': True}, caps)
        entries = out.get('binary_unparsable') or []
        self.assertIn('no vision capability', entries[0]['reason'])

    def test_ocr_route_skips_the_images_it_reads_itself(self):
        # api-ocr has no chat vision but /v1/ocr reads png/jpg/jpeg/webp.
        caps = {'vision': False, 'file_access': 'api-ocr'}
        for name in ('a.jpg', 'a.webp'):
            out = self._h2('mistral-ocr-latest',
                           {'route': 'OCR route', 'file_tools': False,
                            'web': False, 'image_inputs': False},
                           caps, noted_binary=[name])
            self.assertEqual(out.get('binary_unparsable'), [], name)


class PreflightGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, 'Example Client'))

    def tearDown(self):
        self.tmp.cleanup()

    def _guards(self, task, mode, caught=None, project_path=None):
        return agent_executor._preflight_guards(
            task, {}, project_path if project_path is not None else self.root,
            mode, caught or [])

    def test_url_with_batch_holds(self):
        holds, warnings = self._guards(
            _task(description='Read https://www.example.com/en/ and summarise.'), 'batch')
        self.assertEqual([h['code'] for h in holds], ['web'])
        self.assertIn('https://www.example.com/en/', holds[0]['message'])

    def test_unknown_route_fails_open(self):
        # No model id / unknown provider: the route cannot be classified, so a
        # hold would be a guess. Fail open (no hold) — the over-holding
        # regression (#10001148) this guard set must never reintroduce.
        holds, _w = self._guards(
            _task(description='Read https://example.com and the folder "Example Client".'),
            'inline')
        self.assertEqual(holds, [])

    def test_url_with_agentic_route_does_not_hold(self):
        with patch.object(agent_config, 'MISTRAL_MODE', 'vibe'), \
                patch.object(agent_config, 'VIBE_MODELS', ['mistral-large-4']):
            holds, _w = self._guards(
                _task(description='Read https://example.com and summarise.'), 'inline')
        self.assertEqual(holds, [])

    def test_cited_folder_zero_caught_files_holds(self):
        holds, _w = self._guards(
            _task(description='Summarise every PDF in the folder "Example Client".'), 'batch')
        self.assertEqual([h['code'] for h in holds], ['folder'])
        self.assertIn('Example Client', holds[0]['message'])

    def test_cited_folder_with_caught_file_does_not_hold(self):
        holds, _w = self._guards(
            _task(description='Summarise every PDF in the folder "Example Client".'),
            'batch', caught=['Example Client/timetable.pdf'])
        self.assertEqual(holds, [])

    def test_cited_folder_agentic_route_does_not_hold(self):
        with patch.object(agent_config, 'MISTRAL_MODE', 'vibe'), \
                patch.object(agent_config, 'VIBE_MODELS', ['mistral-large-4']):
            holds, _w = self._guards(
                _task(description='Summarise every PDF in the folder "Example Client".'),
                'inline')
        self.assertEqual(holds, [])

    def test_nonexistent_folder_does_not_hold(self):
        holds, _w = self._guards(
            _task(description='Read the folder "Nowhere At All".'), 'batch')
        self.assertEqual(holds, [])

    def test_batch_file_creation_is_warning_only(self):
        holds, warnings = self._guards(
            _task(description='Write the comparison to report.md'), 'batch')
        self.assertEqual(holds, [])
        self.assertEqual([w['code'] for w in warnings], ['deliverable'])

    def test_clean_task(self):
        holds, warnings = self._guards(
            _task(description='Summarise the attached timetable.'), 'inline')
        self.assertEqual(holds, [])
        self.assertEqual(warnings, [])

    def test_cron_tasks_are_skipped(self):
        holds, warnings = self._guards(
            _task(source='cron', description='Read https://example.com/x'), 'batch')
        self.assertEqual((holds, warnings), ([], []))


class ReviewerSeesRouteTests(unittest.TestCase):
    H2_REPLY = json.dumps({'completeness': 'complete', 'missing_files': [],
                           'binary_unparsable': [], 'capability_warnings': [],
                           'strategic_advice': 'clear', 'gate': 'run',
                           'reason': 'clear'})
    H2A_REPLY = json.dumps({'questions': [], 'reason': 'clear'})
    LEGACY_FOLDER = ('A folder listed above as existing inside the project is reachable '
                     'by an agentic model and is never a reason to hold.')

    def setUp(self):
        self.prompts = []
        self.project = {'name': 'Rail', 'path': '', 'aingel_model': 'm'}
        self.task = {'id': 10001296, 'title': 'RP-129-E1',
                     'description': 'Compare the folder "Example Client" with https://x.test/'}

    def _fake_ask(self, reply):
        def fake_ask(model, prompt, budget, path, caller):
            self.prompts.append(prompt)
            return reply, None
        return fake_ask

    def _h2(self, route_facts):
        with patch.object(agent_overseer, '_ask',
                          side_effect=self._fake_ask(self.H2_REPLY)):
            agent_overseer.pre_run_check(
                self.task, self.project, caught_files=[], noted_binary=[],
                model_caps={}, dep_titles=[], route_facts=route_facts)
        return self.prompts[-1]

    def _h2a(self, route_facts):
        with patch.object(agent_overseer, '_ask',
                          side_effect=self._fake_ask(self.H2A_REPLY)):
            agent_overseer.pre_check_questions(
                self.task, self.project, [], route_facts=route_facts)
        return self.prompts[-1]

    def test_h2_prompt_carries_route_and_conditional_rule(self):
        prompt = self._h2({'route': 'batch (toolless)', 'file_tools': False, 'web': False})
        self.assertIn('Route for this run: batch (toolless) — file tools: no; '
                      'web access: no.', prompt)
        self.assertIn('NOT reachable on this route (no file tools)', prompt)
        self.assertNotIn(self.LEGACY_FOLDER, prompt)
        self.assertIn('A description that cites a URL requires web access', prompt)
        # the route line sits before the Description block
        self.assertLess(prompt.index('Route for this run:'),
                        prompt.index('Description:'))

    def test_h2_prompt_without_route_facts_is_unchanged(self):
        prompt = self._h2(None)
        self.assertNotIn('Route for this run:', prompt)
        self.assertIn(self.LEGACY_FOLDER, prompt)

    def test_h2a_prompt_carries_route_and_rule(self):
        prompt = self._h2a({'route': 'batch (toolless)', 'file_tools': False, 'web': False})
        self.assertIn('Route for this run: batch (toolless) — file tools: no; '
                      'web access: no.', prompt)
        self.assertIn('ask ONE question about how to proceed', prompt)
        self.assertLess(prompt.index('Route for this run:'),
                        prompt.index('Description:'))

    def test_h2a_prompt_without_route_facts_is_unchanged(self):
        prompt = self._h2a(None)
        self.assertNotIn('Route for this run:', prompt)


class OverrideLatchTests(unittest.TestCase):
    """The 'user-override' latch must survive the gate writes that happen
    BEFORE the guards run, and must be consumed exactly once.

    Regression (found validating 2707715): run_task writes a transient H2
    review marker (gate_source='H2a') and then update_task_h2_gate
    (gate_source='H2') before the pre-flight guards execute. The original
    implementation re-read gate_source at the guard, so on an autopilot
    project the latch was always gone and the override never worked — a
    fail-closed trap on the escape hatch.
    """

    def _entry_latch(self, gate_source):
        """Mirror run_task's ENTRY block (capture + consume) exactly."""
        calls = []
        task = _task(gate_source=gate_source)
        with patch.object(agent_executor.db, 'update_task',
                          side_effect=lambda *a, **kw: calls.append(kw)):
            _pf_user_override = False
            if (task.get('gate_source') or '') == 'user-override':
                _pf_user_override = True
                agent_executor.db.update_task(task['id'], project_path='',
                                              gate_source='', gate_reason='')
        return _pf_user_override, calls

    def test_override_is_captured_at_entry_and_consumed(self):
        flag, calls = self._entry_latch('user-override')
        self.assertTrue(flag)
        self.assertEqual(calls, [{'project_path': '', 'gate_source': '', 'gate_reason': ''}])

    def test_no_override_leaves_no_latch(self):
        for src in ('H2', 'H2a', '', None):
            flag, calls = self._entry_latch(src)
            self.assertFalse(flag, src)
            self.assertEqual(calls, [], src)

    def test_latch_survives_the_h2_gate_writes(self):
        """The whole in-flight sequence: latch → marker → H2 write → guards."""
        task = _task(description='Read https://example.com/ and summarise.')
        flag, _calls = self._entry_latch('user-override')
        # The writes that used to clobber the latch, in run_task's order.
        with patch.object(agent_executor.db, 'get_task',
                          return_value={'id': task['id'], 'gate_source': 'H2'}), \
                patch.object(agent_executor.db, 'update_task'):
            agent_executor.db.update_task(task['id'], gate_source='H2a')   # marker
            agent_executor.db.update_task(task['id'], gate_source='H2')    # H2 gate
            holds, _w = agent_executor._preflight_guards(task, {}, '', 'batch', [])
            self.assertEqual([h['code'] for h in holds], ['web'])
            # The captured local — not the DB — decides the bypass.
            self.assertTrue(flag)
            self.assertFalse((agent_executor.db.get_task(task['id'])
                              or {}).get('gate_source') == 'user-override')

    def test_without_override_the_guard_still_holds(self):
        task = _task(description='Read https://example.com/ and summarise.')
        flag, _calls = self._entry_latch('H2')
        holds, _w = agent_executor._preflight_guards(task, {}, '', 'batch', [])
        self.assertFalse(flag)
        self.assertEqual([h['code'] for h in holds], ['web'])


if __name__ == '__main__':
    unittest.main()
