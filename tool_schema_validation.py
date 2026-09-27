"""Offline JSON Schema validation for tool arguments.

Integration: replace the old helper with ``matches_schema``.  Select input
schemas by key presence / None, not truthiness: boolean False is a schema.
``None`` means
no schema; empty objects and boolean schemas follow JSON Schema semantics.
Schemas without a dialect use Draft 2020-12; known explicit drafts are honored.
OpenAPI's ``nullable: true`` extends an explicit ``type`` to include null,
without bypassing other assertions (for example enum).  Formats are annotations.
Only fragment/empty references are allowed.  Even an unreachable external ref
in a schema position is rejected; the registry never retrieves any resource.

Only serialized schemas and prepared validators are cached, never instances,
validation errors, credentials, or exception messages.  The cache is bounded
by both entry count and schema size.  Diagnostic paths are tuples of property
names / array indices, not formatted exception messages or actual values.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import math
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable

__all__ = [
    'matches_schema', 'diagnose_schema', 'SchemaDiagnostic',
    'clear_schema_cache', 'schema_cache_info',
]

SCHEMA_CACHE_SIZE = 64
MAX_SCHEMA_BYTES = 262_144
MAX_SCHEMA_DEPTH = 128
MAX_SCHEMA_NODES = 20_000


@dataclass(frozen=True, slots=True)
class SchemaDiagnostic:
    """Safe, value-free failure details; empty paths refer to the root."""

    schema_path: tuple[str | int, ...]
    instance_path: tuple[str | int, ...]
    category: str


def _failure(category: str, path: tuple = ()) -> SchemaDiagnostic:
    return SchemaDiagnostic(schema_path=path, instance_path=(), category=category)


def _deny_retrieve(uri: str) -> Any:
    # Do not delegate to urllib, requests, a file loader or the default registry.
    raise NoSuchResource(ref=uri)


_OFFLINE_REGISTRY = Registry(retrieve=_deny_retrieve)
_SINGLE_SCHEMAS = (
    'additionalProperties', 'unevaluatedProperties', 'propertyNames',
    'additionalItems', 'unevaluatedItems', 'contains', 'not', 'if', 'then',
    'else', 'contentSchema',
)
_SCHEMA_MAPS = ('$defs', 'definitions', 'properties', 'patternProperties',
                'dependentSchemas')
_SCHEMA_ARRAYS = ('allOf', 'anyOf', 'oneOf', 'prefixItems')
_REF_KEYWORDS = ('$ref', '$dynamicRef', '$recursiveRef')


def _check_json_tree(schema: object) -> SchemaDiagnostic | None:
    """Bound schema processing and reject non-JSON/cyclic Python input."""
    stack = [(schema, (), 0)]
    nodes = 0
    while stack:
        node, path, depth = stack.pop()
        nodes += 1
        if depth > MAX_SCHEMA_DEPTH or nodes > MAX_SCHEMA_NODES:
            return _failure('schema_limit', path)
        if isinstance(node, dict):
            if any(not isinstance(key, str) for key in node):
                return _failure('invalid_schema', path)
            stack.extend((v, path + (k,), depth + 1) for k, v in node.items())
        elif isinstance(node, list):
            stack.extend((v, path + (i,), depth + 1) for i, v in enumerate(node))
        elif node is None or isinstance(node, (str, bool, int)):
            continue
        elif isinstance(node, float) and math.isfinite(node):
            continue
        else:
            return _failure('invalid_schema', path)
    return None


def _prepare_schema(schema: object) -> SchemaDiagnostic | None:
    """Apply nullable and reference policy only at schema positions.

    Do not interpret literal $ref/type/nullable fields in enum, const, default,
    examples, or annotations as schemas.  jsonschema implements assertions and
    referencing implements resolution; this walk only implements local policy.
    """
    stack = [(schema, ())]
    while stack:
        node, path = stack.pop()
        if isinstance(node, bool):
            continue
        if not isinstance(node, dict):
            return _failure('invalid_schema', path)
        if '$schema' in node:
            dialect = node['$schema']
            if not isinstance(dialect, str) or validator_for(node, default=None) is None:
                return _failure('unsupported_dialect', path + ('$schema',))
        for keyword in _REF_KEYWORDS:
            if keyword in node:
                ref = node[keyword]
                if not isinstance(ref, str):
                    return _failure('invalid_schema', path + (keyword,))
                if ref and not ref.startswith('#'):
                    return _failure('forbidden_reference', path + (keyword,))
        if 'nullable' in node:
            if not isinstance(node['nullable'], bool):
                return _failure('invalid_schema', path + ('nullable',))
            if node['nullable'] and 'type' in node:
                expected = node['type']
                if isinstance(expected, str) and expected != 'null':
                    node['type'] = [expected, 'null']
                elif isinstance(expected, list) and 'null' not in expected:
                    node['type'] = [*expected, 'null']
        for keyword in _SINGLE_SCHEMAS:
            if keyword in node:
                stack.append((node[keyword], path + (keyword,)))
        for keyword in _SCHEMA_MAPS:
            mapping = node.get(keyword)
            if isinstance(mapping, dict):
                stack.extend((v, path + (keyword, k)) for k, v in mapping.items())
        for keyword in _SCHEMA_ARRAYS:
            children = node.get(keyword)
            if isinstance(children, list):
                stack.extend((v, path + (keyword, i)) for i, v in enumerate(children))
        if 'items' in node:
            items = node['items']
            if isinstance(items, list):  # Draft 7 tuple validation.
                stack.extend((v, path + ('items', i)) for i, v in enumerate(items))
            else:
                stack.append((items, path + ('items',)))
        dependencies = node.get('dependencies')
        if isinstance(dependencies, dict):
            stack.extend((v, path + ('dependencies', k))
                         for k, v in dependencies.items() if not isinstance(v, list))
    return None


@lru_cache(maxsize=SCHEMA_CACHE_SIZE)
def _compile_schema(serialized: str) -> tuple[Any, SchemaDiagnostic | None]:
    # Own the schema copy so later caller mutation cannot alter a cached validator.
    schema = json.loads(serialized)
    try:
        problem = _prepare_schema(schema)
        if problem is not None:
            return None, problem
        cls = validator_for(schema, default=Draft202012Validator)
        cls.check_schema(schema)
        return cls(schema, registry=_OFFLINE_REGISTRY), None
    except SchemaError as error:
        return None, _failure('invalid_schema', tuple(error.absolute_path))
    except Exception:
        # Invalid regexes, dialects, or unsupported keyword payloads fail closed.
        return None, _failure('invalid_schema')


def diagnose_schema(value: object, schema: object) -> SchemaDiagnostic | None:
    """Return None on success, otherwise paths and a non-sensitive category.

    No logging, exception messages, instance/schema excerpts, or I/O occurs.
    Malformed schemas and reference/evaluation failures are controlled rejects.
    """
    if schema is None:
        return None
    if not isinstance(schema, (dict, bool)):
        return _failure('invalid_schema')
    try:
        problem = _check_json_tree(schema)
        if problem is not None:
            return problem
        serialized = json.dumps(schema, ensure_ascii=True, allow_nan=False,
                                sort_keys=True, separators=(',', ':'))
        if len(serialized) > MAX_SCHEMA_BYTES:
            return _failure('schema_limit')
        validator, problem = _compile_schema(serialized)
        if problem is not None:
            return problem
        errors = validator.iter_errors(value)
        try:
            error = next(errors, None)
            if error is None:
                return None
            keyword = error.validator if isinstance(error.validator, str) else 'false_schema'
            return SchemaDiagnostic(tuple(error.absolute_schema_path),
                                    tuple(error.absolute_path), f'validation.{keyword}')
        finally:
            errors.close()
    except (Unresolvable, NoSuchResource):
        return _failure('unresolved_reference')
    except RecursionError:
        return _failure('evaluation_limit')
    except Exception:
        # jsonschema wraps some referencing errors in its own exception type.
        # Never expose repr/str(error): they can contain argument values.
        return _failure('evaluation_error')


def matches_schema(value: object, schema: object) -> bool:
    """Validate a tool value without raising or retaining that value."""
    return diagnose_schema(value, schema) is None


def clear_schema_cache() -> None:
    """Discard cached schemas; useful for tests and explicit lifecycle control."""
    _compile_schema.cache_clear()


def schema_cache_info() -> Any:
    """Return only bounded cache counters (no schema or instance contents)."""
    return _compile_schema.cache_info()
