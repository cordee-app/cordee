import unittest
from unittest.mock import patch

import agent_scw_deploy


# Mimics the live Scaleway `GET /models` response shape (2026-08-19): node
# quantizations use `quantization_bits` (not `bits`) and `max_context_size` is a
# per-quantization field. Includes one catalogue model and one imported custom
# model (tag `custom`).
_LIVE_PAYLOAD = {
    'models': [
        {
            'id': '48ca644a-87a0-473a-b04d-98279cffc5eb',
            'name': 'qwen/qwen3.5-397b-a17b:int4',
            'tags': ['instruct', 'chat', 'featured'],
            'status': 'ready',
            'has_eula': False,
            'size_bytes': 123,
            'parameter_size_bits': 397,
            'nodes_support': [{'nodes': [
                {'node_type_name': 'L4', 'quantizations': [
                    {'quantization_bits': 4, 'allowed': False, 'max_context_size': 0},
                ]},
                {'node_type_name': 'H100-SXM-8', 'quantizations': [
                    {'quantization_bits': 4, 'allowed': True, 'max_context_size': 262144},
                ]},
            ]}],
        },
        {
            'id': '413dee1e-7566-452b-aa80-a7e16c29013e',
            'name': 'Polish_Law_Bielik',
            'tags': ['custom'],
            'status': 'ready',
            'has_eula': False,
            'size_bytes': 28970000000,
            'parameter_size_bits': 7,
            'nodes_support': [{'nodes': [
                {'node_type_name': 'L4', 'quantizations': [
                    {'quantization_bits': 32, 'allowed': False, 'max_context_size': 0},
                ]},
                {'node_type_name': 'L40S', 'quantizations': [
                    {'quantization_bits': 32, 'allowed': True, 'max_context_size': 4096},
                ]},
                {'node_type_name': 'H100-SXM-2', 'quantizations': [
                    {'quantization_bits': 32, 'allowed': True, 'max_context_size': 4096},
                ]},
            ]}],
        },
    ],
}


class ScwDeployListModelsTests(unittest.TestCase):
    def _list(self, payload):
        with patch.object(agent_scw_deploy, '_SCW_SECRET_KEY', 'test-key'), \
             patch.object(agent_scw_deploy, '_scw_get', return_value=payload):
            return agent_scw_deploy.list_models(region='fr-par')

    def test_surfaces_custom_model_with_quantization_bits(self):
        models = self._list(_LIVE_PAYLOAD)
        by_name = {m['name']: m for m in models}
        self.assertEqual(len(models), 2)

        cat = by_name['qwen/qwen3.5-397b-a17b:int4']
        self.assertFalse(cat['custom'])
        self.assertEqual(cat['node_types'], ['H100-SXM-8'])
        self.assertEqual(cat['quantizations'], {'H100-SXM-8': [4]})
        self.assertEqual(cat['max_context_size'], 262144)

        custom = by_name['Polish_Law_Bielik']
        self.assertTrue(custom['custom'])
        self.assertEqual(custom['status'], 'ready')
        self.assertEqual(custom['node_types'], ['L40S', 'H100-SXM-2'])
        self.assertNotIn('L4', custom['node_types'])  # fp32 not allowed on L4
        self.assertEqual(custom['quantizations'], {'L40S': [32], 'H100-SXM-2': [32]})
        self.assertEqual(custom['max_context_size'], 4096)

    def test_old_bits_field_yields_no_models(self):
        # Regression guard: the live API renamed the field to `quantization_bits`.
        # A response using the old `bits` key must not surface anything, so a
        # future rename back would fail loudly here.
        wrong = {
            'models': [{
                'id': 'x', 'name': 'm', 'tags': [], 'status': 'ready',
                'has_eula': False, 'nodes_support': [{'nodes': [
                    {'node_type_name': 'H100-SXM-8', 'quantizations': [
                        {'bits': 4, 'allowed': True},
                    ]},
                ]}],
            }],
        }
        self.assertEqual(self._list(wrong), [])

    def test_pick_quantization_highest_bits(self):
        model = {'quantizations': {'H100-SXM-2': [16, 32], 'L4': [8]}}
        self.assertEqual(agent_scw_deploy._pick_quantization(model, 'H100-SXM-2'), 32)
        self.assertIsNone(agent_scw_deploy._pick_quantization(model, 'H100'))
        self.assertIsNone(agent_scw_deploy._pick_quantization({}, 'H100-SXM-2'))


