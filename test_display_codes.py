"""Tests for human-readable task display codes ("RP-014").

Covers the additive display fields: projects.short_code (central DB), tasks
display_no/display_code (per-project DB), the generator, allocation at
creation (create_task / upsert_task / copy_task), execution rows
(exec_no + task_display_code), the resolve_task_code round-trip, the project
PATCH rename, and the ops/backfill_display_numbers.py script.

Internal integer ids (keys, FKs, URLs, filenames, branches) are unchanged —
the display fields ride alongside them.

Isolation style follows test_agent_archive.py: fresh temp dir per test, the
central DB path patched to <tmp>/aingel.db, PROJECTS_ROOT patched to
<tmp>/projects (both the agent_db import and the live agent_config value),
and the per-process project-DB schema cache cleared between tests.

Run:  venv/bin/python3 -m unittest test_display_codes.py -v
"""
import importlib.util
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import agent_config
import agent_db


def _load_backfill():
    """Import ops/backfill_display_numbers.py (not a package) by file path."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'ops', 'backfill_display_numbers.py')
    spec = importlib.util.spec_from_file_location('backfill_display_numbers', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _DisplayCodeTestCase(unittest.TestCase):
    """Base fixture: temp central DB + temp PROJECTS_ROOT + helpers."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tmp = self.td.name
        self.central = os.path.join(self.tmp, 'aingel.db')
        self.root = os.path.join(self.tmp, 'projects')
        os.makedirs(self.root, exist_ok=True)
        self._patches = [
            patch.object(agent_db, 'DB_PATH', self.central),
            patch.object(agent_db, 'PROJECTS_ROOT', self.root),
            patch.object(agent_config, 'PROJECTS_ROOT', self.root),
        ]
        for p in self._patches:
            p.start()
        agent_db.init_db()

    def tearDown(self):
        with agent_db._PROJECT_DB_READY_LOCK:
            agent_db._PROJECT_DB_READY.clear()
        for p in reversed(self._patches):
            p.stop()
        self.td.cleanup()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _make_project(self, name):
        path = os.path.join(self.root, name)
        os.makedirs(path, exist_ok=True)
        slug = name.lower().replace(' ', '-')
        return agent_db.upsert_project(name, slug, path), path

    def _insert_legacy_project(self, name):
        """A projects row WITHOUT short_code (pre-feature shape)."""
        path = os.path.join(self.root, name)
        os.makedirs(path, exist_ok=True)
        conn = agent_db.get_db()
        conn.execute('INSERT INTO projects (name, slug, path) VALUES (?,?,?)',
                     (name, name.lower().replace(' ', '-'), path))
        conn.commit()
        conn.close()
        return path

    def _insert_legacy_task(self, path, pid, title, created_at=None, task_id=None):
        """A task row WITHOUT display fields (pre-feature shape), registry row
        included so _get_task_path() resolves it."""
        conn = agent_db.get_db()
        conn.execute('INSERT INTO task_registry (project_id, project_path) VALUES (?,?)',
                     (pid, path))
        conn.commit()
        tid = task_id or conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.close()
        pconn = agent_db.get_project_db(path)
        pconn.execute(
            'INSERT INTO tasks (id, project_id, title, status, created_at) VALUES (?,?,?,?,?)',
            (tid, pid, title, 'done', created_at))
        pconn.commit()
        pconn.close()
        return tid


