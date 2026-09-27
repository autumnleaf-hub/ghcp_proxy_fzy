"""Offline bounded-concurrency checks; no real upstream or live settings changes."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from request_concurrency import (
    ConcurrencyQueueFull, RequestConcurrencyLimiter, RequestConcurrencyMiddleware,
    RequestConcurrencyService, RequestConcurrencySettings,
)


class ConcurrencySettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'concurrency.json'
        self.settings = RequestConcurrencySettings(str(self.path))
        self.settings.save({'limit': 3, 'queue_size': 10})

    def test_finite_settings_persist_and_reload(self):
        self.assertEqual(self.settings.load(), {'limit': 3, 'queue_size': 10})
        self.assertEqual(RequestConcurrencyService(self.settings).status(),
                         {'limit': 3, 'queue_size': 10, 'active': 0, 'queued': 0})

    def test_invalid_values_preserve_file(self):
        before = self.path.read_bytes()
        for field in ('limit', 'queue_size'):
            for value in (-1, True, False, 1.5, '3', None):
                with self.subTest(field=field, value=value):
                    payload = {'limit': 3, 'queue_size': 10, field: value}
                    with self.assertRaises(ValueError):
                        self.settings.save(payload)
                    self.assertEqual(self.path.read_bytes(), before)
        for payload in ([], {}, {'limit': 3}, {'limit': 3, 'queue_size': 10, 'extra': 1}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.settings.save(payload)

    def test_failed_atomic_save_preserves_settings_and_cleans_temporary(self):
        before = self.path.read_bytes()
        with mock.patch('request_concurrency.os.replace', side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError):
                self.settings.save({'limit': 2, 'queue_size': 4})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [self.path])

    def test_corrupt_configuration_does_not_fall_back_to_unlimited(self):
        self.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.settings.load()


class ConcurrencyLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.limiter = RequestConcurrencyLimiter(limit=3, queue_size=10)
        self.tickets = []

    async def asyncTearDown(self):
        for ticket in self.tickets:
            self.limiter.release(ticket)
        self.assertEqual(self.limiter.status()['active'], 0)
        self.assertEqual(self.limiter.status()['queued'], 0)

    def acquire(self):
        ticket = self.limiter.acquire()
        self.tickets.append(ticket)
        return ticket

    async def test_three_active_ten_waiting_and_next_rejected(self):
        active = [self.acquire() for _ in range(3)]
        waiting = [self.acquire() for _ in range(10)]
        self.assertTrue(all(ticket.done() for ticket in active))
        self.assertFalse(any(ticket.done() for ticket in waiting))
        with self.assertRaises(ConcurrencyQueueFull):
            self.acquire()
        self.assertEqual(self.limiter.status(), {'limit': 3, 'queue_size': 10, 'active': 3, 'queued': 10})

    async def test_fifo_release_and_cancelled_waiter(self):
        active = [self.acquire() for _ in range(3)]
        waiting = [self.acquire() for _ in range(3)]
        self.limiter.release(waiting[1])
        self.limiter.release(active[0])
        self.assertTrue(waiting[0].done())
        self.assertTrue(waiting[1].cancelled())
        self.assertFalse(waiting[2].done())
        self.limiter.release(active[1])
        self.assertTrue(waiting[2].done())
        self.assertEqual(self.limiter.status()['active'], 3)

    async def test_finite_limit_changes_preserve_running_requests(self):
        active = [self.acquire() for _ in range(3)]
        waiting = [self.acquire() for _ in range(2)]
        self.limiter.configure(limit=4, queue_size=10)
        self.assertTrue(waiting[0].done())
        self.assertFalse(waiting[1].done())
        self.limiter.configure(limit=2, queue_size=10)
        self.assertEqual(self.limiter.status()['active'], 4)
        for ticket in active[:2]:
            self.limiter.release(ticket)
        self.assertFalse(waiting[1].done())
        self.limiter.release(active[2])
        self.assertTrue(waiting[1].done())
        self.assertEqual(self.limiter.status()['active'], 2)

    async def test_smaller_queue_keeps_existing_waiters_and_rejects_new_requests(self):
        active = [self.acquire() for _ in range(3)]
        waiting = [self.acquire() for _ in range(3)]
        self.limiter.configure(limit=3, queue_size=1)
        self.assertEqual(self.limiter.status()['queued'], 3)
        with self.assertRaises(ConcurrencyQueueFull):
            self.acquire()
        self.limiter.release(active[0])
        self.assertTrue(waiting[0].done())
        self.assertEqual(self.limiter.status()['queued'], 2)

    async def test_zero_queue_rejects_excess_without_waiting(self):
        self.limiter.configure(limit=3, queue_size=0)
        for _ in range(3):
            self.acquire()
        with self.assertRaises(ConcurrencyQueueFull):
            self.acquire()
        self.assertEqual(self.limiter.status()['queued'], 0)

    async def test_releasing_a_granted_ticket_twice_does_not_free_another_slot(self):
        active = [self.acquire() for _ in range(3)]
        waiting = [self.acquire() for _ in range(2)]
        self.limiter.release(active[0])
        self.limiter.release(active[0])
        self.assertTrue(waiting[0].done())
        self.assertFalse(waiting[1].done())
        self.assertEqual(self.limiter.status()['active'], 3)


class ConcurrencyServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = RequestConcurrencySettings(str(Path(self.directory.name) / 'concurrency.json'))
        self.settings.save({'limit': 3, 'queue_size': 10})
        self.service = RequestConcurrencyService(self.settings)

    async def test_save_updates_persistence_and_runtime(self):
        result = await self.service.update({'limit': 2, 'queue_size': 4})
        self.assertEqual(result, {'limit': 2, 'queue_size': 4, 'active': 0, 'queued': 0})
        self.assertEqual(self.settings.load(), {'limit': 2, 'queue_size': 4})

    async def test_failed_save_keeps_runtime_limit(self):
        with mock.patch.object(self.settings, 'save', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                await self.service.update({'limit': 2, 'queue_size': 4})
        self.assertEqual(self.service.status()['limit'], 3)
        self.assertEqual(self.settings.load(), {'limit': 3, 'queue_size': 10})

    async def test_cancelled_save_finishes_atomic_configuration_change(self):
        entered = threading.Event()
        proceed = threading.Event()
        original_save = self.settings.save

        def blocked_save(value):
            entered.set()
            if not proceed.wait(3):
                raise TimeoutError('fixture save was not released')
            return original_save(value)

        with mock.patch.object(self.settings, 'save', side_effect=blocked_save):
            operation = asyncio.create_task(self.service.update({'limit': 2, 'queue_size': 4}))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                operation.cancel()
                proceed.set()
                with self.assertRaises(asyncio.CancelledError):
                    await operation
            finally:
                proceed.set()
                await asyncio.gather(operation, return_exceptions=True)
        self.assertEqual(self.settings.load(), {'limit': 2, 'queue_size': 4})
        self.assertEqual(self.service.status()['limit'], 2)


class ASGIExchange:
    def __init__(self, name, path='/v1/responses', chunks=None):
        self.scope = {
            'type': 'http', 'method': 'POST', 'path': path, 'fixture_name': name,
            'asgi': {'version': '3.0', 'spec_version': '2.4'}, 'headers': [],
        }
        self.incoming = asyncio.Queue()
        self.messages = []
        parts = [b'{"model":"fixture"}'] if chunks is None else chunks
        for index, chunk in enumerate(parts):
            self.incoming.put_nowait({'type': 'http.request', 'body': chunk, 'more_body': index < len(parts) - 1})

    async def receive(self):
        return await self.incoming.get()

    async def send(self, message):
        self.messages.append(message)


class ConcurrencyMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.limiter = RequestConcurrencyLimiter(limit=3, queue_size=10)
        self.middleware = RequestConcurrencyMiddleware(self.application, self.limiter)
        self.operations = []
        self.started = []
        self.gates = {}
        self.bodies = {}

    async def asyncTearDown(self):
        for operation in self.operations:
            operation.cancel()
        await asyncio.gather(*self.operations, return_exceptions=True)
        self.assertEqual(self.limiter.status()['active'], 0)
        self.assertEqual(self.limiter.status()['queued'], 0)

    async def application(self, scope, receive, send):
        name = scope['fixture_name']
        if scope['path'] == '/api/config/concurrency':
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'{}'})
            return
        body = bytearray()
        while True:
            message = await receive()
            body.extend(message.get('body', b''))
            if not message.get('more_body', False):
                break
        self.bodies[name] = bytes(body)
        self.started.append(name)
        if name == 'failure':
            raise RuntimeError('fixture upstream failure')
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'first', 'more_body': True})
        await self.gates[name].wait()
        await send({'type': 'http.response.body', 'body': b'last', 'more_body': False})

    def launch(self, name, path='/v1/responses', chunks=None):
        exchange = ASGIExchange(name, path, chunks)
        self.gates[name] = asyncio.Event()
        operation = asyncio.create_task(self.middleware(exchange.scope, exchange.receive, exchange.send))
        self.operations.append(operation)
        return operation, exchange

    async def wait_until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.001)
        await asyncio.wait_for(wait(), timeout=2)

    async def occupy_three(self):
        for index, path in enumerate(('/v1/responses', '/v1/chat/completions', '/v1/messages')):
            self.launch('active-' + str(index), path)
        await self.wait_until(lambda: len(self.started) == 3)

    async def test_stream_headers_do_not_release_capacity_and_queue_is_fifo(self):
        await self.occupy_three()
        self.launch('fourth', '/v1/responses/compact')
        await self.wait_until(lambda: self.limiter.status()['queued'] == 1)
        self.launch('fifth', '/api/credentials/fixture/test')
        await self.wait_until(lambda: self.limiter.status()['queued'] == 2)
        self.assertEqual(self.started, ['active-0', 'active-1', 'active-2'])
        self.gates['active-0'].set()
        await self.wait_until(lambda: 'fourth' in self.started)
        self.assertNotIn('fifth', self.started)
        self.gates['active-1'].set()
        await self.wait_until(lambda: 'fifth' in self.started)
        self.assertEqual(self.started[-2:], ['fourth', 'fifth'])

    async def test_full_queue_returns_429_without_dispatching_or_reading_body(self):
        await self.occupy_three()
        for index in range(10):
            self.launch('queued-' + str(index))
        await self.wait_until(lambda: self.limiter.status()['queued'] == 10)
        operation, exchange = self.launch('overflow')
        await asyncio.wait_for(operation, timeout=1)
        self.assertEqual(exchange.messages[0]['status'], 429)
        payload = json.loads(exchange.messages[-1]['body'])
        self.assertEqual(payload['error']['code'], 'concurrency_queue_full')
        self.assertEqual(len(self.started), 3)
        self.assertEqual(exchange.incoming.qsize(), 1)

    async def test_queue_disconnect_removes_request_without_dispatch(self):
        await self.occupy_three()
        operation, exchange = self.launch('disconnected')
        await self.wait_until(lambda: self.limiter.status()['queued'] == 1)
        exchange.incoming.put_nowait({'type': 'http.disconnect'})
        await asyncio.wait_for(operation, timeout=1)
        self.assertNotIn('disconnected', self.started)
        self.assertEqual(self.limiter.status()['queued'], 0)

    async def test_cancelling_waiter_and_active_stream_releases_their_slots(self):
        await self.occupy_three()
        waiting, _ = self.launch('cancelled')
        await self.wait_until(lambda: self.limiter.status()['queued'] == 1)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        self.assertEqual(self.limiter.status()['queued'], 0)
        self.operations[0].cancel()
        await asyncio.gather(self.operations[0], return_exceptions=True)
        self.assertEqual(self.limiter.status()['active'], 2)
        self.launch('replacement')
        await self.wait_until(lambda: 'replacement' in self.started)
        self.assertEqual(self.limiter.status()['active'], 3)

    async def test_queued_request_body_is_replayed_without_changes(self):
        await self.occupy_three()
        chunks = [b'{"model":', b'"fixture",', b'"input":"hello"}']
        self.launch('body', chunks=chunks)
        await self.wait_until(lambda: self.limiter.status()['queued'] == 1)
        self.gates['active-0'].set()
        await self.wait_until(lambda: 'body' in self.started)
        self.assertEqual(self.bodies['body'], b''.join(chunks))

    async def test_failure_releases_capacity(self):
        operation, _ = self.launch('failure')
        with self.assertRaisesRegex(RuntimeError, 'fixture upstream failure'):
            await operation
        self.assertEqual(self.limiter.status()['active'], 0)

    async def test_management_is_available_when_slots_and_queue_are_full(self):
        await self.occupy_three()
        for index in range(10):
            self.launch('queued-' + str(index))
        await self.wait_until(lambda: self.limiter.status()['queued'] == 10)
        operation, exchange = self.launch('settings', '/api/config/concurrency')
        await asyncio.wait_for(operation, timeout=1)
        self.assertEqual(exchange.messages[0]['status'], 200)
        self.assertEqual(self.limiter.status()['queued'], 10)


if __name__ == '__main__':
    unittest.main()
