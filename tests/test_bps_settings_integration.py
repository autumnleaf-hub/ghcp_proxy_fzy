"""Offline integration tests executing current proxy.py definitions via AST.

No proxy import/lifespan, sockets, subprocesses, real credentials, or worker
threads. HTTP uses ASGITransport; credential state is memory-only, and settings
use temporary files. Only external I/O/planning/telemetry boundaries are mocked.
Run with the repo virtualenv: python -B -m unittest -v tests.test_bps_settings_integration
"""
from __future__ import annotations

import ast
import asyncio
import base64
from collections import deque
from dataclasses import dataclass
import copy
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import attachment_inputs
from attachment_store import FileStoreError
import bps_credentials
import bps_failover
import codex_agent_compat
import excel_upstream
import format_translation
import model_routing_config
import outbound_proxy
import request_headers
import protocol_replies
import upstream_errors
import util

ROOT = Path(__file__).resolve().parents[1]
NOW = 2_000_000_000.0
EXPIRY = 4_102_444_800.0
MODEL = 'gpt-6-sol-excel'
PORT = 54321  # An ASGI scope value only; nothing binds or connects to this port.
PROXY_FUNCTIONS = {
    'parse_json_request', '_require_local_bps_management',
    'outbound_proxy_status_api', 'outbound_proxy_config_api',
    'bps_credentials_status_api', 'bps_credentials_update_api',
    'bps_credentials_delete_api', 'bps_credentials_test_api',
    '_bps_session_key', '_bps_response_payload', '_bps_effective_failure_status',
    '_close_bps_stream', '_bps_preflight_stream', '_bps_observe_stream_result',
    '_send_excel_credential_attempt', '_handle_excel_responses',
    'responses', 'responses_compact', 'chat_completions', 'anthropic_messages',
    'auth_status_api', 'auth_device_api',
    'ExcelInlineImageUploadError', 'ExcelImageInputError',
    '_normalize_excel_image_part', '_materialize_excel_inline_images',
    'UpstreamRequestPlan', '_handle_upstream_error', '_extract_upstream_json_payload',
    '_extract_upstream_text', '_default_upstream_error_trace', 'proxy_non_streaming_response',
}


def load_proxy_functions(namespace):
    """Compile entire unchanged function bodies, minus only route decorators."""
    source = ROOT / 'proxy.py'
    tree = ast.parse(source.read_text(encoding='utf-8-sig'), filename=str(source))
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in PROXY_FUNCTIONS:
            if not isinstance(node, ast.ClassDef):
                node.decorator_list = []
            selected.append(node)
    found = {node.name for node in selected}
    if found != PROXY_FUNCTIONS:
        raise AssertionError(f'Missing production definitions: {PROXY_FUNCTIONS - found}')
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[]))
    exec(compile(module, str(source), 'exec'), namespace)


async def inline_to_thread(function, *args, **kwargs):
    """Run deterministic memory/temp-file operations without creating threads."""
    return function(*args, **kwargs)


