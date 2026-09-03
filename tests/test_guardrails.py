"""
Regression tests for the guardrails.

Every test here corresponds to something that was measured wrong, built wrong,
or nearly built wrong during development. The eval measures retrieval quality
and moves as the corpus moves; these assert behaviour and must not move at all.

The distinction matters. A retrieval score dropping four points is information.
A superseded circular becoming eligible is a compliance failure, and it should
fail a test rather than show up as a slightly different percentage.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.answer import sql_tool
from app.answer.router import Router, classify
from app.ingest.chunker import chunk_markdown
from app.ingest.pipeline import circular_meta
from app.retrieve.hybrid import Hybrid
from app.retrieve.store import Store, content_terms


# --------------------------------------------------------------- chunking

def test_chunk_carries_its_heading_trail():
    """
    A chunk with no section path can be quoted but not cited, and the trail is
    also most of why a keyword search finds the right section at all.
    """
    chunks = chunk_markdown(
        "# Title\n\n## 3. Resolution procedure\n\n"
        "1. Confirm both entries carry the same RRN and reverse the second.\n"
        "2. Notify the customer before they notice the second debit.\n")
    assert chunks
    body = chunks[0]
    assert "Resolution procedure" in body.section_path
    assert body.text.startswith(body.section_path)


def test_chunker_cuts_on_headings_not_length():
    md = ("# T\n\n## A\n\n" + "alpha alpha alpha. " * 12 +
          "\n\n## B\n\n" + "beta beta beta. " * 12)
    paths = {c.section_path for c in chunk_markdown(md)}
    assert any(p.endswith("A") for p in paths)
    assert any(p.endswith("B") for p in paths)


# ------------------------------------------------------------- supersession

def test_circular_status_is_read_from_the_document():
    """
    Without this every circular lands as 'current' and the date filter has
    nothing to filter on. This was a real bug: it made supersession score 0%.
    """
    meta = circular_meta("**Status:** SUPERSEDED  |  **Effective from:** 2023-03-14\n"
                         "**Effective to:** 2025-07-01\n"
                         "**Replaced by:** CIR-x-2025\n")
    assert meta["status"] == "superseded"
    assert meta["effective_to"] == "2025-07-01"
    assert meta["superseded_by"]


def test_superseded_document_is_never_eligible(index):
    """
    The compliance control. A superseded circular is topically perfect and
    wrong, so it must be excluded by predicate rather than merely outranked.
    """
    s = Store(index)
    hits = s.search("what is the fee cap for wallet transfer", k=10)
    uris = {h.source_uri for h in hits}
    assert "circular/CIR-wallet-transfer-2025.md" in uris
    assert "circular/CIR-wallet-transfer-2023.md" not in uris
    s.close()


def test_superseded_is_present_but_filtered_not_absent(index):
    """It stays indexed on purpose, so the filter is what is being tested."""
    conn = sqlite3.connect("file:%s?mode=ro" % index, uri=True)
    n = conn.execute(
        "SELECT count(*) FROM documents WHERE status = 'superseded'").fetchone()[0]
    assert n == 1
    conn.close()


# ------------------------------------------------------------- coverage

def test_content_terms_strips_filler():
    """
    Stopword removal is what separates a real question from junk. Without it
    'what is our policy on annual leave' shares 'what is our on' with half the
    corpus and scores like a real question.
    """
    assert content_terms("what is our policy on annual leave") == {
        "policy", "annual", "leave"}
    assert content_terms("how do I") == set()


def test_junk_question_gets_no_coverage(index):
    s = Store(index)
    for junk in ("what is the capital of France",
                 "who won the cricket match yesterday"):
        best = max((h.coverage for h in s.search(junk, k=5)), default=0.0)
        assert best == 0.0, junk
    s.close()


def test_real_question_gets_coverage(index):
    s = Store(index)
    hits = s.search("the customer was charged twice", k=5)
    assert hits
    assert max(h.coverage for h in hits) > 0.0
    s.close()


# --------------------------------------------------------------- routing

@pytest.mark.parametrize("q,want", [
    ("how many disputes are still open", "numeric"),
    ("what is the total value of breaks", "numeric"),
    ("what is the balance on 03001234567", "lookup"),
    ("the customer was charged twice", "guidance"),
])
def test_intent_classification(q, want):
    assert classify(q) == want


def test_numeric_never_retrieves(index):
    """A count must come from SQL. Retrieval must not even be attempted."""
    r = Router(Hybrid(index), mode="keyword").route("how many disputes are open")
    assert r.intent == "numeric"
    assert r.procedure == [] and r.precedent == [] and r.reference == []


def test_exact_tier_requires_a_known_class(index):
    """
    Exact is a lookup, never a score. Measuring showed no coverage threshold
    separates a correct procedure from a wrong one, so the tier cannot be
    inferred from retrieval at all.
    """
    rt = Router(Hybrid(index), mode="keyword")
    with_code = rt.route("the customer was charged twice",
                         anomaly_code="DUPLICATE_POSTING")
    without = rt.route("the customer was charged twice")
    assert with_code.tier == "exact"
    assert with_code.procedure[0].source_uri == "sop/SOP-DUPLICATE_POSTING.md"
    assert without.tier != "exact"


def test_junk_is_none_not_derived(index):
    """
    Regression. The derived fallback accepted any non-empty precedent list, and
    every pool returns something for any query, so junk came back as derived.
    """
    r = Router(Hybrid(index), mode="keyword").route("what is our policy on annual leave")
    assert r.tier == "none"


def test_precedent_for_known_class_is_exact(index):
    """
    Narratives and procedures share no vocabulary for the same defect, so
    precedent for a known class is a lookup on anomaly_code.
    """
    r = Router(Hybrid(index), mode="keyword").route(
        "the customer was charged twice", anomaly_code="DUPLICATE_POSTING",
        precedent_k=3)
    assert r.precedent
    assert all("N-TEST01" in h.source_uri for h in r.precedent)


# --------------------------------------------------------------- sql tool

def test_registry_rejects_non_select():
    bad = sql_tool.Query(name="evil", question="x", sql="DELETE FROM sim_disputes")
    sql_tool.REGISTRY.append(bad)
    try:
        with pytest.raises(ValueError):
            sql_tool._validate_registry()
    finally:
        sql_tool.REGISTRY.remove(bad)


def test_registry_rejects_multiple_statements():
    bad = sql_tool.Query(name="two", question="x",
                         sql="SELECT 1; DROP TABLE sim_disputes")
    sql_tool.REGISTRY.append(bad)
    try:
        with pytest.raises(ValueError):
            sql_tool._validate_registry()
    finally:
        sql_tool.REGISTRY.remove(bad)


def test_connection_is_read_only(data_db):
    conn = sqlite3.connect("file:%s?mode=ro" % data_db, uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM sim_disputes")
    conn.close()


def test_parameter_is_a_value_not_sql(data_db):
    """An injection attempt is bound as a value and matches nothing."""
    r = sql_tool.run(data_db, sql_tool.BY_NAME["count_by_response_code"],
                     {"code": "00'; DROP TABLE sim_disputes; --"})
    assert r.rows == []
    conn = sqlite3.connect("file:%s?mode=ro" % data_db, uri=True)
    assert conn.execute("SELECT count(*) FROM sim_disputes").fetchone()[0] > 0
    conn.close()


def test_missing_parameter_is_refused(data_db):
    r = sql_tool.run(data_db, sql_tool.BY_NAME["count_by_response_code"])
    assert "missing parameter" in r.error


def test_unanticipated_question_gets_nothing(data_db):
    """The registry is closed on purpose: no query means no number."""
    r = sql_tool.answer(data_db, "what is the airspeed velocity of a swallow")
    assert r.query is None and r.error


def test_result_shows_its_statement(data_db):
    r = sql_tool.answer(data_db, "how many disputes are still open")
    assert "SELECT" in r.render()
    assert r.rows[0][0] == 4


def test_short_sections_are_not_dropped():
    """
    Regression, and the worst bug found so far. MIN_CHARS was 120, which
    silently deleted every "## 2. The cap" section from the fee circulars.
    Those sections are one sentence and are the only place the figure appears,
    so no chunk in the corpus carried a cap. A question about the current cap
    retrieved the right document with the answer removed from it, and the
    supersession measure still read 100% because it checked which document
    ranked rather than whether the chunk held the answer.
    """
    md = ("# Fee cap: wallet transfer\n\n"
          "## 2. The cap\n\n"
          "The maximum fee is **PKR 4,000** per transaction.\n")
    chunks = chunk_markdown(md)
    assert any("maximum fee" in c.text for c in chunks), \
        "the section carrying the figure was dropped"
    assert any("The cap" in c.section_path for c in chunks)


def test_retrieved_chunk_carries_the_answer(index):
    """
    Retrieving the right document is not the same as retrieving the answer.
    Assert on content, because document identity hid the bug above.
    """
    s = Store(index)
    hits = s.search("what is the fee cap for wallet transfer", k=10)
    current = [h for h in hits
               if h.source_uri == "circular/CIR-wallet-transfer-2025.md"]
    assert current, "current circular not retrieved at all"
    assert any("maximum fee" in h.text for h in current), \
        "right document, but no chunk carries the figure"
    assert not any("2,500" in h.text for h in hits), \
        "the superseded figure leaked into the results"
    s.close()
