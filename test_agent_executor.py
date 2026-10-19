import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import agent_db
import agent_chats
import agent_router
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

    def test_failed_run_moves_its_root_files_to_the_output_folder(self):
        # Regression (task #10001192): a failed run skipped the success-path
        # root relocation and `checkout -f` kept its untracked scripts, so the
        # next run's checkpoint commit swept 16 files into the default branch.
        def fake_route(*a, **k):
            with open(os.path.join(self.path, 'stray_script.py'), 'w') as f:
                f.write('print(1)')
            raise RuntimeError('provider failed')

        built = {'mode': 'inline', 'prompt': 'do it', 'max_tokens': 1000}
        with patch.object(agent_executor.agit, 'resolve_enabled', return_value=False), \
             patch('agent_scw_session.sync_bucket_to_working_docs', return_value=None), \
             patch('prompt_builder.build', return_value=built), \
             patch.object(agent_executor, 'route', side_effect=fake_route):
            result = agent_executor.run_task(self.task_id)

        self.assertEqual(result.get('status'), 'failed')
        self.assertFalse(os.path.exists(os.path.join(self.path, 'stray_script.py')))
        task = agent_db.get_task(self.task_id, project_path=self.path)
        out_dir = agent_executor.task_output_dir(task, self.path)
        self.assertTrue(os.path.exists(os.path.join(self.path, out_dir, 'stray_script.py')))

    def test_failed_run_preserves_provider_usage_on_execution_row(self):
        # Regression (production incident, gemma-4-26b): a Scaleway run that
        # exhausted its tool turns raised with 1.23M tokens consumed, but
        # the generic failure handler finished the execution with default
        # zeros, wiping the heartbeat-tracked usage from the row. Usage
        # must ride the exception and be written to the row on failure.
        import agent_config

        tid = agent_db.create_task(self.proj_id, 'T-usage', project_path=self.path,
                                   model='scw-gemma-4-26b')

        def fake_route(*a, **k):
            exc = RuntimeError('exhausted 40 tool turns')
            exc.tokens_input = 1000
            exc.tokens_output = 200
            raise exc

        built = {'mode': 'inline', 'prompt': 'do it', 'max_tokens': 1000}
        with patch.object(agent_executor.agit, 'resolve_enabled', return_value=False), \
             patch('agent_scw_session.sync_bucket_to_working_docs', return_value=None), \
             patch('prompt_builder.build', return_value=built), \
             patch.object(agent_executor, 'route', side_effect=fake_route), \
             patch.object(agent_db, 'update_budget_spend') as spend:
            result = agent_executor.run_task(tid)

        self.assertEqual(result.get('status'), 'failed')
        execution = agent_db.get_execution(result.get('exec_id'), project_path=self.path)
        self.assertEqual(execution['tokens_input'], 1000)
        self.assertEqual(execution['tokens_output'], 200)
        pricing = agent_config.get_pricing('scw-gemma-4-26b')
        expected_cost = round(
            1000 * pricing['input'] / 1_000_000 +
            200 * pricing['output'] / 1_000_000, 6)
        self.assertAlmostEqual(execution['cost_usd'], expected_cost)
        # Budget spend is rolled by route()'s choke point only — the
        # failure handler fixes the row, it must not double-roll spend.
        spend.assert_not_called()

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

    def test_vibe_turn_limit_is_not_reported_as_simulation(self):
        # Regression (exec 20001185, task #10001192): a run that exhausted the
        # Vibe turn budget executed real tools; quoted markup in its text must
        # not fail it as a simulation.
        task = {'model': 'mistral-large-latest', 'work_session_slot': None}
        text = ('Quoted: <bash>ls</bash>\n\n---\n⚠️ **'
                + agent_executor.VIBE_TURN_LIMIT_MARKER + ' (80 turns)**')
        agent_executor._raise_if_unexecuted_tool_request(text, task)  # no raise
        with self.assertRaises(RuntimeError):
            agent_executor._raise_if_unexecuted_tool_request('<bash>ls</bash>', task)

    def test_markup_stripper_removes_pseudo_json(self):
        stripped = agent_executor._strip_tool_markup(
            'bash{echo hi > a.txt}\n\nThe real answer follows.')
        self.assertNotIn('bash{', stripped)
        self.assertIn('The real answer follows.', stripped)


if __name__ == '__main__':
    unittest.main()


