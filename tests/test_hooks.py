import json
import sqlite3
import unittest

from proofly_testutil import FakeNode, fixture, load_plugin

load_plugin()
from proofly import claims, hooks, render  # noqa: E402
from proofly.ledger import Ledger  # noqa: E402
from proofly.verifier import RpcError, Verdict  # noqa: E402

RECIPIENT = "0x77b0d999195a301477494de535720b58fd085ab2"
REVERTED = fixture("reverted")["transaction"]["hash"]
USDC30 = fixture("usdc_transfer_30")["transaction"]["hash"]
USDC_OTHER = fixture("usdc_transfer_other")["transaction"]["hash"]
TOOL = "demo_send_payment"


def make_runtime(*, mode="enforce", annotate=True, node=None, min_conf=3):
    if node is None:
        node = FakeNode()
        for name in ("reverted", "usdc_transfer_30", "usdc_transfer_other", "eth_transfer"):
            node.add(name)
    settings = hooks.Settings(rpc_url="https://mainnet.base.org", mode=mode, payment_tools=(TOOL,),
                              min_confirmations=min_conf, timeout_seconds=6.0, annotate_tool_results=annotate)
    ledger = Ledger(lambda: sqlite3.connect(":memory:", check_same_thread=False))
    _LEDGERS.append(ledger)
    runtime = hooks.ProoflyRuntime(settings, ledger_factory=lambda: ledger, transport=node)
    runtime.client.sleep = lambda s: None
    return runtime, node, ledger


_LEDGERS: list = []


def tearDownModule():
    for ledger in _LEDGERS:
        ledger.close()


def payload(*, session="s1", turn="t1", call="c1", amount=5_000_000, tx_hash=REVERTED, result=None, args=None):
    if args is None:
        args = {"to_address": RECIPIENT, "amount_raw": str(amount), "token": "USDC"}
    if result is None:
        result = json.dumps({"status": "success", "message": f"Payment sent successfully: {amount / 1e6:g} USDC.",
                             "tx_hash": tx_hash})
    return dict(tool_name=TOOL, args=args, result=result, session_id=session, turn_id=turn, tool_call_id=call,
                task_id="task-1", conversation_id=session, api_request_id="req-1", duration_ms=12, status="ok",
                error_type=None, error_message=None, telemetry_schema_version="hermes.observer.v1")


def pay(runtime, *, hermes_order=False, **kw):
    data = payload(**kw)
    if hermes_order:  # agent loop: transform inside dispatch, terminal post_tool_call afterwards
        out = runtime.on_transform_tool_result(**data)
        runtime.on_post_tool_call(**dict(data, result=out or data["result"]))
        return out
    runtime.on_post_tool_call(**data)
    return runtime.on_transform_tool_result(**data)


def reply(runtime, text, *, session="s1", turn="t1", **extra):
    kwargs = dict(response_text=text, session_id=session, model="test-model", platform="cli", turn_id=turn)
    kwargs.update(extra)
    if turn is None:
        kwargs.pop("turn_id")
    return runtime.on_transform_llm_output(**kwargs)


CLAIM_5 = (f"Done! I've sent 5 USDC to {RECIPIENT}. The payment was successful (tx {REVERTED}). "
           "Let me know if you need anything else.")


