"""Offline regressions: never import the proxy or dispatch any tool."""
import builtins
import copy
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import unittest
from contextlib import ExitStack
from unittest import mock

import client_tool_transport as transport

BS, NL, CR, TAB = map(chr, (92, 10, 13, 9))


def native(code, name="run_officejs", encoded=True):
    arguments = {"code": code}
    return {"type": "function_call", "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False) if encoded else arguments}


class TransportCodeTests(unittest.TestCase):
    def test_object_text_and_explicit_json_fence(self):
        call = {"name": "exec_command", "arguments": {"cmd": "pwd"}}
        text = json.dumps(call)
        for code in (call, text, " " + NL + text + TAB,
                     "```json" + NL + text + NL + "```",
                     "```json" + CR + NL + text + CR + NL + "```"):
            with self.subTest(code=code):
                self.assertEqual(transport.decode_transport_code(code), call)

    def test_rejects_scripts_and_multiple_calls(self):
        text = '{"name":"exec_command","arguments":{"cmd":"pwd"}}'
        bad = ["const call = " + text + ";", "return " + text,
               "await Excel.run(async ctx => {" + text + "});",
               "prefix " + text, text + " suffix", text + text,
               "[" + text + "]", "```javascript" + NL + text + NL + "```",
               "```" + NL + text + NL + "```",
               "```json" + NL + text + NL + "```" + NL + "```json" + NL + text + NL + "```",
               "```json" + NL + text + NL + text + NL + "```",
               None, [], 42, "null", "{};", json.dumps(text)]
        for code in bad:
            with self.subTest(code=code):
                self.assertIsNone(transport.decode_transport_code(code))

    def test_preserves_multiline_js_and_chinese_windows_paths(self):
        path = BS.join(["C:", "用户", "项目", "文件.js"])
        command = 'const p = ' + json.dumps(path, ensure_ascii=False) + ';' + NL + 'console.log(p);' + NL
        args = {"code": command, "path": path, "count": 0, "enabled": False,
                "empty": None, "nested": [1, {"x": TAB + CR + NL}]}
        call = {"name": "mcp__node_repl.js", "arguments": args}
        for ascii_only in (True, False):
            self.assertEqual(transport.decode_transport_code(json.dumps(call, ensure_ascii=ascii_only)), call)
            self.assertEqual(transport.decode_transport_envelope(native(json.dumps(call, ensure_ascii=ascii_only))), call)

    def test_existing_shell_regex_repair_regression(self):
        command = 'rg -n ' + BS + '( pattern'
        malformed = '{"name":"exec_command","arguments":{"cmd":"' + command + '"}}'
        expected = {"name": "exec_command", "arguments": {"cmd": command}}
        self.assertEqual(transport.decode_transport_code(malformed), expected)
        self.assertEqual(transport.decode_transport_envelope(native(malformed)), expected)

    def test_repairs_only_invalid_json_backslashes(self):
        command = 'grep ' + BS + '(x' + BS + ') C:' + BS + '用户' + BS + '项目'
        code = '{"name":"exec_command","arguments":{"cmd":"' + command + '"}}'
        self.assertEqual(transport.decode_transport_code(code)["arguments"]["cmd"], command)
        for suffix in ("uZZZZ", "u12", "q"):
            sequence = BS + suffix
            self.assertEqual(transport.decode_transport_code('{"x":"' + sequence + '"}'), {"x": sequence})

    def test_valid_escape_sequences_never_reinterpreted(self):
        for suffix in ('n', 't', 'r', 'b', 'f', '/', BS, '"', 'u4e2d', 'uD83D' + BS + 'uDE00'):
            text = '{"x":"' + BS + suffix + '"}'
            with self.subTest(suffix=suffix):
                self.assertEqual(transport.decode_transport_code(text), json.loads(text))
        mixed = '{"x":"' + BS + 'n' + BS + 'q' + BS + 'u4e2d"}'
        self.assertEqual(transport.decode_transport_code(mixed), {"x": NL + BS + 'q中'})
        path_like = '{"x":"C:' + BS + 'new' + BS + 'test"}'
        self.assertEqual(transport.decode_transport_code(path_like), json.loads(path_like))

    def test_does_not_repair_other_json_errors(self):
        for text in ('{"x":"line' + chr(0) + 'line"}', '{"x":1,}', "{'x': 1}",
                     '{' + BS + 'q:1}', '{"x":"unfinished' + BS):
            with self.subTest(text=text):
                self.assertIsNone(transport.decode_transport_code(text))

    def test_duplicate_keys_and_nonfinite_numbers_rejected(self):
        for text in ('{"name":"a","name":"b"}', '{"a":{"x":1,"x":2}}',
                     '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e9999}'):
            self.assertIsNone(transport.decode_transport_code(text))

    def test_depth_size_cycles_and_non_json_objects(self):
        cyclic = {}
        cyclic["self"] = cyclic
        cyclic_list = []
        cyclic_list.append(cyclic_list)
        values = [cyclic, {"x": cyclic_list}, {"x": object()}, {1: "x"},
                  {"x": float("inf")}, {"x": (1, 2)},
                  {"x": "x" * transport.MAX_TEXT_LENGTH},
                  ' ' * (transport.MAX_TEXT_LENGTH + 1),
                  '{"x":' + '[' * 10000 + '0' + ']' * 10000 + '}']
        for code in values:
            self.assertIsNone(transport.decode_transport_code(code))
        deep = {}
        for _ in range(1000):
            deep = {"x": deep}
        self.assertIsNone(transport.decode_transport_code(deep))

    def test_exact_depth_boundary_and_quoted_brackets(self):
        value = {}
        for _ in range(transport.MAX_JSON_DEPTH - 1):
            value = {"x": value}
        self.assertEqual(transport.decode_transport_code(value), value)
        self.assertEqual(transport.decode_transport_code(json.dumps(value)), value)
        self.assertIsNone(transport.decode_transport_code({"x": value}))
        self.assertIsNone(transport.decode_transport_code(json.dumps({"x": value})))
        text = {"x": '[' * 1000 + '"' + BS + ']' * 1000}
        self.assertEqual(transport.decode_transport_code(json.dumps(text)), text)

    def test_node_budget_and_shared_objects(self):
        shared = {"x": [1, 2, 3]}
        value = {"first": shared, "second": shared}
        self.assertEqual(transport.decode_transport_code(value), value)
        with mock.patch.object(transport, "MAX_JSON_NODES", 10):
            self.assertIsNone(transport.decode_transport_code({"x": list(range(20))}))
            self.assertIsNone(transport.decode_transport_code(json.dumps({"x": list(range(20))})))


class TransportEnvelopeTests(unittest.TestCase):
    def test_function_argument_representations_and_aliases(self):
        args = {"cmd": "echo 中文" + NL + "next", "path": BS.join(["E:", "项目", "中文"]), "n": None, "flag": False}
        for outer_encoded in (True, False):
            for inner_encoded in (True, False):
                for code_encoded in (True, False):
                    for alias in transport.DEFAULT_TRANSPORT_NAMES:
                        call = {"name": "exec_command", "arguments": json.dumps(args) if inner_encoded else args}
                        code = json.dumps(call) if code_encoded else call
                        envelope = native(code, alias, outer_encoded)
                        original = copy.deepcopy(envelope)
                        with self.subTest(outer=outer_encoded, inner=inner_encoded, code=code_encoded, alias=alias):
                            self.assertEqual(transport.decode_transport_envelope(envelope), {"name": "exec_command", "arguments": args})
                            self.assertEqual(envelope, original)

    def test_nested_wrappers_depth_limit(self):
        call = {"name": "exec_command", "arguments": {"cmd": "pwd"}}
        for encoded in (True, False):
            code = call
            for count in range(5):
                with self.subTest(count=count, encoded=encoded):
                    result = transport.decode_transport_envelope(native(json.dumps(code) if encoded else code, encoded=encoded))
                    self.assertEqual(result, call if count <= 2 else None)
                arguments = {"code": json.dumps(code) if encoded else code}
                code = {"name": "functions.run_officejs" if count % 2 else "run_officejs",
                        "arguments": json.dumps(arguments) if encoded else arguments}

    def test_custom_input_is_byte_for_byte_unchanged(self):
        inputs = ["", '  ' + NL + '*** Begin Patch' + CR + NL + '中文 C:' + BS + '目录' + NL + '*** End Patch' + NL,
                  '{"name":"run_officejs","arguments":{}}',
                  '```json' + NL + '{"x":1}' + NL + '```',
                  "__import__('os').system('echo SHOULD_NOT_RUN')"]
        for raw in inputs:
            call = {"name": "apply_patch", "input": raw}
            for encoded in (True, False):
                self.assertEqual(transport.decode_transport_envelope(native(json.dumps(call) if encoded else call, encoded=encoded)), call)

    def test_invalid_envelopes_and_ambiguous_payloads(self):
        bad = [{}, {"arguments": {}}, {"name": "", "arguments": {}},
               {"name": " exec_command", "arguments": {}},
               {"name": 1, "arguments": {}}, {"name": "exec_command"},
               {"name": "exec_command", "arguments": []},
               {"name": "exec_command", "arguments": "null"},
               {"name": "exec_command", "arguments": "{}{}"},
               {"name": "exec_command", "arguments": {}, "input": "x"},
               {"name": "apply_patch", "input": {}},
               {"name": "exec_command", "arguments": {}, "calls": []}]
        for call in bad:
            with self.subTest(call=call):
                self.assertIsNone(transport.decode_transport_envelope(native(call, encoded=False)))
        for envelope in (None, [], {"name": "exec_command", "arguments": {}},
                         {"name": "run_officejs", "arguments": {}},
                         {"name": "run_officejs", "arguments": "{}{}"}):
            self.assertIsNone(transport.decode_transport_envelope(envelope))

    def test_argument_strings_do_not_use_code_repair_or_fence(self):
        for raw in ('{"cmd":"' + BS + 'q"}', '```json' + NL + '{}' + NL + '```', '{"x":1,"x":2}'):
            self.assertIsNone(transport.decode_transport_envelope(native({"name": "exec_command", "arguments": raw})))
            self.assertIsNone(transport.decode_transport_envelope({"name": "run_officejs", "arguments": raw}))

    def test_alias_configuration_and_no_implicit_name(self):
        call = {"name": "exec_command", "arguments": {}}
        self.assertEqual(transport.decode_transport_envelope(native(call, "relay", False), {"relay"}), call)
        self.assertIsNone(transport.decode_transport_envelope(native(call, "relay", False)))
        for names in ([], "run_officejs", [None], [""], ["x"] * 33, iter(["run_officejs"])):
            self.assertIsNone(transport.decode_transport_envelope(native(call), names))

    def test_catalog_schema_validation_remains_with_caller(self):
        call = {"name": "exec_command", "arguments": {"cmd": 123}}
        self.assertEqual(transport.decode_transport_envelope(native(call)), call)

    def test_cyclic_native_and_deep_encoded_arguments_rejected(self):
        cyclic = native({}, encoded=False)
        cyclic["arguments"]["code"] = cyclic
        self.assertIsNone(transport.decode_transport_envelope(cyclic))
        deep = '{"x":' + '[' * 10000 + '0' + ']' * 10000 + '}'
        self.assertIsNone(transport.decode_transport_envelope(native({"name": "exec_command", "arguments": deep})))

    def test_no_script_filesystem_or_network_execution(self):
        payload = "__import__('pathlib').Path('NEVER_WRITE').write_text('bad'); __import__('socket').create_connection(('example.invalid', 9))"
        call = {"name": "exec_command", "arguments": {"cmd": payload}}
        custom = {"name": "apply_patch", "input": payload}
        targets = [(builtins, "eval"), (builtins, "exec"), (builtins, "open"), (io, "open"),
                   (os, "system"), (os, "popen"), (os, "open"), (os, "remove"),
                   (subprocess, "Popen"), (socket, "socket"), (socket, "create_connection"),
                   (Path, "write_text"), (Path, "write_bytes")]
        with ExitStack() as stack:
            guards = [stack.enter_context(mock.patch.object(owner, attr, side_effect=AssertionError(attr))) for owner, attr in targets]
            self.assertEqual(transport.decode_transport_envelope(native(json.dumps(call))), call)
            self.assertEqual(transport.decode_transport_envelope(native(json.dumps(custom))), custom)
            self.assertIsNone(transport.decode_transport_code(payload))
            self.assertIsNone(transport.decode_transport_code("const call = " + json.dumps(call)))
            for guard in guards:
                guard.assert_not_called()


    def test_native_type_matches_existing_transport_contract(self):
        call = {"name": "exec_command", "arguments": {}}
        for kind in (None, "custom_tool_call", "function_call_output", [], 42):
            envelope = native(call, encoded=False)
            envelope["type"] = kind
            self.assertIsNone(transport.decode_transport_envelope(envelope))
        envelope = native(call, encoded=False)
        del envelope["type"]
        self.assertIsNone(transport.decode_transport_envelope(envelope))

    def test_host_metadata_preserved_and_extra_calls_rejected(self):
        call = {"name": "exec_command", "arguments": {"cmd": "pwd"}}
        envelope = native(call, encoded=False)
        envelope.update(id="native_id", call_id="call_id", status="completed")
        envelope["arguments"].update(summary="Read local files", extended_summary="Read only",
                                     destructive=False, references=[])
        before = copy.deepcopy(envelope)
        self.assertEqual(transport.decode_transport_envelope(envelope), call)
        self.assertEqual(envelope, before)
        for extra in ("calls", "tool_calls", "output", "code2"):
            bad = copy.deepcopy(envelope)
            bad["arguments"][extra] = [call, call]
            self.assertIsNone(transport.decode_transport_envelope(bad))

    def test_subclass_hooks_are_not_executed(self):
        class PoisonDict(dict):
            def items(self):
                raise AssertionError("items executed")
            def get(self, *args):
                raise AssertionError("get executed")
        class PoisonStr(str):
            def strip(self):
                raise AssertionError("strip executed")
        for value in (PoisonDict(), PoisonStr('{}'), {"x": PoisonDict()}):
            self.assertIsNone(transport.decode_transport_code(value))
        self.assertIsNone(transport.decode_transport_envelope(PoisonDict()))

    def test_every_invalid_ascii_escape_keeps_its_literal_value(self):
        valid = set('bfnrtu/') | {BS, '"'}
        for number in range(32, 127):
            character = chr(number)
            if character in valid:
                continue
            text = '{"x":"' + BS + character + '"}'
            self.assertEqual(transport.decode_transport_code(text), {"x": BS + character})


    def test_namespace_and_functions_prefix_preserved_for_resolver(self):
        arguments = {"code": 'const path = "C:' + BS + BS + '中文";' + NL + 'console.log(path);'}
        for name in ("js", "functions.js", "browser.js", "functions.browser.js"):
            for namespace in ("browser", "functions.browser", "工具"):
                for encoded_arguments in (True, False):
                    expected = {"name": name, "namespace": namespace, "arguments": arguments}
                    call = dict(expected, arguments=json.dumps(arguments) if encoded_arguments else arguments)
                    text = json.dumps(call, ensure_ascii=False)
                    for code in (call, text, '```json' + NL + text + NL + '```'):
                        for outer_encoded in (True, False):
                            envelope = native(code, encoded=outer_encoded)
                            before = copy.deepcopy(envelope)
                            with self.subTest(name=name, namespace=namespace, arguments_encoded=encoded_arguments, outer_encoded=outer_encoded):
                                self.assertEqual(transport.decode_transport_envelope(envelope), expected)
                                self.assertEqual(envelope, before)

    def test_custom_namespace_preserved_without_input_rewriting(self):
        raw = '  *** Begin Patch' + CR + NL + '中文 ' + BS + 'q' + NL + '*** End Patch  '
        call = {"name": "functions.apply_patch", "namespace": "patch_tools", "input": raw}
        for encoded in (True, False):
            self.assertEqual(transport.decode_transport_envelope(native(json.dumps(call) if encoded else call, encoded=encoded)), call)

    def test_namespace_validation_rejects_nonstring_and_blank_values(self):
        for namespace in (None, 0, False, [], {}, "", " ", NL, " browser", "browser ", TAB + "browser"):
            call = {"name": "js", "namespace": namespace, "arguments": {"code": "never execute"}}
            with self.subTest(namespace=namespace):
                self.assertIsNone(transport.decode_transport_envelope(native(call, encoded=False)))
                outer = native({"name": "exec_command", "arguments": {}}, encoded=False)
                outer["namespace"] = namespace
                if namespace is None:
                    self.assertEqual(transport.decode_transport_envelope(outer),
                                     {"name": "exec_command", "arguments": {}})
                else:
                    self.assertIsNone(transport.decode_transport_envelope(outer))
                wrapper = {"name": "functions.run_officejs", "namespace": namespace,
                           "arguments": {"code": {"name": "exec_command", "arguments": {}}}}
                self.assertIsNone(transport.decode_transport_envelope(native(wrapper, encoded=False)))

    def test_namespace_survives_nested_fenced_backslash_repair(self):
        command = 'rg -n ' + BS + '( pattern'
        text = '{"name":"functions.js","namespace":"browser","arguments":{"code":"' + command + '"}}'
        expected = {"name": "functions.js", "namespace": "browser", "arguments": {"code": command}}
        code = '```json' + NL + text + NL + '```'
        for count in range(4):
            outer = native(code)
            outer["namespace"] = "functions"
            with self.subTest(extra_wrappers=count):
                self.assertEqual(transport.decode_transport_envelope(outer), expected if count <= 2 else None)
            wrapper = {"name": "functions.run_officejs" if count % 2 else "run_officejs",
                       "namespace": "functions", "arguments": json.dumps({"code": code})}
            code = json.dumps(wrapper)

    def test_wrapper_namespace_not_inherited_or_used_to_guess_name(self):
        call = {"name": "exec_command", "arguments": {}}
        outer = native(call, encoded=False)
        outer["namespace"] = "functions"
        self.assertEqual(transport.decode_transport_envelope(outer), call)
        for incomplete in ({"namespace": "browser", "arguments": {}},
                           {"namespace": "browser.js", "name": "", "arguments": {}}):
            self.assertIsNone(transport.decode_transport_envelope(native(incomplete)))

    def test_namespaced_script_still_cannot_be_mined_for_json(self):
        call = {"name": "js", "namespace": "browser", "arguments": {"code": "never execute"}}
        text = json.dumps(call)
        for code in ('const call = ' + text + ';', 'return ' + text,
                     'await Excel.run(async ctx => {' + text + '});',
                     '```javascript' + NL + text + NL + '```', text + NL + text):
            with self.subTest(code=code):
                self.assertIsNone(transport.decode_transport_envelope(native(code)))


    def test_native_passthrough_metadata_preserved_without_mutation(self):
        call = {"name": "js", "namespace": "browser", "arguments": {"code": "never execute"}}
        for encoded in (True, False):
            outer = native(json.dumps(call) if encoded else call, encoded=encoded)
            metadata = {"turn_id": "native-turn", "nested": {"values": [None, False, "中文"]}}
            outer["internal_chat_message_metadata_passthrough"] = metadata
            before = copy.deepcopy(outer)
            self.assertEqual(transport.decode_transport_envelope(outer), call)
            self.assertEqual(outer, before)
            self.assertIs(outer["internal_chat_message_metadata_passthrough"], metadata)

    def test_passthrough_metadata_cannot_change_tool_identity_or_arguments(self):
        call = {"name": "exec_command", "arguments": {"cmd": "pwd"}}
        outer = native(call, encoded=False)
        outer["internal_chat_message_metadata_passthrough"] = {
            "turn_id": "native-turn", "name": "wrong_tool", "namespace": "wrong_namespace",
            "arguments": {"cmd": "must not override"}, "input": "must not override"}
        before = copy.deepcopy(outer)
        self.assertEqual(transport.decode_transport_envelope(outer), call)
        self.assertEqual(outer, before)

    def test_passthrough_metadata_allowed_only_at_native_outer_layer(self):
        call = {"name": "exec_command", "arguments": {"cmd": "pwd"}}
        metadata = {"turn_id": "native-turn"}
        bad_final = dict(call, internal_chat_message_metadata_passthrough=metadata)
        self.assertIsNone(transport.decode_transport_envelope(native(bad_final)))
        wrapper = {"name": "functions.run_officejs", "arguments": {"code": call},
                   "internal_chat_message_metadata_passthrough": metadata}
        self.assertIsNone(transport.decode_transport_envelope(native(wrapper)))
        for extra in ("tool_calls", "calls", "another_call"):
            outer = native(call, encoded=False)
            outer["internal_chat_message_metadata_passthrough"] = metadata
            outer[extra] = [call, call]
            self.assertIsNone(transport.decode_transport_envelope(outer))

    def test_native_metadata_does_not_relax_script_rejection(self):
        call = {"name": "exec_command", "arguments": {"cmd": "pwd"}}
        outer = native('const request = ' + json.dumps(call) + ';')
        outer["internal_chat_message_metadata_passthrough"] = {"turn_id": "native-turn"}
        before = copy.deepcopy(outer)
        self.assertIsNone(transport.decode_transport_envelope(outer))
        self.assertEqual(outer, before)

    def test_native_metadata_roundtrip_and_cache_are_lossless(self):
        from collections import OrderedDict
        import excel_upstream

        source = {"tools": [{"type": "function", "name": "shell_command",
                             "parameters": {"type": "object",
                                            "properties": {"command": {"type": "string"}},
                                            "required": ["command"]}}]}
        call = {"name": "shell_command", "arguments": {"command": "Get-ChildItem"}}
        outer = native(json.dumps(call, separators=(",", ":")))
        outer.update(id="fc_codec_metadata_cache", call_id="call_codec_metadata_cache", status="completed")
        outer["internal_chat_message_metadata_passthrough"] = {"turn_id": "native-turn"}
        before = copy.deepcopy(outer)
        with mock.patch.object(excel_upstream, "_native_call_cache", OrderedDict()):
            converted = excel_upstream.extract_native_client_tool_call({"output": [outer]}, source)
            self.assertIsNotNone(converted)
            self.assertEqual(converted["name"], "shell_command")
            self.assertEqual(converted["call_id"], outer["call_id"])
            self.assertEqual(json.loads(converted["arguments"]), call["arguments"])
            self.assertEqual(outer, before)
            remembered = excel_upstream._remembered_native_call(outer["call_id"])
            self.assertEqual(remembered, before)
            remembered["internal_chat_message_metadata_passthrough"]["turn_id"] = "modified copy"
            self.assertEqual(excel_upstream._remembered_native_call(outer["call_id"]), before)
            replay = excel_upstream.translate_input_items(
                [converted, {"type": "function_call_output", "call_id": outer["call_id"], "output": "file.txt"}],
                {"shell_command": "function"})
            self.assertEqual(replay[0], before)
            self.assertEqual(replay[1]["output"], "file.txt")
            self.assertEqual(outer, before)
            outer["internal_chat_message_metadata_passthrough"]["turn_id"] = "changed original after caching"
            self.assertEqual(excel_upstream._remembered_native_call(outer["call_id"]), before)


if __name__ == "__main__":
    unittest.main()
