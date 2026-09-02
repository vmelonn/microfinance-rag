# Baseline and mode comparison

Keyword was recorded first, before any vector search existed, so later changes
had something to beat. Vector and hybrid were then measured on the identical
question set.

## Result

| Measure | keyword | vector | **hybrid** |
|---|---|---|---|
| Documented, retrieval@5 | 79.2% | 79.2% | **95.8%** |
| Held out, all analogues | 12.5% | 0.0% | 0.0% |
| Held out, at least one | 87.5% | 87.5% | 87.5% |
| Supersession | 100% | 100% | 100% |
| Refusal | 100% | 100% | 100% |

Embedding model `BAAI/bge-small-en-v1.5`, 384 dims, on a laptop RTX 4060.
258 chunks embedded in 1.0s at 246 chunks/sec; VRAM returned to idle after.

## The finding: the two modes fail on different questions

Vector search scores **exactly the same as keyword**, 19 of 24. Taken alone that
reads as "embeddings added nothing", which is what PLAN.md limit 1 predicted for
a corpus this small.

It is the wrong reading. Look at *which* questions each one misses:

| Missed by keyword | Missed by vector |
|---|---|
| ORPHAN_SWITCH (both phrasings) | UNBALANCED_ENTRY (both phrasings) |
| AMOUNT_MISMATCH | STALE_REVERSAL |
| DUPLICATE_POSTING | APPROVED_BUT_DECLINED |
| NEGATIVE_BALANCE | NEGATIVE_BALANCE |

**The sets are almost disjoint.** Only NEGATIVE_BALANCE defeats both. Keyword
fails on paraphrase, where "the customer was charged twice" shares no vocabulary
with "posted more than once". Vector fails on the opposite: precise technical
phrasing where the exact words carry the meaning and semantic similarity blurs
"debit and credit legs" into every other ledger passage.

Fusing them recovers almost everything: **95.8%, 23 of 24**. That is a +16.6
point gain over either mode alone, from two components that individually look
identical in score.

The lesson generalises past this corpus. Comparing retrieval modes on a single
aggregate number hides whether they are redundant or complementary, and that
distinction is the entire case for hybrid search. Two modes at 79% each can be
worth 96% together or worth nothing together, and the score alone cannot tell
you which.

## What still fails

`NEGATIVE_BALANCE` on the phrasing "solvency invariant was violated on an
account" misses in every mode. The SOP says "wallet balance went negative" and
never uses the word "solvency", so there is neither a lexical overlap for BM25
nor enough semantic proximity for a small embedding model. Either the SOP should
name the invariant, or this is the kind of gap a reranker is meant to close.
That is the next thing to measure, not to assume.

## Held-out defects got worse, and that is informative

"All analogues in the top 5" fell from 12.5% to 0% under vector and hybrid, while
"at least one analogue" held at 87.5% everywhere. Vector search pulls in
semantically adjacent material that crowds out the second and third analogue.
For a derived answer that means thinner grounding: one procedure to reason from
instead of three. Whether that materially weakens the derivation is an answer
quality question, not a retrieval one, and it cannot be settled from these
numbers.

---

## Original keyword baseline

Index: 124 documents, 258 chunks. 12 SOPs, 10 circulars (5 supersession pairs),
98 simulator narratives, 4 platform documents.

| Measure | Result | |
|---|---|---|
| Documented, retrieval@5 | **79.2%** | 19/24 |
| Held out, all analogues in top 5 | **12.5%** | 1/8 |
| Held out, at least one analogue | **87.5%** | 7/8 |
| Supersession, old suppressed | **100%** | 5/5 |
| Refusal, correctly declined | **100%** | 5/5 |

## What these mean

**Documented 79.2%** is the plain retrieval number. Five questions miss, and
they cluster on paraphrase: "the customer was charged twice" never lands on
SOP-DUPLICATE_POSTING because the document says "posted more than once". This
is exactly the gap embeddings are supposed to close, so it is the number to
watch when vectors arrive.

**Held out 12.5% / 87.5%** is the more interesting pair. Getting at least one
analogous procedure into the top 5 nearly always works; getting all of them
almost never does. A derived answer built on one analogue rather than three is
thinner but not wrong, so the honest reading is that the material is usually
there and rarely complete.

**Supersession 100%** is a filter result, not a ranking one. No superseded
circular was ever eligible, because status is a WHERE clause and not a
tie-breaker. This is the one number that must stay at 100%; anything less is a
compliance failure rather than a quality dip.

**Refusal 100%**, but only after fixing how it was measured, below.

## The refusal finding

The first attempt used a BM25 score floor and scored 0/5. Measuring the two
populations showed why:

| | real questions | junk questions |
|---|---|---|
| BM25 score | 6.38 to 9.07 | 4.67 to 6.90 |

**They overlap, so no score floor can separate them.** Terms are OR-ed, so a
question about office printers matches "office" somewhere and scores
respectably.

Switching to *content-word coverage*, meaning the share of non-stopword query
terms actually present in the chunk, separates cleanly:

| | real questions | junk questions |
|---|---|---|
| coverage, all words | 0.50 to 0.75 | up to 0.67 (still overlapping) |
| coverage, content words only | **0.40 to 1.00** | **0.00 to 0.33** |

Stopword removal is what does the work. "What is our policy on annual leave"
shares *what is our on* with half the corpus; strip those and it shares nothing.

Floor set to **0.36**, between the two populations.

**Caveat worth carrying:** the margin is 0.33 to 0.40 on five junk questions and
24 real ones. That is a narrow gap on a small sample, and it will need
re-tuning once the question set grows. Do not treat 0.36 as settled.

## Reproduce

```bash
python sim/simulate.py --for 30s --db sim.db
python eval/generate_sops.py --out corpus/
python -m app.ingest.pipeline --index index.db \
    --docs corpus ../microfinance-microservices/docs \
    --sim sim.db --repo-root ..
python eval/run_eval.py --index index.db -k 5 --mode keyword

# then, with a GPU or CPU
python -m app.ingest.embedder --index index.db --device cuda --batch 16
python eval/run_eval.py --index index.db -k 5 --mode hybrid     --model BAAI/bge-small-en-v1.5
```

Narrative counts vary between runs because the simulator is entropy-seeded. The
SOP, circular and platform document counts do not, and those are what the four
measures above depend on.
