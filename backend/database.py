"""SQLite state & audit database with *tier-scoped connections*.

    L0 / L1 connections : PRAGMA query_only=ON  (read-only)
    L2 connection       : SQLite authorizer -> may INSERT/UPDATE **only** ``staged_actions``
                          (and never touch its ``status`` column)
    L3 / system         : full access (used by the human-approved execution path & the sandbox insurer)

So "L2 can write only to staged_actions with status='PENDING_HUMAN_APPROVAL'" is enforced by the
database engine itself, not just by application code.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, monthly_income REAL NOT NULL,
    disposable_income REAL NOT NULL, registered_account TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT PRIMARY KEY, name TEXT NOT NULL, plan_code TEXT NOT NULL, file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL, page_count INTEGER NOT NULL, premium REAL NOT NULL, start_date TEXT NOT NULL,
    user_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY, policy_id TEXT NOT NULL, bill_id TEXT, scenario_id TEXT,
    billed_amount REAL NOT NULL, claimable_amount REAL NOT NULL, status TEXT NOT NULL,
    denial_code TEXT, insurer_reason TEXT, payout_amount REAL DEFAULT 0, payout_destination TEXT,
    processing_ms INTEGER, submitted_payload_json TEXT, appeal_payload_json TEXT, decision_note TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staged_actions (
    action_id TEXT PRIMARY KEY, action_type TEXT NOT NULL, tool TEXT NOT NULL, tier INTEGER NOT NULL,
    status TEXT NOT NULL, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
    created_by_session TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    approved_at TEXT, executed_at TEXT, result_json TEXT
);
CREATE TABLE IF NOT EXISTS approval_challenges (
    action_id TEXT PRIMARY KEY, otp_hash TEXT NOT NULL, salt TEXT NOT NULL, expires_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, consumed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS human_tokens (
    token_hash TEXT PRIMARY KEY, action_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
    expires_at REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0, issued_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rider_purchases (
    purchase_id TEXT PRIMARY KEY, policy_id TEXT NOT NULL, rider_ids TEXT NOT NULL,
    monthly_cost REAL NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_logs (
    seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, session_id TEXT, tier TEXT, actor TEXT, tool TEXT NOT NULL,
    outcome TEXT NOT NULL, action_id TEXT, details_json TEXT NOT NULL, prev_hash TEXT NOT NULL, curr_hash TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_logs
BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_logs
BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END;
CREATE TRIGGER IF NOT EXISTS staged_insert_pending BEFORE INSERT ON staged_actions
WHEN NEW.status != 'PENDING_HUMAN_APPROVAL'
BEGIN SELECT RAISE(ABORT, 'staged actions must start as PENDING_HUMAN_APPROVAL'); END;
CREATE TRIGGER IF NOT EXISTS staged_status_flow BEFORE UPDATE OF status ON staged_actions
WHEN NOT (
    (OLD.status = 'PENDING_HUMAN_APPROVAL' AND NEW.status IN ('APPROVED_BY_HUMAN', 'REJECTED_BY_HUMAN'))
    OR (OLD.status = 'APPROVED_BY_HUMAN' AND NEW.status IN ('EXECUTED', 'EXECUTION_FAILED'))
    OR OLD.status = NEW.status
)
BEGIN SELECT RAISE(ABORT, 'illegal staged action status transition'); END;
CREATE TRIGGER IF NOT EXISTS staged_payload_frozen BEFORE UPDATE OF payload_json, payload_sha256 ON staged_actions
WHEN OLD.status != 'PENDING_HUMAN_APPROVAL'
BEGIN SELECT RAISE(ABORT, 'payload is frozen once an action has left PENDING_HUMAN_APPROVAL'); END;
"""

_L2_WRITABLE_COLUMNS = {"payload_json", "payload_sha256", "updated_at"}


def _l2_authorizer(action: int, arg1, arg2, dbname, source) -> int:  # pragma: no cover - exercised via tests
    S = sqlite3
    if action in (S.SQLITE_READ, S.SQLITE_SELECT, S.SQLITE_FUNCTION, S.SQLITE_TRANSACTION, S.SQLITE_SAVEPOINT, S.SQLITE_RECURSIVE):
        return S.SQLITE_OK
    if action == S.SQLITE_INSERT:
        return S.SQLITE_OK if arg1 == "staged_actions" else S.SQLITE_DENY
    if action == S.SQLITE_UPDATE:
        return S.SQLITE_OK if (arg1 == "staged_actions" and arg2 in _L2_WRITABLE_COLUMNS) else S.SQLITE_DENY
    return S.SQLITE_DENY  # DELETE, DDL, PRAGMA, ATTACH, ...


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def init(self) -> None:
        with self.connect(tier=3) as conn:
            conn.executescript(SCHEMA)

    def connect(self, tier: int = 3) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL") if tier >= 3 else None
        if tier <= 1:
            conn.execute("PRAGMA query_only = ON")
        elif tier == 2:
            conn.set_authorizer(_l2_authorizer)
        return conn

    # ----------------------------------------------------------- convenience
    def query(self, sql: str, params: tuple = (), tier: int = 0) -> list[sqlite3.Row]:
        conn = self.connect(tier)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple = ()) -> None:
        conn = self.connect(3)
        try:
            with conn:
                conn.execute(sql, params)
        finally:
            conn.close()
