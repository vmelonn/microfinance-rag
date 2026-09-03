"""
End to end: a question in, a verified answer out.

    python -m app.answer.ask --index index.db --dry-run \
        --question "the customer was charged twice" --code DUPLICATE_POSTING

    python -m app.answer.ask --index index.db \
        --question "the same transaction was posted three times"

--dry-run prints the exact request without sending it, which is how the prompt
and the guardrails get reviewed without an API key and without spending
anything. It is also the fastest way to see that retrieved text really is inside
a document block rather than concatenated into the instructions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.answer import prompt as prompt_mod  # noqa: E402
from app.answer.citations import verify  # noqa: E402
from app.answer.llm import call  # noqa: E402
from app.answer import lookup as lookup_mod  # noqa: E402
from app.answer import sql_tool  # noqa: E402
from app.answer.router import Router  # noqa: E402
from app.retrieve.hybrid import Hybrid, load_encoder  # noqa: E402
from app.retrieve.store import today  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--index", default="index.db")
    p.add_argument("--question", required=True)
    p.add_argument("--code", default=None,
                   help="anomaly code from the recon engine, when known")
    p.add_argument("--model", default="BAAI/bge-small-en-v1.5")
    p.add_argument("--device", default="auto")
    p.add_argument("--mode", default="hybrid")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--backend", default="ollama",
                   choices=["ollama", "anthropic"],
                   help="ollama keeps everything on this machine")
    p.add_argument("--local-model", default="qwen2.5:7b-instruct")
    p.add_argument("--rrn", default=None, help="a fact to pass through")
    p.add_argument("--data", default=None,
                   help="sim database, for the numeric path")
    p.add_argument("--ledger", default=None,
                   help="practice.db, or an http:// ledger-service url")
    a = p.parse_args()

    encoder = load_encoder(a.model, a.device) if a.mode != "keyword" else None
    router = Router(Hybrid(a.index, encoder=encoder), mode=a.mode)
    routed = router.route(a.question, as_of=today(), anomaly_code=a.code)

    print("intent=%s  tier=%s" % (routed.intent, routed.tier.upper()))
    print("procedure=%d  precedent=%d  reference=%d"
          % (len(routed.procedure), len(routed.precedent), len(routed.reference)))
    print()

    # Three cases never reach the model at all, and that is the guardrail
    # working rather than a failure to answer. Numeric and lookup questions have
    # exact answers that a query produces. Tier NONE means nothing retrieved is
    # close enough, and while the model could be asked to phrase that refusal
    # nicely, sending it is strictly worse: it costs a call and creates one more
    # opportunity to answer from general knowledge instead of from the corpus.
    if routed.intent == "numeric":
        # Counted, not estimated, and the statement is shown. A wrong query an
        # operator can read is a bug; a wrong number with no visible derivation
        # is a liability.
        print("NOT SENT TO THE MODEL")
        print(routed.note)
        print()
        if not a.data:
            print("pass --data to run the query")
            return 0
        params = {"rrn": a.rrn} if a.rrn else {}
        print(sql_tool.answer(a.data, a.question, params,
                              ledger_path=a.ledger).render())
        return 0

    if routed.intent == "lookup":
        # The value comes from the system of record. A model may word it; it may
        # not produce it. The reading carries its own timestamp, because a
        # balance is true at a time rather than in general.
        print("NOT SENT TO THE MODEL")
        print(routed.note)
        print()
        if not a.ledger:
            print("pass --ledger to resolve the value")
            return 0
        backend = (lookup_mod.LedgerService(a.ledger)
                   if a.ledger.startswith("http")
                   else lookup_mod.LocalLedger(a.ledger))
        print(lookup_mod.answer(backend, a.question).render())
        return 0

    if routed.tier == "none":
        print("NOT SENT TO THE MODEL")
        print(routed.note)
        return 0

    facts = {"RRN": a.rrn} if a.rrn else None
    kwargs = prompt_mod.build(routed, break_facts=facts)

    if a.dry_run:
        print(prompt_mod.render(kwargs))
        print()
        print("dry run: nothing was sent")
        return 0

    response = call(kwargs, backend=a.backend, local_model=a.local_model)
    print("backend=%s model=%s" % (response.backend, response.model))

    v = verify(response, kwargs,
               require_citation=(routed.tier in ("exact", "derived")))

    print("-" * 74)
    if v.ok:
        print(v.text)
    else:
        # Withheld, not footnoted. A citation that does not resolve is evidence
        # the answer is not grounded, and showing it anyway teaches an operator
        # to trust the next one.
        print("ANSWER WITHHELD. The draft did not pass verification.")
    print("-" * 74)
    print(v.render())
    return 0 if v.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
