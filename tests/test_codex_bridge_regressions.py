"""Offline Codex bridge regressions; no service/browser/file API calls.

Encrypted reasoning checks establish only local history invariants. They cannot
prove that an opaque upstream ciphertext decrypts or is accepted by upstream.
"""
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import webbrowser
import unittest
from unittest import mock

# Keep incidental Python/runtime output inside the expressly allowed directory.
ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / '.tmp' / 'codex_bridge_regressions'
SCRATCH.mkdir(parents=True, exist_ok=True)
RUNTIME_ENV = {key: str(SCRATCH / key.lower()) for key in
               ('GHCP_CONFIG_DIR', 'GHCP_STATE_DIR', 'GHCP_CACHE_DIR')}


def blocked_network(*args, **kwargs):
    raise AssertionError('OFFLINE REGRESSION: network access is prohibited')


NETWORK_GUARDS = [
    mock.patch.object(socket.socket, 'connect', blocked_network),
    mock.patch.object(socket.socket, 'connect_ex', blocked_network),
    mock.patch.object(socket, 'create_connection', blocked_network),
    mock.patch.object(subprocess, 'Popen', blocked_network),
    mock.patch.object(webbrowser, 'open', blocked_network),
    mock.patch.object(webbrowser, 'open_new', blocked_network),
    mock.patch.object(webbrowser, 'open_new_tab', blocked_network),
]
for _guard in NETWORK_GUARDS:
    _guard.start()
try:
    with mock.patch.dict(os.environ, RUNTIME_ENV):
        import excel_upstream as bridge
finally:
    for _guard in reversed(NETWORK_GUARDS):
        _guard.stop()


LOCAL_TOOLS = [
    {'type': 'function', 'name': 'exec_command', 'description': 'Local shell',
     'parameters': {'type': 'object', 'properties': {'cmd': {'type': 'string'}},
                    'required': ['cmd'], 'additionalProperties': False}},
    {'type': 'function', 'name': 'view_image', 'description': 'Local image',
     'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}},
                    'required': ['path'], 'additionalProperties': False}},
    {'type': 'custom', 'name': 'apply_patch', 'description': 'Local patch',
     'format': {'type': 'text'}},
]


def native_call(call_id='call_offline', name='exec_command', arguments=None, raw=None):
    if arguments is None:
        arguments = {'cmd': 'echo offline'}
    inner = raw if raw is not None else {'name': name, 'arguments': arguments}
    return {'type': 'function_call', 'id': 'fc_' + call_id, 'call_id': call_id,
            'name': 'run_officejs', 'status': 'completed',
            'arguments': json.dumps({'summary': 'Offline fixture',
                'extended_summary': 'Offline fixture never executes a local command',
                'destructive': False, 'references': [],
                'code': json.dumps(inner, ensure_ascii=False)}, ensure_ascii=False)}


class OfflineBase(unittest.TestCase):
    def setUp(self):
        env_patch = mock.patch.dict(os.environ, RUNTIME_ENV)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for guard in NETWORK_GUARDS:
            guard.start()
            self.addCleanup(guard.stop)
        import proxy
        trace_patch = mock.patch.object(proxy, '_append_request_trace')
        self.trace_append = trace_patch.start()
        self.addCleanup(trace_patch.stop)
        with bridge._native_call_cache_lock:
            self.saved_cache = copy.deepcopy(bridge._native_call_cache)
            bridge._native_call_cache.clear()

    def tearDown(self):
        with bridge._native_call_cache_lock:
            bridge._native_call_cache.clear()
            bridge._native_call_cache.update(self.saved_cache)

    def prepare(self, items):
        body = {'model': 'offline-model', 'input': items, 'tools': copy.deepcopy(LOCAL_TOOLS)}
        original = copy.deepcopy(body)
        result = bridge.prepare_responses_body(body)
        self.assertEqual(body, original, 'Request preparation mutated caller-owned input')
        return result

class OfflineHistoryRegressions(OfflineBase):
    def test_local_tool_output_images_and_empty_body_survive_preparation(self):
        image = {'type': 'input_image', 'image_url': 'data:image/png;base64,b2ZmbGluZQ=='}
        for output in ([image], [image, dict(image, detail='high')], '', [],
                       [{'type': 'input_text', 'text': ''}, image]):
            with self.subTest(output=output):
                call = {'type': 'function_call', 'id': 'fc_local', 'call_id': 'call_local',
                        'name': 'view_image', 'arguments': json.dumps({'path': 'E:\\离线\\two images.png'})}
                result = self.prepare([call, {'type': 'function_call_output',
                    'call_id': 'call_local', 'output': copy.deepcopy(output)}])
                outputs = [x for x in result.get('input', []) if x.get('type') == 'function_call_output']
                self.assertEqual(len(outputs), 1)
                if output == '':
                    replay = self.prepare(result['input'])
                    repeated = [x for x in replay['input'] if x.get('type') == 'function_call_output']
                    self.assertEqual(repeated[0]['output'], outputs[0]['output'])
                else:
                    self.assertEqual(outputs[0]['output'], output)

    def test_prepare_does_not_remember_undispatched_native_tool(self):
        self.prepare([native_call('call_not_dispatched')])
        self.assertNotIn('call_not_dispatched', bridge._native_call_cache,
                         'Preparing history must not commit an undispatched native call')




