"""Unit tests for agent_decision (local decision-model pre-filter).

Run: python3 -m unittest test_agent_decision -v
No network access: requests.post/get are mocked.
"""
import json
import time
import unittest
from unittest import mock

import agent_decision as ad


def _task(description='A sufficiently long task description for the pre-filter '
                      'to consider it non-trivial and answer all questions.',
          title='Test task', **extra):
    task = {'id': 1, 'title': title, 'description': description,
            'project_id': 1, 'model': 'test-model'}
    task.update(extra)
    return task


def _noul(p):
    return {'type': 'noul', 'noul': p}


def _choice(label, probs=None, confidence=1.0):
    if probs is None:
        probs = {label: 0.9, 'other': 0.05}
    return {'type': 'choice', 'choice': label, 'probabilities': probs,
            'confidence': confidence}


def _score(value):
    return {'type': 'score', 'score': value,
            'probabilities': {str(i): (1.0 if i == int(value) else 0.0)
                              for i in range(4)}}


def _base_answers():
    """Decisive default answers for every question (nothing flags/ambiguates)."""
    answers = {}
    for key in ad.QUESTION_ORDER:
        qtype = ad._QSET[key]['type']
        if qtype == 'noul':
            answers[key] = _noul(0.05)
        elif qtype == 'score':
            answers[key] = _score(0)
        else:
            first = next(iter(ad._QSET[key]['criteria']))
            answers[key] = _choice(first, {first: 0.9, 'other': 0.05})
    return answers


def _route_facts(file_tools=False, web=False, **extra):
    facts = {'mode': 'inline', 'route': 'test route', 'file_tools': file_tools,
             'web': web}
    facts.update(extra)
    return facts


class DecideTests(unittest.TestCase):
    def setUp(self):
        ad._reset_for_tests()

    def test_loopback_ok(self):
        for url in ('http://127.0.0.1:11434', 'http://localhost:11434',
                    'http://[::1]:11434', 'https://127.0.0.1'):
            self.assertTrue(ad.loopback_ok(url), url)
        for url in ('http://10.1.2.3:11434', 'http://0.0.0.0:11434',
                    'http://localhost.evil.com', 'http://10.0.0.1',
                    'ftp://127.0.0.1', '', None):
            self.assertFalse(ad.loopback_ok(url), url)

    @mock.patch.object(ad.requests, 'post')
    def test_non_loopback_fails_closed(self, post):
        result = ad.decide({'x': 1}, {'q': {'type': 'noul', 'instructions': 'x'}},
                           'tev1:0.8b', base_url='http://10.1.2.3:11434')
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('non-loopback', result['error'])
        post.assert_not_called()

    @mock.patch.object(ad.requests, 'post')
    def test_request_has_no_options_key(self, post):
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            'answers': {'q': {'type': 'noul', 'noul': 0.97}},
            'usage': {'input_tokens': 10, 'output_tokens': 1},
        }
        post.return_value = response
        questions = {'q': {'type': 'noul', 'instructions': 'is it?'}}
        result = ad.decide({'title': 't'}, questions, 'tev1:0.8b')

        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['answers']['q']['noul'], 0.97)
        body = post.call_args.kwargs['json']
        self.assertEqual(set(body), {'model', 'state', 'questions', 'keep_alive'})
        self.assertNotIn('options', body)
        self.assertEqual(post.call_args.kwargs['timeout'], 120)

    @mock.patch.object(ad.requests, 'post')
    def test_partial_answers_tolerated(self, post):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'answers': {'only': _noul(0.9)}}
        post.return_value = response
        result = ad.decide({'title': 't'}, {'only': {'type': 'noul',
                                                     'instructions': 'x'}}, 'm')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(set(result['answers']), {'only'})

    @mock.patch.object(ad.requests, 'post')
    def test_error_and_timeout_become_unknown(self, post):
        post.side_effect = ad.requests.Timeout('timed out')
        result = ad.decide({'title': 't'}, {'q': {'type': 'noul',
                                                  'instructions': 'x'}}, 'm')
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('Timeout', result['error'])

    @mock.patch.object(ad.requests, 'post')
    def test_http_error_becomes_unknown(self, post):
        response = mock.Mock(status_code=500, text='boom')
        post.return_value = response
        result = ad.decide({'title': 't'}, {'q': {'type': 'noul',
                                                  'instructions': 'x'}}, 'm')
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('500', result['error'])

    @mock.patch.object(ad.requests, 'post')
    def test_no_answers_object_is_unknown(self, post):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'model': 'm'}
        post.return_value = response
        result = ad.decide({'title': 't'}, {'q': {'type': 'noul',
                                                  'instructions': 'x'}}, 'm')
        self.assertEqual(result['status'], 'unknown')