class ScwCustomModelMatchTests(unittest.TestCase):
    def test_bielik_repo_matches_custom_model(self):
        import agent_api
        self.assertEqual(
            agent_api._repo_tokens('4x32/Bielik-7B-polish-law'),
            ['bielik', 'polish', 'law'],
        )
        self.assertTrue(agent_api._custom_model_matches(
            '4x32/Bielik-7B-polish-law', {'name': 'Polish_Law_Bielik'}))

    def test_unrelated_repo_does_not_match(self):
        import agent_api
        self.assertFalse(agent_api._custom_model_matches(
            'FinanceInc/auditor_sentiment_finetuned', {'name': 'Polish_Law_Bielik'}))


class ClassifyDeployErrorTests(unittest.TestCase):
    def _classify(self, code, body):
        return agent_scw_deploy._classify_deploy_error(code, body)

    def test_auth_403(self):
        kind, msg = self._classify(403, 'forbidden')
        self.assertEqual(kind, 'auth')
        self.assertTrue(msg.startswith('Scaleway rejected the request'))

    def test_auth_401(self):
        kind, msg = self._classify(401, 'unauthorized')
        self.assertEqual(kind, 'auth')
        self.assertTrue(msg.startswith('Scaleway rejected the request'))

    def test_not_found_404(self):
        kind, msg = self._classify(404, 'missing resource')
        self.assertEqual(kind, 'not_found')
        self.assertTrue(msg.startswith('The model or deployment was not found'))

    def test_conflict_409(self):
        kind, msg = self._classify(409, 'already exists')
        self.assertEqual(kind, 'conflict')
        self.assertTrue(msg.startswith('A deployment with that name already exists'))

    def test_out_of_stock_space(self):
        kind, msg = self._classify(400, 'GPU is out of stock right now')
        self.assertEqual(kind, 'out_of_stock')
        self.assertTrue(msg.startswith('This GPU node type is out of stock'))

    def test_out_of_stock_underscore(self):
        kind, _ = self._classify(400, 'node is out_of_stock today')
        self.assertEqual(kind, 'out_of_stock')

    def test_out_of_stock_insufficient_capacity(self):
        kind, _ = self._classify(400, 'insufficient capacity in region')
        self.assertEqual(kind, 'out_of_stock')

    def test_quota(self):
        kind, msg = self._classify(400, 'quota exceeded for project')
        self.assertEqual(kind, 'quota')
        self.assertTrue(msg.startswith('Scaleway quota exceeded'))

    def test_model_not_usable_resource_not_usable(self):
        kind, msg = self._classify(400, 'resource_not_usable for this model')
        self.assertEqual(kind, 'model_not_usable')
        self.assertTrue(msg.startswith('Scaleway says this model is not usable'))

    def test_model_not_usable_not_usable(self):
        kind, _ = self._classify(400, 'model is not usable here')
        self.assertEqual(kind, 'model_not_usable')

    def test_eula(self):
        kind, msg = self._classify(400, 'must accept the eula first')
        self.assertEqual(kind, 'eula')
        self.assertTrue(msg.startswith('This model requires accepting a EULA'))

    def test_invalid_validation(self):
        body = 'validation failed for field name'
        kind, msg = self._classify(400, body)
        self.assertEqual(kind, 'invalid')
        self.assertIn(body, msg)

    def test_invalid_invalid(self):
        body = 'invalid parameter value'
        kind, msg = self._classify(400, body)
        self.assertEqual(kind, 'invalid')
        self.assertIn(body, msg)

    def test_error_fallback(self):
        body = 'something went wrong on the server'
        kind, msg = self._classify(500, body)
        self.assertEqual(kind, 'error')
        self.assertIn('HTTP 500', msg)
        self.assertIn(body, msg)

    # `(body or '').lower()` keeps the matcher safe for None on early code
    # branches that return a fixed message without slicing body.
    def test_body_none_on_auth_branch(self):
        kind, msg = self._classify(403, None)
        self.assertEqual(kind, 'auth')
        self.assertTrue(msg.startswith('Scaleway rejected the request'))

    def test_body_empty_fallback(self):
        kind, msg = self._classify(500, '')
        self.assertEqual(kind, 'error')
        self.assertIn('HTTP 500', msg)

    # None body must not crash the fallback branch (it slices body for the
    # message). Regression guard for the `(body or '')[:200]` fix.
    def test_body_none_fallback(self):
        kind, msg = self._classify(500, None)
        self.assertEqual(kind, 'error')
        self.assertIn('HTTP 500', msg)

    # Code is checked before body keywords: a 409 carrying 'quota' is a
    # conflict, not a quota error.
    def test_code_precedence_over_body(self):
        kind, _ = self._classify(409, 'quota exceeded')
        self.assertEqual(kind, 'conflict')


