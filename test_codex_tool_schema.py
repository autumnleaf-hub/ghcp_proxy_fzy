"""Offline tests; run with .venv/Scripts/python.exe -X utf8 -m unittest."""
from contextlib import ExitStack
from dataclasses import asdict, fields
import gc
import json
import unittest
from unittest.mock import patch
import weakref

from tool_schema_validation import (
    MAX_SCHEMA_BYTES, SCHEMA_CACHE_SIZE, SchemaDiagnostic, clear_schema_cache,
    diagnose_schema, matches_schema, schema_cache_info,
)


# Representative Codex catalog shape: a closed outer object, local definitions,
# a tagged oneOf target, nested anyOf, nullable input and typed argument arrays.
CODEX_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['prompt', 'target'],
    'properties': {
        'prompt': {'$ref': '#/$defs/nonEmpty'},
        'target': {'$ref': '#/$defs/target'},
        'model': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
        'title': {'type': 'string', 'nullable': True},
        'thinking': {'enum': ['low', 'medium', 'high']},
        'tags': {'type': 'array', 'items': {'type': 'string'},
                 'minItems': 1, 'maxItems': 3, 'uniqueItems': True},
    },
    '$defs': {
        'nonEmpty': {'type': 'string', 'minLength': 1},
        'target': {'oneOf': [
            {'type': 'object', 'additionalProperties': False,
             'properties': {'type': {'const': 'project'},
                            'projectId': {'$ref': '#/$defs/nonEmpty'},
                            'environment': {'$ref': '#/$defs/environment'}},
             'required': ['type', 'projectId', 'environment']},
            {'type': 'object', 'additionalProperties': False,
             'properties': {'type': {'const': 'projectless'},
                            'directoryName': {'type': 'string'}},
             'required': ['type']},
        ]},
        'environment': {'anyOf': [
            {'type': 'object', 'properties': {'type': {'const': 'local'}},
             'required': ['type'], 'additionalProperties': False},
            {'type': 'object', 'properties': {'type': {'const': 'worktree'},
              'startingState': {'type': 'object', 'properties': {
                  'type': {'const': 'branch'}, 'branchName': {'type': 'string'}},
                  'required': ['type', 'branchName'], 'additionalProperties': False}},
             'required': ['type'], 'additionalProperties': False},
        ]},
    },
}


