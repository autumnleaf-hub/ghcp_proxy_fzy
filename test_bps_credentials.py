"""Offline tests: synthetic credentials, mocked network/DPAPI, temporary state only."""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from email.utils import formatdate
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import bps_credentials as bps
import excel_upstream
import openai_oauth


def jwt(account='account-secret-A', expiry=None, **extra):
    claims = {'exp': time.time() + 3600 if expiry is None else expiry, **extra}
    if account is not None:
        claims['https://api.openai.com/auth'] = {'chatgpt_account_id': account}
    raw = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'eyJhbGciOiJIUzI1NiJ9.' + raw + '.signature'


def tokens(account='account-secret-A', expiry=None, refresh='refresh-secret-A'):
    expiry = time.time() + 3600 if expiry is None else expiry
    return {'account_id': account, 'access_token': jwt(account, expiry),
            'refresh_token': refresh, 'expires_at': expiry}


def store_for(account='account-secret-A', expiry=None):
    store = excel_upstream.ExcelSessionStore()
    store.configure({'authorization': 'Bearer ' + jwt(account, expiry), 'chatgpt-account-id': account}, persist=False, allow_expired=True)
    return store


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')).start()
        mock.patch.object(openai_oauth.httpx, 'Client', side_effect=AssertionError('HTTP forbidden')).start()
        self.now = time.time()
        self.pool = bps.CredentialPool(clock=lambda: self.now)
        self.pool.load()

    def add(self, account='account-secret-A', **kwargs):
        return self.pool.upsert_oauth(tokens(account, **kwargs))['id']

    def status(self, cid):
        return next(c for c in self.pool.list_credentials()['credentials'] if c['id'] == cid)

    def assert_pool_error(self, status, callback):
        with self.assertRaises(bps.PoolError) as caught:
            callback()
        self.assertEqual(caught.exception.status_code, status)
        return caught.exception