class QuestionSetTests(unittest.TestCase):
    def test_twenty_questions_with_exact_types(self):
        qset = ad.questions_for_tier()
        self.assertEqual(len(qset), 20)
        self.assertEqual(tuple(qset.keys()), ad.QUESTION_ORDER)

        expected = {
            'read_scope': 'score',
            'asks_system_level_commands': 'noul',
            'asks_outside_project_paths': 'noul',
            'requires_web_research': 'noul',
            'prohibits_project_files': 'noul',
            'prohibition_topics': 'choice',
            'asks_modify_or_overwrite_existing': 'choice',
            'asks_secrets_or_credentials': 'noul',
            'asks_external_side_effects': 'noul',
            'asks_delete_move': 'noul',
            'binary_load': 'score',
            'references_pattern_not_file': 'noul',
            'deliverable_named': 'noul',
            'deliverable_format_clear': 'choice',
            'task_category': 'choice',
            'has_user_constraints': 'noul',
            'mentions_other_projects_or_tasks': 'noul',
            'risk_score': 'score',
            'expects_tool_use': 'choice',
            'expects_file_creation': 'noul',
        }
        self.assertEqual({k: v['type'] for k, v in qset.items()}, expected)

    def test_instructions_at_most_25_words(self):
        for key, question in ad.questions_for_tier().items():
            words = question['instructions'].split()
            self.assertLessEqual(len(words), 25, f'{key}: {len(words)} words')

    def test_choice_and_score_legends(self):
        qset = ad.questions_for_tier()
        self.assertIn('none', qset['prohibition_topics']['criteria'])
        self.assertIn('multiple', qset['prohibition_topics']['criteria'])
        self.assertEqual(qset['prohibition_topics']['criteria']['project-files'],
                         'Must not use or read the project\'s files.')
        self.assertIn('text-answer-only', qset['expects_tool_use']['criteria'])
        self.assertIn('data-processing', qset['task_category']['criteria'])
        self.assertEqual(len(qset['read_scope']['criteria']), 4)
        self.assertEqual(len(qset['risk_score']['criteria']), 4)

    def test_questions_returns_a_copy(self):
        qset = ad.questions_for_tier()
        qset['read_scope']['instructions'] = 'mutated'
        self.assertNotEqual(ad.questions_for_tier()['read_scope']['instructions'],
                            'mutated')


class BuildStateTests(unittest.TestCase):
    def setUp(self):
        ad._reset_for_tests()

    def test_clarifications_block_preserved(self):
        marker = '**User clarifications (Guide pre-run check):**'
        desc = ('A' * 4000) + '\n\n' + marker + '\n- use the shared folder\n- do not delete'
        state = ad.build_state(_task(desc), route_facts=_route_facts())
        description = state['description']

        self.assertIn(marker, description)
        self.assertIn('- use the shared folder', description)
        self.assertIn('- do not delete', description)
        self.assertIn('truncated', description)
        self.assertLessEqual(len(description), 2500 + 200)

        from agent_overseer import _description_for_review
        self.assertEqual(description, _description_for_review(desc, ad.STATE_DESC_CHARS))

    def test_short_description_is_skipped_trivial(self):
        state = ad.build_state(_task('too short'), route_facts=_route_facts())
        self.assertEqual(state['status'], 'skipped_trivial')

    def test_missing_model_uses_caps_get_no_keyerror(self):
        task = _task(model='not-in-model-caps')
        # No explicit route facts: agent_executor._task_route_facts runs and
        # model_caps.get_cap must be used with .get(), never [].
        state = ad.build_state(task)
        self.assertIn('route_facts', state)
        self.assertIsInstance(state['route_facts'], dict)

    def test_build_state_never_raises(self):
        self.assertIsInstance(ad.build_state(None), dict)
        self.assertIsInstance(ad.build_state({}), dict)


