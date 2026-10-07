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


class UserFilesNotVersionedTests(unittest.TestCase):
    """User folders leave git: deleted files cannot come back through it and
    repos stop growing with every PDF. Task deliverables stay versioned."""

    def _git(self, p, *args):
        return subprocess.run(['git', *args], cwd=p, capture_output=True, text=True,
                              check=True).stdout

    def test_existing_repo_untracks_user_files_and_keeps_them_on_disk(self):
        with tempfile.TemporaryDirectory() as p:
            self._git(p, 'init', '-q')
            self._git(p, 'config', 'user.email', 't@t')
            self._git(p, 'config', 'user.name', 't')
            _write(os.path.join(p, '.gitignore'), '*.memory.md\n')
            _write(os.path.join(p, 'Working Documents', 'letter.pdf'), 'pdf')
            _write(os.path.join(p, 'Artifacts', 'outputs', 't', 'reply.txt'), 'out')
            _write(os.path.join(p, 'docs', 'api.md'), 'doc')
            self._git(p, 'add', '-A')
            self._git(p, 'commit', '-qm', 'init')

            agent_git._ensure_gitignore(p)
            res = agent_git._untrack_runtime_files(p)

            self.assertIn('Working Documents/letter.pdf', res['untracked'])
            tracked = self._git(p, 'ls-files').split('\n')
            self.assertNotIn('Working Documents/letter.pdf', tracked)
            self.assertIn('Artifacts/outputs/t/reply.txt', tracked)
            self.assertIn('docs/api.md', tracked)
            self.assertTrue(os.path.exists(os.path.join(p, 'Working Documents', 'letter.pdf')))
            # Ignored now: survives a branch round-trip and is never re-added.
            self._git(p, 'checkout', '-qb', 'task/1-x')
            self._git(p, 'checkout', '-q', '-')
            self.assertTrue(os.path.exists(os.path.join(p, 'Working Documents', 'letter.pdf')))
            self.assertEqual(self._git(p, 'status', '--porcelain').strip(), '')

    def test_new_repo_template_ignores_user_folders(self):
        for rule in agent_git.USER_FILE_IGNORE_RULES:
            self.assertIn(rule, agent_git.GITIGNORE_TEMPLATE)
        self.assertNotIn('\ndocs/', agent_git.GITIGNORE_TEMPLATE)


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


