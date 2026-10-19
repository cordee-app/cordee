"""Vision-aware binary-file notes in the task prompt (RP-087 regression).

The binary note used to tell every model "Use a code-execution model to
parse it" — including vision models that can simply LOOK at an image via
read_file (delivered as an image part by agent_router._tool_result_content).
RP-087 E3 took the bait: a vision model built a local OCR pipeline, burned
2.4M tokens, and never produced the deliverable. These checks pin the
routing so it cannot silently regress to the one-size-fits-all advice.

Run: python3 -m unittest test_prompt_binary_note.py -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import prompt_builder  # noqa: E402
from prompt_builder import binary_note, build  # noqa: E402

VISION = 'scw-deepseek-v4.1-flash'   # vision: true in model_caps.json
TEXT_ONLY = 'scw-deepseek-v4-flash'  # vision: false


class BinaryNoteTests(unittest.TestCase):
    def test_vision_model_gets_view_hint_for_image(self):
        note = binary_note('PKP_map.png', VISION)
        self.assertIn('VIEW it', note)
        self.assertIn('read_file', note)
        self.assertNotIn('code-execution model', note)

    def test_text_only_model_gets_old_advice_for_image(self):
        note = binary_note('PKP_map.png', TEXT_ONLY)
        self.assertIn('code-execution model', note)
        self.assertNotIn('VIEW it', note)

    def test_no_model_keeps_old_advice(self):
        # conservative default when the model is unknown/not passed
        self.assertIn('code-execution model', binary_note('PKP_map.png'))
        self.assertNotIn('VIEW it', binary_note('PKP_map.png'))

    def test_unknown_model_keeps_old_advice(self):
        # get_cap falls back to defaults (vision False) — safe advice
        self.assertIn('code-execution model', binary_note('map.png', 'no-such-model'))

    def test_non_image_binary_keeps_old_advice_even_with_vision(self):
        # .xlsx read_file results are base64, NOT image parts — the hint
        # would send a vision model into a useless read.
        for name in ('data.xlsx', 'report.pdf', 'db.sqlite'):
            self.assertIn('code-execution model', binary_note(name, VISION))

    def test_image_extensions_covered(self):
        for name in ('m.png', 'm.jpg', 'm.jpeg', 'm.gif', 'm.webp', 'm.bmp'):
            self.assertIn('VIEW it', binary_note(name, VISION), name)


class BuildIntegrationTests(unittest.TestCase):
    """build() must route the note for files it catches as binary."""

    def setUp(self):
        from PIL import Image
        self.td = tempfile.TemporaryDirectory()
        self.proj = self.td.name
        wd = os.path.join(self.proj, 'Working Documents')
        os.makedirs(wd)
        Image.new('RGB', (10, 10), 'red').save(os.path.join(wd, 'PKP_map.png'))
        with open(os.path.join(wd, 'data.xlsx'), 'wb') as f:
            f.write(b'\x00fake-xlsx')

    def tearDown(self):
        self.td.cleanup()

    def _task(self, desc, model):
        return {'id': 1, 'title': 'Routes', 'description': desc, 'model': model}

    def test_build_vision_prompt_carries_view_hint(self):
        out = build(self._task('Analyze the attached image file PKP_map.png', VISION),
                    self.proj)
        self.assertIn('VIEW it', out['prompt'])
        self.assertIn('PKP_map.png', out['noted_binary'])

    def test_build_text_only_prompt_keeps_old_advice(self):
        out = build(self._task('Analyze the attached image file PKP_map.png', TEXT_ONLY),
                    self.proj)
        self.assertIn('code-execution model', out['prompt'])
        self.assertNotIn('VIEW it', out['prompt'])

    def test_build_mixed_binaries_get_each_the_right_note(self):
        out = build(self._task('Analyze the attached image file PKP_map.png and the data.xlsx spreadsheet',
                              VISION), self.proj)
        self.assertIn('VIEW it', out['prompt'])                # image → view hint
        self.assertIn('code-execution model', out['prompt'])  # xlsx → old advice


if __name__ == '__main__':
    unittest.main(verbosity=2)