class PoolTests(OfflineCase):
    def test_initialization_and_status_have_no_credential_io(self):
        with mock.patch('builtins.open', side_effect=AssertionError('No I/O')), mock.patch.object(bps, 'user_state_dir', side_effect=AssertionError('No path lookup')):
            pool = bps.CredentialPool(_default_storage=True)
            self.assertFalse(pool.list_credentials()['loaded'])
            self.assert_pool_error(503, lambda: pool.acquire('unloaded', False))

    def test_add_dedupe_label_limit_and_disabled_preserved(self):
        a, b = self.add(), self.add('account-secret-B')
        self.pool.update(a, {'label': 'Team A', 'enabled': False})
        again = self.pool.upsert_oauth(tokens(refresh='rotated-secret'))
        self.assertEqual(again['id'], a)
        self.assertFalse(again['enabled'])
        self.assertEqual(again['label'], 'Team A')
        self.assertEqual(len(self.pool.list_credentials()['credentials']), 2)
        self.assertEqual(self.pool.acquire('new', False).credential_id, b)
        for value in ('false', 0, 1, None, [], {}):
            self.assert_pool_error(400, lambda value=value: self.pool.update(a, {'enabled': value}))
        for change in ({'label': ''}, {'label': 'x' * 121}, {'access_token': 'bad'}):
            self.assert_pool_error(400, lambda change=change: self.pool.update(a, change))
        tiny = bps.CredentialPool(max_accounts=1)
        tiny.load()
        tiny.upsert_oauth(tokens())
        self.assert_pool_error(409, lambda: tiny.upsert_oauth(tokens('another-secret')))

    def test_default_maximum_is_32(self):
        for i in range(32):
            self.add('private-account-' + str(i))
        self.assert_pool_error(409, lambda: self.add('private-account-33'))

    def test_explicit_session_import_dedupe_and_disabled(self):
        store = store_for()
        first = self.pool.upsert_session(store, label='Captured')['id']
        self.assertEqual(self.pool.upsert_session(store)['id'], first)
        self.pool.update(first, {'enabled': False})
        self.assertFalse(self.pool.upsert_session(store)['enabled'])
        self.pool.remove(first)
        self.assert_pool_error(410, lambda: self.pool.upsert_session(store))
        self.assertEqual(self.pool.list_credentials()['credentials'], [])

    def test_migration_once_tombstones_and_legacy_files_untouched(self):
        legacy = openai_oauth.OpenAIOAuth(store_for())
        legacy._tokens = tokens()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.dpapi'
            path.write_bytes(b'untouched-encrypted-legacy-placeholder')
            legacy.persistence_file = str(path)
            self.pool.migrate_legacy(legacy.store, legacy)
            first = self.pool.list_credentials()['credentials'][0]['id']
            self.pool.remove(first)
            self.pool.migrate_legacy(legacy.store, legacy)
            self.pool.load()
            self.assertEqual(self.pool.list_credentials()['credentials'], [])
            self.assertEqual(path.read_bytes(), b'untouched-encrypted-legacy-placeholder')
        # Deletion before the first migration is also a tombstone.
        another = bps.CredentialPool()
        another.load()
        another.remove(another.upsert_oauth(tokens())['id'])
        another.migrate_legacy(legacy.store, legacy)
        self.assertEqual(another.list_credentials()['credentials'], [])
        self.assertIsNone(another.upsert_oauth(tokens(), allow_create=False))
        self.assertIsNotNone(another.upsert_oauth(tokens()))

    def test_empty_migration_does_not_import_later_legacy_state(self):
        legacy = openai_oauth.OpenAIOAuth(excel_upstream.ExcelSessionStore())
        self.pool.migrate_legacy(legacy.store, legacy)
        legacy._accept_tokens({'access_token': jwt()})
        self.pool.migrate_legacy(legacy.store, legacy)
        self.assertEqual(self.pool.list_credentials()['credentials'], [])

    def test_round_robin_sticky_and_selection_immutable(self):
        a, b = self.add(), self.add('account-secret-B')
        first = self.pool.acquire('first', False)
        self.assertEqual(first.credential_id, a)
        self.assertFalse(first.switched)
        self.assertEqual(self.pool.acquire('second', True).credential_id, b)
        self.assertEqual(self.pool.acquire('third', False).credential_id, a)
        again = self.pool.acquire('first', True)
        self.assertEqual(again.credential_id, a)
        self.assertEqual(again.headers['accept'], 'text/event-stream')
        with self.assertRaises(TypeError):
            again.headers['authorization'] = 'changed'
        with self.assertRaises(FrozenInstanceError):
            again.switched = True
        self.assertNotIn('Bearer', repr(again))

    def test_failover_rebound_remains_true_and_excludes(self):
        a, b = self.add(), self.add('account-secret-B')
        self.assertEqual(self.pool.acquire('session', False).credential_id, a)
        switched = self.pool.acquire('session', False, exclude_ids={a})
        self.assertEqual(switched.credential_id, b)
        self.assertTrue(switched.switched)
        self.assertTrue(self.pool.acquire('session', True).switched)
        self.pool.remove(b)
        self.assertEqual(self.pool.acquire('session', False).credential_id, a)
        self.assertTrue(self.pool.acquire('session', False).switched)
        self.assert_pool_error(503, lambda: self.pool.acquire('session', False, exclude_ids={a}))

    def test_failover_disabled_expired_auth_cooldown(self):
        for fault in ('disabled', 'expired', 401, 403, 429, 500, 502, 503):
            with self.subTest(fault=fault):
                pool = bps.CredentialPool(clock=lambda: self.now)
                pool.load()
                a = pool.upsert_oauth(tokens(expiry=self.now + 5, refresh=None))['id']
                b = pool.upsert_oauth(tokens('account-secret-B', expiry=self.now + 3600))['id']
                pool.acquire('s', False)
                if fault == 'disabled':
                    pool.update(a, {'enabled': False})
                elif fault == 'expired':
                    self.now += 10
                else:
                    pool.record_result(a, fault)
                selection = pool.acquire('s', False)
                self.assertEqual(selection.credential_id, b)
                self.assertTrue(selection.switched)

    def test_auth_pause_until_enable_or_relogin_and_200_not_verified(self):
        a = self.add()
        self.pool.record_result(a, 200)
        self.assertFalse(self.status(a)['bps_verified'])
        self.pool.record_result(a, 401)
        self.assert_pool_error(503, lambda: self.pool.acquire('s', False))
        self.pool.record_result(a, 200)
        self.assertEqual(self.status(a)['status'], 'paused')
        self.pool.update(a, {'enabled': True})
        self.pool.acquire('s', False)
        self.pool.record_result(a, 403)
        self.pool.upsert_oauth(tokens())
        self.pool.acquire('s', False)
        self.pool.record_probe(a, True, 'private response')
        self.assertTrue(self.status(a)['bps_verified'])

    def test_retry_after_dates_defaults_and_caps(self):
        a = self.add()
        for value, expected in ((None, 60), ('bad secret', 60), ('120', 120), ('999999', 86400), ('-10', 1), ('nan', 60)):
            self.pool.record_result(a, 429, value)
            self.assertEqual(self.status(a)['cooldown_until'], self.now + expected)
        self.pool.record_result(a, 429, formatdate(self.now + 180, usegmt=True))
        self.assertAlmostEqual(self.status(a)['cooldown_until'], self.now + 180, delta=1)
        self.pool.record_result(a, 502, 999999)
        self.assertEqual(self.status(a)['cooldown_until'], self.now + 15)
        self.now += 16
        self.assertEqual(self.pool.acquire('s', False).credential_id, a)

    def test_internal_probe_exact_target_cannot_enable_disabled(self):
        a, b = self.add(), self.add('account-secret-B')
        self.pool.record_result(a, 403)
        self.assertEqual(self.pool.acquire('probe', False, credential_id=a).credential_id, a)
        self.pool.update(a, {'enabled': False})
        self.assert_pool_error(503, lambda: self.pool.acquire('probe', False, credential_id=a))
        self.assert_pool_error(404, lambda: self.pool.acquire('probe', False, credential_id='missing'))
        self.assertEqual(self.pool.acquire('normal', False).credential_id, b)

    def test_bounded_cache_conservatively_marks_evicted_session(self):
        pool = bps.CredentialPool(max_sticky_sessions=2)
        pool.load()
        pool.upsert_oauth(tokens())
        pool.upsert_oauth(tokens('account-secret-B'))
        pool.acquire('first-private-session', False)
        for i in range(10):
            pool.acquire('s-' + str(i), False)
        self.assertEqual(pool.list_credentials()['sticky_sessions'], 2)
        self.assertTrue(pool.acquire('first-private-session', False).switched)
        self.assertNotIn('first-private-session', json.dumps(pool._export()))

    def test_concurrent_balanced_round_robin_and_same_session(self):
        a, b = self.add(), self.add('account-secret-B')
        with ThreadPoolExecutor(max_workers=12) as workers:
            ids = list(workers.map(lambda i: self.pool.acquire('s-' + str(i), False).credential_id, range(80)))
            same = list(workers.map(lambda i: self.pool.acquire('shared', False).credential_id, range(40)))
        self.assertEqual(ids.count(a), 40)
        self.assertEqual(ids.count(b), 40)
        self.assertEqual(len(set(same)), 1)

    def test_invalid_tokens_and_metadata_do_not_leak(self):
        a = self.add()
        for expiry in (float('nan'), float('inf'), True, 'not-an-expiry', 1):
            self.assert_pool_error(400, lambda expiry=expiry: self.pool.upsert_oauth({**tokens(), 'expires_at': expiry}))
        self.pool.record_probe(a, False, json.dumps(tokens()))
        self.pool.update(a, {'label': 'account-secret-A refresh-secret-A'})
        public = json.dumps(self.pool.list_credentials())
        for secret in ('account-secret-A', 'refresh-secret-A', jwt(), 'authorization', 'Bearer'):
            self.assertNotIn(secret, public)
        self.pool.remove(a)
        self.pool.record_result(a, 200)
        self.pool.record_probe(a, True, 'late result')
        self.assertEqual(self.pool.list_credentials()['credentials'], [])


