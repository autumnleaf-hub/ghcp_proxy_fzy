"""Offline execution of real disabled entry definitions; no production initialization."""
import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parent

def definitions(filename, names, **scope):
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8-sig"))
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert {n.name for n in nodes} == set(names)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), filename, "exec"), scope)
    return scope

class DisabledCopilotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = self.enterContext(mock.patch.dict(os.environ, {"GHCP_RESPONSES_UPSTREAM": "sdk", "GHCP_COPILOT_SDK_INGEST_INTERVAL": "1"}))
        self.forbidden = mock.Mock(side_effect=AssertionError("Copilot side effect attempted"))
        self.auth = definitions("auth.py", ["auth_status", "load_access_token", "load_api_key", "load_api_key_payload", "get_api_key", "get_api_base", "begin_device_flow", "ensure_authenticated", "_device_flow", "_refresh_api_key", "_save_access_token"], open=self.forbidden, httpx=self.forbidden, Thread=self.forbidden)
        self.sdk = definitions("copilot_sdk_upstream.py", ["enabled", "responses_upstream", "_get_client", "handle_responses", "models_response", "start_background_scanner", "scan_session_state", "shutdown"], JSONResponse=JSONResponse, CopilotClient=self.forbidden, auth=self.forbidden, threading=self.forbidden, open=self.forbidden, excel_upstream=SimpleNamespace(merge_local_models_payload=lambda _: {"object": "list", "data": [{"id": "gpt-6-astra-excel"}]}), format_translation=SimpleNamespace(openai_error_response=lambda status,message: JSONResponse({"error": {"message": message}},status_code=status)))

    def tearDown(self):
        self.forbidden.assert_not_called()

    def test_status_never_loads_tokens(self):
        status = self.auth["auth_status"]()
        self.assertEqual(status["status"], "disabled")
        self.assertFalse(status["authenticated"])
        self.assertFalse(status["enabled"])

    def test_credential_loaders_are_inert(self):
        self.assertIsNone(self.auth["load_access_token"]())
        self.assertIsNone(self.auth["load_api_key"]())
        self.assertEqual(self.auth["load_api_key_payload"](), {})

    def test_auth_entrypoints_blocked(self):
        for name,args,kwargs in [("get_api_key",(),{}),("get_api_key",(),{"interactive":True}),("get_api_base",(),{}),("begin_device_flow",(),{}),("ensure_authenticated",(),{}),("_device_flow",(),{}),("_refresh_api_key",("synthetic",),{}),("_save_access_token",("synthetic",),{})]:
            with self.subTest(name=name,kwargs=kwargs), self.assertRaisesRegex(RuntimeError, "disabled"):
                self.auth[name](*args,**kwargs)

    def test_environment_cannot_reenable_sdk(self):
        self.assertFalse(self.sdk["enabled"]())
        self.assertEqual(self.sdk["responses_upstream"](), "disabled")

    async def test_sdk_client_cannot_start(self):
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            await self.sdk["_get_client"]()

    async def test_sdk_dispatch_fails_without_credentials_or_client(self):
        response = await self.sdk["handle_responses"](None, {"model":"anything","input":"fixture"})
        self.assertEqual(response.status_code,501)

    async def test_sdk_discovery_is_local(self):
        response = await self.sdk["models_response"]()
        self.assertEqual(json.loads(response.body)["data"][0]["id"],"gpt-6-astra-excel")

    async def test_scanning_and_shutdown_are_inert(self):
        self.assertIsNone(self.sdk["start_background_scanner"](self.forbidden, interval_seconds=0.001))
        self.sdk["scan_session_state"](self.forbidden)
        self.assertIsNone(await self.sdk["shutdown"]())

    def test_sdk_package_import_is_unreachable(self):
        tree=ast.parse((ROOT / "copilot_sdk_upstream.py").read_text(encoding="utf-8"))
        blocks=[n for n in tree.body if isinstance(n,ast.Try) and any(isinstance(x,ast.ImportFrom) and x.module == "copilot" for x in n.body)]
        self.assertEqual(len(blocks),1)
        self.assertIsInstance(blocks[0].body[0],ast.Raise)
        namespace={"__builtins__":dict(vars(__import__("builtins")),__import__=self.forbidden)}
        module=ast.Module(body=[ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0),blocks[0]],type_ignores=[])
        # Future import is compiler metadata, not a runtime import in this isolated block.
        module.body=module.body[1:]
        exec(compile(ast.fix_missing_locations(module),"sdk-disabled-import","exec"),namespace)
        self.assertIsNone(namespace["CopilotClient"])

if __name__ == "__main__":
    unittest.main()
