"""Indirect prompt-injection firewall for uploaded PDFs (Risk 1 mitigation).

Scans extracted PDF text -- both the visible layer and any hidden layer
(white-on-white, micro-font, off-page) -- for:
  * instruction-override patterns       ("ignore previous instructions", "system override")
  * structural role-spoofing markers    ("SYSTEM:", "<|im_start|>", "[INST]", "you are now ...")
  * payout-account redirection          ("wire claim payout to IBAN ...", "account #9999")
  * authorization-bypass / tool-call coaxing ("bypass human approval", "execute_claim_submission")
  * data-exfiltration requests          ("reveal the system prompt / HMAC secret")
and fails *closed*: a BLOCK verdict quarantines the document from the agent pipeline.

This is defence layer #2.  Even if every pattern here is evaded, layer #3
(``permission_engine``) still cannot be talked into executing an L3 action.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Sequence

from .pdf_utils import PageContent

ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)

# category -> (weight, [patterns])
_RAW: dict[str, tuple[int, list[str]]] = {
    "INSTRUCTION_OVERRIDE": (5, [
        r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier|preceding)\s+(?:instructions?|rules?|prompts?|polic(?:y|ies)|limits?)",
        r"disregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier|policy|safety)\s*\w*",
        r"system\s+override",
        r"override\s+(?:the\s+)?(?:policy|policies|limits?|safety|guardrails?|permissions?)",
        r"forget\s+(?:everything|all|your)\s+(?:above|instructions?|rules?)",
        r"new\s+instructions?\s*:",
        r"ignore\s+policy\s+limits?",
    ]),
    "ROLE_SPOOFING": (3, [
        r"(?m)^\s*(?:system|assistant|developer)\s*:",
        r"<\|?\s*(?:im_start|system|assistant)\s*\|?>",
        r"\[/?\s*(?:inst|sys)\s*\]",
        r"you\s+are\s+now\s+(?:an?\s+)?(?:unrestricted|admin|administrator|insurer|root|dan)\b",
        r"act\s+as\s+(?:an?\s+)?(?:unrestricted|admin|administrator|insurer)\b",
    ]),
    "PAYOUT_REDIRECTION": (5, [
        r"\b(?:wire|transfer|redirect|re-?route|divert|send)\b.{0,60}\b(?:payout|payment|claim|funds?|benefits?)\b.{0,80}(?:\baccount\b|\biban\b|\[PII_BANK_\d+\]|[A-Z]{2}\d{2}[A-Z0-9]{10,})",
        r"\b(?:payout|payment|funds?)\b.{0,40}\bto\s+(?:iban|account)\b",
        r"\bchange\s+(?:the\s+)?(?:beneficiary|payout\s+(?:account|destination)|bank\s+account)\b",
        r"\baccount\s*#\s*\d{4}\b",
        r"\bapprove\s+(?:a\s+|the\s+)?(?:usd\s*)?\$?\s*[\d,]{4,}\s+payout\b",
    ]),
    "AUTH_BYPASS": (5, [
        r"\bbypass\s+(?:the\s+)?(?:user\s+|human\s+)?(?:confirmation|approval|verification|otp|authorization)",
        r"\bwithout\s+(?:asking\s+(?:for\s+)?)?(?:user\s+|human\s+)?(?:approval|confirmation|consent)\b",
        r"\bdo\s+not\s+(?:ask|request|wait\s+for)\s+(?:the\s+)?(?:user|human)\b",
        r"\bexecute_(?:claim_submission|appeal_filing|rider_purchase)\b",
        r"/sandbox/(?:execute|appeal-claim)",
        r"\bauto[-\s]?approve\b",
        r"\bforce\s+(?:immediate\s+)?submission\b",
    ]),
    "EXFILTRATION": (4, [
        r"\b(?:reveal|print|output|show|leak)\s+(?:the\s+|your\s+)?(?:system\s+prompt|hidden\s+prompt|api\s+key|hmac|secret|credentials)",
    ]),
}

PATTERNS: dict[str, tuple[int, list[re.Pattern[str]]]] = {
    k: (w, [re.compile(p, re.IGNORECASE | re.DOTALL) for p in pats]) for k, (w, pats) in _RAW.items()
}

BLOCK_THRESHOLD = 8
SUSPICIOUS_THRESHOLD = 3


def normalize_for_scan(text: str) -> str:
    """NFKC fold (defeats full-width / homoglyph tricks) + strip zero-width chars + collapse whitespace."""
    t = unicodedata.normalize("NFKC", text or "").translate(ZERO_WIDTH)
    return re.sub(r"[ \t\r\f\v]+", " ", t)


@dataclass
class Finding:
    category: str
    page: int
    layer: str  # "visible" | "hidden"
    matched_text: str
    weight: int


@dataclass
class FirewallResult:
    verdict: str  # SAFE | SUSPICIOUS | BLOCK
    risk_score: int
    findings: list[Finding] = field(default_factory=list)
    hidden_text_pages: list[int] = field(default_factory=list)
    sanitized_pages: dict[int, str] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.verdict == "BLOCK"

    @property
    def categories(self) -> list[str]:
        return sorted({f.category for f in self.findings})

    def first_finding_for_page(self, page: int) -> Finding | None:
        return next((f for f in self.findings if f.page == page), None)

    def summary(self) -> dict:
        return {
            "verdict": self.verdict, "risk_score": self.risk_score, "categories": self.categories,
            "hidden_text_pages": self.hidden_text_pages,
            "finding_count": len(self.findings),
            "pages_with_findings": sorted({f.page for f in self.findings}),
        }


class PromptFirewall:
    def __init__(self, block_threshold: int = BLOCK_THRESHOLD, suspicious_threshold: int = SUSPICIOUS_THRESHOLD) -> None:
        self.block_threshold = block_threshold
        self.suspicious_threshold = suspicious_threshold

    def scan_text(self, text: str, page: int = 1, layer: str = "visible") -> list[Finding]:
        norm = normalize_for_scan(text)
        out: list[Finding] = []
        for cat, (weight, regs) in PATTERNS.items():
            for rx in regs:
                m = rx.search(norm)
                if m:
                    snippet = norm[max(0, m.start()): m.end() + 60].strip()
                    out.append(Finding(cat, page, layer, snippet[:200], weight))
                    break  # one hit per category per layer/page
        return out

    def scan_pages(self, pages: Sequence[PageContent]) -> FirewallResult:
        findings: list[Finding] = []
        hidden_pages: list[int] = []
        sanitized: dict[int, str] = {}
        score = 0
        for pg in pages:
            sanitized[pg.page] = pg.text
            vis = self.scan_text(pg.text, pg.page, "visible")
            findings += vis
            score += sum(f.weight for f in vis)
            if pg.hidden_text.strip():
                hidden_pages.append(pg.page)
                hid = self.scan_text(pg.hidden_text, pg.page, "hidden")
                findings += hid
                score += sum(f.weight for f in hid)
                # any hidden layer is itself anomalous; weight by amount of hidden text
                structural = 3 if len(pg.hidden_text) > 40 else 1
                findings.append(Finding("HIDDEN_TEXT_LAYER", pg.page, "hidden",
                                        f"{len(pg.hidden_text)} hidden characters (white/micro/off-page text)", structural))
                score += structural
                if hid:  # hidden AND adversarial: double the suspicion
                    score += 2
        if score >= self.block_threshold:
            verdict = "BLOCK"
        elif score >= self.suspicious_threshold:
            verdict = "SUSPICIOUS"
        else:
            verdict = "SAFE"
        # sanitized view always removes the hidden layer and masks any visible injection span
        for f in findings:
            if f.layer == "visible" and f.category != "HIDDEN_TEXT_LAYER":
                sanitized[f.page] = "[FIREWALL_REMOVED_SUSPICIOUS_CONTENT]\n" + sanitized.get(f.page, "")
        return FirewallResult(verdict=verdict, risk_score=score, findings=findings,
                              hidden_text_pages=hidden_pages, sanitized_pages=sanitized)