class EmailPoolTests(OfflineCase):
    def test_public_email_default_label_and_custom_label(self):
        access = jwt(email='person@example.test')
        public = self.pool.upsert_oauth({'access_token': access, 'refresh_token': 'private-refresh'})
        self.assertEqual(public['email'], 'person@example.test')
        self.assertEqual(public['label'], public['email'])
        self.pool.update(public['id'], {'label': 'Team budget'})
        updated = self.pool.upsert_oauth({'access_token': jwt(email='new@example.test')})
        self.assertEqual(updated['label'], 'Team budget')
        self.assertEqual(updated['email'], 'new@example.test')
        for sensitive in ('access_token', 'refresh_token', 'id_token', 'tokens', 'headers', 'account_id'):
            self.assertNotIn(sensitive, updated)
        encoded = json.dumps(self.pool.list_credentials())
        for secret in (access, 'private-refresh', 'account-secret-A', 'authorization',
                       'access_token', 'id_token', 'headers', 'account_id'):
            self.assertNotIn(secret, encoded)

    def test_secret_shaped_like_email_is_not_exposed_after_rotation(self):
        secret = 'private-refresh@example.test'
        public = self.pool.upsert_oauth({'access_token': jwt(expiry=self.now + 5, email=secret),
                                         'refresh_token': secret})
        self.assertEqual(public['email'], '')
        self.assertNotIn(secret, json.dumps(public))
        self.pool._token_request = mock.Mock(return_value={'access_token': jwt(), 'refresh_token': 'rotated-private-refresh'})
        self.pool.acquire('test-session', False)
        self.assertEqual(self.status(public['id'])['email'], '')
        self.assertNotIn(secret, json.dumps(self.pool.list_credentials()))

    def test_no_email_cached_session_is_honest_fallback(self):
        public = self.pool.upsert_session(store_for())
        self.assertEqual(public['email'], '')
        self.assertTrue(public['label'].startswith('ChatGPT ****'))
        self.assertEqual(self.pool.upsert_oauth(tokens())['email'], '')

    def test_cached_session_claims_and_auto_label_upgrade(self):
        public = self.pool.upsert_session(store_for())
        store = excel_upstream.ExcelSessionStore()
        store.configure({'authorization': 'Bearer ' + jwt(**{
            'https://api.openai.com/profile': {'email': 'cached@example.test'}}),
            'chatgpt-account-id': 'account-secret-A'}, persist=False)
        updated = self.pool.upsert_session(store)
        self.assertEqual(updated['id'], public['id'])
        self.assertEqual(updated['email'], 'cached@example.test')
        self.assertEqual(updated['label'], updated['email'])

    def test_refresh_keeps_id_token_email_and_custom_label(self):
        for label in (None, 'Custom team'):
            with self.subTest(label=label):
                self.pool = bps.CredentialPool(clock=lambda: self.now)
                self.pool.load()
                public = self.pool.upsert_oauth({
                    'access_token': jwt(expiry=self.now + 5),
                    'id_token': jwt(email='id@example.test'), 'refresh_token': 'private-refresh'}, label=label)
                self.pool._token_request = mock.Mock(return_value={'access_token': jwt()})
                self.pool.acquire('test-session', False)
                refreshed = self.status(public['id'])
                self.assertEqual(refreshed['email'], 'id@example.test')
                self.assertEqual(refreshed['label'], label or 'id@example.test')
                self.assertEqual(self.pool._credentials[public['id']].tokens['email'], 'id@example.test')

    def test_auto_label_tracks_new_email_but_explicit_label_does_not(self):
        public = self.pool.upsert_oauth({'access_token': jwt(expiry=self.now + 5, email='old@example.test'),
                                         'refresh_token': 'private-refresh'})
        self.pool._token_request = mock.Mock(return_value={'access_token': jwt(email='new@example.test')})
        self.pool.acquire('test-session', False)
        self.assertEqual(self.status(public['id'])['label'], 'new@example.test')
        self.pool.update(public['id'], {'label': 'new@example.test'})
        self.pool.upsert_oauth({'access_token': jwt(email='third@example.test')})
        self.assertEqual(self.status(public['id'])['label'], 'new@example.test')

    def test_legacy_migration_keeps_email_and_never_reads_credentials(self):
        service = openai_oauth.OpenAIOAuth(excel_upstream.ExcelSessionStore())
        service._accept_tokens({'access_token': jwt(), 'id_token': jwt(email='id@example.test')})
        with mock.patch('builtins.open', side_effect=AssertionError('Credential reads forbidden')):
            self.pool.migrate_legacy(service.store, service)
        public = self.pool.list_credentials()['credentials'][0]
        self.assertEqual(public['email'], 'id@example.test')


class RefreshTests(OfflineCase):
    def test_concurrent_refresh_runs_once_and_retains_identity(self):
        a = self.add(expiry=self.now + 5)
        callback = mock.Mock(return_value={'access_token': jwt(None), 'refresh_token': 'rotated-private-refresh'})
        self.pool._token_request = callback
        with ThreadPoolExecutor(max_workers=12) as workers:
            selections = list(workers.map(lambda i: self.pool.acquire('shared', False), range(40)))
        self.assertEqual(callback.call_count, 1)
        self.assertTrue(all(s.credential_id == a for s in selections))
        self.assertEqual(selections[0].headers['chatgpt-account-id'], 'account-secret-A')
        self.assertEqual(self.pool._credentials[a].tokens['refresh_token'], 'rotated-private-refresh')

    def test_identity_mismatch_fails_over_without_corruption(self):
        a = self.add(expiry=self.now + 5)
        b = self.add('account-secret-B')
        self.pool._token_request = mock.Mock(return_value={'access_token': jwt('wrong-private-owner')})
        selected = self.pool.acquire('s', False)
        self.assertEqual(selected.credential_id, b)
        self.assertTrue(selected.switched)
        self.assertEqual(self.pool._credentials[a].account_id, 'account-secret-A')
        self.assertNotIn('wrong-private-owner', json.dumps(self.pool.list_credentials()))

    def test_refresh_exception_is_redacted_and_attempts_bounded(self):
        for i in range(3):
            self.add('private-' + str(i), expiry=self.now + 5)
        callback = mock.Mock(side_effect=RuntimeError('Bearer TOP-SECRET refresh-secret-A'))
        self.pool._token_request = callback
        exc = self.assert_pool_error(503, lambda: self.pool.acquire('s', False))
        self.assertEqual(callback.call_count, 3)
        self.assertNotIn('TOP-SECRET', str(exc) + json.dumps(self.pool.list_credentials()))

    def test_refresh_can_be_removed_while_other_account_is_available(self):
        a = self.add(expiry=self.now + 5)
        b = self.add('account-secret-B')
        started, release = threading.Event(), threading.Event()
        def refresh(_):
            started.set()
            if not release.wait(3):
                raise AssertionError('refresh timeout')
            return {'access_token': jwt()}
        self.pool._token_request = refresh
        with ThreadPoolExecutor(max_workers=2) as workers:
            pending = workers.submit(self.pool.acquire, 'refreshing', False)
            try:
                self.assertTrue(started.wait(3))
                self.pool.remove(a)
                self.assertEqual(self.pool.acquire('other', False).credential_id, b)
            finally:
                release.set()
            selected = pending.result(timeout=3)
        self.assertEqual(selected.credential_id, b)
        self.assertTrue(selected.switched)
        self.assertNotIn(a, self.pool._credentials)

    def test_refresh_reuses_token_when_not_rotated(self):
        a = self.add(expiry=self.now + 5)
        self.pool._token_request = mock.Mock(return_value={'access_token': jwt()})
        self.pool.acquire('s', False)
        self.assertEqual(self.pool._credentials[a].tokens['refresh_token'], 'refresh-secret-A')


class PersistenceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'pool.dpapi')
        self.sealed = {}
        def protect(raw):
            ciphertext = b'MOCK-DPAPI-' + hashlib.sha256(raw).digest()
            self.sealed[ciphertext] = raw
            return ciphertext
        mock.patch.object(bps.sys, 'platform', 'win32').start()
        self.protect = mock.patch.object(excel_upstream, '_protect_windows_data', side_effect=protect).start()
        mock.patch.object(excel_upstream, '_unprotect_windows_data', side_effect=lambda raw: self.sealed[raw]).start()
        self.pool = bps.CredentialPool(self.path, clock=lambda: self.now)
        self.pool.load()

    def test_maximum_length_email_survives_reload_without_relaxing_custom_labels(self):
        email = 'a' * 64 + '@' + 'b' * 63 + '.' + 'c' * 63 + '.' + 'd' * 61
        self.assertEqual(len(email), 254)
        public = self.pool.upsert_oauth({'access_token': jwt(email=email)})
        restored = bps.CredentialPool(self.path, clock=lambda: self.now)
        restored.load()
        row = restored.list_credentials()['credentials'][0]
        self.assertEqual(row['email'], email)
        self.assertEqual(row['label'], email)
        with self.assertRaises(bps.PoolError):
            self.pool.update(public['id'], {'label': email})

    def test_old_email_less_pool_and_invalid_saved_email_keep_fallback(self):
        self.add()
        for value in (None, [], 'bad' + chr(10) + '@example.test'):
            payload = self.pool._export()
            row = payload['credentials'][0]
            row.pop('label_is_custom')
            row['email'] = value
            row['tokens']['email'] = value
            Path(self.path).write_bytes(self.protect(json.dumps(payload).encode()))
            restored = bps.CredentialPool(self.path, clock=lambda: self.now)
            restored.load()
            public = restored.list_credentials()['credentials'][0]
            self.assertEqual(public['email'], '')
            self.assertTrue(public['label'].startswith('ChatGPT ****'))

    def test_email_and_custom_labels_survive_encrypted_reload(self):
        for label in (None, 'Custom team'):
            public = self.pool.upsert_oauth({'access_token': jwt(),
                'id_token': jwt(email='id@example.test'), 'refresh_token': 'private-refresh'}, label=label)
            self.assertNotIn(b'id@example.test', Path(self.path).read_bytes())
            restored = bps.CredentialPool(self.path, clock=lambda: self.now)
            restored.load()
            row = restored.list_credentials()['credentials'][0]
            self.assertEqual(row['email'], 'id@example.test')
            self.assertEqual(row['label'], label or 'id@example.test')
            self.assertEqual(restored._credentials[public['id']].label_is_custom, label is not None)

    def test_old_pool_without_email_derives_stored_claims_offline(self):
        for source in ('oauth', 'excel-cache'):
            for label in (None, 'Preserved team'):
                with self.subTest(source=source, label=label):
                    access = jwt(email='old@example.test')
                    if source == 'oauth':
                        self.pool.upsert_oauth({'access_token': access}, label=label)
                    else:
                        self.pool = bps.CredentialPool(self.path, clock=lambda: self.now)
                        self.pool._loaded = True
                        store = excel_upstream.ExcelSessionStore()
                        store.configure({'authorization': 'Bearer ' + access,
                            'chatgpt-account-id': 'account-secret-A'}, persist=False)
                        self.pool.upsert_session(store, label=label)
                    payload = self.pool._export()
                    row = payload['credentials'][0]
                    row.pop('email')
                    row.pop('label_is_custom')
                    row['tokens'].pop('email', None)
                    row['label'] = label or 'ChatGPT ****' + bps._identity(row['account_id'])[:6]
                    Path(self.path).write_bytes(self.protect(json.dumps(payload).encode()))
                    restored = bps.CredentialPool(self.path, clock=lambda: self.now)
                    restored.load()
                    public = restored.list_credentials()['credentials'][0]
                    self.assertEqual(public['email'], 'old@example.test')
                    self.assertEqual(public['label'], label or 'old@example.test')
                    self.assertEqual(restored._export()['version'], 1)

    def test_revision_survives_encrypted_reload(self):
        cid = self.add()
        self.pool.update(cid, {'label': 'Revision one'})
        self.pool.upsert_oauth(tokens(refresh='replacement-refresh'))
        self.assertEqual(self.pool.acquire('persisted-revision', False).revision, 2)
        restored = bps.CredentialPool(self.path, clock=lambda: self.now)
        restored.load()
        selected = restored.acquire('persisted-revision', False)
        self.assertEqual(selected.revision, 2)
        restored.record_result(cid, 401, expected_revision=1)
        restored.record_probe(cid, True, 'stale probe', expected_revision=1)
        status = restored.list_credentials()['credentials'][0]
        self.assertEqual(status['status'], 'active')
        self.assertFalse(status['bps_verified'])

    def test_old_encrypted_format_without_revision_loads_as_zero(self):
        cid = self.add()
        self.pool.update(cid, {'label': 'Old format'})
        payload = self.pool._export()
        del payload['credentials'][0]['revision']
        Path(self.path).write_bytes(self.protect(json.dumps(payload).encode()))
        restored = bps.CredentialPool(self.path, clock=lambda: self.now)
        restored.load()
        self.assertEqual(restored.acquire('old-format', False).revision, 0)

    def test_atomic_encrypted_reload_pins_rebound_and_tombstones(self):
        a, b = self.add(), self.add('account-secret-B')
        self.pool.acquire('very-private-session', False)
        self.pool.acquire('very-private-session', False, exclude_ids={a})
        self.pool.remove(a)
        raw = Path(self.path).read_bytes()
        for secret in (b'account-secret-A', b'account-secret-B', b'refresh-secret-A', b'very-private-session', b'access_token'):
            self.assertNotIn(secret, raw)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [Path(self.path)])
        restored = bps.CredentialPool(self.path, clock=lambda: self.now)
        restored.load()
        selection = restored.acquire('very-private-session', False)
        self.assertEqual(selection.credential_id, b)
        self.assertTrue(selection.switched)
        self.assertEqual(len(restored._tombstones), 1)
        with mock.patch('builtins.open', side_effect=AssertionError('idempotent load must not reopen')):
            restored.load()
        legacy = openai_oauth.OpenAIOAuth(store_for())
        legacy._tokens = tokens()
        restored.migrate_legacy(legacy.store, legacy)
        self.assertEqual(len(restored.list_credentials()['credentials']), 1)

    def test_atomic_replace_failure_rolls_back_and_never_writes_plaintext(self):
        a = self.add()
        before = Path(self.path).read_bytes()
        real_replace = bps.os.replace
        def deny_replace(src, dst):
            temp = Path(src).read_bytes()
            self.assertTrue(temp.startswith(b'MOCK-DPAPI-'))
            self.assertNotIn(b'refresh-secret-A', temp)
            raise OSError('raw-account-secret must not appear')
        with mock.patch.object(bps.os, 'replace', side_effect=deny_replace):
            exc = self.assert_pool_error(503, lambda: self.pool.remove(a))
        self.assertNotIn('raw-account-secret', str(exc))
        self.assertIn(a, self.pool._credentials)
        self.assertEqual(before, Path(self.path).read_bytes())
        self.assertEqual(len(list(Path(self.directory.name).iterdir())), 1)
        self.assertIs(bps.os.replace, real_replace)

    def test_encrypt_failure_has_no_plaintext_fallback(self):
        with mock.patch.object(excel_upstream, '_protect_windows_data', side_effect=RuntimeError('secret error')):
            self.assert_pool_error(503, lambda: self.add())
        self.assertFalse(Path(self.path).exists())
        self.assertEqual(self.pool.list_credentials()['credentials'], [])

    def test_corruption_blocks_load_and_migration_without_overwriting(self):
        Path(self.path).write_bytes(b'invalid-encrypted-file')
        pool = bps.CredentialPool(self.path)
        self.assert_pool_error(503, pool.load)
        legacy = openai_oauth.OpenAIOAuth(store_for())
        legacy._tokens = tokens()
        self.assert_pool_error(503, lambda: pool.migrate_legacy(legacy.store, legacy))
        self.assertFalse(pool.list_credentials()['loaded'])
        self.assertEqual(Path(self.path).read_bytes(), b'invalid-encrypted-file')

    def test_nonwindows_is_explicitly_memory_only(self):
        with mock.patch.object(bps.sys, 'platform', 'linux'):
            pool = bps.CredentialPool(self.path)
            pool.load()
            pool.upsert_oauth(tokens())
            self.assertFalse(Path(self.path).exists())
            self.assertEqual(pool.list_credentials()['storage'], 'memory-only')
            self.assertFalse(pool.list_credentials()['persistence_supported'])
            self.assertIn('memory-only', pool.list_credentials()['persistence_note'])

    def test_migration_marker_survives_empty_removed_pool(self):
        legacy = openai_oauth.OpenAIOAuth(store_for())
        legacy._tokens = tokens()
        self.pool.migrate_legacy(legacy.store, legacy)
        cid = self.pool.list_credentials()['credentials'][0]['id']
        self.pool.remove(cid)
        pool = bps.CredentialPool(self.path)
        pool.load()
        pool.migrate_legacy(legacy.store, legacy)
        self.assertEqual(pool.list_credentials()['credentials'], [])
        self.assertTrue(pool.list_credentials()['legacy_migrated'])