class OfflineCacheRegressions(OfflineBase):
    def test_mixed_valid_invalid_batch_does_not_cache_valid_prefix(self):
        output = [native_call('call_valid_prefix'),
                  native_call('call_invalid_suffix', name='not_in_catalog')]
        result = bridge.extract_native_client_tool_calls({'output': output}, {'tools': LOCAL_TOOLS})
        self.assertEqual(result, [], 'Invalid mixed batch must not be dispatched')
        self.assertEqual(dict(bridge._native_call_cache), {},
                         'Rejected mixed batch polluted the native replay cache')

    def test_diagnostics_do_not_remember_valid_calls(self):
        response = {'output': [native_call('call_diagnostic'),
                              native_call('call_bad_diagnostic', name='not_in_catalog')]}
        bridge.client_tool_rejection_diagnostics(response, {'tools': LOCAL_TOOLS})
        self.assertEqual(dict(bridge._native_call_cache), {},
                         'Read-only rejection diagnostics remembered a native tool')


def run_immediate(coro):
    # These fixtures contain no real asynchronous I/O. Avoid even the loopback
    # socketpair that Windows asyncio.run may create when starting an event loop.
    try:
        unexpected = coro.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coro.close()
    raise AssertionError(f'Fixture unexpectedly scheduled real I/O: {unexpected!r}')


def stream_events(output=None, *, terminal='response.completed', terminal_output='same', done=False):
    import format_translation as ft
    output = output if output is not None else [native_call()]
    events = []
    for index, item in enumerate(output):
        events.extend([
            ('response.output_item.added', {'output_index': index, 'item': item}),
            ('response.function_call_arguments.delta', {'output_index': index,
                'item_id': item['id'], 'delta': item.get('arguments', '')}),
            ('response.function_call_arguments.done', {'output_index': index,
                'item_id': item['id'], 'arguments': item.get('arguments', '')}),
            ('response.output_item.done', {'output_index': index, 'item': item}),
        ])
    if terminal is not None:
        response = {'id': 'resp_offline', 'status': terminal.split('.')[-1],
                    'model': 'offline-model'}
        if terminal_output != 'missing':
            response['output'] = copy.deepcopy(output if terminal_output == 'same' else terminal_output)
        if terminal == 'response.failed':
            response['error'] = {'code': 'rate_limit_exceeded', 'message': 'offline failure'}
        if terminal == 'response.incomplete':
            response['incomplete_details'] = {'reason': 'max_output_tokens'}
        events.append((terminal, {'response': response}))
    wire = b''.join(ft.sse_encode(kind, {'type': kind, **data}) for kind, data in events)
    return wire + (b'data: [DONE]' + bytes([10, 10]) if done else b'')


class OfflineStreamBase(OfflineBase):
    def collect(self, wire, chunk_size=17, *, trace_plan=None, source_body=None):
        import proxy
        import format_translation as ft
        async def source():
            for offset in range(0, len(wire), chunk_size):
                yield wire[offset:offset + chunk_size]
        async def collect():
            transform = proxy._excel_tool_stream_transform(source_body if source_body is not None else {'tools': LOCAL_TOOLS}, trace_plan=trace_plan)
            transformed = transform(source()) if transform is not None else source()
            return [(kind, json.loads(data)) async for kind, data in ft.iter_sse_messages(transformed)
                    if data != '[DONE]']
        with mock.patch('subprocess.Popen', side_effect=blocked_network):
            return run_immediate(collect())

    def assert_no_dispatched_tools(self, events):
        for kind, data in events:
            self.assertNotIn(kind, {'response.function_call_arguments.delta',
                'response.function_call_arguments.done', 'response.custom_tool_call_input.delta',
                'response.custom_tool_call_input.done'}, 'Tool arguments leaked before safe dispatch')
            item = data.get('item', {})
            self.assertNotIn(item.get('type'), {'function_call', 'custom_tool_call'})
            for item in data.get('response', {}).get('output', []) or []:
                self.assertNotIn(item.get('type'), {'function_call', 'custom_tool_call'})
        self.assertEqual(dict(bridge._native_call_cache), {}, 'Undispatched stream populated replay cache')

