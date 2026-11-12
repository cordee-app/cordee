"""Tests for the improve-prompt verbatim payload helpers in agent_api.

Covers _extract_payload_blocks / _reinject_payloads: payload blocks embedded in
a task description (''' … ''' or ``` … ```) are replaced by [[PAYLOAD_n]]
tokens before the model call and re-injected byte-exactly afterwards.

Run:  python3 -m unittest test_improve_payload -v
"""
import os
import unittest
from unittest.mock import patch

import agent_router
from agent_api import _extract_payload_blocks, _reinject_payloads


class ExtractTests(unittest.TestCase):

    def test_single_triple_quote_block(self):
        desc = "Write a letter to the bank.\n\nBody:\n'''Dear Sir,\nI object.\n'''\n\nSend it."
        text, payloads = _extract_payload_blocks(desc)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0], "'''Dear Sir,\nI object.\n'''")
        self.assertIn('[[PAYLOAD_1]]', text)
        self.assertNotIn('Dear Sir', text)
        self.assertTrue(text.startswith("Write a letter to the bank.\n\nBody:\n"))
        self.assertTrue(text.endswith("\n\nSend it."))

    def test_single_fenced_block(self):
        desc = "Summarise this:\n```\nline one\nline two\n```\nThen stop."
        text, payloads = _extract_payload_blocks(desc)
        self.assertEqual(payloads, ["```\nline one\nline two\n```"])
        self.assertIn('[[PAYLOAD_1]]', text)
        self.assertNotIn('line one', text)

    def test_interleaved_delimiters(self):
        desc = "'''A ```code``` B'''\n```C '''quoted''' D```"
        text, payloads = _extract_payload_blocks(desc)
        self.assertEqual(payloads, ["'''A ```code``` B'''", "```C '''quoted''' D```"])
        self.assertEqual(text, '[[PAYLOAD_1]]\n[[PAYLOAD_2]]')

    def test_unclosed_triple_quote_untouched(self):
        desc = "Intro '''never closed\nstill going"
        text, payloads = _extract_payload_blocks(desc)
        self.assertEqual(text, desc)
        self.assertEqual(payloads, [])

    def test_empty_block_skipped(self):
        desc = "A '''   ''' B '''real payload''' C"
        text, payloads = _extract_payload_blocks(desc)
        self.assertEqual(payloads, ["'''real payload'''"])
        self.assertEqual(text, "A '''   ''' B [[PAYLOAD_1]] C")

    def test_crlf_payload_round_trips(self):
        payload = "'''Dear Sir,\r\n\r\nI object.\r\n'''"
        desc = "Send this:\r\n" + payload + "\r\nDone."
        text, payloads = _extract_payload_blocks(desc)
        self.assertEqual(payloads, [payload])
        self.assertIn('\r\n', payloads[0])
        model_out = "Send the letter:\n[[PAYLOAD_1]]\nThen confirm."
        final, missing = _reinject_payloads(model_out, payloads)
        self.assertIn(payload, final)
        self.assertEqual(missing, [])

    def test_large_payload_round_trips(self):
        body = "Lorem ipsum dolor sit amet.\n" * 480  # ~12k chars
        payload = "'''\n" + body + "'''"
        desc = "Use this text:\n" + payload
        text, payloads = _extract_payload_blocks(desc)
        self.assertEqual(len(payloads[0]), len(payload))
        self.assertGreater(len(payload), 12000)
        final, missing = _reinject_payloads("[[PAYLOAD_1]]", payloads)
        self.assertEqual(final, payload)
        self.assertEqual(missing, [])

    def test_existing_token_disables_extraction(self):
        desc = "Keep [[PAYLOAD_1]] here '''and this block too'''"
        text, payloads = _extract_payload_blocks(desc)
        self.assertEqual(text, desc)
        self.assertEqual(payloads, [])

    def test_partial_token_disables_extraction(self):
        # Any token-like text (even without digits/brackets) bails out, so a
        # payload that discusses the convention is never mangled later.
        desc = "Mention [[PAYLOAD_ here '''block'''"
        text, payloads = _extract_payload_blocks(desc)
        self.assertEqual(text, desc)
        self.assertEqual(payloads, [])

    def test_stray_marker_in_prose_pairs_real_block(self):
        # RP-136: the description mentions "the ''' below" in prose, so there
        # are three markers. Left-to-right pairing used to grab the 50-char
        # instruction phrase and leave the ~800-char letter body unprotected.
        desc = ("Draft the reply using the following message in the between the ''' below, "
                "do not modify the text, only adapt:\n'''\nDear Sir,\nBody text.\n'''\n"
                "Use the original references.")
        text, payloads = _extract_payload_blocks(desc)
        self.assertEqual(payloads, ["'''\nDear Sir,\nBody text.\n'''"])
        self.assertIn("do not modify the text, only adapt", text)   # instruction preserved
        self.assertIn("[[PAYLOAD_1]]", text)
        self.assertNotIn("Dear Sir", text)
        self.assertIn("the ''' below", text)                        # stray marker restored in prose

    def test_inline_ambiguous_odd_count_is_conservative(self):
        desc = "'''A''' then '''B"
        text, payloads = _extract_payload_blocks(desc)
        self.assertEqual(payloads, ["'''A'''"])
        self.assertIn("then '''B", text)

    def test_block_style_two_blocks_one_unclosed(self):
        desc = "'''\nA\n''' then '''\nB"
        text, payloads = _extract_payload_blocks(desc)
        self.assertEqual(payloads, ["'''\nA\n'''"])

    def test_even_counts_never_masked(self):
        desc = "First:\n'''\nA\n'''\nSecond:\n'''\nB\n'''\nEnd."
        text, payloads = _extract_payload_blocks(desc)
        self.assertEqual(payloads, ["'''\nA\n'''", "'''\nB\n'''"])
        self.assertEqual(text, "First:\n[[PAYLOAD_1]]\nSecond:\n[[PAYLOAD_2]]\nEnd.")


