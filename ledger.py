"""Bounded local verdict ledger (SQLite).

Stored per row: an HMAC of the session id (never the raw id), an HMAC of the turn id, source,
tx hash, chain id, verdict, reason code, transfer_match, the expected/observed transfer fields
needed for debugging, the RPC endpoint label (scheme + host only) and a timestamp. No
conversation text, no raw tool output, no RPC URL path or query, no credentials.

The connection comes from a factory so Hermes can hand in ``plugin_db("proofly")`` (profile
scoped, outside the install directory) and tests can use ``:memory:`` or a temp file.
No Hermes imports.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from .verifier import ProofResult

SCHEMA_VERSION = 1
DEFAULT_MAX_ROWS = 300


class Ledger:
    def __init__(self, connect: Callable[[], sqlite3.Connection], *, max_rows: int = DEFAULT_MAX_ROWS):
        self._connect = connect
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._salt: Optional[bytes] = None
        self.max_rows = max(10, min(int(max_rows), 5000))

    # -- connection / schema -----------------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = self._connect()
            self._migrate(conn)
            self._conn = conn
        return self._conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS proofly_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = conn.execute("SELECT value FROM proofly_meta WHERE key = 'schema_version'").fetchone()
        version = int(row[0]) if row else 0
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"proofly ledger schema {version} is newer than this plugin ({SCHEMA_VERSION})")
        if version < 1:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS proofly_verdicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checked_at TEXT NOT NULL,
                    session_ref TEXT NOT NULL,
                    turn_ref TEXT NOT NULL,
                    source TEXT NOT NULL,
                    tx_hash TEXT NOT NULL,
                    chain_id INTEGER,
                    verdict TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    transfer_match INTEGER,
                    block_number INTEGER,
                    confirmations INTEGER,
                    expected_json TEXT,
                    observed_json TEXT,
                    rpc_label TEXT NOT NULL,
                    mode TEXT NOT NULL
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS proofly_verdicts_session ON proofly_verdicts(session_ref, id)")
            conn.execute("INSERT OR REPLACE INTO proofly_meta(key, value) VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
        # Future migrations: ``if version < 2: ...`` then bump schema_version, in one transaction.
        salt_row = conn.execute("SELECT value FROM proofly_meta WHERE key = 'ref_salt'").fetchone()
        if salt_row is None:
            conn.execute("INSERT INTO proofly_meta(key, value) VALUES ('ref_salt', ?)", (secrets.token_hex(16),))
        conn.commit()

    def _ref(self, value: str) -> str:
        if not value:
            return ""
        if self._salt is None:
            row = self._db().execute("SELECT value FROM proofly_meta WHERE key = 'ref_salt'").fetchone()
            self._salt = bytes.fromhex(row[0])
        return hmac.new(self._salt, value.encode("utf-8"), hashlib.sha256).hexdigest()[:16]

    # -- writes ------------------------------------------------------------------------------

    def record(self, result: ProofResult, *, session_id: str, turn_id: str, source: str, mode: str) -> None:
        intent = result.intent.to_dict() if result.intent else None
        if intent:
            intent.pop("source", None)
            intent = dict(intent, intent_source=result.intent.source)
        observed = result.evidence.get("matched_transfer") or result.evidence.get("observed_transfer")
        diffs = result.evidence.get("differences")
        observed_doc = {"transfer": observed, "differences": diffs} if (observed or diffs) else None
        match = None if result.transfer_match is None else int(bool(result.transfer_match))
        with self._lock:
            db = self._db()
            db.execute(
                """INSERT INTO proofly_verdicts (checked_at, session_ref, turn_ref, source, tx_hash, chain_id,
                   verdict, reason, transfer_match, block_number, confirmations, expected_json, observed_json,
                   rpc_label, mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (result.checked_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 self._ref(session_id), self._ref(turn_id), source[:64], result.tx_hash[:80], result.chain_id,
                 result.verdict.value, result.reason[:64], match, result.block_number, result.confirmations,
                 json.dumps(intent) if intent else None, json.dumps(observed_doc) if observed_doc else None,
                 result.rpc_endpoint_label[:120], mode[:16]))
            db.execute("DELETE FROM proofly_verdicts WHERE id <= (SELECT MAX(id) FROM proofly_verdicts) - ?",
                       (self.max_rows,))
            db.commit()

    def clear_session(self, session_id: str) -> int:
        if not session_id:
            return 0
        with self._lock:
            db = self._db()
            cur = db.execute("DELETE FROM proofly_verdicts WHERE session_ref = ?", (self._ref(session_id),))
            db.commit()
            return cur.rowcount

    # -- reads -------------------------------------------------------------------------------

    def recent(self, *, session_id: str, limit: int = 5) -> list[dict]:
        if not session_id:
            return []
        limit = max(1, min(int(limit), 50))
        with self._lock:
            rows = self._db().execute(
                """SELECT checked_at, source, tx_hash, verdict, reason, transfer_match, block_number,
                          confirmations, expected_json, observed_json, rpc_label, mode
                   FROM proofly_verdicts WHERE session_ref = ? ORDER BY id DESC LIMIT ?""",
                (self._ref(session_id), limit)).fetchall()
        keys = ("checked_at", "source", "tx_hash", "verdict", "reason", "transfer_match", "block_number",
                "confirmations", "expected", "observed", "rpc_label", "mode")
        out = []
        for row in rows:
            item = dict(zip(keys, row))
            for key in ("expected", "observed"):
                item[key] = json.loads(item[key]) if item[key] else None
            item["transfer_match"] = None if item["transfer_match"] is None else bool(item["transfer_match"])
            out.append(item)
        return out

    def counts(self) -> dict:
        """Aggregate verdict counts across the profile (no hashes, no session refs)."""
        with self._lock:
            rows = self._db().execute(
                "SELECT verdict, COUNT(*) FROM proofly_verdicts GROUP BY verdict ORDER BY verdict").fetchall()
        return {verdict: count for verdict, count in rows}

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