class ToolResultTests(unittest.TestCase):
    def test_post_tool_call_is_observer_only(self):
        runtime, _, _ = make_runtime()
        self.assertIsNone(runtime.on_post_tool_call(**payload()))
        self.assertIsNone(runtime.on_post_tool_call(tool_name="web_search", args={}, result="x"))
        self.assertIsNone(runtime.on_post_tool_call())  # no kwargs at all
        self.assertIsNone(runtime.on_post_tool_call(tool_name=TOOL, args=["not", "a", "dict"], result={"x": 1},
                                                    session_id=None, turn_id=7))

    def test_unrelated_tools_are_untouched(self):
        runtime, node, _ = make_runtime()
        result = json.dumps({"tx_hash": USDC30, "status": "success"})
        self.assertIsNone(runtime.on_transform_tool_result(tool_name="web_search", args={}, result=result,
                                                           session_id="s1", turn_id="t1", tool_call_id="x"))
        self.assertIsNone(runtime.on_transform_tool_result(tool_name=None, result=result))
        self.assertEqual(node.calls, [])

    def test_false_success_tool_result_is_replaced(self):
        runtime, _, ledger = make_runtime()
        out = json.loads(pay(runtime))
        verification = out["proofly_verification"]
        self.assertEqual(verification["verdict"], "REVERTED")
        self.assertEqual(verification["label"], "TRANSACTION REVERTED")
        self.assertIn("This payment is NOT verified on Base", out["proofly_instruction"])
        self.assertIn("not a success message", out["proofly_instruction"])
        self.assertIn("claims success", out["proofly_instruction"])
        self.assertEqual(out["unverified_tool_output"]["tx_hash"], REVERTED)
        self.assertEqual(ledger.recent(session_id="s1")[0]["verdict"], "REVERTED")

    def test_verified_tool_result_keeps_output(self):
        runtime, _, _ = make_runtime()
        out = json.loads(pay(runtime, amount=30_000_000, tx_hash=USDC30))
        self.assertEqual(out["proofly_verification"]["verdict"], "VERIFIED_TRANSFER")
        self.assertEqual(out["tool_output"]["tx_hash"], USDC30)

    def test_success_text_without_hash(self):
        runtime, node, _ = make_runtime()
        out = json.loads(pay(runtime, result="Payment sent successfully! 5 USDC is on its way."))
        self.assertEqual(out["proofly_verification"]["reason"], "no_tx_hash")
        self.assertEqual(out["proofly_verification"]["label"], "UNVERIFIED - NO TRANSACTION HASH")
        self.assertEqual(node.calls, [])

    def test_ambiguous_and_invalid_hashes(self):
        runtime, _, _ = make_runtime()
        text = f"sent! approval {USDC30} and payment {USDC_OTHER}"
        out = json.loads(pay(runtime, result=text))
        self.assertEqual(out["proofly_verification"]["reason"], "ambiguous_tx_hashes")
        out = json.loads(pay(runtime, call="c2", result=json.dumps({"status": "success", "tx_hash": "0xdeadbeef"})))
        self.assertEqual(out["proofly_verification"]["verdict"], "INVALID_HASH")
        # the same hash twice in structured fields is not ambiguous
        out = json.loads(pay(runtime, call="c3", amount=30_000_000,
                             result=json.dumps({"tx_hash": USDC30, "receipt": {"transactionHash": USDC30}})))
        self.assertEqual(out["proofly_verification"]["verdict"], "VERIFIED_TRANSFER")

    def test_rpc_down_is_unverified(self):
        node = FakeNode()

        def down(method, request):
            raise RpcError("network_error", "unreachable")
        node.override = down
        runtime, _, _ = make_runtime(node=node)
        out = json.loads(pay(runtime, amount=30_000_000, tx_hash=USDC30))
        self.assertEqual(out["proofly_verification"]["label"], "UNVERIFIED - RPC UNAVAILABLE")
        text = reply(runtime, f"I sent 30 USDC to {RECIPIENT}, transaction {USDC30}.")
        self.assertIn("UNVERIFIED - RPC UNAVAILABLE", text)
        self.assertNotIn("VERIFIED TRANSFER ON BASE", text)
        self.assertNotIn(f"I sent 30 USDC to {RECIPIENT}", text)

    def test_transform_exception_fails_closed(self):
        runtime, _, _ = make_runtime()

        def broken(*a, **k):
            raise RuntimeError("bug")
        runtime._verify_event = broken
        out = json.loads(pay(runtime))
        self.assertEqual(out["proofly_verification"]["label"], "UNVERIFIED - CHECK FAILED")
        self.assertEqual(runtime.stats["errors"], 1)

    def test_annotate_off_still_records_and_checks_reply(self):
        runtime, _, ledger = make_runtime(annotate=False)
        self.assertIsNone(pay(runtime))
        self.assertEqual(len(ledger.recent(session_id="s1")), 1)
        self.assertIn("TRANSACTION REVERTED", reply(runtime, CLAIM_5))

    def test_warn_mode_tool_instruction(self):
        runtime, _, _ = make_runtime(mode="warn")
        out = json.loads(pay(runtime))
        self.assertIn("say clearly that it is not verified", out["proofly_instruction"])