class OfflineStreamRegressions(OfflineStreamBase):
    def test_completed_empty_output_does_not_flush_native_events(self):
        events = self.collect(stream_events(terminal_output=[]))
        self.assert_no_dispatched_tools(events)
        self.assertIn('incomplete_tool_stream', json.dumps(events, ensure_ascii=False))

    def test_completed_missing_output_does_not_flush_native_events(self):
        events = self.collect(stream_events(terminal_output='missing'))
        self.assert_no_dispatched_tools(events)
        self.assertIn('incomplete_tool_stream', json.dumps(events, ensure_ascii=False))

    def test_eof_without_terminal_never_flushes_native_events(self):
        events = self.collect(stream_events(terminal=None))
        self.assert_no_dispatched_tools(events)
        self.assertFalse(any(kind == 'response.completed' for kind, _ in events),
                         'True upstream EOF was forged into success')

    def test_done_marker_without_terminal_is_not_success(self):
        events = self.collect(stream_events(terminal=None, done=True))
        self.assert_no_dispatched_tools(events)
        self.assertFalse(any(kind == 'response.completed' for kind, _ in events))

    def test_real_failed_stream_preserves_failure_without_native_calls(self):
        events = self.collect(stream_events(terminal='response.failed'))
        self.assert_no_dispatched_tools(events)
        self.assertFalse(any(kind == 'response.completed' for kind, _ in events))
        terminal = [data['response'] for kind, data in events if kind == 'response.failed']
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]['error']['code'], 'rate_limit_exceeded')

    def test_real_incomplete_stream_preserves_failure_without_native_calls(self):
        events = self.collect(stream_events(terminal='response.incomplete'))
        self.assert_no_dispatched_tools(events)
        self.assertFalse(any(kind == 'response.completed' for kind, _ in events))
        terminal = [data['response'] for kind, data in events if kind == 'response.incomplete']
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]['incomplete_details']['reason'], 'max_output_tokens')


class OfflineNonstreamRegressions(OfflineBase):
    def receive(self, status, *, sse=False, valid=True):
        from types import SimpleNamespace
        import httpx
        import proxy
        call = native_call('call_nonstream', name='exec_command' if valid else 'unknown_local_tool')
        payload = {'id': 'resp_nonstream', 'status': status, 'output': [call],
                   'error': {'code': 'rate_limit_exceeded', 'message': 'offline only'} if status == 'failed' else None,
                   'incomplete_details': {'reason': 'max_output_tokens'} if status == 'incomplete' else None}
        request = httpx.Request('POST', 'https://offline.invalid/responses')
        response = (httpx.Response(200, request=request, content=stream_events([call], terminal='response.' + status),
                                  headers={'content-type': 'text/event-stream'}) if sse else
                    httpx.Response(200, request=request, json=payload))
        client = mock.Mock()
        client.build_request.return_value = request
        plan = SimpleNamespace(upstream_url=str(request.url), headers={}, body={})
        with mock.patch.object(proxy, '_get_excel_upstream_client', return_value=client), \
             mock.patch.object(proxy, 'throttled_client_send', new=mock.AsyncMock(return_value=response)) as send, \
             mock.patch.object(proxy, '_finish_usage_and_trace'):
            result = run_immediate(proxy._post_excel_non_streaming_request(plan, client_body={'tools': LOCAL_TOOLS}))
        self.assertEqual(send.await_count, 1)
        return json.loads(result.body)

    def assert_failure_preserved(self, status, **kwargs):
        result = self.receive(status, **kwargs)
        self.assertEqual(result.get('status'), status, result)
        self.assertFalse(any(x.get('type') in {'function_call', 'custom_tool_call'}
                             for x in result.get('output', [])), result)
        self.assertEqual(dict(bridge._native_call_cache), {}, 'Failure remembered undispatched tool')
        if status == 'failed':
            self.assertEqual(result['error']['code'], 'rate_limit_exceeded')
        else:
            self.assertEqual(result['incomplete_details']['reason'], 'max_output_tokens')

    def test_failed_json_does_not_dispatch_or_become_success(self):
        self.assert_failure_preserved('failed')

    def test_incomplete_json_does_not_dispatch_or_become_success(self):
        self.assert_failure_preserved('incomplete')

    def test_failed_sse_to_nonstream_retains_failure(self):
        self.assert_failure_preserved('failed', sse=True)

    def test_incomplete_sse_to_nonstream_retains_failure(self):
        self.assert_failure_preserved('incomplete', sse=True)

    def test_invalid_native_call_does_not_hide_real_json_failure(self):
        for status in ('failed', 'incomplete'):
            with self.subTest(status=status):
                self.assert_failure_preserved(status, valid=False)

