"""``/proofly`` slash command: read-only status, history and explicit on-demand checks.

History is scoped to the calling session (the ledger stores an HMAC of the session id, so rows
from other chats sharing the profile are never listed). ``clear`` deletes only the caller's rows.
No Hermes imports.
"""

from __future__ import annotations

import shlex
from typing import Any

from . import render
from .intent import intent_from_command_tokens
from .verifier import endpoint_label

USAGE = """Proofly: checks an agent's Base transfer claims against chain evidence.

/proofly                 recent verdicts for this chat + settings
/proofly status          settings and verdict counts
/proofly last            the most recent verdict in this chat, in full
/proofly history [n]     up to n (max 20) recent verdicts in this chat
/proofly check <hash>    execution-only check of a Base tx hash
/proofly check <hash> to=0x... amount_raw=N [token=0x...|usdc|eth] [from=0x...]
                         check the tx against the transfer you expected
/proofly clear           delete this chat's Proofly history
/proofly help            this text"""


def _row_line(row: dict) -> str:
    match = row.get("transfer_match")
    match_text = "" if match is None else (" match" if match else " no-match")
    tx = row.get("tx_hash") or "(no hash)"
    return (f"{row.get('checked_at', '?')}  {row.get('verdict')}{match_text}  "
            f"tx {render.short(tx, 10, 6)}  via {row.get('source')}  ({row.get('reason')})")


def _settings_lines(runtime) -> list[str]:
    s = runtime.settings
    lines = [
        f"mode: {s.mode}",
        f"rpc: {endpoint_label(s.rpc_url)} (Base mainnet, chain 8453)",
        f"payment tools watched: {', '.join(s.payment_tools) if s.payment_tools else '(none configured)'}",
        f"min confirmations: {s.min_confirmations}; rpc timeout: {s.timeout_seconds:.0f}s",
        f"annotate tool results: {'yes' if s.annotate_tool_results else 'no'}",
    ]
    for warning in s.warnings:
        lines.append(f"settings warning: {warning}")
    return lines


def handle(runtime, raw_args: str, *, session_id: str = "", **_: Any) -> str:
    try:
        parts = shlex.split(raw_args or "")
    except ValueError:
        parts = (raw_args or "").split()
    sub = parts[0].lower() if parts else ""
    ledger = runtime.ledger()

    if sub in ("help", "-h", "--help"):
        return USAGE

    if sub == "check":
        if len(parts) < 2:
            return "Usage: /proofly check <tx_hash> [to=0x... amount_raw=N token=0x...|usdc|eth from=0x...]"
        intent, error = intent_from_command_tokens(parts[2:])
        if error:
            return f"Could not read the expected transfer ({error}). " \
                   "Use to=0x... amount_raw=<integer base units> [token=0x...|usdc|eth] [from=0x...]."
        result = runtime.verify_hash(parts[1][:100], intent, runtime.new_deadline())
        runtime.record(result, session_id=session_id, turn_id="", source="command")
        lines = [render.one_line(result)]
        if result.detail and result.detail not in lines[0]:
            lines.append(result.detail)
        if result.explorer_url:
            lines.append(render.inspect_line(result))
        if intent is None:
            lines.append("No expected transfer given: this is an execution check only, not proof of a payment.")
        lines.append(f"(checked via {result.rpc_endpoint_label} at {result.checked_at})")
        return "\n".join(lines)

    if sub == "clear":
        if ledger is None:
            return "Proofly ledger is unavailable."
        if not session_id:
            return "No session context: nothing cleared."
        removed = ledger.clear_session(session_id)
        return f"Deleted {removed} Proofly record(s) for this chat."

    if sub == "status":
        lines = ["Proofly status"] + _settings_lines(runtime)
        if ledger is not None:
            counts = ledger.counts()
            lines.append("verdicts in this profile: " + (", ".join(f"{k}={v}" for k, v in counts.items())
                                                         if counts else "none yet"))
        else:
            lines.append("ledger: unavailable")
        stats = runtime.stats
        lines.append(f"this process: {stats['tool_results_annotated']} tool results annotated, "
                     f"{stats['replies_rewritten']} replies checked, {stats['errors']} errors")
        return "\n".join(lines)

    if ledger is None:
        return "Proofly ledger is unavailable."
    if not session_id:
        return "No session context yet: run /proofly after a message in this chat.\n\n" + "\n".join(
            _settings_lines(runtime))

    if sub == "last":
        rows = ledger.recent(session_id=session_id, limit=1)
        if not rows:
            return "No Proofly verdicts in this chat yet."
        row = rows[0]
        lines = [_row_line(row)]
        if row.get("expected"):
            lines.append(f"expected: {row['expected']}")
        if row.get("observed"):
            lines.append(f"observed: {row['observed']}")
        if row.get("tx_hash"):
            lines.append(f"Inspect: https://basescan.org/tx/{row['tx_hash']}")
        return "\n".join(lines)

    if sub == "history" or sub == "":
        limit = 5
        if sub == "history" and len(parts) > 1 and parts[1].isdigit():
            limit = max(1, min(int(parts[1]), 20))
        rows = ledger.recent(session_id=session_id, limit=limit)
        lines = ["Proofly: recent verdicts in this chat"]
        lines += [_row_line(r) for r in rows] if rows else ["(none yet)"]
        if sub == "":
            lines += [""] + _settings_lines(runtime)
        return "\n".join(lines)

    return f"Unknown subcommand {sub[:20]!r}.\n\n{USAGE}"
