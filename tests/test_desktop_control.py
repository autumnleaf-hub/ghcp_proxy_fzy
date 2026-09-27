"""Offline ASGI tests: no listener, real PID, process control, or user settings.

Run with ./.venv/Scripts/python.exe -B -m unittest -v tests.test_desktop_control
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx
from fastapi import FastAPI

with ExitStack() as _imports:
    for _target in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo',
                    'subprocess.Popen', 'subprocess.run', 'os.kill'):
        _imports.enter_context(mock.patch(_target, side_effect=AssertionError('offline import')))
    import desktop_control

PORT = 49177  # ASGI scope metadata only; never bound or connected.
PID = 424242  # Synthetic identity; never query or signal a real process.
IDENTITY = '/api/desktop/identity'
STOP = '/api/desktop/stop'


class DesktopControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='desktop-control-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.guards = self.enterContext(ExitStack())
        for target in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo',
                       'subprocess.Popen', 'subprocess.run', 'os.kill', 'os.execv', 'os._exit'):
            self.guards.enter_context(mock.patch(target, side_effect=AssertionError('external I/O forbidden')))
        self.guards.enter_context(mock.patch.object(desktop_control.os, 'getpid', return_value=PID))
        self.callback = mock.Mock(name='shutdown_callback')
        self.getter = mock.Mock(return_value=self.callback)
        self.controller = desktop_control.DesktopServiceController(self.root, PORT, self.getter)
        self.app = FastAPI()
        self.app.include_router(self.controller.router)

    async def request(self, method='GET', path=IDENTITY, *, peer=('127.0.0.1', 12345), **kwargs):
        transport = httpx.ASGITransport(app=self.app, client=peer)
        async with httpx.AsyncClient(transport=transport, base_url=f'http://127.0.0.1:{PORT}') as client:
            return await client.request(method, path, **kwargs)

    def body(self, **changes):
        return {'pid': PID, 'instance_id': self.controller.instance_id, **changes}

    def assert_inactive(self):
        self.assertFalse(self.controller.stopping)
        self.callback.assert_not_called()

    async def test_identity_reports_exact_root_synthetic_pid_and_no_store(self):
        result = await self.request()
        self.assertEqual(result.status_code, 200)
        identity = result.json()
        self.assertEqual(identity, self.controller.identity())
        self.assertEqual(identity['service'], 'ghcp_proxy')
        self.assertEqual(identity['pid'], PID)
        self.assertEqual(identity['port'], PORT)
        self.assertEqual(identity['project_root'], str(self.root.resolve()))
        self.assertNotEqual(identity['project_root'], str((self.root / 'wrong-root').resolve()))
        self.assertRegex(identity['instance_id'], r'^[0-9a-f]{32}$')
        self.assertIsInstance(identity['started_at'], (float, int))
        self.assertTrue(identity['graceful_stop_supported'])
        self.assertFalse(identity['stopping'])
        self.assertEqual(result.headers['cache-control'], 'no-store')
        self.assert_inactive()

    async def test_new_instance_same_pid_root_and_port_rejects_stale_nonce(self):
        old_nonce = self.controller.instance_id
        replacement = desktop_control.DesktopServiceController(self.root, PORT, self.getter)
        self.assertNotEqual(replacement.instance_id, old_nonce)
        self.controller = replacement
        self.app = FastAPI()
        self.app.include_router(replacement.router)
        result = await self.request('POST', STOP, json=self.body(instance_id=old_nonce))
        self.assertEqual(result.status_code, 409)
        self.assert_inactive()

    async def test_other_project_same_pid_cannot_reuse_its_identity(self):
        other = desktop_control.DesktopServiceController(self.root / 'other-project', PORT, self.getter)
        self.assertNotEqual(other.identity()['project_root'], self.controller.identity()['project_root'])
        result = await self.request('POST', STOP, json=other.identity())
        self.assertEqual(result.status_code, 409)
        self.assert_inactive()

    async def test_wrong_pid_and_non_integer_pid_rejected(self):
        for pid in (PID + 1, 0, -1, str(PID), float(PID), True, False, None, [], {}):
            with self.subTest(pid=pid):
                result = await self.request('POST', STOP, json=self.body(pid=pid))
                self.assertEqual(result.status_code, 409)
        self.assert_inactive()

    async def test_missing_fields_wrong_type_stale_and_unicode_nonces(self):
        bodies = [{}, {'pid': PID}, {'instance_id': self.controller.instance_id}]
        bodies += [self.body(instance_id=value) for value in
                   ('stale', '', None, 123, True, [], {}, '\u96ea', '\U0001f512',
                    self.controller.instance_id + '\u00e9', '\ud800')]
        for body in bodies:
            with self.subTest(body=body):
                result = await self.request('POST', STOP, content=json.dumps(body),
                                            headers={'content-type': 'application/json'})
                self.assertEqual(result.status_code, 409)
        self.assert_inactive()

    async def test_remote_and_missing_clients_rejected_on_both_routes(self):
        for peer in (('203.0.113.5', 1234), ('::ffff:127.0.0.1', 1234), None):
            for method, path in (('GET', IDENTITY), ('POST', STOP)):
                with self.subTest(peer=peer, method=method):
                    result = await self.request(method, path, peer=peer, json=self.body())
                    self.assertEqual(result.status_code, 403)
        self.assert_inactive()

    async def test_untrusted_hosts_and_origins_rejected(self):
        cases = [
            {'host': ''}, {'host': 'example.invalid'}, {'host': 'localhost'},
            {'host': f'127.0.0.1:{PORT + 1}'}, {'host': f'localhost.evil.invalid:{PORT}'},
            {'origin': 'null'}, {'origin': 'https://evil.invalid'},
            {'origin': f'https://127.0.0.1:{PORT}'},
            {'origin': f'http://localhost:{PORT}'},
            {'origin': f'http://127.0.0.1:{PORT}/'}, {'sec-fetch-site': 'cross-site'},
        ]
        for headers in cases:
            for method, path in (('GET', IDENTITY), ('POST', STOP)):
                with self.subTest(headers=headers, method=method):
                    result = await self.request(method, path, json=self.body(), headers=headers)
                    self.assertEqual(result.status_code, 403)
        self.assert_inactive()

    async def test_loopback_ipv6_peer_and_localhost_same_origin_allowed(self):
        for host, peer in ((f'127.0.0.1:{PORT}', ('127.0.0.1', 1234)),
                           (f'localhost:{PORT}', ('::1', 1234))):
            result = await self.request(peer=peer, headers={'host': host, 'origin': f'http://{host}'})
            self.assertEqual(result.status_code, 200)
        self.assert_inactive()

    async def test_write_requires_json_media_type(self):
        for media in ('', 'text/plain', 'application/x-www-form-urlencoded', 'application/jsonp'):
            with self.subTest(media=media):
                result = await self.request('POST', STOP, content=json.dumps(self.body()),
                                            headers={'content-type': media})
                self.assertEqual(result.status_code, 415)
        self.assert_inactive()

    async def test_malformed_json_and_non_object_bodies_return_400(self):
        for content in ('', '{', 'null', '[]', 'true', '1', '"text"', b'\xff'):
            with self.subTest(content=content):
                result = await self.request('POST', STOP, content=content,
                                            headers={'content-type': 'application/json'})
                self.assertEqual(result.status_code, 400)
        self.assert_inactive()

    async def test_external_server_without_callback_reports_unsupported_and_refuses_stop(self):
        for callback in (None, False, 'not-callable'):
            self.getter.return_value = callback
            result = await self.request()
            self.assertFalse(result.json()['graceful_stop_supported'])
            result = await self.request('POST', STOP, json=self.body())
            self.assertEqual(result.status_code, 409)
        self.assert_inactive()

    async def test_valid_stop_schedules_once_and_only_invokes_mock_callback(self):
        loop = asyncio.get_running_loop()
        with mock.patch.object(loop, 'call_later') as schedule:
            responses = await asyncio.gather(*[self.request('POST', STOP, json=self.body(),
                headers={'content-type': 'application/json; charset=utf-8'}) for _ in range(8)])
            for result in responses:
                self.assertEqual(result.status_code, 202)
                self.assertEqual(result.json(), {'stopping': True, 'pid': PID,
                                                  'instance_id': self.controller.instance_id})
                self.assertEqual(result.headers['cache-control'], 'no-store')
            schedule.assert_called_once_with(0.25, self.controller._trigger_stop, self.callback)
            self.callback.assert_not_called()
            identity = await self.request()
            self.assertTrue(identity.json()['stopping'])
            _, trigger, callback = schedule.call_args.args
            self.assertIs(callback, self.callback)
            trigger(callback)
        self.callback.assert_called_once_with()

    async def test_stale_request_after_stop_is_not_acknowledged(self):
        with mock.patch.object(asyncio.get_running_loop(), 'call_later') as schedule:
            self.assertEqual((await self.request('POST', STOP, json=self.body())).status_code, 202)
            stale = await self.request('POST', STOP, json=self.body(instance_id='old-instance'))
            self.assertEqual(stale.status_code, 409)
            schedule.assert_called_once()
        self.callback.assert_not_called()

    async def test_callback_exception_resets_state_and_allows_retry(self):
        self.callback.side_effect = RuntimeError('simulated callback failure')
        with mock.patch.object(asyncio.get_running_loop(), 'call_later') as schedule:
            self.assertEqual((await self.request('POST', STOP, json=self.body())).status_code, 202)
            with self.assertLogs('desktop_control', level='ERROR'):
                self.controller._trigger_stop(self.callback)
            self.assertFalse(self.controller.stopping)
            self.callback.side_effect = None
            self.assertEqual((await self.request('POST', STOP, json=self.body())).status_code, 202)
            self.assertEqual(schedule.call_count, 2)
        self.callback.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