class OfflineReplayRegressions(OfflineBase):
    def replay_call(self, native, output, *, cold=False):
        calls = bridge.extract_native_client_tool_calls({'output': [native]}, {'tools': LOCAL_TOOLS})
        self.assertEqual(len(calls), 1)
        if cold:
            with bridge._native_call_cache_lock:
                bridge._native_call_cache.clear()
        kind = 'custom_tool_call_output' if calls[0]['type'] == 'custom_tool_call' else 'function_call_output'
        history = [calls[0], {'type': kind, 'call_id': calls[0]['call_id'], 'output': copy.deepcopy(output)}]
        first = self.prepare(history)
        second = self.prepare(history)
        self.assertEqual(first, second, 'Same replay input changed between preparations')
        replayed = [x for x in first['input'] if x.get('type') == 'function_call']
        outputs = [x for x in first['input'] if x.get('type') == 'function_call_output']
        self.assertEqual(len(replayed), 1)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(replayed[0]['call_id'], calls[0]['call_id'])
        self.assertEqual(outputs[0]['call_id'], calls[0]['call_id'])
        if not cold:
            self.assertEqual(replayed[0], native, 'Warm replay lost original native identity')
        return replayed[0], outputs[0]['output']

    def test_images_multi_images_and_empty_outputs_are_stable_across_replay(self):
        image_a = {'type': 'input_image', 'image_url': 'data:image/png;base64,YQ=='}
        image_b = {'type': 'input_image', 'image_url': 'data:image/png;base64,Yg==', 'detail': 'high'}
        outputs = [[image_a], [image_a, image_b], [], '',
                   [{'type': 'input_text', 'text': ''}, image_a, image_b]]
        for index, output in enumerate(outputs):
            with self.subTest(index=index):
                native = native_call('call_images_' + str(index), name='view_image',
                                     arguments={'path': 'E:' + chr(92) + '离线图.png'})
                _, warm = self.replay_call(native, output)
                _, cold = self.replay_call(native, output, cold=True)
                self.assertEqual(warm, cold, 'Cache miss changed tool output replay')
                if output != '':
                    self.assertEqual(warm, output)

    def test_windows_local_path_survives_warm_and_cold_replay(self):
        path = chr(92).join(['E:', '离线 workspace', 'images (2)', 'a #1.png'])
        native = native_call('call_path', name='view_image', arguments={'path': path})
        for cold in (False, True):
            with self.subTest(cold=cold):
                restored, _ = self.replay_call(native, [], cold=cold)
                inner = json.loads(json.loads(restored['arguments'])['code'])
                self.assertEqual(inner['name'], 'view_image')
                self.assertEqual(inner['arguments'], {'path': path})

    def test_raw_custom_input_survives_warm_and_cold_replay(self):
        raw_input = chr(10).join(['*** Begin Patch', '*** Add File: local only.txt',
                                 '+离线 fixture; not executed', '*** End Patch', ''])
        native = native_call('call_custom', raw={'name': 'apply_patch', 'input': raw_input})
        for cold in (False, True):
            with self.subTest(cold=cold):
                restored, output = self.replay_call(native, 'offline result', cold=cold)
                inner = json.loads(json.loads(restored['arguments'])['code'])
                self.assertEqual(inner, {'name': 'apply_patch', 'input': raw_input})
                self.assertEqual(output, 'offline result')

    def test_recovery_does_not_replay_rejected_turn_encrypted_reasoning(self):
        import proxy
        ciphertext = 'opaque-rejected-turn-not-a-real-upstream-token'
        reasoning = {'type': 'reasoning', 'id': 'rs_rejected', 'summary': [],
                     'encrypted_content': ciphertext}
        rejected = {'id': 'resp_rejected', 'status': 'completed', 'output': [reasoning,
                    native_call('call_rejected', name='not_in_catalog')]}
        recovered = proxy._recoverable_excel_tool_response(rejected, {'tools': LOCAL_TOOLS})
        self.assertIsNotNone(recovered)
        history = [{'role': 'user', 'content': 'offline task'}] + recovered['output']
        history.append({'role': 'user', 'content': 'retry the rejected call'})
        replay = self.prepare(history)
        tokens = [x.get('encrypted_content') for x in replay['input'] if x.get('type') == 'reasoning']
        self.assertNotIn(ciphertext, tokens,
                         'Rejected tool turn kept orphan encrypted reasoning in next request')
        self.assertEqual(dict(bridge._native_call_cache), {})

    def test_successful_native_identity_keeps_its_encrypted_reasoning(self):
        ciphertext = 'opaque-successful-turn-not-a-real-upstream-token'
        native = native_call('call_successful_pair')
        calls = bridge.extract_native_client_tool_calls({'output': [native]}, {'tools': LOCAL_TOOLS})
        history = [{'type': 'reasoning', 'id': 'rs_good', 'summary': [], 'encrypted_content': ciphertext},
                   calls[0], {'type': 'function_call_output', 'call_id': native['call_id'], 'output': 'ok'}]
        replay = self.prepare(history)
        self.assertTrue(any(x.get('encrypted_content') == ciphertext for x in replay['input']))
        self.assertIn(native, replay['input'])