class OverseerOutputNameTests(unittest.TestCase):
    """#10001185: H2 held the run because the output-name example
    'DPP-XXXXXX.md' was read as a missing input file."""

    DESC = ("Extract the decision number (starting with DPP-) from the document. "
            "Name the output file using the extracted decision number "
            "(e.g., 'DPP-XXXXXX.md'). Save the MD file in the folder 'OCR_mistral'.")

    def test_placeholder_name_is_not_a_reference(self):
        self.assertEqual(agent_overseer._extract_referenced_files(self.DESC), [])
        refs = agent_overseer._extract_referenced_files(
            "Write `report_<date>.md` and `summary_YYYY.csv` and 'notes-{id}.txt'.")
        self.assertEqual(refs, [])

    def test_dotted_case_number_is_not_a_file(self):
        # #10001184: '.2026' was read as an extension, so strict mode would
        # have held the run on a "missing" file.
        desc = "Open the file named 'DPN-WPOA.501.282.2026.1.DK_pisemne_ostrzeżenie'."
        self.assertEqual(agent_overseer._extract_referenced_files(desc), [])
        self.assertEqual(agent_overseer._extract_referenced_files("Read `scan.mp3` and `a.7z`."),
                         ['a.7z', 'scan.mp3'])

    def test_uppercase_case_number_suffix_is_not_a_file(self):
        # #10001186: reply headers like "… DPN-WPOA.501.282.2026.1.DK" were
        # listed as unread files with a '.DK' extension.
        desc = ('[Header: "Simple Reply to UTK Letter DPN-WPOA.501.282.2026.1.DK"] '
                "and 'DPP-WOPN.718.4.2021.PP'")
        self.assertEqual(agent_overseer._extract_referenced_files(desc), [])

    def test_known_extensions_in_any_case_and_long_ones(self):
        desc = "Read 'Report.XLSX', 'Notes.PDF', `data.parquet`, `main.c` and 'model.scad'."
        self.assertEqual(agent_overseer._extract_referenced_files(desc),
                         ['Notes.PDF', 'Report.XLSX', 'data.parquet', 'main.c', 'model.scad'])

    def test_outputs_listed_under_an_output_parent(self):
        # #10001186: "- Produce two separate text files:" with the names as
        # sub-items was not seen as output, so they looked like missing inputs.
        desc = ("2. **Output:**\n"
                "   - Produce **two separate text files** (no folder paths):\n"
                "     - `UTK_reply_option1.txt` (simple admission of facts).\n"
                "     - `UTK_reply_option2.txt` (aggressive reply).\n"
                "Use these files:\n  - `notes.txt`\n")
        refs = agent_overseer._extract_referenced_files(desc)
        self.assertEqual(agent_overseer._output_target_refs(desc, refs),
                         {'UTK_reply_option1.txt', 'UTK_reply_option2.txt'})

    def test_do_not_use_reference_is_not_an_input(self):
        # #10001186: "Do **not** reference `READMEFIRST.md`" listed it as unread.
        ex = agent_overseer._extract_referenced_files
        self.assertEqual(ex("Do **not** reference `READMEFIRST.md` or other files."), [])
        self.assertEqual(ex("Do not modify `a.pdf`."), ['a.pdf'])
        self.assertEqual(ex("Never use `old.xlsx`; use `new.xlsx`."), ['new.xlsx'])
        self.assertEqual(ex("Do not use `x.md`. Then read `x.md` again."), ['x.md'])

    def test_roman_numerals_are_not_placeholders(self):
        self.assertEqual(agent_overseer._extract_referenced_files("Read 'Uchwala_XXXIV.pdf'."),
                         ['Uchwala_XXXIV.pdf'])

    def test_imperative_name_marks_an_output(self):
        desc = "Name the output file using the decision number: `DPP-WOPN.718.4.2021.PP.md`."
        refs = agent_overseer._extract_referenced_files(desc)
        self.assertEqual(agent_overseer._output_target_refs(desc, refs),
                         {'DPP-WOPN.718.4.2021.PP.md'})

    def test_named_input_is_still_an_input(self):
        desc = "Open the file named `DPP-WOPN.718.4.2021.PP.md` and summarise it."
        refs = agent_overseer._extract_referenced_files(desc)
        self.assertEqual(agent_overseer._output_target_refs(desc, refs), set())

    def test_h2_does_not_hold_on_placeholder(self):
        with tempfile.TemporaryDirectory() as p:
            _write(os.path.join(p, 'Working Documents', 'UTK_Decisions_PDF', 'DU_20211300-sig.pdf'))

            def fake_ask(model, prompt, *a, **k):
                return '{"gate": "run", "reason": "ok", "completeness": "complete"}', None

            with mock.patch.object(agent_overseer, '_ask', side_effect=fake_ask):
                h2 = agent_overseer.pre_run_check(
                    {'id': 1, 'title': 't', 'description': self.DESC},
                    {'name': 'P', 'path': p, 'aingel_model': 'mistral-medium-latest'},
                    [], [], {'file_access': 'native'}, [])
            self.assertEqual(h2.get('self_serve') or [], [])
            self.assertEqual(h2.get('missing_files') or [], [])


class GitEverywhereTests(unittest.TestCase):
    """Every project is versioned (Reject + audit trail), not only code."""

    def test_unset_means_on_even_without_code(self):
        with tempfile.TemporaryDirectory() as p:
            _write(os.path.join(p, 'Working Documents', 'letter.pdf'))
            self.assertTrue(agent_git.resolve_enabled(None, p))
            self.assertTrue(agent_git.resolve_enabled(1, p))
            self.assertFalse(agent_git.resolve_enabled(0, p))


class TaskExecutionTypeTests(unittest.TestCase):
    """Processing type moved from the project to the task."""

    def test_normalisation(self):
        import agent_api
        norm = agent_api._norm_task_execution_type
        self.assertIsNone(norm(None))
        self.assertIsNone(norm(''))
        self.assertIsNone(norm('standard'))
        self.assertEqual(norm(' Research '), 'research')
        self.assertEqual(norm('deployment'), 'deployment')
        with self.assertRaises(ValueError):
            norm('software')


