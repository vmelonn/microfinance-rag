"""
Continuous event simulator. Runs until you stop it, never repeats itself.

    python sim/simulate.py                          # sqlite, 20 events/sec, forever
    python sim/simulate.py --rate 200               # faster
    python sim/simulate.py --for 30m                # stop after 30 minutes
    python sim/simulate.py --dsn postgresql://...   # into Postgres instead

WHY THIS IS SEPARATE FROM practice_db.py. That generator is seeded and
deterministic on purpose: the evaluation set is written against exactly those
rows, so it must not move. This one is the opposite. Every run is unique, it
never terminates on its own, and it exists to grow a corpus that keeps looking
different so retrieval is never scored against something it has memorised.

Keep the two apart when ingesting. Everything written here carries
source='sim', so the eval corpus can always exclude it.

WHAT IT PRODUCES. A transaction stream, a minority of which turn into disputes,
a majority of which are later resolved. Each resolution gets a written
narrative, because a dispute row is a label and RAG needs prose. The narratives
are assembled from a large enough vocabulary that repeats are vanishingly rare:
roughly 10^9 distinct bodies before the identifiers are even considered.
"""

from __future__ import annotations

import argparse
import random
import signal
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------
# vocabulary. every axis multiplies, which is where the variety comes from.
# --------------------------------------------------------------------------

REASONS = [
    ("agent_no_cash",      "the agent did not hand over cash"),
    ("double_debit",       "the account was debited twice"),
    ("wrong_amount",       "the amount debited did not match the amount entered"),
    ("no_credit",          "the beneficiary was never credited"),
    ("unauthorised",       "the customer does not recognise the transaction"),
    ("failed_but_debited", "the transaction failed but the account was still debited"),
    ("atm_short",          "the ATM dispensed less than the amount requested"),
    ("merchant_no_goods",  "the merchant took payment and did not release the goods"),
    ("reversal_missing",   "a promised reversal never arrived"),
    ("fee_disputed",       "the fee charged was not the fee quoted"),
    ("duplicate_bill",     "the same bill was paid twice"),
    ("stale_balance",      "the balance shown did not reflect the transaction"),
]

CHANNELS = ["agent", "atm", "pos", "ussd", "app", "web"]

ENTRY_MODES = [
    ("01", "manual entry"), ("02", "magnetic stripe"), ("05", "chip"),
    ("07", "contactless"), ("81", "e-commerce"), ("90", "full track"),
]

RESP_CODES = [
    ("00", "approved"), ("051", "insufficient funds"), ("05", "do not honour"),
    ("14", "invalid account"), ("54", "expired card"), ("91", "issuer unavailable"),
    ("96", "system malfunction"), ("13", "invalid amount"),
]

OUTCOMES = [
    ("upheld_refunded",    "upheld", "the customer was refunded in full"),
    ("upheld_partial",     "upheld", "a partial refund was issued for the difference"),
    ("rejected_evidence",  "rejected", "the switch log showed the transaction completed normally"),
    ("rejected_duplicate", "rejected", "the case duplicated an earlier one already closed"),
    ("timing_difference",  "closed", "the posting landed in the following settlement slot"),
    ("agent_recovered",    "upheld", "the amount was recovered from the agent's float"),
    ("merchant_credited",  "upheld", "the merchant credited the customer directly"),
    ("withdrawn",          "closed", "the customer withdrew the complaint"),
]

STEPS = [
    "pulled the switch log for the RRN",
    "compared the ledger posting against the settlement file",
    "checked the agent's float movements for the day",
    "reviewed the terminal's batch totals",
    "confirmed the response code with the acquirer",
    "traced the correlation ID across all seven services",
    "checked whether a reversal had been attempted",
    "verified the beneficiary account was active",
    "looked for a matching entry in the following slot",
    "asked the branch to confirm the cash position",
    "re-ran the reconciliation for the affected window",
    "checked the customer's transaction history for a pattern",
    "confirmed the card was not blocked at the time",
    "reviewed the risk service decision for this transaction",
    "checked the ISO 8583 field 39 value against our records",
]

OPENERS = [
    "Customer reported that {reason}.",
    "Case raised at the {channel} channel: {reason}.",
    "Complaint logged after {reason}.",
    "Escalated from the branch. The customer states that {reason}.",
    "Ticket opened because {reason}.",
    "Received via the call centre: {reason}.",
    "Agent-side report. According to the customer, {reason}.",
    "Raised the same day. The customer's account is that {reason}.",
]

CLOSERS = [
    "Case closed as {status}: {outcome}.",
    "Resolved {status}. Outcome: {outcome}.",
    "Concluded {status} on review, since {outcome}.",
    "Final position is {status}, because {outcome}.",
    "Marked {status}. In the end {outcome}.",
]