class OfflineBatchStreamRegressions(OfflineStreamBase):
    def test_completed_omits_one_observed_tool_rejects_entire_batch(self):
        first = native_call('call_first')
        second = native_call('call_second')
        events = self.collect(stream_events([first, second], terminal_output=[first]))
        self.assert_no_dispatched_tools(events)
        self.assertIn('incomplete_tool_stream', json.dumps(events, ensure_ascii=False))

    def test_completed_replaces_observed_call_identity_rejects_batch(self):
        seen = native_call('call_observed')
        replaced = native_call('call_different')
        events = self.collect(stream_events([seen], terminal_output=[replaced]))
        self.assert_no_dispatched_tools(events)
        self.assertIn('incomplete_tool_stream', json.dumps(events, ensure_ascii=False))

    def test_mixed_valid_invalid_stream_batch_dispatches_nothing(self):
        events = self.collect(stream_events([native_call('call_valid_stream'),
                    native_call('call_invalid_stream', name='unknown_local_tool')]))
        self.assert_no_dispatched_tools(events)
        self.assertIn('tool_conversion_rejected', json.dumps(events, ensure_ascii=False))

    def test_custom_input_events_do_not_leak_at_eof(self):
        import format_translation as ft
        events = [('response.custom_tool_call_input.delta', {'item_id': 'ct_offline',
                   'output_index': 0, 'delta': 'never execute this patch'}),
                  ('response.custom_tool_call_input.done', {'item_id': 'ct_offline',
                   'output_index': 0, 'input': 'never execute this patch'})]
        wire = b''.join(ft.sse_encode(kind, {'type': kind, **data}) for kind, data in events)
        result = self.collect(wire)
        self.assert_no_dispatched_tools(result)
        self.assertFalse(any(kind == 'response.completed' for kind, _ in result))

    def test_valid_stream_converts_once_without_raw_transport(self):
        for chunk_size in (1, 17, 65536):
            with self.subTest(chunk_size=chunk_size):
                with bridge._native_call_cache_lock:
                    bridge._native_call_cache.clear()
                native = native_call('call_complete')
                result = self.collect(stream_events([native]), chunk_size=chunk_size)
                completed = [data['response'] for kind, data in result if kind == 'response.completed']
                self.assertEqual(len(completed), 1)
                tools = [x for x in completed[0]['output'] if x.get('type') == 'function_call']
                self.assertEqual(len(tools), 1)
                self.assertEqual(tools[0]['name'], 'exec_command')
                self.assertEqual(json.loads(tools[0]['arguments']), {'cmd': 'echo offline'})
                for _, data in result:
                    self.assertNotEqual(data.get('item', {}).get('name'), 'run_officejs')
                self.assertEqual(bridge._native_call_cache['call_complete'], native)

    def test_duplicate_call_ids_reject_batch_without_cache(self):
        batch = [native_call('call_duplicate'), native_call('call_duplicate')]
        result = bridge.extract_native_client_tool_calls({'output': batch}, {'tools': LOCAL_TOOLS})
        self.assertEqual(result, [])
        self.assertEqual(dict(bridge._native_call_cache), {})

    def test_explicit_dry_run_extraction_does_not_remember(self):
        result = bridge.extract_native_client_tool_call({'output': [native_call('call_dry_run')]},
                                                        {'tools': LOCAL_TOOLS}, remember=False)
        self.assertIsNotNone(result)
        self.assertEqual(dict(bridge._native_call_cache), {})