class H2CitedFolderTests(unittest.TestCase):
    """#10001184: under strict mode H2 held because it could not tell that
    the cited folder 'OCR_Mistral' exists."""

    def test_cited_folder_found_case_insensitively(self):
        with tempfile.TemporaryDirectory() as p:
            _write(os.path.join(p, 'Working Documents', 'OCR_mistral', 'a.md'))
            _write(os.path.join(p, 'Working Documents', 'Legal', 'b.md'))
            _write(os.path.join(p, 'Artifacts', 'outputs', '7-search-ocr-mistral', 'c.md'))
            found = agent_overseer._cited_project_folders(
                'Search the folder **OCR_Mistral** for the decision.', p)
            self.assertEqual(found, [os.path.join('Working Documents', 'OCR_mistral')])

    def test_h2_prompt_lists_cited_folder(self):
        with tempfile.TemporaryDirectory() as p:
            _write(os.path.join(p, 'Working Documents', 'OCR_mistral', 'a.md'))
            captured = {}

            def fake_ask(model, prompt, *a, **k):
                captured['prompt'] = prompt
                return '{"gate": "run", "reason": "ok", "completeness": "complete"}', None

            with mock.patch.object(agent_overseer, '_ask', side_effect=fake_ask):
                agent_overseer.pre_run_check(
                    {'id': 1, 'title': 't', 'description': 'Search the folder OCR_Mistral.'},
                    {'name': 'P', 'path': p, 'aingel_model': 'mistral-medium-latest'},
                    [], [], {'file_access': 'native'}, [])
            self.assertIn('Folders named in the description that EXIST', captured['prompt'])
            self.assertIn('OCR_mistral', captured['prompt'])

    def test_h2_prompt_states_rag_outcome(self):
        captured = {}

        def fake_ask(model, prompt, *a, **k):
            captured['prompt'] = prompt
            return '{"gate": "run", "reason": "ok", "completeness": "complete"}', None

        intent = {'corpus_id': 'railway', 'reason': 'auto-detected legal intent'}
        with mock.patch.object(agent_overseer, '_ask', side_effect=fake_ask), \
                mock.patch('agent_rag.detect_rag_intent', return_value=intent):
            agent_overseer.pre_run_check(
                {'id': 1, 'title': 't', 'description': 'Cite Art. 12.'},
                {'name': 'P', 'path': '', 'aingel_model': 'mistral-medium-latest'},
                [], [], {'file_access': 'native'}, [],
                rag_provenance={'n_hits': 6, 'corpus_id': 'railway'})
        self.assertIn('6 citations inlined in the prompt', captured['prompt'])


class H2RagRequestedTests(unittest.TestCase):
    """#10001186: the description asked for the RAG library, but Requires RAG
    was off and nothing legal triggered it, so the run would cite sources it
    never retrieved."""

    DESC = "Use the RAG system for factual verification. Cite it as \"RAG: [query]\"."

    def _h2(self, desc, intent, model='mistral-glm-5-3', use_rag=1):
        def fake_ask(model, prompt, *a, **k):
            return '{"gate": "run", "reason": "ok", "completeness": "complete"}', None

        with tempfile.TemporaryDirectory() as p, \
                mock.patch.object(agent_overseer, '_ask', side_effect=fake_ask), \
                mock.patch('agent_rag.detect_rag_intent', return_value=intent):
            return agent_overseer.pre_run_check(
                {'id': 1, 'title': 't', 'description': desc, 'model': model},
                {'name': 'P', 'path': p, 'aingel_model': 'mistral-medium-latest',
                 'use_rag': use_rag},
                [], [], {'file_access': 'bash'}, [])

    def test_tool_loop_model_has_rag_query(self):
        # The live run of #10001186 was on scw-qwen3.5-397b, which can call
        # rag_query itself: holding it was a false positive.
        self.assertEqual(self._h2(self.DESC, None, model='scw-qwen3.5-397b')['gate'], 'run')
        self.assertEqual(
            self._h2(self.DESC, None, model='scw-qwen3.5-397b', use_rag=0)['gate'], 'hold')

    def test_requested_but_not_wired_holds(self):
        h2 = self._h2(self.DESC, None)
        self.assertEqual(h2['gate'], 'hold')
        self.assertIn('Requires RAG library', h2['reason'])

    def test_wired_runs(self):
        h2 = self._h2(self.DESC, {'corpus_id': 'railway', 'reason': 'explicit requires_rag'})
        self.assertEqual(h2['gate'], 'run')

    def test_no_mention_runs(self):
        self.assertEqual(self._h2('Write a short reply.', None)['gate'], 'run')


