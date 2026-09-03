"""
The router. Decides what kind of question this is, and what to retrieve for it.

WHY POOLS AND NOT ONE RANKING. The corpus holds four kinds of document that
answer four different questions, and they compete unfairly in a single pool.
`architecture.html` is 131 KB of payments vocabulary in 103 long chunks; it
matches almost any operational phrasing and outranks the short precise SOP that
actually answers the question. Retrieving everything together does not rank the
best answer first, it ranks the biggest document first.

So retrieval runs once per pool and each pool is returned separately:

    procedure   sop, circular      "what do I do about this"
    precedent   narrative          "how was this handled before"
    reference   platform_doc       "how does this system work"

An operator working a break wants the first two side by side, not one list where
they are interleaved. That is also what the answer layer needs: the procedure is
what it follows, the precedent is what it cites as evidence, and conflating them
makes a derived answer impossible to attribute.

NUMERIC QUESTIONS NEVER REACH RETRIEVAL. Counts, totals and rankings go to SQL
or they do not get answered. A model asked "how many like this" over retrieved
text answers from the handful of chunks it can see, is confidently wrong, and
nothing in the output signals it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.retrieve.hybrid import Hybrid
from app.retrieve.store import Hit

PROCEDURE_TYPES = ["sop", "circular"]
PRECEDENT_TYPES = ["narrative"]
REFERENCE_TYPES = ["platform_doc"]

# Deterministic, because it is cheap and auditable. A model here would be a
# second thing that can be wrong, in front of the thing that decides whether a
# number gets computed or invented.
NUMERIC = re.compile(
    r"\b(how many|how much|count|number of|total|sum|average|mean|median|"
    r"top \d+|most|least|highest|lowest|rank|trend|per day|per slot|rate of)\b",
    re.I)

LOOKUP = re.compile(
    r"\b(balance|limit|status of|current balance|available funds)\b", re.I)


@dataclass
class Routed:
    question: str
    intent: str                      # numeric | lookup | guidance
    procedure: list[Hit] = field(default_factory=list)
    precedent: list[Hit] = field(default_factory=list)
    reference: list[Hit] = field(default_factory=list)
    tier: str = "none"               # exact | derived | none
    note: str = ""

    def all_hits(self) -> list[Hit]:
        return self.procedure + self.precedent + self.reference


def classify(question: str) -> str:
    if NUMERIC.search(question):
        return "numeric"
    if LOOKUP.search(question):
        return "lookup"
    return "guidance"


class Router:
    def __init__(self, store: Hybrid, *, mode: str = "hybrid",
                 floor: float = 0.36):
        self.store = store
        self.mode = mode
        self.floor = floor

    def route(self, question: str, *, k: int = 5, as_of: str | None = None,
              precedent_k: int = 3, reference_k: int = 2,
              anomaly_code: str | None = None) -> Routed:
        """
        `anomaly_code` is the detected defect class, when there is one.

        A break does not arrive as a bare question. The reconciliation engine
        found it with a deterministic predicate, so it already knows the class,
        and the procedure for a known class is a **lookup, not a search**.
        Retrieval is for the case where the class is unknown or novel.

        This corrects an earlier design error. The tier was going to be decided
        by a retrieval score threshold, and measuring showed no such threshold
        exists: on this corpus, coverage where rank 1 is the correct procedure
        spans 0.25 to 1.00, where it is the wrong procedure spans 0.25 to 0.60,
        and for held-out defects with no correct answer at all it spans 0.25 to
        0.67. Those ranges overlap almost completely, so no cut separates them.
        Coverage separates a real question from junk, which is what the refusal
        floor uses it for; it does not separate a right answer from a wrong one.
        """
        intent = classify(question)
        r = Routed(question=question, intent=intent)

        if intent == "numeric":
            r.note = ("Numeric question. This must be answered by a SQL query "
                      "whose statement is shown, never from retrieved text.")
            return r
        if intent == "lookup":
            r.note = ("Exact-value question. Fetch from the system of record; "
                      "the model may word the answer but not produce the value.")
            return r

        # Known class: fetch its procedure directly. No ranking involved, so
        # nothing can outrank the right answer.
        looked_up = None
        if anomaly_code:
            looked_up = self._procedure_for(anomaly_code, as_of)

        r.procedure = looked_up or self.store.search(
            question, k=k, as_of=as_of, mode=self.mode,
            doc_types=PROCEDURE_TYPES)
        # Precedent for a known class is a lookup too, and for the same reason
        # as the procedure but a sharper one: narratives are written in customer
        # language ("the account was debited twice") and procedures in
        # operational language ("One authorisation, two postings"). They share no
        # vocabulary for the same defect, so a search phrased either way
        # retrieves the other badly. The class was already established
        # deterministically; carrying it across is free and exact.
        r.precedent = (self._precedent_for(anomaly_code, precedent_k)
                       if anomaly_code else
                       self.store.search(question, k=precedent_k, as_of=as_of,
                                         mode=self.mode,
                                         doc_types=PRECEDENT_TYPES))
        r.reference = self.store.search(question, k=reference_k, as_of=as_of,
                                        mode=self.mode,
                                        doc_types=REFERENCE_TYPES)

        r.tier, r.note = self._tier(question, r, bool(looked_up))
        return r

    def _procedure_for(self, code: str, as_of: str | None):
        """The SOP for a known defect class, by name rather than by score."""
        uri = "sop/SOP-%s.md" % code
        rows = self.store.conn.execute(
            """SELECT k.id, k.document_id, d.doc_type, d.title, d.source_uri,
                      k.section_path, k.text
               FROM chunks k JOIN documents d ON d.id = k.document_id
               WHERE d.source_uri = ? AND d.status = 'current'
               ORDER BY k.ordinal""", (uri,)).fetchall()
        if not rows:
            return None
        return [Hit(chunk_id=r['id'], document_id=r['document_id'],
                    doc_type=r['doc_type'], title=r['title'],
                    source_uri=r['source_uri'],
                    section_path=r['section_path'] or '',
                    text=r['text'], score=1.0, coverage=1.0)
                for r in rows]

    def _precedent_for(self, code: str, k: int) -> list[Hit]:
        """Closed cases of the same operational class, most recent first."""
        rows = self.store.conn.execute(
            """SELECT k.id, k.document_id, d.doc_type, d.title, d.source_uri,
                      k.section_path, k.text
               FROM chunks k JOIN documents d ON d.id = k.document_id
               WHERE d.anomaly_code = ? AND d.doc_type = 'narrative'
               ORDER BY d.effective_from DESC LIMIT ?""", (code, k)).fetchall()
        return [Hit(chunk_id=r["id"], document_id=r["document_id"],
                    doc_type=r["doc_type"], title=r["title"],
                    source_uri=r["source_uri"],
                    section_path=r["section_path"] or "",
                    text=r["text"], score=1.0, coverage=1.0)
                for r in rows]

    def _tier(self, question: str, r: Routed,
              looked_up: bool = False) -> tuple[str, str]:
        """
        Three outcomes, not two. See GUARDRAILS.md.

        The middle tier is the one that matters: a defect nobody wrote a
        procedure for still deserves a grounded suggestion, marked as derived
        and requiring sign-off, rather than either a confident wrong answer or
        a useless refusal.
        """
        # Exact means the defect class was known and its procedure was fetched
        # by name. It is never inferred from a score, because the scores do not
        # support that inference (see route()).
        if looked_up:
            return ("exact",
                    "The defect class is known and this is its procedure. "
                    "Follow it and cite it.")

        best = max((h.coverage for h in r.procedure), default=0.0)

        if not r.procedure or best < self.floor:
            # The fallback material must clear the floor too. Every pool returns
            # *something* for any query, so accepting a non-empty list here made
            # "what is our policy on annual leave" come back as derived rather
            # than as nothing found, which is the failure the refusal floor
            # exists to prevent.
            fallback = max((h.coverage for h in r.precedent + r.reference),
                           default=0.0)
            if fallback >= self.floor:
                return ("derived",
                        "No procedure matched closely enough. Any answer must be "
                        "derived from the analogous material below, marked "
                        "unverified, and signed off by a person.")
            return ("none",
                    "Nothing in the corpus answers this. Say so; do not assemble "
                    "something plausible from whatever ranked highest.")

        # Everything reached by search is derived, however good the score
        # looks. Without a known class there is no evidence that the top-ranked
        # procedure is the right one rather than merely the closest one.
        return ("derived",
                "No defect class was supplied, so this was reached by search. "
                "Treat the answer as derived, cite what it was reasoned from, "
                "and require sign-off.")
