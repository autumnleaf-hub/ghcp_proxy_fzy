"""Offline routing-service regression tests; never use the user's configuration.

Run with ./.venv/Scripts/python.exe -B -m unittest -v test_model_alias_routing
(or ./.venv/bin/python on POSIX). No proxy/server imports or network requests.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

# Fail closed even if a future dependency accidentally connects during import.
with mock.patch("socket.socket.connect", side_effect=AssertionError("offline test")), \
     mock.patch("socket.create_connection", side_effect=AssertionError("offline test")), \
     mock.patch("socket.getaddrinfo", side_effect=AssertionError("offline test")):
    import excel_upstream
    import model_routing_config as routing
    from fastapi import HTTPException


ASTRA = "gpt-6-astra-excel"
SOL = "gpt-6-sol-excel"


class ModelAliasRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="model-alias-test-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "routing.json"
        self.service = routing.ModelRoutingConfigService(
            routing.ModelRoutingConfig(config_file=str(self.path))
        )
        for name in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
            patcher = mock.patch(name, side_effect=AssertionError("network is forbidden"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def rule(self, source="alias", target=ASTRA, **extra):
        return {"source_model": source, "target_model": target, **extra}

    def save(self, *rules, **extra):
        return self.service.save_settings({"enabled": True, "mappings": list(rules), **extra})

    def persist(self, payload):
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def assert_bad_request(self, payload):
        before = self.path.read_bytes() if self.path.exists() else None
        with self.assertRaises(HTTPException) as raised:
            self.service.save_settings(payload)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)
        return raised.exception

    def test_missing_configuration_is_disabled_without_writing(self):
        settings = self.service.config_payload()
        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["mappings"], [])
        self.assertEqual(settings["warnings"], [])
        self.assertIsNone(self.service.resolve_target_model("alias"))
        self.assertFalse(self.path.exists())

    def test_grouped_input_trims_lowercases_and_accepts_chinese_comma(self):
        saved = self.save(self.rule(" Alias1 , ALIAS2， 自定义 "))
        self.assertEqual(saved["mappings"][0]["source_model"], "alias1,alias2,自定义")
        for alias in ("alias1", " ALIAS2 ", "自定义"):
            with self.subTest(alias=alias):
                self.assertEqual(self.service.resolve_target_model(alias), ASTRA)
        self.assertIsNone(self.service.resolve_target_model("alias1,alias2"))
        self.assertIsNone(self.service.resolve_target_model("not-listed"))

    def test_arbitrary_aliases_are_literal_not_copilot_heuristics(self):
        names = ("tenant/sonnet-special", "custom_opus", "my haiku route", "team:fast")
        saved = self.save(*(self.rule(name) for name in names))
        self.assertEqual([row["source_model"] for row in saved["mappings"]], list(names))
        for name in names:
            self.assertEqual(self.service.resolve_target_model(name.upper()), ASTRA)
        for name in ("sonnet-special", "claude-sonnet-4.6", "custom-opus"):
            self.assertIsNone(self.service.resolve_target_model(name))

    def test_duplicate_aliases_within_groups_rejected_even_for_same_target(self):
        for aliases in ("a,A", "a， a", "a,b,A"):
            with self.subTest(aliases=aliases):
                self.assert_bad_request({"mappings": [self.rule(aliases)]})

    def test_duplicate_aliases_between_groups_rejected_without_overwriting(self):
        self.save(self.rule("original"))
        for target in (ASTRA, SOL):
            with self.subTest(target=target):
                self.assert_bad_request({"mappings": [
                    self.rule("a,b"), self.rule("c， B", target),
                ]})

    def test_invalid_alias_values_rejected(self):
        invalid = (None, True, 3, [], {}, "", " 	", ",a", "a,", "a,,b", "a， ，b",
                   "x" * (routing.MAX_ROUTING_MODEL_NAME_LENGTH + 1),
                   "a\x00b", "a\nb", "a\u200bb", "a\ud800b")
        for source in invalid:
            with self.subTest(source=repr(source)):
                self.assert_bad_request({"mappings": [self.rule(source)]})

    def test_name_length_boundary_and_long_group_of_short_names(self):
        longest = "a" * routing.MAX_ROUTING_MODEL_NAME_LENGTH
        self.save(self.rule(longest))
        self.assertEqual(self.service.resolve_target_model(longest), ASTRA)
        names = [f"alias-{i}" for i in range(80)]
        self.save(self.rule(",".join(names)))
        self.assertEqual(self.service.resolve_target_model(names[-1]), ASTRA)

    def test_unknown_or_invalid_targets_rejected(self):
        for target in (None, False, 2, [], {}, "", "unknown", "gpt-5.4", "claude-sonnet-4.6",
                       "gpt-excel", "gpt-6-unknown-basispoints", "gpt-6-astra-excel-basispoints",
                       "gpt-6-astra-basispoints-excel", "other/gpt-6-astra",
                       "gpt-6-astra,gpt-6-sol", "x" * 257):
            with self.subTest(target=repr(target)):
                self.assert_bad_request({"mappings": [self.rule(target=target)]})

    def test_invalid_body_collections_and_entries_rejected(self):
        for body in (None, [], "text", True, {"mappings": {}}, {"mappings": "text"},
                     {"mappings": [None]}, {"mappings": [False]}, {"mappings": [{}]}):
            with self.subTest(body=body):
                self.assert_bad_request(body)

    def test_flags_require_json_booleans(self):
        for key in ("enabled", "approval_enabled"):
            for value in ("false", "true", 0, 1, None, [], {}):
                with self.subTest(key=key, value=value):
                    self.assert_bad_request({key: value})

    def test_disabled_routing_does_not_map_but_keeps_rules(self):
        result = self.save(self.rule(), enabled=False)
        self.assertEqual(len(result["mappings"]), 1)
        self.assertIsNone(self.service.resolve_target_model("alias"))
        self.save(self.rule())
        self.assertEqual(self.service.resolve_target_model("alias"), ASTRA)

    def test_available_models_are_exactly_six_upstream_canonical_models(self):
        with mock.patch.dict(routing.MODEL_PRICING, {"gpt-placeholder": {}}):
            service = routing.ModelRoutingConfigService(self.service._config)
            rows = service.config_payload()["available_models"]
        self.assertEqual(len(rows), 6)
        self.assertEqual([row["model"] for row in rows], list(excel_upstream.MODEL_IDS))
        for row in rows:
            self.assertEqual(row["provider"], "codex")
            self.assertTrue(row["model"].endswith("-excel"))
            self.assertEqual(row["label"], row["model"][:-6])

    def test_available_models_follow_model_ids_not_a_hardcoded_list(self):
        with mock.patch.object(excel_upstream, "MODEL_IDS", (SOL,)):
            service = routing.ModelRoutingConfigService(self.service._config)
            self.assertEqual([row["model"] for row in service.config_payload()["available_models"]], [SOL])
            with self.assertRaises(HTTPException):
                service.save_settings({"mappings": [self.rule(target=ASTRA)]})

    def test_all_base_basispoints_excel_targets_are_canonicalized(self):
        for canonical in excel_upstream.MODEL_IDS:
            base = canonical.removesuffix("-excel")
            for target in (base, base + "-basispoints", canonical, " OPENAI/" + base.upper() + " " ):
                with self.subTest(target=target):
                    saved = self.save(self.rule(target=target))
                    self.assertEqual(saved["mappings"][0]["target_model"], canonical)
                    self.assertEqual(self.service.resolve_target_model("alias"), canonical)

    def test_legacy_source_target_keys_and_single_source_configs_load(self):
        self.persist({"enabled": True, "mappings": [
            {"source": "gpt-5.4", "target": "gpt-6-astra-basispoints"},
            {"source_model": "claude-sonnet-4.6", "target_model": "gpt-6-sol"},
        ]})
        self.assertEqual(self.service.resolve_target_model(" GPT-5.4 "), ASTRA)
        self.assertEqual(self.service.resolve_target_model("claude-sonnet-4.6"), SOL)
        self.assertEqual(self.service.load_settings()["warnings"], [])
        saved = self.save({"source": "old,new", "target": "gpt-6-astra"})
        self.assertEqual(saved["mappings"][0]["source_model"], "old,new")

    def test_invalid_modern_fields_do_not_fall_back_to_valid_legacy_keys(self):
        self.assert_bad_request({"mappings": [{"source_model": None, "source": "valid", "target": ASTRA}]})
        self.assert_bad_request({"mappings": [{"source": "valid", "target_model": False, "target": ASTRA}]})

    def test_mapping_is_one_step_including_cycles(self):
        self.save(self.rule("start", ASTRA), self.rule(ASTRA, SOL), self.rule(SOL, ASTRA))
        self.assertEqual(self.service.resolve_target_model("start"), ASTRA)
        self.assertEqual(self.service.resolve_target_model(ASTRA), SOL)
        self.assertEqual(self.service.resolve_target_model(SOL), ASTRA)

    def test_distinct_incoming_model_suffixes_remain_distinct_aliases(self):
        self.save(self.rule("gpt-6-astra", SOL), self.rule(ASTRA, ASTRA), builtin_mappings=[])
        self.assertEqual(self.service.resolve_target_model("gpt-6-astra"), SOL)
        self.assertEqual(self.service.resolve_target_model(ASTRA), ASTRA)
        self.assertIsNone(self.service.resolve_target_model("gpt-6-astra-basispoints"))

    def test_invalid_requested_models_do_not_raise(self):
        self.save(self.rule())
        for value in (None, "", {}, [], False, 2, "a,b", "x" * 257):
            with self.subTest(value=value):
                self.assertIsNone(self.service.resolve_target_model(value))

    def test_obsolete_persisted_rules_and_defaults_do_not_break_loading(self):
        self.persist({"enabled": True, "mappings": [
            self.rule("old", "claude-sonnet-4.6"), self.rule("valid"),
            self.rule("bad", "<script>secret-token</script>"), None,
        ], "claude_code_defaults": {"opus_model": "unsupported-legacy-default"}})
        original = self.path.read_bytes()
        settings = self.service.config_payload()
        self.assertEqual([row["source_model"] for row in settings["mappings"]], ["valid"])
        self.assertEqual(len(settings["warnings"]), 4)
        self.assertNotIn("secret-token", json.dumps(settings))
        self.assertEqual(settings["claude_code_defaults"]["opus_model"], "")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertIsNone(self.service.resolve_target_model("old"))
        self.assertEqual(self.service.resolve_target_model("valid"), ASTRA)
        self.save(self.rule("replacement"))
        self.assertEqual(self.service.resolve_target_model("replacement"), ASTRA)

    def test_invalid_legacy_sections_are_ignored_independently(self):
        self.persist({"enabled": True, "mappings": [self.rule()],
                      "approval_enabled": "false", "approval_mappings": "obsolete",
                      "claude_code_defaults": []})
        settings = self.service.load_settings()
        self.assertEqual(len(settings["warnings"]), 3)
        self.assertFalse(settings["approval_enabled"])
        self.assertEqual(settings["approval_mappings"], [])
        self.assertEqual(self.service.resolve_target_model("alias"), ASTRA)

    def test_malformed_files_load_disabled_with_safe_warnings(self):
        for contents in ("{ secret-token", "[]", "null", "true"):
            with self.subTest(contents=contents):
                self.path.write_text(contents, encoding="utf-8")
                settings = self.service.config_payload()
                self.assertFalse(settings["enabled"])
                self.assertTrue(settings["warnings"])
                self.assertNotIn("secret-token", str(settings["warnings"]))
                self.assertEqual(self.path.read_text(encoding="utf-8"), contents)

    def test_approval_and_defaults_preserved_when_new_ui_omits_them(self):
        self.save(self.rule(), enabled=False, approval_enabled=True,
                  approval_mappings=[self.rule("gpt-5.4", "claude-sonnet-4.6")],
                  claude_code_defaults={"sonnet_model": "claude-sonnet-4.6"})
        self.assertIsNone(self.service.resolve_target_model("alias"))
        self.assertEqual(self.service.resolve_approval_target_model("GPT-5.4"), "claude-sonnet-4.6")
        saved = self.save(self.rule("updated"))
        self.assertTrue(saved["approval_enabled"])
        self.assertEqual(saved["claude_code_defaults"]["sonnet_model"], "claude-sonnet-4.6")
        self.assertEqual(self.service.resolve_approval_target_model("gpt-5.4"), "claude-sonnet-4.6")
        self.save(self.rule(), approval_enabled=False)
        self.assertIsNone(self.service.resolve_approval_target_model("gpt-5.4"))

    def test_approval_duplicates_and_unknown_targets_still_rejected(self):
        for rows in ([self.rule(target="unknown")], [self.rule("a"), self.rule("A")],
                     [self.rule(None)], [self.rule("")]):
            self.assert_bad_request({"approval_mappings": rows})
        self.assert_bad_request({"claude_code_defaults": {"opus_model": "unknown"}})

    def test_legacy_fallback_metadata_preserved_but_bps_needs_no_fallback(self):
        result = self.save(self.rule("a,b", compact_fallback_model="gpt-5.4"))
        self.assertEqual(result["mappings"][0]["compact_fallback_model"], "gpt-5.4")
        self.assertIsNone(self.service.resolve_compact_fallback_model("b"))
        for target in (False, "unknown", "claude-sonnet-4.6"):
            self.assert_bad_request({"mappings": [self.rule(compact_fallback_model=target)]})

    def test_atomic_replace_uses_complete_fsynced_same_directory_file(self):
        self.save(self.rule("old"))
        original = self.path.read_bytes()
        real_replace = os.replace
        def inspect_replace(source, destination):
            self.assertEqual(Path(source).parent, self.path.parent)
            self.assertEqual(Path(destination), self.path)
            self.assertEqual(self.path.read_bytes(), original)
            document = json.loads(Path(source).read_text(encoding="utf-8"))
            self.assertEqual(document["mappings"][0]["source_model"], "new")
            self.assertNotIn("warnings", document)
            self.assertNotIn("available_models", document)
            return real_replace(source, destination)
        with mock.patch.object(routing.os, "replace", side_effect=inspect_replace) as replace, \
             mock.patch.object(routing.os, "fsync", wraps=os.fsync) as fsync:
            self.save(self.rule("new"))
        replace.assert_called_once()
        fsync.assert_called_once()
        self.assertEqual(self.service.resolve_target_model("new"), ASTRA)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_failed_replace_keeps_original_and_cleans_temporary_file(self):
        self.save(self.rule("old"))
        original = self.path.read_bytes()
        with mock.patch.object(routing.os, "replace", side_effect=OSError("simulated")):
            with self.assertRaises(OSError):
                self.save(self.rule("new"))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_failed_serialization_never_truncates_destination(self):
        self.save(self.rule("old"))
        original = self.path.read_bytes()
        def partial_dump(payload, stream, **kwargs):
            stream.write('{"partial":')
            raise OSError("simulated disk failure")
        with mock.patch.object(routing.json, "dump", side_effect=partial_dump):
            with self.assertRaises(OSError):
                self.save(self.rule("new"))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_nested_directory_and_relative_config_path(self):
        path = self.path.parent / "nested" / "routing.json"
        previous_directory = os.getcwd()
        try:
            # Windows temporary storage may be on a different drive than the repo.
            os.chdir(self.path.parent)
            service = routing.ModelRoutingConfigService(
                routing.ModelRoutingConfig(config_file=os.path.join("nested", "routing.json"))
            )
            service.save_settings({"enabled": True, "mappings": [self.rule()]})
            self.assertEqual(service.resolve_target_model("alias"), ASTRA)
            self.assertEqual(list(path.parent.iterdir()), [path])
        finally:
            os.chdir(previous_directory)

    def test_concurrent_readers_and_multiple_service_writers_never_see_partial_json(self):
        self.save(self.rule("initial"), approval_enabled=True,
                  approval_mappings=[self.rule("approval", "gpt-5.4")])
        barrier = threading.Barrier(6)
        def worker(index):
            service = routing.ModelRoutingConfigService(self.service._config)
            barrier.wait(timeout=10)
            for iteration in range(20):
                if index < 3:
                    alias = f"writer-{index}-{iteration}"
                    result = service.save_settings({"enabled": True, "mappings": [self.rule(alias)]})
                    self.assertEqual(result["mappings"][0]["source_model"], alias)
                else:
                    result = service.load_settings()
                self.assertTrue(result["enabled"])
                self.assertTrue(result["approval_enabled"])
                self.assertEqual(len(result["mappings"]), 1)
                self.assertEqual(result["warnings"], [])
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(worker, range(6)))
        self.assertTrue(json.loads(self.path.read_text(encoding="utf-8"))["enabled"])
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


if __name__ == "__main__":
    unittest.main()
