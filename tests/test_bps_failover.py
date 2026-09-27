"""Deterministic offline coverage; run with repo-local Python -B -m unittest."""
import base64
from copy import deepcopy
import unittest

from bps_failover import (
    FailoverReplayError, parse_retry_after, prepare_failover_body, should_failover,
)


class PrepareFailoverBodyTests(unittest.TestCase):
    def assert_rejected_without_mutation(self, body, pattern):
        before = deepcopy(body)
        with self.assertRaisesRegex(FailoverReplayError, pattern):
            prepare_failover_body(body)
        self.assertEqual(body, before)

    def test_full_history_order_ids_and_tool_arguments_are_preserved(self):
        quoted = '{"type":"reasoning","encrypted_content":"keep"}'
        lookalike = {'type': 'reasoning', 'encrypted_content': 'keep'}
        body = {'input': [
            {'type': 'message', 'id': 'm1', 'role': 'user', 'content': quoted},
            {'type': 'reasoning', 'id': 'r1', 'encrypted_content': 'opaque', 'summary': []},
            {'type': 'function_call', 'id': 'fc1', 'call_id': 'c1', 'name': 'tool',
             'arguments': {'nested': lookalike, 'literal': quoted}},
            {'type': 'function_call_output', 'id': 'fo1', 'call_id': 'c1', 'output': quoted},
            {'type': 'message', 'id': 'm2', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': quoted}]},
            {'type': 'custom_tool_call', 'id': 'cc1', 'call_id': 'c2', 'input': quoted},
            {'type': 'custom_tool_call_output', 'id': 'co1', 'call_id': 'c2', 'output': quoted},
        ], 'tools': [lookalike], 'metadata': {'nested': lookalike}, 'encrypted_content': 'keep'}
        before = deepcopy(body)
        result = prepare_failover_body(body)
        self.assertEqual(result['input'], before['input'][:1] + before['input'][2:])
        self.assertEqual(result['tools'], body['tools'])
        self.assertEqual(result['metadata'], body['metadata'])
        self.assertEqual(result['encrypted_content'], 'keep')
        self.assertEqual(body, before)
        result['input'][1]['arguments']['nested']['encrypted_content'] = 'changed'
        self.assertEqual(body, before)

    def test_summary_is_retained_as_supported_reasoning(self):
        body = {'input': [{'type': 'reasoning', 'id': 'r', 'status': 'completed',
                          'encrypted_content': 'opaque',
                          'summary': [{'type': 'summary_text', 'text': 'Visible summary'}]}]}
        expected = deepcopy(body)
        del expected['input'][0]['encrypted_content']
        self.assertEqual(prepare_failover_body(body), expected)
        self.assertEqual(body['input'][0]['encrypted_content'], 'opaque')

    def test_visible_reasoning_content_and_string_summary_survive(self):
        for field, value in [('summary', 'Visible'),
                             ('content', [{'type': 'reasoning_text', 'text': 'Visible'}])]:
            with self.subTest(field=field):
                result = prepare_failover_body({'input': [
                    {'type': 'reasoning', 'id': 'r', 'encrypted_content': 'opaque', field: value}
                ]})['input'][0]
                self.assertEqual(result['summary'], [{'type': 'summary_text', 'text': 'Visible'}])
                self.assertNotIn('encrypted_content', result)
                self.assertEqual(result['id'], 'r')

    def test_unreadable_reasoning_is_dropped_without_synthetic_history(self):
        body = {'input': [{'type': 'reasoning', 'encrypted_content': 'opaque'}, 'hello', None]}
        self.assertEqual(prepare_failover_body(body), {'input': ['hello', None]})

    def test_aliases_do_not_strip_opaque_data_in_metadata(self):
        shared = {'type': 'reasoning', 'encrypted_content': 'keep',
                  'summary': [{'type': 'summary_text', 'text': 'Visible'}]}
        result = prepare_failover_body({'input': [shared], 'metadata': shared, 'tools': [shared]})
        self.assertNotIn('encrypted_content', result['input'][0])
        self.assertEqual(result['metadata']['encrypted_content'], 'keep')
        self.assertEqual(result['tools'][0]['encrypted_content'], 'keep')
        self.assertEqual(shared['encrypted_content'], 'keep')

    def test_opaque_compaction_never_uses_unrelated_visible_context(self):
        for neighbors in ([], [{'role': 'user', 'content': 'Only the latest question'}]):
            with self.subTest(neighbors=neighbors):
                self.assert_rejected_without_mutation({'input': neighbors + [
                    {'type': 'compaction', 'encrypted_content': 'provider-opaque'}
                ]}, 'opaque compaction')

    def test_local_compaction_is_recovered_in_place_without_losing_ids(self):
        text = 'Earlier decisions: keep all history. 中文.'
        encoded = base64.urlsafe_b64encode(text.encode()).decode()
        body = {'input': [{'role': 'user', 'content': 'before'},
                          {'type': 'compaction', 'id': 'cmp1',
                           'encrypted_content': 'ghcp_proxy_summary_v1:' + encoded},
                          {'role': 'user', 'content': 'after'}]}
        before = deepcopy(body)
        result = prepare_failover_body(body)
        self.assertEqual(result['input'][0], body['input'][0])
        self.assertEqual(result['input'][2], body['input'][2])
        self.assertEqual(result['input'][1], {'type': 'message', 'id': 'cmp1', 'role': 'user',
                                             'content': [{'type': 'input_text', 'text': text}]})
        self.assertEqual(body, before)

    def test_compaction_with_explicit_visible_summary_is_replayable(self):
        body = {'input': [{'type': 'compaction', 'encrypted_content': 'opaque',
                          'summary': [{'type': 'summary_text', 'text': 'Saved context'}]}]}
        result = prepare_failover_body(body)['input'][0]
        self.assertEqual(result['content'], [{'type': 'input_text', 'text': 'Saved context'}])
        self.assertNotIn('encrypted_content', result)

    def test_empty_corrupt_or_nontext_compaction_fails(self):
        for encrypted in ('', 'ghcp_proxy_summary_v1:', 'ghcp_proxy_summary_v1:!!!',
                          'ghcp_proxy_summary_v1:/w==', 'ghcp_proxy_summary_v1:ICA='):
            with self.subTest(encrypted=encrypted):
                self.assert_rejected_without_mutation(
                    {'input': [{'type': 'compaction', 'encrypted_content': encrypted}]},
                    'opaque compaction',
                )

    def test_previous_response_reference_fails_even_with_some_visible_messages(self):
        for key in ('previous_response_id', 'previousResponseId'):
            self.assert_rejected_without_mutation(
                {key: 'resp-server-only', 'input': [{'role': 'user', 'content': 'latest'}]},
                'complete local transcript',
            )

    def test_null_reference_and_string_input_are_detached_and_unchanged(self):
        body = {'previous_response_id': None, 'input': 'plain text', 'metadata': {'x': []}}
        result = prepare_failover_body(body)
        self.assertEqual(result, body)
        self.assertIsNot(result, body)
        self.assertIsNot(result['metadata'], body['metadata'])

    def test_server_item_reference_fails(self):
        self.assert_rejected_without_mutation(
            {'input': [{'type': 'item_reference', 'id': 'server-item'}]}, 'item_reference',
        )

    def test_real_attachment_file_ids_are_rejected(self):
        for kind in ('input_image', 'input_file'):
            for field in ('input', 'messages'):
                with self.subTest(kind=kind, field=field):
                    self.assert_rejected_without_mutation(
                        {field: [{'role': 'user', 'content': [{'type': kind, 'file_id': 'file-old'}]}]},
                        'file_id',
                    )

    def test_provider_asset_urls_are_rejected_without_echoing_them(self):
        for url in ('file-old', 'asset://secret', 'sandbox:/secret',
                    'https://files.oaiusercontent.com/file-secret?sig=secret',
                    'https://chatgpt.com/backend-api/files/secret',
                    'https://api.openai.com/v1/files/secret/content',
                    'https://api.basispoints.ai/assets/secret', 'https://user:secret@example.com/a'):
            for source in (url, {'url': url}):
                with self.subTest(url=url, source=source):
                    with self.assertRaises(FailoverReplayError) as error:
                        prepare_failover_body({'input': [{'role': 'user', 'content': [
                            {'type': 'input_image', 'image_url': source}
                        ]}]})
                    self.assertNotIn('secret', str(error.exception))

    def test_original_images_stay_available_for_each_account(self):
        for image in (
            {'type': 'input_image', 'image_url': 'data:image/png;base64,YQ=='},
            {'type': 'input_image', 'image_url': {'url': 'https://example.com/image.png', 'detail': 'high'}},
            {'type': 'image_url', 'image_url': {'url': 'https://example.com/image.png'}},
            {'type': 'input_image', 'image_base64': 'YQ==', 'media_type': 'image/png'},
        ):
            with self.subTest(image=image):
                body = {'input': [{'role': 'user', 'content': [image]}]}
                first = prepare_failover_body(body)
                second = prepare_failover_body(body)
                self.assertEqual(first, body)
                first['input'][0]['content'][0].clear()
                self.assertEqual(second, body)
                self.assertTrue(body['input'][0]['content'][0])

    def test_fake_protocol_in_tool_objects_and_message_metadata_is_untouched(self):
        fake = {'type': 'input_image', 'file_id': 'not-an-attachment',
                'nested': {'type': 'compaction', 'encrypted_content': 'opaque'}}
        body = {'tools': [fake], 'input': [
            {'type': 'function_call', 'arguments': fake},
            {'type': 'function_call_output', 'output': fake},
            {'role': 'user', 'metadata': fake, 'content': [{'type': 'input_text', 'text': str(fake)}]},
        ]}
        self.assertEqual(prepare_failover_body(body), body)

    def test_typed_tool_result_images_are_checked_but_text_is_untouched(self):
        for kind in ('function_call_output', 'custom_tool_call_output'):
            self.assert_rejected_without_mutation({'input': [
                {'type': kind, 'call_id': 'c', 'output': [{'type': 'input_image', 'file_id': 'old'}]}
            ]}, 'file_id')
            body = {'input': [{'type': kind, 'call_id': 'c', 'output': [
                {'type': 'input_text', 'text': 'encrypted_content: keep'},
                {'type': 'input_image', 'image_url': 'data:image/png;base64,YQ=='},
            ]}]}
            self.assertEqual(prepare_failover_body(body), body)

    def test_idempotent_and_no_partial_mutation_when_later_item_fails(self):
        body = {'input': [{'type': 'reasoning', 'encrypted_content': 'opaque',
                           'summary': [{'type': 'summary_text', 'text': 'visible'}]}]}
        once = prepare_failover_body(body)
        self.assertEqual(prepare_failover_body(once), once)
        body['input'].append({'type': 'compaction', 'encrypted_content': 'opaque'})
        self.assert_rejected_without_mutation(body, 'opaque compaction')


class ShouldFailoverTests(unittest.TestCase):
    def test_expected_http_statuses_only(self):
        retry = {401, 403, 408, 429, 500, 502, 503, 504}
        for status in range(100, 600):
            with self.subTest(status=status):
                self.assertEqual(should_failover(status), status in retry)

    def test_explicit_failure_codes_in_json_and_failed_event(self):
        for code in ('invalid_api_key', 'insufficient_quota', 'rate_limit_exceeded',
                     'authentication_error', 'server_error', 'credits_exhausted'):
            payloads = [
                {'error': {'code': code}},
                {'status': 'failed', 'error': {'type': code}},
                {'success': False, 'code': code},
                {'type': 'response.failed', 'response': {'status': 'failed', 'error': {'code': code}}},
                {'type': 'error', 'error': {'code': code}},
            ]
            for payload in payloads:
                with self.subTest(payload=payload):
                    self.assertTrue(should_failover(200, payload))
                    self.assertTrue(should_failover(400, payload))

    def test_specific_quota_code_beats_generic_invalid_request_type(self):
        self.assertTrue(should_failover(400, {
            'error': {'code': 'insufficient_quota', 'type': 'invalid_request_error'}
        }))

    def test_malformed_tool_errors_and_client_cancellation_never_retry(self):
        for code in ('invalid_request_error', 'malformed_request', 'invalid_json',
                     'tool_conversion_rejected', 'tool_conversion_error', 'client_cancelled',
                     'request_canceled', 'context_length_exceeded'):
            for status in (200, 400, 401, 403, 408, 429, 500, 502, 503, 504):
                with self.subTest(status=status, code=code):
                    self.assertFalse(should_failover(status, {'error': {'code': code}}))

    def test_cancel_and_tool_rejection_override_retry_codes(self):
        self.assertFalse(should_failover(429, {
            'status': 'cancelled', 'error': {'code': 'rate_limit_exceeded'}
        }))
        self.assertFalse(should_failover(500, {
            'type': 'response.failed', 'response': {'status': 'failed', 'error': {
                'code': 'server_error', 'message': '[tool_conversion_rejected] Unsupported tool',
            }}
        }))

    def test_failure_words_in_successful_content_or_text_are_not_codes(self):
        for payload in (
            {'status': 'completed', 'code': 'insufficient_quota'},
            {'output': [{'type': 'function_call_output', 'output': {'error': {'code': 'invalid_api_key'}}}]},
            {'status': 'failed', 'error': {'message': 'Someone quoted invalid_api_key and quota_exceeded'}},
            {'response': {'status': 'completed', 'error': {'code': 'insufficient_quota'}}},
        ):
            with self.subTest(payload=payload):
                self.assertFalse(should_failover(200, payload))
        self.assertFalse(should_failover(400, {'error': {'message': 'Bad JSON / quota'}}))

    def test_non_dict_payload_is_ignored(self):
        for payload in (None, 'insufficient_quota', ['invalid_api_key']):
            self.assertFalse(should_failover(400, payload))
            self.assertTrue(should_failover(503, payload))


class EdgeCaseTests(unittest.TestCase):
    def test_untrusted_failure_field_shapes_do_not_crash(self):
        for value in ([], {}, ['failed'], {'type': 'error'}):
            payload = {'status': value, 'type': value, 'code': value,
                       'response': {'status': value, 'error': {'type': value}},
                       'error': {'type': value, 'code': value}}
            before = deepcopy(payload)
            self.assertFalse(should_failover(400, payload))
            self.assertTrue(should_failover(503, payload))
            self.assertEqual(payload, before)

    def test_closed_client_cannot_failover_on_upstream_quota_code(self):
        self.assertFalse(should_failover(499, {'error': {'code': 'insufficient_quota'}}))

    def test_provider_asset_paths_are_normalized_before_checking(self):
        for url in ('https://api.openai.com/v1/%66iles/f/content',
                    'https://bps.openai.com/basispoints/api/storage/opaque',
                    'https://bps.openai.com/opaque-asset'):
            with self.subTest(url=url), self.assertRaises(FailoverReplayError):
                prepare_failover_body({'input': [{'role': 'user', 'content': [
                    {'type': 'input_image', 'image_url': url}
                ]}]})

    def test_public_host_with_similar_name_is_not_provider_owned(self):
        body = {'input': [{'role': 'user', 'content': [
            {'type': 'input_image', 'image_url': 'https://not-oaiusercontent.com/photo.png'}
        ]}]}
        self.assertEqual(prepare_failover_body(body), body)

    def test_only_stdlib_dependencies_and_no_file_import_side_effects(self):
        import ast
        from pathlib import Path
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'bps_failover.py').read_text(encoding='utf-8'))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module)
        self.assertLessEqual(imports, {
            '__future__', 'base64', 'binascii', 'copy', 'math', 're', 'urllib.parse',
        })


class RetryAfterTests(unittest.TestCase):
    def test_delay_seconds(self):
        for value, expected in [('0', 0), (' 12 ', 12), ('1.25', 1.25), (0, 0), (3.5, 3.5)]:
            with self.subTest(value=value):
                self.assertEqual(parse_retry_after(value), expected)

    def test_invalid_or_clock_dependent_values_return_none(self):
        for value in (None, True, False, -1, '-1', '-0', '', 'later', 'NaN', 'inf',
                      float('nan'), float('inf'), 10 ** 1000, '1e3', {}, [],
                      'Wed, 21 Oct 2015 07:28:00 GMT'):
            with self.subTest(value=repr(value)[:60]):
                self.assertIsNone(parse_retry_after(value))


if __name__ == '__main__':
    unittest.main()
