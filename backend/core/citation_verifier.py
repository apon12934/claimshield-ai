"""Deterministic citation verifier -- the anti-hallucination gate (Risk 3 mitigation).

Every quote an agent returns must appear *verbatim* (after whitespace / quote / dash
normalisation) in the extracted text of the cited PDF page.  Verification is purely
string-based and never calls an LLM, so a model cannot talk its way past it.

    verified  <=>  norm(exact_quote) is a substring of norm(page_text[page])

An optional fuzzy mode (token-window similarity) exists for *diagnostics* only; the
Q&A gate runs in strict mode.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Mapping

_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"), "-")
_QUOTES = {ord("\u2018"): "'", ord("\u2019"): "'", ord("\u201c"): '"', ord("\u201d"): '"'}

UNVERIFIED_WARNING = "UNVERIFIED_CLAUSE_WARNING"
VALID_VERDICTS = {"COVERED", "PARTIALLY_COVERED", "EXCLUDED", "WAITING_PERIOD_ACTIVE"}


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").translate(_DASHES).translate(_QUOTES)
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)  # de-hyphenate line-wrap splits
    t = re.sub(r"\s+", " ", t)
    return t.strip().lower()


@dataclass
class CitationCheck:
    page: int
    clause_id: str
    exact_quote: str
    verified: bool
    match_type: str  # EXACT | FUZZY | NONE | BAD_PAGE | EMPTY
    similarity: float = 0.0


@dataclass
class VerificationReport:
    checks: list[CitationCheck] = field(default_factory=list)
    schema_errors: list[str] = field(default_factory=list)

    @property
    def all_verified(self) -> bool:
        return bool(self.checks) and all(c.verified for c in self.checks) and not self.schema_errors

    @property
    def failed(self) -> list[CitationCheck]:
        return [c for c in self.checks if not c.verified]

    def summary(self) -> dict:
        return {"all_verified": self.all_verified, "checked": len(self.checks), "failed": len(self.failed),
                "schema_errors": self.schema_errors}


class CitationVerifier:
    def __init__(self, strict: bool = True, fuzzy_threshold: float = 0.97) -> None:
        self.strict = strict
        self.fuzzy_threshold = fuzzy_threshold

    # ----------------------------------------------------------------- single
    def verify_quote(self, quote: str, page_text: str | None) -> tuple[bool, str, float]:
        if not quote or not quote.strip():
            return False, "EMPTY", 0.0
        if page_text is None:
            return False, "BAD_PAGE", 0.0
        nq, npg = normalize(quote), normalize(page_text)
        if nq and nq in npg:
            return True, "EXACT", 1.0
        sim = self._best_window_similarity(nq, npg)
        if not self.strict and sim >= self.fuzzy_threshold:
            return True, "FUZZY", sim
        return False, "NONE", sim

    @staticmethod
    def _best_window_similarity(nq: str, npg: str) -> float:
        qt, pt = nq.split(), npg.split()
        if not qt or not pt:
            return 0.0
        n = len(qt)
        best = 0.0
        for i in range(0, max(1, len(pt) - n + 1)):
            window = " ".join(pt[i:i + n])
            r = SequenceMatcher(None, nq, window).ratio()
            if r > best:
                best = r
                if best >= 0.999:
                    break
        return round(best, 4)

    # ---------------------------------------------------------------- response
    def verify_response(self, response: Mapping[str, Any], pages: Mapping[int, str]) -> VerificationReport:
        rep = VerificationReport()
        if not isinstance(response.get("answer"), str) or not response["answer"].strip():
            rep.schema_errors.append("missing 'answer'")
        if response.get("coverage_verdict") not in VALID_VERDICTS:
            rep.schema_errors.append(f"invalid coverage_verdict: {response.get('coverage_verdict')!r}")
        cits = response.get("citations")
        if not isinstance(cits, list) or not cits:
            rep.schema_errors.append("no citations supplied")
            return rep
        for c in cits:
            if not isinstance(c, Mapping):
                rep.schema_errors.append("citation is not an object")
                continue
            try:
                page = int(c.get("page"))
            except (TypeError, ValueError):
                rep.checks.append(CitationCheck(-1, str(c.get("clause_id", "")), str(c.get("exact_quote", "")), False, "BAD_PAGE"))
                continue
            ok, mtype, sim = self.verify_quote(str(c.get("exact_quote", "")), pages.get(page))
            rep.checks.append(CitationCheck(page, str(c.get("clause_id", "")), str(c.get("exact_quote", "")), ok, mtype, sim))
        return rep

    # ------------------------------------------------------------- highlighter
    @staticmethod
    def locate_quote(page_text: str, quote: str) -> tuple[int, int] | None:
        """Locate ``quote`` inside the *original* page text (whitespace-insensitive).

        Returns (start, end) character offsets for the side-by-side PDF highlighter.
        """
        words = [re.escape(w) for w in (quote or "").split()]
        if not words:
            return None
        m = re.search(r"\s+".join(words), page_text or "", flags=re.IGNORECASE)
        return (m.start(), m.end()) if m else None