class ReplyTests(unittest.TestCase):
    def test_no_payment_context_returns_none(self):
        runtime, node, _ = make_runtime()
        self.assertIsNone(reply(runtime, "I sent the report. The transfer of files is complete."))
        self.assertIsNone(reply(runtime, ""))
        self.assertIsNone(runtime.on_transform_llm_output(response_text=None))
        self.assertEqual(node.calls, [])

    def test_case_a_false_success_rewritten(self):
        runtime, _, _ = make_runtime()
        pay(runtime)
        out = reply(runtime, CLAIM_5)
        self.assertNotIn("I've sent 5 USDC", out)
        self.assertNotIn("The payment was successful", out)
        self.assertIn("[Proofly correction: this payment is NOT verified: tx 0x716a...be20 reverted onchain, "
                      "so nothing was transferred.]", out)
        self.assertEqual(out.count("Proofly correction"), 1)  # the adjacent repeat claim is dropped
        self.assertIn("TRANSACTION REVERTED", out)
        self.assertTrue(out.startswith("Done! [Proofly correction"))
        self.assertIn("Let me know if you need anything else.", out)  # unrelated prose kept
        self.assertNotIn("VERIFIED TRANSFER ON BASE", out)
        self.assertIn("https://basescan.org/tx/" + REVERTED, out)

    def test_repeated_claim_later_in_reply_is_marked(self):
        runtime, _, _ = make_runtime()
        pay(runtime)
        text = "I've sent 5 USDC to Alice. Here is your receipt summary. Again: the payment was successful."
        out = reply(runtime, text)
        self.assertEqual(out.count("Proofly correction"), 1)
        self.assertIn("Here is your receipt summary. " + render.REMOVED_CLAIM, out)

    def test_case_b_wrong_payment_flagged(self):
        runtime, _, _ = make_runtime()
        pay(runtime, amount=10_000_000, tx_hash=USDC_OTHER)
        out = reply(runtime, f"Sent 10 USDC to {RECIPIENT}. Transaction: {USDC_OTHER}")
        self.assertIn("MISMATCH - CLAIM NOT SUPPORTED", out)
        self.assertIn("0xd8c9baf0908df28ed858e3b636b174c0c543b595", out)
        self.assertIn("2.115636 USDC", out)
        self.assertNotIn("Sent 10 USDC to", out)

    def test_case_c_verified_kept_with_footer(self):
        runtime, _, _ = make_runtime()
        pay(runtime, amount=30_000_000, tx_hash=USDC30)
        text = f"I've sent 30 USDC to {RECIPIENT}. Transaction hash: {USDC30}."
        out = reply(runtime, text)
        self.assertTrue(out.startswith(text))
        self.assertIn("verified against chain evidence", out)
        self.assertIn("VERIFIED TRANSFER ON BASE", out)
        self.assertIn("30 USDC (raw 30000000)", out)
        self.assertIn("https://basescan.org/tx/" + USDC30, out)

    def test_warn_mode_keeps_text(self):
        runtime, _, _ = make_runtime(mode="warn")
        pay(runtime)
        out = reply(runtime, CLAIM_5)
        self.assertTrue(out.startswith(CLAIM_5))
        self.assertIn("WARNING from Proofly", out)
        self.assertIn("TRANSACTION REVERTED", out)

    def test_hypotheticals_and_quotes_are_not_rewritten(self):
        runtime, _, _ = make_runtime()
        pay(runtime)
        text = ('The wallet said "Payment sent successfully". You should send 5 USDC again if needed. '
                "Did the transfer go through?")
        out = reply(runtime, text)
        self.assertTrue(out.startswith(text))
        self.assertIn("NOT verified", out)  # still annotated: this turn did make a payment call

    def test_session_isolation(self):
        runtime, _, _ = make_runtime()
        pay(runtime, session="A", amount=30_000_000, tx_hash=USDC30)
        self.assertIsNone(reply(runtime, "I sent 30 USDC.", session="B"))
        self.assertIn("VERIFIED TRANSFER ON BASE", reply(runtime, "I sent 30 USDC.", session="A"))

    def test_old_turn_cannot_back_new_claim(self):
        runtime, _, _ = make_runtime()
        pay(runtime, turn="t1", amount=30_000_000, tx_hash=USDC30)  # verified, but t1 never got a reply
        self.assertIsNone(reply(runtime, f"I sent 30 USDC (tx {USDC30}).", turn="t2"))
        pay(runtime, turn="t3", call="c9", amount=30_000_000, tx_hash=USDC30)
        pay(runtime, turn="t4", call="c10")  # a new turn drops t3's pending call
        out = reply(runtime, "I sent the payment successfully.", turn="t4")
        self.assertIn("TRANSACTION REVERTED", out)
        self.assertNotIn("VERIFIED TRANSFER ON BASE", out)

    def test_reply_consumes_events_once(self):
        runtime, _, _ = make_runtime()
        pay(runtime)
        self.assertIsNotNone(reply(runtime, CLAIM_5))
        self.assertIsNone(reply(runtime, CLAIM_5))

    def test_duplicate_calls_are_idempotent(self):
        for hermes_order in (False, True):
            runtime, _, ledger = make_runtime()
            pay(runtime, hermes_order=hermes_order)
            pay(runtime, hermes_order=hermes_order)
            out = reply(runtime, CLAIM_5)
            self.assertEqual(out.count("- TRANSACTION REVERTED"), 1, hermes_order)
            rows = ledger.recent(session_id="s1", limit=20)
            self.assertEqual(len(rows), 1, rows)

    def test_without_tool_call_id_duplicates_collapse(self):
        runtime, _, _ = make_runtime()
        pay(runtime, call="", hermes_order=True)
        out = reply(runtime, CLAIM_5)
        self.assertEqual(out.count("- TRANSACTION REVERTED"), 1)

    def test_final_payload_without_turn_id(self):
        runtime, _, _ = make_runtime()
        pay(runtime)
        self.assertIn("TRANSACTION REVERTED", reply(runtime, CLAIM_5, turn=None))

    def test_executed_only_claim_is_qualified_not_verified(self):
        runtime, _, _ = make_runtime()
        args = {"to_address": RECIPIENT, "amount": "30", "token": "USDC"}  # no decimals: no exact intent
        pay(runtime, args=args, tx_hash=USDC30)
        text = f"I sent 30 USDC to {RECIPIENT}."
        out = reply(runtime, text)
        self.assertIn(text + " [Proofly: executed onchain, but the intended recipient and amount are NOT verified.]",
                      out)
        self.assertIn("ONCHAIN EXECUTED - INTENT NOT VERIFIED", out)
        self.assertNotIn("verified against chain evidence", out)
        self.assertIn("intent_amount_missing_or_inexact", out)

    def test_pending_confirmations_are_qualified(self):
        node = FakeNode(head=55530464)
        node.add("usdc_transfer_30")
        runtime, _, _ = make_runtime(node=node)
        pay(runtime, amount=30_000_000, tx_hash=USDC30)
        out = reply(runtime, f"I sent 30 USDC to {RECIPIENT}.")
        self.assertIn("not yet confirmed", out)
        self.assertIn("PENDING / NOT YET VERIFIED", out)

    def test_hash_in_reply_not_from_tool(self):
        runtime, _, _ = make_runtime()
        pay(runtime, amount=30_000_000, tx_hash=USDC30)
        text = f"Sent 30 USDC. The transfer was confirmed in transaction {REVERTED}."
        out = reply(runtime, text)
        self.assertIn("was not returned by the payment tool", out)
        self.assertIn("TRANSACTION REVERTED", out)
        self.assertNotIn(f"The transfer was confirmed in transaction {REVERTED}.", out)

    def test_internal_error_degrades_to_unverified(self):
        runtime, _, _ = make_runtime()
        pay(runtime, amount=30_000_000, tx_hash=USDC30)

        def broken(*a, **k):
            raise RuntimeError("bug")
        runtime._rewrite_reply = broken
        out = reply(runtime, "I sent 30 USDC.")
        self.assertTrue(out.startswith("I sent 30 USDC."))
        self.assertIn("UNVERIFIED", out)
        self.assertNotIn("VERIFIED TRANSFER", out)

    def test_hook_budget_stays_below_hermes_timeout(self):
        for timeout in (1, 6, 20):
            settings = hooks.Settings(timeout_seconds=float(timeout))
            self.assertLessEqual(settings.hook_budget_seconds, 20.0)


