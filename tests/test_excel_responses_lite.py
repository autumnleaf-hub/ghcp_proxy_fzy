"""Focused offline regressions for Responses Lite client tool declarations."""
import copy
import json
import unittest

import excel_upstream as bridge


class ResponsesLiteTools(unittest.TestCase):
    def setUp(self):
        self.exec_spec = {'type': 'custom', 'name': 'exec', 'format': {'type': 'text'}}
        self.js_spec = {'type': 'function', 'name': 'js', 'parameters': {
            'type': 'object', 'required': ['code'],
            'properties': {'code': {'type': 'string'}}, 'additionalProperties': False}}
        self.declarations = [
            {'type': 'namespace', 'name': 'functions', 'tools': [self.exec_spec]},
            {'type': 'namespace', 'name': 'mcp__cua_repl', 'tools': [self.js_spec]}]
        self.source = {'input': [{'type': 'additional_tools', 'role': 'developer',
                                 'tools': self.declarations}]}

    def custom(self, name='exec', **extra):
        return {'type': 'custom_tool_call', 'id': 'ctc_lite_probe',
                'call_id': 'call_lite_probe', 'name': name, 'input': 'read-only probe', **extra}

    def function(self, code='read-only probe', **extra):
        return {'type': 'function_call', 'id': 'fc_lite_probe',
                'call_id': 'call_lite_probe', 'name': 'js', 'namespace': 'mcp__cua_repl',
                'arguments': json.dumps({'code': code}), **extra}

    def extract(self, call, source=None):
        return bridge.extract_native_client_tool_call(
            {'output': [call]}, self.source if source is None else source, remember=False)

    def test_lite_and_top_level_catalogs_agree(self):
        top = {'tools': self.declarations}
        self.assertEqual(bridge.client_tool_types(self.source), bridge.client_tool_types(top))
        self.assertEqual(bridge._client_tool_specs(self.source), bridge._client_tool_specs(top))
        self.assertEqual(bridge._client_tool_protocol_instructions(self.source),
                         bridge._client_tool_protocol_instructions(top))
        self.assertEqual(bridge.client_tool_catalog_diagnostic(self.source),
                         bridge.client_tool_catalog_diagnostic(top))

    def test_original_exec_failure_accepts_default_namespace(self):
        call = self.custom()
        self.assertEqual(bridge.client_tool_rejection_diagnostics({'output': [call]}, self.source), [])
        result = self.extract(call)
        self.assertEqual((result['name'], result['namespace'], result['input']),
                         ('exec', 'functions', 'read-only probe'))

    def test_explicit_default_namespace_is_preserved(self):
        result = self.extract(self.custom(namespace='functions'))
        self.assertEqual(result['namespace'], 'functions')

    def test_original_browser_failure_accepts_declared_namespace(self):
        call = self.function()
        self.assertEqual(bridge.client_tool_rejection_diagnostics({'output': [call]}, self.source), [])
        self.assertEqual(self.extract(call)['namespace'], 'mcp__cua_repl')

    def test_transport_supports_function_and_custom_tools(self):
        for inner, expected in [
            ({'name': 'functions.exec', 'input': 'read-only probe'}, 'custom_tool_call'),
            ({'name': 'mcp__cua_repl.js', 'arguments': {'code': 'read-only probe'}}, 'function_call')]:
            with self.subTest(expected=expected):
                call = {'type': 'function_call', 'id': 'fc_lite_transport',
                        'call_id': 'call_lite_transport', 'name': 'run_officejs',
                        'arguments': json.dumps({'code': json.dumps(inner)})}
                self.assertEqual(self.extract(call)['type'], expected)

    def test_schema_validation_still_rejects_invalid_arguments(self):
        call = self.function(code=123)
        self.assertIsNone(self.extract(call))
        self.assertEqual(bridge.client_tool_rejection_diagnostics(
            {'output': [call]}, self.source)[0]['reason'], 'invalid_client_tool_arguments')

    def test_unknown_tools_and_arbitrary_namespace_aliases_are_rejected(self):
        for call in [self.custom(name='missing'), self.function(namespace='unavailable'),
                     self.function(namespace='')]:
            with self.subTest(call=call):
                self.assertIsNone(self.extract(call))
                self.assertEqual(bridge.client_tool_rejection_diagnostics(
                    {'output': [call]}, self.source)[0]['reason'], 'unknown_client_tool')

    def test_tool_choice_policies_apply_to_lite_catalog(self):
        none = {**self.source, 'tool_choice': 'none'}
        self.assertEqual(bridge.client_tool_types(none), {})
        self.assertIsNone(self.extract(self.custom(), none))
        required = {**self.source, 'tool_choice': 'required'}
        bridge.validate_client_tool_choice(required)
        forced = {**self.source, 'tool_choice': {'type': 'custom', 'name': 'exec'}}
        self.assertEqual(bridge.client_tool_types(forced), {'functions.exec': 'custom'})
        self.assertIsNotNone(self.extract(self.custom(), forced))
        self.assertIsNone(self.extract(self.function(), forced))

    def test_merges_declaration_items_without_mutating_request(self):
        source = {'tools': [self.exec_spec], 'input': [None,
            {'type': 'additional_tools', 'tools': self.declarations[:1]},
            {'type': 'additional_tools', 'tools': self.declarations[1:]},
            {'type': 'additional_tools', 'tools': None},
            {'type': 'message', 'role': 'user', 'tools': [{'type': 'function', 'name': 'fake'}]}]}
        original = copy.deepcopy(source)
        self.assertEqual(set(bridge.client_tool_types(source)),
                         {'exec', 'functions.exec', 'mcp__cua_repl.js'})
        self.assertEqual(source, original)
        self.assertNotIn('namespace', self.extract(self.custom(), source))

    def test_declarations_are_consumed_before_upstream_history(self):
        message = {'type': 'message', 'role': 'user',
                   'content': [{'type': 'input_text', 'text': 'inspect code'}]}
        source = {**self.source, 'model': 'gpt-6-astra-excel',
                  'input': self.source['input'] + [message]}
        original = copy.deepcopy(source)
        prepared = bridge.prepare_responses_body(source)
        self.assertFalse(any(item.get('type') == 'additional_tools' for item in prepared['input']))
        self.assertIn('mcp__cua_repl.js', json.dumps(prepared['input']))
        self.assertEqual(source, original)


