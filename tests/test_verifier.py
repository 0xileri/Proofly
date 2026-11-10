import copy
import http.server
import json
import threading
import time
import unittest

from proofly_testutil import FakeNode, fixture, load_plugin

load_plugin()
from proofly import intent as I  # noqa: E402
from proofly import verifier as V  # noqa: E402

RECIPIENT = "0x77b0d999195a301477494de535720b58fd085ab2"
OTHER = "0xd8c9baf0908df28ed858e3b636b174c0c543b595"
USDC = I.BASE_USDC
REVERTED = fixture("reverted")["transaction"]["hash"]
USDC30 = fixture("usdc_transfer_30")["transaction"]["hash"]
USDC_OTHER = fixture("usdc_transfer_other")["transaction"]["hash"]
ETH = fixture("eth_transfer")["transaction"]["hash"]
ETH_TO = "0xf2d030657275d825114025fa791ea3f38214909d"
ETH_FROM = "0x21854e24e7a657bf07f3bd282d5baf0d56e6d518"
ETH_WEI = 1185524242299554


def erc20(to=RECIPIENT, amount=30_000_000, token=USDC, sender=None):
    intent, err = I.parse_intent({"type": "erc20", "to_address": to, "amount_raw": str(amount),
                                  "token_address": token, "from_address": sender}, source="test")
    assert err is None, err
    return intent


def native(to=ETH_TO, amount=ETH_WEI, sender=None):
    intent, err = I.parse_intent({"type": "native_eth", "to_address": to, "amount_raw": str(amount),
                                  "from_address": sender}, source="test")
    assert err is None, err
    return intent


def make(node, **kw):
    client = V.RpcClient("https://mainnet.base.org", transport=node, sleep=lambda s: None)
    return V.Verifier(client, **kw)


def run(node, tx_hash, intent=None, **kw):
    return make(node, **kw).verify(V.ProofRequest(tx_hash, intent))


