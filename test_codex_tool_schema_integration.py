"""Real Excel relay/schema integration tests, with all networking disabled.

Run the targeted suites offline using this module's command-line entrypoint.
No tool executor is used: response items are synthetic transport fixtures.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import json
import unittest
from unittest.mock import patch


@contextmanager
def network_disabled():
    """Fail before I/O and assert no attempted network call was swallowed."""
    with ExitStack() as stack:
        operations = []
        for target in (
            'socket.create_connection', 'socket.getaddrinfo',
            'socket.gethostbyname', 'socket.gethostbyname_ex',
            'socket.socket.connect', 'socket.socket.connect_ex',
            'socket.socket.sendto', 'socket.socket.send', 'socket.socket.sendall',
            'socket.socket.bind', 'socket.socket.listen', 'socket.socketpair',
            'socket.getnameinfo', 'urllib.request.urlopen',
            'urllib.request.OpenerDirector.open',
            'http.client.HTTPConnection.connect',
            'http.client.HTTPConnection.request',
            'httpx.Client.request', 'httpx.Client.send',
            'httpx.AsyncClient.request', 'httpx.AsyncClient.send',
        ):
            operations.append(stack.enter_context(
                patch(target, side_effect=AssertionError('network forbidden'))))
        try:
            yield operations
        finally:
            for operation in operations:
                operation.assert_not_called()

TOOL_NAME = 'schema_integration_probe'
TOOL_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['prompt', 'target'],
    'properties': {
        'prompt': {'$ref': '#/$defs/nonEmpty'},
        'target': {'$ref': '#/$defs/target'},
        'title': {'$ref': '#/$defs/nullableTitle'},
    },
    '$defs': {
        'nonEmpty': {'type': 'string', 'minLength': 1},
        'nullableTitle': {'type': 'string', 'nullable': True},
        'target': {'oneOf': [
            {'$ref': '#/$defs/project'}, {'$ref': '#/$defs/projectless'}]},
        'project': {'type': 'object', 'additionalProperties': False,
            'properties': {'type': {'const': 'project'},
                           'projectId': {'$ref': '#/$defs/nonEmpty'}},
            'required': ['type', 'projectId']},
        'projectless': {'type': 'object', 'additionalProperties': False,
            'properties': {'type': {'const': 'projectless'},
                           'directoryName': {'type': 'string'}},
            'required': ['type']},
    },
}


class CodexToolSchemaIntegrationTests(unittest.TestCase):
    def setUp(self):
        from collections import OrderedDict
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(network_disabled())
        # Import under the guard, not at module load.  No production function,
        # schema validator, transport decoder or request builder is mocked.
        import excel_upstream
        import tool_schema_validation
        self.upstream = excel_upstream
        self.assertIs(excel_upstream._value_matches_schema,
                      tool_schema_validation.matches_schema)
        self.stack.enter_context(patch.object(excel_upstream, '_native_call_cache', OrderedDict()))
        tool_schema_validation.clear_schema_cache()
        self.addCleanup(tool_schema_validation.clear_schema_cache)
        self.sequence = 0

    def _source(self, schema=TOOL_SCHEMA, *, key='parameters', fields=None):
        tool = {'type': 'function', 'name': TOOL_NAME, key: schema}
        if fields is not None:
            tool.update(fields)
        return {'model': 'gpt-5.6-sol-excel',
                'input': [{'role': 'user', 'content': 'Validate a simulated tool call.'}],
                'tools': [tool]}

    def _native(self, arguments, *, relayed):
        self.sequence += 1
        payload = arguments
        if relayed:
            payload = {
                'summary': 'Offline schema fixture',
                'extended_summary': 'Decode only; never execute a real tool.',
                'destructive': False, 'references': [],
                'code': json.dumps({'name': TOOL_NAME, 'arguments': arguments}),
            }
        return {'type': 'function_call', 'status': 'completed',
                'id': f'fc_schema_integration_{self.sequence}',
                'call_id': f'call_schema_integration_{self.sequence}',
                'name': 'run_officejs' if relayed else TOOL_NAME,
                'arguments': json.dumps(payload)}

    def _catalog(self, body):
        catalogs = []
        for item in body['input']:
            if not isinstance(item, dict):
                continue
            content = item.get('content', [])
            texts = [content] if isinstance(content, str) else [
                part.get('text', '') for part in content if isinstance(part, dict)]
            for text in texts:
                marker = 'Available client tools:'
                if marker in text:
                    catalog, _ = json.JSONDecoder().raw_decode(text.split(marker, 1)[1].lstrip())
                    catalogs.append(catalog)
        self.assertEqual(len(catalogs), 1, 'Expected one authoritative serialized catalog')
        self.assertEqual(len(catalogs[0]), 1)
        self.assertEqual(catalogs[0][0]['name'], TOOL_NAME)
        return catalogs[0][0]

    def _assert_chain(self, source, arguments, accepted, expected_schema):
        before = deepcopy(source)
        original_tool = source['tools'][0]
        schema_objects = {key: original_tool[key]
                          for key in ('parameters', 'inputSchema', 'input_schema')
                          if key in original_tool}
        body = self.upstream.prepare_responses_body(source)
        self.assertEqual(self._catalog(body)['parameters'], expected_schema)
        for relayed in (False, True):
            with self.subTest(relayed=relayed):
                native = self._native(arguments, relayed=relayed)
                response = {'output': [native]}
                response_before = deepcopy(response)
                calls = self.upstream.extract_native_client_tool_calls(response, source)
                self.assertEqual(response, response_before)
                self.assertEqual(len(calls), 1 if accepted else 0)
                if not accepted:
                    continue
                call = calls[0]
                self.assertEqual(call['type'], 'function_call')
                self.assertEqual(call['name'], TOOL_NAME)
                self.assertEqual(call['call_id'], native['call_id'])
                self.assertEqual(json.loads(call['arguments']), arguments)
                # Simulate an executor result as data.  No executor is imported
                # or called; replay must use the real request translation path.
                replay_source = deepcopy(source)
                replay_source['input'].extend([call, {
                    'type': 'function_call_output', 'call_id': call['call_id'],
                    'output': 'synthetic result; no tool was executed',
                }])
                replay_before = deepcopy(replay_source)
                replay_body = self.upstream.prepare_responses_body(replay_source)
                replay_calls = [item for item in replay_body['input']
                                if item.get('type') == 'function_call']
                replay_outputs = [item for item in replay_body['input']
                                  if item.get('type') == 'function_call_output']
                self.assertEqual(len(replay_calls), 1)
                self.assertEqual(replay_calls[0]['name'], native['name'])
                self.assertEqual(replay_calls[0]['arguments'], native['arguments'])
                self.assertEqual(replay_calls[0]['call_id'], native['call_id'])
                self.assertEqual(len(replay_outputs), 1)
                self.assertEqual(replay_outputs[0]['output'],
                                 'synthetic result; no tool was executed')
                self.assertEqual(self._catalog(replay_body)['parameters'], expected_schema)
                self.assertEqual(replay_source, replay_before)
        self.assertEqual(source, before)
        self.assertIs(source['tools'][0], original_tool)
        for key, original_schema in schema_objects.items():
            self.assertIs(source['tools'][0][key], original_schema)

    def test_defs_ref_oneof_accepts_both_valid_branches(self):
        for target in ({'type': 'project', 'projectId': 'mock-project'},
                       {'type': 'projectless', 'directoryName': 'mock-output'}):
            with self.subTest(target=target):
                self._assert_chain(self._source(), {'prompt': 'Offline', 'target': target},
                                   True, TOOL_SCHEMA)

    def test_defs_ref_oneof_rejects_missing_required_fields(self):
        for arguments in ({}, {'prompt': 'Offline'}, {'target': {'type': 'projectless'}},
                          {'prompt': 'Offline', 'target': {'type': 'project'}}):
            with self.subTest(arguments=arguments):
                self._assert_chain(self._source(), arguments, False, TOOL_SCHEMA)

    def test_defs_ref_oneof_rejects_wrong_branch_and_type(self):
        for target in ({'type': 'unknown'},
                       {'type': 'projectless', 'projectId': 'mock-project'},
                       {'type': 'project', 'projectId': 7},
                       {'type': 'project', 'projectId': 'x', 'directoryName': 'wrong-branch'}):
            with self.subTest(target=target):
                self._assert_chain(self._source(), {'prompt': 'Offline', 'target': target},
                                   False, TOOL_SCHEMA)

    def test_oneof_rejects_overlapping_valid_branches(self):
        schema = {'$defs': {'a': {'type': 'object'}, 'b': {'type': 'object'}},
                  'oneOf': [{'$ref': '#/$defs/a'}, {'$ref': '#/$defs/b'}]}
        self._assert_chain(self._source(schema), {}, False, schema)

    def test_nullable_local_ref_accepts_null_and_string_rejects_number(self):
        for title, accepted in ((None, True), ('Title', True), (7, False)):
            with self.subTest(title=title):
                arguments = {'prompt': 'Offline', 'target': {'type': 'projectless'}, 'title': title}
                self._assert_chain(self._source(), arguments, accepted, TOOL_SCHEMA)

    def test_false_root_stays_false_in_catalog_and_rejects_all_calls(self):
        for key in ('parameters', 'inputSchema', 'input_schema'):
            for arguments in ({}, {'prompt': 'Offline', 'target': {'type': 'projectless'}}):
                with self.subTest(key=key, arguments=arguments):
                    source = self._source(False, key=key)
                    self.assertIs(self._catalog(
                        self.upstream.prepare_responses_body(source))['parameters'], False)
                    self._assert_chain(source, arguments, False, False)

    def test_input_schema_alias_with_absent_or_none_parameters(self):
        for key in ('inputSchema', 'input_schema'):
            for fields in ({}, {'parameters': None}):
                with self.subTest(key=key, fields=fields):
                    source = self._source(key=key, fields=fields)
                    valid = {'prompt': 'Offline', 'target': {'type': 'projectless'}}
                    self._assert_chain(source, valid, True, TOOL_SCHEMA)
                    self._assert_chain(source, {'prompt': 'Offline'}, False, TOOL_SCHEMA)

    def test_false_and_empty_schemas_do_not_fall_through_aliases(self):
        for fields, expected, accepted in (
            ({'parameters': False, 'inputSchema': TOOL_SCHEMA}, False, False),
            ({'parameters': None, 'inputSchema': False, 'input_schema': TOOL_SCHEMA}, False, False),
            ({'parameters': {}, 'inputSchema': False}, {}, True),
            ({'parameters': True, 'inputSchema': False}, True, True),
        ):
            with self.subTest(fields=fields):
                self._assert_chain(self._source(fields=fields), {}, accepted, expected)

    def test_no_schema_remains_compatible(self):
        source = self._source(None)
        self._assert_chain(source, {'arbitrary': [None, True, 3]}, True, {})
        del source['tools'][0]['parameters']
        self._assert_chain(source, {'arbitrary': [None, True, 3]}, True, {})

    def test_source_schema_identity_and_contents_survive_repeated_use(self):
        schema = deepcopy(TOOL_SCHEMA)
        source = self._source(schema, key='inputSchema', fields={'parameters': None})
        before = deepcopy(schema)
        serialized_before = json.dumps(schema, sort_keys=True)
        arguments = {'prompt': 'Offline', 'target': {'type': 'projectless'}, 'title': None}
        for _ in range(3):
            self._assert_chain(source, arguments, True, schema)
        self.assertIs(source['tools'][0]['inputSchema'], schema)
        self.assertEqual(schema, before)
        self.assertEqual(json.dumps(schema, sort_keys=True), serialized_before)
        self.assertEqual(schema['$defs']['nullableTitle']['type'], 'string')

    def test_invalid_batch_rejects_without_remembering_partial_calls(self):
        source = self._source()
        valid = self._native({'prompt': 'Offline', 'target': {'type': 'projectless'}}, relayed=True)
        invalid = self._native({'prompt': 'Offline', 'target': {'type': 'project'}}, relayed=True)
        self.assertEqual(self.upstream.extract_native_client_tool_calls(
            {'output': [valid, invalid]}, source), [])
        self.assertFalse(self.upstream._native_call_cache)

    def test_external_reference_is_rejected_through_real_extractor(self):
        schema = {'$ref': 'https://invalid.example/never-fetch-schema'}
        self._assert_chain(self._source(schema), {}, False, schema)


def run_offline_suites(names):
    """Run targeted tests with no network, including imports and async setup.

    Windows' default asyncio self-pipe uses a loopback TCP socketpair.  These
    mocked, in-process suites need only timers/ready callbacks, so use a
    socketless selector loop rather than exempting any network connections.
    Runtime state is redirected to a disposable directory before proxy import.
    """
    import asyncio
    import selectors
    import os
    import tempfile
    import time

    class OfflineSelector(selectors.SelectSelector):
        def select(self, timeout=None):
            if not self.get_map():
                if timeout is None or timeout > 0:
                    time.sleep(min(timeout if timeout is not None else 0.01, 0.01))
                return []
            raise AssertionError('Unexpected I/O registration in offline suite')

    class OfflineLoop(asyncio.SelectorEventLoop):
        def __init__(self):
            super().__init__(selector=OfflineSelector())

        def _make_self_pipe(self):
            self._ssock = self._csock = None

        def _close_self_pipe(self):
            pass

    class OfflinePolicy(asyncio.DefaultEventLoopPolicy):
        def new_event_loop(self):
            return OfflineLoop()

    previous_policy = asyncio.get_event_loop_policy()
    try:
        asyncio.set_event_loop_policy(OfflinePolicy())
        with tempfile.TemporaryDirectory(prefix='codex-schema-offline-') as state_dir:
            with (network_disabled(),
                  patch.dict(os.environ, {'GHCP_STATE_DIR': state_dir}),
                  patch('app_paths.user_state_dir', return_value=state_dir)):
                suite = unittest.defaultTestLoader.loadTestsFromNames(names)
                counts = {}
                def count_tests(group):
                    for test in group:
                        if isinstance(test, unittest.TestSuite):
                            count_tests(test)
                        else:
                            module = test.__class__.__module__
                            counts[module] = counts.get(module, 0) + 1
                count_tests(suite)
                print('Targeted test counts:', json.dumps(counts, sort_keys=True), flush=True)
                result = unittest.TextTestRunner(verbosity=1).run(suite)
        print('Network attempts: 0 (asserted across import, execution and teardown)', flush=True)
        return 0 if result.wasSuccessful() else 1
    finally:
        asyncio.set_event_loop_policy(previous_policy)


if __name__ == '__main__':
    import sys
    modules = sys.argv[1:] or [
        'test_codex_tool_schema', 'test_codex_tool_schema_integration', 'test_excel_upstream']
    raise SystemExit(run_offline_suites(modules))
