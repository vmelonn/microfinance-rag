"""
Flow B: exact-value lookups against the system of record.

DIFFERENT FROM THE SQL REGISTRY, THOUGH IT LOOKS SIMILAR. The SQL path answers
"how many" over a warehouse. This answers "what is" about one entity, and the
value it returns is authoritative rather than analytical. That difference drives
two rules the SQL path does not need:

  the value is never computed by a model, only worded by one
  the value carries the moment it was read, because a balance is true at a time

TWO BACKENDS, ONE CONTRACT. In the cluster this is an HTTP call to
ledger-service on 8084, which owns accounts and postings. Locally it reads the
practice database directly. Both are read-only, both return the same shape, and
the local one exists so the path can be exercised while the platform is idled,
which it currently is.

BALANCE IS DERIVED, NOT STORED. There is no balance column, and there should not
be: the ledger is double-entry, so a balance is the signed sum of an account's
postings. Reading a stored figure would be reading a cache that can disagree
with the entries beneath it, and the entries are the truth.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

TIMEOUT_S = 5.0


@dataclass
class Value:
    """One authoritative fact, with its provenance attached."""
    kind: str                        # balance | account | card_status
    subject: str                     # what was asked about
    value: object = None
    unit: str = ""
    as_at: str = ""                  # a balance is true at a time, not in general
    source: str = ""                 # which system answered
    detail: dict = field(default_factory=dict)
    error: str = ""

    def render(self) -> str:
        if self.error:
            return "NO VALUE: %s" % self.error
        head = "%s for %s" % (self.kind, self.subject)
        val = ("PKR {:,.2f}".format(self.value / 100)
               if self.unit == "minor" else str(self.value))
        lines = [head, "-" * 60, "  %s" % val,
                 "  as at %s, from %s" % (self.as_at, self.source)]
        for k, v in self.detail.items():
            lines.append("  %-16s %s" % (k, v))
        return "\n".join(lines)

    def as_facts(self) -> dict:
        """The shape the prompt takes as established facts."""
        return {self.kind: self.render().splitlines()[2].strip(),
                "as at": self.as_at, "source": self.source}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- local

class LocalLedger:
    """
    Reads the practice database. Same contract as the HTTP backend, so the
    lookup path can be exercised with the platform idled.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.source = "practice.db (local)"

    def _conn(self):
        c = sqlite3.connect("file:%s?mode=ro" % self.db_path, uri=True)
        c.execute("PRAGMA query_only = ON")
        c.row_factory = sqlite3.Row
        return c

    def balance(self, subject: str) -> Value:
        conn = self._conn()
        try:
            acct = conn.execute(
                """SELECT account_id, msisdn, type FROM accounts
                   WHERE account_id = :s OR msisdn = :s""",
                {"s": subject}).fetchone()
            if acct is None:
                return Value(kind="balance", subject=subject,
                             error="no account matches %r" % subject)

            # Derived from the postings, never from a stored figure.
            row = conn.execute(
                """SELECT
                     sum(CASE WHEN entry_type = 'credit' THEN amount_cents
                              ELSE -amount_cents END) AS bal,
                     count(*) AS entries
                   FROM ledger_entries WHERE account_id = ?""",
                (acct["account_id"],)).fetchone()
        finally:
            conn.close()

        return Value(kind="balance", subject=subject,
                     value=row["bal"] or 0, unit="minor", as_at=_now(),
                     source=self.source,
                     detail={"account_id": acct["account_id"],
                             "type": acct["type"],
                             "postings summed": row["entries"] or 0})

    def card_status(self, subject: str) -> Value:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT card_number, status, account_id FROM cards WHERE card_number = ?",
                (subject,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return Value(kind="card_status", subject=subject,
                         error="no card matches %r" % subject)
        return Value(kind="card_status", subject=subject, value=row["status"],
                     as_at=_now(), source=self.source,
                     detail={"account_id": row["account_id"]})


# ----------------------------------------------------------------- http

class LedgerService:
    """The production backend: ledger-service owns accounts and postings."""

    def __init__(self, base_url: str, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.source = base_url

    def _get(self, path: str) -> dict:
        req = urllib.request.Request(self.base_url + path)
        if self.token:
            req.add_header("Authorization", "Bearer %s" % self.token)
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8"))

    def balance(self, subject: str) -> Value:
        try:
            d = self._get("/internal/accounts/%s/balance" % subject)
        except urllib.error.URLError as exc:
            return Value(kind="balance", subject=subject,
                         error="ledger-service unreachable: %s" % exc)
        return Value(kind="balance", subject=subject,
                     value=d.get("balance_cents"), unit="minor",
                     as_at=d.get("as_at") or _now(), source=self.source,
                     detail={"account_id": d.get("account_id")})

    def card_status(self, subject: str) -> Value:
        try:
            d = self._get("/internal/cards/%s" % subject)
        except urllib.error.URLError as exc:
            return Value(kind="card_status", subject=subject,
                         error="ledger-service unreachable: %s" % exc)
        return Value(kind="card_status", subject=subject,
                     value=d.get("status"), as_at=_now(), source=self.source)


# --------------------------------------------------------------- routing

import re  # noqa: E402

MSISDN = re.compile(r"\b(03\d{9})\b")
ACCOUNT = re.compile(r"\b(acc_[a-z]+_\d+)\b")
CARD = re.compile(r"\b(\d{12,19})\b")


def subject_of(question: str) -> str | None:
    """The entity being asked about, taken from the question verbatim."""
    for pat in (MSISDN, ACCOUNT, CARD):
        m = pat.search(question)
        if m:
            return m.group(1)
    return None


def answer(backend, question: str) -> Value:
    subject = subject_of(question)
    if subject is None:
        return Value(kind="lookup", subject="?",
                     error=("no account, MSISDN or card number in the question. "
                            "An exact-value lookup needs an identifier; it is "
                            "not something to infer."))
    q = question.lower()
    if "card" in q or "block" in q:
        return backend.card_status(subject)
    return backend.balance(subject)
