import base64
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse
import excel_session_capture
import excel_upstream
import openai_oauth


def token(account='account-test', expires=None, **extra):
    data = {'exp': expires or time.time() + 3600, 'https://api.openai.com/auth': {'chatgpt_account_id': account}}
    data.update(extra)
    raw = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
    return 'eyJhbGciOiJIUzI1NiJ9.' + raw + '.signature'


class EmailMetadataTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')).start()
        mock.patch.object(openai_oauth.httpx, 'Client', side_effect=AssertionError('HTTP forbidden')).start()

    def test_standard_and_namespaced_token_email(self):
        for key in ('access_token', 'id_token'):
            for claims in ({'email': 'person@example.test'},
                           {'https://api.openai.com/profile': {'email': 'person@example.test'}},
                           {'https://api.openai.com/profile/email': 'person@example.test'}):
                with self.subTest(key=key, claims=claims):
                    payload = {'access_token': token(), key: token(**claims)}
                    result = openai_oauth.validate_oauth_tokens(payload)
                    self.assertEqual(result['email'], 'person@example.test')
                    self.assertNotIn('id_token', result)

    def test_precedence_refresh_preservation_and_account_binding(self):
        original = openai_oauth.validate_oauth_tokens({
            'access_token': token(email='access@example.test'),
            'id_token': token(email='identity@example.test'), 'refresh_token': 'private-refresh'})
        self.assertEqual(original['email'], 'identity@example.test')
        refreshed = openai_oauth.validate_oauth_tokens({'access_token': token()}, original)
        self.assertEqual(refreshed['email'], original['email'])
        for payload in ({'access_token': token('different-account')},
                        {'access_token': token(), 'id_token': token('different-account')},
                        {'access_token': token('different-account'), 'id_token': token()}):
            with self.assertRaisesRegex(RuntimeError, 'changed account identity'):
                openai_oauth.validate_oauth_tokens(payload, original)
        self.assertEqual(original['account_id'], 'account-test')

    def test_invalid_email_metadata_does_not_invalidate_credentials(self):
        invalid = [None, True, 12, [], {}, ['person@example.test'], 'missing-at',
                   'x' * 65 + '@example.test', 'a@' + 'b' * 250 + '.test',
                   'person@example.test' + chr(13) + chr(10) + 'Authorization: secret',
                   'person@exam' + chr(127) + 'ple.test', 'person@exam' + chr(0) + 'ple.test',
                   ' person@example.test', '<person@example.test>', 'a..b@example.test']
        for value in invalid:
            with self.subTest(value=value):
                result = openai_oauth.validate_oauth_tokens({'access_token': token(email=value)})
                self.assertEqual(result['email'], '')
                self.assertEqual(result['account_id'], 'account-test')
        self.assertEqual(openai_oauth.validate_oauth_tokens({
            'access_token': token(), 'email': 'untrusted@example.test'})['email'], '')

    def test_invalid_jwt_headers_payloads_and_types_are_ignored(self):
        good = token(email='person@example.test')
        head, body, sig = good.split('.')
        def encode(value):
            return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
        for malformed in (None, 123, {}, good + '.extra', good + chr(10),
                          'invalid.' + body + '.' + sig,
                          encode([]) + '.' + body + '.' + sig,
                          encode({'alg': 'none'}) + '.' + body + '.' + sig,
                          head + '.' + encode([]) + '.' + sig,
                          head + '.invalid.' + sig, head + '.' + body + '!.signature',
                          'a' * 32701):
            with self.subTest(token=malformed):
                self.assertEqual(openai_oauth._token_email({'id_token': malformed}), '')

    def test_duplicate_claims_and_invalid_profile_are_ignored(self):
        header = 'eyJhbGciOiJIUzI1NiJ9'
        duplicate = base64.urlsafe_b64encode(
            b'{"email":"first@example.test","email":"second@example.test"}').decode().rstrip('=')
        self.assertEqual(openai_oauth._token_email({'id_token': header + '.' + duplicate + '.signature'}), '')
        for profile in ('person@example.test', ['person@example.test'], True):
            self.assertEqual(openai_oauth.validate_oauth_tokens({'access_token': token(**{
                'https://api.openai.com/profile': profile})})['email'], '')

    def test_nonstandard_json_and_non_utf8_payloads_are_ignored(self):
        header = 'eyJhbGciOiJIUzI1NiJ9'
        for raw in (b'{"email":"person@example.test","exp":NaN}',
                    '{"email":"person@example.test"}'.encode('utf-16')):
            body = base64.urlsafe_b64encode(raw).decode().rstrip('=')
            self.assertEqual(openai_oauth._token_email({'id_token': header + '.' + body + '.signature'}), '')

    def test_encrypted_oauth_email_roundtrip_and_old_format_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'synthetic-oauth.dpapi')
            sealed = {}
            def protect(raw):
                encrypted = b'MOCK-DPAPI-' + hashlib.sha256(raw).digest()
                sealed[encrypted] = raw
                return encrypted
            with mock.patch.object(openai_oauth.sys, 'platform', 'win32'), mock.patch.object(
                    excel_upstream, '_protect_windows_data', side_effect=protect), mock.patch.object(
                    excel_upstream, '_unprotect_windows_data', side_effect=lambda raw: sealed[raw]):
                service = openai_oauth.OpenAIOAuth(excel_upstream.ExcelSessionStore(), path)
                service._accept_tokens({'access_token': token(),
                    'id_token': token(email='identity@example.test'), 'refresh_token': 'private-refresh'})
                with open(path, 'rb') as handle:
                    self.assertNotIn(b'identity@example.test', handle.read())
                restored = openai_oauth.OpenAIOAuth(excel_upstream.ExcelSessionStore(), path)
                restored.load()
                self.assertEqual(restored._tokens['email'], 'identity@example.test')
                restored._accept_tokens({'access_token': token()}, previous_refresh='private-refresh')
                self.assertEqual(restored._tokens['email'], 'identity@example.test')
                old = {'version': 1, 'tokens': {'access_token': token(email='old@example.test'),
                    'account_id': 'account-test', 'expires_at': time.time() + 3600}}
                with open(path, 'wb') as handle:
                    handle.write(protect(json.dumps(old).encode()))
                restored.load()
                self.assertEqual(restored._tokens['email'], 'old@example.test')

    def test_failed_refresh_does_not_replace_known_email_or_session(self):
        service = openai_oauth.OpenAIOAuth(excel_upstream.ExcelSessionStore())
        service._accept_tokens({'access_token': token(email='original@example.test'), 'refresh_token': 'private-refresh'})
        original = dict(service._tokens)
        headers = dict(service.store._headers)
        with self.assertRaises(RuntimeError):
            service._accept_tokens({'access_token': token(),
                'id_token': token('other-account', email='other@example.test')}, previous_refresh='private-refresh')
        self.assertEqual(service._tokens, original)
        self.assertEqual(service.store._headers, headers)

    def test_email_never_substitutes_for_account_identity(self):
        with self.assertRaises(RuntimeError):
            openai_oauth.validate_oauth_tokens({'access_token': token(None, email='person@example.test')})


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.store = excel_upstream.ExcelSessionStore()
        self.service = openai_oauth.OpenAIOAuth(self.store)
        self.addCleanup(self.service.cancel)

    def pending(self):
        self.service._pending = {'state': 'state-test', 'verifier': 'verifier-test', 'deadline': time.time() + 60}
        self.service._status = 'waiting'

    def test_probe_status_distinguishes_login_from_verified_access(self):
        self.assertEqual(self.service.status()['bps_probe_status'], 'untested')
        self.assertFalse(self.service.status()['bps_verified'])
        self.service.record_probe(False, 'denied')
        self.assertEqual(self.service.status()['bps_probe_status'], 'failed')
        self.assertFalse(self.service.status()['bps_verified'])
        self.service.record_probe(True, 'OK')
        self.assertEqual(self.service.status()['bps_probe_status'], 'passed')
        self.assertTrue(self.service.status()['bps_verified'])
        self.service.clear()
        self.assertEqual(self.service.status()['bps_probe_status'], 'untested')
        self.assertFalse(self.service.status()['bps_verified'])

    def test_pkce_and_public_status(self):
        with mock.patch.object(openai_oauth, 'ThreadingHTTPServer'):
            result = self.service.start()
        params = parse_qs(urlparse(result['authorization_url']).query)
        verifier = self.service._pending['verifier']
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        self.assertEqual(params['code_challenge'], [challenge])
        self.assertEqual(params['code_challenge_method'], ['S256'])
        self.assertEqual(params['redirect_uri'], [openai_oauth.REDIRECT_URI])
        self.assertNotIn(verifier, json.dumps(result))
        self.assertNotIn(params['state'][0], json.dumps(self.service.status()))

    def test_busy_callback_port(self):
        with mock.patch.object(openai_oauth, 'ThreadingHTTPServer', side_effect=OSError()):
            with self.assertRaisesRegex(RuntimeError, '1455'):
                self.service.start()

    def test_wrong_or_expired_state_does_not_exchange(self):
        self.pending()
        with mock.patch.object(self.service, '_token_request') as exchange:
            self.assertFalse(self.service.complete({'state': ['wrong'], 'code': ['code']})[0])
            self.assertEqual(self.service.status()['status'], 'waiting')
            self.service._pending['deadline'] = 1
            self.assertFalse(self.service.complete({'state': ['state-test'], 'code': ['code']})[0])
            exchange.assert_not_called()

    def test_success_and_replay_but_bps_unverified(self):
        self.pending()
        access = token()
        with mock.patch.object(self.service, '_token_request', return_value={'access_token': access, 'refresh_token': 'refresh-test'}) as exchange:
            self.assertTrue(self.service.complete({'state': ['state-test'], 'code': ['code-test']})[0])
            self.assertFalse(self.service.complete({'state': ['state-test'], 'code': ['code-test']})[0])
            self.assertEqual(exchange.call_count, 1)
            self.assertEqual(exchange.call_args.args[0]['code_verifier'], 'verifier-test')
        self.assertEqual(self.store.status()['source'], 'oauth')
        self.assertFalse(self.service.status()['bps_verified'])
        for value in (access, 'refresh-test', 'code-test'):
            self.assertNotIn(value, json.dumps(self.service.status()))

    def test_cancel_during_exchange_preserves_session(self):
        self.pending()
        def exchange(_data):
            self.service.cancel()
            return {'access_token': token(), 'refresh_token': 'test-refresh'}
        with mock.patch.object(self.service, '_token_request', side_effect=exchange):
            self.assertFalse(self.service.complete({'state': ['state-test'], 'code': ['test-code']})[0])
        self.assertFalse(self.store.status()['configured'])

    def test_provider_denial_not_reflected(self):
        self.pending()
        with mock.patch.object(self.service, '_token_request') as exchange:
            ok, message = self.service.complete({'state': ['state-test'], 'error': ['untrusted-value']})
            self.assertFalse(ok)
            self.assertNotIn('untrusted-value', message)
            exchange.assert_not_called()

    def test_missing_account_preserves_old_session(self):
        self.service._accept_tokens({'access_token': token('old'), 'refresh_token': 'old-refresh'})
        self.pending()
        with mock.patch.object(self.service, '_token_request', return_value={'access_token': 'opaque'}):
            self.assertFalse(self.service.complete({'state': ['state-test'], 'code': ['code']})[0])
        self.assertEqual(self.store.request_headers(stream=False)['chatgpt-account-id'], 'old')

    def test_new_account_never_reuses_previous_refresh(self):
        self.service._accept_tokens({'access_token': token('old'), 'refresh_token': 'old-refresh'})
        self.service._accept_tokens({'access_token': token('new')})
        self.assertFalse(self.service.status()['has_refresh_token'])

    def test_refresh_rotation_and_reuse(self):
        for rotate in (True, False):
            self.service._accept_tokens({'access_token': token(expires=time.time() + 10), 'refresh_token': 'old-refresh'})
            payload = {'access_token': token()}
            if rotate:
                payload['refresh_token'] = 'new-refresh'
            with mock.patch.object(self.service, '_token_request', return_value=payload) as exchange:
                self.service.ensure_session()
                self.service.ensure_session()
                self.assertEqual(exchange.call_count, 1)
                self.assertEqual(exchange.call_args.args[0]['refresh_token'], 'old-refresh')
            self.assertEqual(self.service._tokens['refresh_token'], 'new-refresh' if rotate else 'old-refresh')

    def test_expired_session_without_refresh_requires_login(self):
        self.service._accept_tokens({'access_token': token(expires=time.time() + 10)})
        with self.assertRaisesRegex(RuntimeError, 'Sign in again'):
            self.service.ensure_session()

    def test_excel_cannot_override_oauth(self):
        self.service._accept_tokens({'access_token': token()})
        for platform, method, loader in (
            ('win32', excel_session_capture.refresh_windows_excel_session, 'load_windows_excel_session'),
            ('darwin', excel_session_capture.refresh_macos_excel_session, 'load_macos_excel_session'),
        ):
            with mock.patch.object(excel_session_capture.sys, 'platform', platform), mock.patch.object(excel_session_capture, loader) as read:
                method(self.store, force=True)
                read.assert_not_called()

    def test_timeout_preserves_session_and_clear_removes_it(self):
        self.service._accept_tokens({'access_token': token()})
        self.pending()
        self.service._expire('state-test')
        self.assertEqual(self.service.status()['status'], 'error')
        self.assertTrue(self.store.status()['configured'])
        self.service.clear()
        self.assertFalse(self.store.status()['configured'])

    @unittest.skipUnless(sys.platform == 'win32', 'Windows DPAPI')
    def test_encrypted_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'oauth.dpapi')
            service = openai_oauth.OpenAIOAuth(self.store, path)
            access = token()
            service._accept_tokens({'access_token': access, 'refresh_token': 'test-refresh'})
            with open(path, 'rb') as handle:
                raw = handle.read()
            self.assertNotIn(access.encode(), raw)
            self.assertNotIn(b'test-refresh', raw)
            restored = openai_oauth.OpenAIOAuth(excel_upstream.ExcelSessionStore(), path)
            restored.load()
            self.assertTrue(restored.status()['has_refresh_token'])
            self.assertEqual(restored.store.status()['source'], 'oauth')
            restored.clear()
            self.assertFalse(os.path.exists(path))


