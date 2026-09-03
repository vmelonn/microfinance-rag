# microfinance-rag

A retrieval layer over the `microfinance-microservices` payment platform. An
operator working a reconciliation break gets the procedure that governs it, the
closest past cases, and a drafted next action with citations.

## Where it stands

| Flow | Trigger | Status |
|---|---|---|
| **A** numeric | "how many", "total" | **done**, answered by a query registry |
| **B** lookup | "balance", "card status" | **done**, answered by the system of record |
| **C** exact | defect class known | retrieval done, generation blocked on a model |
| **D** derived | novel defect, procedure found | retrieval done, generation blocked |
| **E** derived | novel defect, only analogues found | retrieval done, generation blocked |
| **F** none | nothing clears the floor | **done**, correct by design |

**Four of six paths never reach a model.** That was not the plan at the start.
Measurement pushed each one there: counts to SQL, exact values to the system of
record, known defect classes to a `WHERE` clause, and unanswerable questions to a
refusal.

The pattern showed up three separate times, for procedures, for the tier
decision, and for precedent: **when the class is known, it is a lookup, not a
search.** Each time the design began with retrieval and measurement said the
deterministic path was both simpler and correct.

## Measured

See [BASELINE.md](BASELINE.md) for method and the findings behind each number.

| | |
|---|---|
| Documented retrieval@5, hybrid | **100%** (24/24) |
| Held out, at least one analogue | **100%** (8/8) |
| Supersession, old suppressed | **100%**, and the figure is in the chunk |
| Refusal, junk declined | **100%** (5/5) |
| Precedent by search, novel defects | **37.5%** (9/24) |
| Guardrail tests | 28 passing |

The last row of numbers is the honest one: precedent for a **novel** defect is
weak, and that bounds the derived tier. Precedent for a known class is a lookup
and exact by construction, which is why it is not scored.

## Quick start

Nothing here needs the cluster, a GPU, or an API key.

```bash
# 1. generate data and the procedure corpus
python sim/simulate.py --for 2m --db sim.db
python eval/generate_sops.py --out corpus/

# 2. build the index
python -m app.ingest.pipeline --index index.db \
    --docs corpus ../microfinance-microservices/docs \
    --sim sim.db --repo-root ..

# 3. the keyword baseline, no model of any kind
python eval/run_eval.py --index index.db --mode keyword

# 4. add vectors (GPU optional; --device cpu works)
python -m app.ingest.embedder --index index.db --device cuda --batch 32
python eval/run_eval.py --index index.db --mode hybrid \
    --model BAAI/bge-small-en-v1.5

# 5. ask something
python -m app.answer.ask --index index.db --data sim.db \
    --ledger ../microfinance-microservices/practice.db \
    --question "how many disputes are still open"
```

`--dry-run` on `ask` prints the exact model request and sends nothing, which is
how the prompt and its guardrails get reviewed with no key and no spend.

## The test harness

```bash
RAG_INDEX=index.db RAG_DATA=sim.db RAG_LEDGER=../microfinance-microservices/practice.db python -m uvicorn app.api.routes:app --port 8086
```

Then open <http://127.0.0.1:8086>.

It shows the **routing decision**, not just the answer: which of the six flows
was taken and why, each retrieval pool separately, the SQL statement when there
is one, and the exact prompt that would be sent. Since four of six flows never
reach a model, a UI showing only a final answer would hide the part worth
checking.

Preset buttons cover every flow. "Route it" stops before generation; "Route and
generate" calls the model, which needs one pulled in Ollama.

## Layout

```
sim/
  catalogue.py    16 defect classes. One definition, three consumers
  simulate.py     continuous generator, unique per run, never terminates
app/
  ingest/         loaders, structure-aware chunker, offline embedder
  retrieve/       keyword store, hybrid fusion, cross-encoder rerank
  answer/         router, prompt, sql_tool, lookup, citations, llm, ask
eval/
  generate_sops.py  procedures and supersession pairs, from the catalogue
  run_eval.py       five measures, scored separately
tests/            28 guardrail tests
db/migrations/    sqlite for local, postgres+pgvector for deployment
```

`retrieve/` never imports `answer/`, and `answer/` never touches the database
directly. That separation is what lets the eval score retrieval apart from
answer quality.

## The catalogue is the spine

`sim/catalogue.py` defines 16 defect classes once. From that single definition
come the injected data, the procedure that fixes each one, and the evaluation
questions. They cannot drift, because a drifted SOP would make the eval score a
document that no longer describes the planted defect.

Four classes are **held out**: injected into the data with no procedure written,
so the answer cannot be looked up. They test the capability that matters, which
is responding to a defect nobody documented. `analogous_to` records which
procedures a correct derivation should have reasoned from.

## Two data sources, kept apart

| | `practice_db.py` (platform repo) | `sim/simulate.py` (here) |
|---|---|---|
| Seed | fixed | entropy, unique per run |
| Runs | once | until stopped |
| For | a reproducible eval corpus | volume and variety |

Measured at scale: 93,687 narratives, zero duplicate bodies.

## Documents

| File | What it covers |
|---|---|
| [PLAN.md](PLAN.md) | Why it exists, layout, feasibility, six limits |
| [BASELINE.md](BASELINE.md) | Every measurement, and what each one taught |
| [GUARDRAILS.md](GUARDRAILS.md) | The three tiers and the controls at each layer |
| [DEPLOYMENT.md](DEPLOYMENT.md) | How it joins the platform, and the test data plan |
| [PROJECT.html](PROJECT.html) | The same material as a page, with diagrams |

## Not done

- **Generation has never run.** The local model has not been pulled, so flows C,
  D and E have produced retrieval but never an answer. Citation verification has
  never seen a real citation.
- **No deploy.** `DEPLOYMENT.md` describes the manifests, NetworkPolicies and the
  Dockerfile; none exist.
- **Precedent for novel defects is weak**, at 37.5%. Improving it means either
  bridging the two vocabularies at index time or saying plainly in the answer
  that the evidence is thin.
