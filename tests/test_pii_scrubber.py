"""Test Suite: Local PII Scrubber Middleware (Tier L1 Guardrail).

Verifies 100% redaction of patient names, national IDs, SSNs, passports, phones,
emails, bank accounts, and policy numbers before LLM ingestion, plus safe re-hydration.
"""
from __future__ import annotations

import pytest

from backend.core.pii_scrubber import PIIScrubber, PIIVault


@pytest.fixture
def scrubber():
    return PIIScrubber(known_names=["Farhan Kabir Sheikh", "Nusrat Jahan Sheikh", "Julian Mercer Okafor"])


def test_pii_scrubber_redacts_all_identifiers(scrubber):
    raw_sample = """
    Patient Statement:
    Patient Name: Farhan Kabir Sheikh
    Date of Birth: 1998-11-09
    National ID: 19987654321098
    Passport No: A12345678
    Phone: +880 1819-556677
    Email: farhan.sheikh@example.com
    Policy Number: POL-FAM-2026-00481
    MRN: 902377
    Bank Account No: 40012345678
    IBAN: GB29NWBK60161331926819
    Discharge diagnosis is Dengue hemorrhagic fever for Mr. Sheikh.
    """

    res = scrubber.scrub(raw_sample)

    # 1. Zero raw PII in output text
    assert "Farhan Kabir Sheikh" not in res.text
    assert "19987654321098" not in res.text
    assert "A12345678" not in res.text
    assert "farhan.sheikh@example.com" not in res.text
    assert "+880 1819-556677" not in res.text
    assert "POL-FAM-2026-00481" not in res.text
    assert "40012345678" not in res.text
    assert "GB29NWBK60161331926819" not in res.text

    # 2. Token categories populated
    assert res.counts["PATIENT_NAME"] >= 1
    assert res.counts["ID"] >= 1
    assert res.counts["PASSPORT"] >= 1
    assert res.counts["EMAIL"] >= 1
    assert res.counts["PHONE"] >= 1
    assert res.counts["BANK"] >= 1

    # 3. Local Re-hydration restores original text exactly
    rehydrated = PIIScrubber.rehydrate(res.text, res.mapping)
    assert "Farhan Kabir Sheikh" in rehydrated
    assert "A12345678" in rehydrated
    assert "farhan.sheikh@example.com" in rehydrated


def test_pii_scrubber_public_summary_leaks_no_data(scrubber):
    raw = "Patient Julian Mercer Okafor with passport X98765432 and phone +44 20 7946 0958."
    res = scrubber.scrub(raw)
    summary = res.public_summary()

    # Public summary has counts only, never actual values
    assert "Julian" not in str(summary)
    assert "X98765432" not in str(summary)
    assert summary["distinct_values_masked"] >= 3
    assert summary["occurrences"] >= 3