class HashAndUrlTests(unittest.TestCase):
    def test_hash_normalization(self):
        self.assertEqual(V.normalize_tx_hash(USDC30.upper().replace("0X", "0x")), USDC30)
        self.assertEqual(V.normalize_tx_hash(" " + USDC30 + "\n"), USDC30)
        for bad in (USDC30[:-1], USDC30 + "0", USDC30[2:], "0X" + USDC30[2:], "0x" + "z" * 64, None, 12, ""):
            self.assertIsNone(V.normalize_tx_hash(bad), bad)

    def test_rpc_url_validation(self):
        self.assertEqual(V.validate_rpc_url("https://mainnet.base.org")[1], None)
        self.assertEqual(V.validate_rpc_url("https://node.example:8443/v2/key")[1], None)
        bad = {
            "http://mainnet.base.org": "rpc_url_not_https",
            "http://127.0.0.1:8545": "rpc_url_not_https",
            "https://user:pass@mainnet.base.org": "rpc_url_has_credentials",
            "https://token@mainnet.base.org": "rpc_url_has_credentials",
            "https://": "rpc_url_no_host",
            "https://mainnet.base.org/#x": "rpc_url_has_fragment",
            "https://main net.base.org": "rpc_url_has_whitespace",
            "https://mainnet.base.org:99999": "rpc_url_unparseable",
            "ftp://mainnet.base.org": "rpc_url_not_https",
            "": "rpc_url_missing_or_too_long",
            "https://a.org/" + "x" * 600: "rpc_url_missing_or_too_long",
        }
        for url, reason in bad.items():
            self.assertEqual(V.validate_rpc_url(url), (None, reason), url)

    def test_endpoint_label_hides_path_and_query(self):
        self.assertEqual(V.endpoint_label("https://base.example.com/v2/SECRETKEY?x=1"), "https://base.example.com")
        self.assertEqual(V.endpoint_label("https://node.example:8443/k"), "https://node.example:8443")


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.node = FakeNode()
        for name in ("reverted", "usdc_transfer_30", "usdc_transfer_other", "eth_transfer"):
            self.node.add(name)

    def test_invalid_hash_makes_no_rpc_call(self):
        for bad in ("0x1234", "hello", USDC30 + "ff", None):
            r = run(self.node, bad)
            self.assertIs(r.verdict, V.Verdict.INVALID_HASH)
            self.assertEqual(r.execution_status, "unknown")  # never "reverted"/failed
        self.assertEqual(self.node.calls, [])

    def test_not_found_is_not_failure(self):
        r = run(self.node, "0x" + "ab" * 32, erc20())
        self.assertIs(r.verdict, V.Verdict.NOT_FOUND)
        self.assertEqual(r.execution_status, "not_found")
        self.assertIsNone(r.transfer_match)

    def test_pending_without_receipt(self):
        tx = copy.deepcopy(fixture("usdc_transfer_30")["transaction"])
        tx.update(blockHash=None, blockNumber=None)
        node = FakeNode(txs={tx["hash"]: (tx, None)})
        r = run(node, tx["hash"], erc20())
        self.assertIs(r.verdict, V.Verdict.PENDING)
        self.assertEqual(r.reason, "awaiting_inclusion")
        r = run(FakeNode(txs={USDC30: (fixture("usdc_transfer_30")["transaction"], None)}), USDC30)
        self.assertEqual((r.verdict, r.reason), (V.Verdict.PENDING, "receipt_unavailable"))

    def test_reverted(self):
        r = run(self.node, REVERTED)
        self.assertEqual((r.verdict, r.execution_status, r.transfer_match), (V.Verdict.REVERTED, "reverted", None))
        r = run(self.node, REVERTED, erc20(amount=5_000_000))
        self.assertIs(r.verdict, V.Verdict.REVERTED)
        self.assertFalse(r.transfer_match)
        self.assertEqual(r.block_number, 55530464)

    def test_success_without_intent_is_only_executed(self):
        for h in (USDC30, ETH, USDC_OTHER):
            r = run(self.node, h)
            self.assertIs(r.verdict, V.Verdict.EXECUTED)
            self.assertIsNone(r.transfer_match)
            self.assertFalse(r.verified)

    def test_native_eth(self):
        r = run(self.node, ETH, native())
        self.assertIs(r.verdict, V.Verdict.VERIFIED_TRANSFER)
        self.assertEqual(r.evidence["matched_transfer"]["amount_raw"], str(ETH_WEI))
        self.assertIs(run(self.node, ETH, native(sender=ETH_FROM)).verdict, V.Verdict.VERIFIED_TRANSFER)
        wrong_to = run(self.node, ETH, native(to=RECIPIENT))
        self.assertEqual((wrong_to.verdict, wrong_to.transfer_match), (V.Verdict.MISMATCH, False))
        self.assertEqual(wrong_to.evidence["differences"][0]["field"], "recipient")
        wrong_amt = run(self.node, ETH, native(amount=ETH_WEI + 1))
        self.assertEqual(wrong_amt.evidence["differences"][0]["field"], "amount_raw")
        wrong_from = run(self.node, ETH, native(sender=RECIPIENT))
        self.assertEqual(wrong_from.evidence["differences"][0]["field"], "sender")
        # a contract call is not a provable direct transfer
        self.assertIs(run(self.node, USDC30, native(to=USDC, amount=1)).verdict, V.Verdict.UNSUPPORTED)

    def test_erc20_match_and_sender(self):
        r = run(self.node, USDC30, erc20())
        self.assertIs(r.verdict, V.Verdict.VERIFIED_TRANSFER)
        self.assertTrue(r.transfer_match)
        self.assertEqual(r.explorer_url, "https://basescan.org/tx/" + USDC30)
        sender = "0x1a42650a7a4c4fbbd7e1c0879259caf73ffc1ff9"
        self.assertIs(run(self.node, USDC30, erc20(sender=sender)).verdict, V.Verdict.VERIFIED_TRANSFER)
        bad_sender = run(self.node, USDC30, erc20(sender=OTHER))
        self.assertIs(bad_sender.verdict, V.Verdict.MISMATCH)
        self.assertEqual(bad_sender.evidence["differences"][0]["field"], "sender")

    def test_erc20_mismatches(self):
        # expected some other token: the USDC transfer to the same recipient is the wrong token
        wrong_token = run(self.node, USDC30, erc20(token="0x" + "1" * 40))
        self.assertEqual((wrong_token.verdict, wrong_token.reason), (V.Verdict.MISMATCH, "wrong_token_contract"))
        self.assertEqual(wrong_token.evidence["differences"][0]["observed"], USDC)
        # expected token, but nothing at all goes to the recipient in that token or any other
        nothing = run(self.node, USDC_OTHER, erc20(token="0x" + "1" * 40))
        self.assertEqual(nothing.reason, "no_transfer_from_expected_token")
        wrong_to = run(self.node, USDC30, erc20(to=OTHER))
        self.assertIs(wrong_to.verdict, V.Verdict.MISMATCH)
        self.assertEqual(wrong_to.evidence["differences"][0],
                         {"field": "recipient", "expected": OTHER, "observed": RECIPIENT})
        wrong_amount = run(self.node, USDC30, erc20(amount=30_000_001))
        self.assertEqual(wrong_amount.evidence["differences"][0]["field"], "amount_raw")
        # the demo "wrong payment": a real successful tx that paid someone else a different amount
        unrelated = run(self.node, USDC_OTHER, erc20())
        self.assertIs(unrelated.verdict, V.Verdict.MISMATCH)
        fields = [d["field"] for d in unrelated.evidence["differences"]]
        self.assertEqual(fields, ["recipient", "amount_raw"])
        # ERC-20 intent against a plain ETH transfer: no Transfer event at all
        self.assertEqual(run(self.node, ETH, erc20()).reason, "no_transfer_from_expected_token")

    def _with_receipt(self, mutate):
        data = copy.deepcopy(fixture("usdc_transfer_30"))
        mutate(data["transaction"], data["receipt"])
        return FakeNode(txs={USDC30: (data["transaction"], data["receipt"])})

    def test_lookalike_transfer_from_wrong_emitter(self):
        fake_token = "0x" + "fa" * 20

        def mutate(tx, rc):
            rc["logs"][0]["address"] = fake_token  # same topics/data, different contract
        r = run(self._with_receipt(mutate), USDC30, erc20())
        self.assertEqual((r.verdict, r.reason), (V.Verdict.MISMATCH, "wrong_token_contract"))
        self.assertEqual(r.evidence["differences"][0]["observed"], fake_token)

    def test_malformed_log_is_unsupported(self):
        def short_data(tx, rc):
            rc["logs"][0]["data"] = rc["logs"][0]["data"][:-2]
        def extra_topic(tx, rc):
            rc["logs"][0]["topics"].append("0x" + "00" * 32)  # ERC-721 shaped
        def dirty_topic(tx, rc):
            rc["logs"][0]["topics"][2] = "0x" + "ff" * 12 + rc["logs"][0]["topics"][2][26:]
        for mutate in (short_data, extra_topic, dirty_topic):
            r = run(self._with_receipt(mutate), USDC30, erc20())
            self.assertEqual((r.verdict, r.reason), (V.Verdict.UNSUPPORTED, "malformed_transfer_log"), mutate)

    def test_ambiguous_split_transfers(self):
        def dup(tx, rc):
            rc["logs"].append(copy.deepcopy(rc["logs"][0]))
        r = run(self._with_receipt(dup), USDC30, erc20())
        self.assertEqual((r.verdict, r.reason), (V.Verdict.UNSUPPORTED, "ambiguous_multiple_transfers"))

    def test_confirmation_policy(self):
        node = FakeNode(head=55530464)  # included in the head block: 1 confirmation
        node.add("usdc_transfer_30")
        r = run(node, USDC30, erc20(), min_confirmations=3)
        self.assertEqual((r.verdict, r.reason, r.transfer_match, r.confirmations),
                         (V.Verdict.PENDING, "insufficient_confirmations", True, 1))
        self.assertIs(run(node, USDC30, erc20(), min_confirmations=1).verdict, V.Verdict.VERIFIED_TRANSFER)
        lagging = FakeNode(head=55530400)  # head behind the receipt (load-balanced RPC)
        lagging.add("usdc_transfer_30")
        self.assertEqual(run(lagging, USDC30, erc20()).confirmations, 0)

    def test_deposit_transactions_unsupported(self):
        def deposit(tx, rc):
            tx["type"] = "0x7e"
        self.assertEqual(run(self._with_receipt(deposit), USDC30, erc20()).reason, "deposit_transaction")

    def test_non_base_chain_request_makes_no_rpc_call(self):
        node = FakeNode()
        r = make(node).verify(V.ProofRequest(USDC30, None, chain_id=1))
        self.assertEqual((r.verdict, r.reason), (V.Verdict.UNSUPPORTED, "chain_unsupported"))
        self.assertEqual(node.calls, [])