class ShortCodeGeneratorTests(unittest.TestCase):

    def test_basic_initials(self):
        self.assertEqual(agent_db._short_code_base('RAG Pipeline'), 'RP')
        self.assertEqual(agent_db._short_code_base('Zeta Ops'), 'ZO')

    def test_non_alphanumeric_splits_words(self):
        # Only A-Z0-9: the accented 'é' is a separator, not a letter.
        self.assertEqual(agent_db._short_code_base('Cordée Vault'), 'CEV')

    def test_stopwords_are_skipped(self):
        # House, of, the, Rising, Sun → H, R, S
        self.assertEqual(agent_db._short_code_base('House of the Rising Sun'), 'HRS')

    def test_max_four_chars(self):
        self.assertEqual(agent_db._short_code_base('Alpha Beta Gamma Delta Epsilon'), 'ABGD')

    def test_min_two_chars_pads_from_first_word(self):
        # Single significant word: pad with the word's own letters.
        self.assertEqual(agent_db._short_code_base('Superagent'), 'SU')
        self.assertEqual(agent_db._short_code_base('OCR'), 'OC')

    def test_first_char_is_always_a_letter(self):
        # '3D Printing' cannot lead with a digit: resolve/PATCH validate
        # ^[A-Z][A-Z0-9]{1,5}$, so a generated code must satisfy it too.
        code = agent_db._short_code_base('3D Printing')
        self.assertTrue(agent_db._SHORT_CODE_RE.match(code), code)

    def test_collision_appends_incrementing_number(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(agent_db, 'DB_PATH', os.path.join(td, 'aingel.db')):
                agent_db.init_db()
                conn = agent_db.get_db()
                try:
                    conn.execute(
                        'INSERT INTO projects (name, slug, path, short_code) '
                        "VALUES ('RAG Pipeline', 'rag-pipeline', '/x', 'RP')")
                    conn.commit()
                    self.assertEqual(agent_db._generate_short_code('RAG Pipeline', conn), 'RP2')
                    self.assertEqual(
                        agent_db._generate_short_code('RAG Pipeline', conn,
                                                      extra_taken=('RP2',)), 'RP3')
                finally:
                    conn.close()


class ProjectShortCodeTests(_DisplayCodeTestCase):

    def test_upsert_project_assigns_code_on_create(self):
        project, _ = self._make_project('RAG Pipeline')
        self.assertEqual(project['short_code'], 'RP')

    def test_upsert_project_never_overwrites_existing_code(self):
        project, path = self._make_project('RAG Pipeline')
        pid = project['id']
        agent_db.set_project_short_code(pid, 'ZZ')
        # Re-upsert the same slug (importer / apply-guide path).
        again = agent_db.upsert_project('RAG Pipeline', 'rag-pipeline', path)
        self.assertEqual(again['short_code'], 'ZZ')

    def test_collision_suffix_on_create(self):
        first, _ = self._make_project('RAG Pipeline')      # RP
        second, _ = self._make_project('RAG Processor')   # RP → taken → RP2
        self.assertEqual(first['short_code'], 'RP')
        self.assertEqual(second['short_code'], 'RP2')

    def test_upsert_project_retries_when_short_code_races(self):
        first, _ = self._make_project('RAG Pipeline')      # RP
        real_gen = agent_db._generate_short_code
        calls = {'n': 0}

        def racing_gen(name, conn, extra_taken=()):
            # First generation "loses" a race: both concurrent creates
            # generated 'RP' and the other committed first, so our INSERT
            # hits the unique idx_projects_short_code and must retry.
            if calls['n'] == 0:
                calls['n'] += 1
                return 'RP'
            return real_gen(name, conn, extra_taken)

        with patch.object(agent_db, '_generate_short_code', racing_gen):
            second = agent_db.upsert_project(
                'RAG Processor', 'rag-processor',
                os.path.join(self.root, 'RAG Processor'))
        self.assertEqual(second['short_code'], 'RP2')

    def test_short_codes_surface_in_project_payloads(self):
        project, path = self._make_project('RAG Pipeline')
        agent_db.create_task(project['id'], 'A task')  # materializes project.db
        projects = agent_db.get_projects()
        self.assertEqual(projects[0]['short_code'], 'RP')
        self.assertEqual(agent_db.get_dashboard_stats()['projects'][0]['short_code'], 'RP')


class TaskAllocationTests(_DisplayCodeTestCase):

    def test_create_task_assigns_sequential_numbers(self):
        project, path = self._make_project('RAG Pipeline')
        pid = project['id']
        t1 = agent_db.create_task(pid, 'First')
        t2 = agent_db.create_task(pid, 'Second')
        d1, d2 = agent_db.get_task(t1), agent_db.get_task(t2)
        self.assertEqual((d1['display_no'], d1['display_code']), (1, 'RP-001'))
        self.assertEqual((d2['display_no'], d2['display_code']), (2, 'RP-002'))

    def test_counters_are_independent_across_projects(self):
        pa, path_a = self._make_project('RAG Pipeline')      # RP
        pb, path_b = self._make_project('Widget Factory')    # WF
        agent_db.create_task(pa['id'], 'A1')
        b1 = agent_db.create_task(pb['id'], 'B1')
        b2 = agent_db.create_task(pb['id'], 'B2')
        self.assertEqual(agent_db.get_task(b1)['display_code'], 'WF-001')
        self.assertEqual(agent_db.get_task(b2)['display_code'], 'WF-002')

    def test_display_code_format(self):
        import re
        project, _ = self._make_project('RAG Pipeline')
        tid = agent_db.create_task(project['id'], 'Formatted')
        t = agent_db.get_task(tid)
        self.assertRegex(t['display_code'], r'^[A-Z][A-Z0-9]{1,5}-\d{3}$')
        self.assertEqual(t['display_code'], f"{project['short_code']}-001")
        # Integer identity untouched: registry row + tasks.id still the key.
        conn = agent_db.get_db()
        reg = conn.execute('SELECT project_path FROM task_registry WHERE id=?',
                           (tid,)).fetchone()
        conn.close()
        self.assertIsNotNone(reg)

    def test_copy_task_gets_the_next_number(self):
        project, _ = self._make_project('RAG Pipeline')
        pid = project['id']
        agent_db.create_task(pid, 'Original')
        src = agent_db.create_task(pid, 'Second')
        clone = agent_db.copy_task(src)
        self.assertEqual(clone['display_no'], 3)
        self.assertEqual(clone['display_code'], 'RP-003')
        self.assertEqual(clone['title'], 'Second (copy)')

    def test_upsert_task_create_branch_gets_a_number(self):
        project, path = self._make_project('RAG Pipeline')
        pid = project['id']
        tid = agent_db.upsert_task(pid, 'task-1', 'Imported', project_path=path)
        self.assertIsNotNone(tid)
        t = agent_db.get_task(tid)
        self.assertEqual((t['display_no'], t['display_code']), (1, 'RP-001'))
        # Upserting the SAME external_id updates in place, no new number.
        self.assertIsNone(agent_db.upsert_task(pid, 'task-1', 'Imported again',
                                               status='done', project_path=path))
        again = [t for t in agent_db.get_tasks(project_id=pid)
                 if t['external_id'] == 'task-1']
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]['display_no'], 1)

    def test_display_no_is_unique_per_project(self):
        project, path = self._make_project('RAG Pipeline')
        pid = project['id']
        agent_db.create_task(pid, 'First')  # RP-001, materializes project.db
        pconn = agent_db.get_project_db(path)
        try:
            names = {r['name'] for r in pconn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
            # The unique replacement exists; the old non-unique index is gone.
            self.assertIn('idx_tasks_display_unique', names)
            self.assertNotIn('idx_tasks_display', names)
            # The constraint is real: a duplicate (project_id, display_no) is refused.
            with self.assertRaises(sqlite3.IntegrityError):
                pconn.execute(
                    'INSERT INTO tasks (project_id, title, display_no) VALUES (?,?,?)',
                    (pid, 'Duplicate', 1))
        finally:
            pconn.close()

    def test_create_task_retries_when_display_no_races(self):
        project, path = self._make_project('RAG Pipeline')
        pid = project['id']
        agent_db.create_task(pid, 'First')  # RP-001
        real_next = agent_db._next_display_no
        calls = {'n': 0}

        def racing_next(pconn, project_id, short_code):
            # First allocation "loses" a race: it returns the number the
            # existing task already claimed, like a concurrent create that
            # committed between our MAX+1 read and our INSERT.
            if calls['n'] == 0:
                calls['n'] += 1
                return 1, 'RP-001'
            return real_next(pconn, project_id, short_code)

        with patch.object(agent_db, '_next_display_no', racing_next):
            tid = agent_db.create_task(pid, 'Racer')
        t = agent_db.get_task(tid)
        # The retry took the next free number; no IntegrityError leaked.
        self.assertEqual((t['display_no'], t['display_code']), (2, 'RP-002'))

    def test_upsert_task_retries_when_display_no_races(self):
        project, path = self._make_project('RAG Pipeline')
        pid = project['id']
        agent_db.create_task(pid, 'First')  # RP-001
        real_next = agent_db._next_display_no
        calls = {'n': 0}

        def racing_next(pconn, project_id, short_code):
            if calls['n'] == 0:
                calls['n'] += 1
                return 1, 'RP-001'
            return real_next(pconn, project_id, short_code)

        with patch.object(agent_db, '_next_display_no', racing_next):
            tid = agent_db.upsert_task(pid, 'ext-1', 'Imported',
                                       project_path=path)
        t = agent_db.get_task(tid)
        self.assertEqual((t['display_no'], t['display_code']), (2, 'RP-002'))

    def test_attachable_label_uses_display_code(self):
        project, path = self._make_project('RAG Pipeline')
        pid = project['id']
        tid = agent_db.create_task(pid, 'Attachable')
        agent_db.update_task(tid, project_path=path, status='done')
        # Attachable requires a done execution with an output summary.
        eid = agent_db.create_execution(tid, 'claude-sonnet-5-5', project_path=path)
        agent_db.finish_execution(eid, 'done', project_path=path,
                                  output_summary='the result')
        attachable = agent_db.get_attachable_tasks(pid, path)
        self.assertTrue(attachable)
        entry = next(a for a in attachable if a['id'] == tid)
        self.assertEqual(entry['label'], 'RP-001 Attachable')
        self.assertEqual(entry['ref'], tid)

        # Pre-feature rows without a display code keep the '#<id>' label.
        legacy_id = self._insert_legacy_task(path, pid, 'Legacy attachable')
        leid = agent_db.create_execution(legacy_id, 'claude-sonnet-5-5',
                                         project_path=path)
        agent_db.finish_execution(leid, 'done', project_path=path,
                                 output_summary='legacy result')
        legacy_entry = next(a for a in agent_db.get_attachable_tasks(pid, path)
                            if a['id'] == legacy_id)
        self.assertEqual(legacy_entry['label'], f'#{legacy_id} Legacy attachable')


class ExecutionDisplayTests(_DisplayCodeTestCase):

    def test_get_executions_returns_exec_no_and_task_display_code(self):
        project, path = self._make_project('RAG Pipeline')
        pid = project['id']
        t1 = agent_db.create_task(pid, 'With runs')
        t2 = agent_db.create_task(pid, 'Other')
        e1 = agent_db.create_execution(t1, 'claude-sonnet-5-5', project_path=path)
        e2 = agent_db.create_execution(t1, 'claude-sonnet-5-5', project_path=path)
        e3 = agent_db.create_execution(t2, 'claude-sonnet-5-5', project_path=path)
        agent_db.finish_execution(e1, 'done', project_path=path)
        agent_db.finish_execution(e2, 'done', project_path=path)
        agent_db.finish_execution(e3, 'done', project_path=path)

        execs = agent_db.get_executions(limit=50)
        by_id = {e['id']: e for e in execs}
        self.assertEqual(by_id[e1]['exec_no'], 1)
        self.assertEqual(by_id[e2]['exec_no'], 2)
        self.assertEqual(by_id[e3]['exec_no'], 1)
        self.assertEqual(by_id[e1]['task_display_code'], 'RP-001')
        self.assertEqual(by_id[e3]['task_display_code'], 'RP-002')
        # Chat executions are a separate listing; task runs never set chat_id.
        for e in execs:
            self.assertIsNone(e['chat_id'])


class ResolveTests(_DisplayCodeTestCase):

    def test_resolve_round_trip(self):
        project, path = self._make_project('RAG Pipeline')
        pid = project['id']
        t1 = agent_db.create_task(pid, 'First')
        t2 = agent_db.create_task(pid, 'Second')

        hit = agent_db.resolve_task_code('RP-002')
        self.assertEqual(hit['task_id'], t2)
        self.assertEqual(hit['project_id'], pid)
        self.assertEqual(hit['project_path'], path)
        # Lowercase input resolves too.
        self.assertEqual(agent_db.resolve_task_code('rp-001')['task_id'], t1)

    def test_resolve_unknown_code_is_none(self):
        self._make_project('RAG Pipeline')
        self.assertIsNone(agent_db.resolve_task_code('RP-999'))
        self.assertIsNone(agent_db.resolve_task_code('ZZ-001'))
        for bad in ('', None, 'RP', 'RP-', '-001', '001', 'RP-abc', 'R-01',
                    'RAG-PIPE-001', 'RP-0x1'):
            self.assertIsNone(agent_db.resolve_task_code(bad), bad)


class PatchShortCodeTests(_DisplayCodeTestCase):

    def setUp(self):
        super().setUp()
        self._saved_auth = os.environ.get('AINGEL_AUTH')
        os.environ['AINGEL_AUTH'] = 'off'
        import agent_api
        self.agent_api = agent_api
        agent_api.app.config['TESTING'] = True
        self.client = agent_api.app.test_client()

    def tearDown(self):
        if self._saved_auth is None:
            os.environ.pop('AINGEL_AUTH', None)
        else:
            os.environ['AINGEL_AUTH'] = self._saved_auth
        super().tearDown()

    def test_patch_renames_prefix_across_tasks(self):
        project, path = self._make_project('RAG Pipeline')
        pid = project['id']
        t1 = agent_db.create_task(pid, 'First')
        t2 = agent_db.create_task(pid, 'Second')

        resp = self.client.patch(f'/api/projects/{pid}',
                                 json={'short_code': 'NEW'})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(agent_db.get_project(pid)['short_code'], 'NEW')
        self.assertEqual(agent_db.get_task(t1)['display_no'], 1)
        self.assertEqual(agent_db.get_task(t1)['display_code'], 'NEW-001')
        self.assertEqual(agent_db.get_task(t2)['display_code'], 'NEW-002')
        # Resolver follows the rename.
        self.assertEqual(agent_db.resolve_task_code('NEW-002')['task_id'], t2)
        self.assertIsNone(agent_db.resolve_task_code('RP-002'))

    def test_patch_collision_is_409(self):
        pa, _ = self._make_project('RAG Pipeline')     # RP
        pb, _ = self._make_project('Widget Factory')  # WF
        resp = self.client.patch(f"/api/projects/{pb['id']}",
                                 json={'short_code': 'RP'})
        self.assertEqual(resp.status_code, 409)
        # Unchanged on failure.
        self.assertEqual(agent_db.get_project(pb['id'])['short_code'], 'WF')

    def test_patch_invalid_format_is_400(self):
        project, _ = self._make_project('RAG Pipeline')
        pid = project['id']
        for bad in ('', 'x', '1X', 'TOOLONGPREFIX', 'R-P'):
            resp = self.client.patch(f'/api/projects/{pid}',
                                     json={'short_code': bad})
            self.assertEqual(resp.status_code, 400, bad)
        self.assertEqual(agent_db.get_project(pid)['short_code'], 'RP')

    def test_resolve_endpoint(self):
        project, path = self._make_project('RAG Pipeline')
        pid = project['id']
        t1 = agent_db.create_task(pid, 'First')
        resp = self.client.get('/api/tasks/resolve?code=RP-001')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json(), {'task_id': t1, 'project_id': pid})

        self.assertEqual(self.client.get('/api/tasks/resolve?code=RP-009').status_code, 404)
        self.assertEqual(self.client.get('/api/tasks/resolve?code=').status_code, 400)
        self.assertEqual(self.client.get('/api/tasks/resolve').status_code, 400)


