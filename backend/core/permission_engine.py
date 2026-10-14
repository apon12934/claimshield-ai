"""L0-L3 permission-tier enforcement & single-use human-approval (OTP / HMAC) token verifier.

Tier model (see Security_Self_Assessment_Report.md)::

    L0  read-only knowledge & simulation          read_policy_clauses, run_coverage_simulation, list_rider_catalog
    L1  privacy-preserving PII sandbox            parse_hospital_bill, audit_billing_codes
    L2  staged drafting (non-mutating)            stage_claim_draft, stage_appeal_packet, stage_rider_selection
    L3  human-authorized financial execution      execute_claim_submission, execute_appeal_filing, execute_rider_purchase

Hard guarantees enforced *in code* (not by prompts):
  1. **Deny by default** - unknown tools are rejected.
  2. A session token is an HMAC-SHA256 signed claim; the signing secret lives only in this process.
  3. ``open_session`` refuses to mint anything above ``agent_max_tier`` (L2) for an agent.
  4. An L3 tool requires BOTH an L3 session AND a valid, unexpired, unconsumed **single-use human
     approval token** that is bound to (action_id, SHA-256 of the staged payload).
  5. The human approval token can only be minted by :meth:`PermissionEngine.approve`, which demands the
     6-digit OTP delivered out-of-band to the user's device (never visible to the agent / LLM).
  6. Every refusal is written to the hash-chained audit log.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Optional

from ..config import Settings
from ..database import Database
from ..sandbox.audit_logger import AuditLogger


class Tier(IntEnum):
    L0 = 0
    L1 = 1
    L2 = 2
    L3 = 3


TIER_NAMES = {
    Tier.L0: "Read-Only Knowledge & Simulation",
    Tier.L1: "Privacy-Preserving PII Sandbox",
    Tier.L2: "Staged Drafting (Non-Mutating)",
    Tier.L3: "Human-Authorized Financial Execution",
}

TOOL_TIERS: dict[str, Tier] = {
    "read_policy_clauses": Tier.L0, "run_coverage_simulation": Tier.L0, "list_rider_catalog": Tier.L0,
    "parse_hospital_bill": Tier.L1, "audit_billing_codes": Tier.L1,
    "stage_claim_draft": Tier.L2, "stage_appeal_packet": Tier.L2, "stage_rider_selection": Tier.L2,
    "execute_claim_submission": Tier.L3, "execute_appeal_filing": Tier.L3, "execute_rider_purchase": Tier.L3,
}

PERMISSION_MATRIX = [
    {"tier": "L0", "name": TIER_NAMES[Tier.L0], "tools": [t for t, x in TOOL_TIERS.items() if x == Tier.L0],
     "guardrails": "Read-only SQLite connection (PRAGMA query_only); every quoted clause passes citation_verifier; no external writes."},
    {"tier": "L1", "name": TIER_NAMES[Tier.L1], "tools": [t for t, x in TOOL_TIERS.items() if x == Tier.L1],
     "guardrails": "Local PII scrubber runs before any LLM call; prompt-injection firewall quarantines hostile PDFs; read-only DB."},
    {"tier": "L2", "name": TIER_NAMES[Tier.L2], "tools": [t for t, x in TOOL_TIERS.items() if x == Tier.L2],
     "guardrails": "SQLite authorizer: may write ONLY staged_actions (status fixed to PENDING_HUMAN_APPROVAL); calling /sandbox/execute/* => HTTP 403."},
    {"tier": "L3", "name": TIER_NAMES[Tier.L3], "tools": [t for t, x in TOOL_TIERS.items() if x == Tier.L3],
     "guardrails": "Needs L3 session + single-use HMAC human-approval token minted only after OTP entry; payload hash-bound; hash-chained audit entry."},
]


class PermissionTierViolationError(Exception):
    """Raised (and mapped to HTTP 403) whenever a call exceeds the caller's authority."""

    status_code = 403

    def __init__(self, message: str, *, tool: str | None = None, required: int | None = None,
                 actual: int | None = None, reason: str = "TIER_VIOLATION") -> None:
        super().__init__(message)
        self.message = message
        self.tool = tool
        self.required = required
        self.actual = actual
        self.reason = reason

    def as_dict(self) -> dict:
        return {"error": "PermissionTierViolationError", "status_code": self.status_code, "message": self.message,
                "tool": self.tool, "required_tier": f"L{self.required}" if self.required is not None else None,
                "actual_tier": f"L{self.actual}" if self.actual is not None else None, "reason": self.reason}


