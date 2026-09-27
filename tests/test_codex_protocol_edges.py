import copy
import json
from unittest import mock
import unittest
from tests.test_codex_bridge_regressions import OfflineStreamBase, native_call, stream_events, run_immediate, LOCAL_TOOLS


class ProtocolEdges(OfflineStreamBase):
    def test_opaque_native_metadata_is_not_a_client_argument(self):
        import excel_upstream as bridge
        native = native_call()
        native["provider_metadata"] = {"name": "DO_NOT_DISPATCH", "payload": [1, 2]}
        original = copy.deepcopy(native)
        result = bridge.extract_native_client_tool_calls({"output": [native]}, {"tools": LOCAL_TOOLS})
        self.assertEqual(result[0]["name"], "exec_command")
        self.assertEqual(native, original)
        self.assertEqual(bridge._remembered_native_call(native["call_id"]), original)

    def test_transport_failure_category_has_no_argument_values(self):
        import client_tool_transport as codec
        native = native_call()
        native["arguments"] = json.dumps({"summary": "SECRET_NO_LOG"})
        self.assertEqual(codec.diagnose_transport_envelope(native), "missing_transport_code")
        self.assertIsNone(codec.decode_transport_envelope(native))
        native["arguments"] = json.dumps({"code": "const x = SECRET_NO_LOG;"})
        self.assertEqual(codec.diagnose_transport_envelope(native), "invalid_transport_code_json")

    def test_sparse_terminal_identity_is_restored_once(self):
        native = native_call()
        final = dict(native)
        del final["name"]
        events = self.collect(stream_events(output=[native], terminal_output=[final]))
        terminals = [data["response"] for kind, data in events if kind == "response.completed"]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0]["output"][0]["name"], "exec_command")
        self.assertNotIn("tool_conversion_rejected", json.dumps(events))
        self.assertNotIn("name", final)

    def test_conflicting_identity_never_dispatches(self):
        native = native_call()
        final = {**native, "name": "different_tool"}
        events = self.collect(stream_events(output=[native], terminal_output=[final]))
        self.assert_no_dispatched_tools(events)
        self.assertIn("conflicting_tool_identity", json.dumps(events))

    def test_conflicting_call_id_never_dispatches(self):
        native = native_call()
        final = {**native, "call_id": "other_call"}
        events = self.collect(stream_events(output=[native], terminal_output=[final]))
        self.assert_no_dispatched_tools(events)
        self.assertIn("conflicting_tool_identity", json.dumps(events))

    def test_name_never_borrowed_from_another_call(self):
        native = native_call()
        final = {**native, "id": "other_item", "call_id": "other_call"}
        del final["name"]
        events = self.collect(stream_events(output=[native], terminal_output=[final]))
        self.assert_no_dispatched_tools(events)

    def test_missing_arguments_are_never_reconstructed(self):
        native = native_call()
        final = dict(native)
        del final["arguments"]
        events = self.collect(stream_events(output=[native], terminal_output=[final]))
        self.assert_no_dispatched_tools(events)

    def test_sparse_done_keeps_added_identity_evidence(self):
        import proxy
        native = native_call()
        sparse = dict(native)
        del sparse["name"]
        response = {"status": "completed", "output": [sparse]}
        before = copy.deepcopy(response)
        repaired, reason = proxy._reconcile_excel_tool_response(response, {0: sparse}, [native, sparse], True)
        self.assertIsNone(reason)
        self.assertEqual(repaired["output"][0]["name"], "run_officejs")
        self.assertEqual(response, before)

    def read_nonstream(self, wire):
        import proxy
        class Source:
            async def aiter_bytes(self):
                for i in range(0, len(wire), 13):
                    yield wire[i:i+13]
        return run_immediate(proxy._read_excel_non_streaming_response_payload(Source(), client_body={"tools": LOCAL_TOOLS}))

    def test_nonstream_recovers_the_same_sparse_identity(self):
        native = native_call()
        final = dict(native)
        del final["name"]
        payload = self.read_nonstream(stream_events(output=[native], terminal_output=[final]))
        self.assertEqual(payload["output"][0]["name"], "run_officejs")

    def test_nonstream_conflicts_become_safe_diagnostics(self):
        native = native_call()
        payload = self.read_nonstream(stream_events(output=[native], terminal_output=[{**native, "name": "other"}]))
        self.assertEqual(payload["output"][0]["type"], "message")
        self.assertIn("conflicting_tool_identity", json.dumps(payload))

    def compact(self, response):
        import proxy
        with mock.patch.object(proxy, "parse_json_request", mock.AsyncMock(return_value={"model": "gpt-6-sol", "input": "fixture"})),              mock.patch.object(proxy, "_handle_excel_responses", mock.AsyncMock(return_value=response)),              mock.patch.object(proxy.model_routing_config_service, "resolve_target_model", return_value="gpt-6-sol"):
            return run_immediate(proxy.responses_compact(object()))

    def test_excel_compact_returns_a_compaction_item(self):
        import proxy
        import format_translation as ft
        payload = {"id": "resp_fixture", "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "summary marker"}]}]}
        result = self.compact(proxy.JSONResponse(payload, headers={"x-request-id": "req_fixture"}))
        body = json.loads(result.body)
        self.assertEqual(body["output"][0]["type"], "compaction")
        self.assertEqual(ft.decode_fake_compaction(body["output"][0]["encrypted_content"]), "summary marker")
        self.assertEqual(result.headers["x-request-id"], "req_fixture")

    def test_compact_preserves_failed_and_incomplete_status(self):
        import proxy
        for status in ("failed", "incomplete"):
            original = proxy.JSONResponse({"status": status, "output": []})
            self.assertIs(self.compact(original), original)

    def test_compact_preserves_http_errors(self):
        import proxy
        original = proxy.JSONResponse({"error": {"message": "fixture"}}, status_code=429)
        self.assertIs(self.compact(original), original)




class ToolCatalogDiagnostics(OfflineStreamBase):
    def test_browser_namespace_catalog_matches_exact_tool(self):
        import excel_upstream as bridge
        source = {"tools": [{"type": "namespace", "name": "mcp__cua_repl", "tools": [
            {"type": "function", "name": "js", "parameters": {
                "type": "object", "properties": {"code": {"type": "string"}},
                "required": ["code"], "additionalProperties": False}}]}]}
        native = native_call()
        native["arguments"] = json.dumps({"code": json.dumps({
            "name": "mcp__cua_repl.js", "arguments": {"code": "await cua.getState();"}})})
        result = bridge.extract_native_client_tool_calls({"output": [native]}, source)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "js")
        self.assertEqual(result[0]["namespace"], "mcp__cua_repl")
        self.assertEqual(json.loads(result[0]["arguments"]), {"code": "await cua.getState();"})

    def test_browser_tool_absent_in_request_is_not_authorized_by_history(self):
        import excel_upstream as bridge
        source = {"tools": LOCAL_TOOLS, "input": [{"role": "user",
            "content": "Previous parent catalog had mcp__cua_repl.js"}]}
        native = native_call()
        native["arguments"] = json.dumps({"code": json.dumps({
            "name": "mcp__cua_repl.js", "arguments": {"code": "SECRET_BODY"}})})
        self.assertEqual(bridge.extract_native_client_tool_calls({"output": [native]}, source), [])
        issues = bridge.client_tool_rejection_diagnostics({"output": [native]}, source)
        self.assertEqual(issues[0]["reason"], "unknown_client_tool")
        self.assertEqual(dict(bridge._native_call_cache), {})

    def test_catalog_log_excludes_parameters_descriptions_and_history(self):
        import excel_upstream as bridge
        tools = copy.deepcopy(LOCAL_TOOLS)
        tools[0]["description"] = "SECRET_DESCRIPTION"
        tools[0]["parameters"] = {"const": "SECRET_SCHEMA"}
        source = {"tools": tools, "input": "SECRET_HISTORY"}
        first = bridge.client_tool_catalog_diagnostic(source)
        text = json.dumps(first)
        self.assertNotIn("SECRET", text)
        self.assertIn("exec_command", first["callable_tools"])
        self.assertEqual(first["root_tool_count"], len(tools))
        second = bridge.client_tool_catalog_diagnostic({"tools": list(reversed(tools))})
        self.assertEqual(first["catalog_fingerprint"], second["catalog_fingerprint"])
        none = bridge.client_tool_catalog_diagnostic({"tools": tools, "tool_choice": "none"})
        self.assertEqual(none["callable_tools"], [])
        self.assertEqual(none["tool_choice_kind"], "none")
        self.assertNotEqual(none["catalog_fingerprint"], first["catalog_fingerprint"])

    def test_catalog_log_bounds_large_names_and_counts(self):
        import excel_upstream as bridge
        source = {"tools": [{"type": "function", "name": str(i) + "x" * 300,
                                "parameters": {}} for i in range(300)]}
        result = bridge.client_tool_catalog_diagnostic(source)
        self.assertTrue(result["names_truncated"])
        self.assertEqual(result["callable_tool_count"], 300)
        self.assertLessEqual(len(result["callable_tools"]), 256)
        self.assertLessEqual(sum(map(len, result["callable_tools"])), 16384)

    def test_rejection_trace_contains_effective_request_catalog(self):
        import proxy
        native = native_call()
        native["arguments"] = json.dumps({"code": json.dumps({
            "name": "mcp__cua_repl.js", "arguments": {"code": "SECRET_ARGS"}})})
        plan = mock.Mock(spec=proxy.UpstreamRequestPlan, request_id="offline-catalog-check",
                         resolved_model="offline", trace_context={})
        with self.assertLogs("proxy", level="WARNING") as capture:
            proxy._recoverable_excel_tool_response({"status": "completed", "output": [native]},
                                                   {"tools": LOCAL_TOOLS}, trace_plan=plan)
        record = self.trace_append.call_args.args[0]
        self.assertEqual(record["request_id"], plan.request_id)
        self.assertIn("exec_command", record["tool_catalog"]["callable_tools"])
        self.assertNotIn("mcp__cua_repl.js", record["tool_catalog"]["callable_tools"])
        self.assertFalse(record["dispatched"])
        self.assertNotIn("SECRET_ARGS", json.dumps(record) + str(capture.output))

if __name__ == "__main__":
    unittest.main()


class ToolNamespaceDiagnosticTests(OfflineStreamBase):
    def test_unknown_namespace_is_visible_without_logging_arguments(self):
        import excel_upstream as bridge
        native = native_call()
        native["arguments"] = json.dumps({"code": json.dumps({
            "name": "js", "namespace": "unavailable_browser",
            "arguments": {"code": "SECRET_NOT_A_TOOL_NAME"}})})
        issues = bridge.client_tool_rejection_diagnostics({"output": [native]}, {"tools": LOCAL_TOOLS})
        self.assertEqual(issues[0]["requested_namespace"], "unavailable_browser")
        self.assertEqual(issues[0]["reason"], "unknown_client_tool")
        self.assertNotIn("SECRET_NOT_A_TOOL_NAME", json.dumps(issues))
