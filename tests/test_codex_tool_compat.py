import json
import unittest
from unittest.mock import patch
import excel_upstream as u

class CodexToolCompatibilityTests(unittest.TestCase):
    def test_namespace_replay_after_cache_miss(self):
        args = {'code': 'await tab.snapshot();'}
        item = {'type': 'function_call', 'name': 'js', 'namespace': 'browser', 'call_id': 'compat_miss', 'arguments': json.dumps(args)}
        with patch.dict(u._native_call_cache, {}, clear=True):
            replay = u.translate_input_items([item])[0]
        self.assertEqual(json.loads(json.loads(replay['arguments'])['code']), {'name': 'browser.js', 'arguments': args})

    def test_nested_namespaces_keep_parent(self):
        source = {'tools': [{'type': 'namespace', 'name': 'outer', 'tools': [{'type': 'namespace', 'name': 'browser', 'tools': [{'type': 'function', 'name': 'js'}]}]}]}
        self.assertEqual(u.client_tool_types(source), {'outer.browser.js': 'function'})

def source_tools():
    return {'model': 'gpt-6-sol', 'tools': [
        {'type': 'namespace', 'name': 'browser', 'tools': [
            {'type': 'function', 'name': 'js', 'parameters': {'type': 'object', 'properties': {'code': {'type': 'string'}}, 'required': ['code']}},
            {'type': 'custom', 'name': 'apply_patch'}]},
        {'type': 'function', 'name': 'exec_command', 'parameters': {'type': 'object', 'properties': {'cmd': {'type': 'string'}}, 'required': ['cmd']}}]}

def transport(name, args, call_id, custom=False):
    envelope = {'name': name, ('input' if custom else 'arguments'): args}
    return {'type': 'function_call', 'name': 'run_officejs', 'call_id': call_id, 'id': 'fc_' + call_id, 'arguments': json.dumps({'code': json.dumps(envelope)})}

class CodexOutputCompatibilityTests(unittest.TestCase):
    def test_namespaced_custom_tool_roundtrip(self):
        raw = chr(10).join(['*** Begin Patch', '*** Add File: 资料/有 空格.txt', '+原始内容', '*** End Patch'])
        native = transport('browser.apply_patch', raw, 'compat_patch', custom=True)
        result = u.extract_native_client_tool_call({'output': [native]}, source_tools())
        self.assertEqual(result['name'], 'apply_patch')
        self.assertEqual(result['namespace'], 'browser')
        self.assertEqual(result['input'], raw)
        with patch.dict(u._native_call_cache, {}, clear=True):
            replay = u.translate_input_items([result])[0]
        self.assertEqual(json.loads(json.loads(replay['arguments'])['code']), {'name': 'browser.apply_patch', 'input': raw})

    def test_tool_output_preserves_file_paths_and_screenshot(self):
        import copy
        output = [{'type': 'input_text', 'text': 'C:' + chr(92) + '资料' + chr(92) + '有 空格.pdf'}, {'type': 'input_image', 'image_url': 'data:image/png;base64,aGVsbG8='}]
        item = {'type': 'function_call_output', 'call_id': 'compat_files', 'output': output}
        before = copy.deepcopy(item)
        self.assertEqual(u.translate_input_items([item])[0]['output'], output)
        self.assertEqual(item, before)

    def test_parallel_native_calls(self):
        calls = [transport('browser.js', {'code': 'await tab.snapshot();'}, 'compat_browser'), transport('exec_command', {'cmd': 'Get-Content -LiteralPath "C:/资料/有 空格.txt"'}, 'compat_shell')]
        reasoning = {'type': 'reasoning', 'id': 'rs_compat', 'summary': []}
        response = {'id': 'resp_compat', 'output': [reasoning, *calls]}
        translated = u.extract_native_client_tool_calls(response, source_tools())
        self.assertEqual([c['name'] for c in translated], ['js', 'exec_command'])
        self.assertEqual(translated[0]['namespace'], 'browser')
        result = u.response_payload_with_tool_calls(response, translated, model_id='gpt-6-sol')
        self.assertEqual(result['output'][0], reasoning)
        self.assertEqual([i['call_id'] for i in result['output'][1:]], ['compat_browser', 'compat_shell'])
        self.assertNotIn('run_officejs', json.dumps(result['output']))

    def test_parallel_stream_completes_once(self):
        import asyncio
        import proxy
        import format_translation as ft
        calls = [transport('browser.js', {'code': 'await tab.snapshot();'}, 'compat_stream_a'), transport('exec_command', {'cmd': 'Get-Content readme.md'}, 'compat_stream_b')]
        response = {'id': 'resp_compat', 'output': [{'type': 'reasoning', 'id': 'rs_compat', 'summary': []}, *calls]}
        chunks = [ft.sse_encode('response.output_item.added', {'type': 'response.output_item.added', 'output_index': i, 'item': item}) for i, item in enumerate(calls, 1)]
        chunks.append(ft.sse_encode('response.completed', {'type': 'response.completed', 'response': response}))
        async def source():
            for chunk in chunks:
                yield chunk
        async def run():
            return [(name, json.loads(data)) async for name, data in ft.iter_sse_messages(proxy._excel_tool_stream_transform(source_tools())(source()))]
        events = asyncio.run(run())
        done = [data for name, data in events if name == 'response.completed']
        added = [data for name, data in events if name == 'response.output_item.added']
        self.assertEqual(len(done), 1)
        self.assertEqual([i['output_index'] for i in added], [1, 2])
        self.assertEqual([i['item']['name'] for i in added], ['js', 'exec_command'])
        self.assertEqual([i['name'] for i in done[0]['response']['output'][1:]], ['js', 'exec_command'])

