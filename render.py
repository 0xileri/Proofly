"""Human-readable labels, corrections and footers. Pure functions, no Hermes imports.

Rules: only ``VERIFIED_TRANSFER`` ever gets a success label. A Basescan link is offered for
inspection; it is never presented as the proof itself.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from .intent import NATIVE_ETH, format_amount
from .verifier import ProofResult, Verdict

LABELS = {
    Verdict.VERIFIED_TRANSFER: "VERIFIED TRANSFER ON BASE",
    Verdict.EXECUTED: "ONCHAIN EXECUTED - INTENT NOT VERIFIED",
    Verdict.REVERTED: "TRANSACTION REVERTED",
    Verdict.PENDING: "PENDING / NOT YET VERIFIED",
    Verdict.NOT_FOUND: "NOT FOUND ON BASE - UNVERIFIED",
    Verdict.RPC_ERROR: "UNVERIFIED - RPC UNAVAILABLE",
    Verdict.MISMATCH: "MISMATCH - CLAIM NOT SUPPORTED",
    Verdict.INVALID_HASH: "INVALID TX HASH - UNVERIFIED",
    Verdict.UNSUPPORTED: "UNSUPPORTED - NOT VERIFIED",
}
REASON_LABELS = {
    "no_tx_hash": "UNVERIFIED - NO TRANSACTION HASH",
    "ambiguous_tx_hashes": "UNVERIFIED - AMBIGUOUS TRANSACTION HASHES",
    "chain_id_mismatch": "UNVERIFIED - RPC IS NOT BASE MAINNET",
    "invalid_rpc_url": "UNVERIFIED - RPC URL REJECTED",
    "internal_error": "UNVERIFIED - CHECK FAILED",
}
# Worst first: used to pick the headline when several results are non-verified.
SEVERITY = [Verdict.REVERTED, Verdict.MISMATCH, Verdict.INVALID_HASH, Verdict.NOT_FOUND, Verdict.PENDING,
            Verdict.RPC_ERROR, Verdict.UNSUPPORTED, Verdict.EXECUTED, Verdict.VERIFIED_TRANSFER]


def label(result: ProofResult) -> str:
    return REASON_LABELS.get(result.reason) or LABELS[result.verdict]


def short(value: Optional[str], head: int = 6, tail: int = 4) -> str:
    if not value:
        return "?"
    if len(value) <= head + tail + 5:
        return value
    return f"{value[:head]}...{value[-tail:]}"


def _amount(raw: Any, result: ProofResult) -> str:
    intent = result.intent
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        return str(raw)
    if intent is None:
        return f"{amount} raw units"
    return format_amount(amount, intent.type, intent.token_address)


def _difference_text(result: ProofResult) -> str:
    diffs = result.evidence.get("differences") or []
    parts = []
    for diff in diffs[:4]:
        field, expected, observed = diff.get("field"), diff.get("expected"), diff.get("observed")
        if field == "amount_raw":
            parts.append(f"amount expected {_amount(expected, result)}, observed {_amount(observed, result)}")
        else:
            parts.append(f"{field} expected {expected}, observed {observed}")
    return "; ".join(parts)


def _transfer_text(result: ProofResult) -> str:
    matched = result.evidence.get("matched_transfer") or {}
    intent = result.intent
    if not matched or intent is None:
        return ""
    amount = _amount(matched.get("amount_raw"), result)
    kind = "ETH transfer" if intent.type == NATIVE_ETH else f"token {intent.token_address}"
    return f"{amount} from {matched.get('from') or 'unknown sender'} to {matched.get('to')} ({kind})"


def one_line(result: ProofResult) -> str:
    """``LABEL: tx 0x1234...abcd (why)``: the per-result status line used everywhere."""
    if not result.tx_hash:
        head = label(result)
    elif result.verdict is Verdict.INVALID_HASH:
        head = f"{label(result)}: {result.tx_hash[:24]!r}"
    else:
        head = f"{label(result)}: tx {short(result.tx_hash, 10, 6)}"
    v = result.verdict
    if v is Verdict.VERIFIED_TRANSFER:
        return (f"{head}. {_transfer_text(result)}, block {result.block_number}, "
                f"{result.confirmations} confirmations (policy: at least {result.min_confirmations}).")
    if v is Verdict.MISMATCH:
        return f"{head}. Executed onchain, but it is not the expected transfer: {_difference_text(result)}."
    if v is Verdict.REVERTED:
        return f"{head}. Receipt status 0 in block {result.block_number}: nothing was transferred."
    if v is Verdict.EXECUTED:
        return (f"{head}. Receipt status 1 in block {result.block_number}, but no trusted payment details "
                "were available, so it is not proof of the requested transfer.")
    if v is Verdict.PENDING:
        return f"{head}. {result.detail}"
    if v is Verdict.NOT_FOUND:
        return f"{head}. Not on this Base node at check time; that is not proof it failed, and it is not verified."
    if v is Verdict.RPC_ERROR:
        return f"{head}. {result.detail} The payment is unverified, not failed."
    if v is Verdict.INVALID_HASH:
        return f"{head}. That is not a valid 32-byte transaction hash."
    return f"{head}. {result.detail}"


def inspect_line(result: ProofResult) -> str:
    return f"Inspect: {result.explorer_url}" if result.explorer_url else ""


def worst(results: Iterable[ProofResult]) -> Optional[ProofResult]:
    ranked = sorted(results, key=lambda r: SEVERITY.index(r.verdict))
    return ranked[0] if ranked else None


def short_reason(result: ProofResult) -> str:
    """One plain clause per verdict for the inline correction; the footer carries the details."""
    tx = f"tx {short(result.tx_hash, 6, 4)}" if result.tx_hash and result.verdict is not Verdict.INVALID_HASH else ""
    v = result.verdict
    if v is Verdict.REVERTED:
        return f"{tx} reverted onchain, so nothing was transferred"
    if v is Verdict.MISMATCH:
        fields = [d.get("field") for d in result.evidence.get("differences") or []]
        names = {"amount_raw": "amount", "transfer": "token transfer"}
        what = " and ".join(names.get(f, f) for f in fields[:3]) or "transfer"
        verb = "do" if len(fields) > 1 else "does"
        return f"{tx} executed, but its {what} {verb} not match the requested payment"
    if v is Verdict.NOT_FOUND:
        return f"{tx} was not found on Base at check time"
    if v is Verdict.PENDING:
        return f"{tx} is not confirmed yet"
    if v is Verdict.RPC_ERROR:
        return "Base could not be checked (RPC unavailable or not Base mainnet)"
    if v is Verdict.INVALID_HASH:
        return "the reported transaction hash is not a valid hash"
    if result.reason == "no_tx_hash":
        return "the payment tool returned no transaction hash"
    if result.reason == "ambiguous_tx_hashes":
        return "the payment tool returned several different transaction hashes"
    if v is Verdict.EXECUTED:
        return f"{tx} executed, but the requested recipient and amount could not be checked"
    return f"{tx} cannot be verified ({result.reason})".strip()


def correction(results: list[ProofResult]) -> str:
    """The sentence that replaces a false success claim in enforce mode."""
    bad = [r for r in results if not r.verified]
    if not bad:
        return ""
    reasons = "; ".join(short_reason(r) for r in bad[:3])
    return f"[Proofly correction: this payment is NOT verified: {reasons}.]"


REMOVED_CLAIM = "[Proofly: unverified payment claim removed, see the check below.]"


def status_block(results: list[ProofResult], *, mode: str, notes: Iterable[str] = ()) -> str:
    """Footer listing every checked result. Only a fully verified set gets the plain verified header."""
    all_verified = bool(results) and all(r.verified for r in results)
    if all_verified:
        header = "Proofly check (Base mainnet): verified against chain evidence."
    elif mode == "warn":
        header = ("WARNING from Proofly: the payment claims in this reply are NOT verified on Base. "
                  "Treat them as unconfirmed.")
    else:
        header = "Proofly check (Base mainnet): the payment in this reply is NOT verified."
    lines = ["", "---", header]
    for result in results[:6]:
        lines.append(f"- {one_line(result)}")
        link = inspect_line(result)
        if link:
            lines.append(f"  {link}")
    for note in notes:
        lines.append(f"- {note}")
    return "\n".join(lines)


def fallback_block() -> str:
    """Deterministic text used when Proofly itself failed inside a payment context."""
    return ("\n\n---\nProofly could not complete its Base check for this reply (internal error). "
            "Treat any payment or transfer claims above as UNVERIFIED.")


def result_summary(result: ProofResult) -> dict:
    data = result.to_dict()
    data["label"] = label(result)
    data["summary"] = one_line(result)
    return data


def tool_result_payload(result: ProofResult, original: Any, *, mode: str, tool_claimed_success: bool) -> str:
    """Replacement for an allowlisted payment tool's result, shown to the model."""
    try:
        parsed = json.loads(original) if isinstance(original, str) else original
        if not isinstance(parsed, (dict, list)):
            parsed = original
    except (TypeError, ValueError, RecursionError):
        parsed = original
    if isinstance(parsed, str) and len(parsed) > 4000:
        parsed = parsed[:4000] + "...[truncated by Proofly]"
    payload: dict[str, Any] = {"proofly_verification": result_summary(result)}
    if result.verified:
        payload["proofly_instruction"] = (
            "Verified on Base against the expected transfer. You may report it as a verified transfer and "
            "include the transaction hash.")
        payload["tool_output"] = parsed
    else:
        claimed = " The payment tool's own message claims success; that claim is contradicted or unproven." \
            if tool_claimed_success else ""
        if mode == "warn":
            payload["proofly_instruction"] = (
                f"Proofly could not verify this payment ({label(result)}).{claimed} If you mention the payment, "
                "say clearly that it is not verified on Base.")
        else:
            payload["proofly_instruction"] = (
                f"This payment is NOT verified on Base. Proofly verdict: {label(result)}.{claimed} "
                "Report this verdict and its reason to the user, not a success message.")
        payload["unverified_tool_output"] = parsed
    return json.dumps(payload, ensure_ascii=False, default=str)