class OAuthRouteTests(unittest.TestCase):
    def test_local_same_origin_json_only(self):
        from fastapi import HTTPException
        from starlette.requests import Request
        import constants
        import proxy
        host = f'127.0.0.1:{constants.PROXY_PORT}'
        def request(origin=None, hostname=host, content_type='application/json', client='127.0.0.1'):
            headers = [(b'host', hostname.encode()), (b'content-type', content_type.encode())]
            if origin:
                headers.append((b'origin', origin.encode()))
            return Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': headers, 'client': (client, 1234)})
        proxy._require_local_oauth_action(request())
        proxy._require_local_oauth_action(request(origin='http://' + host))
        for req in (request(origin='https://untrusted.example'), request(hostname='attacker.example'), request(content_type='text/plain'), request(client='10.0.0.1')):
            with self.assertRaises(HTTPException):
                proxy._require_local_oauth_action(req)

    def test_default_port_and_old_instance_configuration_are_separate(self):
        import constants
        import app_paths
        import proxy_client_config
        self.assertEqual(constants.PROXY_PORT, int(os.environ.get('GHCP_PORT', '8001')))
        self.assertTrue(constants.PROXY_BASE_URL.endswith(':' + str(constants.PROXY_PORT)))
        if constants.PROXY_PORT != 8000:
            self.assertFalse(proxy_client_config._is_codex_proxy_base_url('http://localhost:8000/v1'))
        if not os.environ.get('GHCP_APP_DIR_NAME'):
            self.assertEqual(app_paths.APP_DIR_NAME, 'ghcp_proxy_fzy')


class BPSProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_uses_production_response_handler(self):
        from fastapi.responses import JSONResponse
        import proxy
        for status, payload, expected in (
            (200, {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'OK'}]}]}, True),
            (403, {'error': {'message': 'denied'}}, False),
            (200, {'error': {'message': 'failed'}}, False),
        ):
            with (
                mock.patch.object(proxy, '_require_local_oauth_action'),
                mock.patch.object(proxy, '_handle_excel_responses', mock.AsyncMock(return_value=JSONResponse(payload, status_code=status))) as handle,
                mock.patch.object(proxy.openai_oauth.login_service, 'record_probe') as record,
            ):
                result = await proxy.excel_oauth_test_api(mock.Mock())
                self.assertEqual(json.loads(result.body)['ok'], expected)
                self.assertEqual(record.call_args.args[0], expected)
                self.assertEqual(handle.call_args.args[1]['model'], 'gpt-6-sol')
                self.assertFalse(handle.call_args.args[1]['stream'])


if __name__ == '__main__':
    unittest.main()
