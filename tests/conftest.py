"""
A tiny index built from scratch per test session.

Deliberately not the 10,000 chunk corpus. These tests lock in behaviour, not
retrieval quality, and behaviour is easier to assert on eight documents you can
hold in your head. Retrieval quality is what eval/run_eval.py measures, and it
needs volume for the opposite reason.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ingest.pipeline import Writer, connect  # noqa: E402
from app.ingest.chunker import chunk_markdown, chunk_record  # noqa: E402

SOP = """\
# One authorisation, two postings

**Code:** `DUPLICATE_POSTING`  |  **Severity:** critical

## 1. What this is

The same RRN was posted to the ledger more than once, double-charging the customer.

## 2. How it is detected

More than one ledger entry sharing an RRN. This is a deterministic check.

## 3. Resolution procedure

1. Confirm both entries carry the same RRN.
2. Identify which posting came second and reverse that one.
3. Notify the customer before they notice the second debit.
"""

SOP2 = """\
# Same reference, different amounts

**Code:** `AMOUNT_MISMATCH`  |  **Severity:** high

## 1. What this is

The switch and the ledger agree a transaction happened but not for how much.

## 2. How it is detected

Matched RRN where the two amounts differ by more than the known fee.
"""

CIRC_OLD = """\
# Fee cap: wallet transfer

**Status:** SUPERSEDED  |  **Effective from:** 2023-03-14
**Effective to:** 2025-07-01
**Replaced by:** CIR-wallet-transfer-2025

## 2. The cap

The maximum fee is **PKR 2,500** per transaction, effective 2023-03-14.
"""

CIRC_NEW = """\
# Fee cap: wallet transfer

**Status:** IN FORCE  |  **Effective from:** 2025-07-01
**Replaces:** CIR-wallet-transfer-2023

## 2. The cap

The maximum fee is **PKR 4,000** per transaction, effective 2025-07-01.
"""


@pytest.fixture(scope="session")
def index(tmp_path_factory) -> str:
    """A small index with a procedure, a supersession pair, and a narrative."""
    from app.ingest.pipeline import circular_meta

    path = str(tmp_path_factory.mktemp("idx") / "test.db")
    conn = connect(path)
    w = Writer(conn)

    w.put(source_uri="sop/SOP-DUPLICATE_POSTING.md",
          title="One authorisation, two postings", doc_type="sop",
          source="generated", chunks=chunk_markdown(SOP))
    w.put(source_uri="sop/SOP-AMOUNT_MISMATCH.md",
          title="Same reference, different amounts", doc_type="sop",
          source="generated", chunks=chunk_markdown(SOP2))

    w.put(source_uri="circular/CIR-wallet-transfer-2023.md",
          title="Fee cap: wallet transfer", doc_type="circular",
          source="generated", chunks=chunk_markdown(CIRC_OLD),
          **circular_meta(CIRC_OLD))
    w.put(source_uri="circular/CIR-wallet-transfer-2025.md",
          title="Fee cap: wallet transfer", doc_type="circular",
          source="generated", chunks=chunk_markdown(CIRC_NEW),
          **circular_meta(CIRC_NEW))

    w.put(source_uri="sim://narrative/N-TEST01",
          title="Dispute AAAA: The account was debited twice",
          doc_type="narrative", source="sim", anomaly_code="DUPLICATE_POSTING",
          chunks=chunk_record("Dispute AAAA",
                              "The account was debited twice. RRN ABCDEF123456. "
                              "Case closed as upheld: the customer was refunded."),
          effective_from="2026-01-02")
    w.put(source_uri="sim://narrative/N-TEST02",
          title="Dispute BBBB: The fee charged was not the fee quoted",
          doc_type="narrative", source="sim", anomaly_code="FEE_OVERCHARGE",
          chunks=chunk_record("Dispute BBBB",
                              "The fee charged was not the fee quoted. "
                              "Case closed as rejected."),
          effective_from="2026-01-01")

    conn.commit()
    conn.close()
    return path


@pytest.fixture(scope="session")
def data_db(tmp_path_factory) -> str:
    """A few simulator rows, enough for the SQL registry to run against."""
    path = str(tmp_path_factory.mktemp("sim") / "sim.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE sim_transactions (
            rrn TEXT PRIMARY KEY, occurred_at TEXT, channel TEXT, entry_mode TEXT,
            resp_code TEXT, amount_minor INTEGER, msisdn TEXT, merchant TEXT,
            branch TEXT, source TEXT);
        CREATE TABLE sim_disputes (
            dispute_id TEXT PRIMARY KEY, rrn TEXT, reason_code TEXT, status TEXT,
            opened_at TEXT, resolved_at TEXT, outcome_code TEXT, source TEXT);
    """)
    conn.executemany(
        "INSERT INTO sim_transactions VALUES (?,?,?,?,?,?,?,?,?,?)",
        [("R%03d" % i, "2026-01-01", "agent", "05",
          "00" if i % 2 else "051", 1000 * i, "0300", None, "Gulberg", "sim")
         for i in range(10)])
    conn.executemany(
        "INSERT INTO sim_disputes VALUES (?,?,?,?,?,?,?,?)",
        [("D%03d" % i, "R%03d" % i, "double_debit", "open",
          "2026-01-01", None, None, "sim") for i in range(4)] +
        [("D%03d#r" % i, "R%03d" % i, "double_debit", "upheld",
          "2026-01-01", "2026-01-02", "upheld_refunded", "sim") for i in range(2)])
    conn.commit()
    conn.close()
    return path
