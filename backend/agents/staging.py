"""Tier-2 staging helper: the ONLY write path available to an L2 agent session.

The INSERT runs on a connection guarded by an SQLite authorizer (see ``database._l2_authorizer``)
that permits writes to ``staged_actions`` alone.  The row always starts as ``PENDING_HUMAN_APPROVAL``
and its SHA-256 payload hash is later bound into the human approval token.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from ..core.permission_engine import payload_hash

EXECUTION_TOOL = {
    "claim_submission": "execute_claim_submission",
    "appeal_filing": "execute_appeal_filing",
    "rider_purchase": "execute_rider_purchase",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def stage_action(ctx: Any, *, session_id: str, action_type: str, payload: dict[str, Any], summary: str) -> dict:
    action_id = "ACT-" + uuid.uuid4().hex[:10].upper()
    sha = payload_hash(payload)
    conn = ctx.db.connect(tier=2)  # authorizer-restricted connection
    try:
        with conn:
            conn.execute(
                "INSERT INTO staged_actions (action_id, action_type, tool, tier, status, payload_json, payload_sha256,"
                " created_by_session, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (action_id, action_type, EXECUTION_TOOL[action_type], 3, "PENDING_HUMAN_APPROVAL",
                 json.dumps(payload, sort_keys=True, default=str), sha, session_id, _now(), _now()),
            )
    finally:
        conn.close()
    ctx.engine.create_challenge(action_id)  # OTP is delivered to the user's device (not to the agent)
    return {"action_id": action_id, "action_type": action_type, "status": "PENDING_HUMAN_APPROVAL",
            "required_tier": "L3", "execution_tool": EXECUTION_TOOL[action_type], "payload_sha256": sha, "summary": summary,
            "payload": payload}


def load_action(ctx: Any, action_id: str) -> dict | None:
    rows = ctx.db.query("SELECT * FROM staged_actions WHERE action_id=?", (action_id,), tier=0)
    if not rows:
        return None
    r = rows[0]
    d = dict(r)
    d["payload"] = json.loads(r["payload_json"])
    d["result"] = json.loads(r["result_json"]) if r["result_json"] else None
    return d