class ReinjectTests(unittest.TestCase):

    def test_spaced_token_tolerated(self):
        payloads = ["'''hello'''"]
        final, missing = _reinject_payloads("A [[ PAYLOAD_1 ]] B", payloads)
        self.assertEqual(final, "A '''hello''' B")
        self.assertEqual(missing, [])

    def test_duplicate_token_substituted_once(self):
        payloads = ["'''hello'''"]
        final, missing = _reinject_payloads("A [[PAYLOAD_1]] B [[PAYLOAD_1]] C", payloads)
        self.assertEqual(final, "A '''hello''' B  C")
        self.assertEqual(final.count("'''hello'''"), 1)
        self.assertNotIn('PAYLOAD_1', final)
        self.assertEqual(missing, [])

    def test_out_of_range_and_partial_fragments_stripped(self):
        payloads = ["'''hello'''"]
        final, missing = _reinject_payloads(
            "A [[PAYLOAD_1]] B [[PAYLOAD_9]] C [[PAYLOAD_", payloads)
        self.assertNotIn('PAYLOAD_9', final)
        self.assertNotIn('[[PAYLOAD_', final)
        self.assertIn("'''hello'''", final)
        self.assertEqual(missing, [])

    def test_missing_token_appends_payload(self):
        payloads = ["'''hello'''"]
        final, missing = _reinject_payloads("Just do the task.", payloads)
        self.assertEqual(missing, [1])
        self.assertIn("[Verbatim payload 1 restored automatically", final)
        self.assertTrue(final.endswith("'''hello'''"))
        self.assertTrue(final.startswith("Just do the task.\n\n---\n"))

    def test_payload_already_present_not_appended(self):
        payloads = ["'''hello'''"]
        final, missing = _reinject_payloads("Do it.\n\n'''hello'''\n", payloads)
        self.assertEqual(missing, [])
        self.assertNotIn('restored automatically', final)
        self.assertEqual(final.count("'''hello'''"), 1)

    def test_regex_metachars_round_trip(self):
        payload = "'''C:\\new\\temp [[\\d]] ^$.*+?(){}|'''"
        desc = "Use:\n" + payload
        text, payloads = _extract_payload_blocks(desc)
        self.assertEqual(payloads, [payload])
        final, missing = _reinject_payloads("[[PAYLOAD_1]]", payloads)
        self.assertEqual(final, payload)
        self.assertEqual(missing, [])

    def test_no_payloads_is_noop(self):
        final, missing = _reinject_payloads("unchanged text", [])
        self.assertEqual(final, "unchanged text")
        self.assertEqual(missing, [])

    def test_letter_n_token_mention_preserved(self):
        # The model may legitimately mention the token convention with a
        # literal "n"; fragment cleanup must not eat it.
        payloads = ["'''hello'''"]
        final, _missing = _reinject_payloads(
            "A [[PAYLOAD_1]] B; [[PAYLOAD_n]] is a placeholder.", payloads)
        self.assertIn("[[PAYLOAD_n]]", final)
        self.assertIn("'''hello'''", final)


class ScaffoldingPathTests(unittest.TestCase):
    """Endpoint-level test: the new-project (scaffolding) path also strips and
    re-injects verbatim payload blocks, exactly like the existing-task path."""

    def setUp(self):
        self._saved_auth = os.environ.get('AINGEL_AUTH')
        os.environ['AINGEL_AUTH'] = 'off'
        import agent_api
        self.agent_api = agent_api
        self.agent_api.app.config['TESTING'] = True
        self.client = self.agent_api.app.test_client()

    def tearDown(self):
        if self._saved_auth is None:
            os.environ.pop('AINGEL_AUTH', None)
        else:
            os.environ['AINGEL_AUTH'] = self._saved_auth

    def test_scaffolding_path_extracts_and_reinjects_payload(self):
        captured = {}

        def fake_route(model, prompt, **_):
            captured['prompt'] = prompt
            return ("Improved pitch [[PAYLOAD_1]] end", 1, 1, 0.0)

        description = "A pitch.\n'''\nVerbatim block text.\n'''\nDone."
        with patch.object(agent_router, 'route', fake_route):
            resp = self.client.post('/api/improve-prompt', json={
                'title': 'Cordee',
                'description': description,
            })

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        prompt = captured['prompt']

        # The current-pitch section carries the token, never the raw block.
        pitch_section = prompt.split('Current pitch:\n', 1)[1].split(
            '\nTech stack hints:', 1)[0]
        self.assertIn('[[PAYLOAD_1]]', pitch_section)
        self.assertNotIn('Verbatim block text', pitch_section)

        # The verbatim text is only present in the context-only annex.
        self.assertIn('Verbatim payload blocks', prompt)
        self.assertIn('Verbatim block text', prompt)

        # Payload restored, token gone.
        self.assertEqual(body['improved'],
                         "Improved pitch '''\nVerbatim block text.\n''' end")
        self.assertNotIn('PAYLOAD_1', body['improved'])


if __name__ == '__main__':
    unittest.main()