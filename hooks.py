"""Hermes hook callbacks, the ``proofly_verify_tx`` handler and per-turn correlation state.

Flow for an allowlisted payment tool call in one turn:

1. ``post_tool_call`` (observer): record the call's structured intent (from its arguments) and the
   tx hash it returned, keyed by session + turn + tool_call_id. Returns nothing.
2. ``transform_tool_result``: verify that hash on Base and hand the model a result that carries
   the verdict, so a false "success" is corrected before the model reasons about it.
3. ``transform_llm_output`` (``hold_stream=True``): take this turn's recorded payment calls, and
   correct or qualify success claims in the final reply that the chain evidence does not support.

Replies with no payment call in the same session/turn are returned untouched (``None``).
Hermes imports are not used here; ``__init__`` wires these callbacks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import claims, render
from .intent import TransferIntent, intent_from_payment_args, parse_intent
from .verifier import (DEFAULT_RPC_URL, STABLE_VERDICTS, ProofRequest, ProofResult, RpcClient, Transport,
                       Verdict, Verifier, normalize_tx_hash, now_iso, validate_rpc_url)

logger = logging.getLogger("proofly")

VERIFY_TOOL = "proofly_verify_tx"
MODES = ("enforce", "warn")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")
_TX_KEYS = {"txhash", "transactionhash", "txid", "transactionid", "tx"}
_SUCCESS_STATUS = {"success", "succeeded", "successful", "ok", "sent", "confirmed", "completed", "complete", "done"}

EVENT_TTL_SECONDS = 15 * 60
MAX_SESSIONS = 64
MAX_EVENTS_PER_SESSION = 16
MAX_EXTRA_REPLY_HASHES = 2
CACHE_TTL_SECONDS = 60
CACHE_MAX = 256
MAX_RESULT_CHARS = 1_000_000


# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    rpc_url: str = DEFAULT_RPC_URL
    mode: str = "enforce"
    payment_tools: tuple[str, ...] = ()
    min_confirmations: int = 3
    timeout_seconds: float = 6.0
    annotate_tool_results: bool = True
    warnings: tuple[str, ...] = ()

    @property
    def hook_budget_seconds(self) -> float:
        """Total RPC time one hook call may spend; stays well under Hermes' 30 s hook timeout."""
        return min(20.0, self.timeout_seconds * 2.5 + 1.0)


def _clamp_int(value: Any, default: int, low: int, high: int) -> tuple[int, bool]:
    if isinstance(value, bool):
        return default, False
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default, False
    return max(low, min(high, number)), low <= number <= high


def load_settings(get: Callable[[str, Any], Any]) -> Settings:
    """Read and bound every setting. Invalid values fall back to safe defaults with a warning; an
    invalid ``rpc_url`` is kept so that every check reports it (fail closed, never another host)."""
    warnings: list[str] = []

    rpc_url = get("rpc_url", DEFAULT_RPC_URL)
    rpc_url = rpc_url.strip() if isinstance(rpc_url, str) and rpc_url.strip() else DEFAULT_RPC_URL
    _, err = validate_rpc_url(rpc_url)
    if err:
        warnings.append(f"rpc_url rejected ({err}); every check will report UNVERIFIED until it is fixed")

    mode = str(get("mode", "enforce") or "enforce").strip().lower()
    if mode not in MODES:
        warnings.append(f"mode {mode[:20]!r} is not enforce|warn; using enforce")
        mode = "enforce"

    raw_tools = get("payment_tools", []) or []
    if isinstance(raw_tools, str):
        raw_tools = [t for t in re.split(r"[,\s]+", raw_tools) if t]
    tools: list[str] = []
    if isinstance(raw_tools, (list, tuple)):
        for name in raw_tools:
            if isinstance(name, str) and _TOOL_NAME_RE.fullmatch(name.strip()) and name.strip() != VERIFY_TOOL:
                if name.strip() not in tools:
                    tools.append(name.strip())
            else:
                warnings.append(f"payment_tools entry {str(name)[:40]!r} ignored")
    else:
        warnings.append("payment_tools must be a list of tool names")
    tools = tools[:32]

    min_conf, ok = _clamp_int(get("min_confirmations", 3), 3, 1, 1000)
    if not ok:
        warnings.append("min_confirmations out of range 1..1000; clamped")
    timeout, ok = _clamp_int(get("timeout_seconds", 6), 6, 1, 20)
    if not ok:
        warnings.append("timeout_seconds out of range 1..20; clamped")
    annotate = get("annotate_tool_results", True)
    if not isinstance(annotate, bool):
        warnings.append("annotate_tool_results must be true/false; using true")
        annotate = True
    return Settings(rpc_url, mode, tuple(tools), min_conf, float(timeout), annotate, tuple(warnings))


