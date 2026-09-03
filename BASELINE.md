# Baseline and mode comparison

Keyword was recorded first, before any vector search existed, so later changes
had something to beat. Vector and hybrid were then measured on the identical
question set.

## First generation, and what it taught about verifying citations

The local 7B produced its first answer, and getting it verified took three
attempts. Each failure was this codebase's fault, and the third is the useful
one.

**Attempt 1: no citations at all.** The model wrote `[1] [PROCEDURE] Confirm
both entries...` instead of `[n] "quoted text"`. The claims were grounded but
not verifiably grounded, and a parser cannot check a span it was not given.
Fixed by giving the instruction a worked example of the right form and two of
the wrong ones.

**Attempt 2: right quotes, wrong blocks.** Every span was verbatim in a supplied
block and every index was wrong. The model was numbering the procedure *steps*
1 to 4, not the blocks 0 to 11, because the answer format asks for numbered
steps and the blocks were also labelled with numbers. My prompt created the
collision.

Two fixes. Blocks are labelled `[A]`, `[B]`, so they cannot collide with step
numbers. And verification now looks for the span in **every** supplied block,
accepting it with a corrected index if found elsewhere. Those are different
properties and only one is a safety property: **a fabricated quote is a lie, a
misnumbered one is a typo.**

**Attempt 3: it passed, and the citations were worthless.** The model emitted
thirteen identical citations of `"One authorisation, two postings"`, the
document title, which the chunker prepends to every chunk as its heading trail.
Every span was verbatim, so every one passed, and together they grounded
nothing.

That is the finding worth keeping. **A check that can be satisfied cheaply will
be satisfied cheaply**, and the model was not being dishonest, it found the
least effortful thing that met the stated rule. The rule was wrong: it asked
whether a span exists, when what matters is whether a citation *narrows down
where the claim came from*. A span present in every block narrows nothing.

Verification now rejects a citation appearing in more than half the blocks, and
rejects the same span cited twice. The next attempt produced four distinct,
substantive citations and passed:

    [D] "Identify which posting came second by created_at, and reverse that one."
    [E] "Escalate when more than five duplicates appear in the same slot..."
    [F] "Do not reverse both entries."
    [G] "Record the RRN, the correlation ID where one exists..."

Worth noting how it was found. No test caught it and no measurement would have:
the score said VERIFIED. It was visible only by reading the output, which is an
argument for looking at what a system actually produces rather than only at
what it scores.

## The bug the tests found

Writing regression tests turned up the worst defect so far, and the eval had
been reporting 100% on it the whole time.

`MIN_CHARS` in the chunker was 120, intended to drop a heading with no body. A
fee circular's `## 2. The cap` section is one sentence, roughly 70 characters,
and it is **the only place the actual figure appears**. Every circular lost it.

Measured after the fix: **zero chunks in the entire corpus contained a fee cap
before; twenty do now.** SOP chunks went from 37 to 84, so content was being
discarded across the corpus, not just from circulars.

Supersession still read 100% throughout, because the measure asked *which
document ranked* and never *whether the chunk held the answer*. The filter was
working perfectly and returning the right document with the answer removed from
it.

Two lessons, and the second is the general one:

- **A length floor on chunks is a content filter.** Short sections are often the
  ones carrying the fact. `MIN_CHARS` is now 25, which drops an empty heading and
  nothing else.
- **Document identity is not answer presence.** The supersession measure now
  asserts the returned chunk contains "maximum fee", and a test asserts it
  separately. Any measure that checks which document came back can pass while the
  answer is missing from it.

A second bug surfaced the same way. Coverage was computed by substring, so
"match" counted as present in "Matched RRN where the two amounts differ" and the
junk question "who won the cricket match yesterday" scored 0.25 instead of 0.
Coverage is what the refusal floor reads, so a substring hit is a junk question
quietly promoted to answerable. It now matches whole words.

Neither bug was found by the eval. Both were found by writing tests that assert
behaviour rather than measure quality.

## Precedent, and why it was bad

Precedent retrieval was visibly poor and unmeasured: a duplicate-posting
question returned fee disputes, 1 of 3 relevant. The cause is not tuning.

**The two corpora describe the same defect in vocabulary that does not overlap.**

| A narrative says | The procedure for it is titled |
|---|---|
| the account was debited twice | One authorisation, two postings |
| the transaction failed but the account was still debited | Approved at the switch, absent from the ledger |
| the amount debited did not match the amount entered | Same reference, different amounts |

No shared term for a keyword index to match, no shared framing for an embedding
to place nearby. That is a property of the domain rather than a defect in the
retriever: complaints are written by customers and procedures are written by
engineers, and they describe the symptom and the cause respectively.

So precedent follows the procedure tier: **a known class is a lookup, not a
search.** `REASON_TO_ANOMALY` maps the twelve complaint reasons onto the
operational catalogue, ingest carries the class onto each narrative, and the
router selects on `anomaly_code`. On the duplicate-posting question that moves
precedent from 1 of 3 relevant to 3 of 3.

Two of the twelve reasons map to nothing on purpose. An agent keeping the cash
is a conduct problem and a merchant withholding goods is a commercial dispute;
neither is a ledger defect, so both remain reachable only by search, which is
correct rather than a gap.

