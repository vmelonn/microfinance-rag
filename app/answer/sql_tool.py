"""
The SQL path. Numeric questions are counted, never estimated.

A REGISTRY, NOT TEXT-TO-SQL. The obvious build is to hand a model the schema and
let it write the query. This does the opposite: every query is written once,
reviewed once, named, and parameterised. The model's job is to pick one and fill
its parameters, which is classification rather than generation.

That trade is deliberate. Free-form SQL from a model is unbounded in what it can
express and therefore unbounded in how it can be wrong, and "wrong" here means a
number an operator will act on. A registry cannot invent a join, cannot read a
column nobody meant to expose, and cannot be talked into anything by text inside
a retrieved document. What it costs is coverage: a question nobody anticipated
gets "no query covers this" rather than an improvised answer, which is the same
bargain the refusal tier makes and for the same reason.

FOUR FENCES, none of which trust the caller:

  read-only connection    opened with mode=ro, so a write fails at the driver
  single statement        a semicolon in a parameter cannot start a second one
  SELECT only             enforced on the registered SQL, checked at import
  bounded rows            LIMIT applied by the executor, not by the query text

And one rule that is not a fence but matters as much: **the statement is
returned with the result and shown to the operator.** A wrong query someone can
read is a bug. A wrong number with no visible derivation is a liability.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field

MAX_ROWS = 200
TIMEOUT_S = 5.0


@dataclass(frozen=True)
class Query:
    name: str
    question: str                    # what it answers, in an operator's words
    sql: str
    params: tuple[str, ...] = ()     # named placeholders this query needs
    triggers: tuple[str, ...] = ()   # words that suggest this query


REGISTRY: list[Query] = [

    Query(
        name="open_disputes",
        question="How many disputes are still open?",
        sql="""SELECT count(*) AS open_disputes
               FROM sim_disputes
               WHERE status = 'open' AND dispute_id NOT LIKE '%#r'""",
        triggers=("open", "outstanding", "unresolved", "still open", "pending"),
    ),

    Query(
        name="disputes_by_outcome",
        question="How did disputes close, by outcome?",
        sql="""SELECT status, count(*) AS n
               FROM sim_disputes
               WHERE dispute_id LIKE '%#r'
               GROUP BY status ORDER BY n DESC""",
        triggers=("outcome", "upheld", "rejected", "closed", "how did", "resolution"),
    ),

    Query(
        name="disputes_by_reason",
        question="What are disputes being raised about?",
        sql="""SELECT reason_code, count(*) AS n
               FROM sim_disputes
               WHERE dispute_id NOT LIKE '%#r'
               GROUP BY reason_code ORDER BY n DESC""",
        triggers=("reason", "about", "cause", "why", "categories", "kinds"),
    ),

    Query(
        name="failed_by_response_code",
        question="How many transactions failed, by response code?",
        sql="""SELECT resp_code, count(*) AS n
               FROM sim_transactions
               WHERE resp_code <> '00'
               GROUP BY resp_code ORDER BY n DESC""",
        triggers=("response code", "resp", "failed", "decline", "declined", "error code"),
    ),

    Query(
        name="volume_by_channel",
        question="Where is volume coming from, by channel?",
        sql="""SELECT channel, count(*) AS n, sum(amount_minor) / 100 AS total_pkr
               FROM sim_transactions
               GROUP BY channel ORDER BY n DESC""",
        triggers=("channel", "agent", "atm", "pos", "ussd", "volume", "where"),
    ),

    Query(
        name="count_by_response_code",
        question="How many transactions carried a specific response code?",
        sql="""SELECT resp_code, count(*) AS n
               FROM sim_transactions
               WHERE resp_code = :code
               GROUP BY resp_code""",
        params=("code",),
        triggers=("code 51", "code 05", "specific code", "response code"),
    ),

    Query(
        name="disputes_for_rrn",
        question="Is there a dispute against this transaction?",
        sql="""SELECT dispute_id, reason_code, status, opened_at, resolved_at
               FROM sim_disputes WHERE rrn = :rrn ORDER BY opened_at""",
        params=("rrn",),
        triggers=("this rrn", "this transaction", "against"),
    ),

    Query(
        name="resolution_rate",
        question="What share of disputes have been resolved?",
        sql="""SELECT
                 (SELECT count(*) FROM sim_disputes WHERE dispute_id LIKE '%#r')
                   AS resolved,
                 (SELECT count(*) FROM sim_disputes WHERE dispute_id NOT LIKE '%#r')
                   AS raised""",
        triggers=("rate", "share", "proportion", "percentage", "resolved"),
    ),
]

BY_NAME = {q.name: q for q in REGISTRY}

_SELECT_ONLY = re.compile(r"^\s*(SELECT|WITH)\b", re.I)
_PLACEHOLDER = re.compile(r":([a-z_]+)")


def _validate_registry() -> None:
    """Checked at import, so a bad query cannot reach production quietly."""
    for q in REGISTRY:
        if not _SELECT_ONLY.match(q.sql):
            raise ValueError("%s: not a SELECT" % q.name)
        if ";" in q.sql:
            raise ValueError("%s: contains a semicolon" % q.name)
        declared = set(q.params)
        used = set(_PLACEHOLDER.findall(q.sql))
        if declared != used:
            raise ValueError("%s: params %s do not match placeholders %s"
                             % (q.name, sorted(declared), sorted(used)))


_validate_registry()


@dataclass
class Result:
    query: Query | None
    sql: str = ""
    params: dict = field(default_factory=dict)
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False
    elapsed_ms: float = 0.0
    error: str = ""

    def render(self) -> str:
        if self.error:
            return "NO RESULT: %s" % self.error
        out = ["QUERY  %s" % self.query.name,
               "-" * 70,
               self.sql.strip(),
               "-" * 70]
        if self.params:
            out.append("params: %s" % self.params)
        out.append("  ".join(self.columns))
        for r in self.rows[:25]:
            out.append("  ".join(str(v) for v in r))
        if self.truncated:
            out.append("... truncated at %d rows" % MAX_ROWS)
        out.append("%d row(s) in %.1f ms" % (len(self.rows), self.elapsed_ms))
        return "\n".join(out)


def pick(question: str) -> Query | None:
    """
    Choose a registered query by trigger overlap.

    Deliberately dumb. A model would choose better, and the intended production
    path is to give it the registry as a tool schema so it selects a name and
    fills parameters. Keeping a deterministic fallback means the SQL path works
    with no model at all, and means the model's choice can be diffed against a
    baseline rather than trusted.
    """
    q_low = question.lower()
    best, best_score = None, 0
    for q in REGISTRY:
        score = sum(1 for t in q.triggers if t in q_low)
        if score > best_score:
            best, best_score = q, score
    return best


def run(db_path: str, query: Query, params: dict | None = None) -> Result:
    params = params or {}

    missing = set(query.params) - set(params)
    if missing:
        return Result(query=query, error="missing parameter(s): %s"
                      % ", ".join(sorted(missing)))

    # Read-only at the driver. A write is refused by SQLite itself rather than
    # by a convention someone can refactor away.
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    conn.execute("PRAGMA query_only = ON")

    # A wall-clock ceiling, so a pathological scan cannot hold the request open.
    deadline = time.monotonic() + TIMEOUT_S
    conn.set_progress_handler(
        lambda: 1 if time.monotonic() > deadline else 0, 10_000)

    started = time.monotonic()
    try:
        cur = conn.execute(query.sql, {k: params[k] for k in query.params})
        rows = cur.fetchmany(MAX_ROWS + 1)
        cols = [d[0] for d in cur.description]
    except sqlite3.OperationalError as exc:
        return Result(query=query, sql=query.sql, params=params,
                      error="query stopped: %s" % exc)
    finally:
        conn.close()

    elapsed = (time.monotonic() - started) * 1000
    truncated = len(rows) > MAX_ROWS
    return Result(query=query, sql=query.sql, params=params, columns=cols,
                  rows=rows[:MAX_ROWS], truncated=truncated, elapsed_ms=elapsed)


def answer(db_path: str, question: str, params: dict | None = None) -> Result:
    q = pick(question)
    if q is None:
        return Result(query=None,
                      error=("no registered query covers this. The registry is "
                             "deliberately closed: an unanticipated question gets "
                             "nothing rather than an improvised number."))
    return run(db_path, q, params)
