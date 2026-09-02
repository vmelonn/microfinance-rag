"""
Cross-encoder reranking. Retrieve wide, then read properly.

WHY THIS IS DIFFERENT FROM RETRIEVAL. BM25 and an embedding both score the
query and the chunk *independently* and then compare the two summaries. A
cross-encoder reads the pair together, so it can notice that "solvency
invariant" and "wallet balance went negative" describe the same condition even
though they share no vocabulary and sit apart in embedding space. That is the
class of miss neither retrieval mode could close.

The cost is that it cannot be precomputed. Every (query, chunk) pair is a
forward pass, which is why it runs over 30 candidates and not over the corpus.
Retrieval decides what is plausible; reranking decides what is right.

MEASURED, AND IT DID NOT HELP HERE. The default MS MARCO model dropped
documented retrieval@5 from 91.7% to 79.2% on this corpus. MS MARCO is web
search passages; this corpus is numbered procedures with heading trails. A
cross-encoder carries a trained notion of relevance, and an out-of-domain one
reorders confidently and wrongly. Reach for the production model, or leave
reranking off, but do not assume it is a free improvement.

MODEST BY DEFAULT, like the embedder: small batches, fp16 on GPU, cache
released after. The default model is small so the pipeline can be proven
without a large download; bge-reranker-v2-m3 is the production choice.
"""

from __future__ import annotations

from app.retrieve.store import Hit

DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"   # 22M params, ~90MB
PRODUCTION_MODEL = "BAAI/bge-reranker-v2-m3"             # 568M params, ~2.2GB


class Reranker:
    def __init__(self, model_name: str = DEFAULT_MODEL, device: str = "auto",
                 batch: int = 16):
        from app.ingest.embedder import pick_device
        from sentence_transformers import CrossEncoder

        self.device = pick_device(device)
        self.batch = batch
        self.model_name = model_name
        self.model = CrossEncoder(model_name, device=self.device)

    def rerank(self, query: str, hits: list[Hit], *, k: int = 5) -> list[Hit]:
        if not hits:
            return []

        pairs = [(query, h.text) for h in hits]
        scores = self.model.predict(pairs, batch_size=self.batch,
                                    show_progress_bar=False)

        if self.device == "cuda":
            import torch
            torch.cuda.empty_cache()

        # The rerank score replaces the retrieval score. Keeping the old one
        # would invite someone to blend them, and blending a probability with a
        # BM25 value is the scale mistake RRF exists to avoid.
        for h, s in zip(hits, scores):
            h.score = float(s)

        return sorted(hits, key=lambda h: h.score, reverse=True)[:k]
