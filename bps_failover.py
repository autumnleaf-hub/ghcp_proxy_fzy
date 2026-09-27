"""Pure cross-account replay guards; no I/O, credentials, retries or account state.

Integration: call prepare_failover_body on the ORIGINAL, locally materialized
transcript, before account-specific image upload. The caller owns stickiness,
at most three attempts, reupload, and the decision that no output has escaped.
Never call should_failover on generated tool output or after delivering output.
"""
from __future__ import annotations

import base64
import binascii
from copy import deepcopy
import math
import re
from urllib.parse import unquote, urlsplit

__all__ = [
    'FailoverReplayError', 'prepare_failover_body',
    'should_failover', 'parse_retry_after',
]


class FailoverReplayError(ValueError):
    """Replay requires local history or original attachment data not supplied."""


# Matches constants.FAKE_COMPACTION_PREFIX without importing runtime modules.
_LOCAL_COMPACTION_PREFIX = 'ghcp_proxy_summary_v1:'
_RETRY_STATUSES = frozenset({401, 403, 408, 429, 500, 502, 503, 504})
_RETRY_CODES = frozenset({
    'invalid_api_key', 'invalid_token', 'expired_token', 'token_expired',
    'invalid_credentials', 'authentication_error', 'authentication_failed',
    'unauthorized', 'unauthenticated', 'forbidden',
    'insufficient_quota', 'quota_exceeded', 'quota_exhausted',
    'usage_limit_reached', 'billing_hard_limit_reached', 'insufficient_credits',
    'credits_exhausted', 'credit_balance_exhausted',
    'rate_limit_exceeded', 'rate_limit_error', 'too_many_requests',
    'server_error', 'internal_server_error', 'overloaded_error',
})
_STOP_CODES = frozenset({
    'client_cancel', 'client_cancelled', 'client_canceled', 'cancelled', 'canceled',
    'request_cancelled', 'request_canceled', 'abort_error',
    'tool_conversion_rejected', 'tool_conversion_error', 'invalid_tool_schema',
    'malformed_request', 'invalid_json', 'invalid_payload', 'validation_error',
    'context_length_exceeded', 'content_policy_violation',
})
_BAD_REQUEST_CODES = frozenset({'invalid_request', 'invalid_request_error', 'bad_request'})
_ROLES = frozenset({'system', 'developer', 'user', 'assistant', 'tool', 'function'})


