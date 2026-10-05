import json
import os
import tempfile
import unittest
from unittest.mock import patch

import hf_catalog


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class HfCatalogValidationTests(unittest.TestCase):
    def test_legal_model_needs_self_hosting(self):
        repo = {
            'id': 'speakleash/Polish-Legal-BERT',
            'pipeline_tag': 'text-classification',
            'tags': ['legal', 'polish', 'bert'],
            'license': 'cc-by-4.0',
            'downloads': 50000, 'likes': 200,
            'cardData': {'language': ['pl'],
                         'base_model': 'google-bert/bert-base-multilingual-cased'},
        }
        result = hf_catalog.validate_candidate(repo)
        self.assertFalse(result['servable'])
        self.assertGreater(result['score'], 0.0)
        self.assertIn('needs self-hosting', result['reasons'])

    def test_finetune_does_not_resolve_to_base(self):
        # A fine-tune declares its parent; its weights differ from the hosted
        # base, so it must NOT be silently mapped onto scw-qwen3.6-35b.
        repo = {
            'id': 'org/Qwen3.6-35B-legal-lora',
            'pipeline_tag': 'text-generation',
            'tags': ['legal'],
            'license': 'apache-2.0',
            'downloads': 12000, 'likes': 150,
            'cardData': {'base_model': 'Qwen/Qwen3.6-35B'},
        }
        self.assertEqual(hf_catalog.resolve_provider_mapping(repo), '')
        self.assertFalse(hf_catalog.validate_candidate(repo)['servable'])

    def test_base_model_resolves_to_scaleway(self):
        # A hosted base model (no declared parent) maps to the Scaleway engine.
        repo = {
            'id': 'Qwen/Qwen3.6-35B-A3B',
            'pipeline_tag': 'text-generation',
            'tags': [],
            'license': 'apache-2.0',
            'downloads': 500000, 'likes': 2000,
        }
        self.assertEqual(hf_catalog.resolve_provider_mapping(repo), 'scw-qwen3.6-35b')
        self.assertTrue(hf_catalog.validate_candidate(repo)['servable'])

    def test_resolve_unknown_base_returns_empty(self):
        self.assertEqual(hf_catalog.resolve_provider_mapping({'id': 'org/unknown-bert'}), '')
        self.assertEqual(hf_catalog.resolve_provider_mapping(None), '')

    def test_ner_model_is_not_a_task_model(self):
        # token-classification must NOT earn the generation bonus and must be
        # flagged task_model=False.
        repo = {
            'id': 'lexedit/herbert-polish-legal-ner',
            'pipeline_tag': 'token-classification',
            'tags': ['legal', 'polish'],
            'license': 'cc-by-4.0',
            'downloads': 143, 'likes': 0,
            'cardData': {'language': ['pl']},
        }
        result = hf_catalog.validate_candidate(repo)
        self.assertFalse(result['task_model'])
        self.assertIn('not a task model (token-classification)', result['reasons'])

    def test_gguf_llm_inferred_as_generation(self):
        # A GGUF LLM with an empty pipeline_tag is inferred as text-generation,
        # so it earns the bonus and is not flagged "not a task model".
        repo = {
            'id': 'mradermacher/Bielik-7B-polish-law-GGUF',
            'pipeline_tag': '',
            'tags': ['gguf', 'legal', 'polish'],
            'license': 'apache-2.0',
            'downloads': 173, 'likes': 1,
            'cardData': {'language': ['pl']},
        }
        result = hf_catalog.validate_candidate(repo)
        self.assertTrue(result['task_model'])
        self.assertIn('text-generation (inferred)', result['reasons'])

    def test_license_is_permissive(self):
        self.assertTrue(hf_catalog.license_is_permissive('apache-2.0'))
        self.assertTrue(hf_catalog.license_is_permissive('openrail'))
        self.assertFalse(hf_catalog.license_is_permissive('cc-by-nc-4.0'))
        self.assertFalse(hf_catalog.license_is_permissive(''))


class HfCatalogSearchTests(unittest.TestCase):
    def test_search_normalizes_and_filters_language(self):
        payload = [
            {
                'id': 'org/polish-legal', 'pipeline_tag': 'text-classification',
                'tags': ['legal'], 'license': 'cc-by-4.0',
                'downloads': 10, 'likes': 2,
                'cardData': {'language': ['pl']},
            },
            {
                'id': 'org/english-legal', 'pipeline_tag': 'text-classification',
                'tags': ['legal'], 'license': 'mit',
                'downloads': 10, 'likes': 2,
                'cardData': {'language': ['en']},
            },
        ]
        with patch('hf_catalog.requests.get', return_value=_FakeResponse(payload)):
            results = hf_catalog.search_models(q='legal', language='pl')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['repo_id'], 'org/polish-legal')
        self.assertIn('validation', results[0])

    def test_get_model_card(self):
        payload = {'id': 'org/x', 'license': 'apache-2.0'}
        with patch('hf_catalog.requests.get', return_value=_FakeResponse(payload)) as get:
            result = hf_catalog.get_model_card('org/x')
        self.assertEqual(result['id'], 'org/x')
        get.assert_called_once()


class HfCatalogPersistenceTests(unittest.TestCase):
    def test_register_and_reload(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'hf_models.json')
            with patch.object(hf_catalog, '_CATALOG_PATH', path):
                entry = {
                    'label': 'Polish Legal BERT',
                    'pipeline_tag': 'text-classification',
                    'provider_mapping': '',
                    'validation': {'score': 0.8, 'reasons': [], 'servable': False},
                }
                hf_catalog.register_model('org/polish-legal', entry)
                self.assertEqual(hf_catalog.catalog_entry('org/polish-legal')['label'],
                                 'Polish Legal BERT')
                # Re-open from disk to confirm it actually persisted.
                with open(path) as f:
                    data = json.load(f)
                self.assertIn('org/polish-legal', data['models'])


if __name__ == '__main__':
    unittest.main()
