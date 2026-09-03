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

    # Which database answers it. The registry spans two: the simulator stream
    # (transactions, disputes, narratives) and the platform's own tables
    # (users, accounts, agents, branches, cards).
    #
    # Leaving this out was a real gap rather than a small one. Every question
    # about an entity was unanswerable, because no query could reach the
    # database the entities live in, and "how many users do we have" is about
    # as ordinary as an operational question gets.
    source: str = "data"             # data | ledger


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
    # ------------------------------------------------------------- ledger
    # The platform's own tables rather than the simulator stream.

    Query(
        name="count_users",
        question="How many customers are on the platform?",
        sql="SELECT count(*) AS users FROM users",
        source="ledger",
        triggers=("users", "customers", "how many users", "customer count"),
    ),

    Query(
        name="count_accounts",
        question="How many accounts exist, by type?",
        sql="""SELECT type, count(*) AS n FROM accounts
               GROUP BY type ORDER BY n DESC""",
        source="ledger",
        triggers=("accounts", "wallets", "account type"),
    ),

    Query(
        name="count_agents",
        question="How many agents are there, and how many are active?",
        sql="""SELECT status, count(*) AS n FROM agents
               GROUP BY status ORDER BY n DESC""",
        source="ledger",
        triggers=("agents", "active agents"),
    ),

    Query(
        name="count_cards",
        question="How many cards are issued, by status?",
        sql="""SELECT status, count(*) AS n FROM cards
               GROUP BY status ORDER BY n DESC""",
        source="ledger",
        triggers=("cards", "blocked cards", "issued"),
    ),

    Query(
        name="users_by_branch",
        question="How are customers distributed across branches?",
        sql="""SELECT b.name AS branch, count(u.user_id) AS n
               FROM branches b LEFT JOIN users u ON u.branch_id = b.branch_id
               GROUP BY b.name ORDER BY n DESC""",
        source="ledger",
        triggers=("branch", "branches", "distributed", "region"),
    ),

    Query(
        name="users_by_tier",
        question="How many customers are in each KYC tier?",
        sql="""SELECT k.name AS tier, count(u.user_id) AS n
               FROM kyc_tiers k LEFT JOIN users u ON u.tier_id = k.tier_id
               GROUP BY k.name ORDER BY n DESC""",
        source="ledger",
        triggers=("tier", "kyc", "verification level"),
    ),

    Query(
        name="ledger_totals",
        question="What is the total value posted to the ledger?",
        sql="""SELECT entry_type, count(*) AS entries,
                      sum(amount_cents) / 100 AS total_pkr
               FROM ledger_entries GROUP BY entry_type""",
        source="ledger",
        triggers=("posted", "ledger total", "total value", "postings"),
    ),
]

BY_NAME = {q.name: q for q in REGISTRY}

_SELECT_ONLY = re.compile(r"^\s*(SELECT|WITH)\b", re.I)
_PLACEHOLDER = re.compile(r":([a-z_]+)")
_WORD = re.compile(r"[a-z0-9]+")

# Filler that every question shares, so it proves nothing about topic.
_STOP = frozenset("""
a an the this that of in on at to from by for with is are was were be
do does did have has had how many much what which who when where why
we our us i you they it there any all some each per and or but not no
give me show tell get list count total number
""".split())


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
    # Whole words, not substrings. Matching on substrings made "how many
    # swallows migrate each year" select the resolution-rate query, because
    # "migrate" contains "rate". A closed registry only protects you if the
    # selector cannot be fooled: a wrong query chosen confidently is worse than
    # the refusal it replaced, and it is the exact failure this design exists to
    # avoid.
    q_words = set(_WORD.findall(question.lower()))

    best, best_score = None, 0
    for q in REGISTRY:
        score = 0
        for t in q.triggers:
            parts = _WORD.findall(t.lower())
            if parts and all(p in q_words for p in parts):
                score += 1
        if score > best_score:
            best, best_score = q, score
    return best


# --------------------------------------------------------------------------
# Semantic selection.
#
# Trigger words do not scale. Every unlisted phrasing of a question that the
# registry can perfectly well answer gets refused, and the fix is always
# somebody remembering to add another keyword. That is a maintenance treadmill
# and it is always behind.
#
# The registry is a closed set of fifteen short questions, and an embedding
# model is already loaded for retrieval. So embed each query's own question
# once, embed the user's, and pick by similarity. The safety property is
# untouched: the set of runnable SQL does not grow, only the ability to
# recognise a paraphrase of something already in it.
#
# The floor still matters. Cosine similarity always returns a best match, so
# without a floor "how many unicorns do we have" selects whichever query is
# least unlike it and answers confidently. The floor is what keeps a refusal
# possible, and it is set from measurement rather than taste.
# --------------------------------------------------------------------------

SIMILARITY_FLOOR = 0.55

_vec_cache: dict = {}


