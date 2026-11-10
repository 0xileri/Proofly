"""Registration contract: what ``register(ctx)`` installs must match ``plugin.yaml`` exactly.

This drives ``register`` with a recording context. The real-Hermes check is
``test_hermes_integration.py`` plus ``hermes plugins doctor/validate`` (see BUILD_REPORT.md).
"""

import importlib.util
import inspect
import re
import sys
import unittest
from pathlib import Path

from proofly_testutil import ROOT, fixture, load_plugin

plugin = load_plugin()

try:
    import yaml  # PyYAML when available
except ImportError:  # pragma: no cover - minimal reader for the block lists used here
    yaml = None


def read_manifest(path: Path) -> dict:
    text = path.read_text()
    if yaml is not None:
        return yaml.safe_load(text)
    data, key = {}, None
    for line in text.splitlines():
        if re.match(r"^[a-z_]+:", line):
            key, _, value = line.partition(":")
            data[key] = value.strip().strip('"') or []
        elif key and line.strip().startswith("- "):
            data[key].append(line.strip()[2:])
    return data


class RecordingContext:
    def __init__(self, config=None):
        self.config = config or {}
        self.hooks, self.tools, self.commands = [], [], []

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_hook(self, name, callback, *, hold_stream=False):
        self.hooks.append((name, callback, hold_stream))

    def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None, is_async=False,
                      description="", emoji="", override=False):
        self.tools.append((name, toolset, schema, handler, override))

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands.append((name, handler, description, args_hint))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = read_manifest(ROOT / "plugin.yaml")
        self.ctx = RecordingContext()
        plugin.register(self.ctx)

    def test_declared_capabilities_match_registration(self):
        self.assertEqual(sorted(h[0] for h in self.ctx.hooks), sorted(self.manifest["provides_hooks"]))
        self.assertEqual([t[0] for t in self.ctx.tools], self.manifest["provides_tools"])
        self.assertEqual(len(self.ctx.hooks), len(set(h[0] for h in self.ctx.hooks)))

    def test_hold_stream_only_on_final_output(self):
        held = {name: hold for name, _, hold in self.ctx.hooks}
        self.assertEqual(held, {"post_tool_call": False, "transform_tool_result": False,
                                "transform_llm_output": True})

    def test_callbacks_accept_kwargs(self):
        for name, callback, _ in self.ctx.hooks:
            params = inspect.signature(callback).parameters.values()
            self.assertTrue(any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params), name)
        handler = self.ctx.tools[0][3]
        self.assertTrue(any(p.kind is inspect.Parameter.VAR_KEYWORD
                            for p in inspect.signature(handler).parameters.values()))

    def test_tool_and_command(self):
        name, toolset, schema, handler, override = self.ctx.tools[0]
        self.assertEqual((name, toolset, override), ("proofly_verify_tx", "proofly", False))
        self.assertEqual(schema["name"], name)
        self.assertEqual(schema["parameters"]["required"], ["tx_hash"])
        self.assertEqual([c[0] for c in self.ctx.commands], ["proofly"])
        out = self.ctx.commands[0][1]("help", session_id="x", session_key="k", platform="cli", source=None)
        self.assertIn("/proofly check", out)

    def test_manifest_fields(self):
        m = self.manifest
        self.assertEqual(m["name"], "proofly")
        self.assertEqual(m["version"], "1.0.0")
        self.assertIsInstance(m["version"], str)
        self.assertEqual(m["license"], "MIT")
        self.assertTrue((ROOT / "LICENSE").read_text().startswith("MIT License"))
        self.assertRegex(m["requires_hermes"], r"^>=\d+\.\d+\.\d+$")
        self.assertNotIn("capabilities", m)  # no privileged host surfaces requested
        schema = m["config_schema"]
        self.assertEqual(set(schema), {"rpc_url", "mode", "payment_tools", "min_confirmations", "timeout_seconds",
                                       "annotate_tool_results"})
        self.assertEqual(schema["mode"]["choices"], ["enforce", "warn"])
        self.assertEqual(schema["rpc_url"]["default"], "https://mainnet.base.org")
        for spec in schema.values():
            self.assertIn(spec["type"], {"str", "int", "list", "bool"})

    def test_config_wiring(self):
        ctx = RecordingContext({"mode": "warn", "payment_tools": ["send_usdc"], "min_confirmations": 5})
        plugin.register(ctx)
        runtime = ctx.hooks[0][1].__self__
        self.assertEqual((runtime.settings.mode, runtime.settings.payment_tools, runtime.settings.min_confirmations),
                         ("warn", ("send_usdc",), 5))

    def test_registration_makes_no_network_calls_or_state(self):
        runtime = self.ctx.hooks[0][1].__self__
        self.assertIsNone(runtime._ledger)  # ledger opened lazily, not during register()


class DemoWalletTests(unittest.TestCase):
    """The simulated demo tool must only ever return the recorded fixture hashes."""

    def setUp(self):
        path = ROOT / "examples" / "demo-wallet"
        spec = importlib.util.spec_from_file_location("proofly_demo_wallet", path / "__init__.py",
                                                      submodule_search_locations=[str(path)])
        self.module = importlib.util.module_from_spec(spec)
        sys.modules["proofly_demo_wallet"] = self.module
        spec.loader.exec_module(self.module)
        self.manifest = read_manifest(path / "plugin.yaml")

    def test_manifest_matches(self):
        ctx = RecordingContext()
        self.module.register(ctx)
        self.assertEqual([t[0] for t in ctx.tools], self.manifest["provides_tools"])
        self.assertEqual(ctx.hooks, [])

    def test_scripted_hashes_are_the_fixtures(self):
        recorded = {fixture(n)["transaction"]["hash"] for n in ("reverted", "usdc_transfer_other", "usdc_transfer_30")}
        self.assertEqual(set(self.module.SCRIPT.values()), recorded)
        import json
        out = json.loads(self.module.send_payment({"to_address": self.module.RECIPIENT, "amount_raw": "30000000",
                                                   "token": "usdc"}))
        self.assertEqual(out["status"], "success")
        self.assertIn("SIMULATED", out["simulation_notice"])
        self.assertEqual(json.loads(self.module.send_payment({"to_address": "0x" + "1" * 40}))["status"], "error")


if __name__ == "__main__":
    unittest.main()
