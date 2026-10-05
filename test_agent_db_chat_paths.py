"""Regression tests for stale chat file_path re-anchoring.

chats.file_path is an absolute path written once at creation. Projects moved
or imported from elsewhere (laptop, old storage root) kept paths that no
longer exist, which silently emptied transcripts and chat search.
"""
import os
import tempfile
import unittest

import agent_db


class ResolveChatFilePathTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.project = os.path.join(self.td.name, 'Proj')
        self.chats_dir = os.path.join(self.project, 'Artifacts', 'chats')
        os.makedirs(self.chats_dir)
        self.name = 'project-aingel-7.chat.md'
        self.real = os.path.join(self.chats_dir, self.name)
        with open(self.real, 'w') as f:
            f.write('# Chat\n')

    def tearDown(self):
        self.td.cleanup()

    def test_stale_path_reanchored_to_project_chats_dir(self):
        chat = {'file_path': f'/home/someone/Projects/Proj/Artifacts/chats/{self.name}'}
        agent_db._resolve_chat_file_path(chat, self.project)
        self.assertEqual(chat['file_path'], self.real)

    def test_existing_path_untouched(self):
        chat = {'file_path': self.real}
        agent_db._resolve_chat_file_path(chat, '/elsewhere')
        self.assertEqual(chat['file_path'], self.real)

    def test_missing_everywhere_left_as_is(self):
        stale = '/old/root/Artifacts/chats/other-9.chat.md'
        chat = {'file_path': stale}
        agent_db._resolve_chat_file_path(chat, self.project)
        self.assertEqual(chat['file_path'], stale)

    def test_empty_path_and_project_are_noops(self):
        chat = {'file_path': None}
        agent_db._resolve_chat_file_path(chat, self.project)
        self.assertIsNone(chat['file_path'])
        chat = {'file_path': '/old/x.chat.md'}
        agent_db._resolve_chat_file_path(chat, None)
        self.assertEqual(chat['file_path'], '/old/x.chat.md')


if __name__ == '__main__':
    unittest.main()
