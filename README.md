# microfinance-rag

A retrieval layer over the `microfinance-microservices` payment platform. Separate
repository, same OpenShift namespace, four read-only seams.

It puts a question box on the operator console and answers with citations drawn from
the platform's own documentation and its accumulated case history.

## Documents

| File | What it covers |
|---|---|
| [PLAN.md](PLAN.md) | What it does, layout, feasibility, and the six limits |
| [DEPLOYMENT.md](DEPLOYMENT.md) | How it joins the platform, and the test data plan |
| [PROJECT.html](PROJECT.html) | The same material as a page, with diagrams |

## Quick start

Nothing here needs the cluster. The simulator writes SQLite by default.

```bash
python sim/simulate.py --rate 50          # runs until Ctrl+C
python sim/simulate.py --for 2m           # or stop on its own
```

Then look at what it made:

```sql
sqlite3 sim.db "select title, body from sim_narratives limit 5;"
```

## Two data sources, kept apart on purpose

| | `practice_db.py` (platform repo) | `sim/simulate.py` (here) |
|---|---|---|
| Seed | fixed, `20260814` | entropy, unique per run |
| Runs | once, terminates | forever, until stopped |
| Output | 13 tables of rows | transactions, disputes, **written narratives** |
| Purpose | the reproducible eval corpus | volume and variety |

The evaluation set is written against exact rows from the first, so it must not
move. The second exists to keep the corpus growing and changing so retrieval is
never scored against something it has memorised.

Everything the simulator writes carries `source = 'sim'`, so the eval corpus can
always exclude it.

## Why the simulator writes prose

`practice_db.py` produces rows. A `disputes.reason` of `"agent did not hand over
cash"` is a label, not a document, and there is nothing there for retrieval to work
on. So the simulator writes a resolution narrative for every case it closes,
assembled from a large enough vocabulary that repeats are vanishingly rare.

Measured on a short run: 98 narratives, 98 distinct bodies, zero duplicates.

## Layout

```
app/
  ingest/     loaders, chunker, metadata, embedder, pipeline
  retrieve/   store, hybrid search, rerank, filters
  answer/     router, sql_tool, prompt, llm, citations
  api/        routes
db/migrations/
eval/         questions.yaml, run_eval.py
sim/          simulate.py
openshift/base/
```

`retrieve/` never imports `answer/`, and `answer/` never touches the database
directly. That separation is what lets the eval measure retrieval quality apart from
answer quality.

## Status

Early. The simulator runs. Schema, ingest, and retrieval are next, in the build
order at the end of [PLAN.md](PLAN.md).
