"""Workflow 1 - Citation-grounded Policy Q&A (L0).

Pipeline:  scrub question -> retrieve clauses (tool, L0) -> draft (live LLM or deterministic engine)
           -> **citation gate** (every exact_quote must exist verbatim on the cited page)
           -> regenerate on failure -> fall back to deterministic extractive answer
           -> if still unverifiable: block with UNVERIFIED_CLAUSE_WARNING.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

from ..core.citation_verifier import UNVERIFIED_WARNING, VALID_VERDICTS
from ..core.policy_store import ClauseChunk, PolicyDocument, tokenize, SYNONYMS

LEADS = {
    "COVERED": "Yes - this appears to be covered.",
    "PARTIALLY_COVERED": "Partly - it is covered, but subject to limits, sub-limits or cost-sharing.",
    "EXCLUDED": "No - this is excluded or not covered under this plan.",
    "WAITING_PERIOD_ACTIVE": "Not yet - a waiting period is still running on this plan.",
}
LIMIT_WORDS = ("up to", "sub-limit", "maximum of", "co-payment", "deductible", "capped", "coinsurance", "per day", "per event", "per policy year")
MAX_REGENERATIONS = 2


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.;])\s+(?=[A-Z(])", text.strip())
    return [p.strip() for p in parts if p.strip()]


class PolicyQAAgent:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------- L0 tool
    def tool_read_policy_clauses(self, policy_id: str, query: str, top_k: int = 4, **_: Any) -> dict:
        doc = self.ctx.policies.get(policy_id)
        hits = doc.search(query, top_k=top_k)
        return {"policy_id": policy_id, "policy_name": doc.name, "query_hash_only": True,
                "clauses": [{**c.to_dict(), "score": s} for c, s in hits]}

    # ----------------------------------------------------------- main entry
    def answer(self, session_token: str, policy_id: str, question: str, today: Optional[date] = None) -> dict:
        ctx = self.ctx
        today = today or ctx.settings.today
        doc = ctx.policies.get(policy_id)
        scrub = ctx.scrubber.scrub(question)  # question goes through the local scrubber first
        retrieval = ctx.gateway.call(session_token, "read_policy_clauses", policy_id=policy_id, query=scrub.text, top_k=4)
        clauses = retrieval["clauses"]
        attempts: list[dict] = []
        final: Optional[dict] = None
        mode = "deterministic"

        if not clauses:
            return {"policy_id": policy_id, "question": question, "answer": "I could not find a clause in this policy that addresses the question.",
                    "coverage_verdict": None, "citations": [], "warning": "NO_RELEVANT_CLAUSE", "mode": mode,
                    "verification": {"all_verified": False, "checked": 0, "failed": 0, "schema_errors": ["no clauses"]},
                    "retrieval": [], "attempts": attempts, "pii_masked": scrub.public_summary()}

        # ---- live LLM attempts (regenerate if the gate rejects) -----------------------------------
        feedback = ""
        if ctx.llm.is_live:
            for n in range(1, MAX_REGENERATIONS + 2):
                draft = self._draft_live(scrub.text, clauses, feedback)
                if draft is None:
                    attempts.append({"attempt": n, "source": "llm", "result": "NO_RESPONSE"})
                    break
                rep = ctx.verifier.verify_response(draft, doc.page_text)
                attempts.append({"attempt": n, "source": "llm", "result": "VERIFIED" if rep.all_verified else "REJECTED", **rep.summary()})
                if rep.all_verified:
                    final, mode = draft, "llm"
                    break
                feedback = "Your previous citations failed verification: " + "; ".join(
                    f"p.{c.page} {c.clause_id}: quote not found verbatim" for c in rep.failed) + ". Copy quotes exactly."
        # ---- deterministic engine (also the fallback) ----------------------------------------------
        if final is None:
            draft = self._draft_deterministic(doc, question, clauses, today)
            rep = ctx.verifier.verify_response(draft, doc.page_text)
            attempts.append({"attempt": len(attempts) + 1, "source": "deterministic", "result": "VERIFIED" if rep.all_verified else "REJECTED", **rep.summary()})
            mode = "deterministic_fallback" if ctx.llm.is_live else "deterministic"
            if rep.all_verified:
                final = draft
        warning = None
        if final is None:
            warning = UNVERIFIED_WARNING
            final = {"answer": "UNVERIFIED_CLAUSE_WARNING: the generated answer could not be verified against the policy text and was blocked.",
                     "coverage_verdict": None, "citations": []}
            rep = ctx.verifier.verify_response(final, doc.page_text)

        # annotate citations with verification metadata for the UI highlighter
        by_key = {(c.page, c.exact_quote): c for c in rep.checks}
        citations = []
        for c in final.get("citations", []):
            chk = by_key.get((int(c["page"]), c["exact_quote"]))
            ch = doc.clause(c["clause_id"])
            citations.append({**c, "section_header": ch.section_header if ch else "", "citation_label": ch.citation_label if ch else f"Page {c['page']}",
                              "verified": bool(chk and chk.verified), "match_type": chk.match_type if chk else "NONE"})
        ctx.audit.log(tool="citation_verifier", outcome="VERIFIED" if warning is None else "BLOCKED_UNVERIFIED", tier=0,
                      actor="system", details={"policy_id": policy_id, "mode": mode, "attempts": len(attempts),
                                               "citations_checked": len(citations), "pii_masked": scrub.public_summary()})
        return {"policy_id": policy_id, "question": question, "answer": final["answer"], "coverage_verdict": final["coverage_verdict"],
                "citations": citations, "verification": rep.summary(), "mode": mode, "warning": warning,
                "retrieval": [{"clause_id": c["clause_id"], "score": c["score"]} for c in clauses], "attempts": attempts,
                "pii_masked": scrub.public_summary()}

    # ------------------------------------------------------------- live draft
    def _draft_live(self, question: str, clauses: list[dict], feedback: str) -> Optional[dict]:
        system = ("You are an insurance policy assistant. Answer ONLY from the supplied policy clauses. Reply with JSON: "
                  '{"answer": str, "coverage_verdict": one of COVERED|PARTIALLY_COVERED|EXCLUDED|WAITING_PERIOD_ACTIVE, '
                  '"citations": [{"page": int, "clause_id": str, "exact_quote": str}]}. '
                  "Every exact_quote MUST be copied verbatim from a clause. Never invent clauses.")
        body = "\n".join(f"[page {c['page']} | clause {c['clause_id']}] {c['text']}" for c in clauses)
        user = f"Question: {question}\n\nClauses:\n{body}\n{feedback}"
        out = self.ctx.llm.generate_json(system, user)
        if not out or out.get("coverage_verdict") not in VALID_VERDICTS:
            return out if out else None
        return out

    # ---------------------------------------------------- deterministic draft
    def _draft_deterministic(self, doc: PolicyDocument, question: str, clauses: list[dict], today: date) -> dict:
        primary = clauses[0]
        chosen = [primary]
        for c in clauses[1:]:
            if c["section_id"] != primary["section_id"] and c["score"] >= 0.45 * primary["score"]:
                chosen.append(c)
                break
        q_tokens = set(tokenize(question))
        for w in re.findall(r"[a-z0-9\-]+", question.lower()):
            for s in SYNONYMS.get(w, []):
                q_tokens |= set(tokenize(s))
        citations = []
        for c in chosen:
            sents = split_sentences(c["text"])
            best = max(sents, key=lambda s: len(q_tokens & set(tokenize(s))))
            citations.append({"page": c["page"], "clause_id": c["clause_id"], "exact_quote": best})
        verdict, note = self.classify(doc, ClauseChunk(doc.policy_id, primary["page"], primary["section_id"], primary["section_header"],
                                                       primary["clause_id"].split("(")[1][0], primary["text"]), question, today)
        lead = LEADS[verdict]
        basis = " ".join(f'{c["clause_id"].replace("(", ", Clause ").rstrip(")")} (p.{c["page"]}): "{c["exact_quote"]}"' for c in citations)
        answer = f"{lead} {note + ' ' if note else ''}Basis: Section {basis}"
        return {"answer": answer, "coverage_verdict": verdict, "citations": citations}

    def classify(self, doc: PolicyDocument, chunk: ClauseChunk, question: str, today: date) -> tuple[str, str]:
        txt = chunk.text.lower()
        sec = chunk.section_id
        if sec == "7.1":
            return "EXCLUDED", ""
        if sec == "7.2":
            return ("PARTIALLY_COVERED", "The claim stays valid; only the Non-Payable Item itself is excluded.") if chunk.clause_letter == "B" else ("EXCLUDED", "")
        if sec.startswith("6."):
            row = self.ctx.db.query("SELECT start_date FROM policies WHERE policy_id=?", (doc.policy_id,), tier=0)
            if row:
                start = date.fromisoformat(row[0]["start_date"])
                elapsed = (today - start).days
                days = doc.terms.waiting_days
                if sec == "6.1":
                    if elapsed < days:
                        return "WAITING_PERIOD_ACTIVE", f"Your policy started {start.isoformat()}; {days - elapsed} of {days} waiting days remain (accidents are exempt)."
                    return "COVERED", f"Your {days}-day waiting period ended on {date.fromordinal(start.toordinal() + days).isoformat()}."
                months = (today.year - start.year) * 12 + today.month - start.month
                if months < doc.terms.preexisting_months:
                    return "WAITING_PERIOD_ACTIVE", f"{doc.terms.preexisting_months - months} more months before pre-existing conditions are covered."
                return "COVERED", "The pre-existing condition waiting period has elapsed."
            return "PARTIALLY_COVERED", ""
        if "not covered" in txt:
            return "EXCLUDED", ""
        if any(w in txt for w in LIMIT_WORDS):
            return "PARTIALLY_COVERED", ""
        return "COVERED", ""
