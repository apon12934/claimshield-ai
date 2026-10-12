"""Domain catalog: plan parameters, medical stress scenarios, riders & billing rules.

This module is the *single source of truth* shared by the synthetic PDF generator
(which renders it into PDFs), the policy-term extractor (which re-derives it from
the PDFs), and the simulation/auditor (which reason over it).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Plans (parameters are rendered into the policy PDFs as clause text)
# ---------------------------------------------------------------------------
PLANS: dict[str, dict] = {
    "POL-STUDENT": dict(
        policy_id="POL-STUDENT", code="STU", name="Basic Student Plan", file="policy_01_basic_student_plan.pdf",
        annual_limit=15_000, deductible=250, deductible_waived_emergency=False, inpatient_coins=20,
        room_cap=150, icu_cap=400, opd_copay=40, imaging_copay=50, pharmacy_copay=40,
        ci_lump_sum=0, ambulance_cap=150, travel_limit=0, waiting_days=30, preexisting_months=24,
        premium=38,
    ),
    "POL-FAMILY": dict(
        policy_id="POL-FAMILY", code="FAM", name="Family Health Plan", file="policy_02_family_health_plan.pdf",
        annual_limit=100_000, deductible=500, deductible_waived_emergency=True, inpatient_coins=0,
        room_cap=300, icu_cap=1_000, opd_copay=20, imaging_copay=20, pharmacy_copay=20,
        ci_lump_sum=0, ambulance_cap=500, travel_limit=5_000, waiting_days=30, preexisting_months=12,
        premium=210,
    ),
    "POL-CRITICAL": dict(
        policy_id="POL-CRITICAL", code="CRI", name="Comprehensive Critical Illness Plan",
        file="policy_03_comprehensive_critical_illness_plan.pdf",
        annual_limit=250_000, deductible=250, deductible_waived_emergency=True, inpatient_coins=0,
        room_cap=600, icu_cap=2_000, opd_copay=10, imaging_copay=10, pharmacy_copay=10,
        ci_lump_sum=100_000, ambulance_cap=1_000, travel_limit=50_000, waiting_days=90, preexisting_months=24,
        premium=389,
    ),
}

# ---------------------------------------------------------------------------
# Billing line items
# ---------------------------------------------------------------------------
# Category semantics used by the simulation & auditor
ROOM, ICU, SURGERY, DIAG, IMAGING, PHARMACY, PROF, PROC, REHAB, NONPAYABLE, BUNDLED_SUPPLY, OTHER = (
    "ROOM", "ICU", "SURGERY", "DIAG", "IMAGING", "PHARMACY", "PROF", "PROC", "REHAB",
    "NONPAYABLE", "BUNDLED_SUPPLY", "OTHER",
)

CODE_CATEGORY: dict[str, str] = {
    "ROOM-SEMI": ROOM, "ROOM-DAY": ROOM, "ICU-DAY": ICU,
    "44970": SURGERY, "29888": SURGERY, "33533": SURGERY, "00840": SURGERY, "01400": SURGERY,
    "74177": DIAG, "80053": DIAG, "85025": DIAG, "87449": DIAG, "93458": DIAG,
    "73721": IMAGING,
    "PHARM": PHARMACY,
    "99285": PROF, "99232": PROF, "99291": PROF,
    "36430": PROC, "97110": REHAB,
    "ADM-KIT": NONPAYABLE, "RM-UPG": NONPAYABLE, "TOILETRY": NONPAYABLE,
    "A4550": BUNDLED_SUPPLY, "99070": BUNDLED_SUPPLY, "OR-SUP": BUNDLED_SUPPLY,
}

# Surgical package codes -> supply codes that must NOT be billed separately (policy Section 2.3(A))
SURGICAL_PACKAGES: dict[str, list[str]] = {
    "44970": ["A4550", "99070", "OR-SUP"],
    "29888": ["A4550", "99070", "OR-SUP"],
    "33533": ["A4550", "99070", "OR-SUP"],
}

# Reference unit prices for a simple upcoding/outlier check (flag if > 150% of reference)
REFERENCE_PRICES: dict[str, float] = {
    "44970": 3200, "29888": 7200, "33533": 38000, "73721": 1200, "74177": 780, "99285": 650,
    "99291": 600, "00840": 900, "01400": 1100, "93458": 4200, "36430": 250, "87449": 120,
}

NONPAYABLE_KEYWORDS = ("admission kit", "upgrade", "surcharge", "toiletr", "personal comfort", "television", "attendant meal")
HIGH_COST_DIAGNOSTIC_THRESHOLD = 1000.0

DISCHARGE_ICD10_TEXT = {
    "A91": "Dengue hemorrhagic fever",
    "K35.80": "Unspecified acute appendicitis",
    "S83.511A": "Sprain of anterior cruciate ligament of right knee, initial encounter",
    "I25.10": "Atherosclerotic heart disease of native coronary artery",
}


def classify_line(code: str, description: str = "") -> str:
    """Map a billing code/description to a simulation category."""
    code = (code or "").strip().upper()
    if code in CODE_CATEGORY:
        return CODE_CATEGORY[code]
    desc = (description or "").lower()
    if any(k in desc for k in NONPAYABLE_KEYWORDS):
        return NONPAYABLE
    return OTHER


@dataclass(frozen=True)
class LineItem:
    code: str
    description: str
    qty: int
    unit_price: float
    day: int = 0  # offset in days from admission date

    @property
    def total(self) -> float:
        return round(self.qty * self.unit_price, 2)

    @property
    def category(self) -> str:
        return classify_line(self.code, self.description)


@dataclass
class Scenario:
    id: str
    name: str
    description: str
    admission_date: date
    stay_days: int
    emergency: bool
    accident: bool
    lines: list[LineItem]
    icd10: str
    icd10_desc: str
    ci_event: Optional[str] = None  # e.g. "CORONARY_ARTERY_BYPASS"
    bill_id: Optional[str] = None
    bill_policy_id: Optional[str] = None

    @property
    def total(self) -> float:
        return round(sum(l.total for l in self.lines), 2)


SCENARIOS: dict[str, Scenario] = {
    "SC-APPENDECTOMY": Scenario(
        id="SC-APPENDECTOMY", name="Emergency Appendectomy",
        description="Acute appendicitis, laparoscopic appendectomy, 3-night stay.",
        admission_date=date(2026, 8, 12), stay_days=3, emergency=True, accident=False,
        icd10="K35.80", icd10_desc=DISCHARGE_ICD10_TEXT["K35.80"], bill_id="BILL-APPENDECTOMY", bill_policy_id="POL-FAMILY",
        lines=[
            LineItem("99285", "Emergency department visit, level 5", 1, 650),
            LineItem("74177", "CT abdomen/pelvis with contrast", 1, 780),
            LineItem("44970", "Laparoscopic appendectomy (surgical package)", 1, 3200),
            LineItem("00840", "Anesthesia, lower abdomen", 1, 900),
            LineItem("ROOM-SEMI", "Semi-private room and board (per day)", 3, 400),
            LineItem("80053", "Comprehensive metabolic panel", 1, 220),
            LineItem("PHARM", "Pharmacy: IV antibiotics and analgesia", 1, 400),
            LineItem("99232", "Post-operative hospital visits", 1, 250, 1),
            LineItem("ADM-KIT", "Admission kit", 1, 50),
            LineItem("RM-UPG", "Room upgrade surcharge", 1, 150),
        ],
    ),
    "SC-DENGUE-ICU": Scenario(
        id="SC-DENGUE-ICU", name="Severe Dengue ICU Admission (6 Days)",
        description="Dengue hemorrhagic fever with critical thrombocytopenia, 6 ICU days.",
        admission_date=date(2026, 9, 2), stay_days=6, emergency=True, accident=False,
        icd10="A91", icd10_desc=DISCHARGE_ICD10_TEXT["A91"], bill_id="BILL-DENGUE-ICU", bill_policy_id="POL-FAMILY",
        lines=[
            LineItem("ICU-DAY", "Intensive care unit bed and nursing (per day)", 6, 450),
            LineItem("99291", "Critical care physician services (intensivist)", 1, 600),
            LineItem("85025", "CBC with platelet count (serial daily)", 6, 55),
            LineItem("87449", "Dengue NS1 antigen assay", 1, 120),
            LineItem("80053", "Comprehensive metabolic panel", 2, 100, 1),
            LineItem("PHARM", "Pharmacy: IV fluids and supportive medication", 1, 400),
            LineItem("36430", "Platelet transfusion", 1, 250, 3),
            LineItem("ADM-KIT", "Admission kit", 1, 50),
            LineItem("RM-UPG", "Private suite upgrade surcharge", 1, 200),
        ],
    ),
    "SC-ACL": Scenario(
        id="SC-ACL", name="ACL Sports Injury (MRI + Surgery)",
        description="Anterior cruciate ligament rupture: pre-op MRI, day-surgery reconstruction, rehab.",
        admission_date=date(2026, 7, 14), stay_days=1, emergency=False, accident=True,
        icd10="S83.511A", icd10_desc=DISCHARGE_ICD10_TEXT["S83.511A"], bill_id="BILL-ACL", bill_policy_id="POL-STUDENT",
        lines=[
            LineItem("73721", "MRI knee without contrast", 1, 1200, -11),
            LineItem("29888", "ACL reconstruction (surgical package)", 1, 7200),
            LineItem("01400", "Anesthesia, knee arthroscopy", 1, 1100),
            LineItem("ROOM-DAY", "Day-surgery bed (per day)", 1, 450),
            LineItem("97110", "Post-operative physiotherapy session", 6, 100, 7),
            LineItem("PHARM", "Pharmacy: analgesia and antibiotics", 1, 350),
            LineItem("ADM-KIT", "Admission kit", 1, 50),
            LineItem("TOILETRY", "Personal comfort and toiletry items", 1, 100),
        ],
    ),
    "SC-CARDIAC": Scenario(
        id="SC-CARDIAC", name="Critical Cardiac Event (CABG)",
        description="Acute coronary syndrome requiring bypass surgery and 4 ICU days.",
        admission_date=date(2026, 10, 1), stay_days=11, emergency=True, accident=False,
        icd10="I25.10", icd10_desc=DISCHARGE_ICD10_TEXT["I25.10"], ci_event="CORONARY_ARTERY_BYPASS",
        lines=[
            LineItem("ICU-DAY", "ICU bed and nursing (per day)", 4, 1200),
            LineItem("ROOM-SEMI", "Semi-private room and board (per day)", 7, 450),
            LineItem("33533", "CABG, arterial, single graft (surgical package)", 1, 38000, 1),
            LineItem("93458", "Cardiac catheterization with angiography", 1, 4200),
            LineItem("PHARM", "Pharmacy and cardiac medication", 1, 3800),
            LineItem("ADM-KIT", "Admission kit", 1, 50),
            LineItem("TOILETRY", "Personal comfort and toiletry items", 1, 350),
        ],
    ),
}

# Errors deliberately injected into the generated bills (used by generator & tests)
INJECTED_BILL_ERRORS: dict[str, dict] = {
    "BILL-APPENDECTOMY": dict(
        duplicate=LineItem("44970", "Laparoscopic appendectomy (surgical package)", 1, 3200),
        unbundled=LineItem("A4550", "Surgical tray and OR supplies", 1, 420),
        icd10_present=True, note_present=True,
    ),
    "BILL-DENGUE-ICU": dict(duplicate=None, unbundled=None, icd10_present=True, note_present=True),
    "BILL-ACL": dict(
        duplicate=LineItem("73721", "MRI knee without contrast", 1, 1200, -11),
        unbundled=LineItem("OR-SUP", "Operating room supplies", 1, 380),
        icd10_present=False, note_present=False,
    ),
}

# ---------------------------------------------------------------------------
# Synthetic patients (entirely fictional)
# ---------------------------------------------------------------------------
PATIENTS: dict[str, dict] = {
    "BILL-APPENDECTOMY": dict(
        name="Nusrat Jahan Sheikh", dob="1991-03-22", id_label="National ID", id_value="1991234567890",
        passport="EB0123456", phone="+880 1711-234567", email="nusrat.sheikh@example.com",
        policy_number="POL-FAM-2026-00481", mrn="884201", hospital="Evergreen Regional Medical Center",
        physician="Dr. Anika Sultana, MD (General Surgery)",
    ),
    "BILL-DENGUE-ICU": dict(
        name="Farhan Kabir Sheikh", dob="1998-11-09", id_label="National ID", id_value="19987654321098",
        passport="A12345678", phone="+880 1819-556677", email="farhan.sheikh@example.com",
        policy_number="POL-FAM-2026-00481", mrn="902377", hospital="Evergreen Regional Medical Center",
        physician="Dr. Imran Chowdhury, MD (Infectious Disease and Critical Care)",
    ),
    "BILL-ACL": dict(
        name="Ishita Roy Mallick", dob="2003-07-15", id_label="SSN", id_value="512-48-9370",
        passport="N7654321", phone="+1 (415) 555-0132", email="ishita.mallick@student.example.edu",
        policy_number="POL-STU-2026-03317", mrn="771560", hospital="Lakeshore Orthopedic Hospital",
        physician="Dr. Marcus Oyelaran, MD (Orthopedic Surgery)",
    ),
}

DEMO_USER = dict(
    user_id="USR-001", display_name="Farhan Kabir Sheikh", monthly_income=3200.0, disposable_income=1100.0,
    registered_account="40012345678",
)

# ---------------------------------------------------------------------------
# Riders (micro-riders) -- effects are applied to PolicyTerms in simulation
# ---------------------------------------------------------------------------
RIDERS: dict[str, dict] = {
    "R-ICU": dict(id="R-ICU", name="ICU Daily Limit Booster (to USD 1,500/day)", monthly_cost=6.50,
                  effects={"icu_cap": ("max", 1500)}),
    "R-ROOM": dict(id="R-ROOM", name="Room Rent Upgrade (to USD 500/day)", monthly_cost=9.00,
                   effects={"room_cap": ("max", 500)}),
    "R-DED": dict(id="R-DED", name="Deductible & Coinsurance Waiver", monthly_cost=18.00,
                  effects={"deductible": ("set", 0), "inpatient_coins": ("set", 0)}),
    "R-CI": dict(id="R-CI", name="Critical Illness Shield (USD 50,000 lump sum)", monthly_cost=24.00,
                 effects={"ci_lump_sum": ("add", 50_000)}),
    "R-LIMIT": dict(id="R-LIMIT", name="Annual Limit Extender (+USD 100,000)", monthly_cost=14.00,
                    effects={"annual_limit": ("add", 100_000)}),
    "R-TRAVEL": dict(id="R-TRAVEL", name="Overseas Emergency & Repatriation (USD 25,000)", monthly_cost=7.00,
                     effects={"travel_limit": ("max", 25_000)}),
    "R-OPD": dict(id="R-OPD", name="Outpatient & Imaging Co-pay Reducer (to 10%)", monthly_cost=11.00,
                  effects={"opd_copay": ("min", 10), "imaging_copay": ("min", 10), "pharmacy_copay": ("min", 10)}),
    "R-WAIT": dict(id="R-WAIT", name="Waiting Period Waiver (to 0 days)", monthly_cost=15.00,
                   effects={"waiting_days": ("set", 0), "preexisting_months": ("min", 12)}),
}

# Policy start dates for the demo portfolio, relative to "today" where noted
def demo_policy_start_dates(today: date) -> dict[str, date]:
    return {
        "POL-STUDENT": today - timedelta(days=20),  # inside the 30-day waiting period on purpose
        "POL-FAMILY": date(2025, 11, 1),
        "POL-CRITICAL": date(2026, 1, 15),
    }


# Statistics used across UI, docs, deck and appeal packet (as cited in the project spec)
STATISTICS = [
    dict(key="denials", headline="49M+ claim denials per year; algorithms rejected 300,000+ claims in two months at ~1.2 s each",
         sources=[("AAPC Knowledge Center", "https://www.aapc.com/blog/93960-taking-a-stand-against-ai-denials/"),
                  ("The Guardian", "https://www.theguardian.com/us-news/2025/jan/25/health-insurers-ai")]),
    dict(key="overturn", headline="82%-90% of appealed denials are overturned, yet fewer than 0.2% of patients appeal",
         sources=[("Stanford Report", "https://news.stanford.edu/stories/2026/01/ai-algorithms-health-insurance-care-risks-research"),
                  ("AAPC Knowledge Center", "https://www.aapc.com/blog/93960-taking-a-stand-against-ai-denials/"),
                  ("The Guardian", "https://www.theguardian.com/us-news/2025/jan/25/health-insurers-ai")]),
    dict(key="preventable", headline="Up to 85% of denials stem from preventable form/coding errors",
         sources=[("The Guardian", "https://www.theguardian.com/us-news/2025/jan/25/health-insurers-ai"),
                  ("Stanford Report", "https://news.stanford.edu/stories/2026/01/ai-algorithms-health-insurance-care-risks-research")]),
    dict(key="gap", headline="USD 258B Asian health protection gap (+21%); USD 132B mortality protection gap",
         sources=[("Swiss Re Institute", "https://www.swissre.com/reinsurance/insights/asia-protection-gap-consumer-survey.html")]),
]