def _pipeline_source():
    """A Lite request as the proxy receives it: no top-level tools at all."""
    return {'model': 'gpt-6-sol', 'input': [{'type': 'additional_tools', 'role': 'developer', 'tools': [
        {'type': 'namespace', 'name': 'functions', 'tools': [
            {'type': 'custom', 'name': 'exec', 'format': {'type': 'text'}}]},
        {'type': 'namespace', 'name': 'mcp__cua_repl', 'tools': [
            {'type': 'function', 'name': 'js', 'parameters': {
                'type': 'object', 'required': ['code'],
                'properties': {'code': {'type': 'string'}}, 'additionalProperties': False}}]}]}]}


def _transport_native(call_id, envelope):
    return {'type': 'function_call', 'name': 'run_officejs', 'call_id': call_id,
            'id': 'fc_' + call_id,
            'arguments': json.dumps({'code': json.dumps(envelope)})}


class ResponsesLiteStreamPipeline(unittest.TestCase):
    def test_stream_converts_lite_declared_custom_call(self):
        import asyncio
        import proxy
        import format_translation as ft
        native = _transport_native('call_lite_stream',
                                   {'name': 'functions.exec', 'input': 'read-only probe'})
        response = {'id': 'resp_lite_stream', 'status': 'completed', 'output': [native]}
        chunks = [ft.sse_encode('response.output_item.added',
                                {'type': 'response.output_item.added', 'output_index': 0, 'item': native})]
        chunks.append(ft.sse_encode('response.completed', {'type': 'response.completed', 'response': response}))

        async def source():
            for chunk in chunks:
                yield chunk

        async def run():
            return [(name, json.loads(data)) async for name, data in
                    ft.iter_sse_messages(proxy._excel_tool_stream_transform(_pipeline_source())(source()))]

        events = asyncio.run(run())
        added = [data['item'] for name, data in events
                 if name == 'response.output_item.added' and data['item'].get('type') == 'custom_tool_call']
        self.assertEqual([(item['name'], item.get('namespace')) for item in added],
                         [('exec', 'functions')])
        done = [data for name, data in events if name == 'response.completed']
        self.assertEqual(len(done), 1)
        item = done[0]['response']['output'][0]
        self.assertEqual((item['type'], item['name'], item.get('namespace'), item['input']),
                         ('custom_tool_call', 'exec', 'functions', 'read-only probe'))


class ResponsesLiteNonStreamPipeline(unittest.IsolatedAsyncioTestCase):
    async def test_non_streaming_converts_lite_declared_calls(self):
        import httpx
        import proxy
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        calls = [
            _transport_native('call_lite_nonstream_exec',
                              {'name': 'functions.exec', 'input': 'read-only probe'}),
            _transport_native('call_lite_nonstream_js',
                              {'name': 'mcp__cua_repl.js', 'arguments': {'code': 'read-only probe'}}),
        ]
        payload = {'id': 'resp_lite_nonstream', 'status': 'completed', 'output': calls}
        plan = SimpleNamespace(upstream_url='https://example.invalid/responses', headers={}, body={})
        response = httpx.Response(200, json=payload,
                                  request=httpx.Request('POST', plan.upstream_url))
        async with httpx.AsyncClient() as client:
            with patch.object(proxy, '_get_excel_upstream_client', return_value=client), \
                    patch.object(proxy, 'throttled_client_send', AsyncMock(return_value=response)), \
                    patch.object(proxy, '_finish_usage_and_trace'):
                result = await proxy._post_excel_non_streaming_request(
                    plan, client_body=_pipeline_source())
        self.assertEqual(result.status_code, 200)
        content = json.loads(result.body)
        self.assertEqual([(item['type'], item['name'], item.get('namespace')) for item in content['output']],
                         [('custom_tool_call', 'exec', 'functions'),
                          ('function_call', 'js', 'mcp__cua_repl')])
        self.assertEqual(json.loads(content['output'][1]['arguments']), {'code': 'read-only probe'})


if __name__ == '__main__':
    unittest.main()
