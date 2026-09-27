import unittest
from unittest import mock

import excel_upstream


class BasisPointsModelAliasTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(excel_upstream, '_UPSTREAM_MODEL_OVERRIDE', '')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_explicit_aliases_resolve_to_same_canonical_model(self):
        for canonical, base in excel_upstream.EXCEL_MODEL_UPSTREAMS.items():
            for requested in (base, canonical, base + '-basispoints',
                              ' OPENAI/' + (base + '-basispoints').upper() + ' '):
                with self.subTest(model=requested):
                    self.assertTrue(excel_upstream.is_excel_model(requested))
                    self.assertEqual(excel_upstream.excel_model_id(requested), canonical)
                    self.assertEqual(excel_upstream.upstream_model_for(requested), base)

    def test_request_body_does_not_forward_basispoints_suffix(self):
        for canonical, base in excel_upstream.EXCEL_MODEL_UPSTREAMS.items():
            with self.subTest(model=base):
                body = excel_upstream.prepare_responses_body(
                    {'model': base + '-basispoints', 'input': 'Hello'})
                self.assertEqual(body['model'], base)
                self.assertEqual(body['model_selection'], 'explicit')

    def test_unknown_suffixes_are_not_silently_rerouted(self):
        for model in ('gpt-6-unknown-basispoints', 'gpt-5.4-basispoints',
                      'gpt-6-astra-basispoints-excel', 'gpt-6-astra-excel-basispoints',
                      'other/gpt-6-astra-basispoints', '', None, {}):
            with self.subTest(model=model):
                self.assertFalse(excel_upstream.is_excel_model(model))

    def test_discovery_includes_aliases_once_and_preserves_input(self):
        original = {'object': 'list', 'data': [
            {'id': 'gpt-6-astra-basispoints', 'owned_by': 'copilot'},
            {'id': 'other-model', 'owned_by': 'copilot'}]}
        merged = excel_upstream.merge_local_models_payload(original)
        self.assertEqual(merged, excel_upstream.merge_local_models_payload(merged))
        ids = [item['id'] for item in merged['data']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {'other-model', *excel_upstream.PUBLIC_MODEL_IDS})
        self.assertEqual(original['data'][0]['owned_by'], 'copilot')
        for model in excel_upstream.BASISPOINTS_MODEL_ALIASES:
            self.assertIn(model, ids)
            self.assertEqual(next(i for i in merged['data'] if i['id'] == model)['owned_by'],
                             'openai-excel')

    def test_capabilities_match_canonical_model(self):
        caps = excel_upstream.merge_local_model_capabilities(None)
        for alias, canonical in excel_upstream.BASISPOINTS_MODEL_ALIASES.items():
            self.assertEqual(caps[alias], caps[canonical])

    def test_astra_reasoning_restriction_applies_to_basispoints_alias(self):
        body = excel_upstream.prepare_responses_body({
            'model': 'gpt-6-astra-basispoints', 'input': 'Hello',
            'reasoning': {'effort': 'low'}})
        self.assertEqual(body['reasoning_effort'], 'medium')


if __name__ == '__main__':
    unittest.main()
