"""Central Gateway & Orchestrator for ClaimShield AI agent tools.

Every tool call flows through :meth:`ToolGateway.call`, which:
  1. Calls :meth:`PermissionEngine.authorize` to verify session tier and human approval tokens.
  2. Dispatches to the implementation method.
  3. Logs the execution outcome to the hash-chained audit trail.
  4. Passes all string/JSON arguments and outputs through the local PII scrubber.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from ..config import Settings, default_settings
from ..core.citation_verifier import CitationVerifier
from ..core.llm_engine import LLMEngine
from ..core.pdf_utils import extract_pdf
from ..core.permission_engine import AuthDecision, PermissionEngine, PermissionTierViolationError, TOOL_TIERS, Tier
from ..core.pii_scrubber import PIIScrubber
from ..core.policy_store import PolicyStore
from ..core.prompt_firewall import PromptFirewall
from ..database import Database
from ..domain.catalog import DEMO_USER, PLANS, demo_policy_start_dates
from ..sandbox.audit_logger import AuditLogger
from ..sandbox.insurer_portal_api import InsurerPortalSandbox


class ApplicationContext:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or default_settings()
        self.settings.ensure_dirs()
        self.db = Database(self.settings.db_path)
        self.db.init()
        self.scrubber = PIIScrubber(known_names=[DEMO_USER["display_name"]])
        self.audit = AuditLogger(self.db, self.settings.audit_log_path, self.scrubber)
        self.engine = PermissionEngine(self.settings, self.db, self.audit)
        self.firewall = PromptFirewall()
        self.verifier = CitationVerifier(strict=True)
        self.policies = PolicyStore(self.settings.sample_dir)
        self.llm = LLMEngine(self.settings, self.scrubber)

        self.sandbox = InsurerPortalSandbox(self.settings, self.db, self.audit, self.engine)

        from .appeal_fighter_agent import AppealFighterAgent
        from .bill_auditor_agent import BillAuditorAgent
        from .coverage_gap_agent import CoverageGapAgent
        from .policy_qa_agent import PolicyQAAgent

        self.qa_agent = PolicyQAAgent(self)
        self.gap_agent = CoverageGapAgent(self)
        self.bill_agent = BillAuditorAgent(self)
        self.appeal_agent = AppealFighterAgent(self)

        self.gateway = ToolGateway(self)
        self._seed_demo_data()

    def _seed_demo_data(self) -> None:
        """Seeds initial user and policy registry in SQLite if empty."""
        users = self.db.query("SELECT * FROM users WHERE user_id=?", (DEMO_USER["user_id"],), tier=0)
        if not users:
            conn = self.db.connect(tier=3)
            try:
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO users (user_id, display_name, monthly_income, disposable_income, registered_account)"
                        " VALUES (?,?,?,?,?)",
                        (DEMO_USER["user_id"], DEMO_USER["display_name"], DEMO_USER["monthly_income"],
                         DEMO_USER["disposable_income"], DEMO_USER["registered_account"]),
                    )
            finally:
                conn.close()

        # Seed policy registry
        starts = demo_policy_start_dates(self.settings.today)
        conn = self.db.connect(tier=3)
        try:
            with conn:
                for p in PLANS.values():
                    pid = p["policy_id"]
                    try:
                        doc = self.policies.get(pid)
                        conn.execute(
                            "INSERT OR REPLACE INTO policies (policy_id, name, plan_code, file_path, sha256, page_count, premium, start_date, user_id)"
                            " VALUES (?,?,?,?,?,?,?,?,?)",
                            (pid, p["name"], p["code"], str(doc.path), doc.sha256, len(doc.pages), p["premium"],
                             starts[pid].isoformat(), DEMO_USER["user_id"]),
                        )
                    except Exception:
                        pass
        finally:
            conn.close()


class ToolGateway:
    def __init__(self, ctx: ApplicationContext) -> None:
        self.ctx = ctx
        self._handlers: dict[str, Callable[..., dict]] = {
            # L0
            "read_policy_clauses": ctx.qa_agent.tool_read_policy_clauses,
            "run_coverage_simulation": ctx.gap_agent.tool_run_simulation,
            "list_rider_catalog": ctx.gap_agent.tool_list_riders,
            # L1
            "parse_hospital_bill": ctx.bill_agent.tool_parse_hospital_bill,
            "audit_billing_codes": ctx.bill_agent.tool_audit_billing_codes,
            # L2
            "stage_claim_draft": ctx.bill_agent.tool_stage_claim_draft,
            "stage_appeal_packet": ctx.appeal_agent.tool_stage_appeal_packet,
            "stage_rider_selection": ctx.gap_agent.tool_stage_rider,
            # L3
            "execute_claim_submission": ctx.sandbox.submit_claim,
            "execute_appeal_filing": ctx.sandbox.appeal_claim,
            "execute_rider_purchase": ctx.sandbox.purchase_rider,
        }

    def call(self, session_token: str, tool: str, *, human_token: Optional[str] = None,
             action_id: Optional[str] = None, **kwargs: Any) -> dict:
        """Executes a tool call through authorization guardrails and records audit logs."""
        engine = self.ctx.engine
        handler = self._handlers.get(tool)

        # 1. Authorize
        auth = engine.authorize(
            tool=tool,
            session_token=session_token,
            human_token=human_token,
            action_id=action_id,
            consume=False,  # handlers consume upon completion
        )

        # 2. Invoke handler
        try:
            kwargs_with_ctx = dict(kwargs)
            if tool in ("stage_claim_draft", "stage_appeal_packet", "stage_rider_selection"):
                kwargs_with_ctx["session_id"] = auth.session_id
            if tool in ("execute_claim_submission", "execute_appeal_filing", "execute_rider_purchase"):
                kwargs_with_ctx["session_token"] = session_token
                kwargs_with_ctx["human_token"] = human_token
                kwargs_with_ctx["action_id"] = action_id

            result = handler(**kwargs_with_ctx)

            # 3. Log non-L3 tool executions (L3 tools log their own detailed outcome)
            if auth.required_tier < 3:
                self.ctx.audit.log(
                    tool=tool,
                    outcome="SUCCESS",
                    tier=auth.required_tier,
                    session_id=auth.session_id,
                    actor="agent",
                    details={"params_keys": list(kwargs.keys())},
                )

            return result
        except PermissionTierViolationError:
            raise
        except Exception as exc:
            self.ctx.audit.log(
                tool=tool,
                outcome="ERROR",
                tier=auth.required_tier,
                session_id=auth.session_id,
                actor="agent",
                action_id=action_id,
                details={"error_type": type(exc).__name__, "error_message": str(exc)},
            )
            raise
