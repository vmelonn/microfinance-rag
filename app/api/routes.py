"""
A test harness with a UI. Exposes the whole decision, not just the answer.

WHAT THIS IS FOR. Every flow through this system makes a routing decision before
it does anything useful, and four of the six never reach a model. A UI that only
showed the final answer would hide exactly the part worth checking: which flow
was taken, why, what was eligible, and what got excluded.

So the response carries the trace. Intent, tier, the flow letter, each retrieval
pool separately, the SQL statement when there is one, and the prompt that would
be sent. The answer, when there is one, is the last thing rather than the only
thing.

    uvicorn app.api.routes:app --reload --port 8086

Port 8086 because the platform occupies 8080 to 8085 and 8090.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.answer import lookup as lookup_mod  # noqa: E402
from app.answer import preflight as pre_mod  # noqa: E402
from app.answer import prompt as prompt_mod  # noqa: E402
from app.answer import sql_tool  # noqa: E402
from app.answer.citations import verify  # noqa: E402
from app.answer.llm import call  # noqa: E402
from app.answer.router import Router  # noqa: E402
from app.retrieve.hybrid import Hybrid, load_encoder  # noqa: E402
from app.retrieve.store import today  # noqa: E402

INDEX = os.environ.get("RAG_INDEX", "index.db")
DATA = os.environ.get("RAG_DATA", "sim.db")
LEDGER = os.environ.get("RAG_LEDGER", "../microfinance-microservices/practice.db")
EMBED = os.environ.get("RAG_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
DEVICE = os.environ.get("RAG_DEVICE", "auto")

app = FastAPI(title="microfinance-rag test harness")
_state: dict = {}


def _router() -> Router:
    if "router" not in _state:
        _state["encoder"] = load_encoder(EMBED, DEVICE)
        _state["router"] = Router(Hybrid(INDEX, encoder=_state["encoder"]))
    return _state["router"]


def _encoder():
    _router()                       # loads it on first use
    return _state["encoder"]


class Ask(BaseModel):
    question: str
    anomaly_code: str | None = None
    rrn: str | None = None
    generate: bool = False
    backend: str = "ollama"


def _hits(hits) -> list[dict]:
    return [{"uri": h.source_uri, "section": h.section_path,
             "coverage": round(h.coverage, 2),
             "text": " ".join(h.text.split())[:300]} for h in hits]


# Which of the six flows was taken. The letters match the diagram in the README
# so a screenshot and the docs describe the same thing.
def _flow(routed) -> tuple[str, str]:
    if routed.intent == "numeric":
        return "A", "counted by SQL, never estimated"
    if routed.intent == "lookup":
        return "B", "fetched from the system of record"
    if routed.tier == "exact":
        return "C", "defect class known, procedure and precedent by lookup"
    if routed.tier == "none":
        return "F", "nothing cleared the floor"
    if routed.procedure:
        return "D", "novel defect, reasoned from analogous procedures"
    return "E", "novel defect, only precedent and reference cleared the floor"


def _plain(rows, columns) -> str:
    """
    Word a query result without a model.

    GUARDRAILS says a model may word an exact value but never produce one. The
    safest way to honour that is not to involve one: a count is a format string,
    and a model asked to phrase it is a chance for the number to change on the
    way out for no benefit.
    """
    if not rows:
        return "The query returned no rows."
    if len(rows) == 1 and len(rows[0]) == 1:
        return "%s: %s" % (columns[0].replace("_", " "), "{:,}".format(rows[0][0])
                           if isinstance(rows[0][0], int) else rows[0][0])
    lines = []
    for r in rows[:8]:
        lines.append("  " + " · ".join(
            "%s %s" % (c, "{:,}".format(v) if isinstance(v, int) else v)
            for c, v in zip(columns, r)))
    more = "" if len(rows) <= 8 else "\n... and %d more rows" % (len(rows) - 8)
    return "\n".join(lines) + more


def _found(routed) -> dict:
    """Which pools came back empty. 'Nothing anywhere' is itself an answer."""
    return {
        "procedure": len(routed.procedure),
        "precedent": len(routed.precedent),
        "reference": len(routed.reference),
        "nothing_anywhere": not (routed.procedure or routed.precedent
                                 or routed.reference),
    }


@app.get("/")
def index_page():
    return FileResponse(str(Path(__file__).parent / "static" / "index.html"))


@app.get("/healthz")
def healthz():
    return {"ok": True, "index": INDEX, "data": DATA, "ledger": LEDGER}


@app.get("/codes")
def codes():
    from sim.catalogue import DOCUMENTED, HELD_OUT
    return {"documented": [a.code for a in DOCUMENTED],
            "held_out": [a.code for a in HELD_OUT]}


@app.post("/ask")
def ask(req: Ask):
    routed = _router().route(req.question, as_of=today(),
                             anomaly_code=req.anomaly_code or None)
    flow, why = _flow(routed)

    out = {
        "flow": flow, "why": why,
        "intent": routed.intent, "tier": routed.tier, "note": routed.note,
        "procedure": _hits(routed.procedure),
        "precedent": _hits(routed.precedent),
        "reference": _hits(routed.reference),
        "reached_model": False,
        "found": _found(routed),
    }

    if flow == "A":
        r = sql_tool.answer(DATA, req.question,
                            {"rrn": req.rrn} if req.rrn else {},
                            ledger_path=LEDGER if not LEDGER.startswith("http") else None,
                            encoder=_encoder())
        out["sql"] = {"name": r.query.name if r.query else None,
                      "statement": r.sql, "columns": r.columns,
                      "rows": [list(x) for x in r.rows[:25]],
                      "error": r.error, "ms": round(r.elapsed_ms, 1)}
        out["answer"] = {
            "backend": "sql", "model": r.query.name if r.query else "none",
            "verified": not r.error,
            "text": ("" if r.error else
                     "%s\n\n%s" % (r.query.question,
                                   _plain(r.rows, r.columns))),
            "withheld": r.error or None,
            "citations": [], "problems": [r.error] if r.error else [],
        }
        return out

    if flow == "B":
        backend = (lookup_mod.LedgerService(LEDGER) if LEDGER.startswith("http")
                   else lookup_mod.LocalLedger(LEDGER))
        v = lookup_mod.answer(backend, req.question)
        out["lookup"] = {"kind": v.kind, "subject": v.subject, "value": v.value,
                         "unit": v.unit, "as_at": v.as_at, "source": v.source,
                         "detail": v.detail, "error": v.error}
        out["answer"] = {
            "backend": "system of record", "model": v.source or "none",
            "verified": not v.error,
            "text": "" if v.error else v.render(),
            "withheld": v.error or None,
            "citations": [], "problems": [v.error] if v.error else [],
        }
        return out

    if flow == "F":
        f = out["found"]
        detail = ("Nothing was retrieved from any pool."
                  if f["nothing_anywhere"] else
                  "Retrieval returned %d procedure, %d precedent and %d reference "
                  "chunks, and none cleared the relevance floor."
                  % (f["procedure"], f["precedent"], f["reference"]))
        out["answer"] = {
            "backend": "none", "model": "none", "verified": True,
            "text": "No answer.\n\n%s\n\n%s" % (detail, routed.note),
            "withheld": None, "citations": [], "problems": [],
        }
        return out

    # Run the read-only checks the defect class implies, before generating.
    # Escalating to a person without first checking what we could have checked
    # is the least useful kind of escalation.
    pf = pre_mod.run(req.question, anomaly_code=req.anomaly_code or None,
                     data_db=DATA,
                     ledger_db=None if LEDGER.startswith("http") else LEDGER,
                     rrn=req.rrn)
    out["checks"] = [{"label": c.label, "ran": c.ran, "summary": c.summary,
                      "statement": c.statement, "needs": c.needs,
                      "columns": c.columns, "rows": c.rows[:8]}
                     for c in pf.checks]
    out["clarify"] = pf.question_to_ask()

    facts = dict(pf.facts())
    if req.rrn:
        facts["RRN"] = req.rrn
    kwargs = prompt_mod.build(routed, break_facts=facts or None)
    out["prompt"] = prompt_mod.render(kwargs)

    if not req.generate:
        return out

    try:
        response = call(kwargs, backend=req.backend)
    except Exception as exc:            # noqa: BLE001
        out["generation_error"] = str(exc)
        return out

    v = verify(response, kwargs, require_citation=routed.tier in ("exact", "derived"))
    out["reached_model"] = True
    out["answer"] = {
        "backend": response.backend, "model": response.model,
        "verified": v.ok,
        # Withheld, not footnoted. A citation that does not resolve is evidence
        # the answer is not grounded.
        "text": v.text if v.ok else "",
        "withheld": None if v.ok else v.text,
        "citations": v.citations, "problems": v.problems,
    }
    return out
