"""Batch-mode guard for tasks that must EDIT a binary document (RP-099).

RP-099 (task 10001216) had to edit an existing .pptx while also referencing a
7,489-line .txt. The oversized .txt forced ``mode='batch'`` — the toolless
map/reduce text route — so editing the .pptx was impossible by construction
and no deliverable was produced. ``prompt_builder.build`` must now keep such
tasks inline (agentic tools) and describe oversized files as paths to read
instead of as chunk payloads.

Run: ./venv/bin/python3 -m unittest test_prompt_builder_batch.py -v
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import prompt_builder  # noqa: E402
from prompt_builder import _binary_document_edit_task, build  # noqa: E402

MODEL = 'mistral-large-4'

# Small context window so a ~60k-char text file is "oversized":
# inline_budget = 10,000 tokens, single_file_max = 8,000 tokens.
SMALL_CAPS = {'context_window': 20000, 'max_tokens': 4096, 'vision': False}

RP099_DESC = (
    "Integrate the Polish plan into the existing document deck.pptx.\n"
    "Open `@deck.pptx` and work with the plan in Working Documents/plan.txt.\n"
    "Starting from slide 5, insert a new column in the table.\n"
    "Save the modified PowerPoint as:\n     `deck_with_plan.pptx`"
)

READ_ONLY_DESC = "Read the plan and summarise it in a markdown file `summary.md`."


class BuildBatchGuardTests(unittest.TestCase):
    """build() must not route binary-edit tasks through text-only batch mode."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.proj = self.td.name
        wd = os.path.join(self.proj, 'Working Documents')
        os.makedirs(wd)
        # Fake bytes: _extract_pptx_text() fails → noted as an inline binary.
        with open(os.path.join(wd, 'deck.pptx'), 'wb') as f:
            f.write(b'\x00fake-pptx-bytes')
        # ~60k chars → 30k approx tokens → oversized for the 8k single-file cap.
        with open(os.path.join(wd, 'plan.txt'), 'w', encoding='utf-8') as f:
            f.write('Polish plan line.\n' * 3158)

    def tearDown(self):
        self.td.cleanup()

    def _task(self, desc):
        return {
            'id': 1,
            'title': 'x',
            'model': MODEL,
            'context_refs': '["Working Documents/deck.pptx", "Working Documents/plan.txt"]',
            'description': desc,
        }

    def test_rp099_binary_edit_stays_inline(self):
        with patch('model_caps.model_capabilities', return_value=dict(SMALL_CAPS)):
            out = build(self._task(RP099_DESC), self.proj)
        self.assertEqual(out['mode'], 'inline')
        self.assertEqual(out['batch_payload'], '')
        self.assertIn('too large to inline', out['prompt'])
        self.assertNotIn('supplied in chunks below', out['prompt'])

    def test_read_only_task_still_batches(self):
        with patch('model_caps.model_capabilities', return_value=dict(SMALL_CAPS)):
            out = build(self._task(READ_ONLY_DESC), self.proj)
        self.assertEqual(out['mode'], 'batch')
        self.assertTrue(out['batch_payload'])
        self.assertIn('supplied in chunks below', out['prompt'])


class BinaryDocumentEditTaskTests(unittest.TestCase):
    """Unit tests for the modify-intent helper."""

    def test_rp099_like_task_is_an_edit(self):
        self.assertTrue(_binary_document_edit_task(
            RP099_DESC, [('Working Documents/deck.pptx', None)]))

    def test_read_only_task_is_not_an_edit(self):
        self.assertFalse(_binary_document_edit_task(
            READ_ONLY_DESC, [('Working Documents/deck.pptx', None)]))

    def test_binary_not_mentioned_is_not_an_edit(self):
        desc = "Insert a new column in the quarterly report."
        self.assertFalse(_binary_document_edit_task(
            desc, [('Working Documents/deck.pptx', None)]))

    def test_text_only_files_are_not_an_edit(self):
        self.assertFalse(_binary_document_edit_task(
            RP099_DESC, [('Working Documents/plan.txt', 'some text')]))


if __name__ == '__main__':
    unittest.main(verbosity=2)