class OfflineTraceRegressions(OfflineStreamBase):
    SECRET_ARGUMENT = 'DO_NOT_LOG_ARGUMENT_VALUE_72ca'
    SECRET_HEADER = 'DO_NOT_LOG_AUTH_HEADER_6be4'
    SECRET_BODY = 'DO_NOT_LOG_BODY_VALUE_927e'

    def trace_plan(self):
        import proxy
        return mock.Mock(spec=proxy.UpstreamRequestPlan, request_id='offline-request-72ca',
                         resolved_model='offline-model', trace_context={},
                         headers={'Authorization': self.SECRET_HEADER},
                         body={'private_body': self.SECRET_BODY})

    def assert_redacted_trace(self, plan, category, log_output):
        self.trace_append.assert_called_once()
        payload = self.trace_append.call_args.args[0]
        self.assertEqual(payload['event'], 'client_tool_rejected')
        self.assertEqual(payload['request_id'], plan.request_id)
        self.assertEqual(payload['model'], plan.resolved_model)
        self.assertIs(payload['dispatched'], False)
        self.assertIn(category, [x['reason'] for x in payload['diagnostics']])
        self.assertEqual(plan.trace_context['client_tool_rejection']['diagnostics'], payload['diagnostics'])
        text = json.dumps(payload, ensure_ascii=False) + ' '.join(log_output)
        self.assertIn(plan.request_id, ' '.join(log_output))
        for secret in (self.SECRET_ARGUMENT, self.SECRET_HEADER, self.SECRET_BODY):
            self.assertNotIn(secret, text)
        forbidden = {'arguments', 'headers', 'Authorization', 'body', 'input', 'code', 'cmd', 'password'}
        def check_keys(value):
            if isinstance(value, dict):
                self.assertFalse(forbidden.intersection(value), value)
                for nested in value.values():
                    check_keys(nested)
            elif isinstance(value, list):
                for nested in value:
                    check_keys(nested)
        check_keys(payload)
        self.assertEqual(dict(bridge._native_call_cache), {})

    def test_direct_rejection_trace_has_request_id_category_without_values(self):
        import proxy
        plan = self.trace_plan()
        invalid = native_call('call_private_arguments', arguments={'cmd': 123, 'password': self.SECRET_ARGUMENT})
        response = {'status': 'completed', 'output': [invalid]}
        with self.assertLogs('proxy', level='WARNING') as logs:
            recovered = proxy._recoverable_excel_tool_response(response, {'tools': LOCAL_TOOLS}, trace_plan=plan)
        self.assertIsNotNone(recovered)
        self.assert_redacted_trace(plan, 'invalid_client_tool_arguments', logs.output)
        self.assertNotIn(self.SECRET_ARGUMENT, json.dumps(recovered, ensure_ascii=False))

    def test_stream_rejection_trace_has_request_id_category_without_values(self):
        plan = self.trace_plan()
        invalid = native_call('call_private_stream', arguments={'cmd': 123, 'password': self.SECRET_ARGUMENT})
        with self.assertLogs('proxy', level='WARNING') as logs:
            result = self.collect(stream_events([invalid]), trace_plan=plan)
        self.assert_no_dispatched_tools(result)
        self.assert_redacted_trace(plan, 'invalid_client_tool_arguments', logs.output)
        self.assertNotIn(self.SECRET_ARGUMENT, json.dumps(result, ensure_ascii=False))

    def test_incomplete_tool_stream_trace_has_correlated_classification(self):
        plan = self.trace_plan()
        call = native_call('call_private_incomplete', arguments={'cmd': self.SECRET_ARGUMENT})
        with self.assertLogs('proxy', level='WARNING') as logs:
            result = self.collect(stream_events([call], terminal_output=[]), trace_plan=plan)
        self.assert_no_dispatched_tools(result)
        self.assert_redacted_trace(plan, 'incomplete_tool_stream', logs.output)

    def test_unknown_tool_trace_never_includes_its_argument_values(self):
        import proxy
        plan = self.trace_plan()
        invalid = native_call('call_unknown', name='unknown_local_tool', arguments={'cmd': self.SECRET_ARGUMENT})
        with self.assertLogs('proxy', level='WARNING') as logs:
            proxy._recoverable_excel_tool_response({'status': 'completed', 'output': [invalid]},
                                                  {'tools': LOCAL_TOOLS}, trace_plan=plan)
        self.assert_redacted_trace(plan, 'unknown_client_tool', logs.output)




    def test_truly_missing_name_reports_missing_client_tool_name(self):
        import proxy
        item = {'type': 'function_call', 'call_id': 'call_truly_missing_name',
                'arguments': json.dumps({'cmd': self.SECRET_ARGUMENT})}
        self.assertNotIn('name', item)
        response = {'status': 'completed', 'output': [item]}
        self.assertEqual(bridge.client_tool_rejection_diagnostics(response, {'tools': LOCAL_TOOLS}),
                         [{'tool': '<missing>', 'reason': 'missing_client_tool_name'}])
        plan = self.trace_plan()
        with self.assertLogs('proxy', level='WARNING') as logs:
            recovered = proxy._recoverable_excel_tool_response(
                response, {'tools': LOCAL_TOOLS}, trace_plan=plan)
        self.assertIsNotNone(recovered)
        self.assert_redacted_trace(plan, 'missing_client_tool_name', logs.output)

    def test_rejection_structure_excludes_secret_argument_text(self):
        direct = {'type': 'function_call', 'call_id': 'call_secret_shape',
                  'arguments': json.dumps({'cmd': self.SECRET_ARGUMENT})}
        wrapped = native_call('call_wrapped_secret_shape',
                              arguments={'cmd': self.SECRET_ARGUMENT})
        structure = bridge.client_tool_rejection_structure({'output': [direct, wrapped]})
        self.assertEqual(len(structure), 2)
        self.assertFalse(structure[0]['name_present'])
        self.assertTrue(structure[1]['envelope_decoded'])
        self.assertNotIn(self.SECRET_ARGUMENT, json.dumps(structure, ensure_ascii=False))


