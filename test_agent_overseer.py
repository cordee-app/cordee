"""Tests for agent_overseer's adaptive retry ladder (no network, no live DB).

The DB is never opened: ``agent_db.get_app_settings`` / ``set_app_setting``
are patched out, and every reviewer call goes through a fake ``route``.
"""
import json
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


class PostRunGitDiffstatTests(unittest.TestCase):
    """H3 must see the run's diffstat, and a catastrophic overwrite must not
    be approvable by a model that only read the run's own summary.

    Exec 20001366: a run overwrote a 1,040-line document with 70 lines; the
    stored diffstat said ``2 files changed, 51 insertions(+), 1020 deletions(-)``
    and H3, never shown it, reported an "append" and approved.
    """

    # E1 — the real regression shape: one deletion-dominated tracked file.
    E1_DIFFSTAT = (' Etap_III_18_Strategic_Points_Arguments.md | 1069 '
                   + '+' * 2 + '-' * 33 + '\n'
                   ' 2 files changed, 51 insertions(+), 1020 deletions(-)')
    # E2 — additions-only run (a new large deliverable): must never trip.
    E2_DIFFSTAT = ' 3 files changed, 3365 insertions(+), 1 deletion(-)'

    def setUp(self):
        self.prompts = []
        self.project = {'name': 'Strategy', 'path': '', 'aingel_model': 'm'}
        self.task = {'id': 10001281, 'title': 'Draft answers',
                     'description': 'Append draft answers to the strategic points document.'}
        self.execution = {'id': 20001366, 'model': 'm', 'status': 'done',
                          'finish_reason': 'stop', 'tokens_input': 10,
                          'tokens_output': 20}
        self.reply = self._reply()

    @staticmethod
    def _reply(**over):
        base = {'severity': 'ok', 'findings': [], 'recommendation': 'approve',
                'gate_for_next': 'proceed', 'brief_2s': 'Done.',
                'unread_files': []}
        base.update(over)
        return json.dumps(base)

    _UNSET = object()

    def _analyze(self, git_diffstat=_UNSET, reply=None, degraded=None):
        def fake_ask(model, prompt, budget, path, caller):
            self.prompts.append(prompt)
            return (reply if reply is not None else self.reply), degraded

        kwargs = {}
        if git_diffstat is not self._UNSET:
            kwargs['git_diffstat'] = git_diffstat
        with patch.object(agent_overseer, '_ask', side_effect=fake_ask):
            return agent_overseer.post_run_analysis(
                task=self.task, execution=self.execution, project=self.project,
                output_text='Appended draft answers to the document.', **kwargs)

    # ── Test 1 — the prompt carries the diffstat ─────────────────────────────
    def test_diffstat_block_appears_in_h3_prompt(self):
        review = self._analyze(' 1 file changed, 40 insertions(+), 2 deletions(-)')
        prompt = self.prompts[0]
        self.assertIn('Git changes this run (diffstat):', prompt)
        self.assertIn('1 file changed, 40 insertions(+), 2 deletions(-)', prompt)
        # the diffstat precedes the output snippet it is meant to qualify
        self.assertLess(prompt.index('Git changes this run'),
                        prompt.index('Output (head and tail'))
        self.assertEqual(review['severity'], 'ok')  # benign diffstat, no backstop

    # ── Test 2 — E1: deterministic backstop overrides an "ok" reply ─────────
    def test_destructive_overwrite_forces_issue(self):
        # The mocked Guide says everything is fine — the code must not agree.
        review = self._analyze(self.E1_DIFFSTAT,
                               reply=self._reply(severity='ok',
                                                 recommendation='approve',
                                                 gate_for_next='proceed'))
        self.assertEqual(review['severity'], 'issue')
        self.assertEqual(review['gate_for_next'], 'hold')
        self.assertEqual(review['recommendation'], 'discuss')
        text = ' '.join(review['findings']).lower()
        self.assertIn('etap_iii_18_strategic_points_arguments.md', text)
        self.assertIn('1020 deletions', text)
        self.assertIn('restored from git history', text)

    def test_destructive_overwrite_survives_degraded_h3(self):
        review = self._analyze(self.E1_DIFFSTAT, reply=None,
                               degraded='provider down')
        self.assertTrue(review.get('degraded'))
        self.assertEqual(review['severity'], 'issue')
        self.assertEqual(review['gate_for_next'], 'hold')
        self.assertEqual(review['recommendation'], 'discuss')

    # ── Test 3 — E2: additions-only run is untouched ────────────────────────
    def test_additions_only_diffstat_does_not_trip(self):
        self.assertEqual(
            agent_overseer._diffstat_overwrite_findings(self.E2_DIFFSTAT), [])
        review = self._analyze(self.E2_DIFFSTAT)
        self.assertEqual(review['severity'], 'ok')
        self.assertEqual(review['recommendation'], 'approve')
        self.assertEqual(review['gate_for_next'], 'proceed')
        self.assertEqual(review['findings'], [])

    def test_threshold_ignores_ordinary_rewrites(self):
        # 499 deletions, or deletions that are not >2x insertions: no finding.
        self.assertEqual(agent_overseer._diffstat_overwrite_findings(
            ' 1 file changed, 10 insertions(+), 499 deletions(-)'), [])
        self.assertEqual(agent_overseer._diffstat_overwrite_findings(
            ' 1 file changed, 400 insertions(+), 800 deletions(-)'), [])

    def test_unparseable_per_file_lines_still_report_totals(self):
        findings = agent_overseer._diffstat_overwrite_findings(
            ' 2 files changed, 20 insertions(+), 1200 deletions(-)')
        self.assertEqual(len(findings), 1)
        self.assertIn('1200 deletions', findings[0])
        self.assertIn('restored from git history', findings[0])

    # ── Test 4 — no diffstat: today's behaviour, byte for byte ──────────────
    def test_no_diffstat_block_when_absent(self):
        for value in ('', None):
            self.prompts.clear()
            review = self._analyze(value)
            self.assertNotIn('Git changes this run', self.prompts[0])
            self.assertNotIn('diffstat', self.prompts[0].lower())
            self.assertEqual(review['severity'], 'ok')
            self.assertEqual(review['recommendation'], 'approve')
            self.assertEqual(review['gate_for_next'], 'proceed')
            self.assertEqual(review['findings'], [])

    def test_default_call_has_no_diffstat(self):
        # Backward compatibility: existing callers pass no git_diffstat at all.
        review = self._analyze()
        self.assertNotIn('Git changes this run', self.prompts[0])
        self.assertEqual(review['severity'], 'ok')


