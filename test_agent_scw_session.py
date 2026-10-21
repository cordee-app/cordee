"""Tests for Scaleway session name sanitising and error formatting.

These exist because the accented brand prefix broke Scaleway project
creation: 'Cordée - <project>' was sent verbatim to account/v3/projects,
which only accepts names matching ^[a-zA-Z0-9\\._\\- ]+$, so every
confidential-session create failed with HTTP 400 before any resource
existed. They also pin transliteration of stroke letters/ligatures that
NFKD leaves alone, typographic-quote stripping, the S3 bucket-name length
cap, and the readable error text returned to the UI.

No network, no Flask, no DB: only the pure helpers are exercised.
"""
import unittest

import agent_scw_session


# The payload Scaleway actually returned (live audit log, HTTP 400).
_LIVE_400 = {
    'details': [{
        'argument_name': 'name',
        'help_message': 'value does not match regex pattern "^[a-zA-Z0-9\\\\._\\\\- ]+$"',
        'reason': 'constraint',
    }],
    'message': 'invalid argument(s)',
    'type': 'invalid_arguments',
}

_ALLOWED = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._- ')


class ScwDisplayNameTests(unittest.TestCase):
    def test_brand_prefix_is_transliterated(self):
        # The exact case that used to 400: 'é' is not in Scaleway's regex.
        self.assertEqual(
            agent_scw_session._scw_display_name('Cordée - Example Client'),
            'Cordee - Example Client',
        )

    def test_diacritics_become_ascii(self):
        self.assertEqual(agent_scw_session._scw_display_name('Zürich'), 'Zurich')
        self.assertEqual(agent_scw_session._scw_display_name('São Paulo'), 'Sao Paulo')

    def test_stroke_letters_and_ligatures_transliterate(self):
        # NFKD leaves these alone, so _SCW_TRANSLIT maps them by hand.
        cases = {
            'Łódź': 'Lodz',
            'Ørsted': 'Orsted',
            'Straße': 'Strasse',
            'Æsir': 'AEsir',
            'Œuvre': 'OEuvre',
            'Þór': 'THor',
            'Đorđe': 'Dorde',
            'İstanbul': 'Istanbul',
        }
        for raw, expected in cases.items():
            self.assertEqual(agent_scw_session._scw_display_name(raw), expected, raw)
        self.assertEqual(agent_scw_session._slugify('Łódź'), 'lodz')
        self.assertEqual(agent_scw_session._slugify('Zürich'), 'zurich')

    def test_every_output_stays_inside_the_scaleway_charset(self):
        for name in ('Cordée - Example Client', 'Łódź', 'Zürich', 'São Paulo',
                     'Bob’s “App”', '🚀🚀 launch', 'Ångström & Søn',
                     'Ørsted ß Æsir Œuvre', 'Þór Đorđe İstanbul', '日本語'):
            out = agent_scw_session._scw_display_name(name)
            self.assertTrue(out, name)
            self.assertEqual(set(out) - _ALLOWED, set(), f'{name!r} -> {out!r}')

    def test_smart_quotes_are_removed_not_hyphenated(self):
        self.assertEqual(
            agent_scw_session._scw_display_name('Bob’s “App”'),
            'Bobs App',
        )
        self.assertEqual(
            agent_scw_session._scw_display_name('Bob\'s "App"'),
            'Bobs App',
        )
        out = agent_scw_session._scw_display_name('Bob’s “App”')
        self.assertEqual(set(out) - _ALLOWED, set())
        self.assertNotIn('’', out)
        self.assertNotIn('“', out)

    def test_emoji_are_replaced_and_result_is_never_empty(self):
        self.assertEqual(agent_scw_session._scw_display_name('🚀🚀'), 'project')
        self.assertEqual(agent_scw_session._scw_display_name('🚀🚀 launch'), 'launch')

    def test_all_non_ascii_name_falls_back(self):
        # Nothing salvageable: must still be a usable, allowed-charset name.
        self.assertEqual(agent_scw_session._scw_display_name('日本語'), 'project')
        self.assertEqual(agent_scw_session._scw_display_name('Ж'), 'project')

    def test_long_name_is_capped_at_100(self):
        self.assertEqual(len(agent_scw_session._scw_display_name('x' * 200)), 100)

    def test_empty_or_none_falls_back_to_project(self):
        for name in ('', None, '   ', '---'):
            self.assertEqual(agent_scw_session._scw_display_name(name), 'project')

    def test_does_not_mutate_plain_names(self):
        self.assertEqual(agent_scw_session._scw_display_name('Example Client'), 'Example Client')


class SlugifyBucketTests(unittest.TestCase):
    def test_slug_is_capped_so_bucket_name_fits_s3_limit(self):
        slug = agent_scw_session._slugify('a' * 120)
        self.assertLessEqual(len(slug), 40)
        bucket = f'aingel-{slug}-abcdef'
        self.assertLessEqual(len(bucket), 63)
        self.assertEqual(bucket, bucket.lower())
        self.assertFalse(slug.startswith('-'))
        self.assertFalse(slug.endswith('-'))

    def test_accented_names_yield_a_clean_slug(self):
        self.assertEqual(
            agent_scw_session._slugify('Cordée - Example Client'),
            'cordee-example-client',
        )

    def test_empty_name_falls_back_to_project(self):
        self.assertEqual(agent_scw_session._slugify(''), 'project')
        self.assertEqual(agent_scw_session._slugify(None), 'project')
        self.assertEqual(agent_scw_session._slugify('---'), 'project')


class ScwErrorTextTests(unittest.TestCase):
    def test_live_400_payload_surfaces_the_regex_hint(self):
        text = agent_scw_session._scw_error_text(_LIVE_400, 400)
        self.assertIn('value does not match regex pattern', text)
        self.assertIn('HTTP 400', text)
        self.assertNotIn('{', text)

    def test_plain_message_payload(self):
        text = agent_scw_session._scw_error_text({'message': 'not found'}, 404)
        self.assertIn('not found', text)
        self.assertIn('HTTP 404', text)

    def test_transport_error_payload(self):
        text = agent_scw_session._scw_error_text(
            {'_error': 'URLError: connection refused'}, 0)
        self.assertIn('connection refused', text)
        self.assertIn('HTTP 0', text)

    def test_non_dict_payload_falls_back_to_repr(self):
        text = agent_scw_session._scw_error_text('boom', 500)
        self.assertIn('boom', text)
        self.assertIn('HTTP 500', text)


if __name__ == '__main__':
    unittest.main()
