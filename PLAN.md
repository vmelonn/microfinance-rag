# microfinance-rag

A retrieval layer over the existing `microfinance-microservices` platform. Separate
folder, deployed into the same OpenShift namespace, so it can read the platform's
data and the platform's console can call it.

---

## Context

The RPA brief argued that the strongest RAG use case in microfinance ops is a
copilot that helps an operator resolve an exception, because the corpus exists as a
byproduct of the work and the answer is checkable by the person receiving it. That
version is blocked on things outside this project: RSA credentials, vendor access,
PII approval, and a break history that does not exist yet.

This is the same architecture pointed at a system we fully own. Nothing here is
blocked, the data is synthetic, and the result is demonstrable end to end.

**What it does:** adds a question box to the operator console. An operator looking
at a failed transaction can ask why it failed and what to do, and get an answer
grounded in the platform's own documentation and trace history, with citations.

---

## What it is actually built on

The corpus already exists in the repo:

| Source | Size | What it gives |
|---|---|---|
| `docs/architecture.html` | 131 KB | The reference, including 10 scenarios recorded from the services' own logs |
| `docs/practice-schema.html` | 31 KB | Ledger schema and its invariants |
| `docs/sql-practice.md` | 17 KB | Query patterns over the warehouse |
| `README.md` | 13 KB | Layer map, service table, port and ownership matrix |
| `CLAUDE.md` | 4 KB | Conventions |
| Service docstrings | n/a | `idempotency.py` and `transaction-service/main.py` both carry real explanatory prose about races and compensations |

Plus structured data the router can query rather than retrieve: ledger Postgres,
the ClickHouse warehouse, and trace events keyed by correlation ID.

---

## The router, applied here

The same three-way split from `RPA/04-rag-for-microfinance-ops.md`, made concrete:

| Question | Path |
|---|---|
| "How many authorizations failed with code 51 today?" | Text to SQL over ClickHouse. Counted, never retrieved |
| "What is the balance on 03001234567?" | Direct lookup against ledger-service. Exact value, model only words it |
| "What happens if the ledger posting fails after the switch approved?" | Retrieval over the architecture doc and the saga docstring |
| "Why did correlation ID abc-123 fail?" | Both: fetch the trace by ID, then retrieve what its failure mode means |

That last row is the interesting one and the reason this is worth building. It is
the shape every real ops question takes: some facts, some reading.

---

## Layout

```
microfinance-rag/
  PLAN.md                     this file
  README.md
  requirements.txt
  Makefile
  docker-compose.yml          local: pgvector + the service
  app/
    main.py                   FastAPI, mirroring services/_base conventions
    config.py                 env only, no literals
    ingest/
      loaders.py              html, markdown, python source
      chunker.py              structure-aware: heading, scenario, docstring
      metadata.py             doc_type, source_uri, section_path, status
      embedder.py             local model, batch, offline
      pipeline.py             the ingest run, idempotent on content hash
    retrieve/
      store.py                pgvector and tsvector queries
      hybrid.py               vector + BM25, reciprocal rank fusion
      rerank.py               cross-encoder, 30 -> 5
      filters.py              hard predicates applied before similarity
    answer/
      router.py               structured vs retrieval vs lookup
      sql_tool.py             read-only, view-scoped, row-limited, statement logged
      prompt.py
      llm.py                  provider client
      citations.py            every quote verified against its source
    api/
      routes.py               /ask, /ingest, /healthz
  db/migrations/              documents, chunks, HNSW and GIN indexes
  eval/
    questions.yaml            the held-out question set
    run_eval.py               retrieval@k and answer scoring, reported separately
  openshift/base/             Deployment, Service, Route, kustomization
  tests/
```

Four modules, one job each, in the order the request travels. `retrieve/` never
imports `answer/`; `answer/` never talks to the database directly. That separation
is what lets the eval measure retrieval quality independently of answer quality,
which is the single most useful thing the eval does.

---

## How it interconnects

Four seams, all read-only:

1. **Ledger Postgres, read-only role.** For exact lookups. A dedicated role with
   `SELECT` on specific views, never the base tables.
2. **ClickHouse.** For aggregates on the SQL path. Also read-only, also view-scoped.
3. **api-gateway over HTTP.** Trace lookup by correlation ID, reusing the existing
   endpoint the console already calls.
4. **The console calls `/ask`.** One new tab. The gateway proxies it so the RAG
   service needs no Route of its own and inherits the existing JWT check.

Correlation IDs propagate inward from the gateway exactly as they do everywhere
else in the platform, so a question is traceable through the same tooling as a
payment.

**Why a separate folder rather than `services/rag-service/`:** the dependency
footprint. This service needs `sentence-transformers` and `torch`, which is a
multi-gigabyte image next to the platform's lean FastAPI images, and it would slow
every build in the existing matrix. Keeping it separate keeps that weight out of
the payment path. The cost is a second pipeline and a second deploy, which is worth
paying here.

