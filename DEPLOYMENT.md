# Deployment and test data

Two questions: how `rag-service` joins the running platform, and how we get enough
data into it to mean anything.

---

## Part 1: how it connects

### Service discovery follows the pattern already in use

Every service in the platform addresses its neighbours the same way: a Kubernetes
Service DNS name, injected as an env var.

```
http://auth-service:8081        AUTH_SERVICE_URL
http://ledger-service:8084      LEDGER_SERVICE_URL
http://risk-service:8083        RISK_SERVICE_URL
http://transaction-service:8082 TRANSACTION_SERVICE_URL
```

So `rag-service` gets exactly one new name and one new variable:

```
http://rag-service:8086         RAG_SERVICE_URL
```

Port 8086 because 8080 to 8085 and 8090 are taken, and 9999 is the switch.

**Two directions to wire:**

| Direction | Variable | Set on |
|---|---|---|
| Gateway proxies `/ask` to RAG | `RAG_SERVICE_URL` | `api-gateway` |
| RAG fetches traces back | `GATEWAY_INTERNAL_URL` | `rag-service` |

The second is a genuine cycle in the call graph, gateway to RAG to gateway. It is
fine because the inner call hits a different endpoint and carries no user token, but
it needs a timeout shorter than the outer request or a slow trace lookup will hold
the operator's request open. Set it explicitly.

### NetworkPolicy is the part that will silently break first

`platform.yaml` declares **`default-deny-ingress` with an empty `podSelector`**,
meaning every pod in the namespace rejects ingress unless a policy allows it. The
existing policies are named per hop:

```
allow-router-to-gateway
allow-gateway-downstream
allow-orchestrator-downstream
allow-adapter-to-soap-gateway
allow-gateway-to-switch
```

A new pod with no matching policy simply gets connection timeouts, with nothing in
its own logs to explain why. Two policies are needed:

1. **`allow-gateway-to-rag`** so the gateway can reach `rag-service:8086`.
2. **`allow-rag-to-datastores`** so `rag-service` can reach the ledger Postgres,
   ClickHouse, and its own Postgres.

Write these at the same time as the Deployment, not after the first timeout.

### Deliberately not doing

- **No Route.** The gateway is the only public Route and stays that way. `/ask` is
  proxied, so the RAG service inherits the JWT check rather than reimplementing it.
- **No writes.** Read-only Postgres role scoped to views, read-only ClickHouse user.
- **Not in the platform's kustomization.** `openshift/base/kustomization.yaml` lists
  the platform's own resources. This project ships its own base and overlays with the
  same structure, applied separately. The only edit to the existing repo is adding
  `RAG_SERVICE_URL` to the gateway and the two NetworkPolicies.

### Secrets

The platform's secret is `microfinance-secrets` with `POSTGRES_PASSWORD` and
`CLICKHOUSE_PASSWORD`. The RAG service needs its own for the LLM API key and its own
Postgres password.

Note the known bug in the existing pipeline: `oc apply -k` overwrites the real Secret
with the repo placeholder on each run. Do not inherit that here, and do not put the
API key anywhere it can be clobbered by a deploy.

### Deployment shape

```
openshift/
  base/
    rag-service.yaml        Deployment, Service (8086), ServiceAccount
    rag-postgres.yaml       StatefulSet, PVC, Service; pgvector/pgvector:pg16
    networkpolicy.yaml      the two policies above
    secret.yaml             template only, real values applied out of band
    kustomization.yaml
  overlays/dev/
  overlays/prod/
```

Resource requests must be modest, matching the platform's own frugality (50m CPU,
128Mi is the house style). See limit 2 in `PLAN.md`: no embedding model runs in the
cluster, so the service is a thin query layer and can genuinely stay small.

---

## Part 2: test data

The point is volume and mess, so that retrieval is doing real work rather than
picking from twelve documents.

### What already exists

`scripts/practice_db.py` in the platform repo is a seeded generator, and it is
better than anything worth writing from scratch. It already produces thirteen tables
with deliberate mess: accounts with no transactions, declined and reversed rows,
`disputes.resolved_at` NULL while open, nullable and mutually exclusive
`agent_id` / `merchant_id`, blocked cards, a merchant with no sales.

