"""Coverage simulation math: claim payout, 6-axis radar scoring, rider optimisation.

Formal definitions are given in Technical_Documentation.md (section "Coverage-Gap Stress Score").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping, Optional

from ..domain.catalog import (
    BUNDLED_SUPPLY, DIAG, ICU, IMAGING, NONPAYABLE, OTHER, PHARMACY, PROC, PROF, REHAB, RIDERS, ROOM, SURGERY,
    LineItem, Scenario, classify_line,
)
from .policy_store import PolicyTerms

AXES = [
    ("inpatient", "Inpatient & ICU Limits"),
    ("critical", "Critical Illness & Surgery"),
    ("outpatient", "Outpatient, Diagnostics & Pharmacy"),
    ("emergency", "Emergency & Cross-Border"),
    ("waiting", "Waiting & Pre-Existing Exposure"),
    ("oop", "Out-of-Pocket Shock Risk"),
]
AXIS_WEIGHTS = {"inpatient": 0.22, "critical": 0.20, "outpatient": 0.13, "emergency": 0.10, "waiting": 0.15, "oop": 0.20}
SCENARIO_WEIGHTS = {"SC-APPENDECTOMY": 0.30, "SC-DENGUE-ICU": 0.30, "SC-ACL": 0.25, "SC-CARDIAC": 0.15}
BENCHMARKS = {"room": 400.0, "icu": 1200.0, "annual": 150_000.0, "surgery_annual": 100_000.0, "ci": 100_000.0,
              "ambulance": 1_000.0, "travel": 50_000.0, "waiting_days": 90.0, "preexisting_months": 24.0, "oop_months": 6.0}


def clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def line_dict(l: LineItem | Mapping[str, Any]) -> dict:
    if isinstance(l, LineItem):
        return {"code": l.code, "description": l.description, "qty": l.qty, "unit_price": l.unit_price,
                "total": l.total, "category": l.category}
    d = dict(l)
    d.setdefault("total", round(float(d["qty"]) * float(d["unit_price"]), 2))
    d.setdefault("category", classify_line(d.get("code", ""), d.get("description", "")))
    return d


# ---------------------------------------------------------------------------
def apply_riders(terms: PolicyTerms, rider_ids: Iterable[str]) -> PolicyTerms:
    t = terms.copy()
    for rid in rider_ids:
        for fld, (op, val) in RIDERS[rid]["effects"].items():
            cur = getattr(t, fld)
            if op == "max":
                new = max(cur, val)
            elif op == "min":
                new = min(cur, val)
            elif op == "add":
                new = cur + val
            else:
                new = val
            setattr(t, fld, new)
    return t


@dataclass
class SimResult:
    bill_total: float
    payout: float
    gap: float
    nonpayable: float
    triggers: list[dict] = field(default_factory=list)
    ci_payout: float = 0.0
    reimbursement: float = 0.0

    @property
    def top_triggers(self) -> list[dict]:
        return sorted([t for t in self.triggers if t.get("amount", 0) > 0], key=lambda t: t["amount"], reverse=True)

    def trigger_text(self, n: int = 2) -> str:
        top = self.top_triggers[:n]
        if not top:
            return "None - fully covered"
        return "; ".join(f"{t['label']} [{t['clause_id']} p.{t['page']}] (-${t['amount']:,.0f})" for t in top)


def _src(terms: PolicyTerms, fld: str, default_clause: str) -> dict:
    s = terms.sources.get(fld, {})
    return {"clause_id": s.get("clause_id", default_clause), "page": s.get("page", 0)}


def simulate_lines(terms: PolicyTerms, lines: Iterable[LineItem | Mapping[str, Any]], *, emergency: bool,
                   ci_event: Optional[str] = None) -> SimResult:
    L = [line_dict(x) for x in lines]
    bill_total = round(sum(x["total"] for x in L), 2)
    triggers: list[dict] = []

    def trig(kind: str, label: str, fld: str, clause: str, amount: float) -> None:
        if amount > 0.005:
            triggers.append({"kind": kind, "label": label, **_src(terms, fld, clause), "amount": round(amount, 2)})

    inpatient_elig = 0.0
    outpatient_pay = 0.0
    nonpay = 0.0
    room_excess = icu_excess = 0.0
    img_copay_amt = opd_copay_amt = 0.0
    bundled = 0.0
    for x in L:
        cat, qty, unit, tot = x["category"], float(x["qty"]), float(x["unit_price"]), float(x["total"])
        if cat == ROOM:
            elig = qty * min(unit, terms.room_cap)
            inpatient_elig += elig
            room_excess += tot - elig
        elif cat == ICU:
            elig = qty * min(unit, terms.icu_cap)
            inpatient_elig += elig
            icu_excess += tot - elig
        elif cat == IMAGING:
            outpatient_pay += tot * (1 - terms.imaging_copay / 100)
            img_copay_amt += tot * terms.imaging_copay / 100
        elif cat == REHAB:
            outpatient_pay += tot * (1 - terms.opd_copay / 100)
            opd_copay_amt += tot * terms.opd_copay / 100
        elif cat == NONPAYABLE:
            nonpay += tot
        elif cat == BUNDLED_SUPPLY:
            bundled += tot
        else:  # SURGERY, DIAG, PHARMACY, PROF, PROC, OTHER -> eligible inpatient charges
            inpatient_elig += tot

    ded = 0.0 if (emergency and terms.deductible_waived_emergency) else terms.deductible
    ded_applied = min(ded, inpatient_elig)
    after_ded = inpatient_elig - ded_applied
    coins_amt = after_ded * terms.inpatient_coins / 100
    inpatient_pay = after_ded - coins_amt
    reimb = inpatient_pay + outpatient_pay

    trig("ROOM_CAP", f"Room rent cap USD {terms.room_cap:,.0f}/day", "room_cap", "2.1(A)", room_excess)
    trig("ICU_CAP", f"ICU sub-limit USD {terms.icu_cap:,.0f}/day", "icu_cap", "2.2(A)", icu_excess)
    trig("DEDUCTIBLE", f"Annual deductible USD {ded_applied:,.0f}", "deductible", "1.1(B)", ded_applied)
    trig("COINSURANCE", f"Inpatient coinsurance {terms.inpatient_coins:.0f}%", "inpatient_coins", "1.1(C)", coins_amt)
    trig("IMAGING_COPAY", f"Imaging co-payment {terms.imaging_copay:.0f}%", "imaging_copay", "4.1(B)", img_copay_amt)
    trig("OPD_COPAY", f"Outpatient/rehab co-payment {terms.opd_copay:.0f}%", "opd_copay", "4.1(A)", opd_copay_amt)
    trig("NON_PAYABLE", "Non-Payable Items excluded", "premium", "7.2(A)", nonpay)
    if bundled:
        triggers.append({"kind": "UNBUNDLED", "label": "Supplies already inside Surgical Package", "clause_id": "2.3(A)", "page": 3, "amount": round(bundled, 2)})
    # fix the clause page for the non-payable / bundled triggers (they do not map to a numeric term)
    for t in triggers:
        if t["kind"] in ("NON_PAYABLE",):
            t["clause_id"], t["page"] = "7.2(A)", _page_hint(terms, "7.2(A)")
        if t["kind"] == "UNBUNDLED":
            t["page"] = _page_hint(terms, "2.3(A)")

    if reimb > terms.annual_limit:
        trig("ANNUAL_LIMIT", f"Annual limit USD {terms.annual_limit:,.0f} exhausted", "annual_limit", "1.1(A)", reimb - terms.annual_limit)
        reimb = terms.annual_limit

    ci_payout = 0.0
    if ci_event:
        if terms.ci_lump_sum > 0:
            ci_payout = terms.ci_lump_sum
        else:
            triggers.append({"kind": "NO_CI_COVER", "label": "No Critical Illness lump sum on this plan", **_src(terms, "ci_lump_sum", "5.1(A)"), "amount": 0.01})
    payout = round(reimb + ci_payout, 2)
    gap = round(max(0.0, bill_total - payout), 2)
    return SimResult(bill_total=bill_total, payout=payout, gap=gap, nonpayable=round(nonpay, 2),
                     triggers=triggers, ci_payout=ci_payout, reimbursement=round(reimb, 2))


def _page_hint(terms: PolicyTerms, clause_id: str) -> int:
    # Page numbers are stable across the specimen plans (one PART per page after the cover page).
    sec = int(clause_id.split(".")[0])
    return sec + 1


def simulate_scenario(terms: PolicyTerms, sc: Scenario, rider_ids: Iterable[str] = ()) -> SimResult:
    return simulate_lines(apply_riders(terms, rider_ids), sc.lines, emergency=sc.emergency, ci_event=sc.ci_event)


# ---------------------------------------------------------------------------
def radar_scores(terms: PolicyTerms, worst_gap: float, monthly_income: float) -> dict[str, float]:
    b = BENCHMARKS
    surgery_score = clip(terms.annual_limit / b["surgery_annual"] * 100) * (1 - terms.inpatient_coins / 100)
    scores = {
        "inpatient": 0.35 * clip(terms.room_cap / b["room"] * 100) + 0.35 * clip(terms.icu_cap / b["icu"] * 100)
        + 0.15 * (100 - terms.inpatient_coins) + 0.15 * clip(terms.annual_limit / b["annual"] * 100),
        "critical": 0.6 * clip(terms.ci_lump_sum / b["ci"] * 100) + 0.4 * surgery_score,
        "outpatient": 0.4 * (100 - terms.opd_copay) + 0.3 * (100 - terms.imaging_copay) + 0.3 * (100 - terms.pharmacy_copay),
        "emergency": 0.4 * clip(terms.ambulance_cap / b["ambulance"] * 100) + 0.6 * clip(terms.travel_limit / b["travel"] * 100),
        "waiting": 100 * (1 - (0.5 * min(1, terms.waiting_days / b["waiting_days"]) + 0.5 * min(1, terms.preexisting_months / b["preexisting_months"]))),
        "oop": 100 * max(0.0, 1 - (worst_gap / max(monthly_income, 1.0)) / b["oop_months"]),
    }
    return {k: round(clip(v), 1) for k, v in scores.items()}


def overall_score(axes: Mapping[str, float]) -> float:
    return round(sum(AXIS_WEIGHTS[k] * axes[k] for k in AXIS_WEIGHTS), 1)


def gap_stress_pct(results: Mapping[str, SimResult]) -> float:
    num = sum(SCENARIO_WEIGHTS[k] * r.gap for k, r in results.items() if k in SCENARIO_WEIGHTS)
    den = sum(SCENARIO_WEIGHTS[k] * r.bill_total for k, r in results.items() if k in SCENARIO_WEIGHTS)
    return round(100 * num / den, 1) if den else 0.0


def recommend_riders(terms: PolicyTerms, sc: Scenario, monthly_budget: float, max_riders: int = 3) -> dict:
    """Greedy cost-effectiveness selection (gap closed per $/month) under a monthly budget cap."""
    base = simulate_scenario(terms, sc)
    floor = base.nonpayable  # non-payable items can never be closed by a rider
    chosen: list[str] = []
    spent = 0.0
    cur_gap = base.gap
    blocked: list[dict] = []
    while len(chosen) < max_riders and cur_gap - floor > 0.5:
        best = None
        for rid, r in RIDERS.items():
            if rid in chosen:
                continue
            g = simulate_scenario(terms, sc, chosen + [rid]).gap
            gain = cur_gap - g
            if gain < 1.0:
                continue
            if spent + r["monthly_cost"] > monthly_budget + 1e-9:
                if not any(b["rider_id"] == rid for b in blocked):
                    blocked.append({"rider_id": rid, "monthly_cost": r["monthly_cost"], "gap_closed": round(gain, 2)})
                continue
            ratio = gain / r["monthly_cost"]
            if best is None or ratio > best[0]:
                best = (ratio, rid, g, gain)
        if best is None:
            break
        _, rid, g, gain = best
        chosen.append(rid)
        spent += RIDERS[rid]["monthly_cost"]
        cur_gap = g
    return {
        "scenario_id": sc.id, "gap_before": base.gap, "gap_after": round(cur_gap, 2), "rider_ids": chosen,
        "riders": [{"rider_id": r, "name": RIDERS[r]["name"], "monthly_cost": RIDERS[r]["monthly_cost"]} for r in chosen],
        "monthly_cost": round(spent, 2), "budget_cap": round(monthly_budget, 2),
        "budget_blocked": blocked, "unclosable_nonpayable": floor,
    }