---

## Feasibility

Genuinely high, and for a specific reason: **every blocker from the company version
is absent.** No RSA, no vendor portal, no PII approval, no waiting on a break
history to accumulate. The corpus is committed to the repo today, Postgres 16 is
already running, and the namespace already exists.

The pieces that are real work: chunking HTML sensibly, getting hybrid retrieval to
fuse properly, and writing an honest eval set. Nothing there is research, it is all
engineering with known answers.

---

## Limits, stated plainly

These are the things that would make this a weak project if left unsaid.

### 1. The corpus is small enough that RAG may not beat keyword search

About 196 KB of documentation, which chunks to somewhere in the low hundreds. At
that scale a well-tuned BM25 index will perform close to a vector index, because
there is not enough semantic variety for embeddings to earn their cost.

Say so up front, and frame the deliverable honestly: this **demonstrates the
architecture and produces a working eval harness**, it does not prove that RAG beats
search on this corpus. Then make the eval do real work by measuring both, because
"we measured and BM25 was within noise on a 400 chunk corpus" is a genuinely good
finding, and a more credible one than a claimed win.

### 2. The namespace almost certainly cannot host an embedding model

The manifests are frugal for a reason. Twelve workloads, services requesting 50m
CPU and 128Mi, ClickHouse alone taking 3Gi, and comments in `ace-stub.yaml` and
`api-gateway.yaml` noting a namespace LimitRange that injects a 1 CPU default and a
ReplicaSet quota of thirty. A `sentence-transformers` container wanting 1 to 2Gi
resident does not fit comfortably into what is left.

**Resolution: split ingest from query.** Ingest runs offline on a laptop or in CI,
where a full-size model is free, and writes vectors into Postgres. At query time the
service embeds exactly one short string, which a quantized `nomic-embed-text-v1.5`
at roughly 300 MB can do, or which can be skipped entirely by running keyword-only
retrieval in the sandbox and reserving vector search for local development.

This is worth designing for from the start rather than discovering at deploy time.

### 3. `postgres:16-alpine` does not ship pgvector

`openshift/base/datastores.yaml` uses the stock Alpine image, which has no
`vector` extension. It needs `pgvector/pgvector:pg16` instead. A one line change,
but it touches the datastore the ledger depends on, so either swap it deliberately
or stand up a second Postgres for the index alone. Given the ledger's data matters
and the index's does not, a second instance is the safer call.

### 4. Synthetic data means no real feedback loop

There are no operators, so nothing generates accept, edit or reject signal. The
eval set has to be hand written, which means it measures what you thought to ask
rather than what people actually ask. That is a real ceiling on how much the
retrieval can be tuned, and it is why the eval questions should come from the
recorded scenarios rather than from imagination.

### 5. It cannot demonstrate the thing that matters most

The company version's value is measured in operator minutes saved. Here there are
no operators and no baseline, so no such number exists. What this can show is that
the pipeline works, that citations verify, and that the router sends numeric
questions to SQL instead of guessing them. Those are worth showing; time saved is
not available and should not be claimed.

### 6. It is more interesting than the thing that pays

The MPG fetcher and the reconciliation pilot are what the company actually needs.
This is the more enjoyable project and it competes for the same hours. Worth being
deliberate about that rather than drifting into it.

---

## Build order

**Done unless marked otherwise.**

1. ~~**Schema and migrations.**~~ Done, both dialects. sqlite+FTS5 for local so
   the baseline needs no Postgres; postgres+pgvector for deployment.
2. ~~**Ingest, offline.**~~ Done, idempotent on content hash. A chunker bug here
   silently deleted every fee figure in the corpus; see BASELINE.md.
3. ~~**The eval set, before any tuning.**~~ Done, generated from the catalogue so
   the correct answer is known by construction. Five measures scored separately.
4. ~~**Retrieval, measured.**~~ Done. Keyword 95.8%, vector 95.8%, hybrid 100%.
   The two modes fail on almost disjoint questions, which is the whole case for
   fusing them and is invisible in the aggregate score.
5. **Rerank.** Measure again. Measured: an off-the-shelf MS MARCO
   cross-encoder made it 12.5 points *worse*, not better. See BASELINE.md.
   An in-domain reranker may still help; assume nothing.
6. ~~**The router and the SQL tool.**~~ Done. A closed registry of vetted queries
   rather than text-to-SQL, plus a lookup path for exact values. Four of six
   flows now never reach a model at all.
7. **Answer and citation verification.** Built and reviewed by dry run;
   **generation has never actually run**, because the local model is not pulled.
8. **Console tab, proxied through api-gateway.**
9. **Deploy**, with query-time embedding sized to whatever the namespace allows.

Steps 3 and 4 are the ones that make this an engineering project rather than a
demo. Do not skip past them to the answer generation, which is the easiest part and
the least informative.
