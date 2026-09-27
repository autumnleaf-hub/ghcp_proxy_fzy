"""Offline settings/runtime tests using AST-extracted production route bodies.

No proxy import/startup, sockets, git, real settings, registry, or process control.
Run: ./.venv/Scripts/python.exe -B -m unittest -v tests.test_auto_update_toggle
"""
from __future__ import annotations

import ast
import asyncio
from contextlib import ExitStack
import gzip
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

with ExitStack() as _imports:
    for _target in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo',
                    'subprocess.Popen', 'subprocess.run', 'os.kill'):
        _imports.enter_context(mock.patch(_target, side_effect=AssertionError('offline import')))
    import auto_update

ROOT = Path(__file__).resolve().parents[1]
PORT = 49178  # In-memory ASGI metadata only; no listener is created.
ROUTE = '/api/config/auto-update'


def extract_functions(path, names, namespace):
    """Execute actual definitions, removing only route decorators; never import proxy."""
    tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
    selected = [node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    if {node.name for node in selected} != set(names):
        raise AssertionError(f'Missing production definitions in {path.name}: {names}')
    for node in selected:
        node.decorator_list = []
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[]))
    exec(compile(module, str(path), 'exec'), namespace)


class TemporaryUpdaterFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='auto-update-toggle-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings_path = self.root / 'settings.json'
        self.state_path = self.root / 'state.json'
        self.lock_path = self.root / 'update.lock'
        self.repo_path = self.root / 'repo'
        self.repo_path.mkdir()
        self.guards = self.enterContext(ExitStack())
        self.guards.enter_context(mock.patch.dict(os.environ, {
            'GHCP_AUTO_UPDATE': '', 'GHCP_AUTO_UPDATE_MODE': '',
            'GHCP_AUTO_UPDATE_INTERVAL_SECONDS': '3600'}))
        for target in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo',
                       'subprocess.Popen', 'subprocess.run', 'os.kill', 'os.execv', 'os._exit'):
            self.guards.enter_context(mock.patch(target, side_effect=AssertionError('external I/O forbidden')))
        self.git = mock.Mock(side_effect=AssertionError('git execution forbidden'))
        self.manager = self.make_manager()

    def make_manager(self):
        return auto_update.AutoUpdateManager(repo_dir=str(self.repo_path),
            settings_file=str(self.settings_path), state_file=str(self.state_path),
            lock_file=str(self.lock_path), command_runner=self.git, clock=lambda: 2_000_000_000.0)

    def persist(self, payload):
        self.settings_path.write_text(json.dumps(payload), encoding='utf-8')

    def stored(self):
        return json.loads(self.settings_path.read_text(encoding='utf-8'))

    def assert_no_git_or_runtime_files(self):
        self.git.assert_not_called()
        self.assertFalse(self.state_path.exists())
        self.assertFalse(self.lock_path.exists())