class PrefilterTests(unittest.TestCase):
    def setUp(self):
        ad._reset_for_tests()

    def _run(self, fast_answers, escalate_answers=None, task=None, facts=None):
        task = task or _task()
        facts = facts or _route_facts()
        escalations = []

        def fake_decide(state, questions, model, **kwargs):
            if model == ad.LOCAL_DEC_FAST:
                return {'status': 'ok', 'model': model, 'answers': fast_answers,
                        'usage': {'input_tokens': 100, 'output_tokens': 2},
                        'error': None}
            escalations.append((questions, kwargs))
            return {'status': 'ok', 'model': model,
                    'answers': escalate_answers or {},
                    'usage': {'input_tokens': 50, 'output_tokens': 2},
                    'error': None}

        with mock.patch.object(ad, 'build_state',
                               return_value={'title': task['title'],
                                             'description': task['description'],
                                             'route_facts': facts}), \
             mock.patch.object(ad, 'model_digest', return_value='digest-1'), \
             mock.patch.object(ad, 'decide', side_effect=fake_decide) as decide:
            payload = ad.prefilter_task(task)
        return payload, decide, escalations

    def test_threshold_bands_and_escalation(self):
        answers = _base_answers()
        answers['asks_system_level_commands'] = _noul(0.95)   # flag
        answers['asks_outside_project_paths'] = _noul(0.5)     # escalate
        answers['asks_secrets_or_credentials'] = _noul(0.2)    # clear
        payload, decide, escalations = self._run(
            answers, {'asks_outside_project_paths': _noul(0.75)})

        self.assertEqual(payload['status'], 'ok')
        self.assertIn('asks_system_level_commands', payload['flags'])
        self.assertIn('asks_outside_project_paths', payload['flags'])
        self.assertNotIn('asks_secrets_or_credentials', payload['flags'])
        self.assertEqual(payload['model_escalate'], ad.LOCAL_DEC_ESCALATE)
        self.assertEqual(decide.call_count, 2)
        self.assertEqual(set(escalations[0][0]), {'asks_outside_project_paths'})
        self.assertEqual(payload['usage']['calls'], 2)

    def test_post_escalation_boundary_070(self):
        for escalated_p, expected_flag in ((0.7, True), (0.69, False)):
            with self.subTest(escalated_p=escalated_p):
                ad._reset_for_tests()
                answers = _base_answers()
                answers['asks_outside_project_paths'] = _noul(0.5)
                payload, _, _ = self._run(
                    answers, {'asks_outside_project_paths': _noul(escalated_p)})
                self.assertEqual('asks_outside_project_paths' in payload['flags'],
                                 expected_flag)

    def test_choice_margin_escalates_and_top1_wins(self):
        answers = _base_answers()
        answers['prohibition_topics'] = _choice(
            'none', {'none': 0.50, 'project-files': 0.45, 'multiple': 0.05})
        payload, _, escalations = self._run(
            answers,
            {'prohibition_topics': _choice(
                'project-files', {'project-files': 0.61, 'none': 0.39})})

        self.assertEqual(set(escalations[0][0]), {'prohibition_topics'})
        self.assertEqual(payload['answers']['prohibition_topics']['choice'],
                         'project-files')
        block = ad.render_h2_block(payload)
        self.assertIn('Prohibitions: project-files', block)

    def test_choice_outside_margin_does_not_escalate(self):
        answers = _base_answers()
        answers['prohibition_topics'] = _choice(
            'none', {'none': 0.70, 'project-files': 0.25, 'multiple': 0.05})
        payload, decide, escalations = self._run(answers)
        self.assertEqual(escalations, [])
        self.assertEqual(decide.call_count, 1)

    def test_score_review_lines_and_p18_never_rendered(self):
        answers = _base_answers()
        answers['read_scope'] = _score(3)
        answers['binary_load'] = _score(2)
        answers['risk_score'] = _score(3)
        payload, _, _ = self._run(answers)

        joined = ' | '.join(payload['llm_review'])
        self.assertIn('read_scope=3', joined)
        self.assertIn('binary_load=2', joined)
        self.assertNotIn('risk_score', joined)

        block = ad.render_h2_block(payload)
        self.assertIn('read_scope=3', block)
        self.assertNotIn('risk_score', block)
        self.assertNotIn('risk', block.lower().replace('risky', ''))

    def test_p18_never_rendered_even_if_forced_into_flags(self):
        payload = {'status': 'ok', 'flags': ['risk_score'],
                   'answers': {}, 'llm_review': []}
        self.assertEqual(ad.render_h2_block(payload), '')

    def test_capability_mismatch_lines(self):
        answers = _base_answers()
        answers['requires_web_research'] = _noul(0.95)
        answers['expects_tool_use'] = _choice(
            'web-fetch', {'web-fetch': 0.9, 'read-files': 0.1})
        payload, _, _ = self._run(answers, facts=_route_facts(file_tools=False,
                                                              web=False))
        joined = ' | '.join(payload['llm_review'])
        self.assertIn('P4 web research requested but the route has no web access', joined)
        self.assertIn('P19 web fetch requested but the route has no web access', joined)

    def test_p19_tool_expectation_without_file_tools(self):
        answers = _base_answers()
        answers['expects_tool_use'] = _choice(
            'read-files', {'read-files': 0.9, 'text-answer-only': 0.1})
        payload, _, _ = self._run(answers, facts=_route_facts(file_tools=False))
        self.assertIn('P19 expects read-files but the route has no file tools',
                      ' | '.join(payload['llm_review']))

    def test_p20_conditional_review_line(self):
        answers = _base_answers()
        answers['expects_file_creation'] = _noul(0.95)

        with mock.patch.object(ad, '_detector_says_file_creation', return_value=False):
            payload, _, _ = self._run(answers, facts=_route_facts(file_tools=False))
        self.assertIn('possible write task not caught by the auto-switch detector',
                      ' | '.join(payload['llm_review']))
        self.assertIn('possible write task', ad.render_h2_block(payload))
        # P20 is never a generic flag line.
        self.assertNotIn('P20', ad.render_h2_block(payload))

        # Route has file tools -> no line.
        with mock.patch.object(ad, '_detector_says_file_creation', return_value=False):
            payload, _, _ = self._run(answers, facts=_route_facts(file_tools=True))
        self.assertNotIn('auto-switch', ' | '.join(payload['llm_review']))

        # Detector already caught it -> no line.
        with mock.patch.object(ad, '_detector_says_file_creation', return_value=True):
            payload, _, _ = self._run(answers, facts=_route_facts(file_tools=False))
        self.assertNotIn('auto-switch', ' | '.join(payload['llm_review']))

    def test_trivial_description_skips_without_a_call(self):
        task = _task('too short')
        with mock.patch.object(ad, 'decide') as decide:
            payload = ad.prefilter_task(task)
        self.assertEqual(payload['status'], 'skipped_trivial')
        decide.assert_not_called()
        self.assertTrue(payload['state_hash'])

    def test_failure_is_unknown_with_attempts(self):
        task = _task(prefilter_json=json.dumps({'status': 'unknown',
                                                'attempts': 2,
                                                'state_hash': 'old'}))
        with mock.patch.object(ad, 'build_state',
                               return_value={'title': 't', 'description': 'd' * 80,
                                             'route_facts': {}}), \
             mock.patch.object(ad, 'model_digest', return_value=''), \
             mock.patch.object(ad, 'decide',
                               return_value={'status': 'unknown', 'model': 'm',
                                             'answers': {}, 'usage': {},
                                             'error': 'down'}):
            payload = ad.prefilter_task(task)
        self.assertEqual(payload['status'], 'unknown')
        self.assertEqual(payload['attempts'], 3)
        self.assertEqual(payload['error'], 'down')
        self.assertTrue(payload['state_hash'])

    def test_success_resets_attempts(self):
        task = _task(prefilter_json=json.dumps({'status': 'unknown', 'attempts': 2}))
        payload, _, _ = self._run(_base_answers(), task=task)
        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(payload['attempts'], 1)