class VibeInlineByteBudgetTests(unittest.TestCase):
    """Unit tests for the vibe argv byte budget helper (RP-114-E1)."""

    def test_budget_value_in_vibe_mode(self):
        from unittest.mock import patch
        import agent_config
        import agent_router
        with patch.object(agent_config, 'MISTRAL_MODE', 'vibe'), \
             patch.object(agent_config, 'VIBE_MODELS', ['mistral-large-4']), \
             patch.object(agent_router, 'VIBE_PROMPT_MAX_BYTES', 120_000):
            self.assertEqual(prompt_builder._vibe_inline_byte_budget('mistral-large-4'),
                             120_000 - prompt_builder._VIBE_PROMPT_RESERVE_BYTES)

    def test_budget_has_floor(self):
        from unittest.mock import patch
        import agent_config
        import agent_router
        with patch.object(agent_config, 'MISTRAL_MODE', 'vibe'), \
             patch.object(agent_config, 'VIBE_MODELS', ['mistral-large-4']), \
             patch.object(agent_router, 'VIBE_PROMPT_MAX_BYTES', 30_000):
            self.assertEqual(prompt_builder._vibe_inline_byte_budget('mistral-large-4'), 20_000)

    def test_budget_none_in_api_mode(self):
        from unittest.mock import patch
        import agent_config
        with patch.object(agent_config, 'MISTRAL_MODE', 'api'), \
             patch.object(agent_config, 'VIBE_MODELS', ['mistral-large-4']):
            self.assertIsNone(prompt_builder._vibe_inline_byte_budget('mistral-large-4'))

    def test_budget_none_for_model_outside_vibe_allowlist(self):
        from unittest.mock import patch
        import agent_config
        with patch.object(agent_config, 'MISTRAL_MODE', 'vibe'), \
             patch.object(agent_config, 'VIBE_MODELS', ['mistral-large-4']):
            self.assertIsNone(prompt_builder._vibe_inline_byte_budget('scw-gpt-oss-120b'))

    def test_budget_applies_to_slot_forced_vibe_runs(self):
        # Slots 2 (Mistral Pro) and 3 (PAYG) make the executor force
        # force_mistral_mode='vibe' even when the global toggle says 'api'.
        import agent_config
        import agent_router
        with patch.object(agent_config, 'MISTRAL_MODE', 'api'), \
             patch.object(agent_config, 'VIBE_MODELS', ['mistral-large-4']), \
             patch.object(agent_router, 'VIBE_PROMPT_MAX_BYTES', 120_000):
            expected = 120_000 - prompt_builder._VIBE_PROMPT_RESERVE_BYTES
            self.assertEqual(
                prompt_builder._vibe_inline_byte_budget('mistral-large-4', slot=2),
                expected)
            self.assertEqual(
                prompt_builder._vibe_inline_byte_budget('mistral-large-4', slot='3'),
                expected)
            # Slots without a forced mode keep the global setting.
            self.assertIsNone(
                prompt_builder._vibe_inline_byte_budget('mistral-large-4', slot=4))
            # A non-vibe model is never budgeted, even on a vibe slot.
            self.assertIsNone(
                prompt_builder._vibe_inline_byte_budget('scw-gpt-oss-120b', slot=3))


