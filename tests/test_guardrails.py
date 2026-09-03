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


# ---------------------------------------------------------------- lookup

def test_lookup_needs_an_identifier():
    """
    An exact-value question without an identifier is refused, not guessed at.
    Inferring which account someone meant is exactly the kind of helpfulness
    that produces a confident answer about the wrong customer.
    """
    from app.answer.lookup import answer as look
    v = look(None, "what is the current balance")
    assert v.error and "identifier" in v.error


def test_balance_is_derived_from_postings(tmp_path):
    """
    There is no balance column and there should not be. A stored figure is a
    cache that can disagree with the entries beneath it, and the entries are
    the truth.
    """
    import sqlite3 as s3
    from app.answer.lookup import LocalLedger

    db = str(tmp_path / "ledger.db")
    c = s3.connect(db)
    c.executescript("""
        CREATE TABLE accounts (account_id TEXT, user_id TEXT, msisdn TEXT,
                               type TEXT, opened_at TEXT);
        CREATE TABLE ledger_entries (entry_id INTEGER, rrn TEXT, account_id TEXT,
                                     entry_type TEXT, amount_cents INTEGER);
        CREATE TABLE cards (card_number TEXT, account_id TEXT, status TEXT,
                            issued_at TEXT);
    """)
    c.execute("INSERT INTO accounts VALUES ('acc_1','u1','03001112222','wallet','2026-01-01')")
    c.executemany("INSERT INTO ledger_entries VALUES (?,?,?,?,?)",
                  [(1, "R1", "acc_1", "credit", 10_000),
                   (2, "R2", "acc_1", "debit", 2_500),
                   (3, "R3", "acc_1", "credit", 500)])
    c.commit()
    c.close()

    v = LocalLedger(db).balance("acc_1")
    assert v.value == 8_000                    # 10000 - 2500 + 500
    assert v.detail["postings summed"] == 3
    assert v.as_at                             # a balance is true at a time
    assert v.source


def test_lookup_by_msisdn_and_missing_account(tmp_path):
    import sqlite3 as s3
    from app.answer.lookup import LocalLedger

    db = str(tmp_path / "ledger2.db")
    c = s3.connect(db)
    c.executescript("""
        CREATE TABLE accounts (account_id TEXT, user_id TEXT, msisdn TEXT,
                               type TEXT, opened_at TEXT);
        CREATE TABLE ledger_entries (entry_id INTEGER, rrn TEXT, account_id TEXT,
                                     entry_type TEXT, amount_cents INTEGER);
        CREATE TABLE cards (card_number TEXT, account_id TEXT, status TEXT,
                            issued_at TEXT);
    """)
    c.execute("INSERT INTO accounts VALUES ('acc_9','u9','03009998888','wallet','2026-01-01')")
    c.execute("INSERT INTO ledger_entries VALUES (1,'R1','acc_9','credit',4200)")
    c.commit(); c.close()

    lg = LocalLedger(db)
    assert lg.balance("03009998888").value == 4200
    assert lg.balance("03000000000").error


# ------------------------------------------------------- query selection

def test_selection_matches_whole_words_not_substrings():
    """
    Regression, and the third time substring matching caused a real bug.
    "how many swallows migrate each year" selected the resolution-rate query
    because "migrate" contains "rate". A closed registry only protects you if
    the selector cannot be fooled: a wrong query answered confidently is worse
    than the refusal it replaced.
    """
    assert sql_tool.pick("how many swallows migrate each year") is None
    assert sql_tool.pick("what share of disputes have been resolved") is not None


def test_registry_spans_both_databases():
    """
    Every entity question used to be unanswerable, because no query could reach
    the database users and accounts live in.
    """
    sources = {q.source for q in sql_tool.REGISTRY}
    assert sources == {"data", "ledger"}
    assert sql_tool.pick("how many users do we have").source == "ledger"


def test_ledger_query_without_a_ledger_path_is_refused(data_db):
    r = sql_tool.run(data_db, sql_tool.BY_NAME["count_users"])
    assert "platform database" in r.error


