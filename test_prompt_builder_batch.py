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