Volume scales linearly with `--scale`:

| | scale 1 | scale 50 | scale 100 |
|---|---|---|---|
| users | 400 | 20,000 | 40,000 |
| branches | 24 | 1,200 | 2,400 |
| transactions | 9,000 | 450,000 | 900,000 |
| disputes | 120 | 6,000 | 12,000 |

**Use `--scale 50` as the working set.** Half a million transactions is enough for
the SQL path to be non-trivial and for aggregate questions to have interesting
answers, while still building in under a minute and fitting a laptop.

The seed is fixed, so the data is identical every run. That matters more than it
sounds: it means the evaluation set stays valid across rebuilds.

### What is missing, and has to be generated

`practice_db.py` produces **rows, not prose.** The SQL path is fully served by it.
The retrieval path has almost nothing, because a `disputes.reason` of
`"agent did not hand over cash"` is a label, not a document.

So a second generator, `eval/generate_corpus.py`, producing three things:

**1. Resolution narratives.** For each closed dispute, a short paragraph saying what
was investigated and how it was concluded, written from the structured row. Roughly
6,000 documents at scale 50. This is the corpus that makes "how was a case like this
closed before" answerable, and it is the closest analogue to the real break history
the company version would accumulate.

**2. SOP documents.** Twenty to thirty procedures covering the dispute reasons and
failure modes that actually appear in the generated data, written with numbered
sections so the chunker has real structure to cut on. These must reference the same
vocabulary the disputes use, or retrieval will never connect a case to its procedure.

**3. Circulars with a supersession chain.** The important one.

To test that filtering happens before similarity, the corpus **must contain documents
that are topically perfect but no longer in force.** So generate families: a 2023
circular setting a threshold, a 2025 circular replacing it with a different threshold,
`superseded_by` linking them, and both indexed.

Then the evaluation can ask a question whose correct answer is only in the 2025
document, and a system that skips the filter will confidently return the 2023 figure.
That is a test that fails loudly for the right reason, and without a supersession
chain in the data there is no way to write it.

### Volume target

| Source | Documents | Chunks, roughly |
|---|---|---|
| Platform docs (real, in repo) | 5 | 300 to 600 |
| Resolution narratives | 6,000 | 6,000 |
| SOPs | 25 | 400 |
| Circulars, incl. superseded | 60 | 900 |
| **Total** | **~6,100** | **~8,000** |

Eight thousand chunks is a different regime from the few hundred in `PLAN.md` limit 1.
At that size embeddings have something to do and the BM25 comparison becomes a real
experiment rather than a foregone conclusion. **This is the single change that makes
the project's central measurement meaningful**, which is a better reason to generate
data than "more is better".

### The evaluation set comes from the generator, not from imagination

Because the data is generated, the correct answer for each question is known by
construction. `generate_corpus.py` writes `eval/questions.yaml` as it goes:

- **Retrieval questions**: "what governs a dispute where the agent did not hand over
  cash" with the SOP section id that was generated as the answer.
- **Supersession questions**: a threshold question whose only correct source is the
  current circular, with the superseded one recorded as the wrong answer to watch for.
- **Routing questions**: "how many disputes are still open" must go to SQL. The test
  is which path the router chose, not the number.
- **Refusal questions**: things deliberately absent from the corpus, where the correct
  behaviour is saying nothing was found.

That last category is worth building deliberately. A system that never refuses will
score well on the other three and be untrustworthy in use.

### Ingest run

```
python scripts/practice_db.py --scale 50 --postgres "$RAG_PG_URL"
python eval/generate_corpus.py --scale 50 --out corpus/
python -m app.ingest.pipeline corpus/ docs/
python eval/run_eval.py --mode keyword          # the baseline
```

Ingest runs offline, per limit 2. Embeddings are computed on a laptop or in CI and
written to Postgres; the cluster only ever reads them.

Run the keyword baseline **before** building vector search, and write the number
down. Everything after that is measured against it.
