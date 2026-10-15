"""Hash-chained, append-only audit logger (Deliverable #06 evidence).

Each entry is::

    curr_hash = SHA-256( canonical_json( entry_without_curr_hash ) )     with entry.prev_hash = previous curr_hash

so altering, deleting or re-ordering any historical line breaks every later hash.  Entries are
persisted in SQLite (``audit_logs``, protected by append-only triggers) *and* mirrored to a
JSONL file (``sandbox_operation_logs.jsonl``).  ``details`` is passed through the local PII
scrubber first -- logs never contain raw PII.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..core.pii_scrubber import PIIScrubber
from ..database import Database

GENESIS_HASH = "0" * 64


def canonical(entry: dict) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_hash(entry_without_hash: dict) -> str:
    return hashlib.sha256(canonical(entry_without_hash).encode("utf-8")).hexdigest()


def tier_label(tier: int | None) -> str:
    return f"L{tier}" if tier is not None else "SYSTEM"


def verify_entries(entries: Iterable[dict]) -> dict:
    """Verify a sequence of entries. Returns {ok, count, broken_at, reason}."""
    prev = GENESIS_HASH
    count = 0
    expected_seq = 1
    for e in entries:
        count += 1
        body = {k: v for k, v in e.items() if k != "curr_hash"}
        if e.get("seq") != expected_seq:
            return {"ok": False, "count": count, "broken_at": e.get("seq"), "reason": f"sequence gap (expected {expected_seq})"}
        if e.get("prev_hash") != prev:
            return {"ok": False, "count": count, "broken_at": e.get("seq"), "reason": "prev_hash does not match previous curr_hash"}
        if compute_hash(body) != e.get("curr_hash"):
            return {"ok": False, "count": count, "broken_at": e.get("seq"), "reason": "curr_hash mismatch (entry was modified)"}
        prev = e["curr_hash"]
        expected_seq += 1
    return {"ok": True, "count": count, "broken_at": None, "reason": "chain intact"}


def read_jsonl(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


class AuditLogger:
    def __init__(self, db: Database, jsonl_path: str | Path | None, scrubber: PIIScrubber | None = None) -> None:
        self.db = db
        self.jsonl_path = Path(jsonl_path) if jsonl_path else None
        self.scrubber = scrubber or PIIScrubber()
        self._lock = threading.Lock()
        self._last_seq, self._last_hash = self._load_tail()
        self._sync_file()

    # ------------------------------------------------------------------ io
    def _load_tail(self) -> tuple[int, str]:
        rows = self.db.query("SELECT seq, curr_hash FROM audit_logs ORDER BY seq DESC LIMIT 1", tier=0)
        return (rows[0]["seq"], rows[0]["curr_hash"]) if rows else (0, GENESIS_HASH)

    def _sync_file(self) -> None:
        """Make the JSONL mirror consistent with the DB (fresh chain => fresh file)."""
        if not self.jsonl_path:
            return
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        entries = self.entries()
        self.jsonl_path.write_text("".join(canonical(e) + "\n" for e in entries), encoding="utf-8")

    # ----------------------------------------------------------------- api
    def log(self, *, tool: str, outcome: str, tier: int | None = None, session_id: str | None = None,
            actor: str = "agent", action_id: str | None = None, details: dict[str, Any] | None = None) -> dict:
        safe_details = self.scrubber.scrub_obj(details or {})
        with self._lock:
            seq = self._last_seq + 1
            body = {
                "seq": seq,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "session_id": session_id, "tier": tier_label(tier), "actor": actor, "tool": tool,
                "outcome": outcome, "action_id": action_id, "details": safe_details, "prev_hash": self._last_hash,
            }
            curr = compute_hash(body)
            entry = {**body, "curr_hash": curr}
            conn = self.db.connect(3)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO audit_logs (seq, ts, session_id, tier, actor, tool, outcome, action_id, details_json, prev_hash, curr_hash)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (seq, body["timestamp"], session_id, body["tier"], actor, tool, outcome, action_id,
                         json.dumps(safe_details, sort_keys=True, ensure_ascii=False), body["prev_hash"], curr),
                    )
            finally:
                conn.close()
            if self.jsonl_path:
                with open(self.jsonl_path, "a", encoding="utf-8") as f:
                    f.write(canonical(entry) + "\n")
            self._last_seq, self._last_hash = seq, curr
            return entry

    def entries(self, limit: int | None = None, descending: bool = False) -> list[dict]:
        order = "DESC" if descending else "ASC"
        sql = f"SELECT * FROM audit_logs ORDER BY seq {order}" + (f" LIMIT {int(limit)}" if limit else "")
        out = []
        for r in self.db.query(sql, tier=0):
            out.append({
                "seq": r["seq"], "timestamp": r["ts"], "session_id": r["session_id"], "tier": r["tier"], "actor": r["actor"],
                "tool": r["tool"], "outcome": r["outcome"], "action_id": r["action_id"],
                "details": json.loads(r["details_json"]), "prev_hash": r["prev_hash"], "curr_hash": r["curr_hash"],
            })
        return out

    def verify(self) -> dict:
        return verify_entries(self.entries())

    def verify_file(self) -> dict:
        return verify_entries(read_jsonl(self.jsonl_path)) if self.jsonl_path else {"ok": False, "reason": "no file"}

    @property
    def head(self) -> tuple[int, str]:
        return self._last_seq, self._last_hash
