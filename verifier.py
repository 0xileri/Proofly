"""Independent Base mainnet transfer verification over read-only JSON-RPC.

No Hermes imports: this module is usable (and tested) on its own.

Semantics (README "Verification semantics"):

* ``receipt.status == 1`` proves the transaction executed. On its own it never proves that the
  *intended* payment happened, so without a trusted intent the best verdict is ``EXECUTED``.
* A missing transaction or receipt is ``NOT_FOUND`` / ``PENDING``, never "failed".
* Any transport, chain-id or consistency problem is ``RPC_ERROR``: unverified, never success.
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from .intent import BASE_CHAIN_ID, ERC20, NATIVE_ETH, TransferIntent, normalize_address

DEFAULT_RPC_URL = "https://mainnet.base.org"
BASE_CHAIN_ID_HEX = "0x2105"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
EXPLORER_TX_URL = "https://basescan.org/tx/"
USER_AGENT = "proofly/1.0"  # mainnet.base.org (Cloudflare) answers 403 to the default urllib UA
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_LOGS = 5000
DEPOSIT_TX_TYPE = "0x7e"

_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_HEX_QTY_RE = re.compile(r"^0x(0|[1-9a-fA-F][0-9a-fA-F]{0,63})$")
_TOPIC_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


class Verdict(str, Enum):
    INVALID_HASH = "INVALID_HASH"
    NOT_FOUND = "NOT_FOUND"
    PENDING = "PENDING"
    REVERTED = "REVERTED"
    EXECUTED = "EXECUTED"
    VERIFIED_TRANSFER = "VERIFIED_TRANSFER"
    MISMATCH = "MISMATCH"
    RPC_ERROR = "RPC_ERROR"
    UNSUPPORTED = "UNSUPPORTED"


# Verdicts that the chain will not change on a re-check a few seconds later (barring a reorg).
STABLE_VERDICTS = frozenset({Verdict.INVALID_HASH, Verdict.REVERTED, Verdict.EXECUTED,
                             Verdict.VERIFIED_TRANSFER, Verdict.MISMATCH, Verdict.UNSUPPORTED})


def normalize_tx_hash(value: Any) -> Optional[str]:
    """``0x`` + exactly 64 hex characters (either case) -> lower-case; anything else -> ``None``."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not _TX_HASH_RE.fullmatch(value):
        return None
    return value.lower()


def explorer_url(tx_hash: str) -> str:
    return EXPLORER_TX_URL + tx_hash


def validate_rpc_url(url: Any) -> tuple[Optional[str], Optional[str]]:
    """``(url, None)`` when acceptable, else ``(None, reason)``. HTTPS only, no embedded
    credentials, no fragment, a real host. Plain HTTP (even localhost) is refused."""
    if not isinstance(url, str) or not url or len(url) > 512:
        return None, "rpc_url_missing_or_too_long"
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        return None, "rpc_url_has_whitespace"
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return None, "rpc_url_unparseable"
    if parts.scheme != "https":
        return None, "rpc_url_not_https"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return None, "rpc_url_has_credentials"
    if not parts.hostname:
        return None, "rpc_url_no_host"
    if parts.fragment:
        return None, "rpc_url_has_fragment"
    return url, None


def endpoint_label(url: str) -> str:
    """Scheme + host[:port] only, so a provider key embedded in the path is never logged or stored."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or "invalid"
        return f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")
    except ValueError:
        return "invalid"


# ---------------------------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------------------------

class RpcError(Exception):
    """A JSON-RPC exchange that produced no trustworthy answer. ``code`` is a stable reason code."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail[:200]


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib API
        try:
            fp.close()
        except Exception:
            pass
        raise RpcError("redirect_refused", f"HTTP {code} redirect refused")


_OPENER = urllib.request.build_opener(_RefuseRedirects)

# transport(url, body, timeout) -> response bytes; raises RpcError. Injected in tests.
Transport = Callable[[str, bytes, float], bytes]


