"""Workflow 3A - Pre-Claim Hospital Bill Error Auditor (Tier L1 analysis, Tier L2 claim staging).

Audits itemized hospital statements against policy terms and clinical discharge summaries
to prevent the 85% form/coding denial trap:
  1. Duplicate Line Items (same CPT code and date billed twice)
  2. Unbundled Charges (operating room supplies billed alongside a Surgical Package)
  3. Non-Payable Consumables (admission kit, room upgrade surcharges, toiletries)
  4. Missing Clinical Justification (high-cost diagnostic or ICU stay lacking ICD-10 or physician note)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

from ..core.pdf_utils import extract_pdf
from ..core.pii_scrubber import PIIScrubber, PIIVault
from ..core.prompt_firewall import PromptFirewall
from ..domain.catalog import (
    HIGH_COST_DIAGNOSTIC_THRESHOLD, INJECTED_BILL_ERRORS, NONPAYABLE, NONPAYABLE_KEYWORDS,
    PATIENTS, REFERENCE_PRICES, SURGICAL_PACKAGES, LineItem, classify_line,
)
from .staging import stage_action

LINE_ITEM_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+([A-Z0-9\-]+)\s+(.+?)\s+(\d+)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})$"
)


@dataclass
class ParsedBillLine:
    service_date: str
    code: str
    description: str
    qty: int
    unit_price: float
    total: float
    category: str


@dataclass
class AuditFinding:
    severity: str  # CRITICAL, WARNING, INFO
    category: str  # DUPLICATE, UNBUNDLED, NONPAYABLE, MISSING_JUSTIFICATION, UPCODING
    title: str
    description: str
    policy_clause: str
    policy_page: int
    impact_amount: float
    flagged_line_indices: list[int] = field(default_factory=list)


class BillAuditorAgent:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------- L1 tools
    def tool_parse_hospital_bill(self, bill_pdf_path: str, **_: Any) -> dict:
        """Parses an uploaded hospital bill PDF, redacting PII and extracting structured line items."""
        ctx = self.ctx
        p = Path(bill_pdf_path)
        if not p.is_absolute():
            p = ctx.settings.sample_dir / bill_pdf_path
        if not p.exists():
            raise FileNotFoundError(f"Bill PDF not found: {p}")

        pages = extract_pdf(p)
        full_text = "\n".join(pg.full_text for pg in pages)

        # Security check 1: Prompt firewall scan
        fw_result = ctx.firewall.scan_pages(pages)

        # Security check 2: Local PII scrubber
        vault = PIIVault()
        scrubbed = ctx.scrubber.scrub(full_text, vault=vault)

        # Extract line items and clinical notes
        lines, metadata = self._parse_bill_text(scrubbed.text)

        return {
            "file_name": p.name,
            "page_count": len(pages),
            "firewall_verdict": fw_result.verdict,
            "firewall_risk_score": fw_result.risk_score,
            "pii_summary": scrubbed.public_summary(),
            "scrubbed_text_preview": scrubbed.text[:600],
            "metadata": metadata,
            "line_items": [l.__dict__ for l in lines],
            "total_billed": round(sum(l.total for l in lines), 2),
            "pii_vault_id": id(vault),  # kept in-memory
        }

    def tool_audit_billing_codes(self, policy_id: str, line_items: list[dict], metadata: dict, **_: Any) -> dict:
        """Audits parsed line items against policy rules, returning clean claimable amount and findings."""
        doc = self.ctx.policies.get(policy_id)
        parsed = [ParsedBillLine(**l) for l in line_items]
        findings: list[AuditFinding] = []

        total_billed = round(sum(l.total for l in parsed), 2)
        total_deductions = 0.0

        # 1. Duplicate procedure detection (Section 2.3(C))
        seen_codes: dict[tuple[str, str], int] = {}
        for idx, item in enumerate(parsed):
            key = (item.code, item.service_date)
            if key in seen_codes:
                orig_idx = seen_codes[key]
                findings.append(AuditFinding(
                    severity="CRITICAL",
                    category="DUPLICATE",
                    title=f"Duplicate Procedure Code '{item.code}' Billed on {item.service_date}",
                    description=(
                        f"Code {item.code} ('{item.description}') billed twice on {item.service_date}. "
                        "Insurer algorithms automatically deny the entire claim when duplicate lines are present."
                    ),
                    policy_clause="2.3(C)",
                    policy_page=3,
                    impact_amount=item.total,
                    flagged_line_indices=[orig_idx, idx],
                ))
                total_deductions += item.total
            else:
                seen_codes[key] = idx

        # 2. Unbundled OR supply detection (Section 2.3(A))
        surgical_packages = {l.code for l in parsed if l.code in SURGICAL_PACKAGES}
        for pkg_code in surgical_packages:
            allowed_supplies = SURGICAL_PACKAGES[pkg_code]
            for idx, item in enumerate(parsed):
                if item.code in allowed_supplies:
                    findings.append(AuditFinding(
                        severity="CRITICAL",
                        category="UNBUNDLED",
                        title=f"Unbundled Supply '{item.code}' Billed Separately from Surgical Package {pkg_code}",
                        description=(
                            f"Line '{item.description}' (${item.total:,.2f}) is standard operating room supply "
                            f"already included under Surgical Package {pkg_code}. Must be removed before claim filing."
                        ),
                        policy_clause="2.3(A)",
                        policy_page=3,
                        impact_amount=item.total,
                        flagged_line_indices=[idx],
                    ))
                    total_deductions += item.total

        # 3. Non-payable administrative & comfort consumables separation (Section 7.2(A))
        for idx, item in enumerate(parsed):
            is_nonpay = item.category == NONPAYABLE or any(k in item.description.lower() for k in NONPAYABLE_KEYWORDS)
            if is_nonpay:
                findings.append(AuditFinding(
                    severity="WARNING",
                    category="NONPAYABLE",
                    title=f"Non-Payable Item Separated: '{item.description}'",
                    description=(
                        f"Item '{item.description}' (${item.total:,.2f}) is categorized as non-medical administrative/comfort. "
                        "Under Clause 7.2(B), separating this item prevents whole-claim rejection."
                    ),
                    policy_clause="7.2(A)",
                    policy_page=8,
                    impact_amount=item.total,
                    flagged_line_indices=[idx],
                ))
                total_deductions += item.total

        # 4. Clinical justification check (Section 2.2(B), 8.1(A))
        has_icd10 = bool(metadata.get("icd10") and metadata["icd10"] != "NOT PROVIDED")
        has_note = metadata.get("medical_necessity_note_attached", False)

        has_icu = any(l.code == "ICU-DAY" for l in parsed)
        has_high_cost = any(l.total >= HIGH_COST_DIAGNOSTIC_THRESHOLD for l in parsed)

        if (has_icu or has_high_cost) and (not has_icd10 or not has_note):
            missing_items = []
            if not has_icd10:
                missing_items.append("ICD-10 Primary Diagnosis Code")
            if not has_note:
                missing_items.append("Physician Medical Necessity Note")
            findings.append(AuditFinding(
                severity="CRITICAL",
                category="MISSING_JUSTIFICATION",
                title="Missing Required Clinical Documentation",
                description=(
                    f"Claim involves high-cost inpatient/ICU services but lacks: {', '.join(missing_items)}. "
                    "Claim would trigger immediate automated denial ERR-MN-404 without clinical justification."
                ),
                policy_clause="2.2(B)" if has_icu else "8.1(A)",
                policy_page=3 if has_icu else 9,
                impact_amount=0.0,
                flagged_line_indices=[],
            ))

        clean_claimable = max(0.0, round(total_billed - total_deductions, 2))

        return {
            "policy_id": policy_id,
            "total_billed": total_billed,
            "total_deductions": round(total_deductions, 2),
            "clean_claimable_amount": clean_claimable,
            "findings_count": len(findings),
            "findings": [f.__dict__ for f in findings],
            "audit_verdict": "READY_AFTER_CORRECTION" if findings else "CLEAN",
        }

    # ------------------------------------------------------------- text parser
    def _parse_bill_text(self, text: str) -> tuple[list[ParsedBillLine], dict]:
        lines: list[ParsedBillLine] = []
        metadata: dict = {
            "statement_no": None,
            "patient_name": None,
            "policy_number": None,
            "admission_date": None,
            "discharge_date": None,
            "icd10": None,
            "medical_necessity_note_attached": False,
        }

        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue

            # metadata headers
            if "Statement No:" in line:
                metadata["statement_no"] = line.split(":", 1)[1].strip()
            elif "Patient Name:" in line:
                metadata["patient_name"] = line.split(":", 1)[1].strip()
            elif "Policy Number:" in line:
                metadata["policy_number"] = line.split(":", 1)[1].strip()
            elif "Admission Date:" in line:
                metadata["admission_date"] = line.split(":", 1)[1].strip().split()[0]
            elif "Discharge Date:" in line:
                metadata["discharge_date"] = line.split(":", 1)[1].strip().split()[0]
            elif "Primary Diagnosis (ICD-10):" in line:
                metadata["icd10"] = line.split(":", 1)[1].strip()
            elif "Physician Medical-Necessity Note: ATTACHED" in line:
                metadata["medical_necessity_note_attached"] = True

            # table lines
            m = LINE_ITEM_RE.match(line)
            if m:
                sdate, code, desc, qty_str, unit_str, tot_str = m.groups()
                code = code.strip()
                desc = desc.strip()
                qty = int(qty_str)
                unit_price = float(unit_str.replace(",", ""))
                total = float(tot_str.replace(",", ""))
                cat = classify_line(code, desc)
                lines.append(ParsedBillLine(
                    service_date=sdate,
                    code=code,
                    description=desc,
                    qty=qty,
                    unit_price=unit_price,
                    total=total,
                    category=cat,
                ))

        return lines, metadata

    # ------------------------------------------------------------- L2 tool
    def tool_stage_claim_draft(self, session_id: str, policy_id: str, bill_id: str, clean_claimable_amount: float,
                               billed_amount: float, audit_summary: dict, **_: Any) -> dict:
        """Stages corrected claim form in Tier 2 (PENDING_HUMAN_APPROVAL)."""
        payload = {
            "policy_id": policy_id,
            "bill_id": bill_id,
            "billed_amount": billed_amount,
            "clean_claimable_amount": clean_claimable_amount,
            "deductions_removed": round(billed_amount - clean_claimable_amount, 2),
            "audit_summary": audit_summary,
        }
        return stage_action(
            self.ctx,
            session_id=session_id,
            action_type="claim_submission",
            payload=payload,
            summary=f"Submit audited claim for ${clean_claimable_amount:,.2f} under {policy_id}",
        )
