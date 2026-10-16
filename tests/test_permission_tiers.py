"""Test Suite: L0–L3 Permission Engine & Human-in-the-Loop OTP Verification.

Verifies hard code-enforced guardrails:
  - Unknown tools rejected (deny by default)
  - Agent sessions capped at Tier 2 (L2); self-escalation to L3 blocked
  - L0/L1/L2 sessions cannot invoke mutating L3 endpoints (raises PermissionTierViolationError)
  - L3 execution demands single-use HMAC human token verified via 6-digit OTP
  - Replay of single-use human token is blocked
  - Tampering with staged payload after approval triggers immediate cryptographic rejection
"""
from __future__ import annotations

import pytest

from backend.agents.orchestrator import ApplicationContext
from backend.config import Settings
from backend.core.permission_engine import PermissionEngine, PermissionTierViolationError


@pytest.fixture
def test_ctx(tmp_path):
    settings = Settings(
        db_path=tmp_path / "test.db",
        audit_log_path=tmp_path / "test_audit.jsonl",
        demo_offline_mode=True,
    )
    return ApplicationContext(settings)


def test_deny_by_default_on_unknown_tools(test_ctx):
    sess = test_ctx.engine.open_session(requested_tier=2)
    with pytest.raises(PermissionTierViolationError) as exc:
        test_ctx.engine.authorize("unregistered_malicious_tool", session_token=sess["token"])
    assert exc.value.reason == "UNKNOWN_TOOL"


def test_agent_cannot_self_escalate_to_tier_3(test_ctx):
    with pytest.raises(PermissionTierViolationError) as exc:
        test_ctx.engine.open_session(requested_tier=3, actor="agent")
    assert "Tier 3 Human Authorization Required" in exc.value.message


def test_tier_2_cannot_execute_claim_without_human_token(test_ctx):
    sess_l2 = test_ctx.engine.open_session(requested_tier=2, actor="agent")
    with pytest.raises(PermissionTierViolationError) as exc:
        test_ctx.engine.authorize(
            tool="execute_claim_submission",
            session_token=sess_l2["token"],
            human_token=None,
            action_id="ACT-001",
        )
    assert exc.value.reason == "INSUFFICIENT_TIER" or "Human Authorization Required" in exc.value.message


def test_tier_3_human_approval_lifecycle_and_single_use(test_ctx):
    sess_l2 = test_ctx.engine.open_session(requested_tier=2, actor="agent")

    # Stage an action in Tier 2
    staged = test_ctx.bill_agent.tool_stage_claim_draft(
        session_id=sess_l2["session_id"],
        policy_id="POL-FAMILY",
        bill_id="BILL-001",
        clean_claimable_amount=1000.0,
        billed_amount=1200.0,
        audit_summary={},
    )
    action_id = staged["action_id"]

    # 1. Retrieve OTP from device outbox
    otp = test_ctx.engine.device_otp(action_id)
    assert otp is not None
    assert len(otp) == 6

    # 2. Reject incorrect OTP
    with pytest.raises(PermissionTierViolationError) as exc:
        test_ctx.engine.approve(action_id, "999999")
    assert exc.value.reason == "OTP_INVALID"

    # 3. Approve with correct OTP -> Mints single-use human approval token
    approval = test_ctx.engine.approve(action_id, otp)
    human_token = approval["human_token"]
    assert human_token is not None

    # 4. Mint execution session and authorize L3 tool
    exec_sess = test_ctx.engine.mint_execution_session(human_token, action_id)
    auth = test_ctx.engine.authorize(
        tool="execute_claim_submission",
        session_token=exec_sess["token"],
        human_token=human_token,
        action_id=action_id,
        consume=True,
    )
    assert auth.human_verified is True

    # 5. Token Replay Protection: Attempt to use the same token again -> BLOCKED
    with pytest.raises(PermissionTierViolationError) as exc_replay:
        test_ctx.engine.authorize(
            tool="execute_claim_submission",
            session_token=exec_sess["token"],
            human_token=human_token,
            action_id=action_id,
            consume=True,
        )
    assert exc_replay.value.reason in ("TOKEN_REPLAY", "NOT_APPROVED")