class VibeByteBudgetBuildTests(unittest.TestCase):
    """build() must not inline more file content than the vibe argv can carry.

    Regression (RP-114-E1): the token budget is sized from the model context
    window (mistral-large-latest = 262k tokens), so a ~260 KB document was
    inlined into a prompt the vibe CLI hard-caps at 120 KB and tail-truncates —
    dropping the task description at the front. With MISTRAL_MODE='vibe' the
    oversized file must be demoted to a "read it with your tools" path note.
    """

    MODEL = 'mistral-large-4'
    # Huge context window so the TOKEN gate lets the file through: the demotion
    # under test must come from the byte budget, not from the token cap.
    BIG_CAPS = {'context_window': 1_000_000, 'max_tokens': 8192, 'vision': False}
    SENTINEL = 'UNIQUE-DOC-SENTINEL-LINE'

    def setUp(self):
        import agent_config
        import agent_router
        self.agent_config = agent_config
        self.agent_router = agent_router
        self.td = tempfile.TemporaryDirectory()
        self.proj = self.td.name
        wd = os.path.join(self.proj, 'Working Documents')
        os.makedirs(wd)
        self.fname = 'report_notes.md'
        with open(os.path.join(wd, self.fname), 'w', encoding='utf-8') as f:
            f.write(self.SENTINEL + '\n' + ('x' * 300_000))

    def tearDown(self):
        self.td.cleanup()

    def _task(self):
        return {
            'id': 1,
            'title': 'x',
            'model': self.MODEL,
            'context_refs': '["Working Documents/report_notes.md"]',
            'description': f'Analyse {self.fname} and write summary.md',
        }

    def _build(self, mode):
        with patch('model_caps.model_capabilities', return_value=dict(self.BIG_CAPS)), \
             patch.object(self.agent_config, 'MISTRAL_MODE', mode), \
             patch.object(self.agent_config, 'VIBE_MODELS', [self.MODEL]), \
             patch.object(self.agent_router, 'VIBE_PROMPT_MAX_BYTES', 40_000):
            return build(self._task(), self.proj)

    def test_vibe_demotes_oversized_inline_file_to_path_note(self):
        out = self._build('vibe')
        self.assertEqual(out['mode'], 'inline')
        self.assertNotIn(self.SENTINEL, out['prompt'])
        self.assertNotIn('x' * 1000, out['prompt'])
        self.assertIn('too large to inline', out['prompt'])
        self.assertIn(self.fname, out['prompt'])
        # No chunk payload: the model reads the file with read_file instead.
        self.assertEqual(out['batch_payload'], '')

    def test_api_mode_still_inlines_the_same_file(self):
        out = self._build('api')
        self.assertEqual(out['mode'], 'inline')
        self.assertIn(self.SENTINEL, out['prompt'])
        self.assertNotIn('too large to inline', out['prompt'])


class VibeByteBudgetFallbackTests(VibeByteBudgetBuildTests):
    """Same gate on the fallback (no context_refs) auto-catch path."""

    def _task(self):
        # No context_refs: the file is caught by _resolve_referenced_files
        # because its exact name appears in the description.
        return {
            'id': 1,
            'title': 'x',
            'model': self.MODEL,
            'description': f'Analyse {self.fname} and write summary.md',
        }