class OAuthIntegrationTests(OfflineCase):
    def test_multiple_logins_add_accounts_and_keep_legacy_store(self):
        store = excel_upstream.ExcelSessionStore()
        service = openai_oauth.OpenAIOAuth(store, pool=self.pool)
        service._accept_tokens({'access_token': jwt(), 'refresh_token': 'refresh-secret-A'})
        service._accept_tokens({'access_token': jwt('account-secret-B')})
        self.assertEqual(len(self.pool.list_credentials()['credentials']), 2)
        self.assertEqual(store.request_headers(stream=False)['chatgpt-account-id'], 'account-secret-B')
        service._accept_tokens({'access_token': jwt()})
        self.assertEqual(len(self.pool.list_credentials()['credentials']), 2)

    def test_legacy_refresh_does_not_recreate_deleted_account(self):
        service = openai_oauth.OpenAIOAuth(excel_upstream.ExcelSessionStore(), pool=self.pool)
        service._accept_tokens({'access_token': jwt(expiry=self.now + 5), 'refresh_token': 'refresh-secret-A'})
        cid = self.pool.list_credentials()['credentials'][0]['id']
        self.pool.remove(cid)
        self.assert_pool_error(410, service.ensure_session)
        service._accept_tokens({'access_token': jwt()}, previous_refresh='refresh-secret-A')
        self.assertEqual(self.pool.list_credentials()['credentials'], [])

    def test_legacy_refresh_and_pool_share_rotated_tokens(self):
        service = openai_oauth.OpenAIOAuth(excel_upstream.ExcelSessionStore(), pool=self.pool)
        service._accept_tokens({'access_token': jwt(expiry=self.now + 5), 'refresh_token': 'refresh-secret-A'})
        callback = mock.Mock(return_value={'access_token': jwt(), 'refresh_token': 'rotated-secret'})
        self.pool._token_request = callback
        self.pool.acquire('s', False)
        service.ensure_session()
        self.assertEqual(service._tokens['refresh_token'], 'rotated-secret')
        self.assertEqual(callback.call_count, 1)

    def test_validation_rejects_switch_and_invalid_expiry_without_mutation(self):
        service = openai_oauth.OpenAIOAuth(excel_upstream.ExcelSessionStore())
        service._accept_tokens({'access_token': jwt(), 'refresh_token': 'refresh-secret-A'})
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            service._accept_tokens({'access_token': jwt('other-owner')}, previous_refresh='refresh-secret-A')
        for expiry in (float('nan'), float('inf'), True, 'invalid', 1):
            with self.assertRaises(RuntimeError):
                service._accept_tokens({'access_token': jwt(expiry=expiry)})
        self.assertEqual(service._tokens['account_id'], 'account-secret-A')

    def test_outbound_proxy_kwargs_and_configuration_errors_fail_closed(self):
        fake = types.ModuleType('outbound_proxy')
        fake.httpx_client_kwargs = mock.Mock(return_value={'proxy': 'http://mock.invalid:1234', 'trust_env': False})
        response = mock.Mock(status_code=200)
        response.json.return_value = {'access_token': 'synthetic'}
        client = mock.MagicMock()
        client.__enter__.return_value.post.return_value = response
        service = openai_oauth.OpenAIOAuth(excel_upstream.ExcelSessionStore())
        with mock.patch.dict('sys.modules', {'outbound_proxy': fake}), mock.patch.object(openai_oauth.os.path, 'isfile', return_value=True), mock.patch.object(openai_oauth.httpx, 'Client', return_value=client) as factory:
            self.assertEqual(service._token_request({'code': 'fake-code'}), {'access_token': 'synthetic'})
            self.assertEqual(factory.call_args.kwargs, {'timeout': 30, 'follow_redirects': False, 'proxy': 'http://mock.invalid:1234', 'trust_env': False})
            fake.httpx_client_kwargs.return_value = {}
            service._token_request({'code': 'fake-code'})
            self.assertEqual(factory.call_args.kwargs, {'timeout': 30, 'follow_redirects': False})
            fake.httpx_client_kwargs.side_effect = ValueError('SECRET proxy config')
            factory.reset_mock()
            with self.assertRaisesRegex(RuntimeError, 'outbound proxy') as caught:
                service._token_request({'code': 'fake-code'})
            self.assertNotIn('SECRET', str(caught.exception))
            factory.assert_not_called()

    def test_absent_proxy_module_keeps_legacy_client_defaults(self):
        client = mock.MagicMock()
        client.__enter__.return_value.post.return_value.status_code = 200
        client.__enter__.return_value.post.return_value.json.return_value = {}
        service = openai_oauth.OpenAIOAuth(excel_upstream.ExcelSessionStore())
        with mock.patch.object(openai_oauth.os.path, 'isfile', return_value=False), mock.patch.object(openai_oauth.httpx, 'Client', return_value=client) as factory:
            service._token_request({'code': 'fake'})
            self.assertEqual(factory.call_args.kwargs, {'timeout': 30, 'follow_redirects': False})



