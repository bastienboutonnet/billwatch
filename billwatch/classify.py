"""Decide whether an email is a bill, using content rather than the sender.

Scoring (senders vary, so we lean on signals that generalise):
  +3  a PDF attachment is present            (bills almost always are PDFs here)
  +1  per bill keyword found in subject/body/PDF text, capped at +4
  +2  a labelled due date was parsed from the PDF (strong signal)

We use two thresholds:
  score >= CANDIDATE_MIN_SCORE  -> it's a bill candidate (don't ignore it)
  score >= CONFIDENT_MIN_SCORE  -> confident: auto-schedule silently
  candidate but not confident    -> 'review': we still remind, flagged for a glance
This biases toward false positives (a quick dismiss) over false negatives
(a missed bill), which is the whole point of the system.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config


@dataclass
class Decision:
    is_candidate: bool
    is_confident: bool
    score: int
    matched: list[str]


def classify(subject: str, body: str, pdf_text: str, has_pdf: bool,
             due_found: bool) -> Decision:
    score = 0
    matched: list[str] = []

    if has_pdf:
        score += 3
        matched.append("pdf-attachment")

    haystack = " ".join([subject or "", body or "", pdf_text or ""]).lower()
    kw_hits = 0
    for kw in config.BILL_KEYWORDS:
        if kw in haystack:
            kw_hits += 1
            matched.append(kw)
            if kw_hits >= 4:
                break
    score += min(kw_hits, 4)

    if due_found:
        score += 2
        matched.append("labelled-due-date")

    return Decision(
        is_candidate=score >= config.CANDIDATE_MIN_SCORE,
        is_confident=score >= config.CONFIDENT_MIN_SCORE,
        score=score,
        matched=matched,
    )
