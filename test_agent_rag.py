"""Tests for agent_rag.py — legal-intent detection, RAG pre-fetch, provenance labels."""

import json
import unittest
from unittest.mock import patch

import agent_rag


LEGAL_TASK = {
    'description': 'Jakie warunki dla świadectwa maszynisty wg Art. 22b? Podaj Dz.U. i ELI.',
    'title': 'Warunki licencji przewoźnika',
    'requires_rag': 1,
}
GENERAL_TASK = {
    'description': 'Summarize the quarterly budget report and list top costs.',
    'title': 'Budget summary',
}
PROJECT_ON = {'use_rag': 1, 'project_type': 'Legal'}
PROJECT_OFF = {'use_rag': 0, 'project_type': 'Legal'}


class DetectIntentTests(unittest.TestCase):
    def test_explicit_requires_rag_fires(self):
        intent = agent_rag.detect_rag_intent(LEGAL_TASK, PROJECT_ON)
        self.assertIsNotNone(intent)
        self.assertEqual(intent['corpus_id'], 'railway')
        self.assertTrue(intent['requires_rag'])
        self.assertTrue(intent['graph'])          # hierarchy on for legal intent
        self.assertFalse(intent['rerank'])        # cross-encoder off by default

    def test_project_disabled_never_fires(self):
        self.assertIsNone(agent_rag.detect_rag_intent(LEGAL_TASK, PROJECT_OFF))

    def test_no_intent_on_general_task(self):
        self.assertIsNone(
            agent_rag.detect_rag_intent(GENERAL_TASK, {'use_rag': 1, 'project_type': 'Business'})
        )

    def test_eli_marker_does_not_false_positive_on_ordinary_words(self):
        # Regression: task #10001145 — the task's own instructions included the
        # example phrase "Aligns with EU tendering guidelines", and unanchored
        # `ELI` in _LEGAL_REGEX matched "eli" inside "guidelines", auto-firing
        # RAG for a task with no legal content at all.
        task = {
            'description': (
                'Extract the four package-split proposals. Reasoning: '
                'Justification provided in the text (e.g., "Aligns with EU '
                'tendering guidelines"). Also: reliable, deliver, believe.'
            ),
            'title': 'Summarize package split hypotheses',
        }
        self.assertIsNone(
            agent_rag.detect_rag_intent(task, {'use_rag': 1, 'project_type': 'Business'})
        )

    def test_auto_detect_legal_markers(self):
        task = {'description': 'Czy rozporządzenie wykonawcze do art. 22b jest wiążące?', 'title': 'x'}
        intent = agent_rag.detect_rag_intent(task, PROJECT_ON)
        self.assertIsNotNone(intent)
        self.assertFalse(intent['requires_rag'])
        self.assertEqual(intent['reason'], 'auto-detected legal intent')

    def test_legal_project_type_triggers_auto(self):
        task = {'description': 'review contracts', 'title': 'x'}
        intent = agent_rag.detect_rag_intent(task, PROJECT_ON)
        self.assertIsNotNone(intent)
        self.assertEqual(intent['corpus_id'], 'railway')

    def test_explicit_corpus_override(self):
        task = dict(LEGAL_TASK, corpus_id='railway')
        intent = agent_rag.detect_rag_intent(task, PROJECT_ON)
        self.assertEqual(intent['corpus_id'], 'railway')

    def test_focused_query_extraction(self):
        # T1.1 fix: long multi-step task description should yield a focused query,
        # not the whole instruction block. Look for article refs + statute names.
        task = {
            'description': (
                'Step 1: Read the OCR file. Step 2: Analyze whether art. 202 § 6 KSH '
                'and art. 116 Ordynacja podatkowa apply. Step 3: Cite Dz.U. + ELI.'
            ),
            'title': 'Responsibility analysis',
            'requires_rag': 1,
        }
        intent = agent_rag.detect_rag_intent(task, PROJECT_ON)
        q = intent['query']
        q_low = q.lower()
        self.assertLess(len(q), 300, msg=f'query too long: {q}')
        self.assertIn('art. 202', q_low)   # article ref preserved
        self.assertIn('ksh', q_low)         # statute name (abbreviation)
        self.assertNotIn('step 1: read the ocr file', q_low)  # instruction block stripped

    def test_query_strips_placeholder_citations_and_ocr_examples(self):
        # Placeholder/illustrative citations and OCR-flag examples in a task
        # description must NOT leak fake article refs into the prefetch query
        # (observed: task 10001109 picked up "art. 12" from an OCR example and
        # retrieved art. 12 OP instead of art. 116 § 2 OP).
        task = {
            'description': (
                'Cite as (Dz.U. 2023 poz. 1234, ELI: [link]). Flag OCR errors like '
                "[OCR: możliwy błąd w tekście: 'art. 12' → weryfikować z oryginałem]. "
                'Analyze art. 116 § 2 Ordynacji podatkowej.'
            ),
            'title': 'x',
            'requires_rag': 1,
        }
        intent = agent_rag.detect_rag_intent(task, PROJECT_ON)
        q_low = intent['query'].lower()
        self.assertNotIn('art. 12', q_low)          # fake ref from OCR example stripped
        self.assertNotIn('poz. 1234', q_low)        # placeholder citation stripped
        self.assertIn('art. 116', q_low)             # real provision preserved

    def test_query_caps_at_focused_length(self):
        # Very long description with many articles → cap at _RAG_QUERY_CHAR_CAP
        task = {
            'description': ' '.join(f'art. {i}' for i in range(100)),
            'title': 'long',
            'requires_rag': 1,
        }
        intent = agent_rag.detect_rag_intent(task, PROJECT_ON)
        self.assertLessEqual(len(intent['query']), agent_rag._RAG_QUERY_CHAR_CAP)


