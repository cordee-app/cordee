"""Tests for agent_overseer's adaptive retry ladder (no network, no live DB).

The DB is never opened: ``agent_db.get_app_settings`` / ``set_app_setting``
are patched out, and every reviewer call goes through a fake ``route``.
"""
import unittest
from unittest.mock import patch

import agent_db
import agent_overseer
import agent_router
import model_caps


class LadderTests(unittest.TestCase):
    def test_default_ladder_unchanged(self):
        # mistral-glm-5-3: floor 2048, cap 12000 → 4 rungs.
        self.assertEqual(
            agent_overseer._build_ladder(2048, 12000),
            [2048, 4096, 8192, 12000])

    def test_remembered_start_skips_failing_rungs(self):
        self.assertEqual(
            agent_overseer._build_ladder(2048, 12000, start=8192),
            [8192, 12000])

    def test_start_is_clamped_to_floor_and_ceiling(self):
        self.assertEqual(
            agent_overseer._build_ladder(2048, 12000, start=100)[0], 2048)
        self.assertEqual(
            agent_overseer._build_ladder(2048, 12000, start=999999), [12000])

    def test_ladder_never_exceeds_four_rungs(self):
        self.assertLessEqual(len(agent_overseer._build_ladder(1, 16384)), 4)


class RememberedBudgetTests(unittest.TestCase):
    def test_specific_key_wins(self):
        settings = {
            'overseer_budget:m:H2': '6000',
            'overseer_budget:m': '9000',
        }
        with patch.object(agent_db, 'get_app_settings', return_value=settings):
            self.assertEqual(
                agent_overseer._remembered_budget('m', 'H2', 2048, 12000), 6000)

    def test_fallback_key_used(self):
        settings = {'overseer_budget:m': '8192'}
        with patch.object(agent_db, 'get_app_settings', return_value=settings):
            self.assertEqual(
                agent_overseer._remembered_budget('m', 'H3', 2048, 12000), 8192)

    def test_clamped(self):
        settings = {'overseer_budget:m': '999999'}
        with patch.object(agent_db, 'get_app_settings', return_value=settings):
            self.assertEqual(
                agent_overseer._remembered_budget('m', 'H3', 2048, 12000), 12000)

    def test_invalid_or_missing_is_none(self):
        with patch.object(agent_db, 'get_app_settings', return_value={}):
            self.assertIsNone(
                agent_overseer._remembered_budget('m', 'H3', 2048, 12000))
        with patch.object(agent_db, 'get_app_settings',
                          return_value={'overseer_budget:m': 'lots'}):
            self.assertIsNone(
                agent_overseer._remembered_budget('m', 'H3', 2048, 12000))

    def test_db_error_is_none(self):
        with patch.object(agent_db, 'get_app_settings',
                          side_effect=RuntimeError('no table')):
            self.assertIsNone(
                agent_overseer._remembered_budget('m', 'H3', 2048, 12000))

    def test_persist_error_is_swallowed(self):
        with patch.object(agent_db, 'set_app_setting',
                          side_effect=RuntimeError('no table')):
            agent_overseer._remember_budget('m', 'H3', 8192)  # must not raise


class AskTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.saved = []
        self._patches = [
            patch.object(model_caps, 'effective_max_tokens',
                         side_effect=lambda model, want: 2048),
            patch.object(model_caps, 'get_cap',
                         return_value={'max_tokens': 12000}),
            patch.object(agent_db, 'set_app_setting',
                         side_effect=lambda k, v: self.saved.append((k, v))),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def _fake_route(self, replies):
        def route(model, prompt, mt, **kwargs):
            self.calls.append(mt)
            return replies[mt], 1, 1, 'stop'
        return route

    def test_remembered_budget_changes_first_rung(self):
        replies = {8192: '{"ok": true}'}
        with patch.object(agent_db, 'get_app_settings',
                          return_value={'overseer_budget:m:H2': '8192'}), \
             patch.object(agent_router, 'route', side_effect=self._fake_route(replies)):
            text, degraded = agent_overseer._ask('m', 'p', 800, '/tmp/p', 'H2')
        self.assertEqual(text, '{"ok": true}')
        self.assertIsNone(degraded)
        self.assertEqual(self.calls, [8192])
        self.assertEqual(self.saved, [
            ('overseer_budget:m:H2', '8192'),
            ('overseer_budget:m', '8192'),
        ])

    def test_no_remembered_budget_starts_at_floor(self):
        replies = {2048: '{"ok": true}'}
        with patch.object(agent_db, 'get_app_settings', return_value={}), \
             patch.object(agent_router, 'route', side_effect=self._fake_route(replies)):
            text, degraded = agent_overseer._ask('m', 'p', 800, '/tmp/p', 'H2')
        self.assertEqual(text, '{"ok": true}')
        self.assertIsNone(degraded)
        self.assertEqual(self.calls, [2048])

    def test_degraded_leaves_stored_budget_untouched(self):
        with patch.object(agent_db, 'get_app_settings',
                          return_value={'overseer_budget:m:H2': '8192'}), \
             patch.object(agent_router, 'route',
                          side_effect=self._fake_route({8192: '', 12000: ''})):
            text, degraded = agent_overseer._ask('m', 'p', 800, '/tmp/p', 'H2')
        self.assertIsNone(text)
        self.assertIn('empty response', degraded)
        self.assertEqual(self.calls, [8192, 12000])
        self.assertEqual(self.saved, [])

    def test_recovery_persists_successful_rung(self):
        replies = {2048: '', 4096: '{"ok": true}'}
        with patch.object(agent_db, 'get_app_settings', return_value={}), \
             patch.object(agent_router, 'route', side_effect=self._fake_route(replies)):
            text, degraded = agent_overseer._ask('m', 'p', 800, '/tmp/p', 'H3')
        self.assertEqual(text, '{"ok": true}')
        self.assertIsNone(degraded)
        self.assertEqual(self.saved, [
            ('overseer_budget:m:H3', '4096'),
            ('overseer_budget:m', '4096'),
        ])


if __name__ == '__main__':
    unittest.main()