class CodexReplayCompatibilityTests(unittest.TestCase):
    def test_namespaced_update_plan_is_not_native_update_plan(self):
        item = {'type': 'function_call', 'name': 'update_plan', 'namespace': 'mcp', 'call_id': 'compat_plan', 'arguments': json.dumps({'custom_field': '原始内容'})}
        with patch.dict(u._native_call_cache, {}, clear=True):
            replay = u.translate_input_items([item])[0]
        self.assertEqual(replay['name'], 'run_officejs')
        self.assertEqual(json.loads(json.loads(replay['arguments'])['code'])['name'], 'mcp.update_plan')

    def test_windows_paths_and_multiline_script_roundtrip(self):
        path = chr(92).join(['C:', '资料', '有 空格.pdf'])
        code = chr(10).join(['const path = ' + json.dumps(path, ensure_ascii=False) + ';', 'console.log("文本🙂");'])
        native = transport('browser.js', {'code': code}, 'compat_escaped')
        converted = u.extract_native_client_tool_call({'output': [native]}, source_tools())
        self.assertEqual(json.loads(converted['arguments']), {'code': code})
        with patch.dict(u._native_call_cache, {}, clear=True):
            replay = u.translate_input_items([converted])[0]
        envelope = json.loads(json.loads(replay['arguments'])['code'])
        self.assertEqual(envelope, {'name': 'browser.js', 'arguments': {'code': code}})

    def test_parallel_custom_and_function_results_replay(self):
        raw = chr(10).join(['*** Begin Patch', '*** End Patch'])
        natives = [transport('browser.apply_patch', raw, 'compat_custom', True), transport('exec_command', {'cmd': 'echo ok'}, 'compat_exec')]
        calls = u.extract_native_client_tool_calls({'output': natives}, source_tools())
        self.assertEqual([c['type'] for c in calls], ['custom_tool_call', 'function_call'])
        outputs = [{'type': 'custom_tool_call_output', 'call_id': 'compat_custom', 'output': 'Success'}, {'type': 'function_call_output', 'call_id': 'compat_exec', 'output': [{'type': 'input_text', 'text': 'ok'}]}]
        replay = u.translate_input_items(calls + outputs)
        self.assertEqual(replay[:2], natives)
        self.assertEqual(replay[2]['type'], 'function_call_output')
        self.assertEqual(replay[3]['output'], outputs[1]['output'])

    def test_unknown_tools_cannot_be_translated_as_client_calls(self):
        native = transport('unknown.execute', {'cmd': 'ignored'}, 'compat_unknown')
        self.assertEqual(u.extract_native_client_tool_calls({'output': [native]}, source_tools()), [])

    def test_local_file_attachment_context_survives_preparation(self):
        text = 'Files mentioned by user: C:' + chr(92) + '资料' + chr(92) + '有 空格.pdf'
        source = source_tools()
        source['input'] = [{'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}]
        body = u.prepare_responses_body(source)
        self.assertTrue(any(c.get('text') == text for item in body['input'] for c in item.get('content', []) if isinstance(c, dict)))

class CodexInvalidToolCompatibilityTests(unittest.TestCase):
    def test_invalid_native_tool_stream_recovers_without_dispatch(self):
        import asyncio
        import proxy
        import format_translation as ft
        invalid = transport('unknown.execute', {'cmd': 'ignored'}, 'compat_bad_stream')
        valid = transport('browser.js', {'code': 'await tab.snapshot();'}, 'compat_good_stream')
        for calls in ([invalid], [valid, invalid]):
            with self.subTest(count=len(calls)):
                response = {'id': 'resp_bad', 'status': 'completed', 'output': calls}
                chunks = [ft.sse_encode('response.output_item.added', {'type': 'response.output_item.added', 'output_index': i, 'item': item}) for i, item in enumerate(calls)]
                chunks.append(ft.sse_encode('response.completed', {'type': 'response.completed', 'response': response}))
                async def source():
                    for chunk in chunks:
                        yield chunk
                async def run():
                    return [(name, json.loads(data)) async for name, data in ft.iter_sse_messages(proxy._excel_tool_stream_transform(source_tools())(source()))]
                events = asyncio.run(run())
                self.assertFalse(any(name == 'response.output_item.added' and data['item']['type'] in ('function_call', 'custom_tool_call') for name, data in events))
                self.assertFalse(any(name == 'response.failed' for name, _ in events))
                completed = [data for name, data in events if name == 'response.completed']
                self.assertEqual(len(completed), 1)
                response = completed[0]['response']
                self.assertEqual(response['status'], 'completed')
                self.assertIsNone(response['error'])
                self.assertIn('[tool_conversion_rejected]', response['output'][0]['content'][0]['text'])

class CodexNonStreamingCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_streaming_parallel_and_invalid_calls(self):
        import httpx
        import proxy
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        good = [transport('browser.js', {'code': 'await tab.snapshot();'}, 'compat_nonstream_a'), transport('exec_command', {'cmd': 'echo ok'}, 'compat_nonstream_b')]
        bad = [transport('unknown.execute', {}, 'compat_nonstream_bad')]
        for calls, expected_status in ((good, 200), (bad, 200)):
            with self.subTest(expected_status=expected_status):
                plan = SimpleNamespace(upstream_url='https://example.invalid/responses', headers={}, body={})
                payload = {'id': 'resp_nonstream', 'status': 'completed', 'output': calls}
                response = httpx.Response(200, json=payload, request=httpx.Request('POST', plan.upstream_url))
                async with httpx.AsyncClient() as client:
                    with patch.object(proxy, '_get_excel_upstream_client', return_value=client), patch.object(proxy, 'throttled_client_send', AsyncMock(return_value=response)), patch.object(proxy, '_finish_usage_and_trace'):
                        result = await proxy._post_excel_non_streaming_request(plan, client_body=source_tools())
                self.assertEqual(result.status_code, expected_status)
                content = json.loads(result.body)
                if calls is good:
                    self.assertEqual([i['name'] for i in content['output']], ['js', 'exec_command'])
                    self.assertEqual(content['output'][0]['namespace'], 'browser')
                else:
                    self.assertEqual(content['status'], 'completed')
                    self.assertIsNone(content['error'])
                    self.assertIn('[tool_conversion_rejected]', content['output'][0]['content'][0]['text'])
