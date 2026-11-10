"""Opt-in live checks against real Base mainnet RPC.

Run with:  PROOFLY_LIVE=1 python -m unittest discover -s tests -p test_integration.py -v
Optional:  PROOFLY_RPC_URL=https://base-rpc.publicnode.com  (defaults to https://mainnet.base.org)

The hashes are real historical Base mainnet transactions, independently cross-checked on two RPC
providers; block numbers, hashes and timestamps are in tests/fixtures/chain/PROVENANCE.json.
"""

import json
import os
import unittest

from proofly_testutil import FIXTURES, load_plugin

load_plugin()
from proofly import intent as I  # noqa: E402
from proofly import verifier as V  # noqa: E402

LIVE = os.environ.get("PROOFLY_LIVE") == "1"
RPC = os.environ.get("PROOFLY_RPC_URL", V.DEFAULT_RPC_URL)
PROVENANCE = json.loads((FIXTURES / "PROVENANCE.json").read_text())["transactions"]
RECIPIENT = "0x77b0d999195a301477494de535720b58fd085ab2"


def intent(kind, to, amount, token=None):
    value, err = I.parse_intent({"type": kind, "to_address": to, "amount_raw": str(amount), "token_address": token},
                                source="test")
    assert err is None, err
    return value


@unittest.skipUnless(LIVE, "live Base RPC test: set PROOFLY_LIVE=1 to run")
class LiveBaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.verifier = V.Verifier(V.RpcClient(RPC, timeout=10), min_confirmations=3, budget_seconds=30)

    def check(self, name, intent_value=None):
        result = self.verifier.verify(V.ProofRequest(PROVENANCE[name]["tx_hash"], intent_value))
        print(f"\n  [{RPC}] {name}: {result.verdict.value} ({result.reason}), block {result.block_number}, "
              f"{result.confirmations} confirmations")
        return result

    def test_reverted(self):
        r = self.check("reverted", intent("erc20", RECIPIENT, 5_000_000, I.BASE_USDC))
        self.assertIs(r.verdict, V.Verdict.REVERTED)
        self.assertEqual(r.block_number, PROVENANCE["reverted"]["block_number"])

    def test_correct_usdc_payment_verified(self):
        r = self.check("usdc_transfer_30", intent("erc20", RECIPIENT, 30_000_000, I.BASE_USDC))
        self.assertIs(r.verdict, V.Verdict.VERIFIED_TRANSFER)

    def test_wrong_payment_is_mismatch(self):
        r = self.check("usdc_transfer_other", intent("erc20", RECIPIENT, 10_000_000, I.BASE_USDC))
        self.assertIs(r.verdict, V.Verdict.MISMATCH)

    def test_eth_transfer_verified(self):
        p = PROVENANCE["eth_transfer"]
        r = self.check("eth_transfer", intent("native_eth", p["to"], int(p["value_wei"])))
        self.assertIs(r.verdict, V.Verdict.VERIFIED_TRANSFER)

    def test_success_without_intent_is_only_executed(self):
        self.assertIs(self.check("usdc_transfer_30").verdict, V.Verdict.EXECUTED)

    def test_unknown_hash_not_found(self):
        r = self.verifier.verify(V.ProofRequest("0x" + "0" * 63 + "1"))
        print(f"\n  [{RPC}] unknown hash: {r.verdict.value} ({r.reason}) {r.detail[:120]}")
        if RPC == V.DEFAULT_RPC_URL:
            self.assertIs(r.verdict, V.Verdict.NOT_FOUND)
        else:
            # Some providers answer unknown hashes with an error (base-rpc.publicnode.com: "Archive requests
            # require a personal token"). That must surface as unverified, never as found or failed.
            self.assertIn(r.verdict, (V.Verdict.NOT_FOUND, V.Verdict.RPC_ERROR))
        self.assertFalse(r.verified)


if __name__ == "__main__":
    unittest.main()
