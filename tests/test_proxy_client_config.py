import unittest

import excel_upstream
from proxy_client_config import ProxyClientConfigService, _model_token_pricing_description


class ReasoningLevelTests(unittest.TestCase):
    def setUp(self):
        self.service = object.__new__(ProxyClientConfigService)

    def _effort_names(self, model_name, raw_efforts):
        levels, _ = self.service._resolve_reasoning_levels(
            "gpt", raw_efforts, model_name=model_name
        )
        return [level["effort"] for level in levels]

    def test_excel_models_never_expose_max(self):
        raw_efforts = ["low", "medium", "high", "xhigh", "max"]
        for model_name in excel_upstream.PUBLIC_MODEL_IDS:
            with self.subTest(model_name=model_name):
                self.assertEqual(
                    self._effort_names(model_name, raw_efforts),
                    list(excel_upstream.EXCEL_MODEL_REASONING_EFFORTS.get(
                        excel_upstream.excel_model_id(model_name),
                        excel_upstream.EXCEL_REASONING_EFFORTS,
                    )),
                )

    def test_other_gpt_56_models_still_expose_max(self):
        self.assertEqual(
            self._effort_names(
                "gpt-5.6-other", ["low", "medium", "high", "xhigh"]
            ),
            ["low", "medium", "high", "xhigh", "max"],
        )

    def test_excel_aliases_have_matching_pricing_descriptions(self):
        for base, alias in excel_upstream.EXCEL_MODEL_ALIASES.items():
            self.assertEqual(_model_token_pricing_description(base), _model_token_pricing_description(alias))
            self.assertIn("ChatGPT subscription", _model_token_pricing_description(base))

    def test_astra_fallback_uses_adapter_reasoning_levels(self):
        for model in ("gpt-6-astra", "gpt-6-astra-excel"):
            self.assertEqual(self._effort_names(model, None), ["medium", "high", "xhigh"])

    def test_new_base_names_sort_before_legacy_aliases(self):
        names = self.service._sorted_catalog_model_names(set(excel_upstream.PUBLIC_MODEL_IDS))
        for base in ("gpt-6-sol", "gpt-6-luna"):
            self.assertLess(names.index(base), names.index(base + "-excel"))

    def test_generated_catalog_describes_both_names_as_excel(self):
        from types import SimpleNamespace
        from unittest import mock

        self.service._config = SimpleNamespace(
            codex_model_context_window=272_000,
            codex_model_auto_compact_token_limit=180_000,
        )
        with (
            mock.patch.object(self.service, "_model_capabilities", return_value=excel_upstream.merge_local_model_capabilities({})),
            mock.patch.object(self.service, "_model_routing_settings", return_value={}),
        ):
            models = {m["slug"]: m for m in self.service._build_codex_model_catalog_payload()["models"]}
        self.assertEqual(set(models), set(excel_upstream.PUBLIC_MODEL_IDS))
        for base, alias in excel_upstream.EXCEL_MODEL_ALIASES.items():
            self.assertEqual(models[base]["description"], models[alias]["description"])
            self.assertIn("OpenAI Excel", models[base]["description"])
            self.assertEqual(models[base]["supported_reasoning_levels"], models[alias]["supported_reasoning_levels"])


if __name__ == "__main__":
    unittest.main()
