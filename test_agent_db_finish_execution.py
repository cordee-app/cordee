"""Regression tests for finish_execution's zero-clobber guard.

Production incident (model gemma-4-26b): a Scaleway task run failed
mid-loop after 40 tool turns. Its usage (1,227,345 input / 39,575 output
tokens) was live-tracked in the executions row via the heartbeat, but a
failure handler called finish_execution with default zeros and the blind
UPDATE wiped the row to 0/0/$0.00 — while the provider billed ~$0.35.
Usage columns now use MAX(COALESCE(...), ?) so a zero-usage finish can
never erase nonzero live-tracked usage; higher final values still win.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import agent_db


class FinishExecutionZeroGuardTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self._patch = patch.object(
            agent_db, 'DB_PATH', os.path.join(self.td.name, 'aingel.db'))
        self._patch.start()
        agent_db.init_db()
        self.path = os.path.join(self.td.name, 'proj')
        os.makedirs(self.path, exist_ok=True)
        proj = agent_db.upsert_project('P', 'p', self.path)
        self.task_id = agent_db.create_task(proj['id'], 'T', project_path=self.path)
        self.exec_id = agent_db.create_execution(self.task_id, 'scw-gemma-4-26b',
                                                 project_path=self.path)

    def tearDown(self):
        self._patch.stop()
        self.td.cleanup()

    def _set_cost(self, cost):
        conn = agent_db.get_project_db(self.path)
        conn.execute('UPDATE executions SET cost_usd=? WHERE id=?',
                     (cost, self.exec_id))
        conn.commit()
        conn.close()

    def _row(self):
        return agent_db.get_execution(self.exec_id, project_path=self.path)

    def test_zero_finish_does_not_clobber_live_tracked_usage(self):
        # The incident: heartbeat wrote real usage, the failure handler
        # finished with default zeros — the row must keep the live values.
        agent_db.update_execution_tokens(self.exec_id, 500, 50,
                                         project_path=self.path)
        self._set_cost(0.05)
        agent_db.finish_execution(self.exec_id, 'failed', tokens_input=0,
                                  tokens_output=0, cost_usd=0.0,
                                  error_message='exhausted 40 tool turns',
                                  project_path=self.path)
        row = self._row()
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['tokens_input'], 500)
        self.assertEqual(row['tokens_output'], 50)
        self.assertEqual(row['cost_usd'], 0.05)
        self.assertEqual(row['error_message'], 'exhausted 40 tool turns')

    def test_higher_final_values_win(self):
        # A row with no live-tracked usage takes the finish arguments —
        # MAX must not lock the row at zero forever.
        agent_db.finish_execution(self.exec_id, 'done', tokens_input=800,
                                  tokens_output=80, cost_usd=0.5,
                                  project_path=self.path)
        row = self._row()
        self.assertEqual(row['status'], 'done')
        self.assertEqual(row['tokens_input'], 800)
        self.assertEqual(row['tokens_output'], 80)
        self.assertEqual(row['cost_usd'], 0.5)

    def test_lower_final_values_do_not_shrink_live_usage(self):
        # An exception path that only saw part of the run (e.g. a retried
        # call's usage) must not shrink the heartbeat's live totals.
        agent_db.update_execution_tokens(self.exec_id, 1000, 100,
                                         project_path=self.path)
        agent_db.finish_execution(self.exec_id, 'failed', tokens_input=400,
                                  tokens_output=40, cost_usd=0.01,
                                  error_message='boom', project_path=self.path)
        row = self._row()
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['tokens_input'], 1000)
        self.assertEqual(row['tokens_output'], 100)
        self.assertEqual(row['cost_usd'], 0.01)


if __name__ == '__main__':
    unittest.main()