class AdditionalSafetyTests(OfflineCase):
    def test_disabled_session_import_does_not_replace_tokens(self):
        first_store = store_for(expiry=self.now + 100)
        cid = self.pool.upsert_session(first_store)['id']
        original = self.pool._credentials[cid].headers['authorization']
        self.pool.update(cid, {'enabled': False})
        self.pool.upsert_session(store_for(expiry=self.now + 300), label='Should not overwrite')
        self.assertEqual(self.pool._credentials[cid].headers['authorization'], original)
        self.assertNotEqual(self.status(cid)['label'], 'Should not overwrite')

    def test_failed_probe_preserves_cooldown_and_does_not_invent_auth_failure(self):
        cid = self.add()
        self.pool.record_result(cid, 429, 120)
        self.pool.record_probe(cid, False, 'possibly transient')
        self.assertEqual(self.status(cid)['status'], 'cooldown')
        self.assertEqual(self.status(cid)['cooldown_until'], self.now + 120)
        self.now += 121
        self.assertEqual(self.pool.acquire('s', False).credential_id, cid)
        self.pool.record_result(cid, 401)
        self.pool.record_probe(cid, False, 'auth failure')
        self.assertEqual(self.status(cid)['status'], 'paused')

    def test_failed_rate_limit_or_network_probe_preserves_cooldown_without_new_pause(self):
        for status_code, delay in ((429, 120), (502, 15), (None, 0)):
            with self.subTest(status_code=status_code):
                self.pool = bps.CredentialPool(clock=lambda: self.now)
                self.pool.load()
                cid = self.add()
                selected = self.pool.acquire('probe-cooldown', False)
                self.pool.record_probe(cid, True, 'valid inference', expected_revision=selected.revision)
                if status_code is not None:
                    self.pool.record_result(cid, status_code, retry_after=120, expected_revision=selected.revision)
                before = dict(self.pool._export()['credentials'][0])
                deadline = self.now + delay
                for _ in range(2):
                    self.pool.record_probe(cid, False, 'private upstream token or network diagnostic',
                                           expected_revision=selected.revision)
                    after = dict(self.pool._export()['credentials'][0])
                    self.assertEqual(after, {**before, 'bps_verified': False,
                                             'error': 'BPS access verification failed.'})
                    self.assertFalse(after['paused'])
                if delay:
                    self.assertEqual(self.status(cid)['cooldown_until'], deadline)
                    self.assert_pool_error(503, lambda: self.pool.acquire('probe-cooldown', False))
                    self.now = deadline - 0.25
                    self.assert_pool_error(503, lambda: self.pool.acquire('probe-cooldown', False))
                    self.now = deadline + 0.25
                self.assertEqual(self.pool.acquire('probe-cooldown', False).credential_id, cid)
                self.assertFalse(self.pool._credentials[cid].paused)
                # A stale success must not clear fresh suspension/cooldown either.
                self.pool.record_result(cid, 401, expected_revision=selected.revision)
                self.pool.record_result(cid, 429, retry_after=120, expected_revision=selected.revision)
                suspended = self.pool._credentials[cid]
                self.pool.record_probe(cid, True, 'stale success', expected_revision=selected.revision + 1)
                self.assertEqual(self.pool._credentials[cid], suspended)
                self.pool.record_probe(cid, True, 'valid inference', expected_revision=selected.revision)
                recovered = self.pool._credentials[cid]
                self.assertTrue(recovered.bps_verified)
                self.assertFalse(recovered.paused)
                self.assertEqual(recovered.cooldown_until, 0)
                self.assertEqual(recovered.error, '')

    def test_legacy_accept_refresh_preserves_auth_pause(self):
        service = openai_oauth.OpenAIOAuth(excel_upstream.ExcelSessionStore(), pool=self.pool)
        service._accept_tokens({'access_token': jwt(), 'refresh_token': 'refresh-secret-A'})
        cid = self.pool.list_credentials()['credentials'][0]['id']
        self.pool.record_result(cid, 401)
        service._accept_tokens({'access_token': jwt()}, previous_refresh='refresh-secret-A')
        self.assertEqual(self.status(cid)['status'], 'paused')

    def test_redacted_label_remains_safe_after_rotation_and_removal(self):
        a = self.add()
        b = self.add('account-secret-B', refresh='refresh-secret-B')
        self.pool.update(b, {'label': 'Team account-secret-A refresh-secret-A'})
        self.pool.upsert_oauth(tokens(refresh='replacement-refresh'))
        self.pool.remove(a)
        public = json.dumps(self.pool.list_credentials())
        self.assertNotIn('account-secret-A', public)
        self.assertNotIn('refresh-secret-A', public)

    def test_invalid_acquire_inputs(self):
        self.add()
        for key in ('', None, 'x' * 8193):
            self.assert_pool_error(400, lambda key=key: self.pool.acquire(key, False))
        for stream in (None, 'false', 0):
            self.assert_pool_error(400, lambda stream=stream: self.pool.acquire('s', stream))
        for excluded in ('not-a-list', 10, [10]):
            self.assert_pool_error(400, lambda excluded=excluded: self.pool.acquire('s', False, exclude_ids=excluded))

    def test_concurrent_disable_retains_rotated_token_without_reenabling(self):
        a = self.add(expiry=self.now + 5)
        b = self.add('account-secret-B')
        started, release = threading.Event(), threading.Event()
        def refresh(_):
            started.set()
            if not release.wait(3):
                raise AssertionError('refresh timeout')
            return {'access_token': jwt(), 'refresh_token': 'rotated-even-when-disabled'}
        self.pool._token_request = refresh
        with ThreadPoolExecutor(max_workers=2) as workers:
            future = workers.submit(self.pool.acquire, 's', False)
            try:
                self.assertTrue(started.wait(3))
                self.pool.update(a, {'enabled': False, 'label': 'Retained label'})
            finally:
                release.set()
            self.assertEqual(future.result(timeout=3).credential_id, b)
        self.assertFalse(self.status(a)['enabled'])
        self.assertEqual(self.status(a)['label'], 'Retained label')
        self.assertEqual(self.pool._credentials[a].tokens['refresh_token'], 'rotated-even-when-disabled')

    def test_eviction_during_refresh_never_raises_keyerror_or_loses_rebound(self):
        pool = bps.CredentialPool(max_sticky_sessions=1)
        pool.load()
        a = pool.upsert_oauth(tokens(expiry=self.now + 5))['id']
        b = pool.upsert_oauth(tokens('account-secret-B'))['id']
        started, release = threading.Event(), threading.Event()
        def refresh(_):
            started.set()
            if not release.wait(3):
                raise AssertionError('refresh timeout')
            return {'access_token': jwt()}
        pool._token_request = refresh
        with ThreadPoolExecutor(max_workers=2) as workers:
            future = workers.submit(pool.acquire, 'evicted-while-refreshing', False)
            try:
                self.assertTrue(started.wait(3))
                pool.acquire('replacement', False, credential_id=b)
            finally:
                release.set()
            selected = future.result(timeout=3)
        self.assertIn(selected.credential_id, {a, b})
        self.assertTrue(selected.switched)

    def test_captured_session_tools_version_is_preserved(self):
        store = store_for()
        version = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        store.configure(dict(store._headers), tools_version_id=version, persist=False)
        cid = self.pool.upsert_session(store)['id']
        selected = self.pool.acquire('tools', True)
        self.assertEqual(selected.credential_id, cid)
        self.assertEqual(selected.tools_version_id, version)