class VerifyToolTests(unittest.TestCase):
    def test_execution_only(self):
        runtime, _, _ = make_runtime()
        out = json.loads(runtime.handle_verify_tool({"tx_hash": USDC30}, session_id="s1"))
        self.assertEqual(out["proofly_verification"]["verdict"], "EXECUTED")
        self.assertIn("not whether it was the payment", out["note"])

    def test_with_expected_transfer(self):
        runtime, _, ledger = make_runtime()
        args = {"tx_hash": USDC30, "expected_transfer": {"type": "erc20", "to_address": RECIPIENT,
                                                         "amount_raw": "30000000",
                                                         "token_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}}
        out = json.loads(runtime.handle_verify_tool(args, session_id="s1"))
        self.assertEqual(out["proofly_verification"]["verdict"], "VERIFIED_TRANSFER")
        self.assertEqual(ledger.recent(session_id="s1")[0]["source"], "proofly_verify_tx")

    def test_bad_inputs_never_raise(self):
        runtime, _, _ = make_runtime()
        self.assertEqual(json.loads(runtime.handle_verify_tool(None))["proofly_verification"]["verdict"],
                         "INVALID_HASH")
        out = json.loads(runtime.handle_verify_tool({"tx_hash": "0x" + "a" * 500}))
        self.assertEqual(out["proofly_verification"]["verdict"], "INVALID_HASH")
        out = json.loads(runtime.handle_verify_tool({"tx_hash": USDC30, "expected_transfer": {"type": "erc20"}}))
        self.assertEqual(out["expected_transfer_error"], "intent_to_address_invalid")
        self.assertEqual(out["proofly_verification"]["verdict"], "EXECUTED")

    def test_verify_tool_call_creates_reply_context(self):
        runtime, _, _ = make_runtime()
        args = {"tx_hash": REVERTED}
        runtime.on_post_tool_call(tool_name=hooks.VERIFY_TOOL, args=args,
                                  result=runtime.handle_verify_tool(args), session_id="s1", turn_id="t1",
                                  tool_call_id="v1")
        out = reply(runtime, "The payment went through successfully.")
        self.assertIn("TRANSACTION REVERTED", out)


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        s = hooks.load_settings(lambda key, default=None: default)
        self.assertEqual((s.rpc_url, s.mode, s.payment_tools, s.min_confirmations, s.timeout_seconds),
                         ("https://mainnet.base.org", "enforce", (), 3, 6.0))
        self.assertTrue(s.annotate_tool_results)
        self.assertEqual(s.warnings, ())

    def test_bad_values(self):
        values = {"rpc_url": "http://insecure.example", "mode": "yolo", "payment_tools": "pay_a, pay_b proofly_verify_tx",
                  "min_confirmations": 0, "timeout_seconds": 999, "annotate_tool_results": "yes"}
        s = hooks.load_settings(lambda key, default=None: values.get(key, default))
        self.assertEqual(s.rpc_url, "http://insecure.example")  # kept: every check reports it, no silent fallback
        self.assertEqual(s.mode, "enforce")
        self.assertEqual(s.payment_tools, ("pay_a", "pay_b"))
        self.assertEqual((s.min_confirmations, s.timeout_seconds), (1, 20.0))
        self.assertTrue(s.annotate_tool_results)
        self.assertEqual(len(s.warnings), 6)
        runtime = hooks.ProoflyRuntime(s, transport=FakeNode())
        self.assertIs(runtime.verify_hash(USDC30, None, runtime.new_deadline()).verdict, Verdict.RPC_ERROR)


class ClaimDetectionTests(unittest.TestCase):
    def test_claims(self):
        for sentence in (f"I've sent 5 USDC to {RECIPIENT}.", "Payment successful ✅",
                         "The transfer went through.", "Your payment of 30 USDC has been completed!",
                         "Transaction confirmed.", "I transferred 0.5 ETH to Bob."):
            self.assertTrue(claims.is_success_claim(sentence), sentence)

    def test_non_claims(self):
        for sentence in ("You should send 10 USDC.", "Once the transfer is confirmed, I'll let you know.",
                         "The transfer failed.", "The payment was not sent.", "Did the payment go through?",
                         "The tool said the payment was sent.", '"Payment sent successfully."', "Sent!",
                         "I will send 10 USDC now.", "Please confirm the transfer.", "The transaction reverted.",
                         "The report was sent to your email.", "I can send 5 USDC if you want."):
            self.assertFalse(claims.is_success_claim(sentence), sentence)

    def test_code_blocks_and_blockquotes_skipped(self):
        text = "```\nSent 5 USDC successfully.\n```\n> The payment was sent.\nOk."
        self.assertEqual(claims.find_success_claims(text), [])

    def test_decimals_do_not_split_sentences(self):
        found = claims.find_success_claims("I sent 2.115636 USDC to Bob. Thanks.")
        self.assertEqual([c.text.strip() for c in found], ["I sent 2.115636 USDC to Bob."])

    def test_replace_spans_keeps_everything_else(self):
        text = "A. I sent 5 USDC. B."
        claim = claims.find_success_claims(text)[0]
        self.assertEqual(claims.replace_spans(text, [(claim.start, claim.end, "[X]")]), "A. [X] B.")

    def test_hash_extraction(self):
        self.assertEqual(claims.tx_hashes_in(f"a {USDC30.upper().replace('0X', '0x')} b {USDC30}"), [USDC30])
        self.assertEqual(claims.tx_hashes_in("0x" + "a" * 63), [])
        self.assertEqual(claims.tx_hashes_in("0x" + "a" * 65), [])
        self.assertEqual(hooks.extract_tx_hashes(json.dumps({"data": {"txHash": USDC30}}))[0], (USDC30,))
        self.assertEqual(hooks.extract_tx_hashes(json.dumps({"block": {"hash": USDC30}}))[1], "no_tx_hash")
        self.assertEqual(hooks.extract_tx_hashes(None)[1], "no_tx_hash")


if __name__ == "__main__":
    unittest.main()
