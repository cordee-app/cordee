"""Unit tests for agent_quotas owner resolution (Phase 4 multi-tenancy).

Regression context: token metering happens in route()'s single choke point,
which only has a project *path*. It resolved the owner via
projects.owner_id only, so on projects created before multi-tenancy
(owner_id IS NULL — every pre-Phase-4 vault project) the owner came back
None and tokens were never metered for the acting member. The run counter
was metered (it uses resolve_owner_id(task, proj) → task.created_by), which
made the gap easy to miss: runs incremented, tokens stayed at 0.

These tests exercise the pure resolution/caching helpers with a stubbed DB,
so they need no real project tree.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import agent_db
import agent_quotas


class _FakeConn:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def commit(self):
        pass

    def close(self):
        pass


class ResolveOwnerForCallTests(unittest.TestCase):
    def setUp(self):
        agent_quotas._PATH_OWNER_CACHE.clear()

    def tearDown(self):
        agent_quotas._PATH_OWNER_CACHE.clear()

    def test_execution_author_wins_over_path_owner(self):
        """Actor-first: a free member on an admin-owned project is metered.

        Regression: owner-first ordering resolved to the admin owner, whose
        ``quotas_applicable()`` is False, silently disabling the member's
        run/token counters and model whitelist.
        """
        with patch.object(agent_quotas.db, 'get_project_by_path',
                          return_value={'owner_id': 1}), \
             patch.object(agent_quotas.db, 'get_execution',
                          return_value={'created_by': 3}):
            self.assertEqual(
                agent_quotas.resolve_owner_for_call('/p', exec_id=99), 3)

    def test_path_owner_is_fallback_when_no_actor(self):
        with patch.object(agent_quotas.db, 'get_project_by_path',
                          return_value={'owner_id': 7}), \
             patch.object(agent_quotas.db, 'get_execution',
                          return_value={'created_by': None}):
            self.assertEqual(
                agent_quotas.resolve_owner_for_call('/p', exec_id=99), 7)

    def test_falls_back_to_execution_author_when_project_ownerless(self):
        with patch.object(agent_quotas.db, 'get_project_by_path',
                          return_value={'owner_id': None}), \
             patch.object(agent_quotas.db, 'get_execution',
                          return_value={'created_by': 3}):
            self.assertEqual(
                agent_quotas.resolve_owner_for_call('/p', exec_id=20001065), 3)

    def test_falls_back_to_chat_author_when_no_exec(self):
        with patch.object(agent_quotas.db, 'get_project_by_path',
                          return_value={'owner_id': None}), \
             patch.object(agent_quotas.db, 'get_chat',
                          return_value={'created_by': 5}):
            self.assertEqual(
                agent_quotas.resolve_owner_for_call('/p', chat_id=30000141), 5)

    def test_none_when_no_path(self):
        self.assertIsNone(agent_quotas.resolve_owner_for_call(None, exec_id=1))

    def test_none_when_nothing_resolves(self):
        with patch.object(agent_quotas.db, 'get_project_by_path',
                          return_value={'owner_id': None}), \
             patch.object(agent_quotas.db, 'get_execution',
                          return_value={'created_by': None}):
            self.assertIsNone(
                agent_quotas.resolve_owner_for_call('/p', exec_id=1))

    def test_execution_author_cached_per_exec_id(self):
        calls = {'n': 0}

        def fake_get_execution(exec_id, *a, **k):
            calls['n'] += 1
            return {'created_by': 3}

        with patch.object(agent_quotas.db, 'get_project_by_path',
                          return_value={'owner_id': None}), \
             patch.object(agent_quotas.db, 'get_execution',
                          side_effect=fake_get_execution):
            first = agent_quotas.resolve_owner_for_call('/p', exec_id=42)
            second = agent_quotas.resolve_owner_for_call('/p', exec_id=42)
        self.assertEqual(first, second)
        self.assertEqual(calls['n'], 1, 'second call should hit the cache')


class ResolveOwnerIdTests(unittest.TestCase):
    def test_task_author_takes_priority_over_project_owner(self):
        """Actor-first: a member is metered even when an admin owns the project."""
        self.assertEqual(
            agent_quotas.resolve_owner_id({'created_by': 3}, {'owner_id': 1}), 3)

    def test_project_owner_used_when_no_task_author(self):
        self.assertEqual(
            agent_quotas.resolve_owner_id({'created_by': None}, {'owner_id': 7}), 7)

    def test_task_author_used_when_project_ownerless(self):
        self.assertEqual(
            agent_quotas.resolve_owner_id({'created_by': 3}, {'owner_id': None}), 3)

    def test_none_when_neither_present(self):
        self.assertIsNone(
            agent_quotas.resolve_owner_id({'created_by': None}, {'owner_id': None}))


class RecordTokensOverLimitTests(unittest.TestCase):
    """P1-1: usage must always be recorded, even past the limit.

    Regression: record_tokens used to refuse the increment once
    ``used + delta > limit`` and raise QuotaError (swallowed by route()).
    A user near the cap therefore kept spending without their counter
    moving, so the pre-flight check never tripped.
    """

    def setUp(self):
        self._saved_auth = os.environ.get('AINGEL_AUTH')
        os.environ['AINGEL_AUTH'] = 'oidc'
        self.td = tempfile.TemporaryDirectory()
        self._patch = patch.object(
            agent_db, 'DB_PATH', os.path.join(self.td.name, 'aingel.db'))
        self._patch.start()
        agent_db.init_db()
        # First-ever user becomes admin; create a non-admin free-tier user.
        agent_db.get_or_create_user('sub-admin', 'admin@test', 'Admin')
        self.user = agent_db.get_or_create_user('sub-free', 'free@test', 'Free')
        agent_db.update_user_plan(self.user['id'], 'free')

    def tearDown(self):
        self._patch.stop()
        self.td.cleanup()
        if self._saved_auth is None:
            os.environ.pop('AINGEL_AUTH', None)
        else:
            os.environ['AINGEL_AUTH'] = self._saved_auth

    def _total(self):
        usage = agent_db.get_usage(self.user['id'], agent_quotas._current_month())
        return (usage.get('tokens_in', 0) or 0) + (usage.get('tokens_out', 0) or 0)

    def test_over_limit_usage_is_still_recorded(self):
        limit = agent_quotas.FREE_TIER_LIMITS['max_tokens']
        # Land one token under the cap, then make a call larger than the rest.
        agent_quotas.record_tokens(self.user['id'], limit - 1, 0)
        self.assertEqual(self._total(), limit - 1)
        agent_quotas.record_tokens(self.user['id'], 50_000, 0)
        self.assertEqual(self._total(), limit - 1 + 50_000,
                         'usage past the cap must still be counted')

    def test_precheck_refuses_once_over_limit(self):
        limit = agent_quotas.FREE_TIER_LIMITS['max_tokens']
        agent_quotas.record_tokens(self.user['id'], limit + 1, 0)
        with self.assertRaises(agent_quotas.QuotaError):
            agent_quotas.check_token_budget(self.user['id'])


class StorageStockTests(unittest.TestCase):
    """Storage is live disk stock, not a monthly counter (user report
    2026-10-01: a monthly counter resets while files persist, letting a
    free user accumulate +limit every month)."""

    def setUp(self):
        agent_quotas._STOCK_CACHE.clear()
        self._auth = patch.dict(os.environ, {'AINGEL_AUTH': 'oidc'})
        self._auth.start()
        self._user = {'id': 42, 'plan': 'free', 'role': 'user'}
        self._users = patch.object(agent_quotas.db, 'get_user',
                                   return_value=dict(self._user))
        self._users.start()

    def tearDown(self):
        self._users.stop()
        self._auth.stop()
        agent_quotas._STOCK_CACHE.clear()

    def _stock(self, value):
        return patch.object(agent_quotas, 'storage_stock_bytes',
                            return_value=value)

    def test_check_storage_blocks_when_stock_plus_incoming_exceeds(self):
        limit = agent_quotas.FREE_TIER_LIMITS['max_storage']
        with self._stock(limit - 1):
            with self.assertRaises(agent_quotas.QuotaError) as ctx:
                agent_quotas.check_storage(42, 2)
        self.assertEqual(ctx.exception.field, 'storage_bytes')
        self.assertEqual(ctx.exception.limit, limit)

    def test_check_storage_allows_up_to_limit_and_never_increments(self):
        limit = agent_quotas.FREE_TIER_LIMITS['max_storage']
        with self._stock(limit - 10):
            agent_quotas.check_storage(42, 10)  # must not raise
        with patch.object(agent_db, 'check_and_increment_usage') as inc:
            with self._stock(0):
                agent_quotas.check_storage(42, 5)
            inc.assert_not_called()  # storage must be measured, never incremented

    def test_get_usage_reports_stock_not_counter(self):
        counter = {'runs': 3, 'tokens_in': 100, 'tokens_out': 5,
                   'storage_bytes': 999_999}
        with patch.object(agent_db, 'get_usage', return_value=dict(counter)), \
                self._stock(12_345):
            usage = agent_quotas.get_usage(42)
        self.assertEqual(usage['storage_bytes'], 12_345)
        self.assertEqual(usage['runs'], 3)

    def test_stock_walk_sums_owned_project_files(self):
        import tempfile as _tf
        import time as _time
        with _tf.TemporaryDirectory() as td:
            proj = os.path.join(td, 'proj')
            os.makedirs(proj)
            with open(os.path.join(proj, 'a.bin'), 'wb') as f:
                f.write(b'x' * 1000)
            sub = os.path.join(proj, 'sub')
            os.makedirs(sub)
            with open(os.path.join(sub, 'b.bin'), 'wb') as f:
                f.write(b'y' * 500)
            other = os.path.join(td, 'other')  # not owned — must be ignored
            os.makedirs(other)
            with open(os.path.join(other, 'c.bin'), 'wb') as f:
                f.write(b'z' * 7000)
            # Service dirs are transient (upload staging, soft-delete trash):
            # they must not count — the Files UI can't even show them.
            for svc in ('.uploads', '.trash'):
                svcdir = os.path.join(proj, svc)
                os.makedirs(svcdir)
                with open(os.path.join(svcdir, 'chunk.bin'), 'wb') as f:
                    f.write(b's' * 9000)
            projects = [{'owner_id': 42, 'path': proj},
                        {'owner_id': 7, 'path': other}]
            with patch.object(agent_db, 'get_projects', return_value=projects):
                agent_quotas._STOCK_CACHE.clear()
                self.assertEqual(agent_quotas.storage_stock_bytes(42), 1500)
                # cache hit path returns the same value without a re-walk
                with patch.object(agent_db, 'get_projects', side_effect=AssertionError):
                    self.assertEqual(agent_quotas.storage_stock_bytes(42), 1500)
                # and the TTL expiry recomputes
                agent_quotas._STOCK_CACHE[42] = (_time.time() - 999, 0)
                self.assertEqual(agent_quotas.storage_stock_bytes(42), 1500)


class ModelWhitelistPreFlightTests(unittest.TestCase):
    """_kick_run_seq's model pre-flight refuses free-tier disallowed models
    before any exec row/counter exists (task #10001176: Claude on free ran,
    failed silently, consumed a run)."""

    def setUp(self):
        self._auth = patch.dict(os.environ, {'AINGEL_AUTH': 'oidc'})
        self._auth.start()

    def tearDown(self):
        self._auth.stop()

    def test_preflight_blocks_disallowed_model_for_free_owner(self):
        import agent_api
        task = {'id': 1, 'model': 'claude-sonnet-4-6', 'created_by': 42,
                'project_id': 5}
        proj = {'id': 5, 'owner_id': 42}
        with patch.object(agent_api.db, 'get_task', return_value=dict(task)), \
                patch.object(agent_api.db, 'get_project', return_value=dict(proj)), \
                patch.object(agent_quotas, 'quotas_applicable', return_value=True), \
                patch.object(agent_quotas, 'is_allowed_model', return_value=False):
            err = agent_api._preflight_model_allowed([1])
        self.assertIsNotNone(err)
        self.assertIn('not available on the free tier', str(err))
        self.assertIn('claude-sonnet-4-6', str(err))

    def test_preflight_allows_free_models_and_paid_owners(self):
        import agent_api
        task = {'id': 1, 'model': 'mistral-small-latest', 'created_by': 42,
                'project_id': 5}
        proj = {'id': 5, 'owner_id': 42}
        with patch.object(agent_api.db, 'get_task', return_value=dict(task)), \
                patch.object(agent_api.db, 'get_project', return_value=dict(proj)), \
                patch.object(agent_quotas, 'quotas_applicable', return_value=True), \
                patch.object(agent_quotas, 'is_allowed_model', return_value=True):
            self.assertIsNone(agent_api._preflight_model_allowed([1]))
        # paid owner: quotas not applicable at all
        with patch.object(agent_api.db, 'get_task', return_value=dict(task)), \
                patch.object(agent_api.db, 'get_project', return_value=dict(proj)), \
                patch.object(agent_quotas, 'quotas_applicable', return_value=False):
            self.assertIsNone(agent_api._preflight_model_allowed([1]))


if __name__ == '__main__':
    unittest.main()