class AutoUpdateToggleSettingsTests(TemporaryUpdaterFixture, unittest.TestCase):
    def test_missing_settings_enabled_by_default_without_writing(self):
        self.assertTrue(self.manager.enabled())
        self.assertEqual(self.manager.enabled_source(), 'default')
        self.assertEqual(self.manager.settings_payload()['enabled_source'], 'default')
        self.assertFalse(self.settings_path.exists())
        self.assert_no_git_or_runtime_files()

    def test_disabled_persists_and_both_startup_paths_skip_git_even_forced(self):
        saved = self.manager.set_enabled(False)
        self.assertFalse(saved['enabled'])
        self.assertEqual(saved['enabled_source'], 'settings')
        manager = self.make_manager()
        self.assertFalse(manager.enabled())
        for mode in ('user', 'developer'):
            manager.set_mode(mode)
            for force in (False, True):
                for method in (manager.startup_check_for_update, manager.startup_check_and_update):
                    with self.subTest(mode=mode, force=force, method=method.__name__):
                        result = method(force=force)
                        self.assertEqual(result['reason'], 'disabled')
                        self.assertFalse(result['attempted'])
                        self.assertFalse(result['updated'])
        self.assertFalse(self.stored()['enabled'])
        self.assert_no_git_or_runtime_files()

    def test_toggle_preserves_mode_and_unknown_settings_and_mode_preserves_toggle(self):
        self.persist({'mode': 'developer', 'future_setting': {'value': 7}})
        for value in (False, True, False):
            self.manager.set_enabled(value)
            self.assertEqual(self.stored(), {'mode': 'developer', 'future_setting': {'value': 7},
                                            'enabled': value})
        self.manager.set_mode('user')
        self.assertFalse(self.stored()['enabled'])
        self.assertEqual(self.stored()['future_setting'], {'value': 7})
        self.assertEqual(self.make_manager().mode(), 'user')
        self.assert_no_git_or_runtime_files()

    def test_set_enabled_rejects_non_boolean_without_modification(self):
        self.manager.set_enabled(False)
        before = self.settings_path.read_bytes()
        for value in (None, 'true', 'false', 0, 1, 0.0, 1.0, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.manager.set_enabled(value)
            self.assertEqual(self.settings_path.read_bytes(), before)
        self.assert_no_git_or_runtime_files()

    def test_saved_non_boolean_flags_use_default_not_truthiness(self):
        for value in (None, 'false', 'true', 0, 1, [], {}):
            with self.subTest(value=value):
                self.persist({'enabled': value})
                self.assertTrue(self.manager.enabled())
                self.assertEqual(self.manager.enabled_source(), 'default')
        for value in (False, True):
            self.persist({'enabled': value})
            self.assertIs(self.manager.enabled(), value)
            self.assertEqual(self.manager.enabled_source(), 'settings')

    def test_env_precedence_and_lock_preserve_saved_setting(self):
        for saved in (False, True):
            self.manager.set_enabled(saved)
            before = self.settings_path.read_bytes()
            for raw, expected in (('0', False), ('false', False), ('no', False), ('OFF', False),
                                  ('1', True), ('true', True), ('yes', True), (' ON ', True)):
                with self.subTest(saved=saved, env=raw), mock.patch.dict(os.environ, {'GHCP_AUTO_UPDATE': raw}):
                    self.assertIs(self.manager.enabled(), expected)
                    self.assertEqual(self.manager.enabled_source(), 'env')
                    with self.assertRaises(ValueError):
                        self.manager.set_enabled(not expected)
                    self.assertEqual(self.settings_path.read_bytes(), before)
            self.assertIs(self.manager.enabled(), saved)
        self.assert_no_git_or_runtime_files()

    def test_blank_environment_is_not_locked(self):
        for raw in ('', '   '):
            with mock.patch.dict(os.environ, {'GHCP_AUTO_UPDATE': raw}):
                self.manager.set_enabled(False)
                self.assertFalse(self.manager.enabled())
                self.assertEqual(self.manager.enabled_source(), 'settings')

    def test_atomic_replace_uses_fsynced_complete_same_directory_file(self):
        self.persist({'mode': 'developer', 'enabled': True, 'future_setting': 4})
        before = self.settings_path.read_bytes()
        replace = os.replace
        def inspect(source, destination):
            self.assertEqual(Path(source).parent, self.settings_path.parent)
            self.assertEqual(Path(destination), self.settings_path)
            self.assertEqual(self.settings_path.read_bytes(), before)
            self.assertEqual(json.loads(Path(source).read_text()),
                             {'mode': 'developer', 'enabled': False, 'future_setting': 4})
            return replace(source, destination)
        with mock.patch.object(auto_update.os, 'replace', side_effect=inspect) as replacement, \
             mock.patch.object(auto_update.os, 'fsync', wraps=os.fsync) as fsync:
            self.manager.set_enabled(False)
        replacement.assert_called_once()
        fsync.assert_called_once()
        self.assertEqual(set(self.root.iterdir()), {self.repo_path, self.settings_path})

    def test_failed_atomic_replace_preserves_settings_and_cleans_temp_file(self):
        self.manager.set_enabled(False)
        before = self.settings_path.read_bytes()
        with mock.patch.object(auto_update.os, 'replace', side_effect=OSError('simulated replace failure')):
            with self.assertRaises(OSError):
                self.manager.set_enabled(True)
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.assertEqual(set(self.root.iterdir()), {self.repo_path, self.settings_path})

    def test_failed_serialization_preserves_settings_and_cleans_temp_file(self):
        self.manager.set_enabled(False)
        before = self.settings_path.read_bytes()
        def partial_dump(payload, stream, **kwargs):
            stream.write('{')
            raise OSError('simulated partial write')
        with mock.patch.object(auto_update.json, 'dump', side_effect=partial_dump):
            with self.assertRaises(OSError):
                self.manager.set_enabled(True)
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.assertEqual(set(self.root.iterdir()), {self.repo_path, self.settings_path})


class AutoUpdateToggleRouteTests(TemporaryUpdaterFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.manager.set_enabled(False)
        self.restart = mock.Mock(name='restart_callback')
        self.runtime = auto_update.AutoUpdateRuntimeController(self.manager,
                            reexec_func=self.restart, logger=mock.Mock())
        # Keep the real periodic task/cancellation machinery; no git or thread work.
        self.check = self.guards.enter_context(mock.patch.object(self.runtime, 'run_due_check',
                                              new_callable=mock.AsyncMock, return_value={'reason': 'offline'}))
        self.start = self.guards.enter_context(mock.patch.object(self.runtime, 'start_periodic_checks',
                                                                wraps=self.runtime.start_periodic_checks))
        self.stop = self.guards.enter_context(mock.patch.object(self.runtime, 'stop_periodic_checks',
                                                               wraps=self.runtime.stop_periodic_checks))
        self.usage = SimpleNamespace(record_request_error=mock.Mock())
        parser_ns = {'json': json, 'gzip': gzip, 'zlib': zlib, 'HTTPException': HTTPException,
                     'utc_now_iso': lambda: '2033-05-18T03:33:20Z', 'brotli': None,
                     'zstd_decompress': mock.Mock(side_effect=AssertionError('not used'))}
        extract_functions(ROOT / 'util.py', {'parse_json_request'}, parser_ns)
        self.namespace = {'Request': Request, 'HTTPException': HTTPException, 'JSONResponse': JSONResponse,
            'PROXY_PORT': PORT, 'auto_update_manager': self.manager,
            'auto_update_runtime_controller': self.runtime, 'usage_tracker': self.usage,
            'util': SimpleNamespace(parse_json_request=parser_ns['parse_json_request'])}
        extract_functions(ROOT / 'proxy.py',
                          {'auto_update_config_api', '_require_local_bps_management', 'parse_json_request'},
                          self.namespace)
        self.app = FastAPI()
        self.app.add_api_route(ROUTE, self.namespace['auto_update_config_api'], methods=['POST'])

    async def asyncTearDown(self):
        await self.runtime.stop_periodic_checks()
        self.restart.assert_not_called()
        self.assert_no_git_or_runtime_files()

    async def request(self, *, peer=('127.0.0.1', 12345), **kwargs):
        transport = httpx.ASGITransport(app=self.app, client=peer, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=f'http://127.0.0.1:{PORT}') as client:
            return await client.post(ROUTE, **kwargs)

    async def toggle(self, value, **kwargs):
        return await self.request(json={'action': 'set_enabled', 'enabled': value}, **kwargs)

    async def test_runtime_enable_starts_task_disable_cancels_and_persists(self):
        self.assertFalse(self.runtime.start_periodic_checks())
        self.start.reset_mock()
        result = await self.toggle(True)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['enabled'])
        self.assertTrue(result.json()['settings']['enabled'])
        self.assertEqual(result.json()['enabled_source'], 'settings')
        self.assertTrue(result.json()['runtime']['periodic_task_running'])
        self.start.assert_called_once_with()
        self.stop.assert_not_awaited()
        task = self.runtime._task
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertTrue(self.make_manager().enabled())
        result = await self.toggle(False)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()['runtime']['periodic_task_running'])
        self.stop.assert_awaited_once_with()
        self.assertTrue(task.cancelled())
        self.assertIsNone(self.runtime._task)
        self.assertFalse(self.make_manager().enabled())
        self.check.assert_not_awaited()

    async def test_repeated_enable_reuses_task_and_reenable_creates_new_task(self):
        self.assertEqual((await self.toggle(True)).status_code, 200)
        original = self.runtime._task
        self.assertEqual((await self.toggle(True)).status_code, 200)
        self.assertIs(self.runtime._task, original)
        self.assertEqual((await self.toggle(False)).status_code, 200)
        self.assertTrue(original.cancelled())
        self.assertEqual((await self.toggle(True)).status_code, 200)
        self.assertIsNot(self.runtime._task, original)
        self.assertFalse(self.runtime._task.done())

    async def test_runtime_off_cancels_pending_restart_without_executing_it(self):
        self.assertEqual((await self.toggle(True)).status_code, 200)
        handle = asyncio.get_running_loop().call_later(3600, self.restart)
        self.runtime._restart_handle = handle
        self.assertEqual((await self.toggle(False)).status_code, 200)
        self.assertTrue(handle.cancelled())
        self.assertIsNone(self.runtime._restart_handle)
        self.restart.assert_not_called()

    async def test_env_locked_toggle_returns_400_without_persistence_or_scheduling(self):
        before = self.settings_path.read_bytes()
        for raw in ('0', '1', 'false', 'true'):
            with mock.patch.dict(os.environ, {'GHCP_AUTO_UPDATE': raw}):
                for desired in (False, True):
                    result = await self.toggle(desired)
                    self.assertEqual(result.status_code, 400, result.text)
                    self.assertIn('GHCP_AUTO_UPDATE', result.json()['detail'])
                    self.assertEqual(self.settings_path.read_bytes(), before)
        self.start.assert_not_called()
        self.stop.assert_not_awaited()

    async def test_invalid_or_missing_boolean_returns_400_without_mutation(self):
        before = self.settings_path.read_bytes()
        for value in (None, 'false', 'true', 0, 1, 0.0, 1.0, [], {}):
            with self.subTest(value=value):
                result = await self.toggle(value)
                self.assertEqual(result.status_code, 400, result.text)
                self.assertEqual(self.settings_path.read_bytes(), before)
        result = await self.request(json={'action': 'set_enabled'})
        self.assertEqual(result.status_code, 400)
        self.start.assert_not_called()
        self.stop.assert_not_awaited()

    async def test_malformed_json_returns_400_without_mutation(self):
        before = self.settings_path.read_bytes()
        with mock.patch('builtins.print'):
            for content in ('', '{', b'\xff'):
                result = await self.request(content=content, headers={'content-type': 'application/json'})
                self.assertEqual(result.status_code, 400, result.text)
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.start.assert_not_called()
        self.stop.assert_not_awaited()

    async def test_non_object_json_returns_400_not_server_error(self):
        before = self.settings_path.read_bytes()
        for content in ('null', '[]', 'true', '1', '"text"'):
            with self.subTest(content=content):
                result = await self.request(content=content, headers={'content-type': 'application/json'})
                self.assertEqual(result.status_code, 400, result.text)
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.start.assert_not_called()
        self.stop.assert_not_awaited()

    async def test_remote_host_origin_and_cross_site_security_guards(self):
        before = self.settings_path.read_bytes()
        cases = [
            {'peer': ('203.0.113.10', 1234)},
            {'headers': {'host': ''}}, {'headers': {'host': 'evil.invalid'}},
            {'headers': {'host': f'localhost:{PORT + 1}'}},
            {'headers': {'origin': 'null'}}, {'headers': {'origin': 'https://evil.invalid'}},
            {'headers': {'origin': f'http://localhost:{PORT}'}},
            {'headers': {'origin': f'https://127.0.0.1:{PORT}'}},
            {'headers': {'sec-fetch-site': 'cross-site'}},
        ]
        for kwargs in cases:
            with self.subTest(case=kwargs):
                result = await self.toggle(True, **kwargs)
                self.assertEqual(result.status_code, 403, result.text)
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.start.assert_not_called()
        self.stop.assert_not_awaited()

    async def test_media_type_security_guard(self):
        before = self.settings_path.read_bytes()
        for media in ('', 'text/plain', 'application/x-www-form-urlencoded', 'application/jsonp'):
            result = await self.request(content=json.dumps({'action': 'set_enabled', 'enabled': True}),
                                        headers={'content-type': media})
            self.assertEqual(result.status_code, 415, result.text)
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.start.assert_not_called()
        self.stop.assert_not_awaited()

    async def test_same_origin_localhost_ipv6_peer_and_json_charset_allowed(self):
        host = f'localhost:{PORT}'
        result = await self.toggle(True, peer=('::1', 1234), headers={
            'host': host, 'origin': f'http://{host}', 'content-type': 'application/json; charset=utf-8'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(self.runtime.status_payload()['runtime']['periodic_task_running'])

    async def test_set_mode_preserves_disabled_and_unknown_action_returns_400(self):
        result = await self.request(json={'action': 'set_mode', 'mode': 'developer'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(self.stored()['enabled'])
        self.assertEqual(self.stored()['mode'], 'developer')
        before = self.settings_path.read_bytes()
        result = await self.request(json={'action': 'not-supported'})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.start.assert_not_called()
        self.stop.assert_not_awaited()

    async def test_disabled_runtime_check_and_apply_do_not_launch_workers(self):
        with mock.patch('asyncio.to_thread', new_callable=mock.AsyncMock,
                        side_effect=AssertionError('worker execution forbidden')) as worker:
            # Class method bypasses only the fixture's periodic-check mock.
            checked = await auto_update.AutoUpdateRuntimeController.run_due_check(self.runtime, force=True)
            applied = await self.runtime.apply_update(override_local_changes=True)
        self.assertEqual(checked['reason'], 'disabled')
        self.assertEqual(applied['reason'], 'disabled')
        worker.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
