"""Local PII scrubber & re-hydration middleware (Tier L1 guardrail).

Runs *locally, before any LLM call*. Deterministic regex + label/gazetteer "NER":
    emails, phone numbers, national IDs / SSNs, passports, bank accounts / IBANs,
    policy & member numbers, MRNs, dates of birth, home addresses and patient names.

Every distinct value gets a reversible placeholder such as ``[PII_PATIENT_NAME_1]``.
The mapping stays in the local process (never logged, never sent to a provider) and is
used to re-hydrate text only in the local UI.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

TOKEN_RE = re.compile(r"\[PII_[A-Z_]+_\d+\]")

# A capitalised name token: "Nusrat", "O'Brien", "Delacroix-Webb" or an initial "J."
_NT = r"(?:[A-Z][A-Za-z'’\-]*[a-z]|[A-Z]\.)"
_NAME = rf"{_NT}(?:[ \t]+{_NT}){{0,4}}"


class PIIVault:
    """Per-session reversible token vault (value <-> placeholder)."""

    def __init__(self) -> None:
        self.value_to_token: dict[tuple[str, str], str] = {}
        self.token_to_value: dict[str, str] = {}
        self.counters: dict[str, int] = {}

    def token_for(self, category: str, value: str) -> str:
        key = (category, value.strip().lower() if category == "PATIENT_NAME" else value.strip())
        tok = self.value_to_token.get(key)
        if tok is None:
            self.counters[category] = self.counters.get(category, 0) + 1
            tok = f"[PII_{category}_{self.counters[category]}]"
            self.value_to_token[key] = tok
            self.token_to_value[tok] = value.strip()
        return tok

    def categories(self) -> dict[str, int]:
        return dict(self.counters)


@dataclass
class ScrubResult:
    text: str
    mapping: dict[str, str]  # token -> original (LOCAL ONLY)
    counts: dict[str, int]  # category -> distinct values masked
    occurrences: int  # total replaced spans

    @property
    def total_distinct(self) -> int:
        return sum(self.counts.values())

    def public_summary(self) -> dict:
        """Safe-to-log summary: counts only, never values."""
        return {"distinct_values_masked": self.total_distinct, "occurrences": self.occurrences, "by_category": dict(self.counts)}


# (category, compiled regex, group holding the sensitive value or 0 for whole match)
def _lab(labels: str) -> str:
    return rf"(?i:{labels})"


_RULES: list[tuple[str, re.Pattern[str], int]] = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), 0),
    ("BANK", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b"), 0),  # IBAN
    ("ID", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), 0),  # US SSN
    ("BANK", re.compile(rf"{_lab(r'(?:Bank )?Account(?: No\.?| Number| #)|Acct\.?(?: No\.?)?|Payout Account|A/C(?: No\.?)?')}\s*[:#]?\s*(\d(?:[ -]?\d){{6,22}})"), 1),
    ("PASSPORT", re.compile(rf"{_lab(r'Passport(?: No\.?| Number| #)?')}\s*[:#]?\s*([A-Z0-9]{{6,12}})\b"), 1),
    ("PASSPORT", re.compile(r"\b[A-Z]{1,2}\d{7,8}\b"), 0),
    ("ID", re.compile(rf"{_lab(r'National ID|NID|SSN|Social Security(?: No\.?| Number)?|Patient ID|Identity (?:No\.?|Number)|ID Number|Tax ID|NRIC')}\s*[:#]?\s*(\d(?:[ -]?\d){{6,20}})"), 1),
    ("MRN", re.compile(rf"{_lab(r'MRN|Medical Record(?: No\.?| Number)?')}\s*[:#]?\s*([A-Z0-9][A-Z0-9\-]{{4,13}})"), 1),
    ("POLICY_ACCOUNT", re.compile(r"\bPOL-[A-Z]{2,4}-\d{4}-\d{3,6}\b"), 0),
    ("POLICY_ACCOUNT", re.compile(rf"{_lab(r'Policy(?: No\.?| Number| #)|Member ID')}\s*[:#]?\s*([A-Z0-9][A-Z0-9\-]{{5,24}})"), 1),
    ("DOB", re.compile(rf"{_lab(r'DOB|Date of Birth|Birth Date')}\s*[:#]?\s*(\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}[/\-]\d{{1,2}}[/\-]\d{{2,4}})"), 1),
    ("PHONE", re.compile(rf"{_lab(r'Phone|Mobile|Tel(?:ephone)?|Cell|Contact(?: No\.?)?|Emergency Contact|Fax')}\s*[:#]?\s*(\+?\(?\d[\d ()\-]{{5,18}}\d)"), 1),
    ("PHONE", re.compile(r"(?<![\w/])\+\d{1,3}[ \-]?\(?\d{1,4}\)?(?:[ \-]?\d{2,4}){2,3}(?!\d)"), 0),
    ("PHONE", re.compile(r"(?<!\d)\(?\d{3}\)?[\-. ]\d{3}[\-.]\d{4}(?!\d)"), 0),
    ("ADDRESS", re.compile(rf"{_lab(r'Patient Address|Home Address|Residential Address|Address of Patient')}\s*:\s*([^\n]+)"), 1),
]

_NAME_LABEL_RE = re.compile(
    rf"{_lab(r'Patient(?: Name)?|Name of Patient|Insured(?: Name)?|Policy ?holder(?: Name)?|Claimant|Guardian|Next of Kin|Account Holder')}[ \t]*[:\-][ \t]*({_NAME})"
)
_TITLED_RE = re.compile(rf"\b(?:Mr|Mrs|Ms|Mx|Miss)\.?[ \t]+({_NAME})")


class PIIScrubber:
    def __init__(self, known_names: Iterable[str] = ()) -> None:
        self.known_names = [n for n in known_names if n and n.strip()]

    # ---------------------------------------------------------------- scrub
    def scrub(self, text: str, vault: PIIVault | None = None, extra_names: Iterable[str] = ()) -> ScrubResult:
        vault = vault or PIIVault()
        if not text:
            return ScrubResult("", {}, {}, 0)
        occurrences = 0
        out = text

        def sub_rule(category: str, rx: re.Pattern[str], grp: int, s: str) -> str:
            nonlocal occurrences

            def repl(m: re.Match[str]) -> str:
                nonlocal occurrences
                if grp == 0:
                    value = m.group(0)
                    tok = vault.token_for(category, value)
                    occurrences += 1
                    return tok
                value = m.group(grp)
                if TOKEN_RE.fullmatch(value.strip()) or "[PII_" in value:
                    return m.group(0)
                tok = vault.token_for(category, value)
                occurrences += 1
                start, end = m.span(grp)
                return m.group(0)[: start - m.start()] + tok + m.group(0)[end - m.start():]

            return rx.sub(repl, s)

        # 1) structured identifiers first (emails, IDs, phones, accounts, etc.)
        for category, rx, grp in _RULES:
            out = sub_rule(category, rx, grp, out)

        # 2) names (labels + titles + gazetteer), replacing every occurrence
        names: list[str] = list(self.known_names) + list(extra_names)
        for rx in (_NAME_LABEL_RE, _TITLED_RE):
            for m in rx.finditer(out):
                cand = m.group(1).strip()
                if cand and "[PII_" not in cand:
                    names.append(cand)
        seen: set[str] = set()
        for name in sorted({n.strip() for n in names if len(n.strip()) >= 4}, key=len, reverse=True):
            low = name.lower()
            if low in seen:
                continue
            seen.add(low)
            variants = {name}
            parts = name.split()
            if len(parts) >= 2:
                variants.add(f"{parts[-1]}, {' '.join(parts[:-1])}")
                variants.add(f"{parts[-1]},{' '.join(parts[:-1])}")
            tok = vault.token_for("PATIENT_NAME", name)
            for v in sorted(variants, key=len, reverse=True):
                pat = re.compile(rf"(?<![\w\[]){re.escape(v)}(?![\w\]])", re.IGNORECASE)
                out, n = pat.subn(tok, out)
                occurrences += n
            # Note: surname with title matching to be refined

        counts = vault.categories()
        return ScrubResult(text=out, mapping=dict(vault.token_to_value), counts=counts, occurrences=occurrences)

    # ------------------------------------------------------------- rehydrate
    @staticmethod
    def rehydrate(text: str, mapping: dict[str, str]) -> str:
        return TOKEN_RE.sub(lambda m: mapping.get(m.group(0), m.group(0)), text or "")

    # -------------------------------------------------------------- objects
    def scrub_obj(self, obj: Any, vault: PIIVault | None = None) -> Any:
        """Recursively scrub every string inside a JSON-like object."""
        vault = vault or PIIVault()
        if isinstance(obj, str):
            return self.scrub(obj, vault).text
        if isinstance(obj, dict):
            return {k: self.scrub_obj(v, vault) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.scrub_obj(v, vault) for v in obj]
        return obj

    @staticmethod
    def contains_pii_token(text: str) -> bool:
        return bool(TOKEN_RE.search(text or ""))
