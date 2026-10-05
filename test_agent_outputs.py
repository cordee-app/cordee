"""Task outputs live in Artifacts/outputs/<task-slug>/ — never the project root
or Working Docs (regression: task #10001148 wrote its press release to the
project root, where task #10001149, H2a and the Files view couldn't find it).
"""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import agent_executor
import agent_git
import agent_overseer
import agent_tools


def _write(path, text='x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


class OutputGitignoreTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.p = self.td.name
        subprocess.run(['git', 'init', '-q'], cwd=self.p, check=True)

    def tearDown(self):
        self.td.cleanup()

    def _untracked(self):
        out = subprocess.run(['git', 'status', '--porcelain', '-uall'], cwd=self.p,
                             capture_output=True, text=True, check=True).stdout
        return {line[3:] for line in out.splitlines()}

    def test_legacy_artifacts_rule_is_narrowed_to_keep_deliverables_versioned(self):
        _write(os.path.join(self.p, '.gitignore'), 'Artifacts/\n*.memory.md\n')
        _write(os.path.join(self.p, 'Artifacts/outputs/t/report.md'))
        _write(os.path.join(self.p, 'Artifacts/outputs/t/exec-1-output.md'))
        _write(os.path.join(self.p, 'Artifacts/outputs/exec-2-output.md'))
        _write(os.path.join(self.p, 'Artifacts/chats/c.chat.md'))
        agent_git._ensure_gitignore(self.p)
        seen = self._untracked()
        self.assertIn('Artifacts/outputs/t/report.md', seen)
        self.assertNotIn('Artifacts/outputs/t/exec-1-output.md', seen)
        self.assertNotIn('Artifacts/outputs/exec-2-output.md', seen)
        self.assertNotIn('Artifacts/chats/c.chat.md', seen)
        with open(os.path.join(self.p, '.gitignore'), encoding='utf-8') as f:
            self.assertNotIn('Artifacts/\n', f.read())

    def test_new_repo_template_versions_deliverables_only(self):
        _write(os.path.join(self.p, 'Artifacts/outputs/t/report.md'))
        _write(os.path.join(self.p, 'Artifacts/outputs/t/exec-1-output.md'))
        agent_git._ensure_gitignore(self.p)
        seen = self._untracked()
        self.assertIn('Artifacts/outputs/t/report.md', seen)
        self.assertNotIn('Artifacts/outputs/t/exec-1-output.md', seen)


class WriteFileRoutingTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.p = self.td.name
        os.makedirs(os.path.join(self.p, 'Working Documents'))

    def tearDown(self):
        self.td.cleanup()

    def test_bare_filename_goes_to_task_output_dir(self):
        with agent_tools.output_dir_scope(os.path.join('Artifacts', 'outputs', 'my-task')):
            r = agent_tools.write_file(self.p, 'report.md', 'hi')
        self.assertTrue(r.get('ok'), r)
        self.assertEqual(r['path'], os.path.join('Artifacts', 'outputs', 'my-task', 'report.md'))
        self.assertTrue(os.path.isfile(os.path.join(self.p, r['path'])))
        self.assertFalse(os.path.exists(os.path.join(self.p, 'Working Documents', 'report.md')))

    def test_bare_filename_without_scope_goes_to_outputs_root(self):
        r = agent_tools.write_file(self.p, 'report.md', 'hi')
        self.assertEqual(r['path'], os.path.join('Artifacts', 'outputs', 'report.md'))

    def test_new_working_docs_file_is_redirected_keeping_subpath(self):
        with agent_tools.output_dir_scope(os.path.join('Artifacts', 'outputs', 't')):
            r = agent_tools.write_file(self.p, 'Working Documents/sub/new.md', 'hi')
        self.assertEqual(r['path'], os.path.join('Artifacts', 'outputs', 't', 'sub', 'new.md'))
        self.assertFalse(os.path.exists(os.path.join(self.p, 'Working Documents', 'sub', 'new.md')))

    def test_existing_working_docs_file_is_edited_in_place(self):
        _write(os.path.join(self.p, 'Working Documents', 'notes.md'), 'old')
        r = agent_tools.write_file(self.p, 'Working Documents/notes.md', 'new')
        self.assertEqual(r['path'], 'Working Documents/notes.md')
        with open(os.path.join(self.p, 'Working Documents', 'notes.md'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'new')

    def test_other_folders_stay_refused(self):
        r = agent_tools.write_file(self.p, 'Legal/x.md', 'hi')
        self.assertIn('error', r)
        r = agent_tools.write_file(self.p, 'Artifacts/chats/x.md', 'hi')
        self.assertIn('error', r)

    def test_list_working_docs_includes_earlier_outputs(self):
        _write(os.path.join(self.p, 'Artifacts', 'outputs', 't', 'report.md'))
        files = agent_tools.list_working_docs(self.p)['files']
        self.assertIn(os.path.join('Artifacts', 'outputs', 't', 'report.md'), files)


class RelocateRootFilesTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.p = self.td.name
        subprocess.run(['git', 'init', '-q'], cwd=self.p, check=True)
        _write(os.path.join(self.p, '.gitignore'), 'digest_*.md\n')
        _write(os.path.join(self.p, 'old.md'))
        self.before = agent_executor._root_files_snapshot(self.p)
        self.out = os.path.join('Artifacts', 'outputs', 'my-task')

    def tearDown(self):
        self.td.cleanup()

    def test_moves_new_root_deliverable_only(self):
        _write(os.path.join(self.p, 'PressRelease.md'))
        _write(os.path.join(self.p, 'GUIDE.md'))          # definition file
        _write(os.path.join(self.p, '.hidden'))
        _write(os.path.join(self.p, 'digest_2026.md'))    # git-ignored (cron)
        moved = agent_executor._relocate_new_root_files(self.p, self.before, self.out)
        self.assertEqual(moved, [('PressRelease.md', os.path.join(self.out, 'PressRelease.md'))])
        self.assertTrue(os.path.isfile(os.path.join(self.p, self.out, 'PressRelease.md')))
        for keep in ('old.md', 'GUIDE.md', '.hidden', 'digest_2026.md'):
            self.assertTrue(os.path.isfile(os.path.join(self.p, keep)), keep)

    def test_name_collision_gets_suffix(self):
        _write(os.path.join(self.p, self.out, 'r.md'), 'earlier')
        _write(os.path.join(self.p, 'r.md'), 'new')
        moved = agent_executor._relocate_new_root_files(self.p, self.before, self.out)
        self.assertEqual(moved, [('r.md', os.path.join(self.out, 'r-2.md'))])

    def test_no_snapshot_means_no_move(self):
        _write(os.path.join(self.p, 'x.md'))
        self.assertEqual(agent_executor._relocate_new_root_files(self.p, None, self.out), [])


class BatchAndPromptTests(unittest.TestCase):
    def test_batch_extraction_writes_to_output_dir(self):
        with tempfile.TemporaryDirectory() as p:
            out = os.path.join('Artifacts', 'outputs', 't')
            text = 'Here is the file.\nsummary.md\n```markdown\n# Hello\n```\n'
            written = agent_executor._extract_batch_writes(text, p, out)
            self.assertEqual(len(written), 1)
            self.assertTrue(os.path.isfile(os.path.join(p, out, 'summary.md')))
            self.assertFalse(os.path.exists(os.path.join(p, 'Working Documents')))

    def test_task_output_dir_is_numbered_by_task(self):
        self.assertEqual(
            agent_executor.task_output_dir({'id': 10001149, 'title': 'Get list of Arguments!'}),
            os.path.join('Artifacts', 'outputs', '10001149-get-list-of-arguments'))

    def test_task_output_dir_survives_title_change(self):
        with tempfile.TemporaryDirectory() as p:
            os.makedirs(os.path.join(p, 'Artifacts', 'outputs', '42-old-title'))
            os.makedirs(os.path.join(p, 'Artifacts', 'outputs', '420-other-task'))
            self.assertEqual(
                agent_executor.task_output_dir({'id': 42, 'title': 'New title'}, p),
                os.path.join('Artifacts', 'outputs', '42-old-title'))

    def test_write_full_output_puts_log_in_task_folder(self):
        with tempfile.TemporaryDirectory() as p:
            task = {'id': 7, 'title': 'My Task', 'model': 'm'}
            path = agent_executor._write_full_output(p, 99, task, 'hello', 1, 2)
            self.assertEqual(path, os.path.join(p, 'Artifacts', 'outputs', '7-my-task',
                                                'exec-99-output.md'))

    def test_changed_output_rels_lists_files_written_during_run(self):
        import time
        with tempfile.TemporaryDirectory() as p:
            out = os.path.join('Artifacts', 'outputs', '7-t')
            _write(os.path.join(p, out, 'old.md'))
            os.utime(os.path.join(p, out, 'old.md'), (1, 1))
            _write(os.path.join(p, out, 'new.md'))
            _write(os.path.join(p, out, 'exec-5-output.md'))
            rels = agent_executor._changed_output_rels(p, out, time.time() - 5)
            self.assertEqual(rels, ['Artifacts/outputs/7-t/new.md'])

    def test_agentic_prompt_names_the_output_folder(self):
        task = {'id': 1, 'title': 'My Task', 'description': 'do it',
                'model': 'claude-sonnet-4-6',
                '_output_dir': os.path.join('Artifacts', 'outputs', 'my-task')}
        with mock.patch.object(agent_executor, '_task_uses_direct_text_route', return_value=False):
            prompt = agent_executor._build_prompt(task, None, mode='inline')
        self.assertIn('`Artifacts/outputs/my-task/`', prompt)
        self.assertIn('Never create files in the project root', prompt)


class OutputFolderListingTests(unittest.TestCase):
    def test_files_view_attributes_unclaimed_output_to_folder_task(self):
        import agent_filecat
        with tempfile.TemporaryDirectory() as p:
            f = os.path.join(p, 'Artifacts', 'outputs', '10001149-args', 'Supporting.md')
            _write(f)
            info = agent_filecat.classify_file(p, f, {})
            self.assertEqual(info['category'], 'deliverable')
            self.assertEqual(info['task_id'], 10001149)
            log = os.path.join(p, 'Artifacts', 'outputs', '10001149-args', 'exec-5-output.md')
            _write(log)
            self.assertEqual(agent_filecat.classify_file(p, log, {})['task_id'], 10001149)
            flat = os.path.join(p, 'Artifacts', 'outputs', 'loose.md')
            _write(flat)
            self.assertIsNone(agent_filecat.classify_file(p, flat, {})['task_id'])

    def test_memory_panel_lists_logs_inside_task_folders(self):
        import agent_memory
        with tempfile.TemporaryDirectory() as p:
            _write(os.path.join(p, 'Artifacts', 'outputs', '7-t', 'exec-5-output.md'))
            _write(os.path.join(p, 'Artifacts', 'outputs', '7-t', 'report.md'))
            _write(os.path.join(p, 'Artifacts', 'outputs', 'exec-1-output.md'))
            names = {e['name'] for e in agent_memory.list_memory_files(p)
                     if e.get('file_type') == 'output'}
            self.assertEqual(names, {'exec-5-output.md', 'exec-1-output.md'})


class OverseerReferenceTests(unittest.TestCase):
    def test_url_encoded_path_is_decoded(self):
        refs = agent_overseer._extract_referenced_files(
            'Read `Otwarta_Kolej/Do%C5%9Bwiadczenia%20z%20niemieckiego%20rynku.pdf` now')
        self.assertIn('Otwarta_Kolej/Doświadczenia z niemieckiego rynku.pdf', refs)
        self.assertNotIn('20rynku.pdf', refs)

    def test_bare_name_resolves_inside_task_output_folder(self):
        with tempfile.TemporaryDirectory() as p:
            _write(os.path.join(p, 'Artifacts', 'outputs', 't', 'PressRelease_2026.md'))
            self.assertEqual(
                agent_overseer._resolve_referenced_path('PressRelease_2026.md', p),
                os.path.join(p, 'Artifacts', 'outputs', 't', 'PressRelease_2026.md'))

    def test_h2a_reports_root_file_instead_of_calling_it_missing(self):
        with tempfile.TemporaryDirectory() as p:
            _write(os.path.join(p, 'ALL-RAIL_PressRelease_2026-09-25.md'))
            captured = {}

            def fake_ask(model, prompt, *a, **k):
                captured['prompt'] = prompt
                return '{"questions": [], "reason": "clear"}', None

            with mock.patch.object(agent_overseer, '_ask', side_effect=fake_ask):
                agent_overseer.pre_check_questions(
                    {'id': 1, 'title': 't',
                     'description': 'Use `ALL-RAIL_PressRelease_2026-09-25.md`.'},
                    {'name': 'P', 'path': p, 'aingel_model': 'mistral-medium-latest'},
                    [])
            self.assertIn('exist at the project ROOT', captured['prompt'])
            self.assertIn('ALL-RAIL_PressRelease_2026-09-25.md', captured['prompt'])


if __name__ == '__main__':
    unittest.main()
