"""
The evaluation harness. Produces the baseline number.

    python eval/run_eval.py --index index.db

Questions are built from the catalogue rather than written by hand, so the
correct answer is known by construction and cannot drift from the corpus.

FOUR THINGS ARE MEASURED SEPARATELY, because they have different fixes:

  documented   an SOP exists. Is it in the top k?
                 -> a plain retrieval score

  held_out     no SOP exists. Are the analogous SOPs in the top k?
                 -> whether a derived answer would have the right material
                    to derive from. Retrieval cannot be blamed for a bad
                    derivation if the analogues never surfaced.

  supersession the current circular must rank, and the superseded one must be
               absent entirely
                 -> a filter test, not a ranking test. Anything above zero on
                    "leaked" is a compliance failure, not a quality dip.

  refusal      nothing in the corpus answers it
                 -> the floor should suppress it. A system that never refuses
                    scores well everywhere else and cannot be trusted.

Conflating these is how a RAG project convinces itself it is working.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.retrieve.hybrid import Hybrid, load_encoder  # noqa: E402
from app.retrieve.store import today  # noqa: E402
from sim.catalogue import DOCUMENTED, HELD_OUT  # noqa: E402

REFUSAL_QUESTIONS = [
    "what is the capital of France",
    "how do I reset my email password",
    "what is our policy on annual leave",
    "who won the cricket match yesterday",
    "how do I configure the office printer",
]

FEE_PRODUCTS = ["wallet transfer", "agent cash out", "bill payment",
                "merchant POS", "ATM withdrawal"]


def sop_uri(code: str) -> str:
    return "sop/SOP-%s.md" % code


def hit_docs(hits) -> list[str]:
    seen, out = set(), []
    for h in hits:
        if h.source_uri not in seen:
            seen.add(h.source_uri)
            out.append(h.source_uri)
    return out


def run(index: str, k: int, floor: float, verbose: bool,
        mode: str = "keyword", model: str | None = None,
        device: str = "auto") -> int:
    encoder = None
    if mode in ("vector", "hybrid"):
        if not model:
            print("--mode %s needs --model" % mode); return 2
        encoder = load_encoder(model, device)

    store = Hybrid(index, encoder=encoder)
    as_of = today()

    print("=" * 74)
    print("RETRIEVAL EVAL   mode=%s   k=%d   floor=%.2f" % (mode, k, floor))
    if model:
        print("                 model=%s" % model)
    print("=" * 74)

    # ---------------------------------------------------------- documented
    hits_at_k = total = 0
    misses = []
    for a in DOCUMENTED:
        want = sop_uri(a.code)
        for q in a.questions:
            total += 1
            docs = hit_docs(store.search(q, k=k, as_of=as_of, mode=mode,
                                         exclude_sources=["sim"]))
            if want in docs:
                hits_at_k += 1
            else:
                misses.append((a.code, q, docs[:3]))
    doc_score = hits_at_k / total if total else 0.0
    print("\n1. DOCUMENTED       retrieval@%d  %5.1f%%   (%d/%d)"
          % (k, doc_score * 100, hits_at_k, total))
    for code, q, got in misses:
        print("     miss  %-22s %s" % (code, q))
        if verbose:
            print("           got: %s" % (got or "nothing"))

    # ------------------------------------------------------------ held out
    h_hits = h_total = 0
    h_partial = 0
    for a in HELD_OUT:
        wanted = {sop_uri(c) for c in a.analogous_to}
        for q in a.questions:
            h_total += 1
            docs = set(hit_docs(store.search(q, k=k, as_of=as_of, mode=mode,
                                             exclude_sources=["sim"])))
            found = wanted & docs
            if found == wanted:
                h_hits += 1
            elif found:
                h_partial += 1
    print("\n2. HELD OUT         all analogues in top %d  %5.1f%%   (%d/%d)"
          % (k, (h_hits / h_total * 100) if h_total else 0, h_hits, h_total))
    print("                    at least one analogue     %5.1f%%   (%d/%d)"
          % (((h_hits + h_partial) / h_total * 100) if h_total else 0,
             h_hits + h_partial, h_total))
    print("                    (no SOP exists for these; this is the material")
    print("                     a derived answer would have to reason from)")

    # -------------------------------------------------------- supersession
    s_ok = s_total = 0
    leaked = []
    for product in FEE_PRODUCTS:
        slug = product.replace(" ", "-").lower()
        current = "circular/CIR-%s-2025.md" % slug
        old_2023 = "circular/CIR-%s-2023.md" % slug
        q = "what is the current fee cap for %s" % product
        s_total += 1
        docs = hit_docs(store.search(q, k=k, as_of=as_of, mode=mode))
        if old_2023 in docs:
            leaked.append((product, "superseded circular was returned"))
        elif current in docs:
            s_ok += 1
        else:
            leaked.append((product, "current circular not in top %d" % k))
    print("\n3. SUPERSESSION     current returned, old suppressed  %5.1f%%   (%d/%d)"
          % ((s_ok / s_total * 100) if s_total else 0, s_ok, s_total))
    for product, why in leaked:
        print("     FAIL  %-18s %s" % (product, why))
    if not leaked:
        print("     no superseded document was eligible in any query")

    # ------------------------------------------------------------- refusal
    r_ok = 0
    for q in REFUSAL_QUESTIONS:
        hits = store.search(q, k=k, as_of=as_of, mode=mode, exclude_sources=["sim"])
        # Coverage, not BM25. Measured on this corpus, score does not separate
        # the two populations (real 6.38-9.07, junk 4.67-6.90) but coverage over
        # content words does, cleanly: junk 0.00, real 0.25-0.75.
        best = max((h.coverage for h in hits), default=0.0)
        if not hits or best < floor:
            r_ok += 1
        elif verbose:
            print("     would answer: %-38s coverage=%.2f  %s"
                  % (q, best, hits[0].source_uri))
    print("\n4. REFUSAL          correctly declined  %5.1f%%   (%d/%d)"
          % (r_ok / len(REFUSAL_QUESTIONS) * 100, r_ok, len(REFUSAL_QUESTIONS)))

    print("\n" + "-" * 74)
    if mode == "keyword":
        print("BASELINE, keyword only. Record these before adding vectors;")
        print("a retrieval change that cannot beat this is not worth its cost.")
    else:
        print("Compare against the keyword baseline in BASELINE.md.")
        print("A mode that does not beat it is not worth its dependencies.")
    print("-" * 74)

    store.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--index", default="index.db")
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--floor", type=float, default=0.36,
                   help="content-word coverage below which we refuse")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--mode", default="keyword",
                   choices=["keyword", "vector", "hybrid"])
    p.add_argument("--model", default=None)
    p.add_argument("--device", default="auto",
                   choices=["auto", "cpu", "cuda"])
    a = p.parse_args()
    return run(a.index, a.k, a.floor, a.verbose, a.mode, a.model, a.device)


if __name__ == "__main__":
    raise SystemExit(main())