class BatchRunSkipsVibeByteGateTests(unittest.TestCase):
    """A batch run must skip the vibe argv byte gate (RP-114-E1 follow-up).

    A vibe-routed model can reference (a) one token-oversized text file (routed
    to batch mode) and (b) other inline files whose cumulative bytes exceed the
    argv byte budget. Demoting (b) to "read it with your tools" path notes was
    pointless and lossy: a batch run routes Mistral through the toolless API
    (run_large_file_batch forces force_mistral_mode='api', no argv limit), so
    the demoted content became a path note a toolless model cannot act on — it
    never reached the model. The byte gate must only run when the run stays
    inline (batch_files empty, or the RP-099 binary-edit override about to
    clear it back to inline).
    """

    MODEL = 'mistral-large-4'
    # Token/byte arithmetic these tests rely on (prompt_builder.build):
    #   * _approx_token_count = len(text) // 2
    #   * inline_budget   = 200_000 * 0.50 = 100_000 tokens
    #   * single_file_max = 200_000 * 0.40 =  80_000 tokens (160_000 chars)
    #   * vibe byte budget = max(20_000, 40_000 - _VIBE_PROMPT_RESERVE_BYTES)
    #     = max(20_000, 40_000 - 45_000) = 20_000 bytes (the floor)
    # big.txt:    500_000 chars -> 250_000 tokens > 80_000  -> token-batched.
    # medium.md:   50_000 chars ->  25_000 tokens <= 80_000 -> inline-eligible,
    #              but 50_000 bytes > the 20_000-byte budget, so it IS demoted
    #              whenever the byte gate runs (that is how these tests tell
    #              a skipped gate apart from a run one).
    CAPS = {'context_window': 200_000, 'max_tokens': 8192, 'vision': False}
    BIG_SENTINEL = 'UNIQUE-BIG-FILE-SENTINEL-LINE'
    MEDIUM_SENTINEL = 'UNIQUE-MEDIUM-FILE-SENTINEL-LINE'

    def setUp(self):
        import agent_config
        import agent_router
        self.agent_config = agent_config
        self.agent_router = agent_router
        self.td = tempfile.TemporaryDirectory()
        self.proj = self.td.name
        wd = os.path.join(self.proj, 'Working Documents')
        os.makedirs(wd)
        with open(os.path.join(wd, 'big.txt'), 'w', encoding='utf-8') as f:
            f.write(self.BIG_SENTINEL + '\n'
                    + 'x' * (500_000 - len(self.BIG_SENTINEL) - 1))
        with open(os.path.join(wd, 'medium.md'), 'w', encoding='utf-8') as f:
            f.write(self.MEDIUM_SENTINEL + '\n'
                    + 'y' * (50_000 - len(self.MEDIUM_SENTINEL) - 1))
        # Fake PDF bytes: _extract_pdf_text() fails -> inline binary note.
        with open(os.path.join(wd, 'input.pdf'), 'wb') as f:
            f.write(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')

    def tearDown(self):
        self.td.cleanup()

    def _task(self):
        return {
            'id': 1,
            'title': 'x',
            'model': self.MODEL,
            'context_refs': '["Working Documents/big.txt", '
                            '"Working Documents/medium.md"]',
            'description': ('Summarise big.txt using context from medium.md '
                            'and write summary.md'),
        }

    def _binary_edit_task(self):
        """RP-099 binary-edit variant: run stays inline, so the gate runs."""
        task = self._task()
        task['context_refs'] = ('["Working Documents/big.txt", '
                                '"Working Documents/medium.md", '
                                '"Working Documents/input.pdf"]')
        task['description'] = ('Update input.pdf using context from medium.md '
                               'and big.txt, then write summary.md')
        return task

    def _build(self, task=None):
        with patch('model_caps.model_capabilities', return_value=dict(self.CAPS)), \
             patch.object(self.agent_config, 'MISTRAL_MODE', 'vibe'), \
             patch.object(self.agent_config, 'VIBE_MODELS', [self.MODEL]), \
             patch.object(self.agent_router, 'VIBE_PROMPT_MAX_BYTES', 40_000):
            return build(task or self._task(), self.proj)

    def test_batch_run_skips_vibe_byte_gate(self):
        # big.txt is token-batched, so the run goes to the toolless batch API:
        # medium.md must stay in the base prompt — demoting it to a "read it
        # with your tools" path note there would lose its content entirely.
        out = self._build()
        self.assertEqual(out['mode'], 'batch')
        self.assertIn(self.MEDIUM_SENTINEL, out['prompt'])
        # No demotion happened: no "read it with your tools" path note.
        self.assertNotIn('too large to inline', out['prompt'])
        # big.txt rides the chunk payload, not the prompt.
        self.assertNotIn(self.BIG_SENTINEL, out['prompt'])
        self.assertIn(self.BIG_SENTINEL, out['batch_payload'])
        self.assertIn('supplied in chunks below', out['prompt'])

    def test_binary_edit_override_keeps_vibe_byte_gate(self):
        # RP-099: "Update input.pdf" must MODIFY a referenced binary document,
        # so the override clears batch_files and the run stays inline — the
        # byte gate must still run: medium.md (over budget) is demoted to a
        # path note the toolful vibe model CAN act on, and big.txt is
        # path-noted instead of chunked.
        out = self._build(self._binary_edit_task())
        self.assertEqual(out['mode'], 'inline')
        self.assertEqual(out['batch_payload'], '')
        self.assertNotIn(self.MEDIUM_SENTINEL, out['prompt'])
        self.assertNotIn(self.BIG_SENTINEL, out['prompt'])
        self.assertIn('too large to inline', out['prompt'])
        # input.pdf itself got the binary note.
        self.assertIn('cannot be inlined as text', out['prompt'])


class BatchRunSkipsVibeByteGateFallbackTests(BatchRunSkipsVibeByteGateTests):
    """Same skip on the fallback (no context_refs) auto-catch path.

    The files are caught by _resolve_referenced_files because their exact
    basenames appear in the description.
    """

    def _task(self):
        return {
            'id': 1,
            'title': 'x',
            'model': self.MODEL,
            'description': ('Summarise big.txt using context from medium.md '
                            'and write summary.md'),
        }

    def _binary_edit_task(self):
        # Fallback variant: no context_refs — all three files (input.pdf
        # included) must be caught by name from the description alone.
        task = self._task()
        task['description'] = ('Update input.pdf using context from medium.md '
                               'and big.txt, then write summary.md')
        return task
