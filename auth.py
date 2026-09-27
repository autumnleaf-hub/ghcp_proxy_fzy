"""GitHub OAuth device flow, token management, and API key handling."""

import json
import os
import sys
import time
from datetime import datetime, timezone
from threading import Lock, Thread

import httpx

from constants import (
    GITHUB_CLIENT_ID, GITHUB_DEVICE_CODE_URL, GITHUB_ACCESS_TOKEN_URL,
    GITHUB_API_KEY_URL,
    TOKEN_DIR, ACCESS_TOKEN_FILE, API_KEY_FILE,
    GITHUB_COPILOT_API_BASE,
)

_DNS_LOOKUP_ERROR_FRAGMENTS = (
    "nodename nor servname provided, or not known",
    "name or service not known",
    "temporary failure in name resolution",
    "failed to resolve",
    "resolving timed out",
)


_AUTH_FLOW_LOCK = Lock()
_AUTH_FLOW_STATE: dict[str, object] = {
    "state": "idle",
    "flow_id": None,
    "started_at": None,
    "expires_at": None,
    "poll_interval_seconds": None,
    "verification_uri": None,
    "verification_uri_complete": None,
    "user_code": None,
    "error": None,
    "warning": "",
    "message": "",
}


def _gh_headers(access_token: str = None) -> dict:
    h = {
        "accept": "application/json",
        "editor-version": "vscode/1.85.1",
        "editor-plugin-version": "copilot/1.155.0",
        "user-agent": "GithubCopilot/1.155.0",
        "accept-encoding": "gzip,deflate,br",
        "content-type": "application/json",
    }
    if access_token:
        h["authorization"] = f"token {access_token}"
    return h


def load_access_token() -> str | None:
    return None


def _save_access_token(token: str):
    raise RuntimeError("Copilot is disabled in this BPS-only build.")


def load_api_key() -> str | None:
    return None


def load_api_key_payload() -> dict:
    return {}


def get_api_base() -> str:
    raise RuntimeError("Copilot is disabled in this BPS-only build.")


def _utc_timestamp() -> float:
    return datetime.now(timezone.utc).timestamp()


