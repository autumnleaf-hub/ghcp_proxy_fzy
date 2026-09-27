"""Offline model discovery regressions; run with .venv/Scripts/python.exe -B.

Import proxy with all repository dependencies replaced before execution, so no
real global service, migration, credential loader, or background task exists.
Only the pure local catalog definitions are compiled
from their source into stubs. ASGI requests never run application lifespan.
"""
import ast
import importlib.util
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response


ROOT = Path(__file__).resolve().parents[1]


def _stub_module(name):
    module = types.ModuleType(name)

    def attribute(key):
        if key.startswith('__'):
            raise AttributeError(key)
        value = mock.MagicMock(name=f'{name}.{key}')
        setattr(module, key, value)
        return value

    module.__getattr__ = attribute
    return module


def _load_definitions(filename, namespace, names):
    """Execute only named definitions, never a module's service initializers."""
    path = ROOT / filename
    tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
    selected = []
    found = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            keys = {node.name}
        elif isinstance(node, ast.Assign):
            keys = {target.id for target in node.targets if isinstance(target, ast.Name)}
        else:
            continue
        if keys & names:
            selected.append(node)
            found.update(keys & names)
    if found != names:
        raise AssertionError(f'Missing isolated definitions: {names - found}')
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), 'exec'), namespace)


class LocalModelDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scratch = self.enterContext(tempfile.TemporaryDirectory(prefix='ghcp-model-discovery-'))
        state = Path(scratch)
        env = {key: str(state / key.lower()) for key in (
            'GHCP_CONFIG_DIR', 'GHCP_STATE_DIR', 'GHCP_CACHE_DIR',
            'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA',
            'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'CODEX_HOME',
        )}
        env.update({
            'GHCP_AUTO_UPDATE': '0',
            'GHCP_CODEX_NATIVE_INGEST_INTERVAL': '0',
            'GHCP_COPILOT_SDK_INGEST_INTERVAL': '0',
            'GHCP_CACHE_DB_PATH': str(state / 'cache.sqlite3'),
            'GHCP_PORT': '0',
        })
        self.enterContext(mock.patch.dict(os.environ, env))
        self.enterContext(mock.patch.object(sys, 'dont_write_bytecode', True))
        self.blocked_calls = []

        def blocked(*args, **kwargs):
            self.blocked_calls.append('forbidden external side effect')
            raise AssertionError('Offline test attempted network, storage, process, or thread access')

        for target, attribute in (
            (socket.socket, 'connect'), (socket.socket, 'connect_ex'),
            (socket, 'create_connection'), (socket, 'getaddrinfo'),
            (subprocess, 'Popen'), (threading.Thread, 'start'),
            (sqlite3, 'connect'),
            (httpx.HTTPTransport, 'handle_request'),
            (httpx.AsyncHTTPTransport, 'handle_async_request'),
        ):
            self.enterContext(mock.patch.object(target, attribute, side_effect=blocked))

        source = ROOT / 'proxy.py'
        tree = ast.parse(source.read_text(encoding='utf-8-sig'))
        local_names = set()
        for node in ast.walk(tree):
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                if name and (ROOT / (name + '.py')).is_file():
                    local_names.add(name)
        self.dependencies = {name: _stub_module(name) for name in local_names}
        deps = self.dependencies
        deps['attachment_api'].create_attachment_router.return_value = APIRouter()
        deps['desktop_control'].DesktopServiceController.return_value.router = APIRouter()
        deps['migrate_runtime_paths'].migrate_legacy_runtime_files.return_value = []
        deps['auth'].get_api_key.side_effect = AssertionError('Real credentials are forbidden')
        deps['auth'].load_api_key_payload.side_effect = AssertionError('Real credentials are forbidden')

        self.catalog = deps['excel_upstream']
        _load_definitions('excel_upstream.py', vars(self.catalog), {
            'EXCEL_MODEL_UPSTREAMS', 'EXCEL_MODEL_ALIASES', 'BASISPOINTS_MODEL_ALIASES',
            'MODEL_IDS', 'PUBLIC_MODEL_IDS', 'local_model_payload', 'merge_local_models_payload',
        })
        self.sdk = deps['copilot_sdk_upstream']
        self.sdk_client = types.SimpleNamespace(list_models=mock.AsyncMock(return_value=[]))
        self.sdk._get_client = mock.AsyncMock(return_value=self.sdk_client)
        self.sdk.enabled.return_value = True
        self.sdk.Response = Response
        self.sdk.JSONResponse = JSONResponse
        self.sdk.excel_upstream = self.catalog
        self.sdk.format_translation = deps['format_translation']
        deps['format_translation'].openai_error_response.side_effect = (
            lambda status, message: JSONResponse(
                status_code=status, content={'error': {'message': message, 'type': 'api_error'}}
            )
        )
        self.sdk.models_response = mock.AsyncMock(side_effect=AssertionError('SDK discovery forbidden'))

        spec = importlib.util.spec_from_file_location('_offline_model_discovery_proxy', source)
        self.proxy = importlib.util.module_from_spec(spec)
        self.enterContext(mock.patch.dict(sys.modules, {**deps, spec.name: self.proxy}))
        spec.loader.exec_module(self.proxy)

    def tearDown(self):
        self.assertEqual(self.blocked_calls, [], 'A forbidden side effect was attempted')
        for name in ('codex_native_ingest', 'copilot_sdk_upstream'):
            self.dependencies[name].start_background_scanner.assert_not_called()
        self.dependencies['auto_update'].AutoUpdateRuntimeController.return_value.start_periodic_checks.assert_not_called()
        self.dependencies['openai_oauth'].login_service.load.assert_not_called()
        self.catalog.excel_session_store.load.assert_not_called()
        self.dependencies['auth'].get_api_key.assert_not_called()
        self.dependencies['auth'].load_api_key_payload.assert_not_called()
        self.sdk.enabled.assert_not_called()
        self.sdk._get_client.assert_not_awaited()
        self.sdk_client.list_models.assert_not_awaited()
        self.sdk.models_response.assert_not_awaited()

    async def request(self, path='/v1/models'):
        transport = httpx.ASGITransport(app=self.proxy.app)
        async with httpx.AsyncClient(transport=transport, base_url='http://offline.invalid') as client:
            return await client.get(path)

    def assert_local_catalog(self, response):
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload, self.catalog.merge_local_models_payload({}))
        self.assertEqual(payload['object'], 'list')
        ids = [row['id'] for row in payload['data']]
        self.assertIn('gpt-6-sol', ids)
        self.assertEqual(set(ids), set(self.catalog.PUBLIC_MODEL_IDS))
        self.assertEqual(len(ids), len(set(ids)))
        for row in payload['data']:
            self.assertEqual(row['object'], 'model')
            self.assertEqual(row['owned_by'], 'openai-excel')

    async def test_sdk_not_authenticated_is_never_queried_on_both_routes(self):
        self.sdk_client.list_models.side_effect = RuntimeError('models.list Not authenticated')
        for path in ('/v1/models', '/models'):
            with self.subTest(path=path):
                self.assert_local_catalog(await self.request(path))
        self.sdk_client.list_models.assert_not_awaited()

    async def test_sdk_client_start_failure_is_never_reached(self):
        self.sdk._get_client.side_effect = RuntimeError('Copilot is not configured')
        self.assert_local_catalog(await self.request())
        self.sdk._get_client.assert_not_awaited()

    async def test_successful_sdk_catalog_is_ignored_not_merged(self):
        self.sdk_client.list_models.return_value = [
            types.SimpleNamespace(id='copilot-only-fixture'),
            types.SimpleNamespace(id='gpt-6-sol'),
        ]
        response = await self.request()
        self.assert_local_catalog(response)
        self.assertNotIn('copilot-only-fixture', [row['id'] for row in response.json()['data']])
        self.sdk_client.list_models.assert_not_awaited()

    async def test_empty_sdk_catalog_is_never_queried(self):
        self.assert_local_catalog(await self.request())
        self.sdk_client.list_models.assert_not_awaited()

    async def test_http_backend_without_credentials_does_not_read_auth(self):
        self.sdk.enabled.return_value = False
        self.dependencies['auth'].get_api_key.side_effect = RuntimeError('Not authenticated')
        self.assert_local_catalog(await self.request())
        self.dependencies['auth'].get_api_key.assert_not_called()

    async def test_backend_errors_are_never_requested_or_retried(self):
        for sdk_enabled in (True, False):
            for status in (401, 403, 429, 500, 502, 503):
                with self.subTest(sdk=sdk_enabled, status=status):
                    self.sdk.enabled.return_value = sdk_enabled
                    error = JSONResponse(status_code=status, content={'error': {'message': 'offline'}})
                    with mock.patch.object(self.sdk, 'models_response', new=mock.AsyncMock(return_value=error)) as sdk_call, mock.patch.object(self.proxy, '_proxy_models_request', new=mock.AsyncMock(return_value=error)) as http_call:
                        self.assert_local_catalog(await self.request())
                        sdk_call.assert_not_awaited()
                        http_call.assert_not_awaited()

    async def test_successful_backend_payload_and_headers_are_not_exposed(self):
        payload = {'object': 'list', 'data': [{'id': 'copilot-fixture', 'owned_by': 'github-copilot'}], 'extra': 'upstream-only'}
        for sdk_enabled in (True, False):
            with self.subTest(sdk=sdk_enabled):
                self.sdk.enabled.return_value = sdk_enabled
                success = JSONResponse(payload, headers={'x-discovery-fixture': 'upstream-only'})
                with mock.patch.object(self.sdk, 'models_response', new=mock.AsyncMock(return_value=success)) as sdk_call, mock.patch.object(self.proxy, '_proxy_models_request', new=mock.AsyncMock(return_value=success)) as http_call:
                    for path in ('/models', '/v1/models'):
                        response = await self.request(path)
                        self.assert_local_catalog(response)
                        self.assertNotIn('x-discovery-fixture', response.headers)
                        self.assertNotIn('extra', response.json())
                    sdk_call.assert_not_awaited()
                    http_call.assert_not_awaited()

    def test_startup_does_not_schedule_any_scanner(self):
        tree = ast.parse((ROOT / 'proxy.py').read_text(encoding='utf-8-sig'))
        startup = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == '_app_startup_restore_client_proxy_configs')
        calls = [ast.unparse(node.func) for node in ast.walk(startup) if isinstance(node, ast.Call)]
        references = [node.attr for node in ast.walk(startup) if isinstance(node, ast.Attribute)]
        self.assertFalse(any('scanner' in call or 'copilot_sdk' in call for call in calls + references), calls)


if __name__ == '__main__':
    unittest.main()