# ---------------------------------------------------------------------------------------------
# Tool-result parsing
# ---------------------------------------------------------------------------------------------

def _parse_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and len(value) <= MAX_RESULT_CHARS:
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            return None
    return None


def _walk_tx_fields(node: Any, depth: int, found: list[str]) -> None:
    if depth > 3 or len(found) > 16:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            norm = str(key).lower().replace("_", "").replace("-", "")
            if (norm in _TX_KEYS or (depth == 0 and norm == "hash")) and isinstance(value, str) and value.strip():
                found.append(value.strip())
            elif isinstance(value, (dict, list)):
                _walk_tx_fields(value, depth + 1, found)
    elif isinstance(node, list):
        for item in node[:32]:
            _walk_tx_fields(item, depth + 1, found)


_PROSE_KEYS = {"message", "msg", "summary", "detail", "details", "text", "output", "description", "content", "note"}


def _prose_values(node: Any, depth: int) -> list[str]:
    if depth > 3:
        return []
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in list(node.items())[:64]:
            if isinstance(value, str) and str(key).lower() in _PROSE_KEYS:
                out.append(value)
            elif isinstance(value, (dict, list)):
                out.extend(_prose_values(value, depth + 1))
    elif isinstance(node, list):
        for item in node[:32]:
            out.extend(_prose_values(item, depth + 1))
    elif isinstance(node, str) and depth == 0:
        out.append(node)
    return out


def extract_tx_hashes(result: Any) -> tuple[tuple[str, ...], Optional[str], Optional[str]]:
    """``(hashes, problem, invalid_value)`` from a payment tool's result.

    Structured fields (``tx_hash``, ``transactionHash``, ``txid``, top-level ``hash``...) win over
    free text. Several distinct hashes are ambiguous; Proofly does not pick one.
    """
    parsed = _parse_json(result)
    candidates: list[str] = []
    if parsed is not None:
        _walk_tx_fields(parsed, 0, candidates)
    if candidates:
        valid = []
        for value in candidates:
            norm = normalize_tx_hash(value)
            if norm and norm not in valid:
                valid.append(norm)
        if len(valid) == 1:
            return (valid[0],), None, None
        if len(valid) > 1:
            return tuple(valid[:4]), "ambiguous_tx_hashes", None
        return (), "invalid_hash", candidates[0][:80]
    if parsed is not None:  # structured output: only prose fields, never block hashes / topics / data
        text = " ".join(_prose_values(parsed, 0))
    else:
        text = result if isinstance(result, str) else ""
    found = claims.tx_hashes_in(text[:MAX_RESULT_CHARS], limit=4)
    if len(found) == 1:
        return (found[0],), None, None
    if len(found) > 1:
        return tuple(found), "ambiguous_tx_hashes", None
    return (), "no_tx_hash", None


