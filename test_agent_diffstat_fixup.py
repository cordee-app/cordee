"""Tests for executor-side diffstat path fixup (_fixup_abbrev_diffstat).

Covers both git `--stat` truncation forms:
  • `.../basename`      — path truncation, basename intact;
  • `...tail_of_name`   — filename truncation, no slash.
"""
import os
import tempfile
import unittest

import agent_executor


LONG_NAME = '_Bilateral_Meeting_2026-11-05_with_Polish_Plan.pptx'
LONG_REL = 'Artifacts/Projects/Poland/%s' % LONG_NAME
TAIL = '_Bilateral_Meeting_2026-11-05_with_Polish_Plan.pptx'


class FixupAbbrevDiffstatTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.project = os.path.join(self.td.name, 'proj')
        os.makedirs(self.project, exist_ok=True)

    def tearDown(self):
        self.td.cleanup()

    def _write(self, rel, content='x'):
        path = os.path.join(self.project, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(content)
        return path

    def _fix(self, diffstat, **kwargs):
        return agent_executor._fixup_abbrev_diffstat(self.project, diffstat, **kwargs)

    # 1 — no-slash form, priority 1 (extra_candidates)
    def test_no_slash_resolves_via_extra_candidates(self):
        self._write(LONG_REL)
        line = ' ...%s | Bin 0 -> 101653 bytes' % LONG_NAME
        out = self._fix(line, extra_candidates=[LONG_REL])
        self.assertIn(LONG_REL, out)
        self.assertNotIn('...', out)
        self.assertTrue(out.endswith('| Bin 0 -> 101653 bytes'))

    # 1b — RP-099 shape: the visible token is only a TAIL of the real filename
    def test_no_slash_truncated_tail_resolves_via_extra_candidates(self):
        rel = 'Artifacts/Projects/Poland/PL_Bilateral_Meeting_2026-11-05_with_Polish_Plan.pptx'
        self._write(rel)
        line = ' ..._Bilateral_Meeting_2026-11-05_with_Polish_Plan.pptx | Bin 0 -> 101653 bytes'
        out = self._fix(line, extra_candidates=[rel])
        self.assertIn(rel, out)
        self.assertNotIn('...', out)

    # 2 — no-slash form, priority 2 (task output folder, no extra candidates)
    def test_no_slash_resolves_via_task_output_folder(self):
        rel = 'Artifacts/outputs/99-run/%s' % LONG_NAME
        self._write(rel)
        line = ' ...%s | Bin 0 -> 101653 bytes' % LONG_NAME
        out = self._fix(line, task_id=99)
        self.assertIn(rel, out)
        self.assertNotIn('...', out)

    # 3 — no-slash form, priority 3 (unique suffix match elsewhere in the tree)
    def test_no_slash_resolves_via_unique_index_suffix(self):
        rel = 'Docs/2026/%s' % LONG_NAME
        self._write(rel)
        line = ' ...%s | Bin 0 -> 1 bytes' % LONG_NAME
        out = self._fix(line)
        self.assertIn(rel, out)
        self.assertNotIn('...', out)

    # 4 — no-slash form, ambiguous at the same priority → unchanged
    def test_no_slash_ambiguous_is_unchanged(self):
        a = 'A/%s' % LONG_NAME
        b = 'B/%s' % LONG_NAME
        self._write(a)
        self._write(b)
        line = ' ...%s | Bin 0 -> 1 bytes' % LONG_NAME
        out = self._fix(line, extra_candidates=[a, b])
        self.assertEqual(out, line)

    # 4b — ambiguity via the index (no extra candidates) → unchanged
    def test_no_slash_ambiguous_index_is_unchanged(self):
        self._write('A/%s' % LONG_NAME)
        self._write('B/%s' % LONG_NAME)
        line = ' ...%s | Bin 0 -> 1 bytes' % LONG_NAME
        out = self._fix(line)
        self.assertEqual(out, line)

    # 5 — no-slash form, file deleted / never existed → unchanged, no resurrection
    def test_no_slash_missing_file_is_unchanged(self):
        line = ' ...%s | Bin 0 -> 101653 bytes' % LONG_NAME
        out = self._fix(line, task_id=7, extra_candidates=[LONG_REL])
        self.assertEqual(out, line)

    # 6 — slash form regression: still matches by exact basename
    def test_slash_form_still_resolves(self):
        self._write('docs/notes.md', 'hello')
        line = ' .../notes.md | 2 +-'
        out = self._fix(line)
        self.assertIn('docs/notes.md', out)
        self.assertNotIn('...', out)
        self.assertTrue(out.endswith('| 2 +-'))

    # 6b — slash form does NOT fall back to suffix matching (basename must be exact)
    def test_slash_form_does_not_match_filename_suffix(self):
        self._write('docs/my_notes.md', 'hello')
        line = ' .../notes.md | 2 +-'
        out = self._fix(line)
        self.assertEqual(out, line)

    # 6c — slash form ambiguity stays unchanged
    def test_slash_form_ambiguous_is_unchanged(self):
        self._write('a/notes.md')
        self._write('b/notes.md')
        line = ' .../notes.md | 2 +-'
        out = self._fix(line)
        self.assertEqual(out, line)

    # 7 — idempotency
    def test_idempotent_second_run(self):
        self._write(LONG_REL)
        line = ' ...%s | Bin 0 -> 101653 bytes' % LONG_NAME
        once = self._fix(line, extra_candidates=[LONG_REL])
        twice = self._fix(once, extra_candidates=[LONG_REL])
        self.assertEqual(once, twice)
        self.assertIn(LONG_REL, twice)

    # 8 — non-abbreviated lines and non-matching text untouched
    def test_non_abbreviated_and_unrelated_text_unchanged(self):
        text = '\n'.join([
            ' README.md | 2 +-',
            ' Docs/guide.md | 4 ++--',
            'some prose without a pipe',
            '',
            ' total 6 files changed',
        ])
        self._write('README.md')
        self._write('Docs/guide.md')
        self.assertEqual(self._fix(text), text)

    # 8b — empty / whitespace input returned as-is
    def test_empty_input_unchanged(self):
        self.assertEqual(self._fix(''), '')
        self.assertEqual(self._fix(None), None)


if __name__ == '__main__':
    unittest.main()