class RpcFailureTests(unittest.TestCase):
    def setUp(self):
        self.node = FakeNode()
        self.node.add("usdc_transfer_30")

    def assertRpcError(self, result, reason):
        self.assertIs(result.verdict, V.Verdict.RPC_ERROR)
        self.assertEqual(result.reason, reason)
        self.assertFalse(result.verified)

    def test_chain_id_mismatch(self):
        self.node.chain_id = "0x1"
        self.assertRpcError(run(self.node, USDC30, erc20()), "chain_id_mismatch")
        self.assertEqual(self.node.calls, ["eth_chainId"])

    def test_timeout_retried_once_then_error(self):
        def boom(method, request):
            raise V.RpcError("timeout", "slow")
        self.node.override = boom
        self.assertRpcError(run(self.node, USDC30, erc20()), "timeout")
        self.assertEqual(self.node.calls, ["eth_chainId", "eth_chainId"])

    def test_http_429(self):
        def limited(method, request):
            raise V.RpcError("rate_limited", "HTTP 429")
        self.node.override = limited
        self.assertRpcError(run(self.node, USDC30), "rate_limited")

    def test_bad_and_truncated_json(self):
        for body in (b"<html>oops</html>", b'{"jsonrpc":"2.0","id":1,"result":"0x21', b"", b"\xff\xfe"):
            self.node.override = lambda m, r, body=body: body
            self.assertRpcError(run(self.node, USDC30), "bad_json")

    def test_rpc_error_object_and_bad_ids(self):
        self.node.override = lambda m, r: json.dumps({"jsonrpc": "2.0", "id": r["id"],
                                                      "error": {"code": -32000, "message": "boom"}}).encode()
        self.assertRpcError(run(self.node, USDC30), "rpc_error")
        self.node.override = lambda m, r: json.dumps({"jsonrpc": "2.0", "id": 999, "result": "0x2105"}).encode()
        self.assertRpcError(run(self.node, USDC30), "bad_response")
        self.node.override = lambda m, r: json.dumps([{"id": r["id"], "result": "0x2105"}]).encode()
        self.assertRpcError(run(self.node, USDC30), "bad_response")
        self.node.override = lambda m, r: json.dumps({"jsonrpc": "2.0", "id": r["id"],
                                                      "error": {"code": -32005, "message": "limit"}}).encode()
        self.assertRpcError(run(self.node, USDC30), "rate_limited")

    def _mutated(self, mutate):
        data = copy.deepcopy(fixture("usdc_transfer_30"))
        mutate(data["transaction"], data["receipt"])
        return FakeNode(txs={USDC30: (data["transaction"], data["receipt"])})

    def test_inconsistent_responses(self):
        mutations = {
            "block hash differs": lambda tx, rc: rc.update(blockHash="0x" + "11" * 32),
            "block number differs": lambda tx, rc: rc.update(blockNumber="0x1"),
            "receipt hash differs": lambda tx, rc: rc.update(transactionHash="0x" + "22" * 32),
            "tx hash differs": lambda tx, rc: tx.update(hash="0x" + "33" * 32),
            "tx has no block": lambda tx, rc: tx.update(blockNumber=None, blockHash=None),
        }
        for name, mutate in mutations.items():
            self.assertRpcError(run(self._mutated(mutate), USDC30, erc20()), "inconsistent_response")
        node = FakeNode(txs={USDC30: (None, fixture("usdc_transfer_30")["receipt"])})
        self.assertRpcError(run(node, USDC30), "inconsistent_response")

    def test_malformed_fields(self):
        for mutate in (lambda tx, rc: rc.update(status="0x2"), lambda tx, rc: rc.update(status=None),
                       lambda tx, rc: rc.update(logs="nope"), lambda tx, rc: rc.update(blockNumber="12")):
            r = run(self._mutated(mutate), USDC30, erc20())
            self.assertIs(r.verdict, V.Verdict.RPC_ERROR)
        node = FakeNode()
        node.override = lambda m, r: (json.dumps({"jsonrpc": "2.0", "id": r["id"], "result": "a string"}).encode()
                                      if m == "eth_getTransactionByHash" else None)
        self.assertRpcError(run(node, USDC30), "bad_response")

    def test_deadline(self):
        clock = [0.0]
        node = FakeNode()
        node.add("usdc_transfer_30")

        def slow(method, request):
            clock[0] += 5.0
            return None
        node.override = slow
        client = V.RpcClient("https://mainnet.base.org", transport=node, clock=lambda: clock[0],
                             sleep=lambda s: None)
        verifier = V.Verifier(client, budget_seconds=8.0, clock=lambda: clock[0])
        self.assertRpcError(verifier.verify(V.ProofRequest(USDC30, erc20())), "deadline_exceeded")

    def test_invalid_url_never_calls_transport(self):
        node = FakeNode()
        client = V.RpcClient("http://mainnet.base.org", transport=node)
        r = V.Verifier(client).verify(V.ProofRequest(USDC30))
        self.assertRpcError(r, "invalid_rpc_url")
        self.assertEqual(node.calls, [])

    def test_unexpected_exception_is_unverified(self):
        def explode(method, request):
            raise KeyError("surprise")
        self.node.override = explode
        self.assertRpcError(run(self.node, USDC30, erc20()), "internal_error")