class HashAndStalenessTests(unittest.TestCase):
    def setUp(self):
        ad._reset_for_tests()

    def _payload(self, task, digest='digest-1', qset_version=ad.QSET_VERSION):
        facts = _route_facts()
        state = ad.build_state(task, route_facts=facts)
        return {
            'status': 'ok',
            'state_hash': ad.state_hash(state, digest, '', qset_version),
            'digests': {'fast': digest, 'escalate': None},
            'qset_version': qset_version,
            'model_escalate': None,
            'attempts': 1,
        }

    @mock.patch.object(ad, 'model_digest', return_value='digest-1')
    def test_fresh_payload_is_not_stale(self, _digest):
        task = _task()
        payload = self._payload(task)
        self.assertFalse(ad.is_stale(payload, task, route_facts=_route_facts()))

    @mock.patch.object(ad, 'model_digest', return_value='digest-1')
    def test_description_change_is_stale(self, _digest):
        task = _task()
        payload = self._payload(task)
        changed = dict(task, description=task['description'] + ' changed')
        self.assertTrue(ad.is_stale(payload, changed, route_facts=_route_facts()))

    @mock.patch.object(ad, 'model_digest', return_value='digest-2')
    def test_digest_change_is_stale(self, _digest):
        task = _task()
        payload = self._payload(task, digest='digest-1')
        self.assertTrue(ad.is_stale(payload, task, route_facts=_route_facts()))

    def test_missing_or_unknown_is_stale(self):
        task = _task()
        self.assertTrue(ad.is_stale(None, task))
        self.assertTrue(ad.is_stale({}, task))
        self.assertTrue(ad.is_stale({'status': 'unknown'}, task))

    @mock.patch.object(ad, 'model_digest', return_value='digest-1')
    def test_skipped_trivial_payload_stays_fresh(self, _digest):
        task = _task('short')
        state = ad.build_state(task, route_facts=_route_facts())
        payload = {'status': 'skipped_trivial',
                   'state_hash': ad.state_hash(state, '', '', ad.QSET_VERSION),
                   'digests': {'fast': '', 'escalate': None},
                   'qset_version': ad.QSET_VERSION}
        self.assertFalse(ad.is_stale(payload, task, route_facts=_route_facts()))

    def test_hash_changes_with_qset_version(self):
        state = {'title': 't', 'description': 'd', 'route_facts': {}}
        self.assertNotEqual(ad.state_hash(state, 'a', '', 1),
                            ad.state_hash(state, 'a', '', 2))
        self.assertNotEqual(ad.state_hash(state, 'a', '', 1),
                            ad.state_hash(state, 'b', '', 1))
        self.assertNotEqual(ad.state_hash(state, 'a', 'e', 1),
                            ad.state_hash(state, 'a', '', 1))