class ApiPayloadTests(_DisplayCodeTestCase):
    """Flask-level payload contracts: single-exec GET, the archive/delete 409
    block lists, and the memory fork response."""

    def setUp(self):
        super().setUp()
        self._saved_auth = os.environ.get('AINGEL_AUTH')
        os.environ['AINGEL_AUTH'] = 'off'
        import agent_api
        self.agent_api = agent_api
        agent_api.app.config['TESTING'] = True
        self.client = agent_api.app.test_client()

    def tearDown(self):
        if self._saved_auth is None:
            os.environ.pop('AINGEL_AUTH', None)
        else:
            os.environ['AINGEL_AUTH'] = self._saved_auth
        super().tearDown()

    def test_single_execution_get_returns_display_fields(self):
        project, path = self._make_project('RAG Pipeline')
        pid = project['id']
        t1 = agent_db.create_task(pid, 'With runs')
        e1 = agent_db.create_execution(t1, 'claude-sonnet-5-5', project_path=path)
        e2 = agent_db.create_execution(t1, 'claude-sonnet-5-5', project_path=path)

        resp = self.client.get(f'/api/executions/{e2}')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertEqual(body['task_display_code'], 'RP-001')
        self.assertEqual(body['exec_no'], 2)

        first = self.client.get(f'/api/executions/{e1}').get_json()
        self.assertEqual(first['task_display_code'], 'RP-001')
        self.assertEqual(first['exec_no'], 1)

    def test_delete_409_running_block_carries_display_fields(self):
        project, path = self._make_project('RAG Pipeline')
        pid = project['id']
        t1 = agent_db.create_task(pid, 'Live')
        agent_db.create_task(pid, 'Idle')
        e1 = agent_db.create_execution(t1, 'claude-sonnet-5-5', project_path=path)
        e2 = agent_db.create_execution(t1, 'claude-sonnet-5-5', project_path=path)
        agent_db.finish_execution(e1, 'done', project_path=path)  # e2 stays running

        resp = self.client.delete(f'/api/projects/{pid}')
        self.assertEqual(resp.status_code, 409)
        running = resp.get_json()['running']
        self.assertEqual(len(running), 1)
        entry = running[0]
        self.assertEqual(entry['id'], e2)
        self.assertEqual(entry['task_display_code'], 'RP-001')
        # exec_no matches the execution log's numbering (position among ALL of
        # the task's runs by id), not the filtered running set.
        self.assertEqual(entry['exec_no'], 2)

    def test_delete_409_confirmed_block_carries_display_fields(self):
        project, path = self._make_project('RAG Pipeline')
        pid = project['id']
        t1 = agent_db.create_task(pid, 'Queued')
        agent_db.update_task(t1, project_path=path, status='confirmed')

        resp = self.client.delete(f'/api/projects/{pid}')
        self.assertEqual(resp.status_code, 409)
        confirmed = resp.get_json()['confirmed']
        self.assertEqual(len(confirmed), 1)
        entry = confirmed[0]
        self.assertEqual(entry['id'], t1)
        self.assertEqual(entry['display_code'], 'RP-001')
        self.assertEqual(entry['task_display_code'], 'RP-001')

    def test_archive_409_block_carries_display_fields(self):
        project, path = self._make_project('RAG Pipeline')
        pid = project['id']
        t1 = agent_db.create_task(pid, 'Live')
        agent_db.create_execution(t1, 'claude-sonnet-5-5', project_path=path)

        resp = self.client.post(f'/api/projects/{pid}/archive')
        self.assertEqual(resp.status_code, 409)
        running = resp.get_json()['running']
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0]['task_display_code'], 'RP-001')
        self.assertEqual(running[0]['exec_no'], 1)

    def test_fork_returns_new_task_display_code(self):
        project, path = self._make_project('RAG Pipeline')
        pid = project['id']
        t1 = agent_db.create_task(pid, 'Source')  # RP-001
        e1 = agent_db.create_execution(t1, 'claude-sonnet-5-5', project_path=path)

        resp = self.client.post('/api/memory/fork', json={'exec_id': e1})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        # The fork is a fresh create_task → next number in the project.
        self.assertEqual(body['new_task_display_code'], 'RP-002')
        self.assertEqual(
            agent_db.get_task(body['new_task_id'])['display_code'], 'RP-002')