def _visible_text(value, allowed_types):
    """Read only the protocol's text slots, not nested arbitrary dictionaries."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    result = []
    for part in value:
        if isinstance(part, str) and part.strip():
            result.append(part)
        elif (isinstance(part, dict) and isinstance(part.get('type'), str)
              and part['type'] in allowed_types):
            text = part.get('text')
            if isinstance(text, str) and text.strip():
                result.append(text)
    return result


def _reasoning(item):
    # Build a fresh item: deepcopy preserves aliases, which may also occur in
    # metadata/tool arguments. Mutating an aliased protocol dict would harm them.
    result = {key: value for key, value in item.items() if key != 'encrypted_content'}
    summary = _visible_text(item.get('summary'), {'summary_text'})
    content = _visible_text(item.get('content'), {'reasoning_text', 'text'})
    if not summary and not content:
        return None
    if not summary:
        result['summary'] = [{'type': 'summary_text', 'text': text} for text in content]
    elif not isinstance(item.get('summary'), list) or any(
        not isinstance(part, dict) or part.get('type') != 'summary_text'
        for part in item['summary']
    ):
        result['summary'] = [{'type': 'summary_text', 'text': text} for text in summary]
    return result


def _compaction(item, path):
    texts = _visible_text(item.get('summary'), {'summary_text', 'text', 'input_text'})
    texts += _visible_text(item.get('content'), {'text', 'input_text', 'output_text'})
    encrypted = item.get('encrypted_content')
    if isinstance(encrypted, str) and encrypted.startswith(_LOCAL_COMPACTION_PREFIX):
        try:
            decoded = base64.b64decode(
                encrypted[len(_LOCAL_COMPACTION_PREFIX):], altchars=b'-_', validate=True,
            ).decode('utf-8')
        except (ValueError, UnicodeError, binascii.Error):
            decoded = ''
        if decoded.strip() and decoded not in texts:
            texts.append(decoded)
    if not texts:
        raise FailoverReplayError(
            f'{path}: opaque compaction has no recoverable visible context; '
            'supply the complete local transcript before switching accounts'
        )
    result = {
        'type': 'message', 'role': 'user',
        'content': [{'type': 'input_text', 'text': text} for text in texts],
    }
    if 'id' in item:
        result['id'] = item['id']
    return result


def _check_url(value, path):
    if isinstance(value, dict):
        value = value.get('url')
    if not isinstance(value, str) or not value.strip():
        raise FailoverReplayError(f'{path}: original image data or a public URL is required')
    value = value.strip()
    if value.lower().startswith('data:image/'):
        return
    try:
        url = urlsplit(value)
        host = (url.hostname or '').lower().rstrip('.')
        provider_host = any(
            host == domain or host.endswith('.' + domain)
            for domain in ('oaiusercontent.com', 'openai.com', 'chatgpt.com',
                           'basispoints.ai', 'basispoints.com')
        )
        provider_asset = (
            host == 'oaiusercontent.com' or host.endswith('.oaiusercontent.com')
            or host == 'bps.openai.com' or host.endswith('.bps.openai.com')
        ) or (
            provider_host and any(
                segment in unquote(url.path).lower()
                for segment in ('/files', '/assets', '/attachments', '/backend-api/')
            )
        )
        safe = (
            url.scheme in {'http', 'https'} and bool(host)
            and url.username is None and url.password is None and not provider_asset
        )
    except ValueError:
        safe = False
    if not safe:
        raise FailoverReplayError(
            f'{path}: account-scoped or nonportable image URL; supply original bytes '
            'as a data URL or an independently accessible public URL'
        )


def _check_content_part(part, path):
    if not isinstance(part, dict):
        return
    kind = part.get('type')
    if not isinstance(kind, str):
        return
    if kind == 'input_file':
        if part.get('file_id') is not None:
            raise FailoverReplayError(
                f'{path}: input_file.file_id must be materialized locally before failover'
            )
    elif kind in {'input_image', 'image_url'}:
        if part.get('file_id') is not None:
            raise FailoverReplayError(
                f'{path}: input_image.file_id belongs to the previous account; '
                'replay original image bytes before per-account upload'
            )
        if part.get('image_url') is not None:
            _check_url(part['image_url'], path + '.image_url')
        elif isinstance(part.get('image_base64'), str) and part['image_base64'].strip():
            media_type = part.get('media_type')
            if not isinstance(media_type, str) or not media_type.lower().startswith('image/'):
                raise FailoverReplayError(f'{path}: original image_base64 requires image/* media_type')
        else:
            raise FailoverReplayError(f'{path}: original image data is missing')


def _prepare_item(item, path):
    if not isinstance(item, dict):
        return item
    kind = item.get('type')
    if kind is not None and not isinstance(kind, str):
        return item
    if kind == 'reasoning':
        return _reasoning(item)
    if kind == 'compaction':
        return _compaction(item, path)
    if kind == 'item_reference':
        raise FailoverReplayError(
            f'{path}: server-only item_reference requires the complete local item'
        )
    _check_content_part(item, path)
    if kind == 'message' or (kind is None and isinstance(item.get('role'), str) and item['role'] in _ROLES):
        parts = item.get('content')
        field = 'content'
    elif kind in {'function_call_output', 'custom_tool_call_output'}:
        # Only typed protocol output arrays, never JSON strings/argument objects.
        parts = item.get('output')
        field = 'output'
    else:
        return item
    if isinstance(parts, list):
        for index, part in enumerate(parts):
            _check_content_part(part, f'{path}.{field}[{index}]')
    return item


def prepare_failover_body(body: dict) -> dict:
    """Return a detached, portable replay body without trimming visible history.

    Only actual input protocol items and message/tool-result content slots are
    interpreted. Schemas, metadata, tool arguments and text are opaque. Nonempty
    previous_response_id (also its legacy camelCase spelling), item references,
    unreadable compaction, and account-scoped attachments fail closed. A caller
    resolving server-only state must first supply the full local transcript and
    remove that reference itself; this helper cannot prove transcript completeness.
    """
    if not isinstance(body, dict):
        raise TypeError('body must be a dictionary')
    for key in ('previous_response_id', 'previousResponseId'):
        if body.get(key) not in (None, ''):
            raise FailoverReplayError(
                f'{key}: server-only history cannot cross accounts; supply the '
                'complete local transcript and remove the reference explicitly'
            )
    result = deepcopy(body)
    for key in ('input', 'messages'):
        items = result.get(key)
        if isinstance(items, list):
            prepared = []
            for index, item in enumerate(items):
                replay_item = _prepare_item(item, f'{key}[{index}]')
                if replay_item is not None or item is None:
                    prepared.append(replay_item)
            result[key] = prepared
    return result


def _failure_fields(status_code, payload):
    """Inspect failure envelopes only; never search generated content recursively."""
    if not isinstance(payload, dict):
        return set(), []
    nodes = [payload]
    response = payload.get('response')
    if isinstance(response, dict) and (
        payload.get('type') in ('response.failed', 'response.error', 'response.cancelled')
        or response.get('status') in ('failed', 'cancelled', 'canceled')
    ):
        nodes.append(response)
    codes, messages = set(), []
    for node in nodes:
        failed = (
            status_code >= 400 or node.get('status') in ('failed', 'cancelled', 'canceled')
            or node.get('success') is False or node.get('type') in (
                'error', 'response.failed', 'response.error', 'response.cancelled',
            ) or bool(node.get('error'))
        )
        if not failed:
            continue
        if node.get('status') in ('cancelled', 'canceled') or node.get('type') == 'response.cancelled':
            codes.add('cancelled')
        error = node.get('error')
        records = [node]
        if isinstance(error, dict):
            records.append(error)
        elif isinstance(error, str):
            codes.add(error.strip().lower())
            messages.append(error)
        for record in records:
            for key in ('code', 'type', 'error_code'):
                value = record.get(key)
                if isinstance(value, str):
                    codes.add(value.strip().lower())
            message = record.get('message')
            if isinstance(message, str):
                messages.append(message)
    return codes, messages


def should_failover(status_code: int, payload: dict | None = None) -> bool:
    """Classify an upstream failure, not permission to retry delivered output.

    Permanent request/tool errors and client cancellation take precedence over
    retryable HTTP statuses. Explicit quota/auth/rate/server codes in a failure
    envelope may identify a failed HTTP-200 response; message substrings cannot.
    """
    if status_code == 499:  # Client closed the request; never restart it.
        return False
    codes, messages = _failure_fields(status_code, payload)
    if codes & _STOP_CODES or any(
        message.lstrip().startswith('[tool_conversion_rejected]') for message in messages
    ):
        return False
    if codes & _RETRY_CODES:
        return True
    if codes & _BAD_REQUEST_CODES:
        return False
    return status_code in _RETRY_STATUSES


def parse_retry_after(value) -> float | None:
    """Parse finite, nonnegative delay seconds (including fractional values).

    HTTP dates return None: converting an absolute date needs a caller-owned
    clock and would make this one-argument helper nondeterministic. No sleeping
    or cooldown policy is performed here.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value):
            return None
    try:
        seconds = float(value)
    except (ValueError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None