class RenderBlockTests(unittest.TestCase):
    def test_empty_unless_ok(self):
        self.assertEqual(ad.render_h2_block(None), '')
        self.assertEqual(ad.render_h2_block({}), '')
        self.assertEqual(ad.render_h2_block({'status': 'unknown',
                                             'flags': ['asks_delete_move']}), '')
        self.assertEqual(ad.render_h2_block({'status': 'skipped_trivial'}), '')

    def test_block_cap_800(self):
        payload = {
            'status': 'ok',
            'flags': list(ad._FLAG_LABELS),
            'llm_review': [f'review line {i} ' + ('x' * 80) for i in range(40)],
            'answers': {},
        }
        block = ad.render_h2_block(payload)
        self.assertLessEqual(len(block), ad.MAX_BLOCK_CHARS)
        self.assertTrue(block)

    def test_signal_only_content(self):
        payload = {
            'status': 'ok',
            'flags': ['asks_system_level_commands', 'asks_secrets_or_credentials'],
            'llm_review': ['read_scope=2 (wide file scope — check containment)'],
            'answers': {},
        }
        block = ad.render_h2_block(payload)
        self.assertIn('Flags:', block)
        self.assertIn('system-level commands (P2)', block)
        self.assertIn('secrets/credentials (P8)', block)
        self.assertIn('Review: read_scope=2', block)
        self.assertIn('Legend:', block)
        # No raw answers / risk scores leak into the prompt block.
        self.assertNotIn('risk_score', block)


