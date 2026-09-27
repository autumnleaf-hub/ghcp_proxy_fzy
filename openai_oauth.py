"""Experimental CPA-compatible ChatGPT OAuth for the BPS adapter.

OAuth parameters follow CLIProxyAPI internal/auth/codex/openai_auth.go:
https://github.com/router-for-me/CLIProxyAPI/blob/main/internal/auth/codex/openai_auth.go
A successful login does NOT establish that BPS accepts this token audience.
Only the explicit BPS probe or a successful inference can establish that.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import os
import secrets
import tempfile
import threading
import time
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

import excel_upstream
from app_paths import user_state_dir

AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
LOGIN_TTL = 600


def _jwt_claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, UnicodeError):
        return {}


def _display_email(value) -> str:
    """Conservative display metadata only; never an authorization identifier."""
    if not isinstance(value, str) or not 3 <= len(value) <= 254:
        return ""
    if not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return ""
    if value.count("@") != 1:
        return ""
    local, domain = value.split("@")
    if not 1 <= len(local) <= 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local):
        return ""
    if "." not in domain or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part) for part in domain.split(".")):
        return ""
    return value


def _email_claims(token) -> dict:
    """Decode well-formed JWT metadata, not signature validity or authorization.

    Only used for already accepted credentials; never changes account binding.
    """
    if not isinstance(token, str) or len(token) > 32700:
        return {}
    parts = token.split(".")
    if len(parts) != 3 or any(not re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
        return {}
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate claim")
            result[key] = value
        return result
    def invalid_constant(_value):
        raise ValueError("invalid JSON constant")

    try:
        header, claims = [json.loads(
            base64.b64decode(part + "=" * (-len(part) % 4), altchars=b"-_", validate=True).decode("utf-8"),
            object_pairs_hook=unique_object, parse_constant=invalid_constant) for part in parts[:2]]
        if not isinstance(header, dict) or not isinstance(claims, dict):
            return {}
        if not isinstance(header.get("alg"), str) or not header["alg"] or header["alg"].lower() == "none":
            return {}
        return claims
    except (ValueError, UnicodeError, RecursionError):
        return {}


def _token_email(tokens, fallback="") -> str:
    """Keep accepted metadata; otherwise prefer ID-token over access-token email.

    Exchange payloads must pass only token fields, never a top-level email.
    Saved metadata may originate from an ID token which we do not retain.
    """
    saved = _display_email(tokens.get("email"))
    if saved:
        return saved
    for key in ("id_token", "access_token"):
        claims = _email_claims(tokens.get(key))
        profile = claims.get("https://api.openai.com/profile")
        for value in (claims.get("email"),
                      profile.get("email") if isinstance(profile, dict) else None,
                      claims.get("https://api.openai.com/profile/email")):
            email = _display_email(value)
            if email:
                return email
    return _display_email(fallback)


def validate_oauth_tokens(payload: dict, previous_tokens: dict | None = None) -> dict:
    """Validate exchange/refresh while retaining, never changing, account identity."""
    if not isinstance(payload, dict):
        raise RuntimeError("The token endpoint returned invalid credentials.")
    access = payload.get("access_token")
    if not isinstance(access, str) or not access or len(access) > 32700:
        raise RuntimeError("The token endpoint returned no usable access token.")
    claims = _jwt_claims(access)
    identity = _jwt_claims(str(payload.get("id_token") or ""))
    previous_account = (previous_tokens or {}).get("account_id")
    account = previous_account
    for token_claims in (claims, identity):
        auth = token_claims.get("https://api.openai.com/auth")
        claimed = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
        if claimed is not None:
            if not isinstance(claimed, str) or not claimed:
                raise RuntimeError("The OAuth token has an invalid account ID.")
            if account and claimed != account:
                raise RuntimeError("OAuth refresh changed account identity. Sign in again.")
            account = claimed
    if not isinstance(account, str) or not account or len(account) > 512:
        raise RuntimeError("The OAuth token has no ChatGPT account ID. BPS session was not configured.")
    try:
        raw_expiry = claims.get("exp")
        if isinstance(raw_expiry, bool) or isinstance(payload.get("expires_in"), bool):
            raise ValueError("invalid expiry")
        expires_at = float(raw_expiry if raw_expiry is not None else time.time() + float(payload.get("expires_in", 3600)))
    except (TypeError, ValueError, OverflowError):
        raise RuntimeError("The token endpoint returned an invalid expiry.") from None
    if not math.isfinite(expires_at) or expires_at <= time.time():
        raise RuntimeError("The token endpoint returned an expired or invalid token.")
    refresh = payload.get("refresh_token") or (previous_tokens or {}).get("refresh_token")
    if refresh is not None and (not isinstance(refresh, str) or len(refresh) > 32700):
        raise RuntimeError("The token endpoint returned an invalid refresh token.")
    try:
        if any(ord(c) < 32 or ord(c) == 127 for c in access + account):
            raise ValueError("invalid credential characters")
        validator = excel_upstream.ExcelSessionStore()
        validator.configure({"authorization": "Bearer " + access, "chatgpt-account-id": account},
                            source="oauth", persist=False, allow_expired=False)
    except (ValueError, TypeError):
        raise RuntimeError("The token endpoint returned an unusable BPS session.") from None
    email = _token_email({"access_token": access, "id_token": payload.get("id_token")},
                         _token_email(previous_tokens or {}))
    return {"access_token": access, "refresh_token": refresh, "account_id": account,
            "expires_at": expires_at, "email": email}


class OpenAIOAuth:
    def __init__(self, store, persistence_file: str | None = None, *, pool=None):
        self.store = store
        self.pool = pool
        self.persistence_file = persistence_file
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._tokens: dict = {}
        self._pending: dict = {}
        self._server = None
        self._timer = None
        self._status = "idle"
        self._error = ""
        self._persistence_error = ""
        self._bps_verified = False
        self._bps_probe_status = "untested"
        self._probe_message = "尚未验证 BPS 访问权限。"

    def status(self) -> dict:
        with self._lock:
            return {
                "status": self._status,
                "error": self._error,
                "expires_at": self._tokens.get("expires_at"),
                "login_deadline": self._pending.get("deadline"),
                "has_refresh_token": bool(self._tokens.get("refresh_token")),
                "persisted": bool(self.persistence_file and os.path.isfile(self.persistence_file)),
                "persistence_error": self._persistence_error,
                "bps_verified": self._bps_verified,
                "bps_probe_status": self._bps_probe_status,
                "probe_message": self._probe_message,
                "callback_port": 1455,
            }

    def start(self) -> dict:
        self.cancel()
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        owner = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # Callback URLs contain one-time credentials; never log them.

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path != "/auth/callback":
                    self.send_error(404)
                    return
                if self.headers.get("Host", "").lower() not in {"localhost:1455", "127.0.0.1:1455"}:
                    self.send_error(400)
                    return
                ok, message = owner.complete(parse_qs(parsed.query))
                body = ("<!doctype html><meta charset=utf-8><title>BPS 登录</title>"
                        + "<h2>" + ("登录完成" if ok else "登录未完成")
                        + "</h2><p>" + message + "</p><p>请返回代理控制台，可以关闭此标签页。</p>").encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        try:
            server = ThreadingHTTPServer(("127.0.0.1", 1455), CallbackHandler)
        except OSError as exc:
            raise RuntimeError("OAuth callback port 1455 is busy. Finish or cancel the CPA/Codex login, then retry.") from exc
        server.daemon_threads = True
        with self._lock:
            self._server = server
            self._pending = {"state": state, "verifier": verifier, "deadline": time.time() + LOGIN_TTL}
            self._status = "waiting"
            self._error = ""
        threading.Thread(target=server.serve_forever, daemon=True, name="ghcp-oauth-callback").start()
        timer = threading.Timer(LOGIN_TTL, self._expire, args=(state,))
        timer.daemon = True
        with self._lock:
            self._timer = timer
        timer.start()
        params = {
            "client_id": CLIENT_ID, "response_type": "code",
            "redirect_uri": REDIRECT_URI, "scope": "openid email profile offline_access",
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            "prompt": "login", "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
        return {**self.status(), "authorization_url": AUTH_URL + "?" + urlencode(params)}

    def _stop_listener(self):
        with self._lock:
            server, self._server = self._server, None
            timer, self._timer = self._timer, None
        if timer:
            timer.cancel()
        if server:
            server.shutdown()
            server.server_close()

    def _expire(self, state):
        with self._lock:
            if self._pending.get("state") != state or self._status != "waiting":
                return
            self._pending = {}
            self._status = "error"
            self._error = "Login timed out. Start a new login."
        self._stop_listener()

    def cancel(self):
        with self._lock:
            self._pending = {}
            if self._status in {"waiting", "exchanging"}:
                self._status = "cancelled"
        self._stop_listener()

    def complete(self, query: dict) -> tuple[bool, str]:
        state = query.get("state", [""])[0]
        with self._lock:
            pending = dict(self._pending)
            if (self._status != "waiting" or not isinstance(state, str) or not state
                    or not secrets.compare_digest(state.encode(), pending.get("state", "").encode())
                    or time.time() >= pending.get("deadline", 0)):
                return False, "Invalid, expired, or already used login state. Start again from the dashboard."
            self._status = "exchanging"
        try:
            if query.get("error"):
                raise RuntimeError("The identity provider declined sign-in. Try again from the dashboard.")
            code = query.get("code", [""])[0]
            if not isinstance(code, str) or not code or len(code) > 8192:
                raise RuntimeError("The login callback did not include a valid authorization code.")
            payload = self._token_request({
                "grant_type": "authorization_code", "client_id": CLIENT_ID,
                "code": code, "redirect_uri": REDIRECT_URI, "code_verifier": pending["verifier"],
            })
            with self._lock:
                if self._pending.get("state") != state or self._status != "exchanging":
                    return False, "Login was cancelled or replaced."
                self._accept_tokens(payload)
                self._pending = {}
                self._status = "complete"
                self._error = ""
                self._bps_verified = False
                self._bps_probe_status = "untested"
                self._probe_message = "已登录，尚未验证凭证兼容性和 BPS 访问权限。"
            return True, "凭证已保存。请在控制台点击“验证 BPS 访问”以检查权限。"
        except RuntimeError as exc:
            with self._lock:
                if self._pending.get("state") == state:
                    self._pending = {}
                    self._status = "error"
                    self._error = str(exc)
            return False, str(exc)
        finally:
            with self._lock:
                replaced = bool(self._pending and self._pending.get("state") != state)
            if not replaced:
                self._stop_listener()

    def _token_request(self, data: dict) -> dict:
        kwargs = {"timeout": 30, "follow_redirects": False}
        if os.path.isfile(os.path.join(os.path.dirname(__file__), "outbound_proxy.py")):
            try:
                from outbound_proxy import httpx_client_kwargs
                kwargs.update(httpx_client_kwargs())
                kwargs["follow_redirects"] = False
            except Exception:
                raise RuntimeError("Could not configure the OAuth outbound proxy.") from None
        try:
            with httpx.Client(**kwargs) as client:
                response = client.post(TOKEN_URL, data=data, headers={"Accept": "application/json"})
            if response.status_code != 200:
                raise RuntimeError(f"OpenAI token exchange failed (HTTP {response.status_code}). Retry login; no token details were logged.")
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("invalid token payload")
            return payload
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("Unable to obtain OpenAI credentials. Check network/proxy settings and retry login.") from None

    def _accept_tokens(self, payload: dict, previous_refresh: str | None = None):
        previous = {**self._tokens, "refresh_token": previous_refresh} if previous_refresh else None
        tokens = validate_oauth_tokens(payload, previous_tokens=previous)
        self._apply(tokens)
        self._tokens = tokens
        self._save()
        if self.pool is not None:
            self.pool.load()
            self.pool.upsert_oauth(tokens, allow_create=previous_refresh is None,
                                   previous_refresh=previous_refresh)

    def _apply(self, tokens):
        self.store.configure({
            "authorization": "Bearer " + tokens["access_token"],
            "chatgpt-account-id": tokens["account_id"],
            "x-openai-account-id": tokens["account_id"],
        }, source="oauth", persist=False, allow_expired=True)

    def ensure_session(self):
        if self.store.status().get("source") != "oauth":
            return
        with self._refresh_lock, self._lock:
            if self.pool is not None:
                self.pool.load()
                tokens = self.pool._refresh_legacy(self._tokens)
                self._apply(tokens)
                self._tokens = tokens
                self._save()
                self._error = ""
                return
            if self._tokens.get("expires_at", 0) > time.time() + 60:
                return
            refresh = self._tokens.get("refresh_token")
            if not refresh:
                raise RuntimeError("OAuth session expired without a refresh token. Sign in again on the dashboard.")
            try:
                self._accept_tokens(self._token_request({
                    "grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": refresh,
                }), previous_refresh=refresh)
                self._error = ""
            except RuntimeError as exc:
                self._error = str(exc)
                raise

    def _save(self):
        if not self.persistence_file:
            return
        temporary = None
        try:
            raw = json.dumps({"version": 1, "tokens": self._tokens}).encode()
            protected = excel_upstream._protect_windows_data(raw)
            directory = os.path.dirname(self.persistence_file)
            os.makedirs(directory, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".oauth-", dir=directory)
            with os.fdopen(fd, "wb") as handle:
                handle.write(protected)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.persistence_file)
            self._persistence_error = ""
        except (OSError, RuntimeError):
            self._persistence_error = "OAuth is active in memory, but encrypted persistence failed."
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError:
                    pass

    def load(self):
        if not self.persistence_file or not os.path.isfile(self.persistence_file):
            return
        try:
            with open(self.persistence_file, "rb") as handle:
                payload = json.loads(excel_upstream._unprotect_windows_data(handle.read()))
            tokens = payload["tokens"]
            if payload.get("version") != 1 or not isinstance(tokens, dict):
                raise ValueError("unsupported credentials")
            if not all(isinstance(tokens.get(key), str) and tokens[key] for key in ("access_token", "account_id")):
                raise ValueError("incomplete credentials")
            tokens["expires_at"] = float(tokens["expires_at"])
            tokens["email"] = _token_email(tokens)
            with self._lock:
                self._apply(tokens)
                self._tokens = tokens
                self._status = "complete"
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            self._persistence_error = "Could not restore encrypted OAuth credentials. Sign in again."

    def record_probe(self, ok: bool, message: str):
        with self._lock:
            self._bps_verified = ok
            self._bps_probe_status = "passed" if ok else "failed"
            self._probe_message = message

    def clear(self):
        self.cancel()
        with self._refresh_lock, self._lock:
            self._tokens = {}
            if self.store.status().get("source") == "oauth":
                self.store.clear()
            self._status = "idle"
            self._error = ""
            self._bps_verified = False
            self._bps_probe_status = "untested"
            self._probe_message = "尚未验证 BPS 访问权限。"
            if self.persistence_file and os.path.isfile(self.persistence_file):
                os.remove(self.persistence_file)


from bps_credentials import credential_pool

login_service = OpenAIOAuth(
    excel_upstream.excel_session_store,
    os.path.join(user_state_dir(), "openai-oauth.dpapi") if sys.platform == "win32" else None,
    pool=credential_pool,
)