class BackfillTests(_DisplayCodeTestCase):

    def setUp(self):
        super().setUp()
        self.backfill = _load_backfill()

    def test_backfill_assigns_short_codes_and_numbers_by_created_at(self):
        path_a = self._insert_legacy_project('Zeta Ops')          # → ZO
        conn = agent_db.get_db()
        pid_a = conn.execute("SELECT id FROM projects WHERE slug='zeta-ops'").fetchone()[0]
        conn.close()
        # Insert out of created_at order: insertion order ≠ creation order.
        self._insert_legacy_task(path_a, pid_a, 'Later task', created_at='2026-02-02T10:00:00')
        self._insert_legacy_task(path_a, pid_a, 'Earlier task', created_at='2026-01-01T09:00:00')

        dry = self.backfill.run(apply=False)
        self.assertEqual(dry[0]['short_code'], 'ZO')
        self.assertEqual(dry[0]['numbered'], 2)
        # Dry run wrote nothing.
        conn = agent_db.get_db()
        self.assertIsNone(conn.execute(
            "SELECT short_code FROM projects WHERE slug='zeta-ops'").fetchone()[0])
        conn.close()

        applied = self.backfill.run(apply=True)
        self.assertEqual(applied[0]['short_code'], 'ZO')
        tasks = agent_db.get_tasks(project_id=pid_a)
        by_title = {t['title']: t for t in tasks}
        self.assertEqual(by_title['Earlier task']['display_code'], 'ZO-001')
        self.assertEqual(by_title['Later task']['display_code'], 'ZO-002')

        # Idempotent: a second run numbers nothing and keeps the codes stable.
        rerun = self.backfill.run(apply=True)
        self.assertEqual(rerun[0]['numbered'], 0)
        self.assertEqual(rerun[0]['short_code'], 'ZO')
        self.assertEqual(by_title, {t['title']: t for t in agent_db.get_tasks(project_id=pid_a)})

    def test_backfill_short_code_collision_gets_suffix(self):
        self._make_project('RAG Pipeline')                       # RP (upsert)
        self._insert_legacy_project('RAG Processor')             # RP taken → RP2
        summaries = self.backfill.run(apply=True)
        codes = {s['name']: s['short_code'] for s in summaries}
        self.assertEqual(codes['RAG Pipeline'], 'RP')
        self.assertEqual(codes['RAG Processor'], 'RP2')

    def test_backfill_continues_after_existing_numbers(self):
        project, path = self._make_project('RAG Pipeline')  # RP, gets no tasks yet
        pid = project['id']
        t1 = agent_db.create_task(pid, 'Created now')       # RP-001
        self._insert_legacy_task(path, pid, 'Legacy row', created_at='2025-01-01T00:00:00')
        summaries = self.backfill.run(apply=True)
        self.assertEqual(summaries[0]['numbered'], 1)
        legacy = next(t for t in agent_db.get_tasks(project_id=pid)
                      if t['title'] == 'Legacy row')
        self.assertEqual(legacy['display_code'], 'RP-002')
        # The existing number is never reassigned.
        self.assertEqual(agent_db.get_task(t1)['display_code'], 'RP-001')