def _iso_timestamp(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _device_flow_info() -> dict:
    with httpx.Client() as c:
        r = c.post(
            GITHUB_DEVICE_CODE_URL,
            headers=_gh_headers(),
            json={"client_id": GITHUB_CLIENT_ID, "scope": "read:user read:org"},
        )
        r.raise_for_status()
        info = r.json()
    if not isinstance(info, dict):
        raise RuntimeError("Device flow failed — invalid GitHub device code response.")
    return info


def _poll_for_access_token(
    device_code: str,
    *,
    interval: int,
    expires_in: int,
    interactive: bool = False,
) -> str:
    raise RuntimeError("Copilot is disabled in this BPS-only build.")


def _device_flow() -> str:
    raise RuntimeError("Copilot is disabled in this BPS-only build.")


def _refresh_api_key(access_token: str) -> str:
    raise RuntimeError("Copilot is disabled in this BPS-only build.")


def _authenticated_snapshot(*, message: str | None = None, warning: str | None = None) -> dict:
    api_key = load_api_key()
    access_token = load_access_token()
    result = {
        "authenticated": bool(api_key or access_token),
        "state": "authenticated",
        "message": message or "GitHub Copilot is authenticated.",
        "error": "",
        "warning": warning if warning is not None else str(_AUTH_FLOW_STATE.get("warning") or ""),
        "has_access_token": bool(access_token),
        "has_api_key": bool(api_key),
        "verification_uri": None,
        "verification_uri_complete": None,
        "user_code": None,
        "started_at": None,
        "expires_at": None,
        "poll_interval_seconds": None,
    }
    return result


def _flow_snapshot_unlocked() -> dict:
    if load_api_key() or load_access_token():
        return _authenticated_snapshot()

    expires_at = _AUTH_FLOW_STATE.get("expires_at")
    state = str(_AUTH_FLOW_STATE.get("state") or "idle")
    error = str(_AUTH_FLOW_STATE.get("error") or "")

    if state in {"starting", "pending"} and isinstance(expires_at, (int, float)) and expires_at <= _utc_timestamp():
        state = "error"
        error = error or "Authorization timed out before completion."
        _AUTH_FLOW_STATE.update(
            {
                "state": state,
                "error": error,
                "warning": "",
                "message": "Start sign-in again to request a new GitHub device code.",
                "verification_uri": None,
                "verification_uri_complete": None,
                "user_code": None,
                "started_at": None,
                "expires_at": None,
                "poll_interval_seconds": None,
                "flow_id": None,
            }
        )

    state = str(_AUTH_FLOW_STATE.get("state") or "idle")
    if state == "starting":
        message = str(_AUTH_FLOW_STATE.get("message") or "Requesting GitHub device code...")
    elif state == "pending":
        message = str(_AUTH_FLOW_STATE.get("message") or "Authorize the device code in GitHub to finish setup.")
    elif state == "error":
        message = str(_AUTH_FLOW_STATE.get("message") or "GitHub sign-in is not complete.")
    else:
        message = "GitHub Copilot is not authenticated yet."

    return {
        "authenticated": False,
        "state": state if state in {"starting", "pending", "error"} else "unauthenticated",
        "message": message,
        "error": str(_AUTH_FLOW_STATE.get("error") or ""),
        "warning": str(_AUTH_FLOW_STATE.get("warning") or ""),
        "has_access_token": False,
        "has_api_key": False,
        "verification_uri": _AUTH_FLOW_STATE.get("verification_uri"),
        "verification_uri_complete": _AUTH_FLOW_STATE.get("verification_uri_complete"),
        "user_code": _AUTH_FLOW_STATE.get("user_code"),
        "started_at": _iso_timestamp(_AUTH_FLOW_STATE.get("started_at")),
        "expires_at": _iso_timestamp(_AUTH_FLOW_STATE.get("expires_at")),
        "poll_interval_seconds": _AUTH_FLOW_STATE.get("poll_interval_seconds"),
    }


def auth_status() -> dict:
    return {"state": "disabled", "status": "disabled", "authenticated": False,
            "enabled": False, "message": "Copilot 已禁用，请使用 ChatGPT / BPS 凭证。"}


def _complete_browser_auth_flow(
    flow_id: str,
    *,
    device_code: str,
    interval: int,
    expires_in: int,
):
    raise RuntimeError("Copilot is disabled in this BPS-only build.")


def begin_device_flow() -> dict:
    raise RuntimeError("Copilot is disabled in this BPS-only build.")


def _friendly_auth_failure_message(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return (
            "GitHub authentication timed out before GitHub responded. "
            "Check your internet, VPN, or firewall, then try `curl -I https://github.com` "
            "and run `start-ghproxy` again."
        )

    if isinstance(exc, httpx.RequestError):
        detail = str(exc).strip() or exc.__class__.__name__
        lower_detail = detail.lower()
        if any(fragment in lower_detail for fragment in _DNS_LOOKUP_ERROR_FRAGMENTS):
            return (
                "GitHub authentication could not start because this computer could not look up "
                "GitHub's network name. Check your internet or VPN, try "
                "`curl -I https://github.com`, then run `start-ghproxy` again."
            )

        request = getattr(exc, "request", None)
        if request is not None:
            return (
                f"GitHub authentication could not reach {request.url}. "
                "Check your internet, VPN, or firewall, then run `start-ghproxy` again."
            )

    return str(exc)


def get_api_key(*, interactive: bool = False) -> str:
    raise RuntimeError("Copilot is disabled in this BPS-only build.")


def ensure_authenticated():
    raise RuntimeError("Copilot is disabled in this BPS-only build.")