def event_bytes(event):
    return ('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode()


def quota_event():
    return {'type': 'response.failed', 'response': {
        'id': 'resp-failed-fixture', 'status': 'failed',
        'error': {'code': 'insufficient_quota', 'message': 'Synthetic quota exhausted'},
    }}


class TrackedStream:
    """An instrumented local transport, not a substitute for preflight logic."""
    def __init__(self, chunks):
        self.chunks = deque(chunks)
        self.closed = 0
        self.reads = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.chunks:
            raise StopAsyncIteration
        self.reads += 1
        value = self.chunks.popleft()
        if isinstance(value, BaseException):
            raise value
        return value

    async def aclose(self):
        self.closed += 1


class BpsSettingsIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scratch = self.enterContext(tempfile.TemporaryDirectory(prefix='bps-integration-'))
        self.scratch = Path(scratch)
        self.blocked_calls = []

        def blocked(*args, **kwargs):
            self.blocked_calls.append('external I/O/thread')
            raise AssertionError('External I/O/thread forbidden')

        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.getaddrinfo',
                       'subprocess.Popen', 'threading.Thread.start', 'socket.create_connection',
                       'httpx.HTTPTransport.handle_request', 'httpx.AsyncHTTPTransport.handle_async_request'):
            self.enterContext(mock.patch(target, side_effect=blocked))
        self.enterContext(mock.patch.object(util, 'print', create=True))
        self.pool = bps_credentials.CredentialPool(clock=lambda: NOW)
        self.pool.load()
        self.ids = []
        self.secrets = []
        self.add_account('A')
        self.add_account('B')
        # Migration sees only these fresh empty snapshots, never module globals.
        session_store = types.SimpleNamespace(_lock=threading.RLock(), _headers={}, _tools_version_id=None)
        oauth = types.SimpleNamespace(_lock=threading.RLock(), _tokens={})
        self.settings = outbound_proxy.OutboundProxySettings(str(self.scratch / 'outbound.json'))
        self.routing = model_routing_config.ModelRoutingConfigService(
            model_routing_config.ModelRoutingConfig(str(self.scratch / 'routing.json')))
        self.results = deque()
        self.plans = []
        self.image_inputs = []
        self.http_calls = []
        self.ns = {
            '__name__': 'isolated_bps_proxy', 'Request': Request, 'Response': Response,
            'JSONResponse': JSONResponse, 'StreamingResponse': StreamingResponse,
            'HTTPException': HTTPException, 'json': json, 'copy': copy, 'hashlib': hashlib,
            'dataclass': dataclass, 'protocol_replies': protocol_replies,
            'GracefulStreamingResponse': StreamingResponse,
            'upstream_errors': types.SimpleNamespace(translate=mock.Mock()),
            '_finish_usage_and_trace': mock.Mock(), '_publish_synthetic_reply_event': mock.Mock(),
            'time': time, 'httpx': httpx, 'uuid4': uuid4, 'PROXY_PORT': PORT,
            'asyncio': types.SimpleNamespace(to_thread=inline_to_thread, wait_for=asyncio.wait_for,
                                            TimeoutError=asyncio.TimeoutError),
            'util': util, 'usage_tracker': types.SimpleNamespace(record_request_error=mock.Mock()),
            'bps_credentials': types.SimpleNamespace(credential_pool=self.pool, PoolError=bps_credentials.PoolError),
            'bps_failover': bps_failover,
            'excel_upstream': types.SimpleNamespace(**{**vars(excel_upstream), 'excel_session_store': session_store}),
            'openai_oauth': types.SimpleNamespace(login_service=oauth),
            'outbound_proxy': types.SimpleNamespace(settings=self.settings),
            'attachment_inputs': attachment_inputs, 'FileStoreError': FileStoreError,
            '_attachment_store': types.SimpleNamespace(get=mock.Mock(side_effect=AssertionError('Unexpected file lookup'))),
            'file_owner_scope': lambda request: 'synthetic-owner',
            'format_translation': format_translation, 'codex_agent_compat': codex_agent_compat,
            'model_provider_family': model_routing_config.model_provider_family,
            'model_routing_config_service': self.routing,
            '_request_headers_module': request_headers,
            'configured_upstream_timeout_seconds': lambda: 5,
            '_append_request_trace': mock.Mock(),
            '_excel_file_id_for_image': mock.AsyncMock(side_effect=self.upload_image),
            '_prepare_upstream_request': mock.Mock(side_effect=self.prepare_plan),
            '_post_excel_non_streaming_request': mock.AsyncMock(side_effect=self.post_result),
            'proxy_streaming_response': mock.AsyncMock(side_effect=self.stream_result),
            '_get_excel_upstream_client': mock.Mock(return_value=object()),
            '_excel_tool_stream_transform': mock.Mock(return_value=None),
            'copilot_sdk_upstream': mock.Mock(name='forbidden_sdk', enabled=mock.Mock(side_effect=AssertionError('Copilot dispatch reached'))),
            'auth': mock.Mock(name='forbidden_auth', get_api_key=mock.Mock(side_effect=AssertionError('Copilot credentials reached'))),
            '_handle_copilot_sdk_responses': mock.AsyncMock(side_effect=AssertionError('Copilot handler reached')),
        }
        isolated = types.ModuleType('isolated_bps_proxy')
        isolated.__dict__.update(self.ns)
        self.ns = isolated.__dict__
        self.enterContext(mock.patch.dict(sys.modules, {'isolated_bps_proxy': isolated}))
        load_proxy_functions(self.ns)
        self.image_error = self.ns['ExcelInlineImageUploadError']
        self.image_input_error = self.ns['ExcelImageInputError']
        self.app = FastAPI()
        for path, methods, name in (
            ('/api/config/outbound-proxy', ['GET'], 'outbound_proxy_status_api'),
            ('/api/config/outbound-proxy', ['POST'], 'outbound_proxy_config_api'),
            ('/api/credentials', ['GET'], 'bps_credentials_status_api'),
            ('/api/credentials/{credential_id}', ['POST'], 'bps_credentials_update_api'),
            ('/api/credentials/{credential_id}', ['DELETE'], 'bps_credentials_delete_api'),
            ('/api/credentials/{credential_id}/test', ['POST'], 'bps_credentials_test_api'),
            ('/responses', ['POST'], 'responses'),
            ('/responses/compact', ['POST'], 'responses_compact'),
            ('/chat/completions', ['POST'], 'chat_completions'),
            ('/v1/chat/completions', ['POST'], 'chat_completions'),
            ('/v1/messages', ['POST'], 'anthropic_messages'),
            ('/api/auth/status', ['GET'], 'auth_status_api'),
            ('/api/auth/device', ['POST'], 'auth_device_api'),
            ('/v1/responses', ['POST'], 'responses'),
            ('/v1/responses/compact', ['POST'], 'responses_compact'),
        ):
            self.app.add_api_route(path, self.ns[name], methods=methods)

    def tearDown(self):
        self.assertEqual(self.blocked_calls, [], 'Forbidden I/O was attempted, even if caught')
        self.assertEqual(self.ns['auth'].mock_calls, [], 'Auth must not be queried')
        self.assertEqual(self.ns['copilot_sdk_upstream'].mock_calls, [], 'SDK must not be queried')
        self.ns['_handle_copilot_sdk_responses'].assert_not_awaited()

    def add_account(self, name):
        account = 'synthetic-private-account-' + name
        claims = {'exp': EXPIRY, 'https://api.openai.com/auth': {'chatgpt_account_id': account}}
        token = 'fixture.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.not-a-signature'
        refresh = 'synthetic-refresh-secret-' + name
        self.secrets.extend((account, token, refresh, 'Bearer ' + token))
        entry = self.pool.upsert_oauth({'account_id': account, 'access_token': token,
                                      'refresh_token': refresh, 'expires_at': EXPIRY}, label='Fixture ' + name)
        self.ids.append(entry['id'])
        return entry['id']

    async def request(self, method, path, *, remote='127.0.0.1', host=None, **kwargs):
        transport = httpx.ASGITransport(app=self.app, client=(remote, 12345))
        async with httpx.AsyncClient(transport=transport, base_url=f'http://{host or f"localhost:{PORT}"}') as client:
            return await client.request(method, path, **kwargs)

    def raw_request(self):
        return Request({'type': 'http', 'method': 'POST', 'path': '/v1/responses',
                        'headers': [(b'host', f'localhost:{PORT}'.encode())],
                        'client': ('127.0.0.1', 12345), 'scheme': 'http', 'query_string': b''})

    def body(self, **overrides):
        return {'model': MODEL, 'input': [{'role': 'user', 'content': [
            {'type': 'input_text', 'text': 'Synthetic hello'}]}],
            'prompt_cache_key': 'offline-session', 'stream': False, **overrides}

    def completed(self, text='Synthetic OK', model='gpt-6-sol'):
        return JSONResponse({'id': 'resp-fixture', 'object': 'response', 'status': 'completed',
                             'model': model, 'output': [{'type': 'message', 'role': 'assistant',
                                 'content': [{'type': 'output_text', 'text': text}]}]})

    def failure(self, status, code='server_error', message='Synthetic upstream failure', **headers):
        return JSONResponse({'error': {'code': code, 'message': message}}, status_code=status, headers=headers)

    def prepare_plan(self, request, **kwargs):
        plan = types.SimpleNamespace(body=copy.deepcopy(kwargs['body']),
            headers=kwargs['header_builder']('unused', 'fixture-request'), upstream_url=kwargs['upstream_url'],
            usage_event={}, source_body=copy.deepcopy(kwargs['source_body']), trace_metadata=kwargs['trace_metadata'])
        self.plans.append(plan)
        return plan, None

    async def upload_image(self, data_url, headers):
        # Only the upload/cache boundary is mocked; materialization is production AST.
        self.image_inputs.append((data_url, dict(headers)))
        return 'file-fixture-' + headers['chatgpt-account-id'].rsplit('-', 1)[-1]

    def next_result(self):
        if not self.results:
            raise AssertionError('Unexpected extra HTTP attempt')
        result = self.results.popleft()
        if isinstance(result, BaseException):
            raise result
        return result

    async def post_result(self, plan, **kwargs):
        self.http_calls.append(plan)
        return self.next_result()

    async def stream_result(self, *args, **kwargs):
        self.http_calls.append(kwargs['trace_plan'])
        return self.next_result()

    async def handle(self, body=None, **kwargs):
        return await self.ns['_handle_excel_responses'](self.raw_request(), self.body() if body is None else body, **kwargs)

    async def consume(self, response):
        chunks = [chunk async for chunk in response.body_iterator]
        return b''.join(chunk.encode() if isinstance(chunk, str) else chunk for chunk in chunks)

    def streaming(self, *events):
        stream = TrackedStream([event_bytes(event) for event in events])
        return StreamingResponse(stream, media_type='text/event-stream'), stream

    async def test_http_401_and_429_fail_over_to_second_synthetic_account(self):
        for status, code in ((401, 'invalid_token'), (429, 'insufficient_quota')):
            with self.subTest(status=status):
                # Explicitly re-enable/reset prior fixture results between cases.
                for credential_id in self.ids:
                    self.pool.update(credential_id, {'enabled': True})
                self.pool._sticky.clear()
                self.pool._cursor = 0
                before = len(self.http_calls)
                self.results.extend([self.failure(status, code), self.completed()])
                result = await self.handle(self.body(prompt_cache_key=f'offline-{status}'))
                self.assertEqual(result.status_code, 200)
                self.assertEqual(len(self.http_calls) - before, 2)
                attempts = self.http_calls[before:]
                self.assertNotEqual(attempts[0].headers['authorization'], attempts[1].headers['authorization'])
                self.assertEqual(result.headers['x-ghcp-credential-id'], attempts[1].trace_metadata['credential_id'])

    async def test_settings_get_and_post_use_real_validation_without_network(self):
        result = await self.request('GET', '/api/config/outbound-proxy')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {'enabled': False, 'url': outbound_proxy.DEFAULT_URL})
        desired = {'enabled': True, 'url': 'http://127.0.0.1:7890'}
        result = await self.request('POST', '/api/config/outbound-proxy', json=desired,
                                    headers={'Origin': f'http://localhost:{PORT}'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), desired)
        self.assertEqual(result.headers['cache-control'], 'no-store')
        self.assertEqual((await self.request('GET', '/api/config/outbound-proxy')).json(), desired)


    async def test_configured_alias_precedes_astra_fallback_on_responses_and_compact(self):
        self.routing.save_settings({'enabled': True, 'mappings': [
            {'source_model': 'customer/opus,FAST', 'target_model': MODEL}]})
        for path in ('/v1/responses', '/v1/responses/compact'):
            with self.subTest(path=path):
                self.results.append(self.completed())
                result = await self.request('POST', path, json=self.body(model='customer/opus'))
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(self.plans[-1].source_body['model'], 'customer/opus')
                self.assertEqual(self.plans[-1].body['model'], 'gpt-6-sol')
                if path.endswith('/compact'):
                    self.assertEqual(result.json()['output'][0]['type'], 'compaction')
                self.assertIn('x-ghcp-credential-id', result.headers)
        self.ns['copilot_sdk_upstream'].enabled.assert_not_called()
        self.ns['auth'].get_api_key.assert_not_called()
        self.ns['_handle_copilot_sdk_responses'].assert_not_awaited()

    async def test_configured_mapping_precedes_even_direct_native_bps_dispatch(self):
        self.routing.save_settings({'enabled': True, 'mappings': [
            {'source_model': 'gpt-6-astra-basispoints', 'target_model': MODEL}]})
        for path in ('/v1/responses', '/v1/responses/compact'):
            with self.subTest(path=path):
                self.results.append(self.completed())
                result = await self.request('POST', path, json=self.body(model='gpt-6-astra-basispoints'))
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(self.plans[-1].body['model'], 'gpt-6-sol')

    async def test_failover_is_bounded_to_three_distinct_accounts(self):
        for name in ('C', 'D', 'E'):
            self.add_account(name)
        self.results.extend(self.failure(502) for _ in range(5))
        result = await self.handle()
        self.assertEqual(result.status_code, 502)
        self.assertEqual(len(self.http_calls), 3)
        self.assertEqual(len({plan.trace_metadata['credential_id'] for plan in self.http_calls}), 3)
        self.assertEqual(len(self.results), 2)

    async def test_bad_request_and_tool_conversion_errors_never_fail_over(self):
        fixtures = ((400, 'invalid_request_error', 'Bad request'),
                    (502, 'tool_conversion_rejected', 'Unsupported tool schema'),
                    (429, 'invalid_tool_schema', 'Unsupported tool schema'),
                    (502, 'server_error', '[tool_conversion_rejected] bad tool'))
        for status, code, message in fixtures:
            with self.subTest(status=status, code=code):
                for credential_id in self.ids:
                    self.pool.update(credential_id, {'enabled': True})
                before = len(self.http_calls)
                self.results.append(self.failure(status, code, message))
                result = await self.handle()
                self.assertEqual(result.status_code, status)
                self.assertEqual(len(self.http_calls) - before, 1)
                self.assertFalse(self.results)

    async def test_conversion_rejection_before_http_does_not_try_another_account(self):
        rejection = self.failure(400, 'tool_conversion_rejected')
        with mock.patch.dict(self.ns, {'_prepare_upstream_request': mock.Mock(return_value=(None, rejection))}):
            result = await self.handle()
            self.ns['_prepare_upstream_request'].assert_called_once()
        self.assertIs(result, rejection)
        self.assertEqual(self.http_calls, [])

    async def test_explicit_credential_probe_does_not_fail_over(self):
        self.results.extend([self.failure(401, 'invalid_token'), self.completed()])
        result = await self.handle(credential_id=self.ids[0])
        self.assertEqual(result.status_code, 401)
        self.assertEqual(len(self.http_calls), 1)
        self.assertEqual(result.headers['x-ghcp-credential-id'], self.ids[0])
        self.assertEqual(len(self.results), 1)

    async def test_all_accounts_disabled_returns_clear_error_without_http(self):
        for credential_id in self.ids:
            self.pool.update(credential_id, {'enabled': False})
        result = await self.handle()
        self.assertEqual(result.status_code, 503)
        self.assertIn('credentials', json.loads(result.body)['error']['message'].lower())
        self.assertEqual(self.http_calls, [])

    async def test_proxy_failures_exhaust_accounts_without_direct_fallback(self):
        self.results.extend([httpx.ProxyError('synthetic proxy failure')] * 2)
        result = await self.handle()
        self.assertEqual(result.status_code, 502)
        self.assertTrue(json.loads(result.body)['error']['message'].strip())
        self.assertEqual(len(self.http_calls), 2)
        self.assertTrue(all(row['status'] == 'cooldown' for row in self.pool.list_credentials()['credentials']))
        again = await self.handle()
        self.assertEqual(again.status_code, 503)
        self.assertEqual(len(self.http_calls), 2)
        self.ns['auth'].get_api_key.assert_not_called()

    async def test_pre_output_quota_failure_switches_and_closes_abandoned_stream(self):
        created = {'type': 'response.created', 'response': {'id': 'abandoned-fixture'}}
        first, abandoned = self.streaming(created, quota_event(), {'type': 'unexpected-tail'})
        second, successful = self.streaming(
            {'type': 'response.created', 'response': {'id': 'successful-fixture'}},
            {'type': 'response.output_text.delta', 'delta': 'Only second account text'},
            {'type': 'response.completed', 'response': {'status': 'completed'}})
        self.results.extend([first, second])
        result = await self.handle(self.body(stream=True))
        self.assertIs(result, second)
        self.assertEqual(len(self.http_calls), 2)
        self.assertEqual(abandoned.closed, 1)
        self.assertEqual(abandoned.reads, 2)
        wire = await self.consume(result)
        self.assertNotIn(b'abandoned-fixture', wire)
        self.assertNotIn(b'insufficient_quota', wire)
        self.assertIn(b'Only second account text', wire)
        self.assertEqual(successful.closed, 1)

    async def test_preflight_handles_chunk_split_utf8_and_crlf(self):
        created = {'type': 'response.created', 'response': {'id': 'synthetic-测试'}}
        encoded = (event_bytes(created) + event_bytes(quota_event())).replace(bytes([10]), bytes([13, 10]))
        abandoned = TrackedStream([bytes([byte]) for byte in encoded])
        self.results.extend([StreamingResponse(abandoned), self.completed()])
        result = await self.handle(self.body(stream=True))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(self.http_calls), 2)
        self.assertEqual(abandoned.closed, 1)

    async def test_after_text_or_function_call_no_generation_is_replayed(self):
        visible_events = (
            {'type': 'response.output_text.delta', 'delta': 'Already visible'},
            {'type': 'response.function_call_arguments.delta', 'delta': '{'},
            {'type': 'response.output_item.added', 'item': {'type': 'function_call', 'name': 'fixture', 'call_id': 'fixture-call'}},
            {'type': 'response.output_item.added', 'item': {'type': 'custom_tool_call', 'name': 'fixture', 'call_id': 'fixture-call'}},
        )
        for visible in visible_events:
            with self.subTest(event=visible):
                for credential_id in self.ids:
                    self.pool.update(credential_id, {'enabled': True})
                events = [{'type': 'response.created'}, visible, quota_event()]
                # Same network chunk exercises stop-at-visible-event, not just chunk boundaries.
                expected = b''.join(event_bytes(event) for event in events)
                stream = TrackedStream([expected])
                response = StreamingResponse(stream)
                before = len(self.http_calls)
                self.results.append(response)
                result = await self.handle(self.body(stream=True))
                self.assertIs(result, response)
                self.assertEqual(len(self.http_calls) - before, 1)
                self.assertEqual(await self.consume(result), expected)
                self.assertEqual(stream.closed, 1)

    async def test_separate_chunk_failure_after_visible_text_is_not_retried(self):
        response, stream = self.streaming({'type': 'response.created'},
            {'type': 'response.output_text.delta', 'delta': 'visible'}, quota_event())
        self.results.append(response)
        with mock.patch.object(self.pool, 'record_result', wraps=self.pool.record_result) as record:
            result = await self.handle(self.body(stream=True))
            first_id = result.headers['x-ghcp-credential-id']
            revision = int(result.headers['x-ghcp-credential-revision'])
            self.assertEqual(stream.reads, 2)
            self.assertEqual(len(self.http_calls), 1)
            self.assertEqual(self.account_status(first_id)['status'], 'active')
            self.assertIn(b'insufficient_quota', await self.consume(result))
            self.assertEqual(len(self.http_calls), 1, 'Late errors must not replay this generation')
            late_calls = [call for call in record.call_args_list if call.args[1] == 429]
            self.assertEqual(len(late_calls), 1)
            self.assertEqual(late_calls[0].args[0], first_id)
            self.assertEqual(late_calls[0].kwargs['expected_revision'], revision)
        self.assertEqual(stream.closed, 1)
        self.assertEqual(self.account_status(first_id)['status'], 'cooldown')
        self.results.append(self.completed('Next turn succeeds on the other account'))
        next_result = await self.handle()
        self.assertEqual(next_result.status_code, 200)
        self.assertNotEqual(next_result.headers['x-ghcp-credential-id'], first_id)
        self.assertEqual(len(self.http_calls), 2)
        self.assertNotEqual(self.http_calls[0].headers['authorization'], self.http_calls[1].headers['authorization'])

    async def test_all_pre_output_stream_failures_close_and_return_clear_error(self):
        streams = []
        for _ in range(2):
            response, stream = self.streaming({'type': 'response.created'}, quota_event())
            self.results.append(response)
            streams.append(stream)
        result = await self.handle(self.body(stream=True))
        self.assertEqual(result.status_code, 429)
        self.assertTrue(json.loads(result.body)['error']['message'])
        self.assertEqual(len(self.http_calls), 2)
        self.assertEqual([stream.closed for stream in streams], [1, 1])

    async def test_preflight_cancellation_closes_stream_and_propagates(self):
        stream = TrackedStream([event_bytes({'type': 'response.created'}), asyncio.CancelledError()])
        response = StreamingResponse(stream)
        with self.assertRaises(asyncio.CancelledError):
            await self.ns['_bps_preflight_stream'](response)
        self.assertEqual(stream.closed, 1)

    async def test_client_closing_replay_closes_underlying_stream(self):
        response, stream = self.streaming({'type': 'response.created'},
            {'type': 'response.output_text.delta', 'delta': 'visible'}, quota_event())
        self.results.append(response)
        result = await self.handle(self.body(stream=True))
        iterator = result.body_iterator.__aiter__()
        await iterator.__anext__()
        await iterator.aclose()
        self.assertEqual(stream.closed, 1)
        self.assertEqual(stream.reads, 2)
        self.assertEqual(len(self.http_calls), 1)

    async def test_each_attempt_uploads_original_local_images_for_its_own_account(self):
        image_url = 'data:image/png;base64,aW1hZ2UtZml4dHVyZQ=='
        body = self.body(input=[{'role': 'user', 'content': [
            {'type': 'input_file', 'file_id': 'local-text-fixture'},
            {'type': 'input_image', 'image_url': image_url},
        ]}])
        original = copy.deepcopy(body)
        self.ns['_attachment_store'].get.side_effect = None
        self.ns['_attachment_store'].get.return_value = {'filename': 'fixture.txt', 'data': b'Original local attachment text'}
        self.results.extend([self.failure(401, 'invalid_token'), self.completed()])
        with mock.patch.object(bps_failover, 'prepare_failover_body', wraps=bps_failover.prepare_failover_body) as replay:
            result = await self.handle(body)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(body, original)
        self.ns['_attachment_store'].get.assert_called_once_with('local-text-fixture', 'synthetic-owner')
        self.assertEqual([image for image, _ in self.image_inputs], [image_url, image_url])
        self.assertNotEqual(self.image_inputs[0][1]['authorization'], self.image_inputs[1][1]['authorization'])
        self.assertTrue(all(headers['Copilot-Vision-Request'] == 'true' for _, headers in self.image_inputs))
        replay.assert_called_once()
        replay_body = json.dumps(replay.call_args.args[0])
        self.assertIn(image_url, replay_body)
        self.assertIn('Original local attachment text', replay_body)
        self.assertNotIn('file-fixture-A', replay_body)
        first_wire, second_wire = (json.dumps(plan.body) for plan in self.http_calls)
        self.assertIn('file-fixture-A', first_wire)
        self.assertIn('file-fixture-B', second_wire)
        self.assertNotIn('file-fixture-A', second_wire)

    async def test_unsafe_opaque_compaction_returns_409_instead_of_dropping_history(self):
        opaque = {'type': 'compaction', 'encrypted_content': 'opaque-account-specific-fixture'}
        body = self.body(input=[opaque, {'role': 'user', 'content': 'Visible conversation'}])
        original = copy.deepcopy(body)
        self.results.extend([self.failure(401, 'invalid_token'), self.completed()])
        result = await self.handle(body)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(len(self.http_calls), 1)
        self.assertEqual(body, original)
        self.assertEqual(len(self.results), 1)
        self.assertTrue(json.loads(result.body)['error']['message'])

    async def test_compact_endpoint_does_not_drop_opaque_history_before_account_switch(self):
        body = self.body(input=[{'type': 'compaction', 'encrypted_content': 'opaque-account-specific-fixture'},
                               {'role': 'user', 'content': 'Visible conversation'}])
        self.results.extend([self.failure(401, 'invalid_token'), self.completed()])
        result = await self.request('POST', '/v1/responses/compact', json=body)
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(len(self.http_calls), 1)

    async def test_account_bound_previous_response_reference_refuses_replay(self):
        self.results.extend([self.failure(429, 'insufficient_quota'), self.completed()])
        result = await self.handle(self.body(previous_response_id='resp-account-only'))
        self.assertEqual(result.status_code, 409)
        self.assertEqual(len(self.http_calls), 1)

    async def test_recording_result_failure_does_not_replay_completed_generation(self):
        self.results.append(self.completed())
        with mock.patch.object(self.pool, 'record_result', side_effect=bps_credentials.PoolError('Synthetic persistence failure')):
            result = await self.handle()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(self.http_calls), 1)

    async def test_settings_reject_invalid_values_and_keep_last_valid_config(self):
        desired = {'enabled': True, 'url': 'http://127.0.0.1:7890'}
        self.settings.save(desired)
        for payload in ([], {'enabled': 'true'}, {'enabled': True, 'url': 'socks5://localhost:7890'},
                        {'enabled': True, 'url': 'http://user:secret@localhost:7890'},
                        {'enabled': True, 'url': 'http://127.0.0.1:8001'},
                        {'enabled': True, 'url': 'http://localhost:7890/path'}):
            with self.subTest(payload=payload):
                result = await self.request('POST', '/api/config/outbound-proxy', json=payload)
                self.assertEqual(result.status_code, 400, result.text)
                self.assertEqual(self.settings.load(), desired)
        result = await self.request('POST', '/api/config/outbound-proxy', content='{broken', headers={'Content-Type': 'application/json'})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.settings.load(), desired)

    async def test_management_guards_reject_remote_host_origin_and_cross_site(self):
        routes = [('GET', '/api/config/outbound-proxy'), ('POST', '/api/config/outbound-proxy'),
                  ('GET', '/api/credentials'), ('POST', f'/api/credentials/{self.ids[0]}'),
                  ('DELETE', f'/api/credentials/{self.ids[0]}'), ('POST', f'/api/credentials/{self.ids[0]}/test')]
        cases = ({'remote': '203.0.113.10'}, {'host': f'evil.example:{PORT}'},
                 {'headers': {'Origin': 'http://evil.example'}},
                 {'headers': {'Origin': f'https://localhost:{PORT}'}},
                 {'headers': {'Sec-Fetch-Site': 'cross-site'}})
        before = self.pool.list_credentials()
        for method, path in routes:
            for case in cases:
                with self.subTest(method=method, path=path, guard=case):
                    result = await self.request(method, path, json={}, **case)
                    self.assertEqual(result.status_code, 403, result.text)
        self.assertEqual(self.pool.list_credentials(), before)
        self.assertFalse(Path(self.settings.path).exists())
        self.assertEqual(self.http_calls, [])

    async def test_management_writes_require_json_content_type(self):
        routes = [('POST', '/api/config/outbound-proxy'), ('POST', f'/api/credentials/{self.ids[0]}'),
                  ('DELETE', f'/api/credentials/{self.ids[0]}'), ('POST', f'/api/credentials/{self.ids[0]}/test')]
        for method, path in routes:
            for headers in ({}, {'Content-Type': 'text/plain'}):
                with self.subTest(method=method, path=path, headers=headers):
                    result = await self.request(method, path, content='{}', headers=headers)
                    self.assertEqual(result.status_code, 415, result.text)
        self.assertEqual(self.http_calls, [])

    async def test_allowed_loopback_hosts_and_ipv6_peer(self):
        for host in (f'localhost:{PORT}', f'127.0.0.1:{PORT}'):
            for remote in ('127.0.0.1', '::1'):
                with self.subTest(host=host, remote=remote):
                    result = await self.request('GET', '/api/credentials', host=host, remote=remote, headers={'Origin': f'http://{host}'})
                    self.assertEqual(result.status_code, 200, result.text)
                    self.assertEqual(result.headers['cache-control'], 'no-store')

    def assert_no_secrets(self, payload):
        serialized = json.dumps(payload)
        for secret in self.secrets:
            self.assertNotIn(secret, serialized)
        def visit(value):
            if isinstance(value, dict):
                self.assertFalse(set(value) & {'tokens', 'access_token', 'refresh_token', 'headers', 'authorization', 'account_id'})
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(payload)

    async def test_credentials_list_redacts_tokens_even_from_labels(self):
        self.pool.update(self.ids[0], {'label': 'Label ' + self.secrets[2]})
        result = await self.request('GET', '/api/credentials')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json()['credentials']), 2)
        self.assert_no_secrets(result.json())
        self.assertEqual(result.headers['cache-control'], 'no-store')

    async def test_credentials_update_delete_and_validation_use_real_pool(self):
        path = f'/api/credentials/{self.ids[0]}'
        result = await self.request('POST', path, json={'enabled': False, 'label': 'Renamed fixture'})
        self.assertEqual(result.status_code, 200, result.text)
        row = next(row for row in result.json()['credentials'] if row['id'] == self.ids[0])
        self.assertFalse(row['enabled'])
        self.assertEqual(row['label'], 'Renamed fixture')
        self.assert_no_secrets(result.json())
        for payload in ([], {'enabled': 'false'}, {'access_token': 'never-accepted'}):
            result = await self.request('POST', path, json=payload)
            self.assertEqual(result.status_code, 400, result.text)
        result = await self.request('DELETE', path, headers={'Content-Type': 'application/json'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual([row['id'] for row in result.json()['credentials']], [self.ids[1]])
        self.assert_no_secrets(result.json())
        result = await self.request('DELETE', path, headers={'Content-Type': 'application/json'})
        self.assertEqual(result.status_code, 404, result.text)

    async def test_credentials_probe_validates_real_completed_output_and_redacts_list(self):
        upstream = self.completed()
        self.results.append(upstream)
        with mock.patch.object(self.pool, 'record_probe', wraps=self.pool.record_probe) as record:
            result = await self.request('POST', f'/api/credentials/{self.ids[0]}/test', json={})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['ok'])
        self.assertEqual(upstream.headers['x-ghcp-credential-id'], self.ids[0])
        record.assert_called_once()
        self.assertEqual(record.call_args.args[0], self.ids[0])
        self.assertEqual(record.call_args.kwargs['expected_revision'], int(upstream.headers['x-ghcp-credential-revision']))
        self.assertTrue(self.account_status(self.ids[0])['bps_verified'])
        self.assert_no_secrets(result.json())
        self.assertEqual(len(self.http_calls), 1)

    async def test_failed_probe_does_not_mark_http_200_error_as_verified(self):
        self.results.append(JSONResponse({'status': 'failed', 'error': {'code': 'insufficient_quota'}, 'output': []}))
        result = await self.request('POST', f'/api/credentials/{self.ids[0]}/test', json={})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()['ok'])
        self.assertFalse(self.pool.list_credentials()['credentials'][0]['bps_verified'])
        self.assertEqual(len(self.http_calls), 1)


    def error_plan(self, *, bps):
        # Execute the real dataclass definition; do not invent upstream_path or
        # add attributes that would hide integration errors in the new guard.
        return self.ns['UpstreamRequestPlan'](
            request_id='fixture-error-request',
            upstream_url=('https://bps.openai.com/basispoints/api/responses' if bps else 'https://copilot.invalid/responses'),
            headers={}, body={}, usage_event={'upstream_path': '/basispoints/api/responses' if bps else '/responses'},
            requested_model=MODEL, resolved_model=MODEL,
        )

    def synthetic_fixture(self):
        synthetic = upstream_errors.SyntheticReply(
            status_for_trace=429, client_status=200, message='Synthetic Copilot limit fixture',
            reason='offline-integration-fixture', usage_shape='zero',
        )
        self.ns['upstream_errors'].translate.return_value = synthetic
        return synthetic

    async def test_bps_retryable_http_errors_bypass_synthetic_translator(self):
        self.synthetic_fixture()
        plan = self.error_plan(bps=True)
        fallback = mock.Mock(side_effect=AssertionError('Fallback must not mask BPS errors'))
        for status, code in ((401, 'invalid_token'), (429, 'insufficient_quota'), (502, 'server_error')):
            for stream in (False, True):
                with self.subTest(status=status, stream=stream):
                    payload = {'error': {'code': code, 'message': 'Synthetic upstream failure'}}
                    upstream = httpx.Response(status, json=payload, headers={'retry-after': '37'})
                    result = self.ns['_handle_upstream_error'](upstream, trace_plan=plan,
                        caller_protocol='responses', stream=stream, model=MODEL, fallback_error_response=fallback)
                    self.assertEqual(result.status_code, status)
                    self.assertEqual(result.headers['retry-after'], '37')
                    self.assertEqual(json.loads(result.body), payload)
                    self.ns['upstream_errors'].translate.assert_not_called()
                    self.ns['_publish_synthetic_reply_event'].assert_not_called()
                    self.assertEqual(self.ns['_finish_usage_and_trace'].call_args.args[1], status)
        fallback.assert_not_called()

    async def test_non_bps_http_429_retains_existing_synthetic_response(self):
        synthetic = self.synthetic_fixture()
        upstream = httpx.Response(429, json={'error': {'code': 'insufficient_quota'}}, headers={'retry-after': '37'})
        fallback = mock.Mock(side_effect=AssertionError('Synthetic response should handle non-BPS quota'))
        result = self.ns['_handle_upstream_error'](upstream, trace_plan=self.error_plan(bps=False),
            caller_protocol='responses', stream=False, model='gpt-5.4', fallback_error_response=fallback)
        self.assertEqual(result.status_code, 200)
        self.assertIn(synthetic.message, result.body.decode())
        self.ns['upstream_errors'].translate.assert_called_once_with(upstream)
        self.ns['_publish_synthetic_reply_event'].assert_called_once()
        fallback.assert_not_called()

    async def test_no_trace_plan_retains_existing_synthetic_response(self):
        self.synthetic_fixture()
        upstream = httpx.Response(429, json={'error': {'code': 'insufficient_quota'}})
        result = self.ns['_handle_upstream_error'](upstream, trace_plan=None,
            caller_protocol='responses', stream=False, model='gpt-5.4',
            fallback_error_response=self.ns['proxy_non_streaming_response'])
        self.assertEqual(result.status_code, 200)
        self.ns['upstream_errors'].translate.assert_called_once_with(upstream)


    def account_status(self, credential_id):
        return next(row for row in self.pool.list_credentials()['credentials'] if row['id'] == credential_id)

    async def test_function_call_late_failure_rotates_only_the_next_request(self):
        response, stream = self.streaming({'type': 'response.created'},
            {'type': 'response.output_item.added', 'item': {'type': 'function_call', 'name': 'fixture', 'call_id': 'late-call'}},
            quota_event())
        self.results.append(response)
        result = await self.handle(self.body(stream=True))
        credential_id = result.headers['x-ghcp-credential-id']
        wire = await self.consume(result)
        self.assertIn(b'late-call', wire)
        self.assertIn(b'insufficient_quota', wire)
        self.assertEqual(len(self.http_calls), 1)
        self.assertEqual(stream.closed, 1)
        self.assertEqual(self.account_status(credential_id)['status'], 'cooldown')
        self.results.append(self.completed())
        next_result = await self.handle()
        self.assertNotEqual(next_result.headers['x-ghcp-credential-id'], credential_id)
        self.assertEqual(len(self.http_calls), 2)

    async def test_duplicate_late_failure_events_record_cooldown_once(self):
        events = [{'type': 'response.created'}, {'type': 'response.output_text.delta', 'delta': 'visible'},
                  quota_event(), quota_event()]
        response, stream = self.streaming(*events)
        self.results.append(response)
        with mock.patch.object(self.pool, 'record_result', wraps=self.pool.record_result) as record:
            result = await self.handle(self.body(stream=True))
            wire = await self.consume(result)
        self.assertEqual(wire, b''.join(event_bytes(event) for event in events))
        self.assertEqual(len([call for call in record.call_args_list if call.args[1] == 429]), 1)
        self.assertEqual(stream.closed, 1)
        self.assertEqual(len(self.http_calls), 1)

    async def test_late_failure_for_old_revision_does_not_cool_reenabled_account(self):
        response, stream = self.streaming({'type': 'response.created'},
            {'type': 'response.output_text.delta', 'delta': 'visible'}, quota_event())
        self.results.append(response)
        result = await self.handle(self.body(stream=True))
        credential_id = result.headers['x-ghcp-credential-id']
        selected_revision = int(result.headers['x-ghcp-credential-revision'])
        self.pool.update(credential_id, {'enabled': True, 'label': 'Reenabled while streaming'})
        self.assertGreater(self.pool._get(credential_id).revision, selected_revision)
        with mock.patch.object(self.pool, 'record_result', wraps=self.pool.record_result) as record:
            await self.consume(result)
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs['expected_revision'], selected_revision)
        self.assertEqual(self.account_status(credential_id)['status'], 'active')
        self.assertEqual(stream.closed, 1)
        self.assertEqual(len(self.http_calls), 1)
        self.results.append(self.completed())
        next_result = await self.handle()
        self.assertEqual(next_result.headers['x-ghcp-credential-id'], credential_id)

    async def test_late_failure_recording_error_does_not_interrupt_or_replay_stream(self):
        events = [{'type': 'response.created'}, {'type': 'response.output_text.delta', 'delta': 'visible'}, quota_event()]
        response, stream = self.streaming(*events)
        self.results.append(response)
        result = await self.handle(self.body(stream=True))
        with mock.patch.object(self.pool, 'record_result', side_effect=bps_credentials.PoolError('Synthetic recording failure')) as record:
            wire = await self.consume(result)
        record.assert_called_once()
        self.assertEqual(wire, b''.join(event_bytes(event) for event in events))
        self.assertEqual(len(self.http_calls), 1)
        self.assertEqual(stream.closed, 1)

    async def test_disabled_probe_without_selection_headers_cannot_change_verification(self):
        credential_id = self.ids[0]
        self.pool.update(credential_id, {'enabled': False})
        before = self.account_status(credential_id)
        with mock.patch.object(self.pool, 'record_probe', wraps=self.pool.record_probe) as record:
            result = await self.request('POST', f'/api/credentials/{credential_id}/test', json={})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()['ok'])
        record.assert_not_called()
        self.assertEqual(self.account_status(credential_id), before)
        self.assertEqual(self.http_calls, [])

    async def test_probe_success_for_old_revision_cannot_verify_edited_credential(self):
        credential_id = self.ids[0]
        selected_revision = self.pool._get(credential_id).revision
        upstream = self.completed()
        self.results.append(upstream)
        async def complete_after_edit(plan, **kwargs):
            self.pool.update(credential_id, {'label': 'Edited during probe'})
            return await self.post_result(plan, **kwargs)
        with mock.patch.dict(self.ns, {'_post_excel_non_streaming_request': mock.AsyncMock(side_effect=complete_after_edit)}):
            with mock.patch.object(self.pool, 'record_probe', wraps=self.pool.record_probe) as record:
                result = await self.request('POST', f'/api/credentials/{credential_id}/test', json={})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['ok'])
        self.assertEqual(upstream.headers['x-ghcp-credential-id'], credential_id)
        self.assertEqual(int(upstream.headers['x-ghcp-credential-revision']), selected_revision)
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs['expected_revision'], selected_revision)
        self.assertGreater(self.pool._get(credential_id).revision, selected_revision)
        self.assertFalse(self.account_status(credential_id)['bps_verified'])
        self.assertEqual(self.account_status(credential_id)['label'], 'Edited during probe')
        self.assertEqual(len(self.http_calls), 1)


    async def test_readable_compaction_summary_survives_second_account_wire(self):
        summary = 'Readable compact fixture: retain the chosen route and pending checks.'
        encoded = 'ghcp_proxy_summary_v1:' + base64.b64encode(summary.encode()).decode()
        follow_up = 'Keep this follow-up requirement after the earlier summary.'
        body = self.body(input=[
            {'type': 'compaction', 'encrypted_content': encoded},
            {'role': 'user', 'content': [{'type': 'input_text', 'text': follow_up}]},
        ])
        completed_text = 'Fresh compact output from the second account.'
        self.results.extend([self.failure(429, 'insufficient_quota'), self.completed(completed_text)])
        with mock.patch.object(bps_failover, 'prepare_failover_body', wraps=bps_failover.prepare_failover_body) as replay:
            result = await self.request('POST', '/v1/responses/compact', json=body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(self.http_calls), 2)
        self.assertNotEqual(self.http_calls[0].headers['authorization'], self.http_calls[1].headers['authorization'])
        self.assertEqual(result.headers['x-ghcp-credential-id'], self.http_calls[1].trace_metadata['credential_id'])
        self.assertEqual(result.headers['x-ghcp-credential-attempt'], '2')
        replay.assert_called_once()
        # Failover sees the original readable compaction, before the compact
        # request builder expands/sanitizes the transcript for the new account.
        self.assertEqual(replay.call_args.args[0]['input'][0], body['input'][0])
        for plan in self.http_calls:
            wire = json.dumps(plan.body['input'])
            self.assertIn(summary, wire)
            self.assertIn(follow_up, wire)
            self.assertNotIn(encoded, wire, 'Readable summaries must be expanded, not forwarded as opaque ciphertext')
            self.assertEqual(plan.source_body, body)
        output = result.json()['output'][0]
        self.assertEqual(output['type'], 'compaction')
        self.assertEqual(format_translation.decode_fake_compaction(output['encrypted_content']), completed_text)
        self.ns['copilot_sdk_upstream'].enabled.assert_not_called()
        self.ns['auth'].get_api_key.assert_not_called()

    async def test_compact_local_input_file_is_materialized_once_and_survives_failover(self):
        summary = 'Readable context before the attached local notes.'
        encoded = 'ghcp_proxy_summary_v1:' + base64.b64encode(summary.encode()).decode()
        file_text = 'Original local compact attachment must survive account failover.'
        body = self.body(input=[
            {'type': 'compaction', 'encrypted_content': encoded},
            {'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'Preserve the attached local notes.'},
                {'type': 'input_file', 'file_id': 'local-compact-notes'},
            ]},
        ])
        self.ns['_attachment_store'].get.side_effect = None
        self.ns['_attachment_store'].get.return_value = {'filename': 'compact-notes.txt', 'data': file_text.encode()}
        self.results.extend([self.failure(401, 'invalid_token'), self.completed('Compact with preserved notes.')])
        with (
            mock.patch.object(attachment_inputs, 'materialize_input_files', wraps=attachment_inputs.materialize_input_files) as materialize,
            mock.patch.object(bps_failover, 'prepare_failover_body', wraps=bps_failover.prepare_failover_body) as replay,
        ):
            result = await self.request('POST', '/v1/responses/compact', json=body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(self.http_calls), 2)
        self.assertNotEqual(self.http_calls[0].headers['authorization'], self.http_calls[1].headers['authorization'])
        self.assertEqual(result.headers['x-ghcp-credential-id'], self.http_calls[1].trace_metadata['credential_id'])
        materialize.assert_called_once()
        self.assertEqual(materialize.call_args.args[0], body)
        self.ns['_attachment_store'].get.assert_called_once_with('local-compact-notes', 'synthetic-owner')
        replay.assert_called_once()
        portable_original = replay.call_args.args[0]
        self.assertEqual(portable_original['input'][0], body['input'][0])
        self.assertIn(file_text, json.dumps(portable_original['input']))
        for plan in self.http_calls:
            wire = json.dumps(plan.body['input'])
            self.assertIn(file_text, wire)
            self.assertIn(summary, wire)
            self.assertNotIn('local-compact-notes', wire)
            self.assertNotIn('input_file', wire)
            self.assertEqual(plan.source_body, body)
        self.assertEqual(result.json()['output'][0]['type'], 'compaction')


    async def assert_astra_fallback(self, incoming):
        for path in ('/responses', '/v1/responses', '/responses/compact', '/v1/responses/compact'):
            with self.subTest(path=path, model=incoming.get('model')):
                self.results.append(self.completed(model='gpt-6-astra'))
                before = len(self.http_calls)
                result = await self.request('POST', path, json=incoming)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(len(self.http_calls), before + 1)
                self.assertEqual(self.plans[-1].body['model'], 'gpt-6-astra')
                self.assertEqual(self.plans[-1].source_body, incoming)
                # Compact reports the resolved canonical ID; the upstream wire uses the bare ID.
                self.assertEqual(result.json()['model'], 'gpt-6-astra-excel' if path.endswith('/compact') else 'gpt-6-astra')
                self.assertIn('x-ghcp-credential-id', result.headers)
                if path.endswith('/compact'):
                    self.assertEqual(result.json()['output'][0]['type'], 'compaction')

    async def test_deleted_builtin_alias_falls_back_to_astra_not_original_sol(self):
        self.routing.save_settings({'builtin_mappings': []})
        for name in ('gpt-6-sol', 'gpt-6-sol-basispoints'):
            self.assertIsNone(self.routing.resolve_bps_model(name))
            await self.assert_astra_fallback(self.body(model=name))
        self.assertEqual(self.routing.load_settings()['builtin_mappings'], [])

    async def test_disabled_builtin_alias_falls_back_to_astra_not_original_sol(self):
        self.routing.save_settings({'builtin_mappings': [
            {'source_model': 'gpt-6-sol,gpt-6-sol-basispoints', 'target_model': MODEL, 'enabled': False}]})
        for name in ('gpt-6-sol', 'gpt-6-sol-basispoints'):
            self.assertIsNone(self.routing.resolve_bps_model(name))
            await self.assert_astra_fallback(self.body(model=name))
        self.assertFalse(self.routing.load_settings()['builtin_mappings'][0]['enabled'])

    async def test_unknown_empty_and_missing_models_use_astra_and_preserve_source(self):
        for name in ('unknown-fixture', 'copilot/gpt-4.1', 'claude-unknown', '', '   ', None):
            await self.assert_astra_fallback(self.body(model=name, instructions='Preserve source', metadata={'fixture': 'offline'}))
        incoming = self.body()
        del incoming['model']
        await self.assert_astra_fallback(incoming)

    async def test_disabled_custom_mapping_cannot_override_astra_fallback(self):
        self.routing.save_settings({'enabled': False, 'mappings': [
            {'source_model': 'custom-disabled', 'target_model': MODEL}]})
        await self.assert_astra_fallback(self.body(model='custom-disabled'))

    async def test_invalid_model_types_return_400_without_dispatch(self):
        for path in ('/responses', '/v1/responses', '/responses/compact', '/v1/responses/compact'):
            for model in (True, False, 0, 7, 1.5, [], {}, ['gpt-6-sol'], {'name': 'gpt-6-sol'}):
                with self.subTest(path=path, model=model):
                    result = await self.request('POST', path, json=self.body(model=model))
                    self.assertEqual(result.status_code, 400, result.text)
                    self.assertIn('model must be a string', result.json()['error']['message'])
        self.assertEqual(self.plans, [])
        self.assertEqual(self.http_calls, [])

    async def test_custom_mapping_overrides_builtin_and_canonical_models_on_all_ingresses(self):
        for name in ('customer/unknown', 'gpt-6-sol-basispoints', 'gpt-6-astra-excel'):
            self.routing.save_settings({'enabled': True, 'mappings': [
                {'source_model': name, 'target_model': 'gpt-6-luna-excel'}]})
            for path in ('/responses', '/v1/responses', '/responses/compact', '/v1/responses/compact'):
                with self.subTest(path=path, model=name):
                    incoming = self.body(model=name, metadata={'fixture': 'mapping-wins'})
                    self.results.append(self.completed(model='gpt-6-luna'))
                    result = await self.request('POST', path, json=incoming)
                    self.assertEqual(result.status_code, 200, result.text)
                    self.assertEqual(self.plans[-1].body['model'], 'gpt-6-luna')
                    self.assertEqual(self.plans[-1].source_body, incoming)

    async def test_disabled_chat_and_messages_never_dispatch_or_parse_credentials(self):
        handler = mock.AsyncMock(side_effect=AssertionError('BPS dispatch forbidden'))
        with mock.patch.dict(self.ns, {'_handle_excel_responses': handler}):
            for path in ('/chat/completions', '/v1/chat/completions', '/v1/messages'):
                for body in (self.body(model='unknown'), {'model': 7}, {'messages': []}):
                    with self.subTest(path=path, body=body):
                        result = await self.request('POST', path, json=body)
                        self.assertEqual(result.status_code, 501, result.text)
                        self.assertIn('/v1/responses', result.json()['error']['message'])
            handler.assert_not_awaited()
        self.assertEqual(self.http_calls, [])

    async def test_disabled_auth_status_and_device_do_not_call_auth_or_sdk(self):
        status = await self.request('GET', '/api/auth/status')
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()['status'], 'disabled')
        self.assertIs(status.json()['enabled'], False)
        self.assertIs(status.json()['authenticated'], False)
        self.assertEqual(status.headers['cache-control'], 'no-store')
        for kwargs in ({'json': {}}, {'content': b'not-json'}):
            device = await self.request('POST', '/api/auth/device', **kwargs)
            self.assertEqual(device.status_code, 410, device.text)
            self.assertEqual(device.json()['error']['code'], 'copilot_disabled')
            self.assertEqual(device.headers['cache-control'], 'no-store')
        self.assertEqual(self.http_calls, [])

    async def test_canonical_model_survives_deleted_builtins_at_both_ingresses(self):
        self.routing.save_settings({'builtin_mappings': []})
        for path in ('/v1/responses', '/v1/responses/compact'):
            self.results.append(self.completed())
            result = await self.request('POST', path, json=self.body(model=MODEL))
            self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(self.http_calls), 2)

    async def test_editing_builtin_target_changes_actual_dispatch(self):
        self.routing.save_settings({'builtin_mappings': [{'source_model': 'gpt-6-sol,gpt-6-sol-basispoints',
            'target_model': 'gpt-6-luna-excel', 'enabled': True}]})
        self.results.append(self.completed())
        result = await self.request('POST', '/v1/responses', json=self.body(model='gpt-6-sol-basispoints'))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.http_calls[0].body['model'], 'gpt-6-luna')


if __name__ == '__main__':
    unittest.main()
