"""BPS multi-account credentials, with no credential I/O before explicit load().

Public metadata never includes tokens, headers, or account IDs; email is display-only. Windows
storage uses atomic DPAPI writes; other platforms are explicitly memory-only.
Session keys are SHA256 hashes, never raw conversation keys or prompts. Rebound
means ever switched, not just this request. An evicted/unknown session is marked
rebound conservatively, so callers must rebuild owner-bound context.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
import sys
import tempfile
import threading
import time
from types import MappingProxyType
from typing import Mapping
from uuid import uuid4

import excel_upstream
from app_paths import user_state_dir

MAX_ACCOUNTS = 32
MAX_STICKY_SESSIONS = 4096
REFRESH_SKEW = 60


class PoolError(RuntimeError):
    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CredentialSelection:
    credential_id: str
    headers: Mapping[str, str] = field(repr=False)
    tools_version_id: str | None = None
    switched: bool = False
    revision: int = 0

    def __post_init__(self):
        object.__setattr__(self, 'headers', MappingProxyType(dict(self.headers)))


@dataclass(frozen=True)
class _Credential:
    id: str
    account_id: str = field(repr=False)
    label: str
    source: str
    headers: dict = field(repr=False)
    tokens: dict = field(repr=False)
    tools_version_id: str | None = None
    email: str = ""
    label_is_custom: bool = False
    expires_at: float | None = None
    enabled: bool = True
    bps_verified: bool = False
    error: str = ''
    cooldown_until: float = 0
    paused: bool = False
    revision: int = 0
    refresh_lock: object = field(default_factory=threading.Lock, repr=False, compare=False)


def _identity(account):
    return hashlib.sha256(('chatgpt-account:' + account).encode()).hexdigest()


def _label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 120 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise PoolError('Label must contain 1-120 printable characters.', 400)
    return value.strip()


def _finite(value):
    if isinstance(value, bool):
        raise ValueError('invalid number')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('invalid number')
    return result


class CredentialPool:
    def __init__(self, persistence_file=None, *, max_accounts=MAX_ACCOUNTS,
                 max_sticky_sessions=MAX_STICKY_SESSIONS, clock=None,
                 token_request=None, _default_storage=False):
        if type(max_accounts) is not int or not 1 <= max_accounts <= MAX_ACCOUNTS:
            raise ValueError('max_accounts must be between 1 and 32')
        if type(max_sticky_sessions) is not int or max_sticky_sessions < 1:
            raise ValueError('max_sticky_sessions must be positive')
        self.persistence_file = os.fspath(persistence_file) if persistence_file is not None else None
        self._default_storage = _default_storage
        self.max_accounts = max_accounts
        self.max_sticky_sessions = max_sticky_sessions
        self._clock = clock or time.time
        self._token_request = token_request
        self._lock = threading.RLock()
        self._credentials = {}
        self._tombstones = set()
        self._sticky = OrderedDict()
        self._evicted = False
        self._cursor = 0
        self._loaded = False
        self._migrated = False
        self._persisted = False
        self._persistence_error = ''
        self._transaction_depth = 0

    def _require_loaded(self):
        if not self._loaded:
            raise PoolError('Credential pool is not loaded. Call load() first.')

    def _public(self, entry):
        from openai_oauth import _display_email
        now = self._clock()
        if not entry.enabled:
            status = 'disabled'
        elif entry.paused:
            status = 'paused'
        elif entry.cooldown_until > now:
            status = 'cooldown'
        elif entry.expires_at is not None and entry.expires_at <= now:
            status = 'refresh_required' if entry.tokens.get('refresh_token') else 'expired'
        else:
            status = 'active'
        label = self._safe_label(entry.label, entry)
        email = _display_email(entry.email)
        if self._safe_label(email, entry) != email:
            email = ''
        return {'id': entry.id, 'label': label, 'email': email, 'enabled': entry.enabled,
                'source': entry.source, 'expires_at': entry.expires_at,
                'bps_verified': entry.bps_verified, 'status': status, 'error': entry.error,
                'cooldown_until': entry.cooldown_until,
                'has_refresh_token': bool(entry.tokens.get('refresh_token')),
                'tools_version_id': entry.tools_version_id}

    def _safe_label(self, label, extra=None):
        records = list(self._credentials.values()) + ([extra] if extra else [])
        for record in records:
            bearer = record.headers.get('authorization', '')
            secrets = [record.account_id, record.tokens.get('access_token'),
                       record.tokens.get('refresh_token'), bearer, bearer.partition(' ')[2]]
            for secret in sorted((s for s in secrets if isinstance(s, str) and s), key=len, reverse=True):
                label = label.replace(secret, '[redacted]')
        return label

    def _sanitize_metadata(self, entry):
        # Scrub while old and new secrets are still known, before rotation or
        # removal makes redaction impossible. Never return a redacted email.
        from openai_oauth import _display_email
        email = _display_email(entry.email)
        if self._safe_label(email, entry) != email:
            email = ''
        label = entry.label if entry.label_is_custom else email or 'ChatGPT ****' + _identity(entry.account_id)[:6]
        tokens = {**entry.tokens, 'email': email} if entry.tokens else {}
        return replace(entry, email=email, tokens=tokens, label=self._safe_label(label, entry))

    def list_credentials(self):
        with self._lock:
            supported = sys.platform == 'win32'
            return {'credentials': [self._public(c) for c in self._credentials.values()],
                    'strategy': 'sticky_round_robin', 'loaded': self._loaded,
                    'legacy_migrated': self._migrated, 'max_accounts': self.max_accounts,
                    'sticky_sessions': len(self._sticky), 'max_sticky_sessions': self.max_sticky_sessions,
                    'persistence_supported': supported, 'persisted': self._persisted,
                    'storage': 'memory-and-windows-dpapi' if self._persisted else 'memory-only',
                    'persistence_error': self._persistence_error,
                    'persistence_note': '' if supported else 'Secure disk storage unavailable; credentials and session pins are memory-only.'}

    def _export(self):
        fields = ('id', 'account_id', 'label', 'source', 'headers', 'tokens', 'tools_version_id',
                  'expires_at', 'enabled', 'bps_verified', 'error', 'cooldown_until', 'paused', 'revision',
                  'email', 'label_is_custom')
        return {'version': 1, 'credentials': [{k: getattr(c, k) for k in fields} for c in self._credentials.values()],
                'tombstones': sorted(self._tombstones), 'legacy_migrated': self._migrated,
                'sticky': self._sticky, 'cursor': self._cursor, 'evicted': self._evicted}

    @contextmanager
    def _transaction(self):
        # Caller holds _lock. Records are immutable, so shallow rollback is safe.
        snapshot = (self._credentials.copy(), self._tombstones.copy(), self._sticky.copy(),
                    self._cursor, self._migrated, self._evicted)
        self._transaction_depth += 1
        try:
            yield
            if self._transaction_depth == 1:
                self._persist()
        except Exception:
            self._credentials, self._tombstones, self._sticky, self._cursor, self._migrated, self._evicted = snapshot
            raise
        finally:
            self._transaction_depth -= 1

    def _persist(self):
        if sys.platform != 'win32' or not self.persistence_file:
            return
        temporary = None
        try:
            protected = excel_upstream._protect_windows_data(
                json.dumps(self._export(), separators=(',', ':'), allow_nan=False).encode())
            directory = os.path.dirname(os.path.abspath(self.persistence_file))
            os.makedirs(directory, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.bps-pool-', suffix='.tmp', dir=directory)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(protected)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.persistence_file)
            self._persisted = True
            self._persistence_error = ''
        except Exception:
            self._persistence_error = 'Encrypted credential persistence failed; the change was not applied.'
            raise PoolError(self._persistence_error) from None
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError:
                    pass

    def load(self):
        with self._lock:
            if self._loaded:
                return self.list_credentials()
            if self._default_storage and sys.platform == 'win32':
                self.persistence_file = os.path.join(user_state_dir(), 'bps-credentials.dpapi')
            if sys.platform == 'win32' and self.persistence_file and os.path.isfile(self.persistence_file):
                try:
                    with open(self.persistence_file, 'rb') as handle:
                        protected = handle.read(8 * 1024 * 1024 + 1)
                    if len(protected) > 8 * 1024 * 1024:
                        raise ValueError('oversized pool')
                    self._restore(json.loads(excel_upstream._unprotect_windows_data(protected)))
                    self._persisted = True
                except Exception:
                    self._persistence_error = 'Could not restore encrypted pool; legacy migration is blocked.'
                    raise PoolError(self._persistence_error) from None
            self._loaded = True
            return self.list_credentials()

    def _normalize_session(self, headers, tools_version_id=None):
        try:
            if not isinstance(headers, dict):
                raise ValueError('invalid headers')
            for value in headers.values():
                if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
                    raise ValueError('invalid header')
            store = excel_upstream.ExcelSessionStore()
            store.configure(headers, tools_version_id=tools_version_id, persist=False, allow_expired=True)
            return dict(store._headers), store._tools_version_id, store._expires_at
        except Exception:
            raise PoolError('Invalid BPS credential headers or tools version.', 400) from None

    def _normalize_oauth(self, tokens, *, allow_expired=False):
        from openai_oauth import _jwt_claims, _token_email, validate_oauth_tokens
        try:
            if not isinstance(tokens, dict):
                raise ValueError('invalid tokens')
            if 'account_id' not in tokens or 'expires_at' not in tokens:
                tokens = validate_oauth_tokens(tokens)
            account, access = tokens['account_id'], tokens['access_token']
            if not isinstance(account, str) or not account or len(account) > 512:
                raise ValueError('invalid account')
            if not isinstance(access, str) or not access or len(access) > 32700:
                raise ValueError('invalid access')
            expiry = _finite(tokens['expires_at'])
            claims = _jwt_claims(access)
            auth = claims.get('https://api.openai.com/auth', {})
            claimed = auth.get('chatgpt_account_id') if isinstance(auth, dict) else None
            if claimed and claimed != account:
                raise ValueError('identity mismatch')
            if claims.get('exp') is not None:
                expiry = min(expiry, _finite(claims['exp']))
            if not allow_expired and expiry <= self._clock():
                raise ValueError('expired tokens')
            refresh = tokens.get('refresh_token')
            if refresh is not None and (not isinstance(refresh, str) or not refresh or len(refresh) > 32700):
                raise ValueError('invalid refresh')
            normalized = {'account_id': account, 'access_token': access, 'refresh_token': refresh, 'expires_at': expiry,
                          'email': _token_email(tokens)}
            headers, _, _ = self._normalize_session({'authorization': 'Bearer ' + access,
                'chatgpt-account-id': account, 'x-openai-account-id': account})
            return normalized, headers
        except Exception:
            raise PoolError('Invalid or expired OAuth credentials. Sign in again.', 400) from None

    def _restore(self, payload):
        from openai_oauth import _token_email
        if not isinstance(payload, dict) or payload.get('version') != 1:
            raise ValueError('unsupported pool')
        rows = payload['credentials']
        if not isinstance(rows, list) or len(rows) > self.max_accounts:
            raise ValueError('invalid credentials')
        credentials, identities = {}, set()
        def hexstr(value, length):
            return isinstance(value, str) and len(value) == length and all(c in '0123456789abcdef' for c in value)
        for row in rows:
            cid = row['id']
            if not hexstr(cid, 32) or cid in credentials:
                raise ValueError('invalid id')
            source = row['source']
            if source not in {'oauth', 'excel-cache'}:
                raise ValueError('invalid source')
            headers, version, expiry = self._normalize_session(row['headers'], row.get('tools_version_id'))
            account, tokens = headers['chatgpt-account-id'], {}
            if source == 'oauth':
                tokens, token_headers = self._normalize_oauth(row['tokens'], allow_expired=True)
                if account != tokens['account_id'] or headers['authorization'] != token_headers['authorization']:
                    raise ValueError('inconsistent credentials')
                expiry = tokens['expires_at']
            if row['account_id'] != account or _identity(account) in identities:
                raise ValueError('duplicate identity')
            for key in ('enabled', 'bps_verified', 'paused'):
                if type(row[key]) is not bool:
                    raise ValueError('invalid flag')
            revision = row.get('revision', 0)
            if type(revision) is not int or revision < 0:
                raise ValueError('invalid credential revision')
            email = _token_email(tokens or {'access_token': headers['authorization'].partition(' ')[2]}, row.get('email'))
            if tokens:
                tokens['email'] = email
            fallback = 'ChatGPT ****' + _identity(account)[:6]
            custom = row.get('label_is_custom', row['label'] != fallback)
            if type(custom) is not bool:
                raise ValueError('invalid label metadata')
            label = _label(row['label']) if custom else email or fallback
            entry = _Credential(cid, account, label, source, headers, tokens,
                email=email, label_is_custom=custom,
                tools_version_id=version, expires_at=expiry, enabled=row['enabled'], bps_verified=row['bps_verified'],
                cooldown_until=_finite(row['cooldown_until']), paused=row['paused'], revision=revision,
                error='Credential requires attention.' if row.get('error') else '')
            credentials[cid] = entry
            identities.add(_identity(account))
        tombstones, sticky = payload['tombstones'], payload['sticky']
        if not isinstance(tombstones, list) or any(not hexstr(t, 64) for t in tombstones) or set(tombstones) & identities:
            raise ValueError('invalid tombstones')
        if not isinstance(sticky, dict) or len(sticky) > self.max_sticky_sessions:
            raise ValueError('invalid sticky cache')
        for key, pin in sticky.items():
            if not hexstr(key, 64) or not isinstance(pin, dict) or not hexstr(pin.get('credential_id'), 32) or type(pin.get('rebound')) is not bool:
                raise ValueError('invalid session pin')
        if type(payload['legacy_migrated']) is not bool or type(payload['evicted']) is not bool or type(payload['cursor']) is not int or payload['cursor'] < 0:
            raise ValueError('invalid metadata')
        self._credentials, self._tombstones, self._sticky = credentials, set(tombstones), OrderedDict(sticky)
        self._credentials = {cid: self._sanitize_metadata(entry) for cid, entry in credentials.items()}
        self._migrated, self._cursor, self._evicted = payload['legacy_migrated'], payload['cursor'], payload['evicted']

    def _put(self, headers, *, tokens=None, tools_version_id=None, expires_at=None, source='excel-cache',
             label=None, allow_create=True, migration=False, previous_refresh=None):
        from openai_oauth import _token_email
        account = headers['chatgpt-account-id']
        identity = _identity(account)
        existing = next((c for c in self._credentials.values() if c.account_id == account), None)
        if existing is None and (not allow_create or (migration and identity in self._tombstones)):
            return None
        if existing is not None and migration:
            return self._public(existing)
        if existing and previous_refresh is not None and existing.tokens.get('refresh_token') != previous_refresh:
            return self._public(existing)
        if existing and existing.source == 'oauth' and source != 'oauth':
            return self._public(existing)
        if existing is None and len(self._credentials) >= self.max_accounts:
            raise PoolError('The credential pool has reached its account limit.', 409)
        if label is not None:
            label = _label(label)
        email = _token_email(tokens or {'access_token': headers['authorization'].partition(' ')[2]},
                             existing.email if existing else '')
        if tokens:
            tokens = {**tokens, 'email': email}
        custom = label is not None or bool(existing and existing.label_is_custom)
        display_label = label or (existing.label if custom and existing else email or 'ChatGPT ****' + identity[:6])
        with self._transaction():
            if existing:
                entry = replace(existing, headers=dict(headers), tokens=dict(tokens or {}),
                    tools_version_id=tools_version_id or existing.tools_version_id, expires_at=expires_at,
                    label=display_label, email=email, label_is_custom=custom, source=source, revision=existing.revision + 1,
                    bps_verified=False, error='', cooldown_until=0, paused=False)
            else:
                entry = _Credential(uuid4().hex, account, display_label,
                    source, dict(headers), dict(tokens or {}), email=email, label_is_custom=custom,
                    tools_version_id=tools_version_id, expires_at=expires_at)
            if existing and not allow_create:
                # Legacy refresh must not clear a manual disable or auth pause.
                entry = replace(entry, paused=existing.paused, cooldown_until=existing.cooldown_until,
                                error=existing.error, bps_verified=existing.bps_verified)
            entry = self._sanitize_metadata(entry)
            self._credentials[entry.id] = entry
            self._tombstones.discard(identity)
        return self._public(entry)

    def upsert_oauth(self, tokens, label=None, *, allow_create=True, previous_refresh=None):
        normalized, headers = self._normalize_oauth(tokens)
        with self._lock:
            self._require_loaded()
            return self._put(headers, tokens=normalized, expires_at=normalized['expires_at'], source='oauth',
                label=label, allow_create=allow_create, previous_refresh=previous_refresh)

    def upsert_session(self, session_store, label=None):
        """Explicitly import a loaded ExcelSessionStore (never read its disk file)."""
        with session_store._lock:
            headers = dict(session_store._headers)
            version = session_store._tools_version_id
        headers, version, expiry = self._normalize_session(headers, version)
        with self._lock:
            self._require_loaded()
            if _identity(headers['chatgpt-account-id']) in self._tombstones:
                raise PoolError('This account was removed. Sign in explicitly to add it again.', 410)
            existing = next((c for c in self._credentials.values() if c.account_id == headers['chatgpt-account-id']), None)
            if existing and not existing.enabled:
                return self._public(existing)
            return self._put(headers, tools_version_id=version, expires_at=expiry, label=label)

    def migrate_legacy(self, session_store, oauth_service):
        self.load()
        with self._lock:
            if self._migrated:
                return self.list_credentials()
        # Avoid lock inversion with OAuth acceptance, which calls the pool while
        # holding the OAuth lock. Migration imports in-memory snapshots only.
        with oauth_service._lock:
            oauth_tokens = dict(oauth_service._tokens)
        with session_store._lock:
            headers = dict(session_store._headers)
            version = session_store._tools_version_id
        with self._lock:
            if self._migrated:
                return self.list_credentials()
            with self._transaction():
                if oauth_tokens:
                    normalized, normalized_headers = self._normalize_oauth(oauth_tokens, allow_expired=True)
                    self._put(normalized_headers, tokens=normalized, expires_at=normalized['expires_at'], source='oauth', migration=True)
                if headers:
                    normalized_headers, version, expiry = self._normalize_session(headers, version)
                    self._put(normalized_headers, tools_version_id=version, expires_at=expiry, migration=True)
                self._migrated = True
            return self.list_credentials()

    def update(self, credential_id, values):
        if not isinstance(values, dict) or set(values) - {'label', 'enabled'}:
            raise PoolError('Only label and enabled may be updated.', 400)
        if 'enabled' in values and type(values['enabled']) is not bool:
            raise PoolError('enabled must be a boolean.', 400)
        values = dict(values)
        if 'label' in values:
            values['label'] = _label(values['label'])
            values['label_is_custom'] = True
        if values.get('enabled') is True:
            values.update(paused=False, cooldown_until=0, error='')
        with self._lock:
            self._require_loaded()
            entry = self._get(credential_id)
            with self._transaction():
                entry = replace(entry, **values, revision=entry.revision + 1)
                entry = self._sanitize_metadata(entry)
                self._credentials[entry.id] = entry
            return self._public(entry)

    def remove(self, credential_id):
        with self._lock:
            self._require_loaded()
            entry = self._get(credential_id)
            with self._transaction():
                self._tombstones.add(_identity(entry.account_id))
                del self._credentials[entry.id]
            return {'id': entry.id, 'removed': True}

    def _get(self, credential_id):
        if not isinstance(credential_id, str) or credential_id not in self._credentials:
            raise PoolError('Credential not found.', 404)
        return self._credentials[credential_id]

    def _eligible(self, entry, *, probe=False):
        return (entry.enabled and (probe or not entry.paused)
                and (probe or entry.cooldown_until <= self._clock())
                and (entry.expires_at is None or entry.expires_at > self._clock() or bool(entry.tokens.get('refresh_token'))))

    def _ensure_fresh(self, credential_id, *, probe=False):
        with self._lock:
            entry = self._get(credential_id)
            refresh_lock = entry.refresh_lock
        with refresh_lock:
            with self._lock:
                entry = self._get(credential_id)
                if not self._eligible(entry, probe=probe):
                    raise PoolError('Credential is unavailable.')
                if entry.expires_at is None or entry.expires_at > self._clock() + REFRESH_SKEW or not entry.tokens.get('refresh_token'):
                    return
                tokens, revision = dict(entry.tokens), entry.revision
            try:
                from openai_oauth import OpenAIOAuth, CLIENT_ID, _token_email, validate_oauth_tokens
                request = self._token_request or OpenAIOAuth(excel_upstream.ExcelSessionStore())._token_request
                payload = request({'grant_type': 'refresh_token', 'client_id': CLIENT_ID, 'refresh_token': tokens['refresh_token']})
                fresh = validate_oauth_tokens(payload, previous_tokens=tokens)
                fresh, headers = self._normalize_oauth(fresh)
            except Exception:
                with self._lock:
                    current = self._credentials.get(credential_id)
                    if current and current.revision == revision:
                        with self._transaction():
                            self._credentials[credential_id] = replace(current, error='OAuth refresh failed. Retry or sign in again.',
                                cooldown_until=self._clock() + 60)
                raise PoolError('OAuth refresh failed. Retry or sign in again.') from None
            with self._lock:
                current = self._get(credential_id)
                if current.tokens != tokens:
                    # A newer explicit login wins; never overwrite its identity or tokens.
                    raise PoolError('Credential changed during refresh. Retry.')
                with self._transaction():
                    # Preserve a concurrent disable/pause/label edit, but retain the
                    # rotated refresh token so re-enabling does not require login.
                    error = '' if current.error == 'OAuth refresh failed. Retry or sign in again.' else current.error
                    email = _token_email(fresh, current.email)
                    label = current.label if current.label_is_custom else email or current.label
                    refreshed = replace(current, headers=headers, tokens=fresh,
                        email=email, label=label,
                        expires_at=fresh['expires_at'], error=error, revision=current.revision + 1)
                    self._credentials[credential_id] = self._sanitize_metadata(refreshed)

    def acquire(self, session_key: str, stream: bool, credential_id=None, exclude_ids=None):
        """Pick an eligible credential; explicit IDs are exact internal probe targets.

        switched stays true for every subsequent acquire after a session rebind.
        A probe may bypass auth/cooldown suspension, never an explicit disable.
        No network call is retried more than once per account in this acquire.
        """
        if not isinstance(session_key, str) or not session_key or len(session_key) > 8192:
            raise PoolError('A nonempty, stable session key is required.', 400)
        if type(stream) is not bool:
            raise PoolError('stream must be a boolean.', 400)
        if exclude_ids is not None and (not isinstance(exclude_ids, (list, tuple, set, frozenset)) or any(not isinstance(i, str) for i in exclude_ids)):
            raise PoolError('exclude_ids must be a collection of credential IDs.', 400)
        excluded = set(exclude_ids or ())
        key = hashlib.sha256(session_key.encode()).hexdigest()
        for _ in range(self.max_accounts):
            with self._lock:
                self._require_loaded()
                pin = self._sticky.get(key)
                if credential_id is not None:
                    entry = self._get(credential_id)
                    candidates = [entry] if entry.id not in excluded and self._eligible(entry, probe=True) else []
                else:
                    candidates = [c for c in self._credentials.values() if c.id not in excluded and self._eligible(c)]
                if not candidates:
                    raise PoolError('No enabled, eligible BPS credentials are available.')
                selected = next((c for c in candidates if pin and c.id == pin['credential_id']), None)
                if selected is None:
                    selected = candidates[self._cursor % len(candidates)]
                    advance = credential_id is None
                else:
                    advance = False
                rebound = (pin['rebound'] or pin['credential_id'] != selected.id) if pin else self._evicted
                new_pin = {'credential_id': selected.id, 'rebound': rebound}
                with self._transaction():
                    if advance:
                        self._cursor += 1
                    if pin is None and len(self._sticky) >= self.max_sticky_sessions:
                        self._sticky.popitem(last=False)
                        self._evicted = True
                        new_pin['rebound'] = True
                    self._sticky[key] = new_pin
                    self._sticky.move_to_end(key)
                selected_id = selected.id
            try:
                self._ensure_fresh(selected_id, probe=credential_id is not None)
            except PoolError:
                excluded.add(selected_id)
                continue
            with self._lock:
                current = self._credentials.get(selected_id)
                pin = self._sticky.get(key)
                if current is None or not self._eligible(current, probe=credential_id is not None):
                    excluded.add(selected_id)
                    continue
                # Another request for this key may have rebound during refresh.
                if pin is None or pin['credential_id'] != selected_id:
                    continue
                headers = dict(current.headers)
                headers.update({'accept': 'text/event-stream' if stream else 'application/json',
                    'accept-encoding': 'identity', 'content-type': 'application/json', 'origin': 'https://bps.openai.com'})
                return CredentialSelection(selected_id, headers, current.tools_version_id, pin['rebound'], current.revision)
        raise PoolError('No enabled, eligible BPS credentials are available.')

    def _refresh_legacy(self, tokens):
        """Share per-account refresh locking with the backwards-compatible store."""
        with self._lock:
            self._require_loaded()
            entry = next((c for c in self._credentials.values() if c.account_id == tokens.get('account_id')), None)
            if not entry:
                raise PoolError('Legacy credential is not in the pool. Sign in explicitly.', 410)
            cid = entry.id
        self._ensure_fresh(cid)
        with self._lock:
            entry = self._get(cid)
            if not self._eligible(entry):
                raise PoolError('Legacy credential is unavailable.')
            return dict(entry.tokens)

    def record_result(self, credential_id, status_code, retry_after=None, expected_revision: int | None = None):
        """Ignore late results for credentials replaced or edited since acquisition."""
        if type(status_code) is not int or not 100 <= status_code <= 599:
            raise PoolError('Invalid upstream status code.', 400)
        with self._lock:
            self._require_loaded()
            entry = self._credentials.get(credential_id)
            if entry is None or (expected_revision is not None and expected_revision != entry.revision):
                return
            changes = {}
            if status_code in {401, 403}:
                changes = {'bps_verified': False, 'paused': True, 'error': 'BPS rejected this credential. Sign in or explicitly enable it again.'}
            elif status_code == 429 or status_code >= 500:
                delay = 60 if status_code == 429 else 15
                if retry_after is not None and status_code == 429:
                    try:
                        delay = _finite(retry_after)
                    except (ValueError, TypeError, OverflowError):
                        try:
                            delay = parsedate_to_datetime(str(retry_after)).timestamp() - self._clock()
                        except (ValueError, TypeError, OverflowError, AttributeError):
                            pass
                delay = min(max(delay, 1), 86400)
                changes = {'cooldown_until': self._clock() + delay, 'error': 'BPS temporarily unavailable; retry after cooldown.'}
            # A 200 response may still contain an inference error. Verification
            # is ONLY recorded by record_probe after the parent validates output.
            if changes:
                with self._transaction():
                    self._credentials[entry.id] = replace(entry, **changes)

    def record_probe(self, credential_id, ok, message, expected_revision: int | None = None):
        """Apply verification only to the selected revision, unless omitted."""
        if type(ok) is not bool:
            raise PoolError('Probe result must be a boolean.', 400)
        with self._lock:
            self._require_loaded()
            entry = self._credentials.get(credential_id)
            if entry is None or (expected_revision is not None and expected_revision != entry.revision):
                return
            # Discard untrusted upstream messages rather than leak echoed tokens.
            changes = {'bps_verified': ok, 'error': '' if ok else 'BPS access verification failed.'}
            if ok:
                changes.update(paused=False, cooldown_until=0)
            # Failed probes do not classify failures: record_result owns auth
            # suspension and Retry-After/network cooldowns. Preserve both.
            with self._transaction():
                self._credentials[entry.id] = replace(entry, **changes)


BPSCredentialPool = CredentialPool
credential_pool = CredentialPool(_default_storage=True)
