"""Proofly inside real Hermes: discovery, registration, tool dispatch and the held final transform.

Skipped unless Hermes is importable. Run it with the Hermes interpreter, e.g.:

    <hermes-venv>/bin/python -m unittest discover -s tests -p "test_hermes_integration.py" -v

It builds a throwaway HERMES_HOME, installs Proofly and the simulated demo wallet there in the
documented ``<HERMES_HOME>/plugins/<name>/`` layout, enables both in ``config.yaml``, and then uses
Hermes' own code paths: ``discover_plugins`` / ``PluginManager``, ``model_tools.handle_function_call``
(which fires post_tool_call and transform_tool_result), ``agent.turn_finalizer.apply_llm_output_transform``
with the held-transform snapshot (``hold_stream=True``), and ``invoke_plugin_command`` for /proofly.

Chain data comes from the recorded fixtures unless PROOFLY_LIVE=1 (then real Base mainnet RPC).
The only test-side change is swapping Proofly's own RPC transport for the fixture node; no Hermes
code is patched.
"""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

from proofly_testutil import ROOT, FakeNode

try:
    import hermes_cli  # noqa: F401
    HERMES = True
except ImportError:
    HERMES = False

LIVE = os.environ.get("PROOFLY_LIVE") == "1"
RECIPIENT = "0x77b0d999195a301477494de535720b58fd085ab2"
REVERTED = "0x716a6f5fdf191d72233bd8c1b5ddb0ee1de6aae95cb5bfaba566150830cebe20"
USDC_OTHER = "0xbc124c469915c21441056b911c75bf359f3d7082461f2fd64850f62499e58044"
USDC30 = "0xaea5caf798ef5492f22d078e96e5a563357188bc6962fcf5cc7069d6a4b19070"
_TMP = None


def setUpModule():
    global _TMP
    if not HERMES:
        return
    _TMP = tempfile.TemporaryDirectory(prefix="proofly-hermes-")
    home = Path(_TMP.name)
    plugins = home / "plugins"
    shutil.copytree(ROOT, plugins / "proofly",
                    ignore=shutil.ignore_patterns(".git", "tests", "__pycache__", "*.db"))
    shutil.copytree(ROOT / "examples" / "demo-wallet", plugins / "proofly-demo-wallet")
    (home / "config.yaml").write_text(
        "plugins:\n"
        "  enabled: [proofly, proofly-demo-wallet]\n"
        "  entries:\n"
        "    proofly:\n"
        "      settings:\n"
        "        mode: enforce\n"
        "        payment_tools: [demo_send_payment]\n"
    )
    os.environ["HERMES_HOME"] = str(home)


def tearDownModule():
    if _TMP is not None:
        _TMP.cleanup()