class TaskStatusSectionTests(unittest.TestCase):
    """Guide's '## Task status' table must survive a running execution.

    Regression (task 10001187): `r.get('started_at')` on a sqlite3.Row raised
    AttributeError, the caller swallowed it, and the whole table vanished
    whenever any task was running — Guide then guessed "stuck" from git/files.
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
        self.task_id = agent_db.create_task(proj['id'], 'T', project_path=self.path)
        self.exec_id = agent_db.create_execution(self.task_id, 'm', project_path=self.path)

    def tearDown(self):
        self._patch.stop()
        self.td.cleanup()

    def _set_heartbeat(self, sql_offset):
        conn = agent_db.get_project_db(self.path)
        conn.execute("UPDATE executions SET status='running', "
                     "started_at=datetime('now','-20 minutes'), "
                     f"last_heartbeat_at=datetime('now','{sql_offset}') WHERE id=?",
                     (self.exec_id,))
        conn.commit()
        conn.close()

    def test_running_with_fresh_heartbeat_is_alive(self):
        self._set_heartbeat('-10 seconds')
        s = agent_executor._task_status_section(self.path)
        self.assertIn(f'#{self.task_id}', s)
        self.assertIn('ALIVE, not stuck', s)

    def test_running_with_stale_heartbeat_is_flagged(self):
        self._set_heartbeat('-15 minutes')
        s = agent_executor._task_status_section(self.path)
        self.assertIn('possibly stalled', s)

    def test_failed_run_shows_code_error_and_gate(self):
        # RP-082·E1: a strict-gate abort left the task `pending` with no error
        # visible to the Guide, which then read "proceed" as a cleared gate.
        conn = agent_db.get_project_db(self.path)
        conn.execute("UPDATE executions SET status='failed', error_message=? WHERE id=?",
                     ('Guide gate (proceed): all files inlined.', self.exec_id))
        conn.execute("UPDATE tasks SET gate_state='open', gate_reason='all files inlined', "
                     "gate_report_h2_json='{\"gate\": \"proceed\"}' WHERE id=?",
                     (self.task_id,))
        conn.commit()
        conn.close()
        code = agent_db.get_task(self.task_id)['display_code']
        s = agent_executor._task_status_section(self.path)
        self.assertIn(f'| {code} | #{self.task_id} |', s)
        self.assertIn('E1 #', s)
        self.assertIn('### Last run failed', s)
        self.assertIn(f'**{code}·E1**', s)
        self.assertIn('error: Guide gate (proceed): all files inlined.', s)
        self.assertIn('gate_state=open, pre-run verdict=proceed', s)


class ChatAutoreplyContainmentTests(unittest.TestCase):
    """Phase-0 containment (2026-10-15): notification-triggered AIngel-chat
    posts must not spawn full agentic chat runs. The autonomous Guide chat
    treated every task-completion notification as license to act, and used
    its write tools to overwrite task deliverables from its own fabricated
    sources (commits 14090e4/559ab69)."""

    def setUp(self):
        # ignore_cleanup_errors: _generate_brief_async fires an H4 daemon
        # thread that can outlive the test and race the tempdir removal.
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = patch.object(agent_db, 'DB_PATH',
                                   os.path.join(self.td.name, 'aingel.db'))
        self._patch.start()
        agent_db.init_db()
        self.path = os.path.join(self.td.name, 'proj')
        os.makedirs(self.path, exist_ok=True)
        proj = agent_db.upsert_project('P', 'p', self.path)
        self.proj_id = proj['id']
        self.task_id = agent_db.create_task(self.proj_id, 'T', project_path=self.path)
        self.chat = agent_db.create_chat(self.proj_id, 'Guide',
                                         model='mistral-medium-latest',
                                         project_path=self.path)
        self.chat_id = self.chat['id']
        agent_db.set_project_field(self.proj_id, 'aingel_chat_id', self.chat_id)
        agent_db.set_project_field(self.proj_id, 'aingel_model', 'mistral-medium-latest')

    def tearDown(self):
        self._patch.stop()
        self.td.cleanup()

    def _chat_file_content(self):
        chat = agent_db.get_chat(self.chat_id)
        with open(chat['file_path'], encoding='utf-8') as f:
            return f.read()

    def _chat_execution_count(self):
        pconn = agent_db.get_project_db(self.path)
        n = pconn.execute('SELECT COUNT(*) FROM executions WHERE chat_id=?',
                          (self.chat_id,)).fetchone()[0]
        pconn.close()
        return n

    # ── the mode resolver ───────────────────────────────────────────────────
    def test_mode_honors_setting_and_falls_back(self):
        with patch.object(agent_db, 'get_app_settings',
                          return_value={'aingel_chat_autoreply': 'off'}):
            self.assertEqual(agent_executor._aingel_autoreply_mode(), 'off')
        with patch.object(agent_db, 'get_app_settings',
                          return_value={'aingel_chat_autoreply': 'READONLY'}):
            self.assertEqual(agent_executor._aingel_autoreply_mode(), 'readonly')
        with patch.object(agent_db, 'get_app_settings',
                          return_value={'aingel_chat_autoreply': 'run'}):
            self.assertEqual(agent_executor._aingel_autoreply_mode(), 'run')
        # Item 17 Phase A: unset, junk or unreadable → 'off'. The notice is
        # still appended for the user; the safe failure is "no model turn".
        with patch.object(agent_db, 'get_app_settings', return_value={}):
            self.assertEqual(agent_executor._aingel_autoreply_mode(), 'off')
        with patch.object(agent_db, 'get_app_settings',
                          return_value={'aingel_chat_autoreply': 'junk'}):
            self.assertEqual(agent_executor._aingel_autoreply_mode(), 'off')
        with patch.object(agent_db, 'get_app_settings',
                          side_effect=RuntimeError('db down')):
            self.assertEqual(agent_executor._aingel_autoreply_mode(), 'off')

    # ── run_agent=False: posted for the record, no agent run ──────────────
    def test_run_agent_false_appends_message_without_execution(self):
        result = agent_executor.chat_reply(self.chat_id, 'a notification',
                                           run_agent=False)
        self.assertTrue(result.get('posted_only'))
        self.assertIn('a notification', self._chat_file_content())
        self.assertEqual(self._chat_execution_count(), 0)

    # ── run_agent=True (default) still creates a real execution ────────────
    def test_run_agent_default_still_runs(self):
        # The legacy path must keep working: a plain chat message (e.g. typed
        # by the human in the UI) still gets its model reply.
        with patch.object(agent_executor, 'route',
                          return_value=('reply', 10, 5, 0.0)) as r, \
             patch.object(agent_executor, '_chat_tools_enabled', return_value=False), \
             patch.object(agent_executor, '_start_heartbeat', return_value=lambda: None):
            result = agent_executor.chat_reply(self.chat_id, 'hello')
        self.assertEqual(result.get('exec_id') is not None, True)
        self.assertEqual(self._chat_execution_count(), 1)
        self.assertIn('hello', self._chat_file_content())
        self.assertIn('reply', self._chat_file_content())

    # ── the H3 notification honors the mode ────────────────────────────────
    def _h3_with_mode(self, settings):
        with patch.object(agent_db, 'get_app_settings', return_value=settings), \
             patch('agent_overseer.post_run_analysis',
                   return_value={'severity': 'ok', 'findings': [],
                                 'recommendation': 'approve',
                                 'gate_for_next': 'proceed',
                                 'brief_2s': 'done', 'unread_files': []}), \
             patch.object(agent_executor, '_aingel_overview_async',
                          lambda *a, **k: None), \
             patch.object(agent_executor, 'chat_reply') as mock_reply:
            agent_executor._generate_brief_async(999, agent_db.get_task(self.task_id),
                                                self.path)
        return mock_reply

    def test_h3_post_off_mode_never_runs_the_agent(self):
        mock_reply = self._h3_with_mode({'aingel_chat_autoreply': 'off'})
        mock_reply.assert_called_once()
        self.assertIs(mock_reply.call_args.kwargs.get('run_agent'), False)
        self.assertIs(mock_reply.call_args.kwargs.get('as_notice'), True)
        self.assertEqual(self._chat_execution_count(), 0)

    def test_h3_post_readonly_mode_binds_no_tools(self):
        mock_reply = self._h3_with_mode({'aingel_chat_autoreply': 'readonly'})
        mock_reply.assert_called_once()
        self.assertIs(mock_reply.call_args.kwargs.get('tools_enabled'), False)
        self.assertIs(mock_reply.call_args.kwargs.get('as_notice'), True)

    def test_h3_post_default_mode_is_off(self):
        mock_reply = self._h3_with_mode({})
        mock_reply.assert_called_once()
        self.assertIs(mock_reply.call_args.kwargs.get('run_agent'), False)
        self.assertIs(mock_reply.call_args.kwargs.get('as_notice'), True)

    def test_h3_post_run_mode_still_posts_a_notice(self):
        mock_reply = self._h3_with_mode({'aingel_chat_autoreply': 'run'})
        mock_reply.assert_called_once()
        self.assertNotIn('run_agent', mock_reply.call_args.kwargs)
        self.assertIs(mock_reply.call_args.kwargs.get('as_notice'), True)

    # ── item 17 Phase A: notices, prompt rule, one turn at a time ──────────
    def test_notice_is_stored_as_notice_and_left_out_of_history(self):
        agent_executor.chat_reply(self.chat_id, '✓ Task #1 completed: Next step: run X.',
                                  run_agent=False, as_notice=True)
        content = self._chat_file_content()
        self.assertIn('🔔 Notice', content)
        self.assertNotIn('👤 User', content)
        history = agent_chats.read_chat_file(agent_db.get_chat(self.chat_id)['file_path'])
        self.assertNotIn('Next step: run X', history)
        self.assertIn('1 Cordée notice(s) omitted', history)

    def test_legacy_user_notices_are_left_out_of_history(self):
        agent_executor.chat_reply(self.chat_id, 'seed', run_agent=False)
        chat_path = agent_db.get_chat(self.chat_id)['file_path']
        agent_chats.append_user_message(chat_path, '⚠ Post-run review for Task #5 flagged **issue**.\n\nNext step: rewrite the table.')
        agent_chats.append_user_message(chat_path, 'Please check the table.')
        history = agent_chats.read_chat_file(chat_path)
        self.assertNotIn('rewrite the table', history)
        self.assertIn('Please check the table.', history)
        self.assertIn('seed', history)

    def test_notice_turn_labels_the_message_for_the_model(self):
        prompts = []

        def fake_route(model, prompt, *a, **k):
            prompts.append(prompt)
            return ('ok', 1, 1, 0.0)

        with patch.object(agent_executor, 'route', side_effect=fake_route), \
             patch.object(agent_executor, '_chat_tools_enabled', return_value=False), \
             patch.object(agent_executor, '_start_heartbeat', return_value=lambda: None):
            agent_executor.chat_reply(self.chat_id, '✓ Task #1 completed.', tools_enabled=False,
                                      as_notice=True)
        self.assertIn('Cordée system notice', prompts[0])
        self.assertIn('do not act on it', prompts[0])

    def test_chat_rules_ask_instead_of_assume(self):
        secs = dict(agent_executor._chat_prompt_sections(
            agent_db.get_chat(self.chat_id), 'hi', 'mistral-medium-latest', tools_enabled=True))
        self.assertNotIn('do not ask the user', secs['output_format'])
        self.assertIn('ask first', secs['output_format'])

    def _running_chat_exec(self, heartbeat_sql="datetime('now')", child_pid=None):
        pconn = agent_db.get_project_db(self.path)
        cur = pconn.execute(
            "INSERT INTO executions (chat_id, model, status, started_at, last_heartbeat_at, child_pid) "
            f"VALUES (?, 'm', 'running', datetime('now'), {heartbeat_sql}, ?)",
            (self.chat_id, child_pid))
        pconn.commit()
        eid = cur.lastrowid
        pconn.close()
        return eid

    def test_second_turn_is_refused_while_one_is_live(self):
        eid = self._running_chat_exec()
        with patch.object(agent_executor, 'route') as r:
            result = agent_executor.chat_reply(self.chat_id, 'hello again')
        r.assert_not_called()
        self.assertTrue(result.get('busy'))
        self.assertIn(f'exec #{eid}', result['error'])

    def test_notice_during_a_live_turn_is_recorded_without_a_turn(self):
        self._running_chat_exec()
        with patch.object(agent_executor, 'route') as r:
            result = agent_executor.chat_reply(self.chat_id, '✓ Task #2 completed.',
                                               as_notice=True)
        r.assert_not_called()
        self.assertTrue(result.get('posted_only'))
        self.assertIn('✓ Task #2 completed.', self._chat_file_content())

    def test_stale_turn_without_live_child_does_not_block(self):
        self._running_chat_exec(heartbeat_sql="datetime('now', '-30 minutes')",
                                child_pid=2 ** 22 + 7)   # no such pid
        with patch.object(agent_executor, 'route', return_value=('reply', 1, 1, 0.0)), \
             patch.object(agent_executor, '_chat_tools_enabled', return_value=False), \
             patch.object(agent_executor, '_start_heartbeat', return_value=lambda: None):
            result = agent_executor.chat_reply(self.chat_id, 'hello')
        self.assertNotIn('busy', result)


class GuideChatReadOnlyTests(unittest.TestCase):
    """Item 17 Phase B: turns in the project's Guide chat run in Guide mode
    (read-only tools, writes only in Artifacts/chats/<id>/, commits limited to
    Artifacts/chats); other chats keep their tools."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = patch.object(agent_db, 'DB_PATH', os.path.join(self.td.name, 'aingel.db'))
        self._patch.start()
        agent_db.init_db()
        self.path = os.path.join(self.td.name, 'proj')
        os.makedirs(self.path)
        proj = agent_db.upsert_project('P', 'p', self.path)
        self.proj_id = proj['id']
        self.guide = agent_db.create_chat(self.proj_id, 'Guide', model='mistral-medium-latest',
                                          project_path=self.path)
        self.other = agent_db.create_chat(self.proj_id, 'Other', model='mistral-medium-latest',
                                          project_path=self.path)
        agent_db.set_project_field(self.proj_id, 'aingel_chat_id', self.guide['id'])

    def tearDown(self):
        self._patch.stop()
        self.td.cleanup()

    def _turn(self, chat_id):
        seen = {}

        def fake_route(model, prompt, *a, **k):
            seen['scope'] = agent_router.current_guide_scope()
            seen['prompt'] = prompt
            return ('reply', 1, 1, 0.0)

        with patch.object(agent_executor, 'route', side_effect=fake_route), \
             patch.object(agent_executor, '_chat_tools_enabled', return_value=True), \
             patch.object(agent_executor, '_start_heartbeat', return_value=lambda: None):
            agent_executor.chat_reply(chat_id, 'hello')
        return seen

    def test_guide_chat_turn_runs_in_guide_scope(self):
        seen = self._turn(self.guide['id'])
        want = os.path.realpath(os.path.join(self.path, 'Artifacts', 'chats', str(self.guide['id'])))
        self.assertEqual(seen['scope']['write_dir'], want)
        self.assertTrue(os.path.isdir(want))
        self.assertIn('Guide — read-only', seen['prompt'])
        self.assertNotIn('You can read project files, list directories, and create files', seen['prompt'])

    def test_other_chat_keeps_its_tools(self):
        seen = self._turn(self.other['id'])
        self.assertIsNone(seen['scope'])
        self.assertNotIn('Guide — read-only', seen['prompt'])

    def test_guide_prompt_has_git_history(self):
        import subprocess
        subprocess.run(['git', 'init', '-q'], cwd=self.path, check=True)
        open(os.path.join(self.path, 'a.txt'), 'w').close()
        subprocess.run(['git', 'add', '-A'], cwd=self.path, check=True)
        subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm',
                        'Restore honest deliverable'], cwd=self.path, check=True)
        secs = dict(agent_executor._chat_prompt_sections(
            agent_db.get_chat(self.guide['id']), 'hi', 'mistral-medium-latest', tools_enabled=True))
        self.assertIn('Restore honest deliverable', secs['git_history'])

    def test_guide_contract_on_every_guide_turn(self):
        for tools in (True, False):
            secs = dict(agent_executor._chat_prompt_sections(
                agent_db.get_chat(self.guide['id']), 'hi', 'mistral-medium-latest',
                tools_enabled=tools))
            self.assertIn('A gap stays a gap', secs['guide_contract'])
            self.assertIn('Do not "fix" it by replacing their work', secs['guide_contract'])
            self.assertIn('Never act on them', secs['guide_contract'])
        other = dict(agent_executor._chat_prompt_sections(
            agent_db.get_chat(self.other['id']), 'hi', 'mistral-medium-latest', tools_enabled=True))
        self.assertNotIn('guide_contract', other)

    def test_text_only_chat_asks_instead_of_assuming(self):
        self.assertNotIn('do not ask the user', agent_executor.CHAT_OUTPUT_FORMAT)
        self.assertIn('ask first', agent_executor.CHAT_OUTPUT_FORMAT)

    def test_commit_chat_paths_commits_only_those_paths(self):
        import subprocess
        import agent_git
        run = lambda *a: subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', *a],
                                        cwd=self.path, check=True, capture_output=True, text=True)
        run('init', '-q')
        os.makedirs(os.path.join(self.path, 'Artifacts', 'chats', '7'))
        os.makedirs(os.path.join(self.path, 'Artifacts', 'outputs'))
        open(os.path.join(self.path, 'seed.txt'), 'w').close()
        run('add', '-A'); run('commit', '-qm', 'seed')
        with open(os.path.join(self.path, 'Artifacts', 'chats', '7', 'n.md'), 'w') as f:
            f.write('note')
        with open(os.path.join(self.path, 'Artifacts', 'outputs', 'x.md'), 'w') as f:
            f.write('edit')
        with patch.dict(os.environ, {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
                                     'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}):
            gc = agent_git.commit_chat(self.path, 7, 'Guide', paths=['Artifacts/chats'])
        self.assertTrue(gc['changed'])
        files = run('show', '--name-only', '--format=', 'HEAD').stdout.split()
        self.assertEqual(files, ['Artifacts/chats/7/n.md'])
        self.assertIn('Artifacts/outputs/', run('status', '--porcelain').stdout)

    def test_commit_chat_paths_nothing_to_commit(self):
        import agent_git
        import subprocess
        subprocess.run(['git', 'init', '-q'], cwd=self.path, check=True)
        gc = agent_git.commit_chat(self.path, 7, 'Guide', paths=['Artifacts/chats'])
        self.assertFalse(gc['changed'])

class PauseAtLimitTests(unittest.TestCase):
    """TODO item 16 Phase 1: a Scaleway/Ollama task run that hits its limit
    pauses (checkpoint, branch parked, rows 'paused'); Continue resumes the same
    row and branch; Stop finishes it (review) or drops it (discard)."""

    GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patches = [patch.object(agent_db, 'DB_PATH', os.path.join(self.td.name, 'aingel.db')),
                         patch.dict(os.environ, self.GIT_ENV)]
        for p in self._patches:
            p.start()
        agent_db.init_db()
        self.path = os.path.join(self.td.name, 'proj')
        os.makedirs(self.path)
        proj = agent_db.upsert_project('P', 'p', self.path)
        self.proj_id = proj['id']
        self.guide = agent_db.create_chat(self.proj_id, 'Guide', model='m', project_path=self.path)
        agent_db.set_project_field(self.proj_id, 'aingel_chat_id', self.guide['id'])
        self.task_id = agent_db.create_task(proj['id'], 'Map routes', project_path=self.path)
        agent_db.update_task(self.task_id, project_path=self.path, model='scw-qwen3.6-35b')
        self.calls = []

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.td.cleanup()

    def _cp(self):
        return {'messages': [{'role': 'user', 'content': 'do it'},
                             {'role': 'assistant', 'content': 'Done 1-4, left 5-8.'}],
                'transcript': ['read tiles 1-4', '--- ⏸ Paused ---'], 'reason': 'turns',
                'turns': 60, 'elapsed_s': 600, 'status_text': 'Done 1-4, left 5-8.'}

    def _route(self, outcomes):
        """Fake route: each run_task call pops the next outcome ('pause' or text)."""
        def fake_route(model, prompt, *a, **k):
            if not str(k.get('caller', '')).startswith('run_task'):
                return ('brief', 0, 0, 0.0)
            out = outcomes.pop(0)
            self.calls.append(agent_router.current_pause_scope())
            if out == 'pause':
                with open(os.path.join(self.path, 'part.md'), 'w') as f:
                    f.write('rows 1-4')
                e = agent_router.LimitReached('turns', self._cp())
                e.tokens_input, e.tokens_output = 1000, 100
                raise e
            with open(os.path.join(self.path, 'part.md'), 'a') as f:
                f.write('\nrows 5-8')
            return (out, 500, 50, 0.0)
        return fake_route

    def _run(self, fn, outcomes, *a, **k):
        built = {'mode': 'inline', 'prompt': 'do it', 'max_tokens': 1000}
        with patch('agent_scw_session.sync_bucket_to_working_docs', return_value=None), \
             patch('prompt_builder.build', return_value=built), \
             patch.object(agent_executor, '_generate_brief_async', lambda *a, **k: None), \
             patch.object(agent_executor, 'route', side_effect=self._route(outcomes)):
            return fn(*a, **k)

    def _git(self, *args):
        import subprocess
        return subprocess.run(['git', *args], cwd=self.path, capture_output=True, text=True).stdout.strip()

    def _pause(self):
        res = self._run(agent_executor.run_task, ['pause'], self.task_id)
        self.assertEqual(res['status'], 'paused', res)
        return res['exec_id']

    def test_limit_pauses_the_run(self):
        eid = self._pause()
        row = agent_db.get_execution(eid, project_path=self.path)
        self.assertEqual((row['status'], row['pause_reason'], row['segments']), ('paused', 'turns', 1))
        self.assertEqual((row['tokens_input'], row['tokens_output']), (1000, 100))
        self.assertEqual(agent_db.get_task(self.task_id, project_path=self.path)['status'], 'paused')
        self.assertIsNotNone(agent_executor.load_checkpoint(self.path, eid))
        self.assertIsNotNone(self.calls[0])                       # ran under pause_scope
        # Branch parked: work committed on it, base checked out and clean.
        self.assertNotEqual(self._git('rev-parse', '--abbrev-ref', 'HEAD'), row['git_branch'])
        self.assertIn('paused at limit (part 1)', self._git('log', row['git_branch'], '-1', '--format=%s'))
        self.assertFalse(os.path.exists(os.path.join(self.path, 'part.md')))
        chat = agent_db.get_chat(self.guide['id'])
        with open(chat['file_path']) as f:
            notice = f.read()
        self.assertIn('🔔 Notice', notice)
        self.assertIn('paused at its turn limit', notice)
        self.assertIn('Done 1-4, left 5-8.', notice)

    def test_continue_resumes_same_row_and_branch(self):
        eid = self._pause()
        res = self._run(agent_executor.continue_execution, ['Table written.'], eid,
                        note='write the table now')
        self.assertEqual(res['status'], 'done', res)
        self.assertEqual(res['exec_id'], eid)
        self.assertEqual(self.calls[1]['resume']['note'], 'write the table now')
        row = agent_db.get_execution(eid, project_path=self.path)
        self.assertEqual((row['status'], row['segments']), ('done', 2))
        self.assertEqual((row['tokens_input'], row['tokens_output']), (1500, 150))
        self.assertIsNone(agent_executor.load_checkpoint(self.path, eid))
        # Merged into the base branch despite GUIDE.md changing on both sides
        # while it was paused (the RP-094 conflict): both parts' work is on disk.
        self.assertTrue(row.get('git_merge_commit'), row)
        with open(os.path.join(self.path, 'part.md')) as f:
            content = f.read()
        self.assertIn('rows 1-4', content)
        self.assertIn('rows 5-8', content)

    def test_pausing_again_counts_parts_and_max_continues_refuses(self):
        eid = self._pause()
        with patch.object(agent_executor, 'MAX_CONTINUES', 1):
            res = self._run(agent_executor.continue_execution, ['pause'], eid)
            self.assertEqual(res['status'], 'paused')
            row = agent_db.get_execution(eid, project_path=self.path)
            self.assertEqual(row['segments'], 2)
            refused = agent_executor.continue_execution(eid)
        self.assertEqual(refused['status'], 'refused')
        self.assertIn('already been continued 1 times', refused['error'])

    def test_stop_and_review_finishes_without_a_model_call(self):
        eid = self._pause()
        res = self._run(agent_executor.stop_execution, [], eid, discard=False)
        self.assertEqual(res['status'], 'done', res)
        self.assertEqual(len(self.calls), 1)                      # no second model call
        row = agent_db.get_execution(eid, project_path=self.path)
        self.assertEqual((row['status'], row['segments'], row['pause_reason']), ('done', 1, 'turns'))
        out = agent_executor._output_file_path(self.path, eid)
        with open(out) as f:
            text = f.read()
        self.assertIn(agent_router.TOOL_TURN_LIMIT_MARKER, text)
        self.assertIn('stopped by the user at the limit', text)
        self.assertIsNone(agent_executor.load_checkpoint(self.path, eid))

    def test_stop_and_discard_drops_branch_and_resets_task(self):
        eid = self._pause()
        branch = agent_db.get_execution(eid, project_path=self.path)['git_branch']
        res = agent_executor.stop_execution(eid, discard=True)
        self.assertEqual(res['status'], 'discarded')
        row = agent_db.get_execution(eid, project_path=self.path)
        self.assertEqual(row['status'], 'failed')
        self.assertIn('changes discarded', row['error_message'])
        self.assertEqual(agent_db.get_task(self.task_id, project_path=self.path)['status'], 'pending')
        self.assertEqual(self._git('branch', '--list', branch), '')
        self.assertIsNone(agent_executor.load_checkpoint(self.path, eid))

    def test_actions_on_a_non_paused_run_are_refused(self):
        eid = self._pause()
        agent_executor.stop_execution(eid, discard=True)
        self.assertEqual(agent_executor.continue_execution(eid)['status'], 'refused')
        self.assertEqual(agent_executor.stop_execution(eid)['status'], 'refused')

    def test_orphan_sweep_leaves_paused_rows_alone(self):
        eid = self._pause()
        pconn = agent_db.get_project_db(self.path)
        pconn.execute("UPDATE executions SET last_heartbeat_at=datetime('now','-1 day') WHERE id=?", (eid,))
        pconn.commit(); pconn.close()
        agent_db.reset_orphaned_executions('tick')
        self.assertEqual(agent_db.get_execution(eid, project_path=self.path)['status'], 'paused')

    def test_pause_report_facts(self):
        agent_db.update_task(self.task_id, project_path=self.path,
                             description='Read the map.\n\nOutput a single markdown file named `routes.md`.')
        cp = self._cp()
        cp['transcript'] = ['→ `read_file` `tiles/a.png`\n' * 5 + '→ `bash`']

        def fake_route(model, prompt, *a, **k):
            if not str(k.get('caller', '')).startswith('run_task'):
                return ('brief', 0, 0, 0.0)
            with open(os.path.join(self.path, 'notes.md'), 'w') as f:
                f.write('x')
            e = agent_router.LimitReached('turns', cp)
            e.tokens_input, e.tokens_output = 1000, 100
            raise e

        built = {'mode': 'inline', 'prompt': 'do it', 'max_tokens': 1000}
        with patch('agent_scw_session.sync_bucket_to_working_docs', return_value=None), \
             patch('prompt_builder.build', return_value=built), \
             patch.object(agent_executor, 'route', side_effect=fake_route):
            res = agent_executor.run_task(self.task_id)
        row = agent_db.get_execution(res['exec_id'], project_path=self.path)
        report = json.loads(row['pause_report_json'])
        f = report['facts']
        self.assertEqual(f['deliverables_missing'], ['routes.md'])
        self.assertEqual(f['deliverables_written'], [])
        self.assertIn('notes.md', f['files_changed'])
        self.assertEqual(f['repeated_actions'][0]['times'], 5)
        self.assertEqual(f['continues_left'], agent_executor.MAX_CONTINUES)
        self.assertIsNone(report['guide'])                     # no Guide model in this project

    def test_publish_adds_guide_read_and_posts_one_notice(self):
        agent_db.set_project_field(self.proj_id, 'aingel_model', 'mistral-glm-5-3')
        proj = agent_db.get_project(self.proj_id)
        task = agent_db.get_task(self.task_id, project_path=self.path)
        eid = agent_db.create_execution(self.task_id, 'scw-qwen3.6-35b', project_path=self.path)
        guide = {'progress': 'progressing', 'recommendation': 'continue_with_note',
                 'suggested_note': 'Write routes.md now and append rows as you go.',
                 'summary': 'Read half the tiles.', 'model': 'mistral-glm-5-3'}

        class SyncThread:
            def __init__(self, target, **k): self.t = target
            def start(self): self.t()

        with patch.object(agent_executor._threading, 'Thread', SyncThread), \
             patch('agent_overseer.pause_report_summary', return_value=guide):
            agent_executor._publish_pause_report(proj, task, eid, self.path,
                                                 {'limit': 'turn', 'part': 1, 'turns_this_part': 60,
                                                  'model_status': 'half done'}, 'transcript')
        report = json.loads(agent_db.get_execution(eid, project_path=self.path)['pause_report_json'])
        self.assertEqual(report['guide']['recommendation'], 'continue_with_note')
        with open(agent_db.get_chat(self.guide['id'])['file_path']) as fh:
            chat = fh.read()
        self.assertEqual(chat.count('🔔 Notice'), 1)
        self.assertIn('Suggested note:** Write routes.md now', chat)
        self.assertIn('Guide (progressing)', chat)

    def test_merge_after_pause_resolves_guide_conflict(self):
        # RP-094: GUIDE.md changed on main ("Checkpoint before resuming") and on
        # the branch while the run waited; the merge aborted and the deliverable
        # stayed on the branch. Generated-file conflicts now resolve.
        with open(os.path.join(self.path, 'GUIDE.md'), 'w') as f:
            f.write('# Guide\n- [ ] Map routes\n')
        eid = self._pause()
        import subprocess
        def git(*a):
            return subprocess.run(['git', *a], cwd=self.path, capture_output=True, text=True)
        branch = agent_db.get_execution(eid, project_path=self.path)['git_branch']
        git('checkout', branch)
        with open(os.path.join(self.path, 'GUIDE.md'), 'w') as f:
            f.write('# Guide\n- [ ] 🔄 Map routes\n')
        git('commit', '-qam', 'branch side')
        git('checkout', 'master'); git('checkout', 'main')
        with open(os.path.join(self.path, 'GUIDE.md'), 'w') as f:
            f.write('# Guide\n- [ ] ⏸ Map routes\n')
        git('commit', '-qam', 'main side')
        res = agent_executor.agit.merge_task_branch(self.path, branch, self.task_id)
        self.assertTrue(res.get('ok'), res)
        self.assertTrue(os.path.exists(os.path.join(self.path, 'part.md')))

    def test_merge_with_real_conflict_still_aborts(self):
        import subprocess
        def git(*a):
            return subprocess.run(['git', *a], cwd=self.path, capture_output=True, text=True)
        eid = self._pause()
        branch = agent_db.get_execution(eid, project_path=self.path)['git_branch']
        base = self._git('rev-parse', '--abbrev-ref', 'HEAD')
        with open(os.path.join(self.path, 'part.md'), 'w') as f:
            f.write('main edit')
        git('add', '-A'); git('commit', '-qm', 'main edits part.md')
        res = agent_executor.agit.merge_task_branch(self.path, branch, self.task_id)
        self.assertFalse(res.get('ok'))
        self.assertIn('conflict', res.get('error', ''))
        self.assertEqual(self._git('rev-parse', '--abbrev-ref', 'HEAD'), base)

    def test_pause_facts_file_names_and_repeats(self):
        cp = self._cp()
        cp['transcript'] = ['\n'.join(['→ `bash`'] * 20 + ['→ `read_file` `a.png`'] * 4)]
        with open(os.path.join(self.path, 'GUIDE.md'), 'w') as f:
            f.write('x')
        import subprocess
        subprocess.run(['git', 'init', '-q'], cwd=self.path)
        subprocess.run(['git', 'add', '-A'], cwd=self.path)
        subprocess.run(['git', 'commit', '-qm', 'seed'], cwd=self.path)
        with open(os.path.join(self.path, 'GUIDE.md'), 'w') as f:
            f.write('y')
        facts = agent_executor._pause_facts(agent_db.get_task(self.task_id, project_path=self.path),
                                            self.path, {}, cp, 1, 1, 1, 0.1, 0.1)
        self.assertIn('GUIDE.md', facts['files_changed'])
        self.assertEqual(facts['repeated_actions'], [{'action': '→ `read_file` `a.png`', 'times': 4}])

    def test_work_dir_is_git_ignored(self):
        self.assertIn('Artifacts/outputs/**/_work/', agent_executor.agit.ARTIFACTS_IGNORE_RULES)
        self.assertIn('Artifacts/outputs/**/_work/', agent_executor.agit._RUNTIME_IGNORE_RULES)

    def _client(self):
        import agent_api
        agent_api.app.config['TESTING'] = True
        return agent_api.app.test_client()

    def test_api_stop_discard_and_refusals(self):
        eid = self._pause()
        with patch.dict(os.environ, {'AINGEL_AUTH': 'off'}):
            c = self._client()
            r = c.post(f'/api/executions/{eid}/stop', json={'discard': True})
            self.assertEqual(r.status_code, 200, r.get_json())
            self.assertEqual(r.get_json()['status'], 'discarded')
            r = c.post(f'/api/executions/{eid}/continue', json={'note': 'x'})
            self.assertEqual(r.status_code, 409)
            r = c.post(f'/api/executions/{eid}/stop', json={})
            self.assertEqual(r.status_code, 409)

    def test_api_cancel_on_paused_run_discards(self):
        eid = self._pause()
        with patch.dict(os.environ, {'AINGEL_AUTH': 'off'}):
            r = self._client().post(f'/api/executions/{eid}/cancel')
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(agent_db.get_task(self.task_id, project_path=self.path)['status'], 'pending')

    def test_api_continue_starts_in_background(self):
        eid = self._pause()
        started = []
        with patch.dict(os.environ, {'AINGEL_AUTH': 'off'}):
            import agent_api
            with patch.object(agent_api, '_run_paused_action_bg',
                              side_effect=lambda fn, e, **kw: started.append((fn.__name__, e, kw))):
                r = self._client().post(f'/api/executions/{eid}/continue', json={'note': 'go on'})
        self.assertEqual(r.status_code, 202, r.get_json())
        self.assertEqual(started, [('continue_execution', eid, {'note': 'go on'})])

    def test_non_tool_loop_models_are_not_pausable(self):
        self.assertTrue(agent_executor._pausable_route('scw-qwen3.6-35b'))
        self.assertFalse(agent_executor._pausable_route('mistral-glm-5-3'))

class OrphanCliStopTests(unittest.TestCase):
    """The orphan sweep stops the agent CLI of a run whose supervisor died
    (2026-10-15: Guide Vibe children kept committing after their rows were
    marked orphaned), and leaves every other process alone."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.proj = os.path.realpath(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def _spawn(self, name, cwd):
        import shutil, subprocess
        bindir = os.path.join(self.td.name, 'bin')
        os.makedirs(bindir, exist_ok=True)
        exe = os.path.join(bindir, name)
        shutil.copy('/bin/sleep', exe)
        p = subprocess.Popen([exe, '30'], cwd=cwd, start_new_session=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        # Wait until the child has exec'd: right after fork its cmdline is
        # still the test runner's, which the sweep rightly refuses to stop.
        import time
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with open(f'/proc/{p.pid}/cmdline', 'rb') as f:
                    if os.path.basename(f.read().split(b'\0')[0].decode()) == name:
                        break
            except OSError:
                pass
            time.sleep(0.01)
        return p

    def test_stops_known_cli_inside_project(self):
        p = self._spawn('vibe', self.proj)
        self.assertTrue(agent_db._stop_orphan_cli(p.pid, self.proj, 1))
        self.assertIsNotNone(p.wait(timeout=5))

    def test_leaves_unknown_process_alone(self):
        p = self._spawn('sleep', self.proj)
        self.assertFalse(agent_db._stop_orphan_cli(p.pid, self.proj, 1))
        self.assertIsNone(p.poll())

    def test_leaves_cli_outside_project_alone(self):
        other = tempfile.mkdtemp()
        self.addCleanup(lambda: os.rmdir(other))
        p = self._spawn('vibe', other)
        self.assertFalse(agent_db._stop_orphan_cli(p.pid, self.proj, 1))
        self.assertIsNone(p.poll())

    def test_missing_pid_is_a_no_op(self):
        self.assertFalse(agent_db._stop_orphan_cli(None, self.proj, 1))
        self.assertFalse(agent_db._stop_orphan_cli(2 ** 22 + 7, self.proj, 1))


class MissingDeliverableWarningTests(unittest.TestCase):
    """A task that names an output file but leaves it unwritten must say so on
    the run output (task #20001312 echoed the report inline and finished done)."""

    NAME = 'validation_report_polish_transport_plan.md'

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.proj = os.path.join(self.td.name, 'proj')
        os.makedirs(self.proj, exist_ok=True)
        self.task = {
            'id': 20001312,
            'title': 'Polish the transport plan',
            'description': ('Write a validation report named `%s` covering the '
                            'transport plan polish.' % self.NAME),
            '_output_dir': os.path.join(self.proj, 'Artifacts', 'outputs',
                                        '20001312-polish'),
        }

    def tearDown(self):
        self.td.cleanup()

    def _write_output(self):
        out = self.task['_output_dir']
        os.makedirs(out, exist_ok=True)
        path = os.path.join(out, self.NAME)
        with open(path, 'w') as f:
            f.write('# report\n')
        return path

    def test_missing_named_file_warns(self):
        warn = agent_executor._missing_deliverable_warning(
            self.task, self.proj, 'Here is the report inline …',
            started_at=time.time() - 60)
        self.assertIsNotNone(warn)
        self.assertIn('Deliverable not written', warn)
        self.assertIn(self.NAME, warn)

    def test_written_named_file_does_not_warn(self):
        self._write_output()
        warn = agent_executor._missing_deliverable_warning(
            self.task, self.proj, 'Done.',
            started_at=time.time() - 60)
        self.assertIsNone(warn)

    def test_no_file_creation_intent_does_not_warn(self):
        task = {'id': 1, 'title': 'Read and summarise',
                'description': 'Summarise the attached PDF and list the risks.',
                '_output_dir': self.task['_output_dir']}
        warn = agent_executor._missing_deliverable_warning(
            task, self.proj, 'Summary …', started_at=time.time() - 60)
        self.assertIsNone(warn)

    def test_internal_error_is_swallowed(self):
        warn = agent_executor._missing_deliverable_warning(
            self.task, '/nonexistent/not-a-project', 'text',
            started_at=time.time() - 60)
        self.assertIsNone(warn)