class FeatureFlagTests(unittest.TestCase):
    def setUp(self):
        ad._reset_for_tests()

    def test_mode_zero_default(self):
        with mock.patch.object(ad, '_read_settings', return_value={}):
            self.assertEqual(ad.prefilter_mode(), 0)
            self.assertFalse(ad.prefilter_enabled('/x'))

    def test_invalid_mode_is_off(self):
        with mock.patch.object(ad, '_read_settings',
                               return_value={'prefilter_enabled': '9'}):
            self.assertEqual(ad.prefilter_mode(), 0)

    def test_mode_two_all_projects(self):
        with mock.patch.object(ad, '_read_settings',
                               return_value={'prefilter_enabled': '2'}):
            self.assertTrue(ad.prefilter_enabled(None))
            self.assertTrue(ad.prefilter_enabled('/anything'))

    def test_mode_one_project_scoping(self):
        settings = {'prefilter_enabled': '1',
                    'prefilter_project_ids': '3, 4'}
        with mock.patch.object(ad, '_read_settings', return_value=settings), \
             mock.patch.object(ad, '_project_id_for_path', return_value=3):
            self.assertTrue(ad.prefilter_enabled('/proj/three'))
        with mock.patch.object(ad, '_read_settings', return_value=settings), \
             mock.patch.object(ad, '_project_id_for_path', return_value=9):
            self.assertFalse(ad.prefilter_enabled('/proj/nine'))
        with mock.patch.object(ad, '_read_settings', return_value=settings):
            self.assertFalse(ad.prefilter_enabled(None))


class WorkerQueueTests(unittest.TestCase):
    def setUp(self):
        ad._reset_for_tests()

    def test_dedup_latest_wins(self):
        with mock.patch.object(ad, 'prefilter_enabled', return_value=True):
            ad.note_description_changed(5, '/p/one')
            ad.note_description_changed(5, '/p/two')
            self.assertEqual(len(ad._PENDING), 1)
            self.assertEqual(ad._PENDING[5], '/p/two')
            self.assertIn(5, ad._EVENTS)

            ad.note_description_changed(6, '/p/six')
            self.assertEqual(len(ad._PENDING), 2)
            self.assertIn(6, ad._PENDING)

    def test_flag_off_does_not_enqueue(self):
        with mock.patch.object(ad, 'prefilter_enabled', return_value=False):
            ad.note_description_changed(7, '/p/off')
        self.assertEqual(ad._PENDING, {})
        self.assertNotIn(7, ad._EVENTS)

    def test_queue_is_bounded(self):
        with mock.patch.object(ad, 'prefilter_enabled', return_value=True):
            for task_id in range(1, 251):
                ad.note_description_changed(task_id, f'/p/{task_id}')
        self.assertEqual(len(ad._PENDING), 200)
        self.assertNotIn(1, ad._PENDING)
        self.assertIn(250, ad._PENDING)

    def test_should_compute_skips_fresh_and_caps_failures(self):
        task = _task()
        fresh = {'status': 'ok', 'state_hash': 'h', 'digests': {'fast': 'd'}}
        with mock.patch.object(ad, 'is_stale', return_value=False):
            self.assertFalse(ad._should_compute(task, fresh, '/p'))

        failed = {'status': 'unknown', 'attempts': 3, 'state_hash': 'h',
                  'digests': {'fast': 'd'}}
        with mock.patch.object(ad, 'build_state', return_value={}), \
             mock.patch.object(ad, 'state_hash', return_value='h'):
            self.assertFalse(ad._should_compute(task, failed, '/p'))
        with mock.patch.object(ad, 'build_state', return_value={}), \
             mock.patch.object(ad, 'state_hash', return_value='different'):
            self.assertTrue(ad._should_compute(task, failed, '/p'))


class WorkerStartTests(unittest.TestCase):
    def setUp(self):
        ad._reset_for_tests()

    def test_start_worker_is_idempotent(self):
        created = []

        class FakeThread:
            def __init__(self, target=None, name=None, daemon=None):
                self.target = target
                self.name = name
                self.daemon = daemon
                created.append(self)

            def start(self):
                pass

            def is_alive(self):
                return True

        with mock.patch.object(ad.threading, 'Thread', FakeThread):
            ad.start_worker()
            ad.start_worker()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].name, 'prefilter-worker')
        self.assertTrue(created[0].daemon)