def test_refusal_lists_what_is_available(data_db):
    """
    Refusing is correct; refusing without saying what exists makes a closed
    registry feel broken rather than deliberate.
    """
    r = sql_tool.answer(data_db, "how many unicorns do we have")
    assert r.query is None
    assert "How many disputes are still open?" in r.error


# ------------------------------------------------------------- citations

def _fake_reply(text, cites):
    class C:
        def __init__(s, t, i): s.cited_text, s.start_block_index, s.type = t, i, "x"
    class B:
        type = "text"
        def __init__(s): s.text, s.citations = text, [C(t, i) for t, i in cites]
    class R:
        content = [B()]
    return R()


def _kwargs(blocks):
    return {"messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "content",
         "content": [{"type": "text", "text": b} for b in blocks]}}]}]}


def test_citation_must_narrow_down_the_source():
    """
    Regression, and the model found this one, not me. Asked to cite, a 7B
    emitted thirteen identical citations of the document title, which the
    chunker prepends to every chunk as its heading trail. Every span was
    verbatim so every one passed, and together they grounded nothing.

    A citation has to identify a particular source. One present in every block
    identifies none.
    """
    from app.answer.citations import verify
    blocks = ["TITLE. step one text", "TITLE. step two text", "TITLE. step three"]
    kw = _kwargs(blocks)

    good = verify(_fake_reply("x", [("step one text", 0)]), kw)
    assert good.ok

    gamed = verify(_fake_reply("x", [("TITLE.", 0), ("TITLE.", 1)]), kw)
    assert not gamed.ok
    assert any("identifies no particular source" in p for p in gamed.problems)


def test_same_span_cited_twice_is_rejected():
    from app.answer.citations import verify
    kw = _kwargs(["alpha unique one", "beta unique two", "gamma unique three"])
    v = verify(_fake_reply("x", [("alpha unique one", 0), ("alpha unique one", 0)]), kw)
    assert not v.ok
    assert any("more than once" in p for p in v.problems)


def test_misnumbered_but_verbatim_citation_is_accepted():
    """
    A fabricated quote is a lie; a misnumbered one is a typo. Only the first is
    a safety property, so the span is looked for in every supplied block and the
    index is corrected rather than the answer withheld.
    """
    from app.answer.citations import verify
    kw = _kwargs(["alpha unique one", "beta unique two", "gamma unique three"])
    v = verify(_fake_reply("x", [("gamma unique three", 0)]), kw)
    assert v.ok
    assert v.citations[0]["block_index"] == 2
    assert v.citations[0]["corrected_from"] == 0


def test_fabricated_quote_is_rejected():
    from app.answer.citations import verify
    kw = _kwargs(["alpha unique one", "beta unique two"])
    v = verify(_fake_reply("x", [("this was never in any block", 0)]), kw)
    assert not v.ok
    assert any("no supplied block" in p for p in v.problems)


# ------------------------------------------------------- intent routing

def test_find_intent_is_distinct_from_numeric():
    """
    'Find an RRN that shows this' and 'how many are there' are different
    questions. Without a find intent the first returned the procedure, which
    answers a question nobody asked.
    """
    from app.answer.router import classify
    assert classify("find an rrn that shows this") == "find"
    assert classify("show me an example of a duplicate posting") == "find"
    assert classify("how many duplicate postings are there") == "numeric"
    assert classify("what do I do about a duplicate posting") == "guidance"


def test_semantic_intent_needs_the_floor():
    """
    Cosine always returns a nearest neighbour and never an absence, so without
    a floor there is no refusal. At 0.60, 'what is our policy on annual leave'
    scored 0.62 against an account-status exemplar and was routed to a balance
    lookup.
    """
    from app.answer.router import SEMANTIC_FLOOR
    assert SEMANTIC_FLOOR >= 0.65


def test_detection_predicates_are_select_only():
    """The catalogue's detection SQL was documentation shaped like code: one of
    twelve executed. These run, and a non-SELECT fails at import."""
    from sim import detect
    assert len(detect.DETECTION) >= 10
    for code, sql in detect.DETECTION.items():
        assert sql.strip().upper().startswith("SELECT"), code
        assert ";" not in sql, code