class ClassifyImportErrorTests(unittest.TestCase):
    def _classify(self, code, body):
        return agent_scw_deploy._classify_import_error(code, body)

    def test_hf_url_normalisation(self):
        self.assertEqual(agent_scw_deploy._hf_url('org/repo'),
                         'https://huggingface.co/org/repo')
        self.assertEqual(agent_scw_deploy._hf_url('https://huggingface.co/org/repo/'),
                         'https://huggingface.co/org/repo')

    def test_gguf_repo_surfaces_help_message(self):
        # Live verify-model rejection for a GGUF-only repo (HTTP 412).
        body = ('{"help_message":"the model with ID \'x/y-GGUF\' is not supported. '
                'Maximum model context length (\'max_position_embeddings\') is not '
                'available in model config.json file.","precondition":"resource_not_usable"}')
        kind, msg = self._classify(412, body)
        self.assertEqual(kind, 'unsupported_format')
        self.assertIn('not supported', msg)

    def test_missing_repo_needs_token(self):
        body = ('{"help_message":"Repository org/nope does not exist or you do not '
                'have enough permissions to access it."}')
        kind, msg = self._classify(412, body)
        self.assertEqual(kind, 'access')
        self.assertIn('HF_TOKEN', msg)

    def test_auth(self):
        kind, _ = self._classify(403, 'forbidden')
        self.assertEqual(kind, 'auth')

    def test_conflict(self):
        kind, msg = self._classify(409, 'already exists')
        self.assertEqual(kind, 'conflict')
        self.assertIn('already exists', msg)

    def test_tokenizer_rejection(self):
        kind, msg = self._classify(400, 'missing tokenizers for this model')
        self.assertEqual(kind, 'unsupported_format')
        self.assertIn('Transformers', msg)

    def test_fallback_none_body(self):
        kind, msg = self._classify(500, None)
        self.assertEqual(kind, 'error')
        self.assertIn('HTTP 500', msg)


class VerifyHfModelTests(unittest.TestCase):
    def _verify(self, payload):
        with patch.object(agent_scw_deploy, '_SCW_SECRET_KEY', 'k'), \
             patch.object(agent_scw_deploy, '_scw_post', return_value=payload) as post:
            result = agent_scw_deploy.verify_hf_model('org/repo')
        return result, post

    def test_success_builds_nodes_and_quantizations(self):
        payload = {
            'nodes': [
                {'node_type_name': 'L4', 'quantizations': [
                    {'quantization_bits': 16, 'allowed': True, 'max_context_size': 40960}]},
                {'node_type_name': 'H100-SXM-2', 'quantizations': [
                    {'quantization_bits': 16, 'allowed': True, 'max_context_size': 40960},
                    {'quantization_bits': 8, 'allowed': False, 'max_context_size': 0}]},
            ],
            'size_bytes': 4522815806,
        }
        result, post = self._verify(payload)
        self.assertTrue(result['ok'])
        self.assertEqual(result['nodes'], ['L4', 'H100-SXM-2'])
        self.assertEqual(result['quantizations'], {'L4': [16], 'H100-SXM-2': [16]})
        self.assertEqual(result['max_context_size'], 40960)
        self.assertEqual(result['size_bytes'], 4522815806)
        # URL passed to Scaleway is the canonical HF model page.
        self.assertEqual(post.call_args[0][1]['source']['url'],
                         'https://huggingface.co/org/repo')

    def test_http_error_maps_to_friendly(self):
        import urllib.error
        err = urllib.error.HTTPError(
            'u', 412, 'precondition', {}, None)
        err.read = lambda: b'{"help_message":"Maximum model context length is not available"}'
        with patch.object(agent_scw_deploy, '_SCW_SECRET_KEY', 'k'), \
             patch.object(agent_scw_deploy, '_scw_post', side_effect=err):
            result = agent_scw_deploy.verify_hf_model('org/gguf')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'unsupported_format')

    def test_missing_key(self):
        with patch.object(agent_scw_deploy, '_SCW_SECRET_KEY', ''):
            result = agent_scw_deploy.verify_hf_model('org/repo')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'auth')


