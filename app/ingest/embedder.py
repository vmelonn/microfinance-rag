"""
Offline embedding. Reads chunks with no vector, writes one back.

    python -m app.ingest.embedder --index index.db
    python -m app.ingest.embedder --index index.db --model BAAI/bge-m3 --device cuda

RUNS OFFLINE, DELIBERATELY. The cluster never loads a model: the namespace is
already carrying twelve workloads under a tight quota, and a sentence-transformers
container wanting 1 to 2Gi resident does not fit (PLAN.md limit 2). So vectors
are computed here and only ever read there.

MODEST BY DEFAULT. Small batches, fp16 on GPU, and the cache is released after
every batch. On an 8GB card this sits around 1.2GB for bge-m3 and leaves the
display alone. If that is still more than you want, --device cpu costs minutes
once rather than seconds, which for a one-off ingest is a fine trade.

The default model is small on purpose so the pipeline can be proven without a
2.2GB download. bge-m3 is the production choice: 1024 dimensions, matching the
Postgres schema, and multilingual for Roman Urdu in ticket text.
"""

from __future__ import annotations

import argparse
import array
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"     # 33M params, ~130MB, 384 dims
PRODUCTION_MODEL = "BAAI/bge-m3"             # 568M params, ~2.2GB, 1024 dims


def ensure_column(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    if "embedding" not in cols:
        conn.execute("ALTER TABLE chunks ADD COLUMN embedding BLOB")
    if "embedding_model" not in cols:
        conn.execute("ALTER TABLE chunks ADD COLUMN embedding_model TEXT")
    conn.commit()


def pack(vec) -> bytes:
    return array.array("f", vec).tobytes()


def unpack(blob: bytes):
    a = array.array("f")
    a.frombytes(blob)
    return a


def pick_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def run(index: str, model_name: str, device: str, batch: int, redo: bool) -> int:
    conn = sqlite3.connect(index)
    ensure_column(conn)

    where = "" if redo else "WHERE embedding IS NULL"
    rows = conn.execute(
        "SELECT id, text FROM chunks %s ORDER BY id" % where).fetchall()
    if not rows:
        print("nothing to embed; every chunk already has a vector")
        return 0

    device = pick_device(device)
    print("model  %s" % model_name)
    print("device %s" % device)
    print("chunks %d, batch %d" % (len(rows), batch))

    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name, device=device)
    if device == "cuda":
        model = model.half()          # fp16 halves resident memory

    dim = model.get_sentence_embedding_dimension()
    print("dims   %d" % dim)

    started = time.monotonic()
    done = 0
    for i in range(0, len(rows), batch):
        window = rows[i:i + batch]
        vecs = model.encode([t for _, t in window],
                            batch_size=batch, normalize_embeddings=True,
                            show_progress_bar=False)
        conn.executemany(
            "UPDATE chunks SET embedding = ?, embedding_model = ? WHERE id = ?",
            [(pack(v), model_name, cid) for (cid, _), v in zip(window, vecs)])
        conn.commit()
        done += len(window)

        if device == "cuda":
            # Release between batches rather than at the end, so the peak is
            # one batch and not the whole run.
            import torch
            torch.cuda.empty_cache()

        if done % (batch * 10) == 0 or done == len(rows):
            el = time.monotonic() - started
            print("  %d/%d  %.0f chunks/s" % (done, len(rows), done / max(el, 1e-6)))

    el = time.monotonic() - started
    print("embedded %d chunks in %.1fs (%.0f/s), %d dims"
          % (done, el, done / max(el, 1e-6), dim))
    conn.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--index", default="index.db")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    p.add_argument("--batch", type=int, default=16,
                   help="small on purpose; raise it if the card is idle")
    p.add_argument("--redo", action="store_true", help="re-embed everything")
    a = p.parse_args()
    return run(a.index, a.model, a.device, a.batch, a.redo)


if __name__ == "__main__":
    raise SystemExit(main())
