"""
Plant real defects in real rows, and record exactly what was planted.

    python sim/inject.py --base ../microfinance-microservices/practice.db \
                         --out defects.db --per-class 25

WHY THIS EXISTS. catalogue.py has claimed since it was written that
"sim/simulate.py injects instances of these into the data". It never did.
simulate.py writes transactions, disputes and narratives *about* defects; the
defective rows themselves were never created. Every table in the project was
clean: zero duplicate postings, zero orphans, zero amount mismatches.

That made a lot of downstream work hollow. The held-out anomalies were described
as "injected into the data with no SOP written", and only the second half was
true. A question like "find an RRN that shows this" could not be answered, not
for want of routing but because there was nothing to find.

HOW IT WORKS. Start from a copy of the platform's own practice database, which
is realistic and clean, then mutate specific rows to create specific defects.
Every mutation is recorded in `injected_defects`, so detection can be scored
against ground truth rather than against a claim: a detection query should find
exactly the planted rows, no more and no fewer. Finding fewer is a miss, finding
more is a false positive, and both are now measurable.

WHAT CANNOT BE INJECTED HERE, AND IS NOT PRETENDED. Six of the sixteen classes
need columns this schema does not have: a currency, a correlation ID, a
settlement slot, a fee schedule, an agent float. Those are marked
`injectable=False` and their detection SQL is marked not runnable, rather than
shipping a query that looks executable and is not.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GROUND_TRUTH = """
CREATE TABLE IF NOT EXISTS injected_defects (
    rrn           TEXT NOT NULL,
    anomaly_code  TEXT NOT NULL,
    note          TEXT,
    PRIMARY KEY (rrn, anomaly_code)
)
"""


def _rrn() -> str:
    return uuid.uuid4().hex[:12].upper()


def _pick(conn, sql, n):
    return [r[0] for r in conn.execute(sql + " LIMIT ?", (n,))]


def _record(conn, rrn, code, note):
    conn.execute("INSERT OR IGNORE INTO injected_defects VALUES (?,?,?)",
                 (rrn, code, note))


# --------------------------------------------------------------------------
# One injector per class. Each mutates rows and records what it did.
# --------------------------------------------------------------------------

def orphan_switch(conn, n):
    """Approved at the switch, absent from the ledger: delete the postings."""
    rrns = _pick(conn, """SELECT t.rrn FROM transactions t
                          JOIN ledger_entries l ON l.rrn = t.rrn
                          WHERE t.status = 'approved' GROUP BY t.rrn""", n)
    for r in rrns:
        conn.execute("DELETE FROM ledger_entries WHERE rrn = ?", (r,))
        _record(conn, r, "ORPHAN_SWITCH", "ledger entries deleted")
    return len(rrns)


def orphan_ledger(conn, n):
    """Posted to the ledger, unknown to the switch: entries with no transaction."""
    accounts = _pick(conn, "SELECT account_id FROM accounts", n)
    made = 0
    for acc in accounts:
        r = _rrn()
        conn.execute("INSERT INTO ledger_entries (rrn, account_id, entry_type, "
                     "amount_cents) VALUES (?,?,?,?)", (r, acc, "debit", 25_000))
        _record(conn, r, "ORPHAN_LEDGER", "posting with no transaction")
        made += 1
    return made


def amount_mismatch(conn, n):
    """Same reference, different amounts: shift the ledger side."""
    rrns = _pick(conn, """SELECT t.rrn FROM transactions t
                          JOIN ledger_entries l ON l.rrn = t.rrn
                          WHERE t.rrn NOT IN (SELECT rrn FROM injected_defects)
                          GROUP BY t.rrn""", n)
    for r in rrns:
        conn.execute("UPDATE ledger_entries SET amount_cents = amount_cents + 1337 "
                     "WHERE rrn = ? AND entry_type = 'debit'", (r,))
        _record(conn, r, "AMOUNT_MISMATCH", "ledger debit shifted by 1337")
    return len(rrns)


def duplicate_posting(conn, n):
    """One authorisation, two postings: copy the debit leg."""
    rrns = _pick(conn, """SELECT rrn FROM ledger_entries
                          WHERE entry_type = 'debit'
                            AND rrn NOT IN (SELECT rrn FROM injected_defects)
                          GROUP BY rrn""", n)
    for r in rrns:
        conn.execute("""INSERT INTO ledger_entries (rrn, account_id, entry_type,
                        amount_cents)
                        SELECT rrn, account_id, entry_type, amount_cents
                        FROM ledger_entries WHERE rrn = ? AND entry_type = 'debit'
                        LIMIT 1""", (r,))
        _record(conn, r, "DUPLICATE_POSTING", "debit leg duplicated")
    return len(rrns)


def triple_posting(conn, n):
    """Held out. Three or more postings, which is not merely a worse duplicate."""
    rrns = _pick(conn, """SELECT rrn FROM ledger_entries
                          WHERE entry_type = 'debit'
                            AND rrn NOT IN (SELECT rrn FROM injected_defects)
                          GROUP BY rrn""", n)
    for r in rrns:
        for _ in range(2):
            conn.execute("""INSERT INTO ledger_entries (rrn, account_id,
                            entry_type, amount_cents)
                            SELECT rrn, account_id, entry_type, amount_cents
                            FROM ledger_entries WHERE rrn = ?
                              AND entry_type = 'debit' LIMIT 1""", (r,))
        _record(conn, r, "TRIPLE_POSTING", "debit leg duplicated twice")
    return len(rrns)


def unbalanced_entry(conn, n):
    """Legs that do not net to zero: add a debit with no matching credit."""
    rows = list(conn.execute("""SELECT rrn, account_id FROM ledger_entries
                                WHERE rrn NOT IN (SELECT rrn FROM injected_defects)
                                GROUP BY rrn LIMIT ?""", (n,)))
    for r, acc in rows:
        conn.execute("INSERT INTO ledger_entries (rrn, account_id, entry_type, "
                     "amount_cents) VALUES (?,?,?,?)", (r, acc, "debit", 999))
        _record(conn, r, "UNBALANCED_ENTRY", "orphan debit leg of 999")
    return len(rows)


def stale_reversal(conn, n):
    """Reversed at the switch, never credited back."""
    rrns = _pick(conn, """SELECT t.rrn FROM transactions t
                          WHERE t.status = 'reversed'
                            AND t.rrn NOT IN (SELECT rrn FROM injected_defects)""", n)
    for r in rrns:
        conn.execute("DELETE FROM ledger_entries WHERE rrn = ? AND entry_type = 'credit'",
                     (r,))
        _record(conn, r, "STALE_REVERSAL", "offsetting credit removed")
    return len(rrns)


def approved_but_declined(conn, n):
    """Declined at the switch, yet money moved."""
    rrns = _pick(conn, """SELECT t.rrn FROM transactions t
                          JOIN ledger_entries l ON l.rrn = t.rrn
                          WHERE t.status = 'approved'
                            AND t.rrn NOT IN (SELECT rrn FROM injected_defects)
                          GROUP BY t.rrn""", n)
    for r in rrns:
        conn.execute("UPDATE transactions SET status = 'declined' WHERE rrn = ?", (r,))
        _record(conn, r, "APPROVED_BUT_DECLINED", "status set to declined, postings kept")
    return len(rrns)


def future_dated(conn, n):
    """A clock that drifted forward."""
    rrns = _pick(conn, """SELECT rrn FROM transactions
                          WHERE rrn NOT IN (SELECT rrn FROM injected_defects)""", n)
    for r in rrns:
        conn.execute("UPDATE transactions SET created_at = '2099-01-01 00:00:00' "
                     "WHERE rrn = ?", (r,))
        _record(conn, r, "FUTURE_DATED", "created_at moved to 2099")
    return len(rrns)


def negative_balance(conn, n):
    """A wallet driven below zero, which the solvency invariant forbids."""
    accounts = _pick(conn, "SELECT account_id FROM accounts WHERE type = 'wallet'", n)
    for acc in accounts:
        r = _rrn()
        conn.execute("INSERT INTO ledger_entries (rrn, account_id, entry_type, "
                     "amount_cents) VALUES (?,?,?,?)",
                     (r, acc, "debit", 99_999_999))
        _record(conn, r, "NEGATIVE_BALANCE", "huge debit on %s" % acc)
    return len(accounts)


INJECTORS = [
    ("ORPHAN_SWITCH", orphan_switch),
    ("ORPHAN_LEDGER", orphan_ledger),
    ("AMOUNT_MISMATCH", amount_mismatch),
    ("DUPLICATE_POSTING", duplicate_posting),
    ("TRIPLE_POSTING", triple_posting),
    ("UNBALANCED_ENTRY", unbalanced_entry),
    ("STALE_REVERSAL", stale_reversal),
    ("APPROVED_BUT_DECLINED", approved_but_declined),
    ("FUTURE_DATED", future_dated),
    ("NEGATIVE_BALANCE", negative_balance),
]

# Named so the gap is explicit rather than silent.
NOT_INJECTABLE = {
    "MISSING_CORRELATION": "no correlation_id column in this schema",
    "SETTLEMENT_GAP": "no slots or settlement_files tables",
    "FEE_OVERCHARGE": "no fee column or fee_schedule table",
    "CURRENCY_MISMATCH": "no currency column",
    "SLOT_OVERLAP": "no settlement_lines table",
    "AGENT_FLOAT_NEGATIVE": "no float_movements table",
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="../microfinance-microservices/practice.db")
    p.add_argument("--out", default="defects.db")
    p.add_argument("--per-class", type=int, default=25)
    a = p.parse_args()

    shutil.copyfile(a.base, a.out)
    conn = sqlite3.connect(a.out)
    conn.execute(GROUND_TRUTH)

    # Capture what the base data already matches, BEFORE planting anything.
    #
    # "The base is clean" was an assumption nobody checked, and it was wrong.
    # practice.db is generated to be deliberately messy, and one of its messes
    # is one of these defect classes: all 156 of its reversed transactions lack
    # an offsetting credit, which is precisely STALE_REVERSAL. Scoring against
    # planted rows alone therefore reported 131 false positives for a query that
    # was correct.
    #
    # Ground truth is what the data contains, not only what this script added.
    from sim.detect import DETECTION
    pre = 0
    for code, sql in DETECTION.items():
        try:
            for (r,) in conn.execute(sql):
                _record(conn, r, code, "pre-existing in the base data")
                pre += 1
        except sqlite3.Error:
            pass
    conn.commit()
    if pre:
        print("  %-24s %3d found in the base data before injection" % ("(pre-existing)", pre))
        print()

    total = 0
    for code, fn in INJECTORS:
        n = fn(conn, a.per_class)
        conn.commit()
        print("  %-24s %3d planted" % (code, n))
        total += n

    print()
    for code, why in sorted(NOT_INJECTABLE.items()):
        print("  %-24s  not injectable: %s" % (code, why))

    rows = conn.execute("SELECT count(*) FROM injected_defects").fetchone()[0]
    print()
    print("%d defects planted across %d classes, %d ground-truth rows in %s"
          % (total, len(INJECTORS), rows, a.out))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