class ImportHfModelTests(unittest.TestCase):
    def test_uses_env_token_when_not_passed(self):
        with patch.object(agent_scw_deploy, '_SCW_SECRET_KEY', 'k'), \
             patch.object(agent_scw_deploy, '_HF_TOKEN', 'hf_secret'), \
             patch.object(agent_scw_deploy, '_scw_post',
                          return_value={'id': 'm1', 'status': 'preparing'}) as post:
            model = agent_scw_deploy.import_hf_model('name1', 'org/repo', project_id='p1')
        self.assertEqual(model['id'], 'm1')
        body = post.call_args[0][1]
        self.assertEqual(body['name'], 'name1')
        self.assertEqual(body['project_id'], 'p1')
        self.assertEqual(body['source']['secret'], 'hf_secret')

    def test_public_repo_omits_secret(self):
        with patch.object(agent_scw_deploy, '_SCW_SECRET_KEY', 'k'), \
             patch.object(agent_scw_deploy, '_HF_TOKEN', ''), \
             patch.object(agent_scw_deploy, '_scw_post',
                          return_value={'id': 'm1', 'status': 'ready'}) as post:
            agent_scw_deploy.import_hf_model('name1', 'org/repo')
        self.assertNotIn('secret', post.call_args[0][1]['source'])

    def test_rejection_raises_scw_import_error(self):
        import urllib.error
        err = urllib.error.HTTPError('u', 409, 'conflict', {}, None)
        err.read = lambda: b'{"message":"already exists"}'
        with patch.object(agent_scw_deploy, '_SCW_SECRET_KEY', 'k'), \
             patch.object(agent_scw_deploy, '_HF_TOKEN', ''), \
             patch.object(agent_scw_deploy, '_scw_post', side_effect=err):
            with self.assertRaises(agent_scw_deploy.ScwImportError) as cm:
                agent_scw_deploy.import_hf_model('name1', 'org/repo')
        self.assertEqual(cm.exception.kind, 'conflict')


class WaitModelReadyTests(unittest.TestCase):
    def test_ready_returns_model(self):
        with patch.object(agent_scw_deploy, '_scw_get', return_value={'status': 'ready'}):
            out = agent_scw_deploy.wait_model_ready('m1', timeout=5)
        self.assertEqual(out['status'], 'ready')

    def test_error_returns_model_with_message(self):
        with patch.object(agent_scw_deploy, '_scw_get',
                          return_value={'status': 'error', 'error_message': 'boom'}):
            out = agent_scw_deploy.wait_model_ready('m1', timeout=5)
        self.assertEqual(out['status'], 'error')
        self.assertEqual(out['error_message'], 'boom')


