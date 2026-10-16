"""Workflow 2 - Coverage-Gap Stress Tester & Portfolio Optimizer (L0 analysis, L2 rider staging)."""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

from ..core.permission_engine import PermissionTierViolationError
from ..core.simulation import (
    AXES, SCENARIO_WEIGHTS, apply_riders, gap_stress_pct, overall_score, radar_scores, recommend_riders, simulate_scenario,
)
from ..domain.catalog import RIDERS, SCENARIOS
from .staging import stage_action


class BudgetCapExceededError(PermissionTierViolationError):
    """A rider selection would cost more than the configured share of disposable income."""

    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(message, reason="BUDGET_CAP_EXCEEDED", **kw)


class CoverageGapAgent:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------- helpers
    def user_profile(self) -> dict:
        rows = self.ctx.db.query("SELECT * FROM users LIMIT 1", tier=0)
        return dict(rows[0]) if rows else {"monthly_income": 3000.0, "disposable_income": 1000.0, "display_name": "Demo User"}

    def budget_cap(self, disposable_income: float) -> float:
        return round(self.ctx.settings.budget_cap_ratio * disposable_income, 2)

    def waiting_status(self, policy_id: str, today: date) -> dict:
        doc = self.ctx.policies.get(policy_id)
        rows = self.ctx.db.query("SELECT start_date FROM policies WHERE policy_id=?", (policy_id,), tier=0)
        if not rows:
            return {"active": False}
        start = date.fromisoformat(rows[0]["start_date"])
        elapsed = (today - start).days
        days = doc.terms.waiting_days
        return {"start_date": start.isoformat(), "elapsed_days": elapsed, "waiting_days": days, "active": elapsed < days,
                "remaining_days": max(0, days - elapsed)}

    # ------------------------------------------------------------- L0 tools
    def tool_list_riders(self, **_: Any) -> dict:
        return {"riders": list(RIDERS.values())}

    def tool_run_simulation(self, policy_id: str, monthly_income: Optional[float] = None, disposable_income: Optional[float] = None,
                            rider_ids: tuple | list = (), **_: Any) -> dict:
        ctx = self.ctx
        doc = ctx.policies.get(policy_id)
        prof = self.user_profile()
        monthly_income = float(monthly_income if monthly_income is not None else prof["monthly_income"])
        disposable = float(disposable_income if disposable_income is not None else prof["disposable_income"])
        terms = apply_riders(doc.terms, rider_ids)
        results = {sid: simulate_scenario(doc.terms, sc, rider_ids) for sid, sc in SCENARIOS.items()}
        worst = max(r.gap for r in results.values())
        axes = radar_scores(terms, worst, monthly_income)
        cap = self.budget_cap(disposable)
        rows = []
        for sid, sc in SCENARIOS.items():
            r = results[sid]
            rec = recommend_riders(doc.terms, sc, cap) if not rider_ids else recommend_riders(terms, sc, cap)
            rows.append({
                "scenario_id": sid, "scenario": sc.name, "description": sc.description,
                "bill_total": r.bill_total, "payout": r.payout, "gap": r.gap,
                "trigger": r.trigger_text(), "triggers": r.top_triggers[:4],
                "recommendation": rec,
                "recommended_rider_text": ("None needed" if not rec["rider_ids"] else
                                           " + ".join(f"{x['name'].split(' (')[0]} (${x['monthly_cost']:.2f}/mo)" for x in rec["riders"])),
            })
        return {
            "policy_id": policy_id, "policy_name": doc.name, "applied_riders": list(rider_ids),
            "axes": [{"key": k, "label": lbl, "score": axes[k]} for k, lbl in AXES],
            "overall_score": overall_score(axes), "gap_stress_pct": gap_stress_pct(results),
            "scenarios": rows, "worst_case_gap": worst, "monthly_income": monthly_income,
            "disposable_income": disposable, "monthly_rider_budget_cap": cap,
            "waiting_status": self.waiting_status(policy_id, ctx.settings.today),
            "terms": doc.terms.as_dict(), "term_sources": doc.terms.sources,
            "scenario_weights": SCENARIO_WEIGHTS,
        }

    # --------------------------------------------------------------- L2 tool
    def tool_stage_rider(self, session_id: str = "", policy_id: str = "", rider_ids: list | tuple = (),
                         disposable_income: Optional[float] = None, **_: Any) -> dict:
        prof = self.user_profile()
        disp = float(disposable_income) if disposable_income is not None else prof["disposable_income"]
        cost = round(sum(RIDERS[r]["monthly_cost"] for r in rider_ids), 2)
        cap = self.budget_cap(disp)
        if not rider_ids:
            raise ValueError("No riders selected")
        if cost > cap + 1e-9:
            raise BudgetCapExceededError(
                f"Rider selection costs ${cost:.2f}/month, above the {self.ctx.settings.budget_cap_ratio:.0%} disposable-income cap of ${cap:.2f}")
        payload = {"policy_id": policy_id, "rider_ids": list(rider_ids), "monthly_cost": cost, "budget_cap": cap,
                   "riders": [RIDERS[r]["name"] for r in rider_ids]}
        return stage_action(self.ctx, session_id=session_id, action_type="rider_purchase", payload=payload,
                            summary=f"Purchase {len(rider_ids)} rider(s) for ${cost:.2f}/month")

    # --------------------------------------------------------- orchestrated API
    def analyze(self, session_token: str, policy_id: str, rider_ids: tuple | list = (), monthly_income: Optional[float] = None,
                disposable_income: Optional[float] = None) -> dict:
        return self.ctx.gateway.call(session_token, "run_coverage_simulation", policy_id=policy_id, rider_ids=list(rider_ids),
                                     monthly_income=monthly_income, disposable_income=disposable_income)

    def compare_portfolio(self, session_token: str, **kw: Any) -> list[dict]:
        return [self.analyze(session_token, pid, **kw) for pid in self.ctx.policies.policy_ids()]

    def stage_riders(self, session_token: str, policy_id: str, rider_ids: list[str]) -> dict:
        return self.ctx.gateway.call(session_token, "stage_rider_selection", policy_id=policy_id, rider_ids=list(rider_ids))
