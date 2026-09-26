"""
Pattern identification, part 2: map parsed alert signals to a runbook.

Order (from PLAN.md):
  1. exact  - alert_name is listed in a runbook's triggers.alert_names -> 1.0
  2. pattern - weighted regex/keyword score over subject + body; best >= 0.6 wins
  3. fallback - the agent (LLM) picks from the catalog; Jev scores the fit
                (see select_runbook in mcp_server.py)
  4. none   - generic-triage runbook, flagged for a human
"""

import re

from catalog import GENERIC_RUNBOOK_ID, runbook_summary, runbooks

PATTERN_THRESHOLD = 0.6


def _pattern_score(book: dict, parsed: dict) -> tuple[float, list[str]]:
    triggers = book.get("triggers", {})
    subject = parsed.get("subject") or ""
    body = parsed.get("body_excerpt") or ""
    hits = []

    subject_hits = [p for p in triggers.get("subject_patterns", []) if re.search(p, subject, re.IGNORECASE)]
    body_hits = [k for k in triggers.get("body_keywords", []) if re.search(re.escape(k), body, re.IGNORECASE)]
    hits += [f"subject~/{p}/" for p in subject_hits] + [f"body:{k}" for k in body_hits]

    # Subject patterns are strong evidence, body keywords weaker; cap each part.
    score = min(len(subject_hits), 2) * 0.35 + min(len(body_hits), 3) * 0.1
    return round(min(score, 1.0), 2), hits


def match(parsed: dict) -> dict:
    alert = (parsed.get("alert_name") or "").lower()
    books = runbooks()

    for book in books.values():
        if alert and alert in [a.lower() for a in book.get("triggers", {}).get("alert_names", [])]:
            return {
                "runbook_id": book["id"],
                "method": "exact",
                "confidence": 1.0,
                "evidence": [f"alert_name={parsed['alert_name']}"],
                "needs_llm_selection": False,
            }

    scored = []
    for book in books.values():
        if book["id"] == GENERIC_RUNBOOK_ID:
            continue
        score, hits = _pattern_score(book, parsed)
        scored.append((score, book["id"], hits))
    scored.sort(reverse=True)

    best_score, best_id, best_hits = scored[0] if scored else (0.0, None, [])
    candidates = [{"runbook_id": rid, "score": s, "evidence": h} for s, rid, h in scored[:3] if s > 0]

    if best_id and best_score >= PATTERN_THRESHOLD:
        return {
            "runbook_id": best_id,
            "method": "pattern",
            "confidence": best_score,
            "evidence": best_hits,
            "candidates": candidates,
            "needs_llm_selection": False,
        }

    return {
        "runbook_id": None,
        "method": "none",
        "confidence": best_score,
        "evidence": best_hits,
        "candidates": candidates,
        "needs_llm_selection": True,
        "catalog": [runbook_summary(b) for b in books.values() if b["id"] != GENERIC_RUNBOOK_ID],
    }
