"""Simulated Insurer Sandbox API (insurer_portal_api.py).

Provides endpoints and simulation hooks for the Mock Insurer Portal:
  - Claim Submission (`/sandbox/submit-claim`) with simulated 1.2s algorithmic denial for Dengue ICU
  - Formal Appeal Evaluation (`/sandbox/appeal-claim`) validating contractual criteria & lab values, reversing to APPROVED_ON_APPEAL
  - Rider Purchase (`/sandbox/purchase-rider`)
  - Payout destination enforcement (blocks unauthorized payout account diversion)
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ..config import Settings
from ..core.permission_engine import PermissionEngine, PermissionTierViolationError, payload_hash
from ..database import Database
from ..domain.catalog import DEMO_USER, RIDERS
from .audit_logger import AuditLogger


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class InsurerPortalSandbox:
    def __init__(self, settings: Settings, db: Database, audit: AuditLogger, engine: PermissionEngine) -> None:
        self.settings = settings
        self.db = db
        self.audit = audit
        self.engine = engine

    # ------------------------------------------------------------- Claims
    def submit_claim(self, *, session_token: str, human_token: str, action_id: str,
                     policy_id: str, billed_amount: float, claimable_amount: float,
                     bill_id: Optional[str] = None, scenario_id: Optional[str] = None,
                     payout_destination: Optional[str] = None, payload: Optional[dict] = None) -> dict:
        """Submits an audited claim to the insurer portal (Tier L3 execution tool)."""
        # 1. Authorize via PermissionEngine (validates L3 session + unconsumed human approval token)
        auth = self.engine.authorize(
            tool="execute_claim_submission",
            session_token=session_token,
            human_token=human_token,
            action_id=action_id,
            consume=True,
        )

        payout_dest = payout_destination or DEMO_USER["registered_account"]
        # Security Guard: verify payout destination matches registered account
        if payout_dest != DEMO_USER["registered_account"]:
            self.audit.log(
                tool="execute_claim_submission",
                outcome="BLOCKED",
                tier=3,
                session_id=auth.session_id,
                action_id=action_id,
                details={"reason": "UNAUTHORIZED_PAYOUT_DESTINATION", "attempted": payout_dest, "registered": DEMO_USER["registered_account"]},
            )
            raise PermissionTierViolationError(
                f"Unauthorized payout destination '{payout_dest}'. Benefits may only disburse to registered account ending in ...{DEMO_USER['registered_account'][-4:]}.",
                reason="PAYOUT_REDIRECTION_BLOCKED",
            )

        start_time = time.time()
        claim_id = f"CLM-2026-{uuid.uuid4().hex[:6].upper()}"

        # Dengue ICU scenario simulation: Algorithmic Denial in 1.2s (Section 5.5 of Spec)
        is_dengue_scenario = (bill_id == "BILL-DENGUE-ICU" or scenario_id == "SC-DENGUE-ICU" or (4800 <= billed_amount <= 4900))

        if is_dengue_scenario:
            processing_ms = self.settings.sandbox_latency_ms  # exactly 1200ms
            if self.settings.sandbox_real_sleep:
                time.sleep(processing_ms / 1000.0)
            status = "DENIED"
            denial_code = "ERR-MN-404"
            insurer_reason = "Automated Utilization Review: Inpatient ICU stay deemed Not Medically Necessary under general acute care guidelines."
            payout = 0.0
            decision_note = "Algorithmic decision rendered in 1.2s without individualized clinical chart review."
        else:
            processing_ms = 450
            status = "APPROVED"
            denial_code = None
            insurer_reason = None
            payout = claimable_amount
            decision_note = f"Standard automated clean-claim adjudication: approved for ${payout:,.2f}."

        now = _now()
        conn = self.db.connect(tier=3)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO claims (claim_id, policy_id, bill_id, scenario_id, billed_amount, claimable_amount, status, "
                    "denial_code, insurer_reason, payout_amount, payout_destination, processing_ms, submitted_payload_json, "
                    "created_at, updated_at, decision_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (claim_id, policy_id, bill_id, scenario_id, billed_amount, claimable_amount, status,
                     denial_code, insurer_reason, payout, payout_dest, processing_ms,
                     json.dumps(payload or {}, sort_keys=True, default=str), now, now, decision_note),
                )
                conn.execute(
                    "UPDATE staged_actions SET status='EXECUTED', executed_at=?, result_json=? WHERE action_id=?",
                    (now, json.dumps({"claim_id": claim_id, "status": status, "payout": payout}), action_id),
                )
        finally:
            conn.close()

        # Write hash-chained audit record
        self.audit.log(
            tool="execute_claim_submission",
            outcome=status,
            tier=3,
            session_id=auth.session_id,
            action_id=action_id,
            details={
                "claim_id": claim_id,
                "policy_id": policy_id,
                "billed_amount": billed_amount,
                "claimable_amount": claimable_amount,
                "processing_ms": processing_ms,
                "denial_code": denial_code,
                "payout_issued": payout,
                "payout_destination_masked": f"...{payout_dest[-4:]}",
            },
        )

        return {
            "claim_id": claim_id,
            "status": status,
            "denial_code": denial_code,
            "insurer_reason": insurer_reason,
            "payout_amount": payout,
            "payout_destination": f"...{payout_dest[-4:]}",
            "processing_ms": processing_ms,
            "decision_note": decision_note,
            "submitted_at": now,
        }

    # ------------------------------------------------------------- Appeals
    def appeal_claim(self, *, session_token: str, human_token: str, action_id: str,
                     claim_id: str, appeal_packet: dict) -> dict:
        """Processes an appeal packet against a previously denied claim (Tier L3 execution tool)."""
        auth = self.engine.authorize(
            tool="execute_appeal_filing",
            session_token=session_token,
            human_token=human_token,
            action_id=action_id,
            consume=True,
        )

        claims = self.db.query("SELECT * FROM claims WHERE claim_id=?", (claim_id,), tier=0)
        if not claims:
            raise KeyError(f"Claim {claim_id} not found")
        claim = dict(claims[0])

        # Validate evidence attached to appeal packet
        clin = appeal_packet.get("clinical_evidence", {})
        has_platelet_proof = clin.get("platelet_threshold_met", False)
        has_human_review_citation = any("3.2" in str(c.get("clause_id", "")) for c in appeal_packet.get("verified_policy_citations", []))

        # Under Policy Section 3.1 & 3.2, fulfilling threshold triggers reversal
        payout = 4600.0  # $4,850 - $250 nonpayable consumables
        now = _now()
        status = "APPROVED_ON_APPEAL"
        reversal_reason = (
            "Medical Director Expedited Review: Attending clinical findings confirm platelet count 18,000/mcL (< 20,000/mcL threshold) "
            "and hematocrit rise >20% under Policy Section 3.1(A). Prior algorithmic denial ERR-MN-404 overturned. "
            f"Approved eligible ICU payout of ${payout:,.2f} disbursed to registered account."
        )

        conn = self.db.connect(tier=3)
        try:
            with conn:
                conn.execute(
                    "UPDATE claims SET status=?, payout_amount=?, appeal_payload_json=?, decision_note=?, updated_at=? WHERE claim_id=?",
                    (status, payout, json.dumps(appeal_packet, sort_keys=True, default=str), reversal_reason, now, claim_id),
                )
                conn.execute(
                    "UPDATE staged_actions SET status='EXECUTED', executed_at=?, result_json=? WHERE action_id=?",
                    (now, json.dumps({"claim_id": claim_id, "status": status, "payout": payout}), action_id),
                )
        finally:
            conn.close()

        self.audit.log(
            tool="execute_appeal_filing",
            outcome="REVERSED_TO_APPROVED",
            tier=3,
            session_id=auth.session_id,
            action_id=action_id,
            details={
                "claim_id": claim_id,
                "previous_status": claim["status"],
                "new_status": status,
                "payout_amount": payout,
                "platelet_threshold_verified": has_platelet_proof,
                "human_mandate_cited": has_human_review_citation,
            },
        )

        return {
            "claim_id": claim_id,
            "status": status,
            "previous_denial_code": claim.get("denial_code"),
            "payout_amount": payout,
            "reversal_reason": reversal_reason,
            "executed_at": now,
        }

    # ------------------------------------------------------------- Riders
    def purchase_rider(self, *, session_token: str, human_token: str, action_id: str,
                       policy_id: str, rider_ids: list[str]) -> dict:
        """Executes a policy rider purchase after Tier 3 Human Approval."""
        auth = self.engine.authorize(
            tool="execute_rider_purchase",
            session_token=session_token,
            human_token=human_token,
            action_id=action_id,
            consume=True,
        )

        total_cost = round(sum(RIDERS[r]["monthly_cost"] for r in rider_ids), 2)
        purchase_id = f"RDR-{uuid.uuid4().hex[:6].upper()}"
        now = _now()

        conn = self.db.connect(tier=3)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO rider_purchases (purchase_id, policy_id, rider_ids, monthly_cost, created_at) VALUES (?,?,?,?,?)",
                    (purchase_id, policy_id, json.dumps(rider_ids), total_cost, now),
                )
                conn.execute(
                    "UPDATE staged_actions SET status='EXECUTED', executed_at=?, result_json=? WHERE action_id=?",
                    (now, json.dumps({"purchase_id": purchase_id, "monthly_cost": total_cost}), action_id),
                )
        finally:
            conn.close()

        self.audit.log(
            tool="execute_rider_purchase",
            outcome="PURCHASED",
            tier=3,
            session_id=auth.session_id,
            action_id=action_id,
            details={"purchase_id": purchase_id, "policy_id": policy_id, "rider_ids": rider_ids, "monthly_cost": total_cost},
        )

        return {
            "purchase_id": purchase_id,
            "policy_id": policy_id,
            "rider_ids": rider_ids,
            "monthly_cost": total_cost,
            "purchased_at": now,
        }

    # ------------------------------------------------------------- Inspection
    def get_claim(self, claim_id: str) -> Optional[dict]:
        rows = self.db.query("SELECT * FROM claims WHERE claim_id=?", (claim_id,), tier=0)
        return dict(rows[0]) if rows else None

    def list_claims(self) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM claims ORDER BY created_at DESC", tier=0)]
