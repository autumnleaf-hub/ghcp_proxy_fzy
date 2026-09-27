"""Exercise real management route bodies against isolated finite-limit settings."""
import gzip
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
import httpx

from request_concurrency import (
    RequestConcurrencyMiddleware, RequestConcurrencyService, RequestConcurrencySettings,
)
from tests.test_auto_update_toggle import extract_functions

ROOT = Path(__file__).resolve().parents[1]
PORT = 49179
ROUTE = '/api/config/concurrency'


class ConcurrencyAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = RequestConcurrencySettings(str(Path(self.directory.name) / 'concurrency.json'))
        self.settings.save({'limit': 3, 'queue_size': 10})
        self.service = RequestConcurrencyService(self.settings)
        parser_namespace = {
            'json': json, 'gzip': gzip, 'zlib': zlib, 'HTTPException': HTTPException,
            'utc_now_iso': lambda: '2033-05-18T03:33:20Z', 'brotli': None,
            'zstd_decompress': mock.Mock(side_effect=AssertionError('not used')),
        }
        extract_functions(ROOT / 'util.py', {'parse_json_request'}, parser_namespace)
        self.namespace = {
            'Request': Request, 'HTTPException': HTTPException, 'JSONResponse': JSONResponse,
            'PROXY_PORT': PORT, 'request_concurrency_service': self.service,
            'usage_tracker': SimpleNamespace(record_request_error=mock.Mock()),
            'util': SimpleNamespace(parse_json_request=parser_namespace['parse_json_request']),
        }
        extract_functions(
            ROOT / 'proxy.py',
            {'request_concurrency_status_api', 'request_concurrency_config_api',
             '_require_local_bps_management', 'parse_json_request'},
            self.namespace,
        )
        self.app = FastAPI()
        self.app.add_middleware(RequestConcurrencyMiddleware, limiter=self.service.limiter)
        self.app.add_api_route(ROUTE, self.namespace['request_concurrency_status_api'], methods=['GET'])
        self.app.add_api_route(ROUTE, self.namespace['request_concurrency_config_api'], methods=['POST'])

    async def request(self, method='GET', peer=('127.0.0.1', 12345), **kwargs):
        transport = httpx.ASGITransport(app=self.app, client=peer, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=f'http://127.0.0.1:{PORT}') as client:
            return await client.request(method, ROUTE, **kwargs)

    async def test_load_and_save_finite_settings(self):
        result = await self.request()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {'limit': 3, 'queue_size': 10, 'active': 0, 'queued': 0})
        self.assertEqual(result.headers['cache-control'], 'no-store')
        result = await self.request('POST', json={'limit': 2, 'queue_size': 4})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.settings.load(), {'limit': 2, 'queue_size': 4})
        self.assertEqual(self.service.status()['limit'], 2)
        self.assertEqual(result.json()['queue_size'], 4)

    async def test_invalid_payloads_do_not_change_configuration(self):
        payloads = ([], {}, {'limit': -1, 'queue_size': 10}, {'limit': True, 'queue_size': 10},
                    {'limit': 3, 'queue_size': -1}, {'limit': 3, 'queue_size': '10'},
                    {'limit': 1.5, 'queue_size': 10}, {'limit': 3, 'queue_size': 10, 'extra': 1})
        for payload in payloads:
            with self.subTest(payload=payload):
                result = await self.request('POST', json=payload)
                self.assertEqual(result.status_code, 400, result.text)
                self.assertEqual(self.settings.load(), {'limit': 3, 'queue_size': 10})
        result = await self.request('POST', content='{broken', headers={'Content-Type': 'application/json'})
        self.assertEqual(result.status_code, 400)

    async def test_writes_require_json_content_type(self):
        for headers in ({}, {'Content-Type': 'text/plain'}):
            with self.subTest(headers=headers):
                result = await self.request('POST', content='{}', headers=headers)
                self.assertEqual(result.status_code, 415, result.text)
        self.assertEqual(self.settings.load(), {'limit': 3, 'queue_size': 10})

    async def test_management_rejects_remote_host_origin_and_cross_site(self):
        variants = [
            {'peer': ('192.0.2.1', 12345)},
            {'headers': {'Host': 'example.invalid'}},
            {'headers': {'Origin': 'https://example.invalid'}},
            {'headers': {'Sec-Fetch-Site': 'cross-site'}},
        ]
        for method in ('GET', 'POST'):
            for kwargs in variants:
                with self.subTest(method=method, kwargs=kwargs):
                    options = dict(kwargs)
                    if method == 'POST':
                        options['json'] = {'limit': 2, 'queue_size': 4}
                    result = await self.request(method, **options)
                    self.assertEqual(result.status_code, 403, result.text)
        self.assertEqual(self.settings.load(), {'limit': 3, 'queue_size': 10})

    async def test_localhost_ipv6_peer_and_json_charset_are_accepted(self):
        host = f'localhost:{PORT}'
        result = await self.request('POST', peer=('::1', 12345),
            headers={'Host': host, 'Origin': 'http://' + host, 'Content-Type': 'application/json; charset=utf-8'},
            content=json.dumps({'limit': 3, 'queue_size': 5}))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['queue_size'], 5)

    async def test_storage_failure_keeps_active_configuration(self):
        with mock.patch.object(self.settings, 'save', side_effect=OSError('fixture disk failure')):
            result = await self.request('POST', json={'limit': 2, 'queue_size': 4})
        self.assertEqual(result.status_code, 500)
        self.assertEqual(self.service.status()['limit'], 3)
        self.assertEqual(self.settings.load(), {'limit': 3, 'queue_size': 10})

    async def test_settings_are_available_when_active_slots_and_queue_are_full(self):
        tickets = [self.service.limiter.acquire() for _ in range(13)]
        try:
            result = await self.request()
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['active'], 3)
            self.assertEqual(result.json()['queued'], 10)
            result = await self.request('POST', json={'limit': 2, 'queue_size': 5})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json(), {'limit': 2, 'queue_size': 5, 'active': 3, 'queued': 10})
        finally:
            for ticket in tickets:
                self.service.limiter.release(ticket)


if __name__ == '__main__':
    unittest.main()
