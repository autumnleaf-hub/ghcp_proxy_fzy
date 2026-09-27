"""Persistent request concurrency limits with a bounded FIFO waiting queue."""
from __future__ import annotations

import asyncio
from collections import deque
import json
import os
import tempfile
import threading

from fastapi.responses import JSONResponse

from app_paths import user_config_dir

DEFAULT_LIMIT = 0
DEFAULT_QUEUE_SIZE = 10
INFERENCE_PATHS = frozenset({
    '/responses', '/v1/responses', '/responses/compact', '/v1/responses/compact',
    '/chat/completions', '/v1/chat/completions', '/v1/messages',
    '/api/config/excel-oauth/test',
})


class RequestConcurrencySettings:
    def __init__(self, path=None):
        self.path = path or os.path.join(user_config_dir(), 'request-concurrency.json')
        self._lock = threading.RLock()

    @staticmethod
    def validate(payload):
        if not isinstance(payload, dict) or set(payload) != {'limit', 'queue_size'}:
            raise ValueError('并发设置必须包含 limit 和 queue_size。')
        for field, label in (('limit', '并发上限'), ('queue_size', '队列容量')):
            if type(payload[field]) is not int or payload[field] < 0:
                raise ValueError(label + '必须为非负整数。')
        return {'limit': payload['limit'], 'queue_size': payload['queue_size']}

    def load(self):
        with self._lock:
            try:
                with open(self.path, encoding='utf-8') as handle:
                    payload = json.load(handle)
            except FileNotFoundError:
                return {'limit': DEFAULT_LIMIT, 'queue_size': DEFAULT_QUEUE_SIZE}
            except (OSError, ValueError) as exc:
                raise ValueError('无法读取并发设置；请修复配置文件，未自动改为无限制。') from exc
            return self.validate(payload)

    def save(self, payload):
        value = self.validate(payload)
        with self._lock:
            parent = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(parent, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix='.request-concurrency-', suffix='.tmp', dir=parent)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                    json.dump(value, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.remove(temporary)
        return value


class ConcurrencyQueueFull(Exception):
    pass


class RequestConcurrencyLimiter:
    """All admission and release operations run on the application's event loop."""

    def __init__(self, limit=DEFAULT_LIMIT, queue_size=DEFAULT_QUEUE_SIZE):
        value = RequestConcurrencySettings.validate({'limit': limit, 'queue_size': queue_size})
        self.limit = value['limit']
        self.queue_size = value['queue_size']
        self._active = set()
        self._waiting = deque()

    def status(self):
        return {
            'limit': self.limit, 'queue_size': self.queue_size,
            'active': len(self._active), 'queued': sum(not ticket.done() for ticket in self._waiting),
        }

    def configure(self, limit, queue_size):
        value = RequestConcurrencySettings.validate({'limit': limit, 'queue_size': queue_size})
        self.limit = value['limit']
        self.queue_size = value['queue_size']
        self._drain()

    def acquire(self):
        self._drain()
        available = self.limit == 0 or len(self._active) < self.limit
        if not available and self.status()['queued'] >= self.queue_size:
            raise ConcurrencyQueueFull('等待队列已满。')
        ticket = asyncio.get_running_loop().create_future()
        self._waiting.append(ticket)
        self._drain()
        return ticket

    def release(self, ticket):
        self._active.discard(ticket)
        if not ticket.done():
            ticket.cancel()
        try:
            self._waiting.remove(ticket)
        except ValueError:
            pass
        self._drain()

    def _drain(self):
        while self._waiting and (self.limit == 0 or len(self._active) < self.limit):
            ticket = self._waiting.popleft()
            if ticket.done():
                continue
            self._active.add(ticket)
            ticket.set_result(None)


class RequestConcurrencyService:
    def __init__(self, settings=None):
        self.settings = settings if settings is not None else RequestConcurrencySettings()
        self.limiter = RequestConcurrencyLimiter(**self.settings.load())
        self._save_lock = asyncio.Lock()

    def status(self):
        return self.limiter.status()

    async def update(self, payload):
        value = self.settings.validate(payload)
        async with self._save_lock:
            operation = asyncio.create_task(self._save_and_apply(value))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                await operation
                raise

    async def _save_and_apply(self, value):
        saved = await asyncio.to_thread(self.settings.save, value)
        self.limiter.configure(**saved)
        return self.status()


class RequestConcurrencyMiddleware:
    def __init__(self, app, limiter):
        self.app = app
        self.limiter = limiter

    @staticmethod
    def is_inference(scope):
        if scope['type'] != 'http' or scope.get('method') != 'POST':
            return False
        path = scope.get('path', '').rstrip('/')
        return path in INFERENCE_PATHS or (path.startswith('/api/credentials/') and path.endswith('/test'))

    async def __call__(self, scope, receive, send):
        if not self.is_inference(scope):
            await self.app(scope, receive, send)
            return
        try:
            ticket = self.limiter.acquire()
        except ConcurrencyQueueFull:
            response = JSONResponse(
                {'error': {'type': 'rate_limit_error', 'code': 'concurrency_queue_full',
                           'message': '并发等待队列已满，请稍后重试；此请求未转发到上游。'}},
                status_code=429, headers={'Cache-Control': 'no-store'},
            )
            await response(scope, receive, send)
            return

        buffered = deque()
        pending_receive = None
        try:
            while not ticket.done():
                if pending_receive is None:
                    pending_receive = asyncio.create_task(receive())
                completed, _ = await asyncio.wait({ticket, pending_receive}, return_when=asyncio.FIRST_COMPLETED)
                if pending_receive in completed:
                    message = pending_receive.result()
                    pending_receive = None
                    if message['type'] == 'http.disconnect':
                        return
                    buffered.append(message)
            await ticket

            async def replay_receive():
                nonlocal pending_receive
                if buffered:
                    return buffered.popleft()
                if pending_receive is not None:
                    operation = pending_receive
                    pending_receive = None
                    return await operation
                return await receive()

            await self.app(scope, replay_receive, send)
        finally:
            self.limiter.release(ticket)
            if pending_receive is not None:
                pending_receive.cancel()
                await asyncio.gather(pending_receive, return_exceptions=True)
