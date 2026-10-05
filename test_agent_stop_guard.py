import importlib.util
import os
import re
import sqlite3
import tempfile
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_guard():
    spec = importlib.util.spec_from_file_location(
        'stop_guard', os.path.join(ROOT, 'ops', 'stop_guard.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class StopGuardTests(unittest.TestCase):
    """ops/stop_guard.py never ran while it was wired as ExecStopPre= (not a
    systemd directive). These cover the script and the unit wiring."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.proj = os.path.join(self.td.name, 'proj')
        os.makedirs(self.proj)
        self.central = os.path.join(self.td.name, 'aingel.db')
        c = sqlite3.connect(self.central)
        c.execute('CREATE TABLE projects (path TEXT)')
        c.execute('INSERT INTO projects VALUES (?)', (self.proj,))
        c.execute('INSERT INTO projects VALUES (NULL)')
        c.commit()
        c.close()
        p = sqlite3.connect(os.path.join(self.proj, 'project.db'))
        p.execute('CREATE TABLE executions (id INTEGER PRIMARY KEY, task_id INTEGER, '
                  'status TEXT, error_message TEXT, finished_at TEXT)')
        p.execute('CREATE TABLE tasks (id INTEGER PRIMARY KEY, status TEXT)')
        p.executemany('INSERT INTO tasks VALUES (?,?)',
                      [(1, 'running'), (2, 'done'), (3, 'running')])
        p.executemany('INSERT INTO executions VALUES (?,?,?,?,?)',
                      [(10, 1, 'running', '', None),
                       (11, 2, 'done', '', 'x'),
                       (12, None, 'running', '', None)])
        p.commit()
        p.close()

    def tearDown(self):
        self.td.cleanup()

    def _rows(self, sql):
        p = sqlite3.connect(os.path.join(self.proj, 'project.db'))
        try:
            return p.execute(sql).fetchall()
        finally:
            p.close()

    def test_fails_running_executions_and_resets_their_tasks(self):
        guard = _load_guard()
        guard.AINGEL_DB = self.central
        with self.assertRaises(SystemExit) as cm:
            guard.main()
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(
            self._rows("SELECT id, status FROM executions ORDER BY id"),
            [(10, 'failed'), (11, 'done'), (12, 'failed')])
        self.assertEqual(
            self._rows("SELECT id, status FROM tasks ORDER BY id"),
            [(1, 'pending'), (2, 'done'), (3, 'running')])
        self.assertIn('ExecStop guard', self._rows(
            "SELECT error_message FROM executions WHERE id=10")[0][0])

    def test_unreadable_central_db_never_blocks_shutdown(self):
        guard = _load_guard()
        guard.AINGEL_DB = os.path.join(self.td.name, 'missing', 'aingel.db')
        with self.assertRaises(SystemExit) as cm:
            guard.main()
        self.assertEqual(cm.exception.code, 0)

    def test_unit_uses_a_real_systemd_stop_directive(self):
        # The shipped template always exists; a host-specific unit may too.
        units = [os.path.join(ROOT, 'ops', 'examples', 'cordee.service'),
                 os.path.join(ROOT, 'ops', 'superagent-vault.service')]
        for path in [u for u in units if os.path.exists(u)] or units[:1]:
            with self.subTest(unit=os.path.relpath(path, ROOT)):
                with open(path) as f:
                    unit = f.read()
                self.assertNotIn('ExecStopPre', unit)
                self.assertRegex(unit, re.compile(r'^ExecStop=.*ops/stop_guard\.py$', re.M))


if __name__ == '__main__':
    unittest.main()
