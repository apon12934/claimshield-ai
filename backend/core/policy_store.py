"""Policy ingestion: page-aware clause chunking, term extraction and lexical retrieval.

Chunks keep exact ``page_number``, ``section_header`` and ``clause_id`` so every answer can cite
e.g. ``Section 3.1, Clause A, Page 4``.  Retrieval is a deterministic BM25-style lexical ranker with a
small insurance-domain synonym map, so it behaves identically online and offline.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from .pdf_utils import PageContent, extract_pdf, sha256_file

SECTION_RE = re.compile(r"^Section\s+(\d+\.\d+)\s*-\s*(.+?)\s*$")
CLAUSE_RE = re.compile(r"^Clause\s+(\d+\.\d+)\(([A-Z])\):\s*(.*)$")
FOOTER_RE = re.compile(r"(Synthetic specimen document|^Page \d+$)")
PART_RE = re.compile(r"^PART\s+\d+\s*-")

STOPWORDS = set("""a an the of and or to in on for is are was be been being by with as at it this that these those from
under any all if my me i do does can could would should will what which who whom whose when where how there their its not no
than then so such per each other into out up about over after before between during without within""".split())

SYNONYMS = {
    "icu": ["intensive", "care", "unit"], "dengue": ["infectious", "platelet", "acute", "hematocrit"],
    "mri": ["imaging", "diagnostic", "scan"], "ct": ["imaging", "diagnostic", "scan"], "scan": ["imaging", "diagnostic"],
    "room": ["accommodation", "board", "rent"], "rent": ["room", "board"],
    "cancer": ["critical", "illness"], "heart": ["critical", "illness", "myocardial", "bypass"], "cardiac": ["critical", "illness", "myocardial", "bypass"],
    "stroke": ["critical", "illness"], "transplant": ["critical", "illness"],
    "travel": ["cross-border", "overseas", "repatriation", "outside"], "abroad": ["cross-border", "overseas", "outside"],
    "overseas": ["cross-border", "outside", "repatriation"],
    "ambulance": ["emergency", "transport"], "cosmetic": ["exclusions", "aesthetic"], "dental": ["exclusions"],
    "waiting": ["waiting", "period"], "wait": ["waiting", "period"], "preexisting": ["pre-existing"],
    "deductible": ["deductible", "annual"], "copay": ["co-payment"], "co-pay": ["co-payment"],
    "surgery": ["surgical", "package"], "supplies": ["surgical", "package", "operating-room"],
    "appeal": ["appeals", "denial", "dispute"], "denied": ["denial", "appeal"], "denial": ["appeal", "necessary", "review"],
    "pharmacy": ["prescription", "medication"], "medicine": ["prescription", "medication"], "drugs": ["prescription", "medication"],
    "physio": ["physiotherapy", "rehabilitation"], "physiotherapy": ["rehabilitation"],
    "limit": ["annual", "limit", "liability"], "premium": ["premium", "monthly"], "payout": ["paid", "registered", "account"],
    "kit": ["non-payable", "items"], "upgrade": ["non-payable", "surcharge"], "toiletries": ["non-payable", "comfort"],
    "necessity": ["necessary"], "necessary": ["necessity"],
}


def tokenize(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9][a-z0-9\-']*", (text or "").lower())
    out = []
    for t in toks:
        if t in STOPWORDS:
            continue
        out.append(_stem(t))
    return out


def _stem(t: str) -> str:
    for suf in ("ations", "ation", "ings", "ing", "ies", "ed", "es", "s"):
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[: -len(suf)] + ("y" if suf == "ies" else "")
    return t


@dataclass
class ClauseChunk:
    policy_id: str
    page: int
    section_id: str
    section_header: str
    clause_letter: str
    text: str

    @property
    def clause_id(self) -> str:
        return f"{self.section_id}({self.clause_letter})"

    @property
    def citation_label(self) -> str:
        return f"Section {self.section_id}, Clause {self.clause_letter}, Page {self.page}"

    def to_dict(self) -> dict:
        return {"policy_id": self.policy_id, "page": self.page, "section_id": self.section_id,
                "section_header": self.section_header, "clause_id": self.clause_id,
                "citation_label": self.citation_label, "text": self.text}


@dataclass
class PolicyTerms:
    """Numeric terms extracted from the policy clauses (with the clause each came from)."""
    annual_limit: float = 0
    deductible: float = 0
    deductible_waived_emergency: bool = False
    inpatient_coins: float = 0
    room_cap: float = 0
    icu_cap: float = 0
    opd_copay: float = 100
    imaging_copay: float = 100
    pharmacy_copay: float = 100
    ci_lump_sum: float = 0
    ambulance_cap: float = 0
    travel_limit: float = 0
    waiting_days: int = 0
    preexisting_months: int = 0
    premium: float = 0
    sources: dict[str, dict] = field(default_factory=dict)  # field -> {clause_id, page}

    def copy(self) -> "PolicyTerms":
        import copy
        return copy.deepcopy(self)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "sources"}
        return d


# field -> (clause_id, regex, caster)
_TERM_RULES: list[tuple[str, str, str, str]] = [
    ("annual_limit", "1.1(A)", r"Annual Limit of Liability per Policy Year is USD ([\d,]+)", "money"),
    ("deductible", "1.1(B)", r"Annual Deductible is USD ([\d,]+)", "money"),
    ("inpatient_coins", "1.1(C)", r"Member pays (\d+)% of Eligible Charges", "int"),
    ("premium", "1.1(D)", r"Monthly Premium for this Plan is USD ([\d,]+)", "money"),
    ("room_cap", "2.1(A)", r"maximum of USD ([\d,]+) per day", "money"),
    ("icu_cap", "2.2(A)", r"daily sub-limit of USD ([\d,]+) per day", "money"),
    ("opd_copay", "4.1(A)", r"Member co-payment of (\d+)%", "int"),
    ("imaging_copay", "4.1(B)", r"Member co-payment of (\d+)%", "int"),
    ("pharmacy_copay", "4.2(A)", r"Member co-payment of (\d+)%", "int"),
    ("ci_lump_sum", "5.1(A)", r"Critical Illness Benefit of USD ([\d,]+)", "money"),
    ("ambulance_cap", "5.2(A)", r"covered up to USD ([\d,]+) per event", "money"),
    ("travel_limit", "5.3(A)", r"covered up to USD ([\d,]+) per Policy Year", "money"),
    ("waiting_days", "6.1(A)", r"waiting period of (\d+) days", "int"),
    ("preexisting_months", "6.2(A)", r"until (\d+) months", "int"),
]


class PolicyDocument:
    def __init__(self, policy_id: str, name: str, path: Path, pages: list[PageContent], extra: Optional[dict] = None) -> None:
        self.policy_id = policy_id
        self.name = name
        self.path = Path(path)
        self.pages = pages
        self.page_text: dict[int, str] = {p.page: p.text for p in pages}
        self.extra = extra or {}
        self.sha256 = sha256_file(self.path)
        self.chunks: list[ClauseChunk] = self._chunk()
        self._df: dict[str, int] = {}
        self._build_index()
        self.terms = self._extract_terms()

    # ------------------------------------------------------------------ chunking
    def _chunk(self) -> list[ClauseChunk]:
        chunks: list[ClauseChunk] = []
        sec_id, sec_header = "0", "Preamble"
        cur: Optional[dict] = None

        def flush() -> None:
            nonlocal cur
            if cur and cur["text"].strip():
                chunks.append(ClauseChunk(self.policy_id, cur["page"], cur["sec"], cur["hdr"], cur["letter"],
                                          re.sub(r"\s+", " ", cur["text"]).strip()))
            cur = None

        for pg in self.pages:
            if cur is not None and cur["page"] != pg.page:
                # a clause that spans pages becomes one chunk per page (so quotes stay page-verifiable)
                carry = (cur["sec"], cur["hdr"], cur["letter"])
                flush()
                cur = {"page": pg.page, "sec": carry[0], "hdr": carry[1], "letter": carry[2], "text": ""}
            for raw in pg.text.splitlines():
                line = raw.strip()
                if not line or FOOTER_RE.search(line) or PART_RE.match(line):
                    continue
                ms = SECTION_RE.match(line)
                if ms:
                    flush()
                    sec_id, sec_header = ms.group(1), ms.group(2)
                    continue
                mc = CLAUSE_RE.match(line)
                if mc and mc.group(1) == sec_id:
                    flush()
                    cur = {"page": pg.page, "sec": sec_id, "hdr": sec_header, "letter": mc.group(2), "text": mc.group(3)}
                    continue
                if cur is not None:
                    cur["text"] += " " + line
        flush()
        return chunks

    def _extract_terms(self) -> PolicyTerms:
        t = PolicyTerms()
        by_id = {c.clause_id: c for c in self.chunks}
        for fld, cid, rx, cast in _TERM_RULES:
            c = by_id.get(cid)
            if c is None:
                continue
            m = re.search(rx, c.text)
            if m:
                val = float(m.group(1).replace(",", ""))
                setattr(t, fld, int(val) if cast == "int" else val)
                t.sources[fld] = {"clause_id": cid, "page": c.page}
            elif fld in ("ci_lump_sum", "travel_limit") and "not covered" in c.text.lower():
                setattr(t, fld, 0)
                t.sources[fld] = {"clause_id": cid, "page": c.page}
        c = by_id.get("1.1(B)")
        if c is not None:
            t.deductible_waived_emergency = "waived for emergency admissions" in c.text.lower()
        return t

    # --------------------------------------------------------------- retrieval
    def _build_index(self) -> None:
        self._doc_tokens = [tokenize(c.section_header + " " + c.text) for c in self.chunks]
        self._header_tokens = [set(tokenize(c.section_header)) for c in self.chunks]
        for toks in self._doc_tokens:
            for t in set(toks):
                self._df[t] = self._df.get(t, 0) + 1
        self._avg_len = (sum(len(t) for t in self._doc_tokens) / max(1, len(self._doc_tokens))) or 1.0

    def search(self, query: str, top_k: int = 4) -> list[tuple[ClauseChunk, float]]:
        q_raw = tokenize(query)
        expanded: list[tuple[str, float]] = [(t, 1.0) for t in q_raw]
        for w in re.findall(r"[a-z0-9\-]+", query.lower()):
            for syn in SYNONYMS.get(w, []):
                expanded.extend((s, 0.5) for s in tokenize(syn))
        n = len(self.chunks)
        scores: list[tuple[int, float]] = []
        k1, b = 1.4, 0.75
        for i, toks in enumerate(self._doc_tokens):
            if not toks:
                continue
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            s = 0.0
            for t, w in expanded:
                f = tf.get(t, 0)
                if not f:
                    continue
                idf = math.log(1 + (n - self._df.get(t, 0) + 0.5) / (self._df.get(t, 0) + 0.5))
                s += w * idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * len(toks) / self._avg_len))
                if t in self._header_tokens[i]:
                    s += w * idf * 0.8
            if s > 0:
                scores.append((i, s))
        scores.sort(key=lambda x: x[1], reverse=True)
        return [(self.chunks[i], round(s, 4)) for i, s in scores[:top_k]]

    def clause(self, clause_id: str) -> Optional[ClauseChunk]:
        return next((c for c in self.chunks if c.clause_id == clause_id), None)


class PolicyStore:
    """Loads every policy listed in sample_data/manifest.json (lazily, cached)."""

    def __init__(self, sample_dir: Path) -> None:
        self.sample_dir = Path(sample_dir)
        self._docs: dict[str, PolicyDocument] = {}
        self._manifest: Optional[dict] = None

    @property
    def manifest(self) -> dict:
        if self._manifest is None:
            mp = self.sample_dir / "manifest.json"
            if not mp.exists():
                raise FileNotFoundError(f"{mp} not found - run data_generator/generate_synthetic_pdfs.py first")
            self._manifest = json.loads(mp.read_text(encoding="utf-8"))
        return self._manifest

    def policy_ids(self) -> list[str]:
        return [p["policy_id"] for p in self.manifest["policies"]]

    def get(self, policy_id: str) -> PolicyDocument:
        if policy_id not in self._docs:
            meta = next((p for p in self.manifest["policies"] if p["policy_id"] == policy_id), None)
            if meta is None:
                raise KeyError(f"Unknown policy {policy_id}")
            path = self.sample_dir / meta["path"]
            self._docs[policy_id] = PolicyDocument(policy_id, meta["name"], path, extract_pdf(path), meta)
        return self._docs[policy_id]

    def all(self) -> list[PolicyDocument]:
        return [self.get(pid) for pid in self.policy_ids()]