class H2aFileRankingTests(unittest.TestCase):
    """H2a's file list is relevance-ranked then capped; each case is a past
    regression that made the overseer call a real file or folder missing."""

    @staticmethod
    def _noise_outputs(n):
        # Old task outputs whose slugs are full of common words.
        return [f'Artifacts/outputs/{10001000 + i}-find-the-decision-file-and-name-it/'
                f'exec-{20000000 + i}-output.md' for i in range(n)]

    def test_cited_basename_ranks_first_despite_relative_paths(self):
        # #10001126 (big project, alphabetical cap) + #10001185 (relative
        # paths killed the exact-name tier).
        paths = self._noise_outputs(200) + [
            f'Working Documents/UTK_Decisions_PDF/DU_2021{i:02d}00-sig.pdf' for i in range(5, 14)]
        desc = ("in the folder 'UTK_Decisions_PDF', find the file 'DU_20211300-sig'. "
                "Transform it as a MD file named by the decision number.")
        files, _ = agent_overseer._rank_h2a_paths(paths, desc)
        self.assertEqual(files[0], 'Working Documents/UTK_Decisions_PDF/DU_20211300-sig.pdf')

    def test_nested_reference_keeps_its_folder_path(self):
        # #10001145: a file two folders deep must be listed with its path.
        target = 'Working Documents/CUPT/II Etap/05_WKR_Założenia_do_oferty_przewozowej-FULL.pdf'
        paths = self._noise_outputs(200) + [target, 'Working Documents/CUPT/other.md']
        desc = f"Open the file `'{target}'` and extract the package split pages."
        files, _ = agent_overseer._rank_h2a_paths(paths, desc)
        self.assertEqual(files[0], target)

    def test_cited_folder_is_listed_even_when_its_files_are_capped_out(self):
        # #10001185: 'OCR_mistral' held 87 files, all past the 120 cap.
        paths = (self._noise_outputs(300)
                 + [f'Working Documents/OCR_mistral/DPP-{i}.md' for i in range(87)])
        desc = "Save the MD file in the folder 'OCR_mistral'."
        files, folders = agent_overseer._rank_h2a_paths(paths, desc)
        self.assertEqual(folders[0], 'Working Documents/OCR_mistral (87 files)')
        self.assertTrue(files[0].startswith('Working Documents/OCR_mistral/'))

    def test_one_word_stem_needs_its_extension_to_be_cited(self):
        # #10001184: 'TASKS.md' and '_summary.json' outranked the cited letter
        # because the description used the words "tasks" and "summary".
        letter = 'Working Documents/UTK Pisma/DPN-WPOA.501.282.2026.1.DK_ostrzeżenie.pdf'
        paths = ['Working Documents/TASKS.md', 'Working Documents/A/_summary.json', letter]
        desc = ("Open 'DPN-WPOA.501.282.2026.1.DK_ostrzeżenie'. Write a 1-sentence summary; "
                "no compound tasks.")
        files, _ = agent_overseer._rank_h2a_paths(paths, desc)
        self.assertEqual(files[0], letter)
        files, _ = agent_overseer._rank_h2a_paths(paths, 'Update TASKS.md.')
        self.assertEqual(files[0], 'Working Documents/TASKS.md')

    def test_output_slug_words_do_not_count_as_relevance(self):
        paths = self._noise_outputs(5) + ['Working Documents/zz_unrelated.md']
        files, _ = agent_overseer._rank_h2a_paths(paths, 'find the decision file')
        # No tier applies to anything: plain alphabetical order.
        self.assertEqual(files, sorted(paths, key=str.lower))

    def test_common_filename_token_is_not_distinctive(self):
        shared = [f'Working Documents/DPP-WOPN.717.{i}.2024.md' for i in range(10)]
        rare = 'Working Documents/zz_umowa_final.md'
        files, _ = agent_overseer._rank_h2a_paths(shared + [rare], 'Check the WOPN umowa text.')
        self.assertEqual(files[0], rare)

    def test_prompt_carries_folder_list(self):
        captured = {}

        def fake_ask(model, prompt, *a, **k):
            captured['prompt'] = prompt
            return '{"questions": [], "reason": "clear"}', None

        with mock.patch.object(agent_overseer, '_ask', side_effect=fake_ask):
            agent_overseer.pre_check_questions(
                {'id': 1, 'title': 't', 'description': "Save into 'OCR_mistral'."},
                {'name': 'P', 'path': '', 'aingel_model': 'mistral-medium-latest'},
                ['Working Documents/OCR_mistral/a.md'])
        self.assertIn('Working Documents/OCR_mistral (1 files)', captured['prompt'])


if __name__ == '__main__':
    unittest.main()