class ReviewerSeesClarificationsTests(unittest.TestCase):
    """Reviewer prompts must carry the 'User clarifications' block even when
    the description is longer than the prompt's body cap.

    Regression: task 10001279 / RP-112 — a 2,874-char description with the
    user's answers at offset 2,698. H2 and H2a capped the description at
    2,500 chars (H3 at 4,000), so every reviewer round was blind to the
    answers: H2a re-asked q1 verbatim, the hold guards made the task
    unstartable, and each re-confirm armed another blind review round.
    """

    MARKER = agent_overseer._USER_CLARIFICATIONS_MARKER
    Q1 = '- q1: Leave the bracketed placeholder as-is for later manual completion'
    Q2 = '- q2: Keep each question once, above the (a)/(b) subsections'
    # Unique body tail placed BEYOND the 2,500/4,000-char caps.
    TAIL_SENTINEL = 'UNIQUE_BODY_TAIL_BEYOND_THE_CAP'

    def setUp(self):
        self.prompts = []
        self.project = {'name': 'Strategy', 'path': '', 'aingel_model': 'm'}

    # Mirrors the real task: a long prose body, then the answers block the
    # /gate 'answer' flow appends under the marker (agent_api line ~3530).
    def _long_description(self, body_chars=2700):
        filler = ('Revise the draft strategy section by section, keeping the '
                  'numbering intact and the tone formal throughout. ')
        body = (filler * (body_chars // len(filler) + 2))[:body_chars]
        return (body + self.TAIL_SENTINEL + '\n\n' + self.MARKER
                + '\n' + self.Q1 + '\n' + self.Q2)

    def _fake_ask(self, reply):
        def fake_ask(model, prompt, budget, path, caller):
            self.prompts.append(prompt)
            return reply, None
        return fake_ask

    # ── Helper unit tests ────────────────────────────────────────────────────
    def test_helper_truncates_body_not_answers(self):
        desc = self._long_description()   # 2,700-char body + answers
        out = agent_overseer._description_for_review(desc, 2500)
        self.assertIn('…[truncated]', out)          # the body WAS capped
        self.assertNotIn(self.TAIL_SENTINEL, out)  # body beyond the cap is gone
        self.assertIn(self.MARKER, out)
        self.assertIn(self.Q1, out)                 # answers survive in full
        self.assertIn(self.Q2, out)
        # the full answers block follows the truncated body
        self.assertLess(out.index('…[truncated]'), out.index(self.MARKER))

    def test_helper_without_marker_matches_old_truncation(self):
        filler = 'Plain long description with no clarifications block. '
        desc = (filler * 200)[:3000] + self.TAIL_SENTINEL
        out = agent_overseer._description_for_review(desc, 2500)
        # byte for byte the old behaviour
        self.assertEqual(out, desc[:2500] + '…[truncated]')
        self.assertNotIn(self.TAIL_SENTINEL, out)
        # short / empty / missing descriptions pass through unchanged
        self.assertEqual(
            agent_overseer._description_for_review('Short task.', 2500),
            'Short task.')
        self.assertEqual(agent_overseer._description_for_review('   ', 2500), '')
        self.assertEqual(agent_overseer._description_for_review(None, 2500), '')

    def test_helper_bounds_huge_answers_block(self):
        huge = (self.MARKER + '\n- q1: ' + 'a very long pasted answer. ' * 200)
        desc = 'Short body.\n\n' + huge
        out = agent_overseer._description_for_review(desc, 2500)
        self.assertIn('…[answers truncated]', out)
        # bounded: short body untouched + answers at the safety-valve cap
        expected = ('Short body.\n\n'
                    + huge[:agent_overseer._USER_CLARIFICATIONS_MAX_CHARS]
                    + '\n…[answers truncated]')
        self.assertEqual(out, expected)
        self.assertLess(len(out), len(desc))

    # ── H2a — the confirm-time question round ───────────────────────────────
    def test_h2a_prompt_carries_answers_and_rule(self):
        task = {'id': 10001279, 'title': 'RP-112',
                'description': self._long_description()}
        reply = json.dumps({'questions': [], 'reason': 'clear'})
        with patch.object(agent_overseer, '_ask',
                          side_effect=self._fake_ask(reply)):
            result = agent_overseer.pre_check_questions(task, self.project, [])
        prompt = self.prompts[0]
        self.assertIn(self.MARKER, prompt)
        self.assertIn(self.Q1, prompt)   # answers visible, verbatim
        self.assertIn(self.Q2, prompt)
        self.assertNotIn(self.TAIL_SENTINEL, prompt)  # body still capped
        self.assertIn('ALREADY answered', prompt)       # the new rule
        self.assertIn('Do NOT re-ask', prompt)
        self.assertEqual(result['questions'], [])
        self.assertEqual(result['gate_state'], 'open')

    # ── H2 — the pre-run gate ───────────────────────────────────────────────
    def test_h2_prompt_carries_answers_and_rule(self):
        task = {'id': 10001279, 'title': 'RP-112',
                'description': self._long_description()}
        reply = json.dumps({'completeness': 'complete', 'missing_files': [],
                            'binary_unparsable': [], 'capability_warnings': [],
                            'strategic_advice': 'clear', 'gate': 'run',
                            'reason': 'clear'})
        with patch.object(agent_overseer, '_ask',
                          side_effect=self._fake_ask(reply)):
            result = agent_overseer.pre_run_check(
                task, self.project, caught_files=[], noted_binary=[],
                model_caps={}, dep_titles=[])
        prompt = self.prompts[0]
        self.assertIn(self.MARKER, prompt)
        self.assertIn(self.Q1, prompt)   # answers visible, verbatim
        self.assertIn(self.Q2, prompt)
        self.assertNotIn(self.TAIL_SENTINEL, prompt)  # body still capped
        self.assertIn('ALREADY answered', prompt)       # the new rule
        self.assertIn('decided requirements', prompt)
        self.assertEqual(result['gate'], 'run')

    # ── H3 — the post-run analysis ──────────────────────────────────────────
    def test_h3_prompt_carries_answers(self):
        # Body past the 4,000-char H3 cap; the marker sits beyond it.
        task = {'id': 10001279, 'title': 'RP-112',
                'description': self._long_description(body_chars=4200)}
        execution = {'id': 20001366, 'model': 'm', 'status': 'done',
                     'finish_reason': 'stop', 'tokens_input': 10,
                     'tokens_output': 20}
        reply = json.dumps({'severity': 'ok', 'findings': [],
                            'recommendation': 'approve',
                            'gate_for_next': 'proceed', 'brief_2s': 'Done.',
                            'unread_files': []})
        with patch.object(agent_overseer, '_ask',
                          side_effect=self._fake_ask(reply)):
            review = agent_overseer.post_run_analysis(
                task=task, execution=execution, project=self.project,
                output_text='Deliverable written.')
        prompt = self.prompts[0]
        self.assertIn(self.MARKER, prompt)
        self.assertIn(self.Q1, prompt)   # answers visible, verbatim
        self.assertIn(self.Q2, prompt)
        self.assertNotIn(self.TAIL_SENTINEL, prompt)  # body still capped
        self.assertEqual(review['severity'], 'ok')


if __name__ == '__main__':
    unittest.main()