def pick_semantic(question: str, encoder, floor: float = SIMILARITY_FLOOR):
    """Nearest registered question by meaning, or None below the floor."""
    import numpy as np

    key = id(encoder)
    if key not in _vec_cache:
        mat = encoder.encode([q.question for q in REGISTRY],
                             normalize_embeddings=True)
        _vec_cache[key] = np.asarray(mat, dtype="float32")
    mat = _vec_cache[key]

    qv = np.asarray(encoder.encode([question], normalize_embeddings=True)[0],
                    dtype="float32")
    sims = mat @ qv
    i = int(sims.argmax())
    return (REGISTRY[i], float(sims[i])) if sims[i] >= floor else (None, float(sims[i]))


SELECTOR_PROMPT = """\
You match an operator's question to one query from a fixed list. You never write
SQL and you never invent a name.

Reply with exactly one line: either the name of the single best query, or NONE.
No explanation, no punctuation, nothing else.

Answer NONE when the question is about something the list does not cover. NONE is
the right answer often; a wrong query returns a real number about the wrong thing,
which is worse than returning nothing.

QUERIES:
%s

QUESTION: %s
"""


def pick_llm(question: str, *, backend: str = "ollama",
             model: str = "qwen2.5:7b-instruct"):
    """
    A model chooses from the closed list. It cannot write SQL, only name a query.

    That containment is what makes this acceptable at all. The model is picking
    from fifteen reviewed statements, so the worst outcome is a real number about
    the wrong thing, shown alongside the statement that produced it. Compare that
    to text-to-SQL, where the worst outcome is unbounded.

    Its answer is still validated against the registry, because a model asked for
    one of fifteen names will occasionally return a sixteenth.
    """
    from app.answer.llm import call_ollama

    listing = "\n".join("  %s: %s" % (q.name, q.question) for q in REGISTRY)
    kwargs = {
        "model": model,
        "max_tokens": 24,
        "system": "You are a strict classifier. One line, one name, or NONE.",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": SELECTOR_PROMPT % (listing, question)}]}],
    }

    reply = call_ollama(kwargs, model=model, cite=False)
    raw = reply.content[0].text.strip().splitlines()[0].strip() if reply.content else ""
    name = raw.split()[0].strip().strip('".:,').lower() if raw else ""

    # Validated, not trusted. A name that is not in the registry is a refusal,
    # not an error to paper over.
    return BY_NAME.get(name)


def _vocab(q: Query) -> set[str]:
    """Every content word this query is about."""
    words = set(_WORD.findall(q.question.lower()))
    for t in q.triggers:
        words |= set(_WORD.findall(t.lower()))
    return words - _STOP


def pick_best(question: str, encoder=None):
    """
    Semantic for recall, lexical for precision. The same lesson as retrieval,
    arrived at the same way.

    Measured on ten questions, each selector scored 7 of 10 and failed on
    different ones. Keyword triggers cannot recognise a paraphrase: "what is our
    total customer base" and "how many people are signed up" both got nothing,
    though the registry answers them. Semantics cannot refuse nonsense: "how many
    unicorns do we have" scored 0.67 against "How many accounts exist", which is
    a perfectly reasonable similarity, because cosine always returns a nearest
    neighbour and never an absence.

    So the embedding proposes and the vocabulary disposes. A candidate must also
    share at least one content word with what that query is about. A paraphrase
    does, because it is about the same thing. Nonsense does not, because
    "unicorns" and "swallows" appear nowhere in the registry.

    Neither half is sufficient and neither is redundant, which is exactly the
    hybrid-retrieval result over again.
    """
    if encoder is None:
        return pick(question)

    cand, score = pick_semantic(question, encoder)
    if cand is None:
        return None

    asked = set(_WORD.findall(question.lower())) - _STOP
    if asked & _vocab(cand):
        return cand

    # Semantically close, lexically unrelated. Fall back to the keyword
    # selector, which refuses rather than guessing.
    return pick(question)


def run(db_path: str, query: Query, params: dict | None = None,
        ledger_path: str | None = None) -> Result:
    params = params or {}

    missing = set(query.params) - set(params)
    if missing:
        return Result(query=query, error="missing parameter(s): %s"
                      % ", ".join(sorted(missing)))

    # Read-only at the driver. A write is refused by SQLite itself rather than
    # by a convention someone can refactor away.
    path = ledger_path if query.source == "ledger" else db_path
    if path is None:
        return Result(query=query, sql=query.sql,
                      error="this query reads the platform database, which was "
                            "not supplied")
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
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


def catalogue() -> str:
    """Every question the registry can answer, grouped by database."""
    out = []
    for src, label in (("data", "the transaction stream"),
                       ("ledger", "the platform database")):
        qs = [q for q in REGISTRY if q.source == src]
        if not qs:
            continue
        out.append("From %s:" % label)
        out.extend("  - %s" % q.question for q in qs)
    return "\n".join(out)


def answer(db_path: str, question: str, params: dict | None = None,
           ledger_path: str | None = None) -> Result:
    q = pick(question)
    if q is None:
        # Refusing is correct. Refusing without saying what *is* available is
        # merely unhelpful, and it makes a closed registry feel broken rather
        # than deliberate. Listing the catalogue costs nothing and turns a dead
        # end into a menu.
        return Result(query=None,
                      error=("No registered query covers this. The registry is "
                             "closed on purpose: an unanticipated question gets "
                             "nothing rather than an improvised number.\n\n"
                             + catalogue()))
    return run(db_path, q, params, ledger_path)