BRANCHES = ["Gulberg", "Saddar", "Clifton", "Model Town", "Blue Area", "Cantt",
            "Hayatabad", "Latifabad", "Satellite Town", "University Road",
            "Bahria", "DHA Phase 2", "Garden Town", "Jinnah Colony"]

MERCHANTS = ["Al-Fatah Store", "Metro Cash", "Imtiaz Super", "Green Valley",
             "PSO Filling", "Shell Select", "Sadaf Traders", "Chase Up",
             "Naheed Store", "Utility Mart", "CityMart", "Rehmat Bakers"]


# --------------------------------------------------------------------------

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sim_transactions (
        rrn TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, channel TEXT NOT NULL,
        entry_mode TEXT NOT NULL, resp_code TEXT NOT NULL, amount_minor INTEGER NOT NULL,
        msisdn TEXT NOT NULL, merchant TEXT, branch TEXT NOT NULL, source TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS sim_disputes (
        dispute_id TEXT PRIMARY KEY, rrn TEXT NOT NULL, reason_code TEXT NOT NULL,
        status TEXT NOT NULL, opened_at TEXT NOT NULL, resolved_at TEXT,
        outcome_code TEXT, source TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS sim_narratives (
        narrative_id TEXT PRIMARY KEY, dispute_id TEXT NOT NULL, rrn TEXT NOT NULL,
        title TEXT NOT NULL, body TEXT NOT NULL, written_at TEXT NOT NULL,
        source TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_sim_disputes_rrn ON sim_disputes(rrn)",
    "CREATE INDEX IF NOT EXISTS ix_sim_narratives_dispute ON sim_narratives(dispute_id)",
]


@dataclass
class Stats:
    txns: int = 0
    disputes: int = 0
    resolved: int = 0
    started: float = 0.0

    def line(self) -> str:
        el = max(time.monotonic() - self.started, 1e-6)
        return ("%d txns  %d disputes  %d resolved  |  %.0fs elapsed  %.1f txn/s"
                % (self.txns, self.disputes, self.resolved, el, self.txns / el))


class Sink:
    """SQLite or Postgres behind one small interface."""

    def __init__(self, dsn: str | None, path: str):
        self.pg = dsn is not None
        if self.pg:
            import psycopg
            self.conn = psycopg.connect(dsn, autocommit=False)
            self.ph = "%s"
        else:
            self.conn = sqlite3.connect(path)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.ph = "?"
        cur = self.conn.cursor()
        for stmt in SCHEMA:
            cur.execute(stmt)
        self.conn.commit()

    def insert(self, table: str, cols: list[str], rows: list[tuple]) -> None:
        if not rows:
            return
        marks = ",".join([self.ph] * len(cols))
        sql = "INSERT INTO %s (%s) VALUES (%s)" % (table, ",".join(cols), marks)
        cur = self.conn.cursor()
        cur.executemany(sql, rows)

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()


def parse_duration(s: str) -> float:
    unit, mult = s[-1], {"s": 1, "m": 60, "h": 3600}
    if unit in mult:
        return float(s[:-1]) * mult[unit]
    return float(s)


def msisdn(rng: random.Random) -> str:
    return "03%d%07d" % (rng.randint(0, 4), rng.randint(0, 9_999_999))


def make_narrative(rng: random.Random, txn: dict, reason: tuple,
                   outcome: tuple) -> tuple[str, str]:
    """One resolution write-up. Deliberately varied in shape, not just wording."""
    _, reason_text = reason
    _, status, outcome_text = outcome

    opener = rng.choice(OPENERS).format(reason=reason_text, channel=txn["channel"])
    steps = rng.sample(STEPS, rng.randint(2, 4))
    body_steps = "The investigation " + "; ".join(steps) + "."
    closer = rng.choice(CLOSERS).format(status=status, outcome=outcome_text)

    facts = ("RRN %s, PKR %s, %s channel, entry mode %s, response code %s."
             % (txn["rrn"], "{:,}".format(txn["amount_minor"] // 100),
                txn["channel"], txn["entry_mode"], txn["resp_code"]))

    extra = ""
    if txn["merchant"] and rng.random() < 0.5:
        extra = " Merchant of record was %s." % txn["merchant"]
    if rng.random() < 0.35:
        extra += " Handled by the %s branch." % txn["branch"]

    title = "Dispute %s: %s" % (txn["rrn"][-8:], reason_text.capitalize())
    body = " ".join([opener, facts.strip(), body_steps, closer]) + extra
    return title, body


def run(args) -> int:
    rng = random.Random()                     # entropy-seeded: unique every run
    sink = Sink(args.dsn, args.db)
    stats = Stats(started=time.monotonic())

    stopping = {"now": False}

    def stop(signum, frame):
        stopping["now"] = True
        print("\nstopping, flushing...", file=sys.stderr)

    signal.signal(signal.SIGINT, stop)
    try:
        signal.signal(signal.SIGTERM, stop)
    except (AttributeError, ValueError):
        pass

    deadline = time.monotonic() + parse_duration(args.until) if args.until else None
    interval = 1.0 / args.rate if args.rate > 0 else 0.0

    open_disputes: list[tuple[str, dict, tuple]] = []
    tbuf, dbuf, nbuf = [], [], []
    last_report = time.monotonic()

    while not stopping["now"]:
        if deadline and time.monotonic() >= deadline:
            break

        now = datetime.now(timezone.utc)
        entry = rng.choice(ENTRY_MODES)
        resp = rng.choice(RESP_CODES) if rng.random() < 0.22 else RESP_CODES[0]
        channel = rng.choice(CHANNELS)

        txn = {
            "rrn": uuid.uuid4().hex[:12].upper(),
            "occurred_at": now.isoformat(),
            "channel": channel,
            "entry_mode": entry[0],
            "resp_code": resp[0],
            "amount_minor": rng.randrange(5_000, 15_000_00),
            "msisdn": msisdn(rng),
            "merchant": rng.choice(MERCHANTS) if channel in ("pos", "web", "app") else None,
            "branch": rng.choice(BRANCHES),
        }
        tbuf.append(tuple(txn[c] for c in
                          ("rrn", "occurred_at", "channel", "entry_mode", "resp_code",
                           "amount_minor", "msisdn", "merchant", "branch")) + ("sim",))
        stats.txns += 1

        # a minority become disputes
        if rng.random() < args.dispute_rate:
            reason = rng.choice(REASONS)
            did = "D-" + uuid.uuid4().hex[:10].upper()
            dbuf.append((did, txn["rrn"], reason[0], "open", now.isoformat(),
                         None, None, "sim"))
            open_disputes.append((did, txn, reason))
            stats.disputes += 1

        # resolve something opened earlier, so the queue behaves like a queue
        if open_disputes and rng.random() < args.resolve_rate:
            idx = rng.randrange(len(open_disputes))
            did, dtxn, reason = open_disputes.pop(idx)
            outcome = rng.choice(OUTCOMES)
            resolved_at = (now + timedelta(minutes=rng.randint(5, 2880))).isoformat()
            title, body = make_narrative(rng, dtxn, reason, outcome)
            nbuf.append(("N-" + uuid.uuid4().hex[:10].upper(), did, dtxn["rrn"],
                         title, body, resolved_at, "sim"))
            dbuf.append((did + "#r", dtxn["rrn"], reason[0], outcome[1],
                         dtxn["occurred_at"], resolved_at, outcome[0], "sim"))
            stats.resolved += 1

        if len(tbuf) >= args.batch:
            flush(sink, tbuf, dbuf, nbuf)
            tbuf, dbuf, nbuf = [], [], []

        if time.monotonic() - last_report >= args.report:
            print(stats.line(), file=sys.stderr)
            last_report = time.monotonic()

        if interval:
            time.sleep(interval)

    flush(sink, tbuf, dbuf, nbuf)
    sink.close()
    print("\nfinal: " + stats.line(), file=sys.stderr)
    print("%d disputes still open at exit" % len(open_disputes), file=sys.stderr)
    return 0


def flush(sink: Sink, tbuf, dbuf, nbuf) -> None:
    sink.insert("sim_transactions",
                ["rrn", "occurred_at", "channel", "entry_mode", "resp_code",
                 "amount_minor", "msisdn", "merchant", "branch", "source"], tbuf)
    # the resolution row reuses the dispute id with a suffix, so an UPDATE is
    # not needed and the writer stays append-only.
    sink.insert("sim_disputes",
                ["dispute_id", "rrn", "reason_code", "status", "opened_at",
                 "resolved_at", "outcome_code", "source"], dbuf)
    sink.insert("sim_narratives",
                ["narrative_id", "dispute_id", "rrn", "title", "body",
                 "written_at", "source"], nbuf)
    sink.commit()


def main() -> int:
    p = argparse.ArgumentParser(description="Continuous, non-repeating event simulator.")
    p.add_argument("--rate", type=float, default=20.0, help="transactions per second (0 = flat out)")
    p.add_argument("--for", dest="until", default=None, help="stop after e.g. 30s, 10m, 2h")
    p.add_argument("--db", default="sim.db", help="sqlite file when --dsn is absent")
    p.add_argument("--dsn", default=None, help="postgresql://... to write there instead")
    p.add_argument("--dispute-rate", type=float, default=0.06, help="share of txns disputed")
    p.add_argument("--resolve-rate", type=float, default=0.05, help="chance per tick of closing one")
    p.add_argument("--batch", type=int, default=200, help="rows per commit")
    p.add_argument("--report", type=float, default=5.0, help="seconds between stat lines")
    return run(p.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