class AccidentalTriggerTests(unittest.TestCase):
    """Example text must not start a RAG lookup (#10001184, #10001186)."""

    PROJECT = {'use_rag': 1, 'project_type': 'Research'}

    def test_example_citation_does_not_trigger(self):
        task = {'description': 'Extract all clauses cited (e.g., "Art. 12(3) of Regulation Y").'}
        self.assertIsNone(agent_rag.detect_rag_intent(task, self.PROJECT))

    def test_real_citation_still_triggers_and_example_is_left_out_of_query(self):
        task = {'description': 'Check art. 14aa ust. 3 (np. art. 5) of the railway act.'}
        intent = agent_rag.detect_rag_intent(task, self.PROJECT)
        self.assertIsNotNone(intent)
        self.assertIn('14aa', intent['query'])
        self.assertNotIn('art. 5', intent['query'])

    def test_local_keyword_only_at_line_start(self):
        self.assertFalse(agent_rag._has_rag_keyword(
            {'description': 'Cite it as "RAG: [query], Source: [URL]".'}))
        self.assertTrue(agent_rag._has_rag_keyword({'description': 'rag: art. 14aa ust. 3'}))
        self.assertTrue(agent_rag._has_rag_keyword({'description': 'Intro\n  - RAG: prawo'}))


class PrefetchTests(unittest.TestCase):
    def test_prefetch_returns_block_and_provenance(self):
        resp = {
            'via': 'rag_api_hybrid',
            'collection': 'railway_L1L3',
            'citations': [
                {
                    'chunk_id': 'c1', 'layer': 'L2', 'dz_u': 'Dz.U. 2025 poz.1234',
                    'eli': 'https://eli.gov.pl/eli/DU/2025/1234/ogl#art_22b',
                    'celex': None, 'article': '22b', 'ustep': '1',
                    'extract': 'Art. 22b ust. 1 — świadectwo maszynisty uprawnia do prowadzenia pojazdu kolejowego.',
                }
            ],
            'hits': 1,
            'retrieval': {'dense': 50, 'sparse': 30, 'exact_article': 1,
                          'fused': 68, 'reranked': False, 'graph': True,
                          'graph_trace': ['L1 32007L0059 → PL Art.22b']},
        }
        intent = agent_rag.detect_rag_intent(LEGAL_TASK, PROJECT_ON)
        with patch('agent_tools.rag_query', return_value=resp):
            block, prov = agent_rag.prefetch_citations(intent)
        self.assertIn('Retrieved legal context', block)
        self.assertIn('Dz.U. 2025 poz.1234', block)
        self.assertIn('eli.gov.pl', block)
        self.assertIn('POWIĄZANIA', block)       # graph trace rendered
        self.assertEqual(prov['n_hits'], 1)
        self.assertEqual(prov['via'], 'rag_api_hybrid')
        self.assertEqual(prov['dz_u_refs'], ['Dz.U. 2025 poz.1234'])

    def test_prefetch_empty_citations(self):
        intent = agent_rag.detect_rag_intent(LEGAL_TASK, PROJECT_ON)
        with patch('agent_tools.rag_query',
                   return_value={'via': 'rag_api_hybrid', 'citations': [], 'hits': 0}):
            block, prov = agent_rag.prefetch_citations(intent)
        self.assertEqual(block, '')
        self.assertEqual(prov['n_hits'], 0)

    def test_prefetch_error_no_crash(self):
        intent = agent_rag.detect_rag_intent(LEGAL_TASK, PROJECT_ON)
        with patch('agent_tools.rag_query', side_effect=Exception('boom')):
            block, prov = agent_rag.prefetch_citations(intent)
        self.assertEqual(block, '')
        self.assertEqual(prov, {})

    def test_prefetch_none_intent(self):
        block, prov = agent_rag.prefetch_citations(None)
        self.assertEqual(block, '')
        self.assertEqual(prov, {})

    def test_prefetch_forwards_graph_and_rerank(self):
        # T1.3 fix: graph and rerank flags from intent must be forwarded to rag_query.
        intent = agent_rag.detect_rag_intent(LEGAL_TASK, PROJECT_ON)
        intent['rerank'] = True
        intent['graph'] = True
        with patch('agent_tools.rag_query', return_value={'citations': []}) as m:
            agent_rag.prefetch_citations(intent)
        # Find the kwargs passed to rag_query
        m.assert_called_once()
        kwargs = m.call_args.kwargs
        self.assertTrue(kwargs.get('rerank'))
        self.assertTrue(kwargs.get('graph'))


class ProvenanceLabelTests(unittest.TestCase):
    def test_prefetched(self):
        self.assertEqual(
            agent_rag.provenance_label(LEGAL_TASK, PROJECT_ON, True), 'RAG-PREFETCHED')

    def test_tool_called(self):
        self.assertEqual(
            agent_rag.provenance_label(LEGAL_TASK, PROJECT_ON, False, ['read_file', 'rag_query']),
            'RAG-TOOL')

    def test_required_missed(self):
        self.assertEqual(
            agent_rag.provenance_label(LEGAL_TASK, PROJECT_ON, False), 'RAG-REQUIRED')

    def test_off(self):
        self.assertEqual(
            agent_rag.provenance_label(GENERAL_TASK, PROJECT_OFF, False), 'RAG-OFF')

    def test_available_but_no_intent(self):
        self.assertEqual(
            agent_rag.provenance_label(GENERAL_TASK, {'use_rag': 1}, False), 'RAG-OFF')


if __name__ == '__main__':
    unittest.main()