class OfflineSchemaTests(unittest.TestCase):
    def setUp(self):
        clear_schema_cache()
        self.stack = ExitStack()
        self.network = []
        # Block DNS, socket connections, stdlib HTTP, and common HTTP clients.
        # If any path tries networking, the assertion fires before I/O.  The
        # explicit assert_not_called in tearDown also catches swallowed errors.
        for target in (
            'socket.create_connection', 'socket.getaddrinfo',
            'socket.socket.connect', 'socket.socket.connect_ex',
            'socket.socket.sendto', 'urllib.request.urlopen',
            'urllib.request.OpenerDirector.open', 'http.client.HTTPConnection.connect',
            'httpx.Client.request', 'httpx.AsyncClient.request',
        ):
            self.network.append(self.stack.enter_context(
                patch(target, side_effect=AssertionError('network forbidden'))))
        self.addCleanup(self.stack.close)

    def tearDown(self):
        for operation in self.network:
            operation.assert_not_called()

    def test_real_codex_valid_variants(self):
        for target in (
            {'type': 'projectless'},
            {'type': 'projectless', 'directoryName': 'output'},
            {'type': 'project', 'projectId': 'p1', 'environment': {'type': 'local'}},
            {'type': 'project', 'projectId': 'p1', 'environment': {
                'type': 'worktree', 'startingState': {'type': 'branch', 'branchName': 'main'}}},
        ):
            with self.subTest(target=target):
                value = {'prompt': 'Implement a fix', 'target': target,
                         'model': None, 'title': None, 'tags': ['offline']}
                self.assertTrue(matches_schema(value, CODEX_SCHEMA))
                self.assertIsNone(diagnose_schema(value, CODEX_SCHEMA))

    def test_real_codex_invalid_variants(self):
        baseline = {'prompt': 'Implement a fix', 'target': {'type': 'projectless'}}
        for value in (
            {}, {'prompt': 'x'}, {**baseline, 'prompt': 1},
            {**baseline, 'prompt': ''}, {**baseline, 'unknown': True},
            {**baseline, 'target': {'type': 'project'}},
            {**baseline, 'target': {'type': 'projectless', 'projectId': 'p1'}},
            {**baseline, 'target': {'type': 'project', 'projectId': 'p1',
                                  'environment': {'type': 'invalid'}}},
            {**baseline, 'model': False}, {**baseline, 'title': 3},
            {**baseline, 'thinking': 'ultra'}, {**baseline, 'tags': []},
            {**baseline, 'tags': ['a', 'a']}, {**baseline, 'tags': [1]},
            {**baseline, 'tags': ['a', 'b', 'c', 'd']},
        ):
            with self.subTest(value=value):
                self.assertFalse(matches_schema(value, CODEX_SCHEMA))

    def test_no_schema_and_boolean_schema(self):
        for schema in (None, {}, True):
            for value in (None, {}, [], 'text', False, 2):
                self.assertTrue(matches_schema(value, schema))
        self.assertFalse(matches_schema({}, False))
        self.assertFalse(matches_schema({'blocked': 1}, {
            'properties': {'blocked': False}}))

    def test_bad_schema_is_controlled_rejection(self):
        for schema in ([], '', 0, 'object', {'type': 'wrong'},
                       {'type': []}, {'required': 'not-an-array'},
                       {'additionalProperties': 3}, {'oneOf': []},
                       {'anyOf': [None]}, {'allOf': [7]}, {'items': None},
                       {'pattern': '['}, {'nullable': 'true'}, {'$ref': 7},
                       {'$schema': 'https://invalid.example/custom'},
                       {'properties': {1: {'type': 'string'}}},
                       {'minimum': float('nan')}, {'enum': [object()]}):
            with self.subTest(schema=repr(schema)):
                self.assertFalse(matches_schema({}, schema))
                self.assertIsInstance(diagnose_schema({}, schema), SchemaDiagnostic)

    def test_oneof_is_exclusive_anyof_is_not(self):
        branches = [{'type': 'integer'}, {'type': 'number'}]
        self.assertFalse(matches_schema(1, {'oneOf': branches}))
        self.assertTrue(matches_schema(1.5, {'oneOf': branches}))
        self.assertTrue(matches_schema(1, {'anyOf': branches}))
        self.assertFalse(matches_schema('1', {'anyOf': branches}))

    def test_allof_and_ref_siblings(self):
        schema = {'$defs': {'number': {'type': 'integer'}},
                  '$ref': '#/$defs/number', 'allOf': [{'minimum': 1}, {'maximum': 3}]}
        self.assertTrue(matches_schema(2, schema))
        for value in (0, 4, '2', True):
            self.assertFalse(matches_schema(value, schema))

    def test_nullable_keeps_other_assertions(self):
        self.assertTrue(matches_schema(None, {'type': 'string', 'nullable': True}))
        self.assertTrue(matches_schema(None, {'type': ['string', 'null']}))
        self.assertFalse(matches_schema(None, {'type': 'string', 'nullable': False}))
        self.assertFalse(matches_schema(None, {'type': 'string', 'nullable': True,
                                             'enum': ['only-this']}))
        self.assertFalse(matches_schema(1, {'type': 'string', 'nullable': True}))

    def test_required_and_typed_additional_properties(self):
        schema = {'type': 'object', 'required': ['name'],
                  'properties': {'name': {'type': 'string'}},
                  'additionalProperties': {'type': 'integer'}}
        self.assertTrue(matches_schema({'name': 'x', 'count': 2}, schema))
        self.assertFalse(matches_schema({'count': 2}, schema))
        self.assertFalse(matches_schema({'name': 'x', 'count': True}, schema))
        self.assertFalse(matches_schema({'extra': 1}, {'additionalProperties': False}))

    def test_array_constraints_and_prefix_items(self):
        schema = {'type': 'array', 'prefixItems': [{'type': 'string'}, {'type': 'integer'}],
                  'items': False, 'minItems': 2, 'maxItems': 2}
        self.assertTrue(matches_schema(['a', 1], schema))
        for value in (['a'], ['a', True], ['a', 1, 2], 'a'):
            self.assertFalse(matches_schema(value, schema))
        contains = {'type': 'array', 'contains': {'type': 'integer'},
                    'minContains': 1, 'maxContains': 1}
        self.assertTrue(matches_schema(['a', 1], contains))
        self.assertFalse(matches_schema([1, 2], contains))
        self.assertFalse(matches_schema(['a'], contains))

    def test_draft7_definitions_and_tuple_items(self):
        schema = {'$schema': 'http://json-schema.org/draft-07/schema#',
                  'definitions': {'name': {'type': 'string'}}, 'type': 'array',
                  'items': [{'$ref': '#/definitions/name'}, {'type': 'integer'}],
                  'additionalItems': False}
        self.assertTrue(matches_schema(['x', 1], schema))
        self.assertFalse(matches_schema([1, 'x'], schema))
        self.assertFalse(matches_schema(['x', 1, 2], schema))

    def test_anchor_and_escaped_pointer(self):
        schema = {'$defs': {'a/b~c': {'type': 'integer'}}, '$ref': '#/$defs/a~1b~0c'}
        self.assertTrue(matches_schema(1, schema))
        self.assertFalse(matches_schema('1', schema))
        anchor = {'$id': 'https://schemas.example/tool',
                  '$defs': {'text': {'$anchor': 'text', 'type': 'string'}}, '$ref': '#text'}
        self.assertTrue(matches_schema('x', anchor))
        self.assertFalse(matches_schema(1, anchor))

    def test_recursive_and_dynamic_local_refs(self):
        schema = {'$defs': {'node': {'type': 'object', 'properties': {
            'value': {'type': 'integer'}, 'next': {'anyOf': [
                {'$ref': '#/$defs/node'}, {'type': 'null'}]}},
            'required': ['value'], 'additionalProperties': False}}, '$ref': '#/$defs/node'}
        self.assertTrue(matches_schema({'value': 1, 'next': {'value': 2}}, schema))
        self.assertFalse(matches_schema({'value': 1, 'next': {'value': 'bad'}}, schema))
        dynamic = {'$dynamicAnchor': 'node', 'type': 'object',
                   'properties': {'next': {'$dynamicRef': '#node'}}, 'additionalProperties': False}
        self.assertTrue(matches_schema({'next': {'next': {}}}, dynamic))
        self.assertFalse(matches_schema({'next': 2}, dynamic))

    def test_external_refs_are_rejected_without_network_or_file_access(self):
        for ref in ('https://invalid.example/schema', 'http://invalid.example/schema',
                    'file:///not-read/schema.json', '../schema.json', '/schema.json',
                    '//invalid.example/schema', 'C:\not-read\schema.json',
                    'urn:external:schema', 'data:application/json,{}'):
            for keyword in ('$ref', '$dynamicRef', '$recursiveRef'):
                for schema in ({keyword: ref},
                               {'anyOf': [True, {keyword: ref}]},
                               {'$defs': {'unused': {keyword: ref}}},
                               {'properties': {'optional': {keyword: ref}}}):
                    with self.subTest(keyword=keyword, ref=ref), patch(
                            'builtins.open', side_effect=AssertionError('file read forbidden')) as fopen:
                        diagnostic = diagnose_schema({}, schema)
                        self.assertIsNotNone(diagnostic)
                        self.assertEqual(diagnostic.category, 'forbidden_reference')
                        self.assertNotIn(ref, repr(diagnostic))
                        fopen.assert_not_called()

    def test_registry_blocks_refs_hidden_behind_unknown_storage(self):
        # The policy walker does not treat annotations as schemas; this targets
        # a schema stored under an unknown keyword to exercise the registry guard.
        schema = {'storage': {'$ref': 'https://invalid.example/private'}, '$ref': '#/storage'}
        self.assertFalse(matches_schema({}, schema))

    def test_missing_and_infinite_local_refs_fail_closed(self):
        for schema in ({'$ref': '#/$defs/missing'}, {'$ref': '#missing'}, {'$ref': '#'}):
            self.assertFalse(matches_schema({}, schema))

    def test_literals_containing_schema_keywords_are_not_interpreted(self):
        literal = {'$ref': 'https://literal.example/data', 'nullable': 'not-a-schema'}
        schema = {'const': literal, 'default': literal, 'examples': [literal]}
        self.assertTrue(matches_schema(literal, schema))
        self.assertFalse(matches_schema({}, schema))
        self.assertTrue(matches_schema(literal, {'enum': [literal]}))

    def test_diagnostic_has_only_paths_and_category(self):
        secret = 'private-argument-DO-NOT-RETAIN'
        diagnostic = diagnose_schema({'token': secret}, {'type': 'object',
            'properties': {'token': {'type': 'integer'}}})
        self.assertEqual({f.name for f in fields(diagnostic)},
                         {'schema_path', 'instance_path', 'category'})
        self.assertEqual(diagnostic.instance_path, ('token',))
        self.assertEqual(diagnostic.schema_path, ('properties', 'token', 'type'))
        self.assertEqual(diagnostic.category, 'validation.type')
        self.assertNotIn(secret, json.dumps(asdict(diagnostic)))
        self.assertNotIn(secret, repr(diagnostic))

    def test_cache_is_bounded_and_mutation_safe(self):
        schema = {'type': 'string'}
        self.assertTrue(matches_schema('x', schema))
        self.assertTrue(matches_schema('y', schema))
        self.assertGreater(schema_cache_info().hits, 0)
        schema['type'] = 'integer'
        self.assertFalse(matches_schema('x', schema))
        self.assertTrue(matches_schema(1, schema))
        for i in range(SCHEMA_CACHE_SIZE + 12):
            self.assertTrue(matches_schema(i, {'const': i}))
        self.assertEqual(schema_cache_info().currsize, SCHEMA_CACHE_SIZE)
        self.assertEqual(schema_cache_info().maxsize, SCHEMA_CACHE_SIZE)
        clear_schema_cache()
        self.assertEqual(schema_cache_info().currsize, 0)

    def test_instances_are_not_retained_even_on_validation_failure(self):
        class Arguments(dict):
            pass
        def validate_once(schema):
            value = Arguments(token='private-value')
            reference = weakref.ref(value)
            matches_schema(value, schema)
            return reference
        references = [validate_once({'type': 'object'}), validate_once({'type': 'string'})]
        gc.collect()
        self.assertTrue(all(reference() is None for reference in references))

    def test_size_and_depth_limits_and_cyclic_schema(self):
        self.assertFalse(matches_schema({}, {'description': 'x' * (MAX_SCHEMA_BYTES + 1)}))
        self.assertEqual(schema_cache_info().currsize, 0)
        cyclic = {}
        cyclic['properties'] = {'self': cyclic}
        self.assertFalse(matches_schema({}, cyclic))
        self.assertEqual(diagnose_schema({}, cyclic).category, 'schema_limit')

    def test_codex_automation_defs_aliases_and_discriminators(self):
        # Exact structural pattern of Codex automation_update: named $defs
        # aliases, top-level oneOf, nested nullable fields and closed branches.
        schema = {
            '$defs': {
                '__schema0': {'type': 'object', 'additionalProperties': False,
                    'properties': {'id': {'$ref': '#/$defs/__schema1'},
                                   'mode': {'enum': ['view'], 'type': 'string'}},
                    'required': ['mode', 'id']},
                '__schema1': {'$ref': '#/$defs/__schema2'},
                '__schema2': {'type': 'string'},
                '__schema3': {'type': 'object', 'additionalProperties': False,
                    'properties': {'id': {'$ref': '#/$defs/__schema1'},
                                   'mode': {'enum': ['delete'], 'type': 'string'},
                                   'notificationPolicy': {'anyOf': [
                                       {'enum': ['failed_runs_only'], 'type': 'string'},
                                       {'type': 'null'}]}},
                    'required': ['mode', 'id']},
            },
            'oneOf': [{'$ref': '#/$defs/__schema0'}, {'$ref': '#/$defs/__schema3'}],
            'properties': {}, 'type': 'object',
        }
        for value in ({'mode': 'view', 'id': 'task'},
                      {'mode': 'delete', 'id': 'task', 'notificationPolicy': None},
                      {'mode': 'delete', 'id': 'task', 'notificationPolicy': 'failed_runs_only'}):
            self.assertTrue(matches_schema(value, schema))
        for value in ({'mode': 'view'}, {'mode': 'view', 'id': 7},
                      {'mode': 'unknown', 'id': 'task'},
                      {'mode': 'view', 'id': 'task', 'extra': True},
                      {'mode': 'delete', 'id': 'task', 'notificationPolicy': False}):
            self.assertFalse(matches_schema(value, schema))

    def test_codex_exec_command_schema(self):
        schema = {'type': 'object', 'additionalProperties': False,
                  'properties': {'cmd': {'type': 'string'},
                    'max_output_tokens': {'type': 'number'},
                    'login': {'type': 'boolean'},
                    'prefix_rule': {'type': 'array', 'items': {'type': 'string'}},
                    'sandbox_permissions': {'type': 'string',
                        'enum': ['use_default', 'require_escalated']}},
                  'required': ['cmd']}
        self.assertTrue(matches_schema({'cmd': 'echo offline', 'login': False,
                                        'prefix_rule': ['echo']}, schema))
        for value in ({}, {'cmd': 7}, {'cmd': 'x', 'login': 1},
                      {'cmd': 'x', 'max_output_tokens': True},
                      {'cmd': 'x', 'prefix_rule': [7]},
                      {'cmd': 'x', 'sandbox_permissions': 'invalid'}):
            self.assertFalse(matches_schema(value, schema))

    def test_diagnostic_nested_paths_and_schema_failures_do_not_leak_values(self):
        secret = 'secret-value-must-never-appear'
        schema = {'type': 'array', 'items': {'type': 'object',
                  'properties': {'token': {'type': 'integer'}}}}
        diagnostic = diagnose_schema([{'token': secret}], schema)
        self.assertEqual(diagnostic.instance_path, (0, 'token'))
        self.assertEqual(diagnostic.schema_path, ('items', 'properties', 'token', 'type'))
        for invalid_schema in (schema, {'type': secret}, {'$ref': secret},
                               {'enum': [secret]}, {'$ref': '#/missing'}):
            diagnostic = diagnose_schema([{'token': secret}], invalid_schema)
            self.assertIsNotNone(diagnostic)
            self.assertNotIn(secret, json.dumps(asdict(diagnostic)))

    def test_nullable_ref_and_known_dialects_stay_offline(self):
        for dialect in ('https://json-schema.org/draft/2020-12/schema',
                        'https://json-schema.org/draft/2019-09/schema',
                        'http://json-schema.org/draft-07/schema#'):
            schema = {'$schema': dialect, 'definitions': {'text': {
                'type': 'string', 'nullable': True}}, '$ref': '#/definitions/text'}
            self.assertTrue(matches_schema(None, schema))
            self.assertTrue(matches_schema('text', schema))
            self.assertFalse(matches_schema(False, schema))

    def test_wide_schemas_are_rejected_before_caching(self):
        self.assertEqual(diagnose_schema({}, {'enum': list(range(20_001))}).category,
                         'schema_limit')
        self.assertEqual(schema_cache_info().currsize, 0)

    def test_input_schema_is_not_modified_by_nullable(self):
        schema = {'properties': {'name': {'type': 'string', 'nullable': True}}}
        before = json.dumps(schema, sort_keys=True)
        self.assertTrue(matches_schema({'name': None}, schema))
        self.assertEqual(json.dumps(schema, sort_keys=True), before)


if __name__ == '__main__':
    unittest.main()