class RestoreShortCodeTests(_DisplayCodeTestCase):

    def test_restore_reassigns_short_code_on_collision(self):
        exported, path = self._make_project('RAG Pipeline')
        pid = exported['id']
        self.assertEqual(exported['short_code'], 'RP')
        tid = agent_db.create_task(pid, 'Coded task')  # RP-001, materializes project.db
        self.assertEqual(agent_db.get_task(tid)['display_code'], 'RP-001')

        manifest = {'project': dict(exported)}
        # Another project claims 'RP' while the first is being restored.
        agent_db.delete_project(pid)
        self._make_project('RAG Processor')  # RP
        restored = agent_db.restore_project_central_rows(manifest, path)
        self.assertEqual(restored['short_code'], 'RP2')
        # The restored tasks follow the re-assigned prefix: RP-001 → RP2-001.
        # (The db-level delete dropped the registry rows, so resolve by path.)
        self.assertEqual(agent_db.get_task(tid, path)['display_code'], 'RP2-001')

    def test_restore_keeps_unique_short_code_without_collision(self):
        project, path = self._make_project('RAG Pipeline')
        pid = project['id']
        tid = agent_db.create_task(pid, 'Coded task')  # RP-001
        manifest = {'project': dict(project)}
        agent_db.delete_project(pid)
        restored = agent_db.restore_project_central_rows(manifest, path)
        self.assertEqual(restored['short_code'], 'RP')
        # No collision → no re-prefix: the code keeps its original prefix.
        self.assertEqual(agent_db.get_task(tid, path)['display_code'], 'RP-001')


if __name__ == '__main__':
    unittest.main()
