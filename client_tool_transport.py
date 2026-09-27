"""Pure, bounded client-tool decoding; no dispatch, I/O or evaluation.

The caller must validate the returned tool name and arguments against its
catalog/schema. Only code (not arguments strings) permits backslash repair.
"""
import json
import math
from collections.abc import Collection

__all__ = ["decode_transport_code", "decode_transport_envelope", "diagnose_transport_envelope"]
DEFAULT_TRANSPORT_NAMES = frozenset({"run_officejs", "functions.run_officejs"})
MAX_TEXT_LENGTH = 1_048_576
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 100_000
MAX_NESTED_TRANSPORTS = 2  # Extra wrappers inside the native transport.
_JSON_ESCAPE_CHARS = frozenset(r'"\/bfnrt')
_CALL_KEYS = frozenset({"name", "namespace", "arguments", "input", "type", "id", "call_id", "status"})
# Native replay metadata is not part of the inner client-call grammar.
_AMBIGUOUS_CALL_KEYS = frozenset({"tool_calls", "calls", "another_call"})
_TRANSPORT_ARGUMENT_KEYS = frozenset({
    "code", "summary", "extended_summary", "destructive", "references"
})


def _bounded_json(value: object) -> bool:
    """Iterative JSON validation: finite values, depth/size bounds, no cycles."""
    stack = [(value, 0, False)]
    active = set()
    nodes = text_length = 0
    while stack:
        item, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(item))
            continue
        nodes += 1
        if nodes > MAX_JSON_NODES:
            return False
        kind = type(item)
        if kind is str:
            text_length += len(item)
        elif kind is dict or kind is list:
            if depth >= MAX_JSON_DEPTH or id(item) in active:
                return False
            if len(item) > MAX_JSON_NODES - nodes - len(stack):
                return False
            active.add(id(item))
            stack.append((item, depth, True))
            if kind is dict:
                for key, child in item.items():
                    if type(key) is not str:
                        return False
                    text_length += len(key)
                    stack.append((child, depth + 1, False))
            else:
                stack.extend((child, depth + 1, False) for child in item)
        elif kind is float:
            if not math.isfinite(item):
                return False
        elif kind not in (int, bool, type(None)):
            return False
        if text_length > MAX_TEXT_LENGTH:
            return False
    return True


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError("Non-finite JSON constant")


def _text_depth_ok(text: str) -> bool:
    depth = 0
    in_string = escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == chr(92):
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return False
        elif character in "]}":
            depth -= 1
    return True


def _repair_invalid_json_backslashes(text: str) -> str:
    """Normalize invalid escapes and literal LF/CR/TAB, preserving decoded bytes."""
    repaired = []
    in_string = False
    index = 0
    while index < len(text):
        character = text[index]
        if not in_string:
            repaired.append(character)
            if character == '"':
                in_string = True
            index += 1
            continue
        if character == '"':
            repaired.append(character)
            in_string = False
            index += 1
            continue
        if character != chr(92):
            if character in (chr(9), chr(10), chr(13)):
                repaired.append(json.dumps(character)[1:-1])
            else:
                repaired.append(character)
            index += 1
            continue
        following = text[index + 1] if index + 1 < len(text) else ""
        if following in (chr(9), chr(10), chr(13)):
            # A literal control used as the escape character must not turn
            # into an extra value backslash. Preserve preceding slash pairs.
            repaired.extend((character, {chr(9): "t", chr(10): "n", chr(13): "r"}[following]))
            index += 2
            continue
        valid = following in _JSON_ESCAPE_CHARS
        if following == "u":
            valid = index + 5 < len(text) and all(
                digit in "0123456789abcdefABCDEF" for digit in text[index + 2:index + 6]
            )
        if valid:
            repaired.extend((character, following))
            index += 2
        else:
            repaired.extend((character, character))
            index += 1
    return "".join(repaired)


def _decode_object(value: object, *, repair: bool = False, fence: bool = False) -> dict | None:
    if type(value) is dict:
        return value if _bounded_json(value) else None
    if type(value) is not str or len(value) > MAX_TEXT_LENGTH:
        return None
    text = value.strip()
    if fence and text.startswith("```"):
        lines = text.splitlines(keepends=True)
        if len(lines) < 3 or lines[0].strip() != "```json" or lines[-1].strip() != "```":
            return None
        text = "".join(lines[1:-1]).strip()
    if not text.startswith("{") or not text.endswith("}") or not _text_depth_ok(text):
        return None
    for attempt in range(2 if repair else 1):
        try:
            decoded = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except json.JSONDecodeError:
            if attempt or not repair:
                return None
            fixed = _repair_invalid_json_backslashes(text)
            if fixed == text or len(fixed) > MAX_TEXT_LENGTH or not _text_depth_ok(fixed):
                return None
            text = fixed
            continue
        except (ValueError, RecursionError):
            return None
        return decoded if type(decoded) is dict and _bounded_json(decoded) else None
    return None


def decode_transport_code(code: object) -> dict | None:
    """Decode one dict, whole JSON object text, or an explicit json fence.

    Repair invalid JSON backslashes and literal LF/CR/TAB inside strings. Do not reinterpret
    valid escapes as path separators. Reject scripts/assignments, duplicate
    keys, arrays, trailing text and concatenated objects; never mine code.
    This decodes JSON only; decode_transport_envelope validates call structure.
    """
    return _decode_object(code, repair=True, fence=True)


