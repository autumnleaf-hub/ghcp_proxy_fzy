"""Loopback-only identity and graceful shutdown for the Windows tray manager.

This module never discovers or kills processes. The caller must validate the
listener's PID/root/instance identity; only the embedded server can be stopped.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse


class DesktopServiceController:
    def __init__(self, project_root, port, shutdown_getter):
        self.project_root = str(Path(project_root).resolve())
        self.port = int(port)
        self.pid = os.getpid()
        self.instance_id = uuid4().hex
        self.started_at = time.time()
        self.shutdown_getter = shutdown_getter
        self.stopping = False
        self.router = APIRouter()
        self.router.add_api_route('/api/desktop/identity', self.identity_api, methods=['GET'])
        self.router.add_api_route('/api/desktop/stop', self.stop_api, methods=['POST'])

    def _guard(self, request, *, write=False):
        if not request.client or request.client.host not in {'127.0.0.1', '::1'}:
            raise HTTPException(403, 'Desktop management is loopback-only.')
        host = request.headers.get('host', '').lower()
        if host not in {f'127.0.0.1:{self.port}', f'localhost:{self.port}'}:
            raise HTTPException(403, 'Unexpected desktop management host.')
        origin = request.headers.get('origin')
        if (origin and origin != f'http://{host}') or request.headers.get('sec-fetch-site') == 'cross-site':
            raise HTTPException(403, 'Cross-origin desktop management is forbidden.')
        if write and request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            raise HTTPException(415, 'Use application/json.')

    def identity(self):
        return {'service': 'ghcp_proxy', 'project_root': self.project_root,
                'pid': self.pid, 'port': self.port, 'instance_id': self.instance_id,
                'started_at': self.started_at, 'stopping': self.stopping,
                'graceful_stop_supported': callable(self.shutdown_getter())}

    async def identity_api(self, request: Request):
        self._guard(request)
        return JSONResponse(self.identity(), headers={'Cache-Control': 'no-store'})

    async def stop_api(self, request: Request):
        self._guard(request, write=True)
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(400, 'Invalid JSON.') from exc
        if not isinstance(body, dict):
            raise HTTPException(400, 'A JSON object is required.')
        requested = body.get('instance_id')
        if (type(body.get('pid')) is not int or body['pid'] != self.pid
                or not isinstance(requested, str) or not requested.isascii()
                or not hmac.compare_digest(requested, self.instance_id)):
            raise HTTPException(409, 'Service instance changed; refresh before stopping.')
        callback = self.shutdown_getter()
        if not callable(callback):
            raise HTTPException(409, 'This server was launched externally; use verified process control.')
        if not self.stopping:
            self.stopping = True
            asyncio.get_running_loop().call_later(0.25, self._trigger_stop, callback)
        return JSONResponse({'stopping': True, 'pid': self.pid, 'instance_id': self.instance_id},
                            status_code=202, headers={'Cache-Control': 'no-store'})

    def _trigger_stop(self, callback):
        try:
            callback()
        except Exception:
            self.stopping = False
            logging.getLogger(__name__).exception('Graceful desktop shutdown failed')