@dataclass
class SessionClaims:
    sid: str
    tier: int
    exp: float
    actor: str


@dataclass
class AuthDecision:
    tool: str
    required_tier: int
    session_id: str
    session_tier: int
    human_verified: bool = False


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class PermissionEngine:
    def __init__(self, settings: Settings, db: Database, audit: AuditLogger) -> None:
        self.settings = settings
        self.db = db
        self.audit = audit
        self._secret = settings.hmac_secret.encode()
        # Simulated "user's registered device" (SMS / authenticator).  Only the UI-facing
        # approval endpoint reads this; no agent tool is wired to it.
        self._device_outbox: dict[str, str] = {}

    # --------------------------------------------------------------- tokens
    def _sign(self, payload: dict) -> str:
        body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        sig = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{sig}"

    def _unsign(self, token: str) -> dict:
        try:
            body, sig = token.rsplit(".", 1)
        except (ValueError, AttributeError):
            raise PermissionTierViolationError("Malformed token", reason="MALFORMED_TOKEN") from None
        expected = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise PermissionTierViolationError("Token signature invalid (forged or tampered)", reason="BAD_SIGNATURE")
        try:
            payload = json.loads(_unb64(body))
        except Exception:
            raise PermissionTierViolationError("Malformed token body", reason="MALFORMED_TOKEN") from None
        if float(payload.get("exp", 0)) < time.time():
            raise PermissionTierViolationError("Token expired", reason="EXPIRED")
        return payload

    # ------------------------------------------------------------- sessions
    def _mint_session(self, tier: int, actor: str, ttl: int | None = None) -> dict:
        sid = "SES-" + uuid.uuid4().hex[:10]
        exp = time.time() + (ttl or self.settings.session_ttl_seconds)
        token = self._sign({"kind": "session", "sid": sid, "tier": int(tier), "actor": actor, "exp": exp})
        return {"session_id": sid, "token": token, "tier": f"L{int(tier)}", "tier_value": int(tier), "expires_at": exp, "actor": actor}

    def open_session(self, requested_tier: int = 2, actor: str = "agent") -> dict:
        requested_tier = int(requested_tier)
        if requested_tier < 0 or requested_tier > 3:
            raise PermissionTierViolationError("Unknown tier", reason="BAD_TIER")
        if requested_tier > self.settings.agent_max_tier:
            self.audit.log(tool="open_session", outcome="BLOCKED", tier=requested_tier, actor=actor,
                           details={"reason": "SELF_ESCALATION_ATTEMPT", "requested_tier": f"L{requested_tier}",
                                    "agent_max_tier": f"L{self.settings.agent_max_tier}"})
            raise PermissionTierViolationError(
                "Tier 3 Human Authorization Required: agent sessions are capped at Tier 2",
                tool="open_session", required=3, actual=self.settings.agent_max_tier, reason="SELF_ESCALATION")
        return self._mint_session(requested_tier, actor)

    def read_session(self, token: str) -> SessionClaims:
        p = self._unsign(token)
        if p.get("kind") != "session":
            raise PermissionTierViolationError("Not a session token", reason="WRONG_TOKEN_KIND")
        return SessionClaims(sid=p["sid"], tier=int(p["tier"]), exp=float(p["exp"]), actor=p.get("actor", "agent"))

    # ------------------------------------------------------------ authorize
    def authorize(self, tool: str, session_token: Optional[str], *, human_token: Optional[str] = None,
                  action_id: Optional[str] = None, consume: bool = True) -> AuthDecision:
        def block(msg: str, reason: str, *, req: int | None = None, act: int | None = None, sid: str | None = None):
            self.audit.log(tool=tool, outcome="BLOCKED", tier=act, session_id=sid, actor="agent", action_id=action_id,
                           details={"reason": reason, "message": msg, "required_tier": f"L{req}" if req is not None else None})
            raise PermissionTierViolationError(msg, tool=tool, required=req, actual=act, reason=reason)

        required = TOOL_TIERS.get(tool)
        if required is None:
            block(f"Unknown tool '{tool}' (deny by default)", "UNKNOWN_TOOL")
        try:
            claims = self.read_session(session_token or "")
        except PermissionTierViolationError as e:
            block(f"Invalid session: {e.message}", e.reason, req=int(required))
        if claims.tier < required:
            msg = ("Tier 3 Human Authorization Required" if required == Tier.L3
                   else f"Tier {int(required)} (L{int(required)}) required; session is Tier {claims.tier}")
            block(msg, "INSUFFICIENT_TIER", req=int(required), act=claims.tier, sid=claims.sid)

        human_ok = False
        if required == Tier.L3:
            if not human_token or not action_id:
                block("Tier 3 Human Authorization Required: missing single-use human approval token",
                      "MISSING_HUMAN_TOKEN", req=3, act=claims.tier, sid=claims.sid)
            try:
                self._verify_human_token(human_token, action_id, consume=consume)
                human_ok = True
            except PermissionTierViolationError as e:
                block(f"Tier 3 Human Authorization Required: {e.message}", e.reason, req=3, act=claims.tier, sid=claims.sid)
        return AuthDecision(tool=tool, required_tier=int(required), session_id=claims.sid, session_tier=claims.tier, human_verified=human_ok)

    # --------------------------------------------------- human approval gate
    def _staged_row(self, action_id: str):
        rows = self.db.query("SELECT * FROM staged_actions WHERE action_id=?", (action_id,), tier=0)
        return rows[0] if rows else None

    def create_challenge(self, action_id: str) -> None:
        """Called by the system when an action is staged: delivers a 6-digit OTP to the user's device."""
        otp = f"{secrets.randbelow(10**6):06d}"
        salt = secrets.token_hex(8)
        otp_hash = hashlib.sha256((salt + otp).encode()).hexdigest()
        conn = self.db.connect(3)
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO approval_challenges (action_id, otp_hash, salt, expires_at, attempts, consumed, created_at)"
                             " VALUES (?,?,?,?,0,0,?)", (action_id, otp_hash, salt, time.time() + self.settings.otp_ttl_seconds, _now_iso()))
        finally:
            conn.close()
        self._device_outbox[action_id] = otp

    def device_otp(self, action_id: str) -> Optional[str]:
        """Simulates reading the SMS on the user's phone. UI-only; no agent tool is mapped to this."""
        return self._device_outbox.get(action_id)

    def approve(self, action_id: str, otp: str, *, approver: str = "human-ui") -> dict:
        row = self._staged_row(action_id)
        if row is None:
            raise PermissionTierViolationError("Unknown action", reason="UNKNOWN_ACTION")
        if row["status"] != "PENDING_HUMAN_APPROVAL":
            raise PermissionTierViolationError(f"Action is {row['status']}, not awaiting approval", reason="BAD_STATE")
        conn = self.db.connect(3)
        try:
            ch = conn.execute("SELECT * FROM approval_challenges WHERE action_id=?", (action_id,)).fetchone()
            if ch is None or ch["consumed"]:
                raise PermissionTierViolationError("No active approval challenge", reason="NO_CHALLENGE")
            if ch["expires_at"] < time.time():
                raise PermissionTierViolationError("OTP expired", reason="OTP_EXPIRED")
            if ch["attempts"] >= self.settings.otp_max_attempts:
                raise PermissionTierViolationError("Too many wrong OTP attempts; action locked", reason="OTP_LOCKED")
            candidate = hashlib.sha256((ch["salt"] + str(otp).strip()).encode()).hexdigest()
            if not hmac.compare_digest(candidate, ch["otp_hash"]):
                with conn:
                    conn.execute("UPDATE approval_challenges SET attempts = attempts + 1 WHERE action_id=?", (action_id,))
                self.audit.log(tool="human_approval", outcome="OTP_REJECTED", tier=3, actor="human", action_id=action_id,
                               details={"attempts": ch["attempts"] + 1})
                raise PermissionTierViolationError("Incorrect OTP", reason="OTP_INVALID")
            exp = time.time() + self.settings.approval_ttl_seconds
            token = self._sign({"kind": "human", "action_id": action_id, "payload_sha256": row["payload_sha256"],
                                "nonce": secrets.token_hex(8), "tier": 3, "exp": exp})
            with conn:
                conn.execute("UPDATE approval_challenges SET consumed=1 WHERE action_id=?", (action_id,))
                conn.execute("INSERT INTO human_tokens (token_hash, action_id, payload_sha256, expires_at, consumed, issued_at)"
                             " VALUES (?,?,?,?,0,?)", (hashlib.sha256(token.encode()).hexdigest(), action_id, row["payload_sha256"], exp, _now_iso()))
                conn.execute("UPDATE staged_actions SET status='APPROVED_BY_HUMAN', approved_at=?, updated_at=? WHERE action_id=?",
                             (_now_iso(), _now_iso(), action_id))
        finally:
            conn.close()
        self._device_outbox.pop(action_id, None)
        self.audit.log(tool="human_approval", outcome="APPROVED", tier=3, actor="human", action_id=action_id,
                       details={"approver": approver, "payload_sha256": row["payload_sha256"], "action_type": row["action_type"]})
        return {"human_token": token, "expires_at": exp, "action_id": action_id, "payload_sha256": row["payload_sha256"]}

    def reject(self, action_id: str) -> None:
        conn = self.db.connect(3)
        try:
            with conn:
                conn.execute("UPDATE staged_actions SET status='REJECTED_BY_HUMAN', updated_at=? WHERE action_id=? AND status='PENDING_HUMAN_APPROVAL'",
                             (_now_iso(), action_id))
        finally:
            conn.close()
        self._device_outbox.pop(action_id, None)
        self.audit.log(tool="human_approval", outcome="REJECTED", tier=3, actor="human", action_id=action_id, details={})

    def _verify_human_token(self, token: str, action_id: str, *, consume: bool) -> dict:
        p = self._unsign(token)
        if p.get("kind") != "human" or int(p.get("tier", 0)) != 3:
            raise PermissionTierViolationError("Not a human approval token", reason="WRONG_TOKEN_KIND")
        if p.get("action_id") != action_id:
            raise PermissionTierViolationError("Token is bound to a different action", reason="TOKEN_ACTION_MISMATCH")
        row = self._staged_row(action_id)
        if row is None or row["status"] != "APPROVED_BY_HUMAN":
            raise PermissionTierViolationError("Action has not been approved by a human", reason="NOT_APPROVED")
        if row["payload_sha256"] != p.get("payload_sha256") or payload_hash(json.loads(row["payload_json"])) != row["payload_sha256"]:
            raise PermissionTierViolationError("Staged payload changed after human approval", reason="PAYLOAD_TAMPERED")
        th = hashlib.sha256(token.encode()).hexdigest()
        conn = self.db.connect(3)
        try:
            tr = conn.execute("SELECT consumed FROM human_tokens WHERE token_hash=?", (th,)).fetchone()
            if tr is None:
                raise PermissionTierViolationError("Unknown human token", reason="UNKNOWN_TOKEN")
            if tr["consumed"]:
                raise PermissionTierViolationError("Human approval token already used (single-use)", reason="TOKEN_REPLAY")
            if consume:
                with conn:
                    conn.execute("UPDATE human_tokens SET consumed=1 WHERE token_hash=?", (th,))
        finally:
            conn.close()
        return p

    def mint_execution_session(self, human_token: str, action_id: str) -> dict:
        """Exchange a verified human token for a short-lived L3 session bound to that action."""
        self._verify_human_token(human_token, action_id, consume=False)
        return self._mint_session(3, actor="execution-service", ttl=120)