@unittest.skipUnless(HERMES, "Hermes is not importable; run with the Hermes interpreter")
class RealHermesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from hermes_cli.plugins import discover_plugins, get_plugin_manager
        discover_plugins(force=True)
        cls.manager = get_plugin_manager()
        transforms = cls.manager._hooks.get("transform_llm_output", [])
        runtimes = [cb.__self__ for cb in transforms if getattr(cb, "__self__", None).__class__.__name__
                    == "ProoflyRuntime"]
        assert runtimes, f"Proofly hooks not registered: {sorted(cls.manager._hooks)}"
        cls.runtime = runtimes[0]
        if not LIVE:
            node = FakeNode()
            for name in ("reverted", "usdc_transfer_30", "usdc_transfer_other", "eth_transfer"):
                node.add(name)
            cls.runtime.client.transport = node

    def dispatch_payment(self, amount, *, session, turn, call):
        import model_tools
        args = {"to_address": RECIPIENT, "amount_raw": str(amount), "token": "USDC"}
        return model_tools.handle_function_call("demo_send_payment", args, "task-1", tool_call_id=call,
                                                session_id=session, turn_id=turn)

    def final_reply(self, text, *, session, turn):
        from agent.turn_finalizer import apply_llm_output_transform
        from hermes_cli.plugins_dispatch import held_stream_transforms
        held = held_stream_transforms()
        agent = types.SimpleNamespace(session_id=session, model="test-model", platform="cli",
                                      _held_transforms=held, _hold_stream=bool(held), _current_turn_id=turn)
        final, transformed, pre = apply_llm_output_transform(agent, text, turn_id=turn)
        return final, transformed, pre

    def test_01_loaded_and_registered(self):
        from hermes_cli.plugins import get_plugin_commands
        from hermes_cli.plugins_dispatch import stream_held
        from tools.registry import registry
        names = set(registry.get_all_tool_names())
        self.assertIn("proofly_verify_tx", names)
        self.assertIn("demo_send_payment", names)
        for hook in ("post_tool_call", "transform_tool_result", "transform_llm_output"):
            self.assertTrue(any(getattr(cb, "__self__", None) is self.runtime for cb in self.manager._hooks[hook]),
                            hook)
        self.assertIn("proofly", get_plugin_commands())
        self.assertTrue(stream_held(), "hold_stream=True should hold this profile's replies")
        self.assertEqual(self.runtime.settings.payment_tools, ("demo_send_payment",))

    def test_02_case_a_false_success(self):
        result = json.loads(self.dispatch_payment(5_000_000, session="sess-1", turn="turn-A", call="call-A"))
        self.assertEqual(result["proofly_verification"]["verdict"], "REVERTED")
        self.assertEqual(result["unverified_tool_output"]["status"], "success")
        claim = f"Done! I've sent 5 USDC to {RECIPIENT}. The payment was successful, transaction {REVERTED}."
        final, transformed, pre = self.final_reply(claim, session="sess-1", turn="turn-A")
        print("\n--- Case A, final reply after Hermes' held transform ---\n" + final)
        self.assertTrue(transformed)
        self.assertEqual(pre, claim)  # Hermes keeps the original only inside the agent (public_pre_transform)
        self.assertNotIn("I've sent 5 USDC", final)
        self.assertIn("reverted onchain", final)
        self.assertIn("TRANSACTION REVERTED", final)
        self.assertNotIn("VERIFIED TRANSFER ON BASE", final)

    def test_03_case_b_wrong_payment(self):
        result = json.loads(self.dispatch_payment(10_000_000, session="sess-1", turn="turn-B", call="call-B"))
        self.assertEqual(result["proofly_verification"]["verdict"], "MISMATCH")
        final, transformed, _ = self.final_reply(f"Sent 10 USDC to {RECIPIENT}. It went through.",
                                                 session="sess-1", turn="turn-B")
        self.assertTrue(transformed)
        self.assertIn("MISMATCH - CLAIM NOT SUPPORTED", final)
        self.assertIn("0xd8c9baf0908df28ed858e3b636b174c0c543b595", final)

    def test_04_case_c_correct_payment(self):
        result = json.loads(self.dispatch_payment(30_000_000, session="sess-1", turn="turn-C", call="call-C"))
        self.assertEqual(result["proofly_verification"]["verdict"], "VERIFIED_TRANSFER")
        text = f"I've sent 30 USDC to {RECIPIENT}. Transaction hash: {USDC30}."
        final, transformed, _ = self.final_reply(text, session="sess-1", turn="turn-C")
        print("\n--- Case C, final reply after Hermes' held transform ---\n" + final)
        self.assertTrue(final.startswith(text))
        self.assertIn("VERIFIED TRANSFER ON BASE", final)

    def test_05_unrelated_reply_untouched(self):
        final, transformed, _ = self.final_reply("The weekly report is attached. I sent it to the team.",
                                                 session="sess-1", turn="turn-D")
        self.assertFalse(transformed)
        self.assertEqual(final, "The weekly report is attached. I sent it to the team.")

    def test_06_verify_tool_through_registry(self):
        import model_tools
        out = json.loads(model_tools.handle_function_call("proofly_verify_tx", {"tx_hash": USDC30}, "task-1",
                                                          tool_call_id="v1", session_id="sess-2", turn_id="turn-V"))
        self.assertEqual(out["proofly_verification"]["verdict"], "EXECUTED")
        final, transformed, _ = self.final_reply("The transaction went through successfully.",
                                                 session="sess-2", turn="turn-V")
        self.assertTrue(transformed)
        self.assertIn("ONCHAIN EXECUTED - INTENT NOT VERIFIED", final)

    def test_07_slash_command_and_ledger_location(self):
        from hermes_cli.plugin_command_dispatch import invoke_plugin_command
        from hermes_cli.plugins import get_plugin_command_handler
        handler = get_plugin_command_handler("proofly")
        out = invoke_plugin_command(handler, "", session_id="sess-1", session_key="k", platform="cli", source=None)
        print("\n--- /proofly ---\n" + out)
        for verdict in ("REVERTED", "MISMATCH", "VERIFIED_TRANSFER"):
            self.assertIn(verdict, out)
        other = invoke_plugin_command(handler, "history", session_id="someone-else", session_key="k2",
                                      platform="telegram", source=None)
        self.assertIn("(none yet)", other)
        home = Path(os.environ["HERMES_HOME"])
        self.assertTrue((home / "plugin-data" / "proofly" / "data.db").is_file())
        self.assertEqual(list((home / "plugins" / "proofly").glob("*.db")), [])


if __name__ == "__main__":
    unittest.main()