class ModelImportDbTests(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        import agent_db
        self.agent_db = agent_db
        self._tmp = tempfile.mkdtemp()
        self._orig = agent_db.DB_PATH
        agent_db.DB_PATH = os.path.join(self._tmp, 'aingel.db')
        agent_db.init_db()

    def tearDown(self):
        self.agent_db.DB_PATH = self._orig
        import shutil as _sh
        _sh.rmtree(self._tmp, ignore_errors=True)

    def test_import_row_lifecycle(self):
        db = self.agent_db
        iid = db.add_model_import(None, 'org/repo', 'm', scw_model_id='uuid1',
                                  status='preparing')
        rows = db.get_model_imports()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['repo_id'], 'org/repo')
        self.assertEqual(rows[0]['status'], 'preparing')

        db.update_model_import(iid, status='ready')
        row = db.get_model_imports(scw_model_id='uuid1')[0]
        self.assertEqual(row['status'], 'ready')
        self.assertIsNone(row['error_message'])
        self.assertEqual(db.get_model_import(iid)['id'], iid)

        db.remove_model_import(iid)
        self.assertEqual(db.get_model_imports(), [])

    def test_error_message_can_be_cleared_to_null(self):
        db = self.agent_db
        iid = db.add_model_import(None, 'org/r', 'm', scw_model_id='u', status='error')
        db.update_model_import(iid, error_message='boom')
        self.assertEqual(db.get_model_import(iid)['error_message'], 'boom')
        # Explicit None must clear the column (previously impossible).
        db.update_model_import(iid, error_message=None)
        self.assertIsNone(db.get_model_import(iid)['error_message'])

    def test_update_without_fields_is_noop(self):
        db = self.agent_db
        iid = db.add_model_import(None, 'org/r', 'm', scw_model_id='u')
        db.update_model_import(iid)  # must not raise
        self.assertEqual(db.get_model_import(iid)['status'], 'preparing')

    def test_find_active_model_import_prefers_ready(self):
        db = self.agent_db
        db.add_model_import(None, 'org/r', 'm1', scw_model_id='u1', status='ready')
        self.assertEqual(db.find_active_model_import('org/r')['scw_model_id'], 'u1')
        # A failed import is not reusable.
        db.add_model_import(None, 'org/f', 'm', scw_model_id='u2', status='failed')
        self.assertIsNone(db.find_active_model_import('org/f'))

    def test_find_active_skips_newer_failed_row(self):
        # A newer FAILED row must be skipped in favour of an older usable row.
        db = self.agent_db
        db.add_model_import(None, 'org/r', 'old', scw_model_id='u-old', status='preparing')
        db.add_model_import(None, 'org/r', 'new', scw_model_id='u-fail', status='failed')
        found = db.find_active_model_import('org/r')
        self.assertEqual(found['scw_model_id'], 'u-old')
        # A newer READY row wins over an older one.
        db.add_model_import(None, 'org/r', 'newest', scw_model_id='u-ready', status='ready')
        self.assertEqual(db.find_active_model_import('org/r')['scw_model_id'], 'u-ready')

    def test_find_latest_model_import_returns_failed(self):
        db = self.agent_db
        db.add_model_import(None, 'org/r', 'm1', scw_model_id='u1', status='ready')
        db.add_model_import(None, 'org/r', 'm2', scw_model_id='u2', status='failed')
        latest = db.find_latest_model_import('org/r')
        self.assertEqual(latest['model_name'], 'm2')
        self.assertEqual(latest['status'], 'failed')
        self.assertIsNone(db.find_latest_model_import('org/absent'))

    def test_ensure_model_import_backfills_once(self):
        db = self.agent_db
        rid = db.ensure_model_import(None, 'org/x', 'lib-name',
                                     'uuid-x', 'ready', size_bytes=123)
        self.assertIsNotNone(rid)
        row = db.find_latest_model_import('org/x')
        self.assertEqual(row['model_name'], 'lib-name')
        self.assertEqual(row['scw_model_id'], 'uuid-x')
        # Second call is a no-op when a row already exists.
        self.assertIsNone(db.ensure_model_import(None, 'org/x', 'other', 'uuid-y', 'ready'))

    def test_delete_project_clears_imports(self):
        db = self.agent_db
        db.upsert_project('tmp-proj', 'tmp-proj', '/tmp/tmp-proj')
        conn = db.get_db()
        pid = conn.execute("SELECT id FROM projects WHERE slug='tmp-proj'").fetchone()['id']
        conn.close()
        db.add_model_import(pid, 'org/r', 'm', scw_model_id='u', status='ready')
        db.delete_project(pid)  # must not raise FK constraint failure
        self.assertEqual(db.get_model_imports(project_id=pid), [])


class RecommendedNodeForOptionsTests(unittest.TestCase):
    def _recommend(self, options):
        import agent_api
        return agent_api._recommended_node_for_options(options)

    def test_empty_options(self):
        self.assertEqual(self._recommend([]), ('L4', 0.93))

    def test_single_in_stock_node(self):
        options = [{'node_types': ['L4'], 'stock_status': {'L4': 'available'}}]
        self.assertEqual(self._recommend(options), ('L4', 0.93))

    def test_cheapest_in_stock_wins_over_cheaper_out_of_stock(self):
        options = [{'node_types': ['L4', 'H100'],
                    'stock_status': {'L4': 'out_of_stock', 'H100': 'available'}}]
        self.assertEqual(self._recommend(options), ('H100', 3.40))

    def test_falls_back_to_cheapest_when_none_in_stock(self):
        options = [{'node_types': ['L4', 'H100'],
                    'stock_status': {'L4': 'out_of_stock', 'H100': 'out_of_stock'}}]
        self.assertEqual(self._recommend(options), ('L4', 0.93))

    def test_cheapest_in_stock_across_models(self):
        options = [
            {'node_types': ['L40S'], 'stock_status': {'L40S': 'available'}},
            {'node_types': ['L4'], 'stock_status': {'L4': 'available'}},
        ]
        self.assertEqual(self._recommend(options), ('L4', 0.93))

    def test_missing_stock_status_falls_back_to_cheapest(self):
        options = [{'node_types': ['H100']}]
        self.assertEqual(self._recommend(options), ('H100', 3.40))

    def test_unknown_node_type_uses_default_rate(self):
        options = [{'node_types': ['WAT'], 'stock_status': {'WAT': 'available'}}]
        self.assertEqual(self._recommend(options), ('WAT', 0.93))


if __name__ == '__main__':
    unittest.main()
