"""Persistent, explicit outbound proxy override for BPS and OAuth traffic.

Disabled preserves existing environment-based HTTPX behavior. Enabled never
silently retries directly; proxy credentials in URLs are intentionally refused.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from urllib.parse import urlsplit, urlunsplit

from app_paths import user_config_dir

DEFAULT_URL = "http://127.0.0.1:7890"


class OutboundProxySettings:
    def __init__(self, path=None):
        self.path = path or os.path.join(user_config_dir(), "outbound-proxy.json")
        self._lock = threading.RLock()

    @staticmethod
    def validate(payload):
        if not isinstance(payload, dict):
            raise ValueError("代理配置必须是 JSON 对象。")
        enabled = payload.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("代理开关必须为布尔值。")
        value = payload.get("url", DEFAULT_URL)
        if not isinstance(value, str) or not value.strip() or len(value) > 2048:
            raise ValueError("请输入有效的代理地址。")
        value = value.strip()
        if any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("代理地址不能包含空格或控制字符。")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise ValueError("代理地址或端口无效。") from None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("请使用 http:// 或 https:// 代理地址。")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("不支持在代理地址中保存用户名或密码。")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or chr(92) in value:
            raise ValueError("代理地址不能包含路径、查询参数或片段。")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("代理端口必须在 1–65535 之间。")
        own_port = int(os.environ.get("GHCP_PORT", "8001"))
        if parsed.hostname.lower() in {"127.0.0.1", "localhost", "::1", "0.0.0.0"} and port in {8000, 8001, own_port}:
            raise ValueError("代理地址不能指向本反代服务，避免请求循环。")
        return {"enabled": enabled, "url": urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))}

    def load(self):
        with self._lock:
            try:
                with open(self.path, encoding="utf-8") as handle:
                    raw = json.load(handle)
            except FileNotFoundError:
                return {"enabled": False, "url": DEFAULT_URL}
            except (OSError, ValueError):
                raise ValueError("无法读取代理配置；请在设置中重新保存，未绕过代理。") from None
            return self.validate(raw)

    def save(self, payload):
        with self._lock:
            value = self.validate(payload)
            parent = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(parent, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".outbound-proxy-", suffix=".tmp", dir=parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(value, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.remove(temporary)
            return dict(value)


settings = OutboundProxySettings()


def httpx_client_kwargs(value=None):
    current = settings.load() if value is None else OutboundProxySettings.validate(value)
    return {"proxy": current["url"], "trust_env": False} if current["enabled"] else {}


def configuration_key():
    current = settings.load()
    return (current["enabled"], current["url"])