def urllib_transport(url: str, body: bytes, timeout: float, *, max_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
    """POST ``body`` and return the response body. Only the two headers below are sent: no
    cookies, no Hermes credentials, no Authorization. Redirects are refused."""
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise RpcError("http_status", f"HTTP {status}")
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise RpcError("response_too_large", f"{length} bytes")
            data = response.read(max_bytes + 1)
    except RpcError:
        raise
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        except Exception:
            pass
        if exc.code == 429:
            raise RpcError("rate_limited", "HTTP 429") from None
        if 300 <= exc.code < 400:
            raise RpcError("redirect_refused", f"HTTP {exc.code}") from None
        raise RpcError("http_status", f"HTTP {exc.code}") from None
    except (socket.timeout, TimeoutError):
        raise RpcError("timeout", f"no response within {timeout:.1f}s") from None
    except ssl.SSLError as exc:
        raise RpcError("tls_error", type(exc).__name__) from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise RpcError("timeout", f"no response within {timeout:.1f}s") from None
        raise RpcError("network_error", type(reason).__name__ if not isinstance(reason, str) else reason) from None
    except (OSError, ValueError) as exc:
        raise RpcError("network_error", type(exc).__name__) from None
    if len(data) > max_bytes:
        raise RpcError("response_too_large", f">{max_bytes} bytes")
    return data


class RpcClient:
    """Minimal JSON-RPC 2.0 client with a per-call timeout, an overall deadline and one retry for
    transient failures. Every response is validated; anything odd raises ``RpcError``."""

    RETRYABLE = frozenset({"timeout", "network_error", "rate_limited"})

    def __init__(self, url: str, *, timeout: float = 6.0, transport: Optional[Transport] = None,
                 max_retries: int = 1, retry_backoff: float = 0.4,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        valid, reason = validate_rpc_url(url)
        self.url = valid
        self.config_error = reason
        self.label = endpoint_label(url) if valid else "invalid-rpc-url"
        self.timeout = float(timeout)
        self.transport = transport or urllib_transport
        self.max_retries = max(0, min(int(max_retries), 2))
        self.retry_backoff = retry_backoff
        self.clock = clock
        self.sleep = sleep
        self._next_id = 0

    def call(self, method: str, params: list, *, deadline: Optional[float] = None) -> Any:
        if self.url is None:
            raise RpcError("invalid_rpc_url", self.config_error or "")
        attempt = 0
        while True:
            remaining = self.timeout if deadline is None else min(self.timeout, deadline - self.clock())
            if remaining <= 0.05:
                raise RpcError("deadline_exceeded", f"{method} not attempted: verification time budget spent")
            try:
                return self._call_once(method, params, remaining)
            except RpcError as exc:
                attempt += 1
                if exc.code not in self.RETRYABLE or attempt > self.max_retries:
                    raise
                if deadline is not None and deadline - self.clock() < self.retry_backoff + 0.2:
                    raise
                self.sleep(self.retry_backoff)

    def _call_once(self, method: str, params: list, timeout: float) -> Any:
        self._next_id += 1
        request_id = self._next_id
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode()
        raw = self.transport(self.url, body, timeout)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise RpcError("bad_json", f"{method}: response is not valid JSON") from None
        if not isinstance(payload, dict):
            raise RpcError("bad_response", f"{method}: response is not a JSON object")
        if payload.get("id") != request_id:
            raise RpcError("bad_response", f"{method}: response id does not match request")
        if "error" in payload and payload["error"] is not None:
            err = payload["error"]
            message = err.get("message", "") if isinstance(err, dict) else str(err)
            code = err.get("code") if isinstance(err, dict) else None
            if code == -32005 or "rate" in str(message).lower() and "limit" in str(message).lower():
                raise RpcError("rate_limited", f"{method}: {str(message)[:120]}")
            raise RpcError("rpc_error", f"{method}: {str(message)[:120]}")
        if "result" not in payload:
            raise RpcError("bad_response", f"{method}: response has no result")
        return payload["result"]


# ---------------------------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Correlation:
    session_id: str = ""
    turn_id: str = ""
    tool_call_id: str = ""
    source_tool: str = ""


@dataclass(frozen=True)
class ProofRequest:
    tx_hash: Any
    intent: Optional[TransferIntent] = None
    chain_id: int = BASE_CHAIN_ID
    correlation: Correlation = field(default_factory=Correlation)


@dataclass
class ProofResult:
    verdict: Verdict
    execution_status: str  # success | reverted | pending | not_found | unknown
    transfer_match: Optional[bool]
    chain_id: Optional[int]
    tx_hash: str
    reason: str
    detail: str = ""
    block_number: Optional[int] = None
    confirmations: Optional[int] = None
    min_confirmations: Optional[int] = None
    evidence: dict = field(default_factory=dict)
    intent: Optional[TransferIntent] = None
    explorer_url: Optional[str] = None
    checked_at: str = ""
    rpc_endpoint_label: str = ""

    @property
    def verified(self) -> bool:
        return self.verdict is Verdict.VERIFIED_TRANSFER

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "execution_status": self.execution_status,
            "transfer_match": self.transfer_match,
            "chain_id": self.chain_id,
            "tx_hash": self.tx_hash,
            "block_number": self.block_number,
            "confirmations": self.confirmations,
            "min_confirmations": self.min_confirmations,
            "reason": self.reason,
            "detail": self.detail,
            "evidence": self.evidence,
            "intent": self.intent.to_dict() if self.intent else None,
            "explorer_url": self.explorer_url,
            "checked_at": self.checked_at,
            "rpc_endpoint_label": self.rpc_endpoint_label,
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hex_int(value: Any, what: str) -> int:
    if not isinstance(value, str) or not _HEX_QTY_RE.fullmatch(value):
        raise RpcError("bad_response", f"{what} is not a hex quantity")
    return int(value, 16)


def _lower(value: Any) -> Optional[str]:
    return value.lower() if isinstance(value, str) else None


# ---------------------------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------------------------

class Verifier:
    """Checks one transaction against Base mainnet evidence. ``verify`` never raises."""

    def __init__(self, client: RpcClient, *, min_confirmations: int = 3, budget_seconds: float = 15.0,
                 clock: Callable[[], float] = time.monotonic):
        self.client = client
        self.min_confirmations = max(1, int(min_confirmations))
        self.budget_seconds = budget_seconds
        self.clock = clock

    def verify(self, request: ProofRequest, *, deadline: Optional[float] = None) -> ProofResult:
        tx_hash = normalize_tx_hash(request.tx_hash)
        shown_hash = tx_hash or (str(request.tx_hash)[:80] if request.tx_hash is not None else "")
        base = dict(tx_hash=shown_hash, checked_at=now_iso(), rpc_endpoint_label=self.client.label,
                    intent=request.intent, min_confirmations=self.min_confirmations)
        if tx_hash is None:
            return ProofResult(Verdict.INVALID_HASH, "unknown", None, None, reason="invalid_hash",
                               detail="Not a 0x-prefixed 32-byte (64 hex character) transaction hash.", **base)
        base["explorer_url"] = explorer_url(tx_hash)
        if request.chain_id != BASE_CHAIN_ID or (request.intent and request.intent.chain_id != BASE_CHAIN_ID):
            return ProofResult(Verdict.UNSUPPORTED, "unknown", None, None, reason="chain_unsupported",
                               detail="Only Base mainnet (chain 8453) is supported.", **base)
        if deadline is None:
            deadline = self.clock() + self.budget_seconds
        try:
            return self._verify(tx_hash, request.intent, deadline, base)
        except RpcError as exc:
            return ProofResult(Verdict.RPC_ERROR, "unknown", None, None, reason=exc.code,
                               detail=f"Base RPC check failed ({exc.code}). {exc.detail}".strip(), **base)
        except Exception as exc:  # defensive: an unexpected shape must never become a success
            return ProofResult(Verdict.RPC_ERROR, "unknown", None, None, reason="internal_error",
                               detail=f"Unexpected verification error ({type(exc).__name__}).", **base)

    # -- steps ---------------------------------------------------------------------------------

    def _verify(self, tx_hash: str, intent: Optional[TransferIntent], deadline: float, base: dict) -> ProofResult:
        chain = self.client.call("eth_chainId", [], deadline=deadline)
        if not isinstance(chain, str) or chain.lower() != BASE_CHAIN_ID_HEX:
            return ProofResult(Verdict.RPC_ERROR, "unknown", None, None, reason="chain_id_mismatch",
                               detail=f"RPC endpoint reports chain id {str(chain)[:20]!r}, expected 8453 (Base).",
                               **base)
        tx = self.client.call("eth_getTransactionByHash", [tx_hash], deadline=deadline)
        receipt = self.client.call("eth_getTransactionReceipt", [tx_hash], deadline=deadline)
        if tx is not None and not isinstance(tx, dict):
            raise RpcError("bad_response", "transaction is not an object")
        if receipt is not None and not isinstance(receipt, dict):
            raise RpcError("bad_response", "receipt is not an object")

        if tx is None and receipt is None:
            return ProofResult(Verdict.NOT_FOUND, "not_found", None, BASE_CHAIN_ID,
                               reason="not_found",
                               detail="This node has no such transaction right now. That is not proof it failed.",
                               **base)
        if tx is None:
            raise RpcError("inconsistent_response", "receipt returned without its transaction")
        if _lower(tx.get("hash")) != tx_hash:
            raise RpcError("inconsistent_response", "transaction hash in response does not match request")

        tx_block_hash = _lower(tx.get("blockHash"))
        tx_block_number = _hex_int(tx["blockNumber"], "tx.blockNumber") if tx.get("blockNumber") else None

        if receipt is None:
            reason = "receipt_unavailable" if tx_block_number is not None else "awaiting_inclusion"
            return ProofResult(Verdict.PENDING, "pending", None, BASE_CHAIN_ID, reason=reason,
                               detail="Transaction is known but has no receipt yet.",
                               block_number=tx_block_number, **base)

        if _lower(receipt.get("transactionHash")) != tx_hash:
            raise RpcError("inconsistent_response", "receipt hash does not match request")
        receipt_block_number = _hex_int(receipt.get("blockNumber"), "receipt.blockNumber")
        receipt_block_hash = _lower(receipt.get("blockHash"))
        if tx_block_number is None or tx_block_hash is None:
            raise RpcError("inconsistent_response", "receipt exists but transaction has no block")
        if receipt_block_number != tx_block_number or receipt_block_hash != tx_block_hash:
            raise RpcError("inconsistent_response", "transaction and receipt disagree on the block")
        status = receipt.get("status")
        if status not in ("0x0", "0x1"):
            raise RpcError("bad_response", "receipt status is not 0x0 or 0x1")
        logs = receipt.get("logs")
        if not isinstance(logs, list) or len(logs) > MAX_LOGS:
            raise RpcError("bad_response", "receipt logs missing or oversized")

        head = _hex_int(self.client.call("eth_blockNumber", [], deadline=deadline), "eth_blockNumber")
        confirmations = max(0, head - receipt_block_number + 1)
        base = dict(base, block_number=receipt_block_number, confirmations=confirmations)

        if status == "0x0":
            return ProofResult(Verdict.REVERTED, "reverted", False if intent else None, BASE_CHAIN_ID,
                               reason="receipt_status_0",
                               detail="Receipt status is 0: the transaction reverted, so it transferred nothing.",
                               evidence={"receipt_status": 0, "tx_from": _lower(tx.get("from")),
                                         "tx_to": _lower(tx.get("to"))}, **base)

        observed = _observed_summary(tx, logs)
        if intent is None:
            return ProofResult(Verdict.EXECUTED, "success", None, BASE_CHAIN_ID, reason="no_trusted_intent",
                               detail="Receipt status is 1 (executed). No trusted transfer details were supplied, "
                                      "so this does not prove the requested payment happened.",
                               evidence={"observed": observed}, **base)
        if _lower(tx.get("type")) == DEPOSIT_TX_TYPE:
            return ProofResult(Verdict.UNSUPPORTED, "success", None, BASE_CHAIN_ID, reason="deposit_transaction",
                               detail="L1-to-L2 deposit transactions are not supported for transfer matching.",
                               evidence={"observed": observed}, **base)

        if intent.type == NATIVE_ETH:
            outcome = _match_native(tx, intent)
        elif intent.type == ERC20:
            outcome = _match_erc20(logs, intent)
        else:
            outcome = ("unsupported", "intent_type_unsupported", "Unsupported intent type.", {})
        kind, reason, detail, evidence = outcome
        evidence = dict(evidence, observed=observed)
        if kind == "unsupported":
            return ProofResult(Verdict.UNSUPPORTED, "success", None, BASE_CHAIN_ID, reason=reason, detail=detail,
                               evidence=evidence, **base)
        if kind == "mismatch":
            return ProofResult(Verdict.MISMATCH, "success", False, BASE_CHAIN_ID, reason=reason, detail=detail,
                               evidence=evidence, **base)
        if confirmations < self.min_confirmations:
            return ProofResult(Verdict.PENDING, "success", True, BASE_CHAIN_ID, reason="insufficient_confirmations",
                               detail=f"Matching transfer found with {confirmations} of {self.min_confirmations} "
                                      "required confirmations; not verified yet.", evidence=evidence, **base)
        return ProofResult(Verdict.VERIFIED_TRANSFER, "success", True, BASE_CHAIN_ID, reason="transfer_matches_intent",
                           detail=detail, evidence=evidence, **base)


# ---------------------------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------------------------

def _parse_transfer_log(log: Any) -> Optional[dict]:
    """A well-formed ERC-20 ``Transfer(address,address,uint256)`` log, or ``None`` if malformed
    (wrong topic count, non-address topics, non-32-byte data, removed=true)."""
    if not isinstance(log, dict) or log.get("removed") is True:
        return None
    topics = log.get("topics")
    data = log.get("data")
    if not isinstance(topics, list) or len(topics) != 3:
        return None
    if not all(isinstance(t, str) and _TOPIC_RE.fullmatch(t) for t in topics):
        return None
    sender_topic, recipient_topic = topics[1].lower(), topics[2].lower()
    if sender_topic[2:26] != "0" * 24 or recipient_topic[2:26] != "0" * 24:
        return None
    if not isinstance(data, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", data):
        return None
    emitter = normalize_address(log.get("address"))
    if emitter is None:
        return None
    return {"token": emitter, "from": "0x" + sender_topic[26:], "to": "0x" + recipient_topic[26:],
            "amount_raw": int(data, 16), "log_index": log.get("logIndex")}


def _is_transfer_topic(log: Any) -> bool:
    topics = log.get("topics") if isinstance(log, dict) else None
    return bool(topics) and isinstance(topics[0], str) and topics[0].lower() == TRANSFER_TOPIC


def _observed_summary(tx: dict, logs: list) -> dict:
    """Bounded, informational view of what the transaction visibly moved."""
    transfers = []
    for log in logs:
        if _is_transfer_topic(log):
            parsed = _parse_transfer_log(log)
            if parsed:
                transfers.append({k: (str(v) if k == "amount_raw" else v) for k, v in parsed.items()})
        if len(transfers) >= 5:
            break
    value = tx.get("value")
    try:
        value_wei = str(int(value, 16)) if isinstance(value, str) else None
    except ValueError:
        value_wei = None
    tx_input = tx.get("input") if isinstance(tx.get("input"), str) else ""
    return {"tx_from": _lower(tx.get("from")), "tx_to": _lower(tx.get("to")), "value_wei": value_wei,
            "has_calldata": tx_input not in ("", "0x"), "erc20_transfers": transfers}


def _match_native(tx: dict, intent: TransferIntent) -> tuple[str, str, str, dict]:
    tx_input = tx.get("input")
    if not isinstance(tx_input, str) or tx_input not in ("0x", ""):
        return ("unsupported", "native_with_calldata",
                "The transaction carries calldata (a contract call), so a direct ETH transfer cannot be "
                "proven from the receipt alone.", {})
    to = _lower(tx.get("to"))
    if to is None:
        return ("unsupported", "contract_creation", "Contract creation is not a transfer.", {})
    value = _hex_int(tx.get("value"), "tx.value")
    sender = _lower(tx.get("from"))
    differences = []
    if to != intent.to_address:
        differences.append({"field": "recipient", "expected": intent.to_address, "observed": to})
    if value != intent.amount_raw:
        differences.append({"field": "amount_raw", "expected": str(intent.amount_raw), "observed": str(value)})
    if intent.from_address and sender != intent.from_address:
        differences.append({"field": "sender", "expected": intent.from_address, "observed": sender})
    matched = {"type": NATIVE_ETH, "from": sender, "to": to, "amount_raw": str(value)}
    if differences:
        return ("mismatch", "native_transfer_mismatch", _describe_differences(differences),
                {"differences": differences, "matched_transfer": None, "observed_transfer": matched})
    return ("match", "transfer_matches_intent", "Direct ETH transfer matches the expected recipient and amount.",
            {"matched_transfer": matched})


def _match_erc20(logs: list, intent: TransferIntent) -> tuple[str, str, str, dict]:
    token_logs, lookalikes = [], []
    for log in logs:
        if not _is_transfer_topic(log):
            continue
        emitter = normalize_address(log.get("address")) if isinstance(log, dict) else None
        if emitter == intent.token_address:
            parsed = _parse_transfer_log(log)
            if parsed is None:
                return ("unsupported", "malformed_transfer_log",
                        "The expected token emitted a Transfer log Proofly cannot decode as standard ERC-20.", {})
            token_logs.append(parsed)
        else:
            parsed = _parse_transfer_log(log)
            if parsed is not None:
                lookalikes.append(parsed)

    if not token_logs:
        same_recipient = [p for p in lookalikes if p["to"] == intent.to_address]
        if same_recipient:
            diff = [{"field": "token", "expected": intent.token_address, "observed": same_recipient[0]["token"]}]
            return ("mismatch", "wrong_token_contract",
                    "A Transfer event to the recipient came from a different contract than the expected token. "
                    "A look-alike Transfer event cannot satisfy the expected token.",
                    {"differences": diff, "observed_transfer": _jsonable(same_recipient[0])})
        return ("mismatch", "no_transfer_from_expected_token",
                "No ERC-20 Transfer event from the expected token contract is in this receipt.",
                {"differences": [{"field": "transfer", "expected": "Transfer event from expected token",
                                  "observed": "none"}]})

    to_recipient = [p for p in token_logs if p["to"] == intent.to_address]
    if intent.from_address:
        to_recipient_from = [p for p in to_recipient if p["from"] == intent.from_address]
    else:
        to_recipient_from = to_recipient
    if not to_recipient:
        observed = token_logs[0]
        diff = [{"field": "recipient", "expected": intent.to_address, "observed": observed["to"]}]
        if observed["amount_raw"] != intent.amount_raw:
            diff.append({"field": "amount_raw", "expected": str(intent.amount_raw),
                         "observed": str(observed["amount_raw"])})
        return ("mismatch", "erc20_transfer_mismatch", _describe_differences(diff),
                {"differences": diff, "observed_transfer": _jsonable(observed),
                 "other_token_transfers": len(token_logs) - 1})
    if len(to_recipient) > 1:
        return ("unsupported", "ambiguous_multiple_transfers",
                "Several Transfer events from the expected token go to the recipient; Proofly does not pick one.",
                {"candidate_count": len(to_recipient)})
    if not to_recipient_from:
        observed = to_recipient[0]
        diff = [{"field": "sender", "expected": intent.from_address, "observed": observed["from"]}]
        return ("mismatch", "erc20_transfer_mismatch", _describe_differences(diff),
                {"differences": diff, "observed_transfer": _jsonable(observed)})
    match = to_recipient_from[0]
    if match["amount_raw"] != intent.amount_raw:
        diff = [{"field": "amount_raw", "expected": str(intent.amount_raw), "observed": str(match["amount_raw"])}]
        return ("mismatch", "erc20_transfer_mismatch", _describe_differences(diff),
                {"differences": diff, "observed_transfer": _jsonable(match)})
    return ("match", "transfer_matches_intent",
            "ERC-20 Transfer from the expected token matches recipient and raw amount.",
            {"matched_transfer": dict(_jsonable(match), type=ERC20)})


def _jsonable(parsed: dict) -> dict:
    return {k: (str(v) if k == "amount_raw" else v) for k, v in parsed.items()}


def _describe_differences(differences: list[dict]) -> str:
    parts = [f"{d['field']} expected {d['expected']}, observed {d['observed']}" for d in differences]
    return "Onchain evidence contradicts the expected transfer: " + "; ".join(parts) + "."