class OfflineRecoveryGenerationRegressions(OfflineBase):
    def reasoning(self, token):
        return {'type': 'reasoning', 'id': 'rs_' + token,
                'summary': [{'type': 'summary_text', 'text': 'offline rationale'}],
                'encrypted_content': token}

    def marker(self, style='id', role='assistant'):
        return {'type': 'message', 'role': role, 'status': 'completed',
                'id': 'msg_proxy_tool_rejection_offline' if style == 'id' else 'msg_other_offline',
                'content': [{'type': 'output_text',
                             'text': 'Offline recovery notice' if style == 'id' else
                                     '[tool_conversion_rejected] Offline recovery notice'}]}

    def tokens(self, prepared):
        return [item['encrypted_content'] for item in prepared['input']
                if item.get('type') == 'reasoning' and item.get('encrypted_content')]

    def successful_generation(self, token, call_id, *, custom=False):
        native = (native_call(call_id, raw={'name': 'apply_patch', 'input': 'offline patch fixture'})
                  if custom else native_call(call_id))
        calls = bridge.extract_native_client_tool_calls({'output': [native]}, {'tools': LOCAL_TOOLS})
        self.assertEqual(len(calls), 1)
        output_type = 'custom_tool_call_output' if custom else 'function_call_output'
        history = [self.reasoning(token), calls[0],
                   {'type': output_type, 'call_id': call_id, 'output': 'previous generation succeeded'}]
        return native, history

    def test_recovery_retains_previous_successful_function_generation(self):
        for style in ('id', 'text'):
            with self.subTest(marker=style):
                good, history = self.successful_generation('opaque_previous_function', 'call_previous_function')
                baseline = self.prepare(history)
                cache = copy.deepcopy(bridge._native_call_cache)
                replay = self.prepare(history + [self.reasoning('opaque_rejected_function'), self.marker(style)])
                self.assertEqual(self.tokens(replay), ['opaque_previous_function'])
                self.assertEqual(replay['input'][:len(baseline['input'])], baseline['input'])
                self.assertIn(good, replay['input'])
                self.assertEqual(bridge._native_call_cache, cache)

    def test_recovery_retains_previous_successful_custom_generation(self):
        for style in ('id', 'text'):
            with self.subTest(marker=style):
                good, history = self.successful_generation('opaque_previous_custom', 'call_previous_custom', custom=True)
                baseline = self.prepare(history)
                replay = self.prepare(history + [self.reasoning('opaque_rejected_custom'), self.marker(style)])
                self.assertEqual(self.tokens(replay), ['opaque_previous_custom'])
                self.assertEqual(replay['input'][:len(baseline['input'])], baseline['input'])
                self.assertIn(good, replay['input'])

    def test_recovery_cleanup_stops_at_user_system_developer_boundaries(self):
        for role in ('user', 'system', 'developer'):
            for style in ('id', 'text'):
                with self.subTest(boundary=role, marker=style):
                    prefix = [self.reasoning('opaque_before_' + role),
                              {'type': 'message', 'role': 'assistant', 'content': 'Previous answer'},
                              {'type': 'message', 'role': role, 'content': 'New generation boundary'}]
                    baseline = self.prepare(prefix)
                    history = prefix + [self.reasoning('opaque_after_' + role), self.marker(style)]
                    replay = self.prepare(history)
                    self.assertEqual(self.tokens(replay), ['opaque_before_' + role])
                    self.assertEqual(replay['input'][:len(baseline['input'])], baseline['input'])

    def test_recovery_removes_all_orphan_reasoning_only_after_latest_boundary(self):
        prefix = [self.reasoning('opaque_older_turn'), {'role': 'user', 'content': 'Latest boundary'}]
        rejected = [self.reasoning('opaque_rejected_one'),
                    {'role': 'assistant', 'content': 'Preparing local operation'},
                    self.reasoning('opaque_rejected_two'), self.marker('text')]
        replay = self.prepare(prefix + rejected)
        self.assertEqual(self.tokens(replay), ['opaque_older_turn'])

    def test_recovery_does_not_drop_following_successful_generation(self):
        previous, prefix = self.successful_generation('opaque_previous', 'call_previous')
        following, suffix = self.successful_generation('opaque_following', 'call_following')
        history = prefix + [self.reasoning('opaque_rejected_between'), self.marker('id')] + suffix
        replay = self.prepare(history)
        self.assertEqual(self.tokens(replay), ['opaque_previous', 'opaque_following'])
        self.assertIn(previous, replay['input'])
        self.assertIn(following, replay['input'])

    def test_nonassistant_recovery_markers_do_not_remove_earlier_reasoning(self):
        for role in ('user', 'system', 'developer'):
            for style in ('id', 'text'):
                with self.subTest(role=role, marker=style):
                    replay = self.prepare([self.reasoning('opaque_earlier_valid'), self.marker(style, role)])
                    self.assertEqual(self.tokens(replay), ['opaque_earlier_valid'])

    def test_recovery_payload_strips_current_ciphertext_without_mutating_source(self):
        import proxy
        previous, history = self.successful_generation('opaque_preserved_payload', 'call_preserved_payload')
        source_reasoning = self.reasoning('opaque_rejected_payload')
        response = {'id': 'resp_payload_isolation', 'status': 'completed',
                    'output': [source_reasoning, native_call('call_rejected_payload', name='unknown_local_tool')]}
        original = copy.deepcopy(response)
        previous_cache = copy.deepcopy(bridge._native_call_cache)
        recovered = proxy._recoverable_excel_tool_response(response, {'tools': LOCAL_TOOLS})
        self.assertEqual(response, original, 'Recovery mutated caller-owned source response')
        self.assertEqual(recovered['output'][0], {key: value for key, value in source_reasoning.items()
                                                 if key != 'encrypted_content'})
        replay = self.prepare(history + recovered['output'])
        self.assertEqual(self.tokens(replay), ['opaque_preserved_payload'])
        self.assertIn(previous, replay['input'])
        self.assertEqual(bridge._native_call_cache, previous_cache)

    def test_normal_multiple_tool_generations_keep_all_reasoning_without_recovery(self):
        first, first_history = self.successful_generation('opaque_normal_one', 'call_normal_one')
        second, second_history = self.successful_generation('opaque_normal_two', 'call_normal_two', custom=True)
        replay = self.prepare(first_history + second_history)
        self.assertEqual(self.tokens(replay), ['opaque_normal_one', 'opaque_normal_two'])
        self.assertIn(first, replay['input'])
        self.assertIn(second, replay['input'])




