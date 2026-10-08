import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import agent_router
import agent_chats
import agent_overseer
import prompt_builder


class _FakePopen:
    """Test double for subprocess.Popen. Returns canned stdout/stderr and a
    returncode from communicate(), mimicking the post-Popen conversion shape
    used by _call_claude_cli / _call_vibe_cli / _call_codex_cli."""
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.pid = 99999
        self.stdin = None

    def communicate(self, input=None, timeout=None):
        return self._stdout, self._stderr

    def kill(self):
        pass


class AgentRouterCliTests(unittest.TestCase):
    def test_claude_permission_denial_is_error(self):
        payload = {
            'type': 'result',
            'subtype': 'success',
            'is_error': False,
            'result': '',
            'stop_reason': 'end_turn',
            'permission_denials': [
                {
                    'tool_name': 'AskUserQuestion',
                    'tool_input': {
                        'questions': [
                            {'question': 'Which folder should the test run target?'},
                        ],
                    },
                },
            ],
            'usage': {'input_tokens': 1, 'output_tokens': 1},
        }
        fake = _FakePopen(returncode=0, stdout=json.dumps(payload), stderr='')

        with patch('shutil.which', return_value='/usr/bin/claude'), \
             patch('subprocess.Popen', return_value=fake):
            with self.assertRaisesRegex(RuntimeError, 'claude CLI permission denied.*Which folder'):
                agent_router._call_claude_cli('claude-opus-4-7', 'prompt', project_path=os.getcwd())

    def test_claude_permission_denial_is_error_even_with_wrappers(self):
        payload = {
            'type': 'result',
            'subtype': 'success',
            'is_error': False,
            'result': '',
            'stop_reason': 'end_turn',
            'permission_denials': [
                {
                    'tool_name': 'AskUserQuestion',
                    'tool_input': {
                        'questions': [
                            {'question': 'Which folder should the test run target?'},
                        ],
                    },
                },
            ],
            'usage': {'input_tokens': 1, 'output_tokens': 1},
        }
        wrapped = f'INFO: starting\n{json.dumps(payload)}\nINFO: done'
        fake = _FakePopen(returncode=0, stdout=wrapped, stderr='')

        with patch('shutil.which', return_value='/usr/bin/claude'), \
             patch('subprocess.Popen', return_value=fake):
            with self.assertRaisesRegex(RuntimeError, 'claude CLI permission denied.*Which folder'):
                agent_router._call_claude_cli('claude-opus-4-7', 'prompt', project_path=os.getcwd())

    def test_codex_chatgpt_uses_explicit_supported_model(self):
        seen = {}

        class _CodexFakePopen(_FakePopen):
            def __init__(self_inner, cmd, *a, **k):
                seen['cmd'] = cmd
                output_path = cmd[cmd.index('--output-last-message') + 1]
                with open(output_path, 'w', encoding='utf-8') as f:
                    f.write('OK')
                super().__init__(returncode=0, stdout='', stderr='')

        with patch('shutil.which', return_value='/usr/bin/codex'), \
             patch('subprocess.Popen', side_effect=_CodexFakePopen):
            text, tok_in, tok_out, cost = agent_router._call_codex_cli(
                'codex-chatgpt',
                'Reply exactly OK.',
                project_path=os.getcwd(),
            )

        self.assertEqual(text, 'OK')
        self.assertEqual(cost, 0.0)
        self.assertGreater(tok_in, 0)
        self.assertGreater(tok_out, 0)
        model_arg = seen['cmd'][seen['cmd'].index('--model') + 1]
        self.assertEqual(model_arg, agent_router.CODEX_CHATGPT_DEFAULT_MODEL)

    def test_vibe_cli_uses_devnull_stdin(self):
        # Regression: _call_vibe_cli must pass stdin=subprocess.DEVNULL so vibe
        # doesn't inherit the parent's stdin. When AIngel is launched via nohup
        # or systemd, the parent's fd 0 is /dev/null (not a TTY); vibe's
        # get_prompt_from_stdin() then calls sys.stdin.read() which hits
        # OSError [Errno 9] Bad file descriptor because vibe's own pre-read
        # setup invalidates fd 0. Explicit DEVNULL gives vibe a clean,
        # always-EOF stdin regardless of how AIngel was launched — mirroring
        # _call_claude_cli (stdin=PIPE) and _call_codex_cli (stdin=PIPE).
        # Regression: exec #20000964, a client project, 2026-08-27.
        seen = {}

        class _VibeFakePopen(_FakePopen):
            def __init__(self_inner, cmd, *a, **k):
                seen['kwargs'] = k
                # Return a minimal valid vibe JSON array so the parser is happy
                super().__init__(returncode=0,
                                 stdout='[{"role":"assistant","content":"OK"}]',
                                 stderr='')

        with patch('shutil.which', return_value='/usr/bin/vibe'), \
             patch.object(agent_router, 'MISTRAL_VIBE_KEY', 'test-key'), \
             patch('subprocess.Popen', side_effect=_VibeFakePopen):
            text, _ti, _to, _cost = agent_router._call_vibe_cli(
                'mistral-medium-latest',
                'Reply exactly OK.',
                project_path=os.getcwd(),
            )

        self.assertEqual(text, 'OK')
        self.assertIn('stdin', seen['kwargs'])
        self.assertEqual(seen['kwargs']['stdin'], subprocess.DEVNULL)

    def test_vibe_cli_timeout_is_not_retried(self):
        # Regression (task 10001187): a run that outlived the wall-clock cap was
        # retried from scratch 3x (each attempt cold, each timing out again).
        # A timeout must spawn the CLI once, raise VibeCLITimeout with a short
        # message (no argv/prompt dump), and use VIBE_CLI_TIMEOUT_SECS.
        spawns = []

        class _SlowPopen(_FakePopen):
            def __init__(self_inner, cmd, *a, **k):
                spawns.append(cmd)
                super().__init__()
                self_inner._first = True

            def communicate(self_inner, input=None, timeout=None):
                if self_inner._first:
                    self_inner._first = False
                    spawns.append(('timeout', timeout))
                    raise subprocess.TimeoutExpired(['vibe'], timeout)
                return '', ''

        with patch('shutil.which', return_value='/usr/bin/vibe'), \
             patch.object(agent_router, 'MISTRAL_VIBE_KEY', 'test-key'), \
             patch.object(agent_router.agent_config, 'VIBE_CLI_TIMEOUT_SECS', 1234), \
             patch.object(agent_router, '_kill_process_group', lambda p: None), \
             patch('subprocess.Popen', side_effect=_SlowPopen):
            with self.assertRaises(agent_router.VibeCLITimeout) as cm:
                agent_router._call_vibe_cli(
                    'mistral-medium-latest', 'secret prompt text',
                    project_path=os.getcwd())

        self.assertEqual(len([c for c in spawns if isinstance(c, list)]), 1)
        self.assertIn(('timeout', 1234), spawns)
        self.assertNotIn('secret prompt text', str(cm.exception))

    def test_vibe_cli_caps_oversized_prompt(self):
        # Regression: the vibe prompt is passed on the argv (`--prompt <prompt>`),
        # so an oversized prompt (e.g. a very long chat thread) would raise
        # `OSError [Errno 7] Argument list too long: 'vibe'` before vibe runs.
        # _call_vibe_cli must truncate to VIBE_PROMPT_MAX_CHARS and keep the tail.
        # Regression: exec #20000977, a client project, 2026-08-28.
        seen = {}

        class _VibeFakePopen(_FakePopen):
            def __init__(self_inner, cmd, *a, **k):
                seen['cmd'] = cmd
                super().__init__(returncode=0,
                                 stdout='[{"role":"assistant","content":"OK"}]',
                                 stderr='')

        big_prompt = 'x' * (agent_router.VIBE_PROMPT_MAX_BYTES + 5000)

        with patch('shutil.which', return_value='/usr/bin/vibe'), \
             patch.object(agent_router, 'MISTRAL_VIBE_KEY', 'test-key'), \
             patch('subprocess.Popen', side_effect=_VibeFakePopen):
            text, _ti, _to, _cost = agent_router._call_vibe_cli(
                'mistral-medium-latest',
                big_prompt,
                project_path=os.getcwd(),
            )

        self.assertEqual(text, 'OK')
        prompt_arg = seen['cmd'][seen['cmd'].index('--prompt') + 1]
        self.assertLessEqual(len(prompt_arg.encode('utf-8')),
                             agent_router.VIBE_PROMPT_MAX_BYTES + 200)
        self.assertIn('Context truncated', prompt_arg)
        self.assertTrue(prompt_arg.endswith('x' * 100))

    def test_vibe_turn_limit_does_not_return_raw_conversation(self):
        # Regression (exec 20001185, task #10001192): the run hit --max-turns and
        # its last assistant message was a bare tool call. The parser fell back
        # to the raw stdout (the whole conversation, system prompt included),
        # whose "do not emit <bash>…</bash>" rule tripped the simulation
        # detector, so 80 turns of real work were failed as "simulated".
        conversation = [
            {'role': 'user', 'content': 'Do NOT emit `<bash>…</bash>` or `<tool_code>…</tool_code>`.'},
            {'role': 'assistant', 'content': 'Extracted 712 text elements.'},
            {'role': 'assistant', 'content': [{'type': 'tool_call', 'name': 'write_file'}]},
            {'role': 'tool', 'content': '{"bytes_written": 19929}'},
        ]

        class _VibeFakePopen(_FakePopen):
            def __init__(self_inner, cmd, *a, **k):
                super().__init__(returncode=1, stdout=json.dumps(conversation),
                                 stderr='<vibe_stop_event>Turn limit of 80 reached</vibe_stop_event>')

        with patch('shutil.which', return_value='/usr/bin/vibe'), \
             patch.object(agent_router, 'MISTRAL_VIBE_KEY', 'test-key'), \
             patch('subprocess.Popen', side_effect=_VibeFakePopen):
            text, _ti, _to, _cost = agent_router._call_vibe_cli(
                'mistral-medium-latest', 'Translate the deck.',
                project_path=os.getcwd())

        self.assertTrue(text.startswith('Extracted 712 text elements.'))
        self.assertIn(agent_router.VIBE_TURN_LIMIT_MARKER, text)
        self.assertNotIn('<bash>', text)

    def test_vibe_no_assistant_text_returns_empty_not_stdout(self):
        conversation = [
            {'role': 'user', 'content': 'Do NOT emit `<bash>…</bash>`.'},
            {'role': 'assistant', 'content': [{'type': 'tool_call', 'name': 'bash'}]},
        ]

        class _VibeFakePopen(_FakePopen):
            def __init__(self_inner, cmd, *a, **k):
                super().__init__(returncode=0, stdout=json.dumps(conversation), stderr='')

        with patch('shutil.which', return_value='/usr/bin/vibe'), \
             patch.object(agent_router, 'MISTRAL_VIBE_KEY', 'test-key'), \
             patch('subprocess.Popen', side_effect=_VibeFakePopen):
            text, _ti, _to, _cost = agent_router._call_vibe_cli(
                'mistral-medium-latest', 'Translate the deck.',
                project_path=os.getcwd())

        self.assertEqual(text, '')

    def test_chat_transcript_normalizes_empty_permission_denial_envelope(self):
        payload = {
            'type': 'result',
            'subtype': 'success',
            'is_error': False,
            'result': '',
            'permission_denials': [
                {
                    'tool_name': 'AskUserQuestion',
                    'tool_input': {
                        'questions': [
                            {'question': 'Which folder should the test run target?'},
                        ],
                    },
                },
            ],
        }
        transcript = (
            '### [2026-05-28 09:59] 🤖 Assistant (exec #363)\n\n'
            '**Model:** claude-opus-4-7 · **Tokens:** 7↑ 6505↓ · **Cost:** $0.4263\n\n'
            f'{json.dumps(payload)}\n\n---\n'
        )

        normalized = agent_chats._normalize_cli_result_envelopes(transcript)

        self.assertIn('Provider returned no assistant text', normalized)
        self.assertIn('Which folder should the test run target?', normalized)
        self.assertNotIn('"permission_denials"', normalized)

    def test_chat_transcript_normalizes_wrapped_permission_denial_envelope(self):
        payload = {
            'type': 'result',
            'subtype': 'success',
            'is_error': False,
            'result': '',
            'permission_denials': [
                {
                    'tool_name': 'AskUserQuestion',
                    'tool_input': {
                        'questions': [
                            {'question': 'Which folder should the test run target?'},
                        ],
                    },
                },
            ],
        }
        transcript = (
            '### [2026-05-28 09:59] 🤖 Assistant (exec #363)\n\n'
            '**Model:** claude-opus-4-7 · **Tokens:** 7↑ 6505↓ · **Cost:** $0.4263\n\n'
            f'INFO: wrapper line\n{json.dumps(payload)}\nINFO: wrapper line 2\n\n---\n'
        )

        normalized = agent_chats.normalize_cli_result_text(transcript)

        self.assertIn('Provider returned no assistant text', normalized)
        self.assertIn('Which folder should the test run target?', normalized)
        self.assertNotIn('"permission_denials"', normalized)

    # ── Agentic tool-loop truncation regressions (task #12 / a client project) ─────
    # The OpenAI-compatible tool loop used to ship the raw transcript (ending
    # mid-flow at a `→ tool` line) when the model returned an empty final text
    # after real tool work, or when it exhausted max_turns. Both paths must now
    # force a final synthesis so the deliverable reads as complete.

    def _run_loop_with_responses(self, responses, *, max_turns=40, tools=True,
                                 exec_id=None, progress=None):
        """Drive _run_openai_tool_loop with a scripted sequence of API responses.

        Each `responses` entry is a dict shaped like an OpenAI-compatible
        /chat/completions body: {'choices':[{'finish_reason':...,'message':{...}}],
        'usage':{'prompt_tokens':N,'completion_tokens':M}}.

        If `exec_id` is given, `agent_db.update_execution_tokens` is patched and
        every mid-run progress write is appended to `progress` (a list) as
        (tokens_input, tokens_output) tuples.
        """
        import tempfile
        seq = list(responses)

        import requests
        call_index = {'i': 0}

        def fake_requests_post(url, headers=None, data=None, timeout=None):
            resp = responses[min(call_index['i'], len(responses) - 1)]
            call_index['i'] += 1
            class R:
                status_code = 200
                def json(self_inner):
                    return resp
            return R()

        def _fake_update_tokens(exec_id_, tok_in, tok_out, project_path=None):
            if progress is not None:
                progress.append((tok_in, tok_out))

        def _fake_get_execution_pid(exec_id_, project_path=None):
            # The loop's cancel check: report the execution as still running
            # so scripted multi-turn loops are not aborted as cancelled.
            return {'id': exec_id_, 'status': 'running', 'child_pid': None,
                    'task_id': None, 'project_path': project_path}

        # exec_id=None → _execution_cancelled short-circuits without a DB
        # lookup; only patch the lookup when a synthetic exec_id is in play.
        patches = [patch.object(requests, 'post', side_effect=fake_requests_post)]
        if exec_id is not None:
            patches.append(patch('agent_db.get_execution_pid',
                                 side_effect=_fake_get_execution_pid))

        with tempfile.TemporaryDirectory() as d:
            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                # Patch agent_tools.dispatch so tool_calls don't hit the FS.
                import agent_tools
                stack.enter_context(patch.object(agent_tools, 'dispatch',
                                                return_value='{"ok": true}'))
                stack.enter_context(patch('agent_db.update_execution_tokens',
                                          side_effect=_fake_update_tokens))
                return agent_router._run_openai_tool_loop(
                    provider_label='Scaleway',
                    api_url='https://example/v1/chat/completions',
                    api_key='k',
                    wire_model='qwen3.6',
                    model_id='scw-qwen3.6-35b',
                    prompt='do the thing',
                    max_tokens=1024,
                    max_turns=max_turns,
                    project_path=d,
                    exec_id=exec_id,
                )

    def test_tool_loop_forces_final_synthesis_on_empty_final_text(self):
        # Turn 1: model calls a tool (recorded as `→ list_files`). Turn 2: model
        # returns an empty final text. Without the fix the deliverable would be
        # just "→ `list_files`" with no conclusion.
        responses = [
            {
                'choices': [{
                    'finish_reason': 'tool_calls',
                    'message': {
                        'tool_calls': [{
                            'id': 'call_1',
                            'function': {
                                'name': 'list_files',
                                'arguments': '{"directory": ""}',
                            },
                        }],
                    },
                }],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
            },
            {
                # Empty final text — the truncation case.
                'choices': [{'finish_reason': 'stop', 'message': {'content': ''}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 0},
            },
            # Forced-synthesis call (no tools bound).
            {
                'choices': [{'finish_reason': 'stop',
                              'message': {'content': 'Here is the final summary of the work done.'}}],
                'usage': {'prompt_tokens': 12, 'completion_tokens': 20},
            },
        ]
        text, _ti, _to, _cost = self._run_loop_with_responses(responses)
        self.assertIn('Here is the final summary of the work done.', text)
        self.assertIn('→ `list_files`', text)  # transcript still preserved

    def test_tool_loop_forces_final_synthesis_on_max_turns(self):
        # 3 tool_calls turns in a row, max_turns=3 → else-branch forces synthesis.
        tool_turn = {
            'choices': [{
                'finish_reason': 'tool_calls',
                'message': {
                    'tool_calls': [{
                        'id': 'call_x',
                        'function': {'name': 'read_file', 'arguments': '{"path": "a.txt"}'},
                    }],
                },
            }],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 4},
        }
        synthesis = {
            'choices': [{'finish_reason': 'stop',
                         'message': {'content': 'Final answer after exhausting tool turns.'}}],
            'usage': {'prompt_tokens': 15, 'completion_tokens': 25},
        }
        responses = [tool_turn, tool_turn, tool_turn, synthesis]
        text, _ti, _to, _cost = self._run_loop_with_responses(responses, max_turns=3)
        self.assertIn('Final answer after exhausting tool turns.', text)

    def test_tool_loop_no_forced_call_when_immediately_empty(self):
        # First turn returns empty final text with no prior transcript → must NOT
        # force a synthesis call (preserves the "model returned nothing" case).
        responses = [
            {'choices': [{'finish_reason': 'stop', 'message': {'content': ''}}],
             'usage': {'prompt_tokens': 5, 'completion_tokens': 0}},
        ]
        text, _ti, _to, _cost = self._run_loop_with_responses(responses)
        self.assertEqual(text, '')

    def test_tool_loop_emits_cumulative_token_progress(self):
        # Mid-run live tokens: the progress writer must receive *cumulative*
        # totals across a multi-turn tool loop, not per-turn deltas.
        # Turn 1: tool call (10+5). Turn 2: final text (10+0) → cumulative 20+5.
        responses = [
            {
                'choices': [{
                    'finish_reason': 'tool_calls',
                    'message': {
                        'tool_calls': [{
                            'id': 'call_1',
                            'function': {
                                'name': 'list_files',
                                'arguments': '{"directory": ""}',
                            },
                        }],
                    },
                }],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
            },
            {
                'choices': [{'finish_reason': 'stop',
                              'message': {'content': 'done'}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 0},
            },
        ]
        progress = []
        text, ti, to, _cost = self._run_loop_with_responses(
            responses, exec_id=42, progress=progress)
        self.assertIn('done', text)
        self.assertEqual(ti, 20)
        self.assertEqual(to, 5)
        # At least one progress write happened, and the last one carries the
        # final cumulative totals.
        self.assertTrue(progress)
        self.assertEqual(progress[-1], (20, 5))

    def test_tool_loop_stops_when_execution_cancelled(self):
        # Regression: task #10001124 — /cancel flips the execution row to
        # 'failed' and resets the task, but the in-process tool loop kept
        # burning turns for minutes and the late return flipped the user-reset
        # task back to 'done'. The loop must check cancellation at the top of
        # every turn and raise ExecutionCancelledError (which run_task handles
        # as a clean stop, and which is excluded from the HTTP retry lists).
        responses = [
            {
                'choices': [{'finish_reason': 'stop',
                              'message': {'content': 'should never be returned'}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
            },
        ]
        progress = []

        import requests
        import tempfile

        def fake_requests_post(url, headers=None, data=None, timeout=None):
            class R:
                status_code = 200
                def json(self_inner):
                    return responses[0]
            return R()

        def _fake_get_execution_pid(exec_id_, project_path=None):
            # Cancelled state: the row is no longer 'running'.
            return {'id': exec_id_, 'status': 'failed', 'child_pid': None,
                    'task_id': None, 'project_path': project_path}

        with tempfile.TemporaryDirectory() as d:
            with patch.object(requests, 'post', side_effect=fake_requests_post), \
                 patch('agent_db.get_execution_pid',
                       side_effect=_fake_get_execution_pid), \
                 patch('agent_db.update_execution_tokens') as _no_progress:
                with self.assertRaises(agent_router.ExecutionCancelledError):
                    agent_router._run_openai_tool_loop(
                        provider_label='Scaleway',
                        api_url='https://example/v1/chat/completions',
                        api_key='k',
                        wire_model='qwen3.6',
                        model_id='scw-qwen3.6-35b',
                        prompt='do the thing',
                        max_tokens=1024,
                        max_turns=40,
                        project_path=d,
                        exec_id=99,
                    )
                # No API call was ever made — the cancel check fires first.
                _no_progress.assert_not_called()

    def test_tool_loop_skips_progress_without_exec_id(self):
        # No exec_id → no progress writes (CLI/other paths unaffected).
        responses = [
            {
                'choices': [{'finish_reason': 'stop',
                              'message': {'content': 'done'}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
            },
        ]
        progress = []
        _text, _ti, _to, _cost = self._run_loop_with_responses(
            responses, exec_id=None, progress=progress)
        self.assertEqual(progress, [])

    def test_tool_loop_retries_on_empty_200_body(self):
        # Regression for task #10001058: Scaleway's envoy LB sporadically
        # returns HTTP 200 with an empty body, which made `resp.json()` raise
        # `JSONDecodeError: Expecting value: line 1 column 1 (char 0)` and fail
        # the whole execution. The @retry in `_post` must treat that as a
        # transient transport error and re-issue the call.
        import requests
        import tempfile

        good = {
            'choices': [{'finish_reason': 'stop',
                          'message': {'content': 'recovered after empty 200'}}],
            'usage': {'prompt_tokens': 8, 'completion_tokens': 12},
        }
        call_index = {'i': 0}

        class _Empty200Resp:
            status_code = 200
            text = ''
            reason = 'OK'
            def json(self):
                raise json.JSONDecodeError('Expecting value', '', 0)

        def fake_requests_post(url, headers=None, data=None, timeout=None):
            i = call_index['i']
            call_index['i'] += 1
            if i == 0:
                return _Empty200Resp()
            class R:
                status_code = 200
                text = json.dumps(good)
                def json(self_inner):
                    return good
            return R()

        with tempfile.TemporaryDirectory() as d:
            with patch.object(requests, 'post', side_effect=fake_requests_post):
                import agent_tools
                with patch.object(agent_tools, 'dispatch', return_value='{"ok": true}'):
                    text, _ti, _to, _cost = agent_router._run_openai_tool_loop(
                        provider_label='Scaleway',
                        api_url='https://example/v1/chat/completions',
                        api_key='k',
                        wire_model='glm-5.2',
                        model_id='scw-glm-5.2',
                        prompt='do the thing',
                        max_tokens=1024,
                        max_turns=5,
                        project_path=d,
                    )
        self.assertIn('recovered after empty 200', text)
        # The loop must have re-issued the call after the empty 200.
        self.assertGreaterEqual(call_index['i'], 2)

    def test_tool_loop_retries_on_504_stream_timeout(self):
        # Regression for task #10001058 (second failure): Scaleway envoy
        # returned `504 stream timeout` after a reasoning model stalled. The
        # @retry in `_post` must treat 5xx as transient and re-issue the call.
        # 4xx (e.g. 400 payload-validation) must NOT be retried — they are
        # permanent and retrying wastes attempts and hides the real cause.
        import requests
        import tempfile

        good = {
            'choices': [{'finish_reason': 'stop',
                          'message': {'content': 'recovered after 504'}}],
            'usage': {'prompt_tokens': 8, 'completion_tokens': 12},
        }
        call_index = {'i': 0}

        class _Resp:
            def __init__(self, status, text='', payload=None):
                self.status_code = status
                self.text = text
                self.reason = 'OK' if status == 200 else 'Error'
                self._payload = payload
            def json(self):
                if self._payload is None:
                    raise json.JSONDecodeError('Expecting value', '', 0)
                return self._payload

        def fake_requests_post(url, headers=None, data=None, timeout=None):
            i = call_index['i']
            call_index['i'] += 1
            if i == 0:
                return _Resp(504, text='stream timeout')
            return _Resp(200, text=json.dumps(good), payload=good)

        with tempfile.TemporaryDirectory() as d:
            with patch.object(requests, 'post', side_effect=fake_requests_post):
                import agent_tools
                with patch.object(agent_tools, 'dispatch', return_value='{"ok": true}'):
                    text, _ti, _to, _cost = agent_router._run_openai_tool_loop(
                        provider_label='Scaleway',
                        api_url='https://example/v1/chat/completions',
                        api_key='k',
                        wire_model='glm-5.2',
                        model_id='scw-glm-5.2',
                        prompt='do the thing',
                        max_tokens=1024,
                        max_turns=5,
                        project_path=d,
                    )
        self.assertIn('recovered after 504', text)
        self.assertGreaterEqual(call_index['i'], 2)

    def test_tool_loop_does_not_retry_on_400_payload_validation(self):
        # 4xx (other than 429) is permanent — must surface immediately, not
        # waste 3 retry attempts. Regression guard for the max_completion_tokens
        # 400 error (tasks #10001052 / #10001054).
        import requests
        import tempfile

        call_index = {'i': 0}

        class _Resp:
            def __init__(self, status, text=''):
                self.status_code = status
                self.text = text
                self.reason = 'Bad Request'
            def json(self):
                return {'message': text}

        def fake_requests_post(url, headers=None, data=None, timeout=None):
            call_index['i'] += 1
            return _Resp(400, text="max_completion_tokens is limited to 16384")

        with tempfile.TemporaryDirectory() as d:
            with patch.object(requests, 'post', side_effect=fake_requests_post):
                import agent_tools
                with patch.object(agent_tools, 'dispatch', return_value='{"ok": true}'):
                    try:
                        agent_router._run_openai_tool_loop(
                            provider_label='Scaleway',
                            api_url='https://example/v1/chat/completions',
                            api_key='k',
                            wire_model='glm-5.2',
                            model_id='scw-glm-5.2',
                            prompt='do the thing',
                            max_tokens=1024,
                            max_turns=5,
                            project_path=d,
                        )
                        self.fail('expected Exception for 400, none raised')
                    except Exception as e:
                        self.assertIn('400', str(e))
        # Exactly one attempt — no retries.
        self.assertEqual(call_index['i'], 1)


class OverseerReferencedFilesTests(unittest.TestCase):
    """The H2 referenced-files extractor used to misclassify URLs, email
    addresses, and bare @domain tokens as filesystem paths, producing
    false-positive "containment breach" gates on tasks that merely mentioned
    a website or an email address. Regression: task #10000698 (a client project)
    flagged `//www.example-client.com`, `@example-client.com`, `@example-group.cz`; and
    the earlier task #12 false-positive on `user@mail.me`.
    """

    def test_extracts_real_file_paths(self):
        desc = (
            "Read `client_whoswho.md` and \"/path/to/contract.pdf\" then "
            "write FULL_TEXT_OCR.txt and ./data/ingest.py"
        )
        refs = agent_overseer._extract_referenced_files(desc)
        for real in ('client_whoswho.md', '/path/to/contract.pdf',
                    'FULL_TEXT_OCR.txt', './data/ingest.py'):
            self.assertIn(real, refs)

    def test_extracts_single_quoted_file_paths(self):
        # Regression: task #10001145 (a client project) referenced a file via a
        # single-quoted path containing spaces and a Polish non-ASCII
        # character. The extractor had backtick/double-quote patterns but
        # nothing for single quotes, so H2's missing_files reconciliation
        # never saw the reference.
        desc = ("See 'Working Documents/CUPT/II Etap/"
                "05_WKR_Zalożenia_do_oferty_przewozowej-FULL.pdf' for details")
        refs = agent_overseer._extract_referenced_files(desc)
        self.assertIn(
            'Working Documents/CUPT/II Etap/'
            '05_WKR_Zalożenia_do_oferty_przewozowej-FULL.pdf', refs)

    def test_filters_url_scheme_and_fragment(self):
        desc = "Visit https://www.example-client.com and `//www.example-client.com` and www.example.com"
        refs = agent_overseer._extract_referenced_files(desc)
        self.assertNotIn('//www.example-client.com', refs)
        self.assertNotIn('www.example-client.com', refs)
        self.assertNotIn('www.example.com', refs)

    def test_filters_email_addresses_and_domains(self):
        # The user@mail.me regression (task #12) and the @example-client.com /
        # @example-group.cz regressions (task #10000698).
        desc = "Send to user@mail.me and filter `@example-client.com` and `@example-group.cz`"
        refs = agent_overseer._extract_referenced_files(desc)
        self.assertNotIn('user@mail.me', refs)
        self.assertNotIn('@example-client.com', refs)
        self.assertNotIn('@example-group.cz', refs)

    def test_filters_bare_domains(self):
        # A bare `example-client.com` (no path separators, no identifier-ish stem)
        # is a domain, not a file.
        desc = "See example-client.com and example-group.cz for details"
        refs = agent_overseer._extract_referenced_files(desc)
        self.assertNotIn('example-client.com', refs)
        self.assertNotIn('example-group.cz', refs)

    def test_filters_hyphenated_and_multilabel_domains(self):
        # Regression: task #10000699 (a client project) flagged
        # `cupt.gov.pl`, `rynek-kolejowy.pl`, `transinfo.pl` as unread files
        # because their stems contain `-` (passing the filename regex's
        # stem filter) and the _looks_like_url_or_email backstop was dead
        # code. The backstop now recognises bare host.tld tokens ending
        # in a known internet TLD, including multi-label TLDs (gov.pl).
        desc = ("Priority: rynek-kolejowy.pl, transinfo.pl. "
                "Official: cupt.gov.pl. Also see example-client.com.")
        refs = agent_overseer._extract_referenced_files(desc)
        self.assertNotIn('rynek-kolejowy.pl', refs)
        self.assertNotIn('transinfo.pl', refs)
        self.assertNotIn('cupt.gov.pl', refs)
        self.assertNotIn('example-client.com', refs)



class ResolveReferencedFilesLeadTokenTests(unittest.TestCase):
    """prompt_builder._resolve_referenced_files's "lead token" fallback matches
    a truncated/partial filename reference (e.g. "Wyrok..." for
    "Wyrok-13.07.2026.md"). Regression: task #10001145 (a client project) — the
    project's files mostly share a "CUPT_" or "Task_NNNNN_" naming prefix, and
    the task description naturally contained the words "CUPT" (the tender
    authority's name) and "Task" (from its own "Analysis Task:" section
    header). The old fallback treated those shared prefixes as distinctive
    identifiers and swept in 25 unrelated files, forcing batch mode (no
    tools) regardless of which model ran it. A prefix shared by many files in
    the project is not a distinctive identifier and must not match alone.
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.project = self.td.name
        self.addCleanup(self.td.cleanup)
        self.wd = os.path.join(self.project, 'Working Documents')
        os.makedirs(self.wd)

    def _write(self, name, content='content'):
        with open(os.path.join(self.wd, name), 'w') as f:
            f.write(content)

    def test_shared_naming_prefix_does_not_sweep_in_unrelated_files(self):
        self._write('CUPT_Target.md')
        self._write('CUPT_Other1.md')
        self._write('CUPT_Other2.md')
        self._write('CUPT_Other3.md')
        task = {'description': 'Summarize the CUPT tender package split hypotheses.'}
        inline, batch = prompt_builder._resolve_referenced_files(
            task, self.project, inline_budget=100000, single_file_max=50000,
            catchall_disabled=True)
        caught = {f for f, _ in inline} | {f for f, _ in batch}
        self.assertNotIn('CUPT_Other1.md', caught)
        self.assertNotIn('CUPT_Other2.md', caught)
        self.assertNotIn('CUPT_Other3.md', caught)

    def test_distinctive_one_off_prefix_still_matches_truncated_reference(self):
        # Only one file has the "Wyrok" prefix — a genuinely distinctive
        # identifier fragment, per the function's own original example.
        self._write('Wyrok-13.07.2026.md')
        task = {'description': 'Please summarize the Wyrok ruling for the client.'}
        inline, batch = prompt_builder._resolve_referenced_files(
            task, self.project, inline_budget=100000, single_file_max=50000,
            catchall_disabled=True)
        caught = {f for f, _ in inline} | {f for f, _ in batch}
        self.assertIn('Wyrok-13.07.2026.md', caught)


class GuideSyncTests(unittest.TestCase):
    """Tests for agent_guide_sync.regenerate_guide (DB-derived GUIDE.md view)."""

    def setUp(self):
        import tempfile, sqlite3, shutil
        self.tmp = tempfile.mkdtemp(prefix='guide-sync-')
        self.project_path = os.path.join(self.tmp, 'MyProj')
        os.makedirs(self.project_path, exist_ok=True)
        self.db_path = os.path.join(self.project_path, 'project.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'CREATE TABLE tasks ('
            '  id INTEGER PRIMARY KEY,'
            '  project_id INTEGER,'
            '  title TEXT,'
            '  status TEXT,'
            '  phase_name TEXT,'
            '  work_session_slot INTEGER,'
            '  slot_position INTEGER,'
            '  archived INTEGER DEFAULT 0,'
            '  created_at TEXT,'
            '  source TEXT DEFAULT "manual"'
            ')'
        )
        conn.commit()
        conn.close()
        # Make sure the module under test is fresh for each test
        import importlib, agent_guide_sync
        importlib.reload(agent_guide_sync)
        self.sync = agent_guide_sync

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _insert(self, pid, title, status, phase, slot=None, pos=0, archived=0):
        import sqlite3
        c = sqlite3.connect(self.db_path)
        c.execute(
            'INSERT INTO tasks (project_id, title, status, phase_name, '
            '                     work_session_slot, slot_position, archived, created_at) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (pid, title, status, phase, slot, pos, archived, '2026-01-01T00:00:00'),
        )
        c.commit()
        c.close()

    def test_regenerate_writes_phase_headers_and_markers(self):
        self._insert(1, 'Design schema',     'pending', 'Phase 1')
        self._insert(1, 'Build parser',      'done',    'Phase 2')
        self._insert(1, 'Wire API',          'failed',  'Phase 2')
        self._insert(1, 'Write tests',       'pending', 'Notebook')

        self.assertTrue(self.sync.regenerate_guide(self.project_path, 1))

        body = open(os.path.join(self.project_path, 'GUIDE.md'), encoding='utf-8').read()
        self.assertIn('# MyProj', body)
        self.assertIn('## Phase 1 \u2014 Phase 1', body)
        self.assertIn('## Phase 2 \u2014 Phase 2', body)
        self.assertIn('## Phase 3 \u2014 Notebook', body)
        self.assertIn('- [ ] Design schema', body)
        self.assertIn('- [x] Build parser', body)
        self.assertIn('- [ ] Write tests', body)

    def test_status_markers_done_failed_running_archived(self):
        self._insert(1, 'Done one',   'done',    'Phase 1', archived=0)
        self._insert(1, 'Failed one', 'failed',  'Phase 1', archived=0)
        self._insert(1, 'Running one','running', 'Phase 1', archived=0)
        self._insert(1, 'Archived',   'pending', 'Phase 1', archived=1)
        self._insert(1, 'Skipped',    'skip',    'Phase 1', archived=0)

        self.sync.regenerate_guide(self.project_path, 1)
        body = open(os.path.join(self.project_path, 'GUIDE.md'), encoding='utf-8').read()
        self.assertIn('- [x] Done one',   body)
        self.assertIn('- [ ] \U0001f4e6 Archived', body)
        self.assertIn('~~Skipped~~', body)
        self.assertIn('Failed one', body)

    def test_no_tasks_leaves_existing_file_untouched(self):
        existing = "# Pre-existing prose\n\nThis was here before.\n"
        guide = os.path.join(self.project_path, 'GUIDE.md')
        with open(guide, 'w', encoding='utf-8') as f:
            f.write(existing)

        self.assertFalse(self.sync.regenerate_guide(self.project_path, 1))
        self.assertEqual(open(guide, encoding='utf-8').read(), existing)

    def test_missing_project_db_returns_false(self):
        empty_proj = os.path.join(self.tmp, 'empty')
        os.makedirs(empty_proj)
        self.assertFalse(self.sync.regenerate_guide(empty_proj, 99))

    def test_idempotent_no_rewrite_when_content_unchanged(self):
        self._insert(1, 'A', 'pending', 'Phase 1')
        self.sync.regenerate_guide(self.project_path, 1)
        mtime1 = os.path.getmtime(os.path.join(self.project_path, 'GUIDE.md'))
        # Second call with no DB changes should be a no-op
        self.sync.regenerate_guide(self.project_path, 1)
        mtime2 = os.path.getmtime(os.path.join(self.project_path, 'GUIDE.md'))
        self.assertEqual(mtime1, mtime2)

    def test_suppress_context_blocks_regeneration(self):
        self._insert(1, 'A', 'pending', 'Phase 1')
        guide = os.path.join(self.project_path, 'GUIDE.md')
        sentinel = "# Sentinel — do not overwrite\n"
        with open(guide, 'w', encoding='utf-8') as f:
            f.write(sentinel)
        with self.sync.suppress():
            self.assertFalse(self.sync.regenerate_guide(self.project_path, 1))
        # Outside the block, regen runs and overwrites
        self.assertTrue(self.sync.regenerate_guide(self.project_path, 1))
        self.assertNotEqual(open(guide, encoding='utf-8').read(), sentinel)

    def test_phase_ordering_notebook_last(self):
        self._insert(1, 'Empty-phase task', 'pending', '')
        self._insert(1, 'Zeta',             'pending', 'Zeta')
        self._insert(1, 'Alpha',            'pending', 'Alpha')
        self.sync.regenerate_guide(self.project_path, 1)
        body = open(os.path.join(self.project_path, 'GUIDE.md'), encoding='utf-8').read()
        alpha_idx = body.find('## Phase 1')
        zeta_idx  = body.find('## Phase 2')
        nb_idx    = body.find('Notebook')
        self.assertLess(alpha_idx, zeta_idx)
        self.assertLess(zeta_idx, nb_idx)

    def test_after_task_change_resolves_project_id_from_task(self):
        import agent_db as db
        # Register a row in central task_registry to test the task_id → project_id path
        import sqlite3
        central_db = os.path.join(self.tmp, 'central.db')
        conn = sqlite3.connect(central_db)
        conn.execute('CREATE TABLE task_registry (id INTEGER PRIMARY KEY, project_id INTEGER, project_path TEXT)')
        conn.commit()
        conn.close()
        # Override get_db resolution by patching
        with patch.object(db, 'get_db') as mock_get_db, \
             patch.object(db, '_resolve_project_id', return_value=1):
            self._insert(1, 'A', 'pending', 'Phase 1')
            mock_get_db.return_value = sqlite3.connect(central_db)
            # Insert a fake registry row
            sqlite3.connect(central_db).execute(
                'INSERT INTO task_registry (id, project_id, project_path) VALUES (?,?,?)',
                (42, 1, self.project_path),
            )
            sqlite3.connect(central_db).commit()
            self.assertTrue(self.sync.after_task_change(self.project_path, task_id=42))

    def test_suppress_is_thread_local(self):
        """One thread's suppress() must not affect another thread's regen
        (Flask threaded=True can run bulk imports concurrently)."""
        import threading, time
        self._insert(1, 'A', 'pending', 'Phase 1')
        guide = os.path.join(self.project_path, 'GUIDE.md')
        # Pre-seed so we can detect when regen writes
        sentinel = "# sentinel\n"
        with open(guide, 'w', encoding='utf-8') as f:
            f.write(sentinel)
        entered = threading.Event()
        release = threading.Event()

        def hold_suppress():
            with self.sync.suppress():
                entered.set()
                release.wait(5)
            # Outside suppress: regen must succeed even though the main
            # thread never entered its own suppress block.
            self.sync.regenerate_guide(self.project_path, 1)

        t = threading.Thread(target=hold_suppress)
        t.start()
        entered.wait(5)
        # While the other thread holds suppress, the main thread can still regen
        self.sync.regenerate_guide(self.project_path, 1)
        body = open(guide, encoding='utf-8').read()
        self.assertNotEqual(body, sentinel)
        release.set()
        t.join(5)
        # And after the other thread exits, a second regen is also fine
        self.sync.regenerate_guide(self.project_path, 1)

    def test_regenerate_swallows_db_read_errors(self):
        """A failing _read_tasks must not propagate; the hook call site must
        see a clean False return rather than an exception (so a stale or
        corrupt project.db can't make create_task/update_task/delete_task
        appear to fail after the DB mutation has already been committed)."""
        # Replace _read_tasks with one that raises
        original = self.sync._read_tasks
        def boom(*a, **kw):
            raise sqlite3.OperationalError('simulated db failure')
        self.sync._read_tasks = boom
        try:
            self.assertFalse(self.sync.regenerate_guide(self.project_path, 99))
        finally:
            self.sync._read_tasks = original

    def test_after_task_change_swallows_db_read_errors(self):
        """after_task_change must not raise even if the underlying regen fails."""
        original = self.sync._read_tasks
        def boom(*a, **kw):
            raise RuntimeError('boom')
        self.sync._read_tasks = boom
        try:
            self.assertFalse(self.sync.after_task_change(self.project_path, project_id=1))
        finally:
            self.sync._read_tasks = original


class ProjectDbSchemaInitTests(unittest.TestCase):
    """Regression test for the _init_project_db_schema bug where CREATE INDEX
    on `executions.last_heartbeat_at` ran BEFORE the ALTER TABLE that added
    the column, breaking any fresh project.db. The fix reorders the script so
    column migrations complete before indexes are created."""

    def setUp(self):
        import tempfile, sqlite3
        self.tmp = tempfile.mkdtemp(prefix='schema-init-')
        self.project_path = os.path.join(self.tmp, 'fresh')
        os.makedirs(self.project_path)
        self.db_path = os.path.join(self.project_path, 'project.db')
        # Build a minimal-but-realistic project.db that has the columns the
        # CREATE INDEX statements reference, except `last_heartbeat_at` which
        # is added by the migration block.
        conn = sqlite3.connect(self.db_path)
        conn.executescript('''
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT,
                phase_name TEXT DEFAULT '', status TEXT DEFAULT 'pending',
                work_session_slot INTEGER, slot_position INTEGER DEFAULT 0,
                archived INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE executions (
                id INTEGER PRIMARY KEY, task_id INTEGER, session_id INTEGER,
                chat_id INTEGER, model TEXT, status TEXT,
                started_at TEXT DEFAULT CURRENT_TIMESTAMP, finished_at TEXT
            );
            CREATE TABLE chats (
                id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT,
                phase_name TEXT DEFAULT '', task_id INTEGER, status TEXT,
                file_path TEXT DEFAULT '', model TEXT DEFAULT '',
                attachments TEXT DEFAULT '[]', scaffold_draft TEXT DEFAULT '',
                auto_inject_defs INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE project_skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL, category TEXT NOT NULL,
                name TEXT NOT NULL, auto_detected INTEGER NOT NULL DEFAULT 1,
                detected_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, category, name)
            );
            CREATE TABLE project_permissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL, group_key TEXT NOT NULL,
                rule TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                auto_added INTEGER NOT NULL DEFAULT 0,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, rule)
            );
        ''')
        conn.commit()
        conn.close()
        import agent_db
        self.agent_db = agent_db

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_init_adds_last_heartbeat_at_column(self):
        conn = self.agent_db.get_project_db(self.project_path)
        cols = {r['name'] for r in conn.execute('PRAGMA table_info(executions)').fetchall()}
        conn.close()
        self.assertIn('last_heartbeat_at', cols)

    def test_init_creates_heartbeat_index(self):
        conn = self.agent_db.get_project_db(self.project_path)
        idxs = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
        conn.close()
        self.assertIn('idx_execs_heartbeat', idxs)

    def test_init_idempotent_on_second_open(self):
        # First open applies migrations + indexes
        c1 = self.agent_db.get_project_db(self.project_path); c1.close()
        # Second open must not fail (no ALTER on existing columns, no CREATE on existing indexes)
        c2 = self.agent_db.get_project_db(self.project_path)
        cols = {r['name'] for r in c2.execute('PRAGMA table_info(executions)').fetchall()}
        c2.close()
        self.assertIn('last_heartbeat_at', cols)


class ZombieGuardTests(unittest.TestCase):
    """Regression tests for task #10001124: a cancelled execution whose
    provider call returns late must not flip the user-reset task back to
    'done'. The zombie guard in run_task's success path re-checks the
    execution row before finish_execution/update_task."""

    def test_zombie_guard_returns_cancelled_when_exec_no_longer_running(self):
        # _execution_cancelled is the single source of truth for both the
        # router tool loop and the executor zombie guard; it must treat
        # a missing row OR a non-running status as cancelled.
        from agent_router import _execution_cancelled
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            # Missing registry row → cancelled (defensive; real rows always
            # exist, but the /cancel race makes the lookup best-effort).
            with patch('agent_db.get_execution_pid', return_value=None):
                # exec_id present, row missing — but per _execution_cancelled
                # semantics a missing row counts as cancelled only when the
                # lookup succeeded; get_execution_pid returning None means
                # 'not found' → info is None → cancelled.
                self.assertTrue(_execution_cancelled(1, d))

            with patch('agent_db.get_execution_pid',
                       return_value={'id': 1, 'status': 'failed'}):
                self.assertTrue(_execution_cancelled(1, d))

            with patch('agent_db.get_execution_pid',
                       return_value={'id': 1, 'status': 'running'}):
                self.assertFalse(_execution_cancelled(1, d))

            with patch('agent_db.get_execution_pid',
                       side_effect=RuntimeError('db locked')):
                # Lookup errors must NOT be treated as cancellation — a DB
                # hiccup must not abort a healthy run.
                self.assertFalse(_execution_cancelled(1, d))

    def test_zombie_guard_none_exec_id_is_never_cancelled(self):
        # No exec_id (e.g. ad-hoc route() calls outside run_task) → the
        # guard must never fire.
        from agent_router import _execution_cancelled
        self.assertFalse(_execution_cancelled(None))


class ScrubbedEnvTests(unittest.TestCase):
    """P1-5: CLI subprocesses must not inherit server secrets."""

    def test_server_secrets_are_stripped(self):
        import agent_config
        with patch.dict(os.environ, {
            'AINGEL_SESSION_SECRET': 'super-secret',
            'AINGEL_DAV_TOKEN': 'dav-secret',
            'SCW_SECRET_KEY': 'scw-secret',
            'OPENAI_API_KEY': 'sk-openai',
            'PATH': '/usr/bin',
            'HOME': '/home/test',
        }, clear=False):
            env = agent_config.cli_subprocess_env()
        self.assertNotIn('AINGEL_SESSION_SECRET', env)
        self.assertNotIn('AINGEL_DAV_TOKEN', env)
        self.assertNotIn('SCW_SECRET_KEY', env)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertEqual(env.get('PATH'), '/usr/bin')

    def test_explicit_provider_key_is_forwarded(self):
        import agent_config
        env = agent_config.cli_subprocess_env({'MISTRAL_API_KEY': 'vibe-key'})
        self.assertEqual(env.get('MISTRAL_API_KEY'), 'vibe-key')


class WebFetchSsrfTests(unittest.TestCase):
    """P1-5: web_fetch must refuse loopback/private targets."""

    def test_loopback_and_private_hosts_are_blocked(self):
        import agent_tools
        for url in ('http://127.0.0.1:8001/api/me',
                    'http://localhost/',
                    'http://10.0.0.5/',
                    'http://172.16.0.5:8002/',
                    'http://169.254.169.254/latest/meta-data/'):
            with self.subTest(url=url):
                self.assertTrue(agent_tools._url_host_is_blocked(url))

    def test_public_host_is_allowed(self):
        import agent_tools
        self.assertFalse(agent_tools._url_host_is_blocked('https://example.com/'))

    def test_web_fetch_refuses_loopback(self):
        import agent_tools
        result = agent_tools.web_fetch('http://127.0.0.1:8001/')
        self.assertIn('error', result)


class CentralDbMigrationGuardTests(unittest.TestCase):
    """P1-7: the v1→v2 projects rebuild must not drop post-v2 columns."""

    def test_v1_db_with_extra_columns_is_not_rebuilt(self):
        import tempfile
        import sqlite3
        import agent_db
        tmp = tempfile.mkdtemp(prefix='central-mig-')
        db_path = os.path.join(tmp, 'aingel.db')
        conn = sqlite3.connect(db_path)
        conn.executescript('''
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, path TEXT NOT NULL,
                owner_id INTEGER
            );
            PRAGMA user_version = 1;
        ''')
        conn.execute("INSERT INTO projects (name, slug, path, owner_id) "
                     "VALUES ('P','p','/tmp/p', 7)")
        conn.commit()
        conn.close()

        with patch.object(agent_db, 'DB_PATH', db_path):
            agent_db.init_db()
            c = agent_db.get_db()
            try:
                cols = {r['name'] for r in c.execute('PRAGMA table_info(projects)').fetchall()}
                ver = c.execute('PRAGMA user_version').fetchone()[0]
                row = c.execute("SELECT owner_id FROM projects WHERE slug='p'").fetchone()
            finally:
                c.close()
        self.assertIn('owner_id', cols, 'post-v2 column was dropped by the rebuild')
        self.assertGreaterEqual(ver, 2)
        self.assertEqual(row['owner_id'], 7)


class OverseerOutputTargetTests(unittest.TestCase):
    """H2 must not hold on a file the task is told to create (tasks 10001139,
    10001148); H3 must not downgrade a run because a read cannot be verified."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.path = self.td.name
        self.project = {'name': 'P', 'path': self.path, 'aingel_model': 'm'}
        self.caps = {'file_access': 'tools', 'tools': True}
        self.prompts = []

    def tearDown(self):
        self.td.cleanup()

    def _h2(self, desc, reply):
        task = {'id': 1, 'title': 'T', 'description': desc}

        def fake_ask(model, prompt, *a, **kw):
            self.prompts.append(prompt)
            return json.dumps(reply), None

        with patch.object(agent_overseer, '_ask', fake_ask):
            return agent_overseer.pre_run_check(
                task, self.project, [], [], self.caps, [])

    def test_target_detection(self):
        desc = ("Read `In_1.pdf` and save as `Out_1.md` in the root.\n"
                "Create a summary of `Z_2.pdf`.\n"
                "Write a small file called 'test2.txt'")
        refs = agent_overseer._extract_referenced_files(desc)
        got = agent_overseer._output_target_refs(desc, refs)
        self.assertEqual(got, {'Out_1.md', 'test2.txt'})

    def test_output_file_hold_is_overridden(self):
        r = self._h2("Save as `Out_1.md` in the project root.",
                     {'gate': 'hold', 'reason': 'Referenced file Out_1.md does not exist'})
        self.assertEqual(r['gate'], 'run')
        self.assertEqual(r['output_targets'], ['Out_1.md'])
        self.assertIn('CREATE', self.prompts[0])

    def test_missing_input_still_holds(self):
        r = self._h2("Read `Missing_1.pdf` and summarise it.",
                     {'gate': 'hold', 'reason': 'Missing_1.pdf does not exist'})
        self.assertEqual(r['gate'], 'hold')
        self.assertEqual(r['output_targets'], [])

    def test_hold_for_other_reason_is_kept(self):
        r = self._h2("Read `Missing_1.pdf`, then save as `Out_1.md`.",
                     {'gate': 'hold', 'reason': 'Out_1.md and Missing_1.pdf absent'})
        self.assertEqual(r['gate'], 'hold')

    def test_output_outside_project_is_still_skipped(self):
        r = self._h2("Save as `/etc/evil_1.md`.", {'gate': 'run', 'reason': ''})
        self.assertEqual(r['gate'], 'skip')

    def test_unverified_read_does_not_downgrade_h3(self):
        prompts = []

        def fake_ask(model, prompt, *a, **kw):
            prompts.append(prompt)
            return json.dumps({'severity': 'ok', 'findings': [],
                               'recommendation': 'approve',
                               'gate_for_next': 'proceed', 'brief_2s': 'done'}), None

        with patch.object(agent_overseer, '_ask', fake_ask):
            r = agent_overseer.post_run_analysis(
                {'id': 1, 'title': 'T', 'description': 'See `READMEFIRST.md`'},
                {'id': 1, 'status': 'done', 'tokens_input': 10, 'tokens_output': 10},
                self.project, 'Report written.',
                referenced_files=['READMEFIRST.md'], caught_files=[],
                self_serve_files=[{'name': 'READMEFIRST.md', 'exists': True}],
                route_agentic=True)
        self.assertEqual(r['unread_files'], ['READMEFIRST.md'])
        self.assertEqual(r['severity'], 'ok')
        self.assertEqual(r['gate_for_next'], 'proceed')
        self.assertIn('UNVERIFIED', prompts[0])
        self.assertNotIn('UNREAD', prompts[0])


class SkipTaskSlotRevivalTests(unittest.TestCase):
    """Assigning a provider slot to a 'skip' task revives it (status ->
    'confirmed'); unassigning leaves it 'skip'.

    These exercise the real PATCH /api/tasks/<id> slot-assignment branch in
    agent_api (the same level the authz tests use) against a temp central DB and
    project DB, with auth off so the endpoint runs its normal logic.
    Regression: 'skip' was in the backend `protected` set, so a slot assignment
    never re-confirmed the task and it could never run.
    """

    def setUp(self):
        import agent_db
        self.agent_db = agent_db
        self._saved_auth = os.environ.get('AINGEL_AUTH')
        os.environ['AINGEL_AUTH'] = 'off'
        self.td = tempfile.TemporaryDirectory()
        self.root = self.td.name
        self.central = os.path.join(self.root, 'aingel.db')
        self.path = os.path.join(self.root, 'proj')
        os.makedirs(self.path, exist_ok=True)

        self._db_path_patch = patch.object(agent_db, 'DB_PATH', self.central)
        self._db_path_patch.start()
        agent_db.init_db()
        self.proj = agent_db.upsert_project('P', 'p', self.path)

        import agent_api
        self.agent_api = agent_api
        agent_api.app.config['TESTING'] = True
        self.client = agent_api.app.test_client()

    def tearDown(self):
        self._db_path_patch.stop()
        self.td.cleanup()
        if self._saved_auth is None:
            os.environ.pop('AINGEL_AUTH', None)
        else:
            os.environ['AINGEL_AUTH'] = self._saved_auth

    def _skip_task(self, title):
        tid = self.agent_db.create_task(
            self.proj['id'], title, description='do the thing',
            model='claude-sonnet-4-6', project_path=self.path)
        self.agent_db.update_task(tid, project_path=self.path, status='skip')
        return tid

    def test_assigning_slot_to_skip_task_revives_it(self):
        tid = self._skip_task('Skipped A')
        resp = self.client.patch(
            f'/api/tasks/{tid}', json={'work_session_slot': 1})
        self.assertEqual(resp.status_code, 200)
        task = self.agent_db.get_task(tid, self.path)
        self.assertEqual(task['status'], 'confirmed')
        # (slot may be auto-promoted on overflow; the revival is the invariant)

    def test_clearing_slot_on_skip_task_leaves_it_skip(self):
        tid = self._skip_task('Skipped B')
        resp = self.client.patch(
            f'/api/tasks/{tid}', json={'work_session_slot': None})
        self.assertEqual(resp.status_code, 200)
        task = self.agent_db.get_task(tid, self.path)
        self.assertEqual(task['status'], 'skip')
        self.assertIsNone(task['work_session_slot'])


if __name__ == '__main__':
    unittest.main()
