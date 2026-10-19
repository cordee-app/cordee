"""
Unit tests for image support in the agentic tool pipeline:
agent_tools.read_file's image branch and agent_router._tool_result_content.

Regression: .png was not in read_file's binary-extension set, so images fell
through to the utf-8 text branch and came back as errors='replace' mojibake —
a vision model (gemma) looped 40 tool turns on the garbage and the task
failed. Images must now come back WHOLE as base64 with an explicit mime, and
the tool loop must deliver them to vision models as image parts (never as
base64 text; text-only models get a note without the payload so one image
cannot blow up the context).

Run:  python3 -m unittest test_agent_tools_images -v
"""
import base64
import json
import os
import tempfile
import unittest

from PIL import Image

import agent_router
import agent_tools

VISION_MODEL = 'scw-deepseek-v4.1-flash'   # vision: true in model_caps.json
TEXT_MODEL = 'scw-deepseek-v4-flash'       # vision: false in model_caps.json


class ReadFileImageTests(unittest.TestCase):
    """read_file on .png/.jpg must return whole-image base64, not mojibake."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.project = self.td.name
        img = Image.new('RGB', (8, 8), (200, 30, 30))
        img.save(os.path.join(self.project, 'map.png'), format='PNG')
        img.save(os.path.join(self.project, 'photo.jpg'), format='JPEG')
        with open(os.path.join(self.project, 'note.txt'), 'w') as f:
            f.write('plain text\n')

    def _raw(self, name):
        with open(os.path.join(self.project, name), 'rb') as f:
            return f.read()

    def test_png_returns_whole_base64_image(self):
        raw = self._raw('map.png')
        result = agent_tools.read_file(self.project, 'map.png')
        self.assertNotIn('error', result)
        self.assertIs(result.get('binary'), True)
        self.assertEqual(result.get('encoding'), 'base64')
        self.assertEqual(result.get('mime'), 'image/png')
        self.assertEqual(result.get('size_bytes'), len(raw))
        self.assertEqual(os.path.getsize(os.path.join(self.project, 'map.png')),
                         result.get('size_bytes'))
        # validate=True: any non-base64 character (the old mojibake) must raise
        self.assertEqual(base64.b64decode(result['content'], validate=True), raw)
        self.assertNotIn('truncated', result)

    def test_jpeg_mime(self):
        result = agent_tools.read_file(self.project, 'photo.jpg')
        self.assertNotIn('error', result)
        self.assertIs(result.get('binary'), True)
        self.assertEqual(result.get('encoding'), 'base64')
        self.assertEqual(result.get('mime'), 'image/jpeg')
        self.assertEqual(base64.b64decode(result['content'], validate=True),
                         self._raw('photo.jpg'))

    def test_png_content_is_base64_not_utf8_mojibake(self):
        # Regression: the old text branch returned raw.decode('utf-8',
        # errors='replace'); the content must be strict base64 that
        # round-trips the raw bytes and must differ from that mojibake.
        raw = self._raw('map.png')
        result = agent_tools.read_file(self.project, 'map.png')
        self.assertNotEqual(result['content'], raw.decode('utf-8', errors='replace'))
        self.assertEqual(base64.b64decode(result['content'], validate=True), raw)

    def test_txt_branch_unchanged(self):
        result = agent_tools.read_file(self.project, 'note.txt')
        self.assertNotIn('error', result)
        self.assertEqual(result.get('content'), 'plain text\n')
        self.assertNotIn('binary', result)
        self.assertNotIn('mime', result)

    def test_traversal_outside_project_blocked(self):
        # _safe_join_for_read must still gate image reads: '../x.png' never
        # reaches the image branch.
        result = agent_tools.read_file(self.project, '../outside.png')
        self.assertIn('error', result)
        self.assertNotIn('content', result)


class ToolResultContentTests(unittest.TestCase):
    """agent_router._tool_result_content maps a dispatch result string to the
    `role: 'tool'` message content: image results become image parts for
    vision models, a payload-free note for text-only models, and everything
    else passes through unchanged."""

    def _image_result_str(self, path='map.png', mime='image/png'):
        # Shaped exactly like json.dumps of agent_tools.read_file's image
        # branch, as agent_tools.dispatch returns it.
        payload = b'\x89PNG-fake-image-bytes-not-really-a-png'
        return json.dumps({
            'path': path,
            'content': base64.b64encode(payload).decode('ascii'),
            'binary': True,
            'encoding': 'base64',
            'mime': mime,
            'size_bytes': len(payload),
        })

    def test_vision_model_gets_image_part(self):
        src = self._image_result_str()
        b64 = json.loads(src)['content']
        content, log = agent_router._tool_result_content(src, VISION_MODEL)
        self.assertIsInstance(content, list)
        self.assertEqual(len(content), 2)
        # text part: metadata only, never the base64 payload
        self.assertEqual(content[0]['type'], 'text')
        self.assertNotIn(b64, content[0]['text'])
        meta = json.loads(content[0]['text'])
        self.assertEqual(meta['path'], 'map.png')
        self.assertEqual(meta['mime'], 'image/png')
        self.assertNotIn('content', meta)
        # image part: proper data URI carrying the payload
        self.assertEqual(content[1]['type'], 'image_url')
        url = content[1]['image_url']['url']
        self.assertTrue(url.startswith('data:image/png;base64,'))
        self.assertTrue(url.endswith(b64))
        # log is redacted metadata — never the base64
        self.assertIsNotNone(log)
        self.assertNotIn(b64, log)
        self.assertIn('map.png', log)
        self.assertIn('vision=True', log)

    def test_non_vision_model_gets_note_without_payload(self):
        src = self._image_result_str()
        b64 = json.loads(src)['content']
        content, log = agent_router._tool_result_content(src, TEXT_MODEL)
        self.assertIsInstance(content, str)
        self.assertIn('no vision', content)
        self.assertNotIn(b64, content)
        parsed = json.loads(content)
        self.assertEqual(parsed['path'], 'map.png')
        self.assertEqual(parsed['mime'], 'image/png')
        self.assertNotIn('content', parsed)
        self.assertIsNotNone(log)
        self.assertNotIn(b64, log)
        self.assertIn('vision=False', log)

    def test_non_image_json_passthrough(self):
        src = json.dumps({'content': 'hello'})
        self.assertEqual(
            agent_router._tool_result_content(src, VISION_MODEL), (src, None))

    def test_plain_non_json_string_passthrough(self):
        self.assertEqual(
            agent_router._tool_result_content('just some text', VISION_MODEL),
            ('just some text', None))

    def test_binary_without_mime_passthrough(self):
        # The pre-existing truncated-binary branch (.db etc.) sets binary=True
        # but no mime — it must keep flowing through as a plain string.
        src = json.dumps({'path': 'data.db', 'content': 'AAAA', 'binary': True,
                          'encoding': 'base64', 'truncated': True})
        self.assertEqual(
            agent_router._tool_result_content(src, VISION_MODEL), (src, None))

    def test_error_result_passthrough(self):
        src = json.dumps({'error': 'File not found: map.png'})
        self.assertEqual(
            agent_router._tool_result_content(src, VISION_MODEL), (src, None))


class CompactImageHistoryTests(unittest.TestCase):
    """agent_router._compact_image_history: image payloads re-send on every
    turn and unbounded accumulation 504s Scaleway's gateway (RP-087 E4: 49
    images / 96 MB fast-fails; 12 / 24.5 MB passes). Within the ~20 MB budget
    NOTHING is compacted — placeholders were their own failure mode (RP-089
    exec 20001269 burned 25 of 60 turns distrusting markers on images it had
    already read). Over budget: oldest consumed images become pointers; the
    pending batch (not yet answered) is never touched."""

    BUDGET = agent_router._IMG_HISTORY_BUDGET_BYTES

    @staticmethod
    def _image_tool_msg(i, path, payload_chars=1000):
        b64 = 'A' * max(100, payload_chars)
        src = json.dumps({'path': path, 'content': b64, 'binary': True,
                          'encoding': 'base64', 'mime': 'image/png',
                          'size_bytes': payload_chars})
        content, _ = agent_router._tool_result_content(src, VISION_MODEL)
        return {'role': 'tool', 'tool_call_id': f'call_{i}', 'content': content}

    @staticmethod
    def _assistant_msg():
        return {'role': 'assistant', 'content': None,
                'tool_calls': [{'id': 'c', 'type': 'function',
                                'function': {'name': 'read_file',
                                            'arguments': '{}'}}]}

    def test_within_budget_nothing_is_compacted(self):
        msgs = [{'role': 'user', 'content': 'go'},
                self._assistant_msg(),
                self._image_tool_msg(1, 'map.png'),
                self._assistant_msg(),
                self._image_tool_msg(2, 'b.png')]
        before = json.dumps(msgs)
        agent_router._compact_image_history(msgs)
        self.assertEqual(before, json.dumps(msgs))

    def test_over_budget_compacts_oldest_first(self):
        # two consumed images ~half-budget each + small pending batch: total
        # just over budget -> exactly the oldest one is compacted
        half = self.BUDGET // 2 + 5000
        msgs = [{'role': 'user', 'content': 'go'},
                self._assistant_msg(),
                self._image_tool_msg(1, 'old_map.png', payload_chars=half),
                self._assistant_msg(),
                self._image_tool_msg(2, 'new_map.png', payload_chars=half),
                self._assistant_msg(),
                self._image_tool_msg(3, 'pending.png', payload_chars=10_000)]
        agent_router._compact_image_history(msgs)
        # oldest consumed image compacted; the newest consumed one and the
        # pending batch must still carry real image parts
        self.assertNotIn('image_url', json.dumps(msgs[2]))
        self.assertIn('old_map.png', json.dumps(msgs[2]))
        self.assertIn('read_file', json.dumps(msgs[2]))
        self.assertIn('image_url', json.dumps(msgs[4]))
        self.assertIn('image_url', json.dumps(msgs[6]))

    def test_pending_batch_survives_even_if_huge(self):
        half = self.BUDGET // 2 + 1000
        msgs = [{'role': 'user', 'content': 'go'},
                self._assistant_msg(),
                self._image_tool_msg(1, 'pending_a.png', payload_chars=half),
                self._image_tool_msg(2, 'pending_b.png', payload_chars=half)]
        agent_router._compact_image_history(msgs)
        self.assertIn('image_url', json.dumps(msgs[2]))
        self.assertIn('image_url', json.dumps(msgs[3]))

    def test_idempotent_over_budget(self):
        half = self.BUDGET // 2 + 5000
        msgs = [{'role': 'user', 'content': 'go'},
                self._assistant_msg(),
                self._image_tool_msg(1, 'old.png', payload_chars=half),
                self._assistant_msg(),
                self._image_tool_msg(2, 'new.png', payload_chars=half),
                self._assistant_msg(),
                self._image_tool_msg(3, 'p.png', payload_chars=10_000)]
        agent_router._compact_image_history(msgs)
        once = json.dumps(msgs)
        agent_router._compact_image_history(msgs)
        self.assertEqual(once, json.dumps(msgs))

    def test_text_tool_results_untouched(self):
        text_msg = {'role': 'tool', 'tool_call_id': 'x', 'content': 'plain result'}
        msgs = [{'role': 'user', 'content': 'go'},
                self._assistant_msg(),
                text_msg,
                self._assistant_msg()]
        before = json.dumps(msgs)
        agent_router._compact_image_history(msgs)
        self.assertEqual(before, json.dumps(msgs))


if __name__ == '__main__':
    unittest.main()
