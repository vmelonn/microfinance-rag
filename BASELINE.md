# Baseline: keyword only

Recorded before any vector search exists. Every later retrieval change is
measured against this. A change that cannot beat it is not worth its cost.

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
python eval/run_eval.py --index index.db -k 5
```

Narrative counts vary between runs because the simulator is entropy-seeded. The
SOP, circular and platform document counts do not, and those are what the four
measures above depend on.
