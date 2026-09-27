"""Persistent, one-step model remapping with literal incoming alias groups.

Main mapping targets and discovery use only excel_upstream.MODEL_IDS. Approval
routing and Claude client defaults retain their independent legacy catalog.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unicodedata
from dataclasses import dataclass

from fastapi import HTTPException

import excel_upstream
import format_translation
from constants import DEFAULT_COMPACT_FALLBACK_MODEL, MODEL_PRICING, MODEL_ROUTING_CONFIG_FILE
from util import _normalize_model_name


MAX_ROUTING_MODEL_NAME_LENGTH = 256
# Shared across service instances: read/merge/write is one transaction in-process.
# Same-directory os.replace also keeps readers in other processes from seeing
# partial JSON. Cross-process writers are intentionally last-writer-wins.
_CONFIG_LOCK = threading.RLock()
_SETTINGS_KEYS = (
    "enabled", "mappings", "builtin_mappings", "approval_enabled", "approval_mappings",
    "claude_code_defaults",
)


def normalize_routing_model_name(model_name) -> str | None:
    """Keep legacy provider normalization for existing external callers."""
    if not isinstance(model_name, str):
        return None
    raw = model_name.strip()
    if not raw:
        return None
    normalized = "-".join(raw.lower().replace("_", "-").split())
    resolved = format_translation.resolve_copilot_model_name(normalized)
    return _normalize_model_name(resolved or normalized)


def model_provider_family(model_name: str | None) -> str | None:
    normalized = normalize_routing_model_name(model_name)
    if not normalized:
        return None
    for prefix, provider in (("claude-", "claude"), ("gpt-", "codex"),
                             ("gemini-", "gemini"), ("grok-", "grok")):
        if normalized.startswith(prefix):
            return provider
    return None


def _available_model_payloads() -> list[dict[str, str]]:
    return [
        {"model": model, "provider": "codex", "label": model.removesuffix("-excel")}
        for model in excel_upstream.MODEL_IDS
    ]


def _alias_name(value: object) -> str | None:
    """Aliases are literal names, not Copilot heuristics or pricing aliases."""
    if not isinstance(value, str):
        return None
    name = value.strip().lower()
    if not name or len(name) > MAX_ROUTING_MODEL_NAME_LENGTH:
        return None
    if any(char in ",，" or unicodedata.category(char) in {"Cc", "Cf", "Cs"}
           for char in name):
        return None
    return name


def _entry_value(entry: dict, key: str, legacy_key: str):
    # An explicitly invalid modern field must not be masked by the old field.
    return entry[key] if key in entry else entry.get(legacy_key)


@dataclass(frozen=True)
class ModelRoutingConfig:
    config_file: str = MODEL_ROUTING_CONFIG_FILE


class ModelRoutingConfigService:
    def __init__(self, config: ModelRoutingConfig):
        self._config = config
        self._available_models = _available_model_payloads()
        self._known_models = {row["model"] for row in self._available_models}
        self._target_aliases = {}
        for model in self._known_models:
            base = model.removesuffix("-excel")
            for alias in (model, base, base + "-basispoints"):
                self._target_aliases[alias] = model
        # Not used for discovery or ordinary mapping targets. These independent
        # legacy features must not change when the main routing UI is simplified.
        self._legacy_models = {
            name for name in MODEL_PRICING if model_provider_family(name) is not None
        } | self._known_models
        self._claude_code_default_slots = ("opus_model", "sonnet_model", "haiku_model")

    def _config_payload(self, current: dict) -> dict[str, object]:
        return {
            **current,
            "available_models": [dict(row) for row in self._available_models],
            "path": os.fspath(self._config.config_file),
        }

    def config_payload(self) -> dict[str, object]:
        return self._config_payload(self.load_settings())

    def load_settings(self) -> dict[str, object]:
        with _CONFIG_LOCK:
            try:
                with open(self._config.config_file, encoding="utf-8") as stream:
                    payload = json.load(stream)
            except FileNotFoundError:
                return self.default_settings()
            except (OSError, ValueError):
                settings = self.default_settings()
                settings["builtin_mappings"] = []
                settings["warnings"].append(
                    "Model routing configuration could not be read; routing is inactive."
                )
                return settings
            if not isinstance(payload, dict):
                settings = self.default_settings()
                settings["builtin_mappings"] = []
                settings["warnings"].append(
                    "Model routing configuration must be an object; routing is inactive."
                )
                return settings
            return self._normalize_settings_payload(payload, loading=True)

    def save_settings(self, payload: dict) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be an object.")
        with _CONFIG_LOCK:
            merged = dict(payload)
            # Partial UI saves preserve all omitted settings, including disabled
            # custom rules and intentionally empty built-in lists from older saves.
            if any(key not in merged for key in _SETTINGS_KEYS):
                current = self.load_settings()
                for key in _SETTINGS_KEYS:
                    merged.setdefault(key, current[key])
            normalized = self._normalize_settings_payload(merged)
            destination = os.path.abspath(self._config.config_file)
            directory = os.path.dirname(destination)
            os.makedirs(directory, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=directory,
                    prefix=".model-routing-", suffix=".tmp", delete=False,
                ) as stream:
                    temporary = stream.name
                    json.dump({key: normalized[key] for key in _SETTINGS_KEYS}, stream,
                              indent=2, ensure_ascii=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                if temporary is not None and os.path.exists(temporary):
                    os.unlink(temporary)
            # Return this save's snapshot, not another writer's subsequent save.
            return self._config_payload(normalized)

    @staticmethod
    def _resolve_mapping(settings: dict, requested: str) -> str | None:
        if settings["enabled"]:
            for mapping in settings["mappings"]:
                if requested in mapping["source_model"].split(","):
                    return mapping["target_model"]
        # The custom routing switch does not disable managed built-in aliases.
        for mapping in settings["builtin_mappings"]:
            if mapping["enabled"] and requested in mapping["source_model"].split(","):
                return mapping["target_model"]
        return None

    def resolve_target_model(self, requested_model: str | None) -> str | None:
        """Resolve explicit custom/built-in mappings only, without identity fallback."""
        requested = _alias_name(requested_model)
        if not requested:
            return None
        return self._resolve_mapping(self.load_settings(), requested)

    def resolve_bps_model(self, requested_model: str | None) -> str | None:
        """Resolve once: active custom, active built-in, then canonical membership.

        Ingress must use this result rather than an is_excel_model fallback:
        removed/disabled bare and basispoints aliases must not be resurrected.
        Canonical IDs remain supported directly; no provider-prefix or implicit
        bare/basispoints expansion is performed on incoming names.
        """
        requested = _alias_name(requested_model)
        if not requested:
            return None
        mapped = self._resolve_mapping(self.load_settings(), requested)
        if mapped is not None:
            return mapped
        return requested if requested in self._known_models else None

    def resolve_compact_fallback_model(self, requested_model: str | None) -> str | None:
        """Keep the legacy hook; supported BPS targets need no compact fallback."""
        normalized_requested = _alias_name(requested_model)
        if not normalized_requested:
            return None
        settings = self.load_settings()
        if not settings["enabled"]:
            return None
        for mapping in settings["mappings"]:
            if normalized_requested not in mapping["source_model"].split(","):
                continue
            if mapping.get("target_provider") == "codex":
                return None
            fallback = mapping.get("compact_fallback_model") or DEFAULT_COMPACT_FALLBACK_MODEL
            return fallback if model_provider_family(fallback) == "codex" else DEFAULT_COMPACT_FALLBACK_MODEL
        return None

    def resolve_approval_target_model(self, requested_model: str | None) -> str | None:
        normalized_requested = normalize_routing_model_name(_alias_name(requested_model))
        if not normalized_requested:
            return None
        settings = self.load_settings()
        if settings["approval_enabled"]:
            for mapping in settings["approval_mappings"]:
                if normalized_requested in mapping["source_model"].split(","):
                    return mapping["target_model"]
        return None

    def _default_builtin_mappings(self) -> list[dict]:
        return self._normalize_mapping_list([
            {"source_model": f"{base},{base}-basispoints", "target_model": model}
            for model in excel_upstream.MODEL_IDS
            for base in (model.removesuffix("-excel"),)
        ], label="Built-in mapping", builtin=True)

    def default_settings(self) -> dict[str, object]:
        return {
            "enabled": False, "mappings": [],
            "builtin_mappings": self._default_builtin_mappings(),
            "approval_enabled": False, "approval_mappings": [],
            "claude_code_defaults": {slot: "" for slot in self._claude_code_default_slots},
            "warnings": [],
        }

    def _normalize_settings_payload(self, payload: dict, *, loading=False) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be an object.")
        warnings: list[str] = []
        settings = {}
        for key in ("enabled", "approval_enabled"):
            value = payload.get(key, False)
            if not isinstance(value, bool):
                if not loading:
                    raise HTTPException(status_code=400, detail=f'{key} must be a boolean.')
                warnings.append(f"Invalid {key} flag; this routing feature is inactive.")
                value = False
            settings[key] = value
        for key, label, legacy in (("mappings", "Mapping", False),
                                   ("approval_mappings", "Approval mapping", True)):
            settings[key] = self._normalize_mapping_list(
                payload.get(key), label=label, legacy=legacy,
                warnings=warnings if loading else None,
            )
        # Only absence seeds defaults: [] means intentional deletion, and invalid
        # stored lists are warned about and inactive rather than resurrected.
        settings["builtin_mappings"] = (
            self._normalize_mapping_list(
                payload["builtin_mappings"], label="Built-in mapping", builtin=True,
                warnings=warnings if loading else None,
            ) if "builtin_mappings" in payload else self._default_builtin_mappings()
        )
        settings["claude_code_defaults"] = self._normalize_claude_code_defaults(
            payload.get("claude_code_defaults"), warnings=warnings if loading else None,
        )
        settings["warnings"] = warnings
        return settings

    def _target_model(self, value, *, legacy=False) -> str | None:
        name = _alias_name(value)
        if not name:
            return None
        if legacy:
            normalized = normalize_routing_model_name(name)
            if normalized in self._legacy_models:
                return normalized
        return self._target_aliases.get(name.removeprefix("openai/"))

    def _normalize_mapping_list(self, raw_mappings, *, label: str, legacy=False, builtin=False,
                                warnings: list[str] | None = None) -> list[dict]:
        if raw_mappings is None and not builtin:
            raw_mappings = []
        if not isinstance(raw_mappings, list):
            if warnings is not None:
                warnings.append(f"Ignored {label.lower()}s: expected a list.")
                return []
            raise HTTPException(status_code=400, detail=f'{label.lower()}s must be a list.')
        mappings = []
        seen_sources: set[str] = set()
        for index, entry in enumerate(raw_mappings, start=1):
            try:
                if not isinstance(entry, dict):
                    raise ValueError("must be an object")
                source = _entry_value(entry, "source_model", "source")
                if not isinstance(source, str):
                    raise ValueError("source_model must be a comma-separated string")
                aliases = [_alias_name(part) for part in source.replace("，", ",").split(",")]
                if not all(aliases):
                    raise ValueError(
                        f"source_model aliases must be nonempty, at most {MAX_ROUTING_MODEL_NAME_LENGTH} characters, and contain no control characters"
                    )
                if legacy:
                    aliases = [normalize_routing_model_name(alias) for alias in aliases]
                    if not all(aliases):
                        raise ValueError("source_model must include valid aliases")
                if len(set(aliases)) != len(aliases) or seen_sources.intersection(aliases):
                    raise ValueError("duplicate source_model alias after normalization")
                target = self._target_model(
                    _entry_value(entry, "target_model", "target"), legacy=legacy,
                )
                if not target:
                    raise ValueError("target_model is invalid or unsupported")
                providers = {model_provider_family(alias) for alias in aliases}
                normalized = {
                    "source_model": ",".join(aliases),
                    "source_provider": next(iter(providers)) if len(providers) == 1 else None,
                    "target_model": target,
                    "target_provider": model_provider_family(target),
                }
                if builtin:
                    enabled = entry.get("enabled", True)
                    if not isinstance(enabled, bool):
                        raise ValueError("enabled must be a boolean")
                    normalized["enabled"] = enabled
                raw_fallback = _entry_value(entry, "compact_fallback_model", "compact_fallback")
                if raw_fallback is not None and not (isinstance(raw_fallback, str) and not raw_fallback.strip()):
                    fallback = self._target_model(raw_fallback, legacy=True)
                    if not fallback or model_provider_family(fallback) != "codex":
                        raise ValueError("compact_fallback_model must be a supported GPT model")
                    normalized["compact_fallback_model"] = fallback
                seen_sources.update(aliases)
                mappings.append(normalized)
            except ValueError as exc:
                # Only static validation messages are exposed: never echo stored
                # aliases/targets, arbitrary HTML, file contents, or credentials.
                message = f"{label} #{index}: {exc}."
                if warnings is None:
                    raise HTTPException(status_code=400, detail=message) from exc
                warnings.append(f"Ignored inactive rule. {message}")
        return mappings

    def _normalize_claude_code_defaults(self, raw_defaults: object, *,
                                         warnings: list[str] | None = None) -> dict[str, str]:
        if raw_defaults is None:
            raw_defaults = {}
        if not isinstance(raw_defaults, dict):
            if warnings is None:
                raise HTTPException(status_code=400, detail='claude_code_defaults must be an object.')
            warnings.append("Ignored obsolete claude_code_defaults: expected an object.")
            raw_defaults = {}
        defaults = {}
        for slot in self._claude_code_default_slots:
            raw = raw_defaults.get(slot)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                defaults[slot] = ""
                continue
            model = self._target_model(raw, legacy=True)
            if not model:
                message = f"claude_code_defaults.{slot} is invalid or unsupported."
                if warnings is None:
                    raise HTTPException(status_code=400, detail=message)
                warnings.append(f"Ignored obsolete default. {message}")
            defaults[slot] = model or ""
        return defaults