class RevisionTests(OfflineCase):
    def assert_stale_results_ignored(self, selected):
        before = json.dumps(self.pool._export(), sort_keys=True)
        with mock.patch.object(self.pool, '_persist') as persist:
            for status in (200, 401, 403, 429, 502):
                self.pool.record_result(selected.credential_id, status, retry_after=120,
                                        expected_revision=selected.revision)
            for ok in (False, True):
                self.pool.record_probe(selected.credential_id, ok, 'old response',
                                       expected_revision=selected.revision)
            persist.assert_not_called()
        self.assertEqual(json.dumps(self.pool._export(), sort_keys=True), before)

    def test_selection_revision_defaults_and_tracks_admin_update(self):
        minimal = bps.CredentialSelection('id', {})
        self.assertEqual(minimal.revision, 0)
        with self.assertRaises(FrozenInstanceError):
            minimal.revision = 1
        cid = self.add()
        first = self.pool.acquire('revision-session', False)
        self.assertEqual(first.revision, 0)
        self.pool.update(cid, {'label': 'New label'})
        updated = self.pool.acquire('revision-session', False)
        self.assertEqual(updated.revision, 1)
        self.assertEqual(updated.revision, self.pool._credentials[cid].revision)
        self.assertEqual(first.revision, 0)
        self.assert_stale_results_ignored(first)

    def test_old_results_after_relogin_do_not_change_new_credential(self):
        service = openai_oauth.OpenAIOAuth(excel_upstream.ExcelSessionStore(), pool=self.pool)
        service._accept_tokens({'access_token': jwt(), 'refresh_token': 'original-refresh'})
        first = self.pool.acquire('relogin-session', False)
        service._accept_tokens({'access_token': jwt(), 'refresh_token': 'replacement-refresh'})
        new = self.pool.acquire('relogin-session', False)
        self.assertEqual(first.credential_id, new.credential_id)
        self.assertEqual(new.revision, first.revision + 1)
        self.assertFalse(self.status(new.credential_id)['bps_verified'])
        self.assert_stale_results_ignored(first)
        self.pool.record_probe(new.credential_id, True, 'valid inference', expected_revision=new.revision)
        self.assert_stale_results_ignored(first)
        self.assertTrue(self.status(new.credential_id)['bps_verified'])
        self.assertEqual(self.status(new.credential_id)['status'], 'active')

    def test_old_results_after_disable_enable_or_label_update_are_ignored(self):
        cid = self.add()
        old = self.pool.acquire('admin-session', False)
        for revision, change in enumerate(({'enabled': False}, {'enabled': True}, {'label': 'Renamed'}), 1):
            with self.subTest(change=change):
                self.pool.update(cid, change)
                self.assertEqual(self.pool._credentials[cid].revision, revision)
                self.assert_stale_results_ignored(old)
        self.assertEqual(self.status(cid)['label'], 'Renamed')
        self.assertEqual(self.status(cid)['status'], 'active')

    def test_inflight_old_results_after_concurrent_refresh_are_ignored(self):
        cid = self.add(expiry=self.now + 120)
        old = self.pool.acquire('refresh-session', False)
        self.now += 100
        started, release = threading.Event(), threading.Event()
        def refresh(_):
            started.set()
            if not release.wait(3):
                raise AssertionError('refresh timeout')
            return {'access_token': jwt(), 'refresh_token': 'rotated-refresh'}
        self.pool._token_request = mock.Mock(side_effect=refresh)
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(self.pool.acquire, 'refresh-session', False)
            try:
                self.assertTrue(started.wait(3))
                self.assertEqual(self.pool._credentials[cid].revision, old.revision)
            finally:
                release.set()
            refreshed = future.result(timeout=3)
        self.assertEqual(refreshed.revision, old.revision + 1)
        self.assertEqual(refreshed.revision, self.pool._credentials[cid].revision)
        self.assertEqual(self.pool._token_request.call_count, 1)
        self.assert_stale_results_ignored(old)
        self.assertEqual(self.status(cid)['status'], 'active')
        self.assertEqual(self.pool._credentials[cid].tokens['refresh_token'], 'rotated-refresh')

    def test_matching_and_omitted_revisions_preserve_legacy_behavior(self):
        cid = self.add()
        self.pool.update(cid, {'label': 'Revision one'})
        selected = self.pool.acquire('matching-session', False)
        self.pool.record_result(cid, 401, expected_revision=selected.revision)
        self.assertEqual(self.status(cid)['status'], 'paused')
        self.pool.record_probe(cid, True, 'valid inference', expected_revision=selected.revision)
        self.assertEqual(self.status(cid)['status'], 'active')
        self.assertTrue(self.status(cid)['bps_verified'])
        self.pool.record_result(cid, 403)
        self.assertEqual(self.status(cid)['status'], 'paused')
        self.pool.record_probe(cid, True, 'legacy caller')
        self.assertEqual(self.status(cid)['status'], 'active')
        self.pool.record_result(cid, 401, expected_revision=None)
        self.assertEqual(self.status(cid)['status'], 'paused')
        self.pool.record_probe(cid, True, 'legacy caller', expected_revision=None)
        self.assertEqual(self.status(cid)['status'], 'active')

    def test_explicit_captured_session_replacement_advances_revision(self):
        cid = self.pool.upsert_session(store_for(expiry=self.now + 120))['id']
        old = self.pool.acquire('capture-session', False)
        self.pool.upsert_session(store_for(expiry=self.now + 3600))
        selected = self.pool.acquire('capture-session', False)
        self.assertEqual(selected.credential_id, cid)
        self.assertEqual(selected.revision, old.revision + 1)
        self.assert_stale_results_ignored(old)

    def test_migration_prefers_same_account_oauth_refresh_token(self):
        legacy = openai_oauth.OpenAIOAuth(excel_upstream.ExcelSessionStore())
        legacy._accept_tokens({'access_token': jwt(expiry=self.now + 1800),
                               'refresh_token': 'legacy-refresh-must-survive'})
        captured = store_for(expiry=self.now + 3600)
        self.pool.migrate_legacy(captured, legacy)
        credentials = self.pool.list_credentials()['credentials']
        self.assertEqual(len(credentials), 1)
        cid = credentials[0]['id']
        self.assertEqual(credentials[0]['source'], 'oauth')
        self.assertTrue(credentials[0]['has_refresh_token'])
        self.assertEqual(self.pool._credentials[cid].tokens, legacy._tokens)
        self.assertEqual(self.pool.acquire('migrated-session', False).revision, 0)
        self.pool.migrate_legacy(captured, legacy)
        self.pool.upsert_session(captured)
        self.assertEqual(self.pool._credentials[cid].tokens['refresh_token'], 'legacy-refresh-must-survive')
        self.pool.remove(cid)
        self.assert_pool_error(410, lambda: self.pool.upsert_session(captured))
        self.pool.migrate_legacy(captured, legacy)
        self.assertEqual(self.pool.list_credentials()['credentials'], [])

    def test_late_revision_results_do_not_resurrect_removed_credentials(self):
        cid = self.add()
        old = self.pool.acquire('deleted-session', False)
        self.pool.remove(cid)
        self.assert_stale_results_ignored(old)
        self.assertEqual(self.pool.list_credentials()['credentials'], [])

if __name__ == '__main__':
    unittest.main()