class OfflineDispatchGuardRegressions(OfflineStreamBase):
    def legacy_marker(self, arguments):
        return '<codex_tool_call>' + json.dumps({'name': 'exec_command', 'arguments': arguments}) + '</codex_tool_call>'

    def completed(self, native_items, marker=None):
        output = copy.deepcopy(native_items)
        if marker is not None:
            output.append({'type': 'message', 'role': 'assistant', 'id': 'msg_marker', 'status': 'completed',
                           'content': [{'type': 'output_text', 'text': marker, 'annotations': []}]})
        return {'id': 'resp_dispatch_guard', 'status': 'completed', 'output': output}

    def wire(self, payload):
        import format_translation as ft
        events = []
        for index, item in enumerate(payload['output']):
            events.append(('response.output_item.added', {'output_index': index, 'item': item}))
            if item.get('type') == 'message':
                events.append(('response.output_text.delta', {'output_index': index, 'item_id': item['id'],
                               'content_index': 0, 'delta': item['content'][0]['text']}))
            events.append(('response.output_item.done', {'output_index': index, 'item': item}))
        events.append(('response.completed', {'response': payload}))
        return b''.join(ft.sse_encode(kind, {'type': kind, **data}) for kind, data in events)

    def nonstream(self, payload, source):
        from types import SimpleNamespace
        import httpx
        import proxy
        request = httpx.Request('POST', 'https://offline.invalid/responses')
        response = httpx.Response(200, request=request, json=payload)
        client = mock.Mock()
        client.build_request.return_value = request
        plan = SimpleNamespace(upstream_url=str(request.url), headers={}, body={})
        with mock.patch.object(proxy, '_get_excel_upstream_client', return_value=client), \
             mock.patch.object(proxy, 'throttled_client_send', new=mock.AsyncMock(return_value=response)), \
             mock.patch.object(proxy, '_finish_usage_and_trace'):
            result = run_immediate(proxy._post_excel_non_streaming_request(plan, client_body=source))
        return json.loads(result.body)

    def assert_blocked_on_both_paths(self, payload, source):
        with self.subTest(path='stream'):
            events = self.collect(self.wire(payload), source_body=source)
            self.assert_no_dispatched_tools(events)
        with self.subTest(path='nonstream'):
            response = self.nonstream(payload, source)
            self.assertFalse(any(item.get('type') in {'function_call', 'custom_tool_call'}
                                 for item in response.get('output', [])), response)
            self.assertEqual(dict(bridge._native_call_cache), {})

    def test_legacy_invalid_schema_never_dispatches(self):
        for arguments in ({'cmd': 123}, {}, {'cmd': 'offline', 'unexpected': True}):
            with self.subTest(arguments=arguments):
                self.assert_blocked_on_both_paths(self.completed([], self.legacy_marker(arguments)),
                                                 {'tools': LOCAL_TOOLS})

    def test_invalid_native_batch_cannot_be_bypassed_by_valid_marker(self):
        for items in ([native_call('call_invalid_only', name='unknown_local_tool')],
                      [native_call('call_valid_first'), native_call('call_invalid_second', name='unknown_local_tool')]):
            with self.subTest(batch_size=len(items)):
                self.assert_blocked_on_both_paths(self.completed(items, self.legacy_marker({'cmd': 'marker bypass'})),
                                                 {'tools': LOCAL_TOOLS})

    def test_valid_native_call_takes_precedence_over_text_marker(self):
        native = native_call('call_preferred_native', arguments={'cmd': 'native command'})
        payload = self.completed([native], self.legacy_marker({'cmd': 'marker command'}))
        events = self.collect(self.wire(payload))
        outputs = [data['response'] for kind, data in events if kind == 'response.completed']
        self.assertEqual(len(outputs), 1)
        outputs.append(self.nonstream(payload, {'tools': LOCAL_TOOLS}))
        for response in outputs:
            calls = [item for item in response['output'] if item.get('type') == 'function_call']
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]['call_id'], 'call_preferred_native')
            self.assertEqual(json.loads(calls[0]['arguments']), {'cmd': 'native command'})

    def test_tool_choice_none_never_leaks_native_tools(self):
        self.assert_blocked_on_both_paths(self.completed([native_call('call_forbidden_none')]),
                                         {'tools': LOCAL_TOOLS, 'tool_choice': 'none'})

    def test_tool_choice_none_never_dispatches_legacy_marker(self):
        self.assert_blocked_on_both_paths(self.completed([], self.legacy_marker({'cmd': 'forbidden marker'})),
                                         {'tools': LOCAL_TOOLS, 'tool_choice': 'none'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