class _Handler(http.server.BaseHTTPRequestHandler):
    behaviour = "ok"
    seen_headers: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        type(self).seen_headers.append({k.lower(): v for k, v in self.headers.items()})
        mode = type(self).behaviour
        if mode == "redirect":
            self.send_response(307)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()
            return
        if mode == "429":
            self.send_response(429)
            self.end_headers()
            return
        if mode == "slow":
            time.sleep(1.5)
        payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": "0x2105"}).encode()
        if mode == "huge":
            payload = b" " * 5000 + payload
        if mode == "truncated":
            payload = payload[:-5]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class UrllibTransportTests(unittest.TestCase):
    """The real transport against a local HTTP server (plain HTTP is used only at this layer;
    the client itself refuses non-HTTPS URLs)."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def call(self, behaviour, timeout=3.0, max_bytes=V.MAX_RESPONSE_BYTES):
        _Handler.behaviour = behaviour
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}).encode()
        return V.urllib_transport(self.url, body, timeout, max_bytes=max_bytes)

    def test_ok_and_headers(self):
        _Handler.seen_headers.clear()
        self.assertIn(b"0x2105", self.call("ok"))
        headers = _Handler.seen_headers[-1]
        self.assertEqual(headers["user-agent"], V.USER_AGENT)
        self.assertNotIn("authorization", headers)
        self.assertNotIn("cookie", headers)

    def test_redirect_refused(self):
        with self.assertRaises(V.RpcError) as ctx:
            self.call("redirect")
        self.assertEqual(ctx.exception.code, "redirect_refused")

    def test_429(self):
        with self.assertRaises(V.RpcError) as ctx:
            self.call("429")
        self.assertEqual(ctx.exception.code, "rate_limited")

    def test_timeout(self):
        with self.assertRaises(V.RpcError) as ctx:
            self.call("slow", timeout=0.3)
        self.assertEqual(ctx.exception.code, "timeout")

    def test_size_limit(self):
        with self.assertRaises(V.RpcError) as ctx:
            self.call("huge", max_bytes=1000)
        self.assertEqual(ctx.exception.code, "response_too_large")

    def test_truncated_body_is_bad_json_in_client(self):
        _Handler.behaviour = "truncated"
        transport = lambda url, body, timeout: V.urllib_transport(self.url, body, timeout)  # noqa: E731
        client = V.RpcClient("https://mainnet.base.org", transport=transport)
        with self.assertRaises(V.RpcError) as ctx:
            client.call("eth_chainId", [])
        self.assertEqual(ctx.exception.code, "bad_json")

    def test_connection_refused(self):
        body = b"{}"
        with self.assertRaises(V.RpcError) as ctx:
            V.urllib_transport("http://127.0.0.1:1/", body, 2.0)
        self.assertEqual(ctx.exception.code, "network_error")


if __name__ == "__main__":
    unittest.main()