def _decode_envelope(native: object, transport_names: Collection[str], details=None) -> tuple[dict | None, str | None]:
    if type(transport_names) not in (set, frozenset, list, tuple):
        return None, "invalid_transport_names"
    if not 0 < len(transport_names) <= 32 or any(
        type(name) is not str or not name or len(name) > 256 for name in transport_names
    ):
        return None, "invalid_transport_names"
    names = frozenset(transport_names)
    if type(native) is not dict or not _bounded_json(native):
        return None, "invalid_native_object"
    if native.get("type") != "function_call":
        return None, "invalid_native_type"
    if native.keys() & _AMBIGUOUS_CALL_KEYS:
        return None, "ambiguous_native_calls"
    current = native
    for depth in range(MAX_NESTED_TRANSPORTS + 2):
        name = current.get("name")
        if type(name) is not str or not name or name != name.strip():
            return None, "missing_or_invalid_tool_name"
        if "namespace" in current and not (depth == 0 and current["namespace"] is None):
            namespace = current["namespace"]
            if type(namespace) is not str or not namespace or namespace != namespace.strip():
                return None, "invalid_namespace"
        # Upstream-native metadata is opaque: only the inner requested call is
        # subject to our client-envelope whitelist. Never execute metadata.
        if depth > 0 and current.keys() - _CALL_KEYS:
            return None, "unexpected_client_envelope_fields"
        if name not in names:
            if depth == 0:
                return None, "not_a_transport_call"
            has_arguments = "arguments" in current
            has_input = "input" in current
            if has_arguments == has_input:
                return None, "ambiguous_or_missing_client_payload"
            result = dict(current)
            if has_input:
                if type(current["input"]) is not str:
                    return None, "invalid_custom_input"
                return result, None
            arguments = _decode_object(current["arguments"])
            if arguments is None:
                return None, "invalid_client_arguments_json"
            result["arguments"] = arguments
            return result, None
        if depth > MAX_NESTED_TRANSPORTS:
            return None, "nested_transport_limit"
        if "input" in current:
            return None, "transport_has_custom_input"
        arguments = _decode_object(current.get("arguments"))
        if arguments is None:
            return None, "invalid_transport_arguments_json"
        if "code" not in arguments:
            return None, "missing_transport_code"
        if arguments.keys() - _TRANSPORT_ARGUMENT_KEYS:
            return None, "unexpected_transport_argument_fields"
        current = decode_transport_code(arguments["code"])
        if current is None:
            if details is not None:
                details.update(_json_failure_details(arguments["code"], repair=True, fence=True))
            return None, "invalid_transport_code_json"
    return None, "nested_transport_limit"


def decode_transport_envelope(
    native: object, transport_names: Collection[str] = DEFAULT_TRANSPORT_NAMES
) -> dict | None:
    """Decode a single bounded call without I/O or mutations.

    Opaque native metadata is ignored; inner call fields stay strict. Function
    arguments become a dict; custom input remains the exact original string.
    Catalog/schema validation is the caller's responsibility.
    """
    return _decode_envelope(native, transport_names)[0]


def diagnose_transport_envelope(
    native: object, transport_names: Collection[str] = DEFAULT_TRANSPORT_NAMES
) -> str | None:
    """Return a fixed failure category, never input values or exception text."""
    return _decode_envelope(native, transport_names)[1]

def _json_failure_details(value, *, repair=False, fence=False):
    if type(value) is not str:
        return {'json_error': 'non_text_or_bounds'}
    if len(value) > MAX_TEXT_LENGTH:
        return {'json_error': 'text_limit', 'text_length': len(value)}
    text=value.strip()
    if fence and text.startswith('```'):
        lines=text.splitlines(keepends=True)
        if len(lines)<3 or lines[0].strip()!='```json' or lines[-1].strip()!='```':
            return {'json_error':'invalid_json_fence','text_length':len(text)}
        text=''.join(lines[1:-1]).strip()
    if not text.startswith('{'):
        return {'json_error':'not_object_text','text_length':len(text)}
    if not _text_depth_ok(text):
        return {'json_error':'depth_limit','text_length':len(text)}
    if repair:
        text=_repair_invalid_json_backslashes(text)
    if len(text)>MAX_TEXT_LENGTH:
        return {'json_error':'text_limit','text_length':len(text)}
    try:
        json.loads(text,object_pairs_hook=_unique_object,parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        kinds=(('Unterminated string','unterminated_string'),('Invalid control character','control_character'),('Extra data','trailing_data'),('Expecting property name','property_name'),('Expecting value','expected_value'),('Expecting','missing_delimiter'),('Invalid','invalid_escape'))
        kind=next((v for prefix,v in kinds if exc.msg.startswith(prefix)),'invalid_json')
        return {'json_error':kind,'text_length':len(text),'line':exc.lineno,'column':exc.colno,'offset':exc.pos}
    except (ValueError,RecursionError):
        return {'json_error':'duplicate_nonfinite_or_depth','text_length':len(text)}
    return {'json_error':'non_object_or_bounds','text_length':len(text)}


def diagnose_transport_envelope_details(native, transport_names=DEFAULT_TRANSPORT_NAMES):
    """Fixed categories and numeric locations only; never return input fragments."""
    details={}
    _,reason=_decode_envelope(native,transport_names,details)
    if reason is not None:
        details['phase']=reason
    return details
