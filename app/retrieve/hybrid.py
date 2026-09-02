"""
Vector search and fusion with the keyword baseline.

BRUTE FORCE ON PURPOSE. At a few thousand chunks an exact scan over normalised
vectors takes single-digit milliseconds, and it is exactly right rather than
approximately right. HNSW earns its keep somewhere north of a hundred thousand
vectors; below that it adds a build step, a tuning surface and a recall cliff
in exchange for nothing measurable. pgvector gets the HNSW index in production
because the corpus there will be larger, not because this one needs it.

FUSION IS RECIPROCAL RANK, NOT SCORE. BM25 scores and cosine similarities live
on different scales that also shift per query, so adding or averaging them
weights whichever happens to be numerically larger. RRF only looks at position,
which is the one thing the two rankings genuinely share.
"""

from __future__ import annotations

import array
import sqlite3
from dataclasses import dataclass

from app.retrieve.store import Hit, Store, content_terms

RRF_K = 60          # the usual constant; damps the top rank's dominance


def _unpack(blob: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(blob)
    return a


@dataclass
class Scored:
    hit: Hit
    bm25_rank: int | None = None
    vec_rank: int | None = None
    rrf: float = 0.0


class Hybrid:
    def __init__(self, index_path: str, encoder=None):
        self.store = Store(index_path)
        self.conn = sqlite3.connect("file:%s?mode=ro" % index_path, uri=True)
        self.conn.row_factory = sqlite3.Row
        self.encoder = encoder          # anything with .encode([str]) -> vectors
        self._matrix_cache: dict = {}

    # ------------------------------------------------------------------ vector

    def _load_matrix(self, current_only: bool,
                     exclude_sources: tuple[str, ...]) -> tuple:
        """
        Read every eligible vector once into one numpy matrix, and keep it.

        The first version scored chunk by chunk in a Python loop, which was
        invisible at 258 chunks and unusable at 50,000: the inner product is
        384 multiplies, so a query became roughly 19 million Python-level
        operations. As one matrix multiply it is milliseconds, and the memory
        is trivial (50k x 384 float32 is about 77MB).

        Cached per filter combination, because the filter changes which rows
        are eligible and a cache that ignored it would silently answer the
        wrong question.
        """
        key = (current_only, exclude_sources)
        if key in self._matrix_cache:
            return self._matrix_cache[key]

        import numpy as np

        where = ["k.embedding IS NOT NULL"]
        params: list = []
        if current_only:
            where.append("d.status = 'current'")
        if exclude_sources:
            where.append("d.source NOT IN (%s)" % ",".join("?" * len(exclude_sources)))
            params += list(exclude_sources)

        sql = """SELECT k.id, k.document_id, d.doc_type, d.title, d.source_uri,
                        k.section_path, k.text, k.embedding
                 FROM chunks k JOIN documents d ON d.id = k.document_id
                 WHERE %s""" % " AND ".join(where)

        rows = self.conn.execute(sql, params).fetchall()
        if not rows:
            empty = (np.zeros((0, 1), dtype="float32"), [])
            self._matrix_cache[key] = empty
            return empty

        mat = np.vstack([np.frombuffer(r["embedding"], dtype="float32")
                         for r in rows])
        # Vectors are written normalised, so a dot product is the cosine.
        # Re-normalising guards against an un-normalised vector ever being
        # written and silently skewing every score.
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        mat = mat / norms

        self._matrix_cache[key] = (mat, rows)
        return mat, rows

    def vector_search(self, query: str, *, k: int = 10,
                      current_only: bool = True,
                      exclude_sources: list[str] | None = None) -> list[Hit]:
        if self.encoder is None:
            return []

        import numpy as np

        mat, rows = self._load_matrix(current_only, tuple(exclude_sources or ()))
        if not rows:
            return []

        qv = np.asarray(self.encoder.encode([query], normalize_embeddings=True)[0],
                        dtype="float32")
        n = np.linalg.norm(qv)
        if n:
            qv = qv / n

        sims = mat @ qv
        top = np.argpartition(-sims, min(k, len(sims) - 1))[:k]
        top = top[np.argsort(-sims[top])]

        terms = content_terms(query)
        out = []
        for i in top:
            r = rows[int(i)]
            body = r["text"].lower()
            cov = (sum(1 for t in terms if t in body) / len(terms)) if terms else 0.0
            out.append(Hit(chunk_id=r["id"], document_id=r["document_id"],
                           doc_type=r["doc_type"], title=r["title"],
                           source_uri=r["source_uri"],
                           section_path=r["section_path"] or "",
                           text=r["text"], score=float(sims[i]), coverage=cov))
        return out

    # ------------------------------------------------------------------ fusion

    def search(self, query: str, *, k: int = 5, pool: int = 30,
               as_of: str | None = None, current_only: bool = True,
               exclude_sources: list[str] | None = None,
               mode: str = "hybrid") -> list[Hit]:
        if mode == "keyword":
            return self.store.search(query, k=k, as_of=as_of,
                                     current_only=current_only,
                                     exclude_sources=exclude_sources)
        if mode == "vector":
            return self.vector_search(query, k=k, current_only=current_only,
                                      exclude_sources=exclude_sources)

        kw = self.store.search(query, k=pool, as_of=as_of,
                               current_only=current_only,
                               exclude_sources=exclude_sources)
        vec = self.vector_search(query, k=pool, current_only=current_only,
                                 exclude_sources=exclude_sources)

        merged: dict[str, Scored] = {}
        for rank, h in enumerate(kw, start=1):
            merged.setdefault(h.chunk_id, Scored(hit=h)).bm25_rank = rank
        for rank, h in enumerate(vec, start=1):
            s = merged.setdefault(h.chunk_id, Scored(hit=h))
            s.vec_rank = rank

        for s in merged.values():
            s.rrf = sum(1.0 / (RRF_K + r)
                        for r in (s.bm25_rank, s.vec_rank) if r is not None)

        ranked = sorted(merged.values(), key=lambda s: s.rrf, reverse=True)
        return [s.hit for s in ranked[:k]]

    def close(self) -> None:
        self.store.close()
        self.conn.close()


def load_encoder(model_name: str, device: str = "auto"):
    """Imported lazily so the keyword path never pays for torch."""
    from app.ingest.embedder import pick_device
    from sentence_transformers import SentenceTransformer

    dev = pick_device(device)
    model = SentenceTransformer(model_name, device=dev)
    if dev == "cuda":
        model = model.half()
    return model
