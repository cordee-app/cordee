import json
import os
import tempfile
import unittest
from unittest.mock import patch

import agent_overseer
import agent_router


class _Resp:
    status_code = 200

    def json(self):
        return {'pages': [{'markdown': 'P12 EX03'}], 'usage_info': {'pages_processed': 1}}


class MistralOcrReferencedFilesTests(unittest.TestCase):
    """RP-085: an image referenced by the task is sent as `image_url`; the
    Working Docs PDF scan only runs when the task references nothing."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.path = self.td.name
        os.makedirs(os.path.join(self.path, 'Working Documents', 'CUPT'))
        self.png = os.path.join('Working Documents', 'CUPT', 'map.png')
        with open(os.path.join(self.path, self.png), 'wb') as f:
            f.write(b'\x89PNG fake')
        with open(os.path.join(self.path, 'Working Documents', 'other.pdf'), 'wb') as f:
            f.write(b'%PDF fake')
        self.sent = []

    def tearDown(self):
        self.td.cleanup()

    def _ocr(self, prompt):
        def fake_post(url, headers=None, json=None, timeout=None):
            self.sent.append(json['document'])
            return _Resp()

        with patch.object(agent_router, 'MISTRAL_VIBE_KEY', 'test-key'), \
             patch('requests.post', fake_post), \
             patch.object(agent_router.agent_config, 'eu_audit', lambda *a, **k: None), \
             patch.object(agent_router.time, 'sleep', lambda s: None):
            return agent_router._call_mistral_ocr('mistral-ocr-latest', prompt,
                                                  project_path=self.path)

    def test_referenced_png_sent_as_image_url(self):
        text, *_ = self._ocr(f"Extract routes.\n\n[Binary file `{self.png}` is present "
                             "in Working Docs but cannot be inlined as text.]")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]['type'], 'image_url')
        self.assertTrue(self.sent[0]['image_url'].startswith('data:image/png;base64,'))
        self.assertIn('P12 EX03', text)
        self.assertTrue(os.path.isfile(os.path.join(
            self.path, 'Working Documents', 'OCR_mistral', 'map.md')))

    def test_large_image_sent_as_overlapping_tiles(self):
        from PIL import Image
        Image.new('RGB', (4000, 2500), 'white').save(os.path.join(self.path, self.png))
        with patch.object(agent_router, '_OCR_TILE_PX', 1800), \
             patch.object(agent_router, '_OCR_TILE_OVERLAP', 200):
            text, *_ = self._ocr(f"[Binary file `{self.png}` is present]")
        # step 1600 px: x in 0/1600/3200, y in 0/1600
        self.assertEqual(len(self.sent), 6)
        self.assertTrue(all(d['image_url'].startswith('data:image/png;base64,') for d in self.sent))
        self.assertIn('## Tile x=3200 y=1600', text)
        self.assertIn('6 pages', text)

    def test_no_reference_falls_back_to_pdf_scan(self):
        self._ocr('OCR the documents.')
        self.assertEqual([d['type'] for d in self.sent], ['document_url'])

    def test_reference_outside_project_ignored(self):
        self.assertEqual(agent_router._ocr_referenced_files(
            '[Binary file `../../etc/x.png` is present]', self.path), [])


class OverseerOcrCapabilityTests(unittest.TestCase):
    """The H2 gate skipped RP-085 because mistral-ocr-latest has vision=false."""

    def _h2(self, caps, noted):
        prompts = []

        def fake_ask(model, prompt, *a, **kw):
            prompts.append(prompt)
            return json.dumps({'gate': 'run', 'reason': 'ok'}), None

        with tempfile.TemporaryDirectory() as td, \
             patch.object(agent_overseer, '_ask', fake_ask):
            agent_overseer.pre_run_check(
                {'id': 1, 'title': 'T', 'description': 'Extract routes from `map.png`'},
                {'name': 'P', 'path': td, 'aingel_model': 'm'}, ['map.png'], noted, caps, [])
        return prompts[0]

    def test_api_ocr_reads_png(self):
        p = self._h2({'file_access': 'api-ocr', 'vision': False}, ['map.png'])
        self.assertNotIn('model has no vision capability', p)
        self.assertIn('OCR-only model', p)
        self.assertIn('run (OCR route)', p)

    def test_caught_relpath_matches_basename_reference(self):
        prompts = []

        def fake_ask(model, prompt, *a, **kw):
            prompts.append(prompt)
            return json.dumps({'gate': 'run', 'reason': 'ok'}), None

        with tempfile.TemporaryDirectory() as td, \
             patch.object(agent_overseer, '_ask', fake_ask):
            os.makedirs(os.path.join(td, 'Working Documents', 'CUPT'))
            open(os.path.join(td, 'Working Documents', 'CUPT', 'map.png'), 'wb').close()
            rel = 'Working Documents/CUPT/map.png'
            agent_overseer.pre_run_check(
                {'id': 1, 'title': 'T', 'description': 'Extract routes from `map.png`'},
                {'name': 'P', 'path': td, 'aingel_model': 'm'}, [rel], [rel],
                {'file_access': 'api-ocr', 'vision': False}, [])
        self.assertNotIn('outside Working Docs scan scope', prompts[0])
        self.assertIn('"vision": "via OCR"', prompts[0])

    def test_prompt_header_survives_missing_budget(self):
        p = self._h2({'file_access': 'none', 'vision': False}, [])
        self.assertTrue(p.startswith('You are the Guide'))
        self.assertIn('"gate": "run|hold|skip"', p)
        self.assertIn('Budget used: n/a', p)

    def _verdict(self, caps, reply):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(agent_overseer, '_ask', lambda *a, **k: (json.dumps(reply), None)):
            return agent_overseer.pre_run_check(
                {'id': 1, 'title': 'T', 'description': 'OCR `map.png`'},
                {'name': 'P', 'path': td, 'aingel_model': 'm'}, ['map.png'], ['map.png'], caps, [])

    def test_ocr_vision_skip_is_overruled(self):
        r = self._verdict({'file_access': 'api-ocr', 'vision': False},
                          {'gate': 'skip', 'reason': 'Model lacks vision capability to parse the PNG.'})
        self.assertEqual(r['gate'], 'run')

    def test_ocr_interpretation_skip_stands(self):
        r = self._verdict({'file_access': 'api-ocr', 'vision': False},
                          {'gate': 'skip', 'reason': 'OCR-only; cannot filter labels inside coloured squares.'})
        self.assertEqual(r['gate'], 'skip')

    def test_text_model_vision_skip_stands(self):
        r = self._verdict({'file_access': 'none', 'vision': False},
                          {'gate': 'skip', 'reason': 'Model lacks vision capability to parse the PNG.'})
        self.assertEqual(r['gate'], 'skip')

    def test_text_model_still_flags_png(self):
        p = self._h2({'file_access': 'none', 'vision': False}, ['map.png'])
        self.assertIn('model has no vision capability', p)
        self.assertNotIn('OCR-only model', p)
        self.assertNotIn('run (OCR route)', p)


if __name__ == '__main__':
    unittest.main()
