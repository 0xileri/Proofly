import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

from proofly_testutil import FakeNode, fixture, load_plugin

plugin = load_plugin()
from proofly import commands, hooks  # noqa: E402
from proofly.ledger import SCHEMA_VERSION, Ledger  # noqa: E402
from proofly.verifier import ProofRequest, RpcClient, Verifier  # noqa: E402
from proofly import intent as I  # noqa: E402

RECIPIENT = "0x77b0d999195a301477494de535720b58fd085ab2"
USDC30 = fixture("usdc_transfer_30")["transaction"]["hash"]
REVERTED = fixture("reverted")["transaction"]["hash"]


def results():
    node = FakeNode()
    node.add("usdc_transfer_30")
    node.add("reverted")
    verifier = Verifier(RpcClient("https://mainnet.base.org", transport=node))
    intent, _ = I.parse_intent({"type": "erc20", "to_address": RECIPIENT, "amount_raw": "30000000",
                                "token_address": I.BASE_USDC}, source="payment_tool_args")
    return verifier.verify(ProofRequest(USDC30, intent)), verifier.verify(ProofRequest(REVERTED))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "data.db"
        self.ledger = Ledger(lambda: sqlite3.connect(self.path, check_same_thread=False))
        self.verified, self.reverted = results()

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_session_scoped_history(self):
        self.ledger.record(self.verified, session_id="session-A", turn_id="t1", source="pay", mode="enforce")
        self.ledger.record(self.reverted, session_id="session-B", turn_id="t9", source="pay", mode="enforce")
        rows_a = self.ledger.recent(session_id="session-A")
        self.assertEqual([r["verdict"] for r in rows_a], ["VERIFIED_TRANSFER"])
        self.assertEqual(rows_a[0]["expected"]["amount_raw"], "30000000")
        self.assertEqual(rows_a[0]["expected"]["intent_source"], "payment_tool_args")
        self.assertEqual(rows_a[0]["observed"]["transfer"]["to"], RECIPIENT)
        self.assertTrue(rows_a[0]["transfer_match"])
        self.assertEqual([r["verdict"] for r in self.ledger.recent(session_id="session-B")], ["REVERTED"])
        self.assertEqual(self.ledger.recent(session_id=""), [])
        self.assertEqual(self.ledger.counts(), {"REVERTED": 1, "VERIFIED_TRANSFER": 1})

    def test_raw_session_ids_are_not_stored(self):
        self.ledger.record(self.verified, session_id="secret-session-id", turn_id="turn-xyz", source="pay",
                           mode="enforce")
        self.ledger.close()
        raw = self.path.read_bytes()
        self.assertNotIn(b"secret-session-id", raw)
        self.assertNotIn(b"turn-xyz", raw)

    def test_survives_restart_and_versioned(self):
        self.ledger.record(self.reverted, session_id="s", turn_id="t", source="pay", mode="warn")
        self.ledger.close()
        reopened = Ledger(lambda: sqlite3.connect(self.path, check_same_thread=False))
        self.assertEqual(reopened.recent(session_id="s")[0]["verdict"], "REVERTED")
        version = sqlite3.connect(self.path).execute(
            "SELECT value FROM proofly_meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(int(version), SCHEMA_VERSION)
        reopened.close()

    def test_newer_schema_is_refused(self):
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE proofly_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO proofly_meta VALUES ('schema_version', '99')")
        conn.commit()
        conn.close()
        with self.assertRaises(RuntimeError):
            Ledger(lambda: sqlite3.connect(self.path)).recent(session_id="s")

    def test_retention_bound(self):
        small = Ledger(lambda: sqlite3.connect(":memory:"), max_rows=10)
        for i in range(25):
            small.record(self.reverted, session_id="s", turn_id=str(i), source="pay", mode="enforce")
        self.assertEqual(sum(small.counts().values()), 10)
        small.close()

    def test_clear_only_own_session(self):
        self.ledger.record(self.verified, session_id="A", turn_id="t", source="pay", mode="enforce")
        self.ledger.record(self.reverted, session_id="B", turn_id="t", source="pay", mode="enforce")
        self.assertEqual(self.ledger.clear_session("A"), 1)
        self.assertEqual(self.ledger.clear_session(""), 0)
        self.assertEqual(self.ledger.recent(session_id="A"), [])
        self.assertEqual(len(self.ledger.recent(session_id="B")), 1)


class SanctionedPathTests(unittest.TestCase):
    def test_open_ledger_uses_plugin_db(self):
        calls = []
        fake = types.ModuleType("plugins.plugin_storage")

        def plugin_db(name, filename="data.db"):
            calls.append((name, filename))
            return sqlite3.connect(":memory:")
        fake.plugin_db = plugin_db
        pkg = types.ModuleType("plugins")
        pkg.plugin_storage = fake
        saved = {k: sys.modules.get(k) for k in ("plugins", "plugins.plugin_storage")}
        sys.modules.update({"plugins": pkg, "plugins.plugin_storage": fake})
        try:
            ledger = plugin._open_ledger()
            self.assertEqual(calls, [])  # lazy: nothing opened at registration
            ledger.counts()
            self.assertEqual(calls, [("proofly", "data.db")])
            ledger.close()
        finally:
            for key, value in saved.items():
                if value is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = value


class CommandTests(unittest.TestCase):
    def setUp(self):
        node = FakeNode()
        for name in ("reverted", "usdc_transfer_30", "usdc_transfer_other"):
            node.add(name)
        settings = hooks.Settings(rpc_url="https://base.example.com/v2/SUPERSECRETKEY", payment_tools=("pay",))
        self.ledger = Ledger(lambda: sqlite3.connect(":memory:", check_same_thread=False))
        self.runtime = hooks.ProoflyRuntime(settings, ledger_factory=lambda: self.ledger, transport=node)

    def tearDown(self):
        self.ledger.close()

    def cmd(self, args, session_id="sess-1"):
        return commands.handle(self.runtime, args, session_id=session_id, platform="cli")

    def test_check_and_history(self):
        out = self.cmd(f"check {REVERTED}")
        self.assertIn("TRANSACTION REVERTED", out)
        self.assertIn("execution check only", out)
        out = self.cmd(f"check {USDC30} to={RECIPIENT} amount_raw=30000000 token=usdc")
        self.assertIn("VERIFIED TRANSFER ON BASE", out)
        history = self.cmd("")
        self.assertIn("VERIFIED_TRANSFER match", history)
        self.assertIn("REVERTED", history)
        self.assertIn("via command", history)
        last = self.cmd("last")
        self.assertIn('"intent_source": "user_command"', json.dumps(self.ledger.recent(session_id="sess-1")[0]["expected"]))
        self.assertIn("VERIFIED_TRANSFER", last)
        self.assertIn("Inspect: https://basescan.org/tx/" + USDC30, last)

    def test_other_sessions_and_secrets_hidden(self):
        self.cmd(f"check {REVERTED}", session_id="other-chat")
        self.assertIn("(none yet)", self.cmd("history"))
        status = self.cmd("status")
        self.assertIn("REVERTED=1", status)  # aggregate counts only
        for text in (status, self.cmd(""), self.cmd("help")):
            self.assertNotIn("SUPERSECRETKEY", text)
        self.assertIn("https://base.example.com", status)
        self.assertNotIn(REVERTED, status)

    def test_bad_input(self):
        self.assertIn("Usage", self.cmd("check"))
        self.assertIn("Could not read the expected transfer", self.cmd(f"check {USDC30} amount=5"))
        self.assertIn("INVALID TX HASH", self.cmd("check 0x1234"))
        self.assertIn("Unknown subcommand", self.cmd("frobnicate"))
        self.assertIn("No session context", self.cmd("history", session_id=""))
        self.assertIn("/proofly check", self.cmd("help"))
        self.assertIsInstance(self.cmd('check "unterminated'), str)

    def test_clear(self):
        self.cmd(f"check {REVERTED}")
        self.cmd(f"check {REVERTED}", session_id="other")
        self.assertIn("Deleted 1", self.cmd("clear"))
        self.assertEqual(len(self.ledger.recent(session_id="other")), 1)


if __name__ == "__main__":
    unittest.main()