### The number that matters is the search path

Precedent by lookup is a `WHERE anomaly_code = ?`, so scoring it would be
scoring a SQL clause. The honest measurement is what a **novel** defect gets,
because search is the only option there:

**37.5% relevant (9 of 24).**

That is low, it is now visible, and it is the correct thing to have measured.
It also bounds the derived tier: when the system meets a defect nobody
documented, roughly a third of the past cases it offers as evidence are the
right kind. Improving it means either bridging the two vocabularies at index
time or accepting that precedent is weak evidence for novel defects and saying
so in the answer.

## Result with document-kind scoping

The measurements below replaced a flawed harness. The earlier runs excluded
`source = 'sim'` on the documented questions, which hid 10,000 narratives a real
query would face: the harness was scoring a 160-chunk problem while the system
faced 10,160. Scoping by **document kind** instead (`doc_type IN ('sop',
'circular')`) is the honest equivalent, because "what do I do about this"
genuinely wants a procedure rather than a case history, and the router applies
the same scoping in production.

| Measure | keyword | vector | **hybrid** |
|---|---|---|---|
| Documented, retrieval@5 | 95.8% | 95.8% | **100%** (24/24) |
| Held out, all analogues | 12.5% | 37.5% | 25.0% |
| Held out, at least one | 100% | 100% | **100%** (8/8) |
| Supersession | 100% | 100% | 100% |
| Refusal | 100% | 100% | 100% |

### The competitor was never the narratives

Scoping by kind raised documented retrieval from 91.7% to 100%, and held-out
"at least one analogue" from 75% to 100%. The 10,000 narratives were not the
problem.

`architecture.html` was. It is 131 KB producing 103 long chunks of general
payments vocabulary, and it matched almost any operational phrasing well enough
to outrank the short precise SOP that actually answered the question. A single
large document was crowding out the whole procedure corpus.

The general point: pooling documents of different **kinds** into one ranking does
not surface the best answer, it surfaces the biggest document. Kind is a
retrieval filter, not just metadata.

### The exact/derived tier cannot come from a score

The plan was to call an answer "exact" above some retrieval threshold. Measuring
the three populations showed no such threshold exists:

| Top-ranked procedure is... | coverage range |
|---|---|
| the correct one | 0.25 to 1.00 |
| the wrong one | 0.25 to 0.60 |
| a held-out defect with no correct answer | 0.25 to 0.67 |

They overlap almost completely. Coverage separates a real question from junk,
which is what the refusal floor uses it for, and it does not separate a right
answer from a wrong one.

The fix is better than a threshold. **A break does not arrive as a bare
question**: the reconciliation engine found it with a deterministic predicate,
so the defect class is already known, and the procedure for a known class is a
lookup by name rather than a search. Retrieval is for the case where the class
is novel, and everything reached by search is therefore `derived` by
construction. Tier accuracy is now 5/5 on the tier cases, and it does not depend
on a number that the data says cannot exist.

## Earlier result at 10,160 chunks, before scoping

The corpus was scaled from 258 chunks to 10,160 by running the simulator to
93,687 unique narratives and ingesting 10,000 of them.

| Measure | keyword | vector | hybrid | hybrid + rerank |
|---|---|---|---|---|
| Documented, retrieval@5 | 79.2% | 79.2% | **91.7%** | 79.2% |
| Held out, at least one | 87.5% | 87.5% | 75.0% | 75.0% |
| Supersession | 100% | 100% | 100% | 100% |
| Refusal | 100% | 100% | 100% | 100% |

Two results here were not what I expected, and both are more useful than the
numbers themselves.

### Reranking made it worse, not better

The build order predicted "the largest single jump here". It was a 12.5 point
**drop**, from 91.7% to 79.2%.

The reranker is `cross-encoder/ms-marco-MiniLM-L-6-v2`, trained on MS MARCO,
which is web search passages answering natural questions. This corpus is
numbered operational procedures with heading trails prepended to every chunk.
The model is confidently reordering on a notion of relevance learned somewhere
else, and it is worse at this than the fusion ranking it replaced.

The lesson is not that reranking does not work. It is that a cross-encoder is a
*trained judgement*, and an untuned one imported from another domain is a
liability rather than a free win. Whether a larger in-domain reranker
(`bge-reranker-v2-m3`) recovers it is the next thing to measure, and it should
be measured rather than assumed, exactly as this was.

### Corpus growth changed results through filters that should have isolated it

Keyword and vector both scored **exactly the same** at 258 and at 10,160 chunks.
Hybrid dropped 4.1 points, from 95.8% to 91.7%.

The eligible set was identical in both runs: the documented questions filter with
`source != 'sim'`, which leaves **160 chunks either way**. The 10,000 narratives
were never candidates.

They still changed the answer, because **BM25's term statistics are global**.
Ten thousand narratives full of "customer", "charged", "transaction" and
"posting" drove down the inverse document frequency of exactly those terms, so
the same 160 chunks ranked differently. `DUPLICATE_POSTING`, whose question is
"the customer was charged twice for one transaction", is precisely the casualty
that mechanism predicts.

Worth carrying: a query-time filter does not insulate a ranking from corpus
growth. Anything sharing an index shares its statistics.

## Earlier result at 258 chunks

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