def tool_claims_success(result: Any) -> bool:
    parsed = _parse_json(result)
    if isinstance(parsed, dict):
        for key in ("status", "state", "result", "outcome"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip().lower() in _SUCCESS_STATUS:
                return True
        if parsed.get("success") is True or parsed.get("ok") is True:
            return True
        text = " ".join(str(parsed.get(k, "")) for k in ("message", "summary", "detail"))
    else:
        text = result if isinstance(result, str) else ""
    return bool(claims.find_success_claims(text[:20_000]))


# ---------------------------------------------------------------------------------------------
# Correlation state
# ---------------------------------------------------------------------------------------------

@dataclass
class ToolEvent:
    call_key: str
    session_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    created: float
    intent: Optional[TransferIntent]
    intent_error: Optional[str]
    hashes: tuple[str, ...]
    hash_problem: Optional[str]
    invalid_hash_value: Optional[str] = None
    tool_claimed_success: bool = False
    tool_status: str = ""
    result: Optional[ProofResult] = None


def _s(kwargs: dict, key: str) -> str:
    value = kwargs.get(key)
    return value if isinstance(value, str) else ""


def _call_key(tool_call_id: str, tool_name: str, args: Any, result: Any) -> str:
    if tool_call_id:
        return f"id:{tool_call_id}"
    digest = hashlib.sha256(json.dumps([tool_name, args, result], default=str, sort_keys=True)[:200_000]
                            .encode("utf-8", "replace")).hexdigest()[:20]
    return f"h:{digest}"


def _synthetic(reason: str, detail: str, *, intent: Optional[TransferIntent], tx_hash: str = "") -> ProofResult:
    """A result for a payment call Proofly could not send to the chain (no hash, several hashes,
    another chain). Always UNSUPPORTED: unverified, never success."""
    return ProofResult(Verdict.UNSUPPORTED, "unknown", None, None, tx_hash=tx_hash, reason=reason, detail=detail,
                       intent=intent, checked_at=now_iso())


class ProoflyRuntime:
    def __init__(self, settings: Settings, *, ledger_factory: Optional[Callable[[], Any]] = None,
                 transport: Optional[Transport] = None, clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.clock = clock
        self.client = RpcClient(settings.rpc_url, timeout=settings.timeout_seconds, transport=transport, clock=clock)
        self.verifier = Verifier(self.client, min_confirmations=settings.min_confirmations,
                                 budget_seconds=settings.hook_budget_seconds, clock=clock)
        self._ledger_factory = ledger_factory
        self._ledger = None
        self._ledger_failed = False
        self._lock = threading.RLock()
        self._events: "OrderedDict[str, OrderedDict[str, ToolEvent]]" = OrderedDict()
        self._cache: "OrderedDict[tuple, tuple[float, ProofResult]]" = OrderedDict()
        self._recorded: "OrderedDict[tuple, None]" = OrderedDict()
        self.stats = {"tool_results_annotated": 0, "replies_rewritten": 0, "replies_untouched": 0, "errors": 0}

    # -- helpers -------------------------------------------------------------------------------

    @property
    def watched_tools(self) -> frozenset:
        return frozenset(self.settings.payment_tools) | {VERIFY_TOOL}

    def ledger(self):
        with self._lock:
            if self._ledger is None and self._ledger_factory is not None and not self._ledger_failed:
                try:
                    self._ledger = self._ledger_factory()
                except Exception:
                    self._ledger_failed = True
                    logger.warning("proofly: ledger unavailable", exc_info=True)
            return self._ledger

    def record(self, result: ProofResult, *, session_id: str, turn_id: str, source: str) -> None:
        key = (session_id, turn_id, source, result.tx_hash, result.verdict.value, result.reason)
        with self._lock:
            if key in self._recorded:
                return
            self._recorded[key] = None
            while len(self._recorded) > 1024:
                self._recorded.popitem(last=False)
        ledger = self.ledger()
        if ledger is None:
            return
        try:
            ledger.record(result, session_id=session_id, turn_id=turn_id, source=source, mode=self.settings.mode)
        except Exception:
            logger.warning("proofly: could not write ledger row", exc_info=True)

    def new_deadline(self) -> float:
        return self.clock() + self.settings.hook_budget_seconds

    def verify_hash(self, tx_hash: Any, intent: Optional[TransferIntent], deadline: float) -> ProofResult:
        """Verify with a short cache for verdicts the chain will not flip within seconds."""
        norm = normalize_tx_hash(tx_hash)
        key = (self.client.label, self.verifier.min_confirmations, norm or str(tx_hash)[:80],
               intent.fingerprint() if intent else "")
        now = self.clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        result = self.verifier.verify(ProofRequest(tx_hash, intent), deadline=deadline)
        if result.verdict in STABLE_VERDICTS:
            with self._lock:
                self._cache[key] = (now + CACHE_TTL_SECONDS, result)
                self._cache.move_to_end(key)
                while len(self._cache) > CACHE_MAX:
                    self._cache.popitem(last=False)
        return result

    def _verify_event(self, event: ToolEvent, deadline: float) -> ProofResult:
        if event.result is not None and event.result.verdict in STABLE_VERDICTS:
            return event.result
        if event.intent is None and event.intent_error == "intent_chain_unsupported":
            result = _synthetic("chain_unsupported", "The payment tool targeted a chain other than Base mainnet; "
                                "Proofly only verifies chain 8453.", intent=None,
                                tx_hash=event.hashes[0] if event.hashes else "")
        elif event.hash_problem == "no_tx_hash":
            result = _synthetic("no_tx_hash", "The payment tool returned no transaction hash, so nothing can be "
                                "checked onchain.", intent=event.intent)
        elif event.hash_problem == "ambiguous_tx_hashes":
            result = _synthetic("ambiguous_tx_hashes", "The payment tool returned several different transaction "
                                "hashes; Proofly does not guess which one is the payment.", intent=event.intent,
                                tx_hash=event.hashes[0])
        elif event.hash_problem == "invalid_hash":
            result = self.verify_hash(event.invalid_hash_value or "", event.intent, deadline)
        else:
            result = self.verify_hash(event.hashes[0], event.intent, deadline)
        with self._lock:
            event.result = result
        return result

    def _gc_locked(self) -> None:
        cutoff = self.clock() - EVENT_TTL_SECONDS
        for session in list(self._events):
            bucket = self._events[session]
            for key in [k for k, e in bucket.items() if e.created < cutoff]:
                del bucket[key]
            if not bucket:
                del self._events[session]

    def _store_event(self, event: ToolEvent) -> ToolEvent:
        with self._lock:
            self._gc_locked()
            bucket = self._events.get(event.session_id)
            if bucket is None:
                bucket = self._events[event.session_id] = OrderedDict()
            if event.turn_id:  # a new turn makes every other turn's pending calls stale
                for key in [k for k, e in bucket.items() if e.turn_id and e.turn_id != event.turn_id]:
                    del bucket[key]
            existing = bucket.get(event.call_key)
            if existing is not None:  # duplicate delivery of the same call: idempotent
                return existing
            bucket[event.call_key] = event
            while len(bucket) > MAX_EVENTS_PER_SESSION:
                bucket.popitem(last=False)
            self._events.move_to_end(event.session_id)
            while len(self._events) > MAX_SESSIONS:
                self._events.popitem(last=False)
            return event

    def _find_event(self, session_id: str, call_key: str) -> Optional[ToolEvent]:
        with self._lock:
            return (self._events.get(session_id) or {}).get(call_key)

    def _take_events(self, session_id: str, turn_id: str) -> list[ToolEvent]:
        """Pop this session's pending calls. With a turn id, calls from any other turn are dropped
        (an earlier turn's verified hash can never back a new claim)."""
        with self._lock:
            self._gc_locked()
            bucket = self._events.pop(session_id, None)
        if not bucket:
            return []
        if turn_id:
            return [e for e in bucket.values() if e.turn_id in (turn_id, "")]
        return list(bucket.values())

    def _build_event(self, kwargs: dict) -> Optional[ToolEvent]:
        tool_name = _s(kwargs, "tool_name")
        if tool_name not in self.watched_tools:
            return None
        args = kwargs.get("args")
        args = args if isinstance(args, dict) else {}
        result = kwargs.get("result")
        session_id, turn_id, tool_call_id = _s(kwargs, "session_id"), _s(kwargs, "turn_id"), _s(kwargs, "tool_call_id")
        if tool_name == VERIFY_TOOL:
            expected = args.get("expected_transfer")
            intent, intent_error = (parse_intent(expected, source="verify_tool_args")
                                    if expected not in (None, {}) else (None, "no_expected_transfer"))
            raw_hash = args.get("tx_hash")
            norm = normalize_tx_hash(raw_hash)
            hashes, problem, invalid = ((norm,), None, None) if norm else ((), "invalid_hash", str(raw_hash)[:80])
            claimed = False
        else:
            intent, intent_error = intent_from_payment_args(args)
            hashes, problem, invalid = extract_tx_hashes(result)
            claimed = tool_claims_success(result)
        return ToolEvent(_call_key(tool_call_id, tool_name, args, result), session_id, turn_id, tool_call_id,
                         tool_name, self.clock(), intent, intent_error, hashes, problem, invalid, claimed,
                         _s(kwargs, "status"))

    # -- hooks ---------------------------------------------------------------------------------

    def on_post_tool_call(self, **kwargs: Any) -> None:
        """Observer: capture a watched tool call's intent and hash. Never returns a directive."""
        try:
            event = self._build_event(kwargs)
            if event is not None:
                self._store_event(event)
        except Exception:
            self.stats["errors"] += 1
            logger.warning("proofly: post_tool_call capture failed", exc_info=True)
        return None

    def on_transform_tool_result(self, **kwargs: Any) -> Optional[str]:
        """Annotate an allowlisted payment tool's result with the Base verdict. Other tools: ``None``."""
        tool_name = _s(kwargs, "tool_name")
        if tool_name not in self.settings.payment_tools:
            return None
        original = kwargs.get("result")
        try:
            built = self._build_event(kwargs)
            if built is None:
                return None
            event = self._find_event(built.session_id, built.call_key) or self._store_event(built)
            result = self._verify_event(event, self.new_deadline())
            self.record(result, session_id=event.session_id, turn_id=event.turn_id, source=tool_name)
            if not self.settings.annotate_tool_results:
                return None
            self.stats["tool_results_annotated"] += 1
            return render.tool_result_payload(result, original, mode=self.settings.mode,
                                              tool_claimed_success=event.tool_claimed_success)
        except Exception:
            self.stats["errors"] += 1
            logger.warning("proofly: transform_tool_result failed", exc_info=True)
            if not self.settings.annotate_tool_results:
                return None
            return json.dumps({
                "proofly_verification": {"verdict": "RPC_ERROR", "label": "UNVERIFIED - CHECK FAILED",
                                         "reason": "internal_error"},
                "proofly_instruction": "Proofly could not check this payment. Do not report it as successful "
                                       "or verified.",
                "unverified_tool_output": original[:4000] if isinstance(original, str) else str(original)[:4000],
            })

    def on_transform_llm_output(self, **kwargs: Any) -> Optional[str]:
        """Held final-reply transform. ``None`` (no change) unless this turn made a watched call."""
        text = kwargs.get("response_text")
        if not isinstance(text, str) or not text.strip():
            return None
        session_id, turn_id = _s(kwargs, "session_id"), _s(kwargs, "turn_id")
        try:
            events = self._take_events(session_id, turn_id)
        except Exception:
            self.stats["errors"] += 1
            logger.warning("proofly: could not read turn state", exc_info=True)
            return None
        if not events:
            self.stats["replies_untouched"] += 1
            return None
        try:
            rewritten = self._rewrite_reply(text, events, session_id, turn_id)
            self.stats["replies_rewritten"] += 1
            return rewritten
        except Exception:
            self.stats["errors"] += 1
            logger.warning("proofly: reply check failed; returning explicit UNVERIFIED note", exc_info=True)
            return text.rstrip() + render.fallback_block()

    # -- tool handler --------------------------------------------------------------------------

    def handle_verify_tool(self, args: Any, **kwargs: Any) -> str:
        """``proofly_verify_tx`` handler: always returns a JSON string, never raises."""
        try:
            args = args if isinstance(args, dict) else {}
            tx_hash = args.get("tx_hash")
            if not isinstance(tx_hash, str) or len(tx_hash) > 100:
                tx_hash = str(tx_hash)[:100] if tx_hash is not None else ""
            expected = args.get("expected_transfer")
            intent, intent_error = None, None
            if expected not in (None, {}, ""):
                intent, intent_error = parse_intent(expected, source="verify_tool_args")
            result = self.verify_hash(tx_hash, intent, self.new_deadline())
            self.record(result, session_id=_s(kwargs, "session_id"), turn_id="", source=VERIFY_TOOL)
            payload = {"proofly_verification": render.result_summary(result)}
            if intent_error:
                payload["expected_transfer_error"] = intent_error
                payload["note"] = ("expected_transfer was rejected, so only execution was checked. "
                                   "Fix the fields (integer amount_raw, 0x addresses) to check the transfer.")
            elif intent is None:
                payload["note"] = ("No expected_transfer was given: this result says whether the transaction "
                                   "executed, not whether it was the payment the user asked for.")
            return json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            self.stats["errors"] += 1
            logger.warning("proofly: proofly_verify_tx failed", exc_info=True)
            return json.dumps({"error": "proofly_verify_tx failed internally",
                               "proofly_verification": {"verdict": "RPC_ERROR", "label": "UNVERIFIED - CHECK FAILED",
                                                        "reason": "internal_error"}})

    # -- reply rewriting -----------------------------------------------------------------------

    def _rewrite_reply(self, text: str, events: list[ToolEvent], session_id: str, turn_id: str) -> str:
        deadline = self.new_deadline()
        by_hash: dict[str, ProofResult] = {}
        results: list[ProofResult] = []
        notes: list[str] = []
        seen_calls: set = set()
        for event in events:
            identity = (event.tool_name, event.hashes, event.hash_problem,
                        event.intent.fingerprint() if event.intent else event.intent_error)
            if identity in seen_calls:  # same call delivered twice without a tool_call_id
                continue
            seen_calls.add(identity)
            result = self._verify_event(event, deadline)
            source = event.tool_name if event.tool_name != VERIFY_TOOL else VERIFY_TOOL
            self.record(result, session_id=session_id, turn_id=turn_id or event.turn_id, source=source)
            results.append(result)
            for h in event.hashes:
                by_hash.setdefault(h, result)
            if event.intent is None and event.intent_error and event.tool_name != VERIFY_TOOL \
                    and event.intent_error != "intent_chain_unsupported":
                notes.append(f"{event.tool_name} arguments had no exact structured transfer details "
                             f"({event.intent_error}); only execution could be checked.")

        extra_results: list[ProofResult] = []
        for h in claims.tx_hashes_in(text, limit=8):
            if h in by_hash or len(extra_results) >= MAX_EXTRA_REPLY_HASHES:
                continue
            extra = self.verify_hash(h, None, deadline)
            self.record(extra, session_id=session_id, turn_id=turn_id, source="reply_hash")
            by_hash[h] = extra
            extra_results.append(extra)
            notes.append(f"tx {render.short(h, 10, 6)} appears in this reply but was not returned by the payment "
                         "tool in this turn; it was checked for execution only.")

        all_results = results + extra_results
        mode = self.settings.mode
        body = text
        if mode == "enforce":
            replacements = []
            corrected: set = set()
            last_end = -1
            for claim in claims.find_success_claims(text):
                attributed = [by_hash[h] for h in claim.hashes if h in by_hash] or all_results
                action = _claim_action(attributed)
                if action == "replace":
                    key = frozenset((r.tx_hash, r.verdict) for r in attributed if not r.verified)
                    if key not in corrected:
                        new = render.correction(attributed)
                    elif not text[last_end:claim.start].strip():
                        new = ""  # repeats the claim corrected right before it: drop it
                    else:
                        new = render.REMOVED_CLAIM
                    corrected.add(key)
                    last_end = claim.end
                elif action == "qualify":
                    new = claim.text.strip() + " " + _qualifier(attributed)
                else:
                    continue
                replacements.append((claim.start, claim.end, new))
            body = claims.replace_spans(text, replacements)
        return body.rstrip() + "\n" + render.status_block(all_results, mode=mode, notes=_dedupe(notes))


def _claim_action(results: list[ProofResult]) -> str:
    """replace: proof contradicts or is absent. qualify: execution proven, the intended transfer is not
    (or not yet confirmed). keep: everything attributed is VERIFIED_TRANSFER."""
    action = "keep"
    for result in results:
        if result.verdict is Verdict.VERIFIED_TRANSFER:
            continue
        if result.verdict is Verdict.EXECUTED or (result.verdict is Verdict.PENDING and result.transfer_match):
            action = "qualify"
            continue
        return "replace"
    return action


def _qualifier(results: list[ProofResult]) -> str:
    pending = [r for r in results if r.verdict is Verdict.PENDING]
    if pending:
        return "[Proofly: matching transfer found but not yet confirmed; not verified yet.]"
    return "[Proofly: executed onchain, but the intended recipient and amount are NOT verified.]"


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out[:6]
