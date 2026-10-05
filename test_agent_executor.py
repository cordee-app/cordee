import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import agent_db
import agent_executor


class RunTaskLifecycleTests(unittest.TestCase):
    """P1-3: run_task must always leave the execution in a terminal state.

    Regression: an exception raised while building the prompt (before the
    provider call) propagated straight out of run_task, leaving the execution
    row 'running' and the heartbeat thread alive until the orphan sweep.
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self._patch = patch.object(
            agent_db, 'DB_PATH', os.path.join(self.td.name, 'aingel.db'))
        self._patch.start()
        agent_db.init_db()
        self.path = os.path.join(self.td.name, 'proj')
        os.makedirs(self.path, exist_ok=True)
        proj = agent_db.upsert_project('P', 'p', self.path)
        self.proj_id = proj['id']
        self.task_id = agent_db.create_task(proj['id'], 'T', project_path=self.path)

    def tearDown(self):
        self._patch.stop()
        self.td.cleanup()

    def test_prompt_build_failure_finishes_execution_failed(self):
        with patch.object(agent_executor.agit, 'resolve_enabled', return_value=False), \
             patch('agent_scw_session.sync_bucket_to_working_docs', return_value=None), \
             patch('prompt_builder.build', side_effect=RuntimeError('boom')):
            result = agent_executor.run_task(self.task_id)

        self.assertEqual(result.get('status'), 'failed')
        self.assertEqual(result.get('error'), 'boom')
        exec_id = result.get('exec_id')
        self.assertIsNotNone(exec_id)
        execution = agent_db.get_execution(exec_id, project_path=self.path)
        self.assertEqual(execution['status'], 'failed')
        self.assertEqual(agent_db.get_task(self.task_id, project_path=self.path)['status'],
                         'failed')

    def test_over_budget_run_is_refused(self):
        agent_db.update_project_budget(self.proj_id, monthly_budget=10.0)
        agent_db.update_budget_spend(self.proj_id, 10.0)
        result = agent_executor.run_task(self.task_id)
        self.assertEqual(result.get('status'), 'failed')
        self.assertIn('budget', (result.get('error') or '').lower())

    def test_claim_task_is_atomic(self):
        self.assertTrue(agent_db.claim_task(self.task_id, project_path=self.path))
        self.assertFalse(agent_db.claim_task(self.task_id, project_path=self.path))

    def test_second_run_of_running_task_is_refused(self):
        # Simulate a live run: the row is already 'running'.
        self.assertTrue(agent_db.claim_task(self.task_id, project_path=self.path))
        result = agent_executor.run_task(self.task_id)
        self.assertIn('running', result.get('error', ''))

    def test_task_waiting_for_project_lock_is_not_claimed(self):
        # While another run holds the project lock, the waiting task must stay
        # pending — a restart during the wait must not strand it as 'running'.
        lock = agent_executor._project_lock(self.path)
        lock.acquire()
        try:
            t = threading.Thread(target=agent_executor.run_task, args=(self.task_id,))
            with patch.object(agent_executor.agit, 'resolve_enabled', return_value=False), \
                 patch('agent_scw_session.sync_bucket_to_working_docs', return_value=None), \
                 patch('prompt_builder.build', side_effect=RuntimeError('stop')):
                t.daemon = True
                t.start()
                time.sleep(0.3)
                self.assertEqual(
                    agent_db.get_task(self.task_id, project_path=self.path)['status'],
                    'pending')
                lock.release()
                t.join(15)
        finally:
            if lock.locked():
                try:
                    lock.release()
                except RuntimeError:
                    pass

    def test_sweep_resets_stranded_running_task_without_execution(self):
        self.assertTrue(agent_db.claim_task(self.task_id, project_path=self.path))
        conn = agent_db.get_project_db(self.path)
        conn.execute("UPDATE tasks SET updated_at='2020-01-01T00:00:00+00:00' WHERE id=?",
                     (self.task_id,))
        conn.commit(); conn.close()
        agent_db.reset_orphaned_executions()
        self.assertEqual(agent_db.get_task(self.task_id, project_path=self.path)['status'],
                         'pending')

    def test_sweep_keeps_recently_claimed_task(self):
        self.assertTrue(agent_db.claim_task(self.task_id, project_path=self.path))
        agent_db.reset_orphaned_executions()
        self.assertEqual(agent_db.get_task(self.task_id, project_path=self.path)['status'],
                         'running')

    def test_concurrent_runs_in_one_project_are_serialized(self):
        task2 = agent_db.create_task(self.proj_id, 'T2', project_path=self.path)
        events = []
        guard = threading.Lock()

        def fake_build(task, path, project=None):
            with guard:
                events.append(('enter', task['id'], time.monotonic()))
            time.sleep(0.3)
            with guard:
                events.append(('exit', task['id'], time.monotonic()))
            raise RuntimeError('stop')

        with patch.object(agent_executor.agit, 'resolve_enabled', return_value=False), \
             patch('agent_scw_session.sync_bucket_to_working_docs', return_value=None), \
             patch('prompt_builder.build', side_effect=fake_build):
            t1 = threading.Thread(target=agent_executor.run_task, args=(self.task_id,))
            t2 = threading.Thread(target=agent_executor.run_task, args=(task2,))
            t1.start(); t2.start()
            t1.join(15); t2.join(15)

        spans = {}
        for kind, tid, ts in events:
            spans.setdefault(tid, {})[kind] = ts
        self.assertEqual(len(spans), 2, 'both runs should have built a prompt')
        (a_enter, a_exit) = spans[self.task_id]['enter'], spans[self.task_id]['exit']
        (b_enter, b_exit) = spans[task2]['enter'], spans[task2]['exit']
        # The two runs must not overlap: one finishes before the other starts.
        self.assertTrue(a_exit <= b_enter or b_exit <= a_enter,
                        f'runs overlapped: {spans}')


class TaskExpectsDeliverableTests(unittest.TestCase):
    """`_task_expects_deliverable` returns True when a task's title/description
    contains a document-authoring keyword (substring match, case-insensitive)."""

    def test_title_with_draft_keyword_is_true(self):
        self.assertTrue(
            agent_executor._task_expects_deliverable({'title': 'Draft a MoU', 'description': ''})
        )

    def test_description_with_write_keyword_is_true(self):
        self.assertTrue(
            agent_executor._task_expects_deliverable({'title': '', 'description': 'Please write the report'})
        )

    def test_british_summarise_and_american_summarize_both_true(self):
        self.assertTrue(
            agent_executor._task_expects_deliverable({'title': 'Summarise the notes', 'description': ''})
        )
        self.assertTrue(
            agent_executor._task_expects_deliverable({'title': 'Summarize the notes', 'description': ''})
        )

    def test_british_analyse_and_american_analyze_both_true(self):
        self.assertTrue(
            agent_executor._task_expects_deliverable({'title': 'Analyse the data', 'description': ''})
        )
        self.assertTrue(
            agent_executor._task_expects_deliverable({'title': 'Analyze the data', 'description': ''})
        )

    def test_mou_case_insensitive_and_memorandum_true(self):
        self.assertTrue(
            agent_executor._task_expects_deliverable({'title': 'MOU review', 'description': ''})
        )
        self.assertTrue(
            agent_executor._task_expects_deliverable({'title': 'MoU review', 'description': ''})
        )
        self.assertTrue(
            agent_executor._task_expects_deliverable({'title': 'Sign the memorandum', 'description': ''})
        )

    def test_purely_factual_task_with_no_keywords_is_false(self):
        self.assertFalse(
            agent_executor._task_expects_deliverable({'title': 'List the open tasks', 'description': 'Show me the count'})
        )

    def test_empty_task_is_false(self):
        # .get returns None -> '' -> no keyword present
        self.assertFalse(agent_executor._task_expects_deliverable({}))

    def test_substring_match_create_is_true(self):
        # Documents the substring-match behaviour: 'create' alone matches.
        self.assertTrue(
            agent_executor._task_expects_deliverable({'title': 'Create', 'description': ''})
        )

    def test_substring_match_inside_unrelated_word_is_true(self):
        # 'create' is a substring of 'recreate' — the match is a plain `in`
        # check, so this is intentionally True.
        self.assertTrue(
            agent_executor._task_expects_deliverable({'title': 'Recreate the scene', 'description': ''})
        )

    def test_plan_keyword_is_true(self):
        self.assertTrue(
            agent_executor._task_expects_deliverable({'title': 'Plan the work', 'description': ''})
        )


# ---------------------------------------------------------------------------
# Function 2: agent_executor._looks_like_acknowledgment(text)
# ---------------------------------------------------------------------------

class LooksLikeAcknowledgmentTests(unittest.TestCase):
    """`_looks_like_acknowledgment` is True for short, ack-phrase-only outputs
    (no actual deliverable). Empty/whitespace-only → True. >400 chars → False."""

    def test_none_is_true(self):
        self.assertTrue(agent_executor._looks_like_acknowledgment(None))

    def test_empty_string_is_true(self):
        self.assertTrue(agent_executor._looks_like_acknowledgment(''))

    def test_whitespace_only_is_false(self):
        # '   '.strip() == '' and the check is `p in ''` where each phrase `p`
        # is non-empty ('i understand', 'understood', ...). A non-empty string
        # is never a substring of '', so any(...) -> False. (Note: `'' in ''`
        # would be True, but none of the ACK phrases is itself the empty string.)
        self.assertFalse(agent_executor._looks_like_acknowledgment('   '))

    def test_short_ack_i_understand_is_true(self):
        self.assertTrue(
            agent_executor._looks_like_acknowledgment('I understand, I will proceed.')
        )

    def test_understood_is_true(self):
        self.assertTrue(agent_executor._looks_like_acknowledgment('Understood.'))

    def test_certainly_i_can_help_is_true(self):
        self.assertTrue(
            agent_executor._looks_like_acknowledgment('Certainly, I can help with that.')
        )

    def test_ok_i_will_is_true(self):
        self.assertTrue(agent_executor._looks_like_acknowledgment('OK, I will do it now.'))

    def test_long_substantive_answer_is_false(self):
        # >400 chars fails the length check before the phrase check runs.
        long_text = 'The contract requires three signatures. ' * 15
        self.assertGreater(len(long_text), 400)
        self.assertFalse(agent_executor._looks_like_acknowledgment(long_text))

    def test_short_non_ack_answer_is_false(self):
        self.assertFalse(
            agent_executor._looks_like_acknowledgment('The contract requires 3 signatures.')
        )

    def test_substantive_under_400_no_ack_phrase_is_false(self):
        text = ('The agreement outlines a two-party framework with specific clauses '
                'covering liability, termination, and dispute resolution.')
        self.assertLessEqual(len(text.strip()), 400)
        self.assertFalse(agent_executor._looks_like_acknowledgment(text))

    def test_400_chars_with_ack_phrase_is_true(self):
        # Exactly 400 chars: not > 400, so the phrase check runs and matches.
        text = 'I understand' + 'x' * (400 - len('I understand'))
        self.assertEqual(len(text), 400)
        self.assertTrue(agent_executor._looks_like_acknowledgment(text))

    def test_401_chars_with_ack_phrase_is_false(self):
        # 401 chars: the >400 length short-circuit wins even though the ack
        # phrase is present at the start.
        text = 'I understand' + 'x' * (401 - len('I understand'))
        self.assertEqual(len(text), 401)
        self.assertFalse(agent_executor._looks_like_acknowledgment(text))

    def test_case_insensitivity(self):
        self.assertTrue(agent_executor._looks_like_acknowledgment('I UNDERSTAND'))


# ---------------------------------------------------------------------------
# Function 3: agent_executor.run_large_file_batch — context-aware chunking
# ---------------------------------------------------------------------------

class RunLargeFileBatchReduceBudgetTests(unittest.TestCase):
    """Verifies the context-aware chunk sizing in `run_large_file_batch`.

    The reduce-budget line `max(ctx - out_max - ..., 4096)` and the chunk-sizing
    line `available_tokens = max(ctx - out_reserve - 2000, 0)` both derive from
    the model's `context_window`. We assert the *observable* consequence: a
    small-context model produces smaller chunks and therefore MORE map calls
    than a large-context model for the same payload.
    """

    def _map_call_count(self, mock_route):
        """Count route calls whose `caller` kwarg is 'large_file_batch:map'."""
        return sum(
            1 for c in mock_route.call_args_list
            if c.kwargs.get('caller') == 'large_file_batch:map'
        )

    @patch('model_caps.model_capabilities')
    @patch('agent_router.route')
    def test_small_context_makes_more_map_calls_than_large_context(
        self, mock_route, mock_caps
    ):
        # Every route call returns a fixed short partial — no reduce loop trips.
        mock_route.return_value = ('partial', 10, 5, 0.0)
        payload = 'a' * 6000  # no newlines -> deterministic splitting

        # Small-context model (4096): CHUNK_CHARS floors at 2000 -> 3 chunks.
        mock_caps.return_value = {'context_window': 4096}
        agent_executor.run_large_file_batch('smol-model', '', payload, 8192, '/tmp/none')
        small_ctx_map_calls = self._map_call_count(mock_route)

        mock_route.reset_mock()
        # Large-context model (128000): CHUNK_CHARS ~200k -> 1 chunk.
        mock_caps.return_value = {'context_window': 128000}
        agent_executor.run_large_file_batch('big-model', '', payload, 8192, '/tmp/none')
        large_ctx_map_calls = self._map_call_count(mock_route)

        self.assertEqual(small_ctx_map_calls, 3)
        self.assertEqual(large_ctx_map_calls, 1)
        self.assertGreater(small_ctx_map_calls, large_ctx_map_calls)

    @patch('model_caps.model_capabilities')
    @patch('agent_router.route')
    def test_empty_payload_falls_back_to_single_inline_call(
        self, mock_route, mock_caps
    ):
        mock_caps.return_value = {'context_window': 128000}
        mock_route.return_value = ('final text', 100, 50, 0.01)
        # _split_text('') -> [] -> the `if not chunks` branch runs a single
        # route call with caller='large_file_batch' (NOT ':map').
        result = agent_executor.run_large_file_batch('m', '', '', 8192, '/tmp/none')

        self.assertEqual(mock_route.call_count, 1)
        only_call = mock_route.call_args_list[0]
        self.assertEqual(only_call.kwargs.get('caller'), 'large_file_batch')
        self.assertNotEqual(only_call.kwargs.get('caller'), 'large_file_batch:map')

    @patch('model_caps.model_capabilities')
    @patch('agent_router.route')
    def test_single_call_path_returns_route_four_tuple_unchanged(
        self, mock_route, mock_caps
    ):
        mock_caps.return_value = {'context_window': 128000}
        mock_route.return_value = ('final text', 100, 50, 0.01)
        result = agent_executor.run_large_file_batch('m', '', '', 8192, '/tmp/none')

        self.assertEqual(len(result), 4)
        self.assertEqual(result, ('final text', 100, 50, 0.01))


class SimulationGapRegressionTests(unittest.TestCase):
    """Regression tests for laptop task 20001131 — a text-only Mistral route
    left to "create a file" it could not write: (A) the text-only decision
    ignored the per-model VIBE_MODELS allowlist, (B) the auto-switch phrase
    list missed common phrasings, (C) the detector was blind to the emitted
    dialect (bash{...} pseudo-JSON, <read_file>/<write_file>)."""

    def setUp(self):
        # agent_config freezes these at import time, so patch the module
        # attributes (which _mistral_route_is_agentic reads at call time).
        import agent_config
        self._patches = [
            patch.object(agent_config, 'MISTRAL_MODE', 'vibe'),
            patch.object(agent_config, 'VIBE_MODELS',
                         ['mistral-large-latest', 'mistral-glm-5-3']),
            # The Vibe CLI is not installed on CI runners; pretend it is.
            patch('shutil.which', return_value='/usr/local/bin/vibe'),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    # ── Fix A: text-only decision honours VIBE_MODELS ──────────────────────
    def test_mistral_medium_not_in_allowlist_is_text_only(self):
        task = {'model': 'mistral-medium-latest', 'work_session_slot': None}
        self.assertTrue(agent_executor._task_uses_direct_text_route(task))

    def test_mistral_small_not_in_allowlist_is_text_only(self):
        task = {'model': 'mistral-small-latest', 'work_session_slot': None}
        self.assertTrue(agent_executor._task_uses_direct_text_route(task))

    def test_mistral_large_in_allowlist_is_agentic(self):
        task = {'model': 'mistral-large-latest', 'work_session_slot': None}
        self.assertFalse(agent_executor._task_uses_direct_text_route(task))

    def test_scaleway_tool_model_is_agentic(self):
        task = {'model': 'scw-qwen3.6-35b', 'work_session_slot': None}
        self.assertFalse(agent_executor._task_uses_direct_text_route(task))

    # ── Fix B: creation-intent detection ───────────────────────────────────
    def test_incident_phrasings_are_detected(self):
        for desc in ('Create a plain text file called test.txt',
                     'Write a test file',
                     'Please create a report',
                     'make a CSV from the data',
                     'output a JSON file'):
            self.assertTrue(
                agent_executor._task_requests_file_creation(
                    {'description': desc, 'title': ''}),
                f'creation intent missed: {desc!r}')

    def test_read_only_tasks_are_not_creation(self):
        for desc in ('Read the attached file and summarise it',
                     'Summarise the PDF in the Working Documents',
                     'Analyse the contract and list risks'):
            self.assertFalse(
                agent_executor._task_requests_file_creation(
                    {'description': desc, 'title': ''}),
                f'read-only task misread as creation: {desc!r}')

    def test_title_is_considered(self):
        self.assertTrue(agent_executor._task_requests_file_creation(
            {'title': 'Save results.csv', 'description': ''}))
        self.assertTrue(agent_executor._task_requests_file_creation(
            {'title': 'Export data', 'description': 'save as results.csv'}))

    # ── Fix C: detector knows the emitted dialect ──────────────────────────
    def test_pseudo_json_bash_block_detected(self):
        self.assertTrue(agent_executor._looks_like_unexecuted_tool_request(
            'bash{echo hi > test.txt}'))

    def test_paired_read_write_tags_detected(self):
        self.assertTrue(agent_executor._looks_like_unexecuted_tool_request(
            'Sure.\n<read_file>test.txt</read_file>\n<write_file>x</write_file>'))

    def test_plain_prose_not_detected(self):
        self.assertFalse(agent_executor._looks_like_unexecuted_tool_request(
            'Here is the report content. All done.'))

    def test_markup_stripper_removes_pseudo_json(self):
        stripped = agent_executor._strip_tool_markup(
            'bash{echo hi > a.txt}\n\nThe real answer follows.')
        self.assertNotIn('bash{', stripped)
        self.assertIn('The real answer follows.', stripped)


if __name__ == '__main__':
    unittest.main()
