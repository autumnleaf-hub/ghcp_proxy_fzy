"""Offline regressions for proxy-generated tool rejection, without a live server."""
import asyncio
import json
import unittest

import excel_upstream as upstream
import format_translation as ft
from test_codex_tool_compat import source_tools, transport


class CodexTransportRecoveryTests(unittest.TestCase):
    def collect(self, output, terminal='response.completed'):
        import proxy
        response = {'id': 'resp_recovery', 'status': terminal.split('.')[1], 'output': output}
        if terminal == 'response.failed':
            response['error'] = {'code': 'rate_limit_exceeded', 'message': 'Upstream throttled'}
        chunks = []
        for index, item in enumerate(output):
            chunks.append(ft.sse_encode('response.output_item.added', {
                'type': 'response.output_item.added', 'output_index': index, 'item': item}))
        chunks.append(ft.sse_encode(terminal, {'type': terminal, 'response': response}))
        wire = b''.join(chunks)

        async def source():
            for offset in range(0, len(wire), 17):
                yield wire[offset:offset + 17]

        async def run():
            transformed = proxy._excel_tool_stream_transform(source_tools())(source())
            return [(name, json.loads(data)) async for name, data in ft.iter_sse_messages(transformed)]

        return asyncio.run(run())

    def test_direct_namespace_call_preserves_identity(self):
        native = {'type': 'function_call', 'id': 'fc_namespace', 'call_id': 'call_namespace',
                  'name': 'js', 'namespace': 'browser',
                  'arguments': json.dumps({'code': 'await tab.snapshot();'})}
        calls = upstream.extract_native_client_tool_calls({'output': [native]}, source_tools())
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]['namespace'], calls[0]['name']), ('browser', 'js'))

    def test_known_host_prefix_is_resolved_without_guessing_unknown_names(self):
        valid = transport('functions.browser.js', {'code': 'await tab.snapshot();'}, 'call_prefix')
        calls = upstream.extract_native_client_tool_calls({'output': [valid]}, source_tools())
        self.assertEqual((calls[0]['namespace'], calls[0]['name']), ('browser', 'js'))
        invalid = transport('functions.unknown_tool', {}, 'call_unknown_prefix')
        self.assertEqual(upstream.extract_native_client_tool_calls({'output': [invalid]}, source_tools()), [])

    def test_exact_catalog_name_wins_over_host_prefix_alias(self):
        names = {'functions.exec_command': 'function', 'exec_command': 'function'}
        self.assertEqual(upstream._original_client_tool_name('functions.exec_command', names), 'functions.exec_command')

    def test_envelope_namespace_is_preserved(self):
        native = transport('js', {'code': 'await tab.snapshot();'}, 'call_envelope_namespace')
        args = json.loads(native['arguments'])
        envelope = json.loads(args['code'])
        envelope['namespace'] = 'browser'
        args['code'] = json.dumps(envelope)
        native['arguments'] = json.dumps(args)
        calls = upstream.extract_native_client_tool_calls({'output': [native]}, source_tools())
        self.assertEqual((calls[0]['namespace'], calls[0]['name']), ('browser', 'js'))

    def test_rejected_batch_preserves_output_indexes_and_finishes_once(self):
        reasoning = {'type': 'reasoning', 'id': 'rs_recovery', 'summary': []}
        note = {'type': 'message', 'id': 'msg_existing', 'role': 'assistant',
                'content': [{'type': 'output_text', 'text': 'Preparing', 'annotations': []}]}
        output = [reasoning, transport('browser.js', {'code': 'ok'}, 'call_valid'),
                  note, transport('not_available', {}, 'call_invalid')]
        events = self.collect(output)
        self.assertFalse(any(name == 'response.failed' for name, _ in events))
        added = [data for name, data in events if name == 'response.output_item.added']
        self.assertFalse(any(data['item']['type'] in ('function_call', 'custom_tool_call') for data in added))
        completed = [data['response'] for name, data in events if name == 'response.completed']
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(completed[0]['output']), 4)
        for data in added:
            self.assertEqual(data['item']['id'], completed[0]['output'][data['output_index']]['id'])
        self.assertEqual(completed[0]['output'][2]['id'], 'msg_existing')
        self.assertIn('[tool_conversion_rejected]', completed[0]['output'][1]['content'][0]['text'])

    def test_real_upstream_failures_are_not_reported_as_success(self):
        invalid = transport('unknown_tool', {}, 'call_failed_upstream')
        for terminal in ('response.failed', 'response.incomplete'):
            with self.subTest(terminal=terminal):
                events = self.collect([invalid], terminal)
                self.assertFalse(any(name == 'response.completed' for name, _ in events))
                self.assertFalse(any(name == 'response.output_item.added' for name, _ in events))
                done = [data for name, data in events if name == terminal]
                self.assertEqual(len(done), 1)
                self.assertEqual(done[0]['response']['output'], [])
                if terminal == 'response.failed':
                    self.assertEqual(done[0]['response']['error']['code'], 'rate_limit_exceeded')

    def test_diagnostics_never_log_argument_values(self):
        import proxy
        invalid = transport('browser.js', {'code': 123, 'secret': 'DO_NOT_LOG_TOKEN'}, 'call_bad_args')
        with self.assertLogs('proxy', level='WARNING') as logs:
            response = proxy._recoverable_excel_tool_response({'output': [invalid]}, source_tools())
        rendered = json.dumps(response) + ' '.join(logs.output)
        self.assertNotIn('DO_NOT_LOG_TOKEN', rendered)
        self.assertIn('invalid_client_tool_arguments', rendered)


if __name__ == '__main__':
    unittest.main()
