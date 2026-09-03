"""
Detection queries that run, scored against what was planted.

The catalogue's `detection_sql` was documentation shaped like code. One of
twelve executed; the rest named tables that do not exist. Those statements are
rendered into the SOPs under "This is a deterministic check, it runs as a
query", so the corpus shipped SQL an operator could copy and watch fail.

These run. Each is written against the platform schema, each is validated at
import, and each is scored against `injected_defects`: a detection should find
exactly the planted rows. Fewer is a miss, more is a false positive, and both
are measurable now rather than asserted.

WHY DETECTION IS SQL AND NOT A MODEL. Finding a defect is a predicate over rows,
and a predicate is exactly what a database is for. It is cheap, it is exact, it
runs over every row rather than a retrieved sample, and its result can be
re-derived by anyone who reads the statement. A model asked to find duplicate
postings would look at whatever fitted in its context and report confidently
about that. The split is the same one the router makes: numbers by SQL, words by
retrieval.
"""

from __future__ import annotations

import re
import sqlite3

# Every query returns a single column named rrn.
DETECTION: dict[str, str] = {

    "ORPHAN_SWITCH": """
        SELECT t.rrn FROM transactions t
        LEFT JOIN ledger_entries l ON l.rrn = t.rrn
        WHERE t.status = 'approved' AND l.rrn IS NULL""",

    "ORPHAN_LEDGER": """
        SELECT DISTINCT l.rrn FROM ledger_entries l
        LEFT JOIN transactions t ON t.rrn = l.rrn
        WHERE t.rrn IS NULL""",

    "AMOUNT_MISMATCH": """
        SELECT DISTINCT t.rrn FROM transactions t
        JOIN ledger_entries l ON l.rrn = t.rrn
        WHERE l.entry_type = 'debit' AND l.amount_cents <> t.amount_cents""",

    "DUPLICATE_POSTING": """
        SELECT rrn FROM ledger_entries WHERE entry_type = 'debit'
        GROUP BY rrn HAVING count(*) = 2""",

    "TRIPLE_POSTING": """
        SELECT rrn FROM ledger_entries WHERE entry_type = 'debit'
        GROUP BY rrn HAVING count(*) > 2""",

    "UNBALANCED_ENTRY": """
        SELECT rrn FROM ledger_entries GROUP BY rrn
        HAVING sum(CASE WHEN entry_type = 'debit' THEN -amount_cents
                        ELSE amount_cents END) <> 0""",

    "STALE_REVERSAL": """
        SELECT t.rrn FROM transactions t
        WHERE t.status = 'reversed'
          AND NOT EXISTS (SELECT 1 FROM ledger_entries l
                          WHERE l.rrn = t.rrn AND l.entry_type = 'credit')""",

    "APPROVED_BUT_DECLINED": """
        SELECT DISTINCT t.rrn FROM transactions t
        JOIN ledger_entries l ON l.rrn = t.rrn
        WHERE t.status = 'declined'""",

    "FUTURE_DATED": """
        SELECT rrn FROM transactions WHERE created_at > datetime('now')""",

    "NEGATIVE_BALANCE": """
        SELECT DISTINCT l.rrn FROM ledger_entries l
        WHERE l.account_id IN (
            SELECT account_id FROM ledger_entries GROUP BY account_id
            HAVING sum(CASE WHEN entry_type = 'debit' THEN -amount_cents
                            ELSE amount_cents END) < 0)
          AND l.amount_cents > 50000000""",
}

_SELECT_ONLY = re.compile(r"^\s*(SELECT|WITH)\b", re.I)


def _validate() -> None:
    for code, sql in DETECTION.items():
        if not _SELECT_ONLY.match(sql.strip()):
            raise ValueError("%s: detection is not a SELECT" % code)
        if ";" in sql:
            raise ValueError("%s: detection contains a semicolon" % code)


_validate()


def find(db_path: str, code: str, limit: int = 25) -> list[str]:
    """The RRNs this defect class matches. Read-only, bounded."""
    sql = DETECTION.get(code)
    if sql is None:
        return []
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    try:
        return [r[0] for r in conn.execute(sql).fetchmany(limit)]
    finally:
        conn.close()


def sweep(db_path: str, *, examples: int = 3) -> list[dict]:
    """
    Run every predicate. This is the reconciliation run, not a lookup.

    "Find any ledger discrepancies" is the most natural question an operator
    asks and the router first answered it with "pick a defect class", which is
    backwards. A reconciliation engine does not ask which defect to look for; it
    runs every check it has and reports what fired. There is a general
    predicate, and it is the union of the specific ones.

    Ordered by severity from the catalogue, then by whether money is at risk.
    Both are data rather than judgement, so the ordering is reviewable and does
    not change when a model does.
    """
    from sim.catalogue import BY_CODE

    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)

    out = []
    for code, sql in DETECTION.items():
        a = BY_CODE.get(code)
        try:
            rows = [r[0] for r in conn.execute(sql)]
        except sqlite3.Error as exc:
            out.append({"code": code, "error": str(exc)[:60], "count": -1})
            continue
        if not rows:
            continue
        out.append({
            "code": code,
            "title": a.title if a else code,
            "severity": a.severity if a else "unknown",
            "money_at_risk": bool(a.money_at_risk) if a else False,
            "count": len(rows),
            "examples": rows[:examples],
        })
    conn.close()

    out.sort(key=lambda d: (rank.get(d.get("severity"), 9),
                            not d.get("money_at_risk"),
                            -d.get("count", 0)))
    return out


def score(db_path: str) -> list[dict]:
    """
    Per class: planted, found, missed, and the extras split two ways.

    THE SPLIT MATTERS AND THE FIRST VERSION MISSED IT. Scoring extras as plain
    false positives said five of ten detections were wrong. They were not.
    Injecting a duplicate posting also unbalances that RRN's legs; planting a
    negative balance also creates a ledger entry with no transaction behind it.
    Those rows genuinely have the second defect, so a detection finding them is
    correct and calling it a false positive would push someone to narrow a query
    that is already right.

    So extras are separated into rows carrying some *other* planted defect,
    which is co-occurrence and expected, and rows carrying no planted defect at
    all, which is the only kind worth alarm. Defects co-occur in production too:
    that is why a break often satisfies several detections and why severity
    comes from the catalogue rather than from whichever query ran first.
    """
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    all_planted = {r[0] for r in conn.execute("SELECT rrn FROM injected_defects")}

    out = []
    for code, sql in DETECTION.items():
        planted = {r[0] for r in conn.execute(
            "SELECT rrn FROM injected_defects WHERE anomaly_code = ?", (code,))}
        try:
            found = {r[0] for r in conn.execute(sql)}
        except sqlite3.Error as exc:
            out.append({"code": code, "error": str(exc)[:60]})
            continue

        extra = found - planted
        out.append({
            "code": code,
            "planted": len(planted),
            "found": len(found),
            "hits": len(planted & found),
            "missed": len(planted - found),
            "co_occurring": len(extra & all_planted),
            "on_clean_rows": len(extra - all_planted),
        })
    conn.close()
    return out
