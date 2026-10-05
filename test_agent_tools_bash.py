"""agent_tools.bash: child processes must not outlive the tool call (C6)."""
import os
import tempfile
import time
import unittest
from unittest import mock

import agent_tools


class BashProcessGroupTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)

    def test_background_job_killed_after_normal_exit(self):
        res = agent_tools.bash(self.td.name, '(sleep 2; touch survived) >/dev/null 2>&1 &', timeout=10)
        self.assertEqual(res.get('returncode'), 0)
        time.sleep(3)
        self.assertFalse(os.path.exists(os.path.join(self.td.name, 'survived')))

    def test_background_job_killed_after_timeout(self):
        res = agent_tools.bash(self.td.name, '(sleep 3; touch survived) & sleep 30', timeout=1)
        self.assertIn('timed out', res.get('error', ''))
        time.sleep(4)
        self.assertFalse(os.path.exists(os.path.join(self.td.name, 'survived')))

    def test_vault_trash_reference_is_blocked(self):
        res = agent_tools.bash(self.td.name, 'ls /var/lib/vault-trash/5/files')
        self.assertIn('blocked', res.get('error', ''))

    def test_configured_trash_root_is_blocked(self):
        with mock.patch.object(agent_tools.agent_config, 'TRASH_ROOT', '/opt/cordee/trash'):
            res = agent_tools.bash(self.td.name, 'ls /opt/cordee/trash/5/files')
        self.assertIn('blocked', res.get('error', ''))


if __name__ == '__main__':
    unittest.main()
