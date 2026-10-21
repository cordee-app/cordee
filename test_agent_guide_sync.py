"""Tests for GUIDE.md regeneration — phase headings must not repeat their prefix.

`agent_phases.parse_guide` stores a whole GUIDE heading ('Phase 1 — Title') as
`phase_name`, and `regenerate_guide` numbers the headings itself. Before the fix
every regenerated file read '## Phase 1 — Phase 1 — Title' (all the vault
projects checked on 2026-10-21).
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import agent_guide_sync as gs


def _row(title, phase, status='pending', archived=0):
    return {
        'title': title,
        'status': status,
        'phase_name': phase,
        'archived': archived,
        'work_session_slot': None,
        'slot_position': None,
        'created_at': '2026-01-01T00:00:00',
    }


class PhaseLabelTests(unittest.TestCase):
    def test_strips_single_prefix(self):
        self.assertEqual(gs._phase_label('Phase 1 — Financial Updates'),
                         'Financial Updates')

    def test_strips_repeated_prefixes(self):
        self.assertEqual(gs._phase_label('Phase 2 — Phase 2 — Core Agent'),
                         'Core Agent')
        self.assertEqual(gs._phase_label('Phase 1 — Phase 2 — Phase 3 — Deep'),
                         'Deep')

    def test_strips_colon_and_hyphen_variants(self):
        self.assertEqual(gs._phase_label('Phase 3: Planning'), 'Planning')
        self.assertEqual(gs._phase_label('PHASE 4 - Go to market'), 'Go to market')

    def test_keeps_names_without_prefix(self):
        self.assertEqual(gs._phase_label('Notebook'), 'Notebook')
        self.assertEqual(gs._phase_label('Discovery'), 'Discovery')
        self.assertEqual(gs._phase_label(''), '')

    def test_bare_phase_number_falls_back_to_original(self):
        self.assertEqual(gs._phase_label('Phase 1'), 'Phase 1')


class RegenerateGuideTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = self._tmp.name
        self.rows = [
            _row('First task', 'Phase 1 — Discovery'),
            _row('Second task', 'Phase 1 — Discovery', status='done'),
            _row('Third task', 'Phase 2 — Phase 2 — Build'),
            _row('Loose note', ''),
        ]

    def tearDown(self):
        self._tmp.cleanup()

    def _regenerate(self):
        phases = ['Phase 1 — Discovery', 'Phase 2 — Phase 2 — Build', 'Notebook']
        with patch.object(gs, '_read_tasks', return_value=self.rows), \
                patch.object(gs, '_phase_order', return_value=phases):
            return gs.regenerate_guide(self.path, 1)

    def _guide(self):
        with open(os.path.join(self.path, 'GUIDE.md'), encoding='utf-8') as f:
            return f.read()

    def test_headings_carry_a_single_phase_prefix(self):
        self.assertTrue(self._regenerate())
        guide = self._guide()
        self.assertIn('## Phase 1 — Discovery\n', guide)
        self.assertIn('## Phase 2 — Build\n', guide)
        self.assertIn('## Phase 3 — Notebook\n', guide)
        self.assertNotIn('Phase 1 — Phase 1', guide)
        self.assertNotIn('Phase 2 — Phase 2', guide)

    def test_first_heading_is_not_glued_to_the_generated_header(self):
        self._regenerate()
        guide = self._guide()
        self.assertNotIn('-->##', guide)
        self.assertIn('\n\n## Phase 1 — Discovery\n', guide)
        self.assertTrue(guide.startswith('# '))

    def test_task_lines_are_preserved(self):
        self._regenerate()
        guide = self._guide()
        self.assertIn('- [ ] First task', guide)
        self.assertIn('- [x] Second task', guide)
        self.assertIn('- [ ] Third task', guide)
        self.assertIn('- [ ] Loose note', guide)

    def test_second_run_is_a_noop(self):
        self.assertTrue(self._regenerate())
        self.assertFalse(self._regenerate())


if __name__ == '__main__':
    unittest.main()