class WaitTests(unittest.TestCase):
    def setUp(self):
        ad._reset_for_tests()

    def test_returns_immediately_when_no_event(self):
        started = time.monotonic()
        ad.wait_for_prefilter(123, max_s=20)
        self.assertLess(time.monotonic() - started, 0.1)

    def test_returns_immediately_when_already_set(self):
        event = mock.Mock()
        event.is_set.return_value = True
        with mock.patch.object(ad, '_EVENTS', {9: event}):
            ad.wait_for_prefilter(9, max_s=20)
        event.wait.assert_not_called()

    def test_bounded_wait_uses_event(self):
        event = mock.Mock()
        event.is_set.return_value = False
        event.wait.return_value = False
        with mock.patch.object(ad, '_EVENTS', {9: event}):
            ad.wait_for_prefilter(9, max_s=20)
        event.wait.assert_called_once_with(20)

    def test_worker_marks_event_done(self):
        event = mock.Mock()
        with mock.patch.object(ad, '_EVENTS', {9: event}):
            ad._mark_done(9)
        event.set.assert_called_once_with()

    def test_never_raises_on_bad_input(self):
        ad.wait_for_prefilter(None)
        ad.wait_for_prefilter('not-an-int')


class SelfCheckTests(unittest.TestCase):
    def setUp(self):
        ad._reset_for_tests()

    @mock.patch.object(ad.requests, 'post')
    def test_self_check_reads_capabilities_and_num_ctx(self, get):
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            'capabilities': ['completion', 'decision'],
            'parameters': 'num_ctx 8192\nnum_thread 8',
        }
        get.return_value = response
        result = ad.self_check()
        self.assertTrue(result['ok'])
        self.assertEqual(result['num_ctx'], 8192)
        self.assertIn('decision', result['capabilities'])
        self.assertEqual(get.call_args.kwargs['json'], {'model': ad.LOCAL_DEC_FAST})

    @mock.patch.object(ad.requests, 'post')
    def test_self_check_best_effort_on_failure(self, get):
        get.side_effect = ad.requests.ConnectionError('down')
        result = ad.self_check()
        self.assertEqual(result, {'ok': False, 'num_ctx': None,
                                  'capabilities': []})

    @mock.patch.object(ad.requests, 'post')
    def test_model_digest_uses_selected_manifest(self, get):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'manifests': [
            {'digest': 'sha-parent', 'selected': False},
            {'digest': 'sha-child', 'selected': True},
        ]}
        get.return_value = response
        self.assertEqual(ad.model_digest('tev1:0.8b'), 'sha-child')
        # Cached per process: no second GET.
        get.reset_mock()
        self.assertEqual(ad.model_digest('tev1:0.8b'), 'sha-child')
        get.assert_not_called()

    @mock.patch.object(ad.requests, 'post')
    def test_model_digest_empty_on_error(self, get):
        get.side_effect = RuntimeError('boom')
        self.assertEqual(ad.model_digest('nope'), '')

    @mock.patch.object(ad.requests, 'get')
    @mock.patch.object(ad.requests, 'post')
    def test_model_digest_falls_back_to_tags(self, post, get):
        """This Ollama build returns no manifests/digest from /api/show."""
        show = mock.Mock(status_code=200)
        show.json.return_value = {'capabilities': ['decision']}
        post.return_value = show
        tags = mock.Mock(status_code=200)
        tags.json.return_value = {'models': [
            {'name': 'other:latest', 'digest': 'sha-other'},
            {'name': ad.LOCAL_DEC_FAST, 'digest': 'sha-tags'},
        ]}
        get.return_value = tags
        self.assertEqual(ad.model_digest(ad.LOCAL_DEC_FAST), 'sha-tags')


class ImportDisciplineTests(unittest.TestCase):
    def test_agent_decision_import_does_not_pull_agent_db_at_module_level(self):
        import os
        import subprocess
        import sys
        code = (
            'import sys\n'
            'import agent_decision\n'
            'assert "agent_db" not in sys.modules, "agent_db imported at module level"\n'
            'assert "agent_overseer" not in sys.modules, "agent_overseer imported at module level"\n'
            'print("ok")\n'
        )
        repo = os.path.dirname(os.path.abspath(ad.__file__))
        out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                             text=True, cwd=repo)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == '__main__':
    unittest.main()
