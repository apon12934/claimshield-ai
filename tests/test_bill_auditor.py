"""Test Suite: Pre-Claim Hospital Bill Auditor (Workflow 3A).

Verifies accurate detection of:
  - Duplicate CPT codes billed on the same service date (Section 2.3(C))
  - Unbundled surgical supplies billed alongside surgical packages (Section 2.3(A))
  - Non-payable administrative & comfort consumables separation (Section 7.2(B))
  - Missing clinical justification & physician medical-necessity notes (Section 2.2(B))
"""
from __future__ import annotations

import pytest

from backend.agents.orchestrator import ApplicationContext
from backend.config import Settings


@pytest.fixture
def ctx(tmp_path):
    settings = Settings(
        db_path=tmp_path / "test.db",
        audit_log_path=tmp_path / "test_audit.jsonl",
        demo_offline_mode=True,
    )
    return ApplicationContext(settings)


def test_appendectomy_bill_auditor_flags_duplicate_and_unbundled(ctx):
    sess = ctx.engine.open_session(requested_tier=1)
    bill_path = "hospital_bills/bill_01_appendectomy_family_plan.pdf"

    parsed = ctx.gateway.call(sess["token"], "parse_hospital_bill", bill_pdf_path=bill_path)
    audit = ctx.gateway.call(
        sess["token"],
        "audit_billing_codes",
        policy_id="POL-FAMILY",
        line_items=parsed["line_items"],
        metadata=parsed["metadata"],
    )

    categories = [f["category"] for f in audit["findings"]]

    # Duplicate 44970 ($3,200) detected
    assert "DUPLICATE" in categories
    dup = next(f for f in audit["findings"] if f["category"] == "DUPLICATE")
    assert dup["impact_amount"] == 3200.0
    assert dup["policy_clause"] == "2.3(C)"

    # Unbundled A4550 ($420) detected
    assert "UNBUNDLED" in categories
    unb = next(f for f in audit["findings"] if f["category"] == "UNBUNDLED")
    assert unb["impact_amount"] == 420.0
    assert unb["policy_clause"] == "2.3(A)"

    # Non-payable consumables ($200: admission kit $50 + room upgrade $150) separated
    assert "NONPAYABLE" in categories
    nonpays = [f for f in audit["findings"] if f["category"] == "NONPAYABLE"]
    assert sum(f["impact_amount"] for f in nonpays) == 200.0

    # Total billed $11,420 -> Deductions $3,820 -> Clean claimable $7,600
    assert audit["total_billed"] == 11420.0
    assert audit["total_deductions"] == 3820.0
    assert audit["clean_claimable_amount"] == 7600.0


def test_dengue_icu_bill_auditor_separates_nonpayables(ctx):
    sess = ctx.engine.open_session(requested_tier=1)
    bill_path = "hospital_bills/bill_02_dengue_icu_family_plan.pdf"

    parsed = ctx.gateway.call(sess["token"], "parse_hospital_bill", bill_pdf_path=bill_path)
    audit = ctx.gateway.call(
        sess["token"],
        "audit_billing_codes",
        policy_id="POL-FAMILY",
        line_items=parsed["line_items"],
        metadata=parsed["metadata"],
    )

    # Coding is clean (no duplicates or unbundling)
    categories = [f["category"] for f in audit["findings"]]
    assert "DUPLICATE" not in categories
    assert "UNBUNDLED" not in categories

    # $250 nonpayables separated (Admission kit $50 + Suite upgrade $200)
    assert "NONPAYABLE" in categories
    nonpays = [f for f in audit["findings"] if f["category"] == "NONPAYABLE"]
    assert sum(f["impact_amount"] for f in nonpays) == 250.0

    # $4,850 billed -> $4,600 clean claimable
    assert audit["total_billed"] == 4850.0
    assert audit["clean_claimable_amount"] == 4600.0


def test_acl_bill_auditor_flags_missing_clinical_justification(ctx):
    sess = ctx.engine.open_session(requested_tier=1)
    bill_path = "hospital_bills/bill_03_acl_sports_injury_student_plan.pdf"

    parsed = ctx.gateway.call(sess["token"], "parse_hospital_bill", bill_pdf_path=bill_path)
    audit = ctx.gateway.call(
        sess["token"],
        "audit_billing_codes",
        policy_id="POL-STUDENT",
        line_items=parsed["line_items"],
        metadata=parsed["metadata"],
    )

    categories = [f["category"] for f in audit["findings"]]
    assert "MISSING_JUSTIFICATION" in categories
    assert "DUPLICATE" in categories  # Duplicate MRI 73721 ($1,200)
