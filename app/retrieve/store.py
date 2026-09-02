"""
Retrieval against the local index.

Keyword only for now, on purpose. It is the baseline in build order step 4, and
every later change is measured against the number it produces. Adding vectors
before that number exists means never knowing whether they helped.

THE FILTER RUNS FIRST. Status and effective date are SQL predicates applied
before ranking, not a post-filter over results. A post-filter that removes three
of five hits leaves two, and the caller silently gets a worse answer with no
signal. A pre-filter changes what was ever eligible.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date


@dataclass
class Hit:
    chunk_id: str
    document_id: str
    doc_type: str
    title: str
    source_uri: str
    section_path: str
    text: str
    score: float
    coverage: float = 0.0      # share of query terms actually present

    def cite(self) -> str:
        return "%s :: %s" % (self.source_uri, self.section_path or "(top)")


_TOKEN = re.compile(r"[A-Za-z0-9_]+")

# Coverage is computed over content words only. Without this, "what is our
# policy on annual leave" scores 0.57 because it shares "what is our on" with
# half the corpus, which is indistinguishable from a real question. Stripping
# filler is what makes the two populations separate.
STOP = frozenset("""
a an the this that these those and or but if then than of in on at to from by
for with without into over under is are was were be been being do does did
done have has had having i we you he she it they them his her our your their
what which who whom whose when where why how do
can could should would may might must shall will
not no nor only own same so too very s t just my me us am our ours
""".split())


def content_terms(query: str) -> set[str]:
    return {t.lower() for t in _TOKEN.findall(query)
            if len(t) > 1 and t.lower() not in STOP}


def to_match(query: str) -> str:
    """
    FTS5 MATCH is a query language, not a string. An apostrophe or a stray
    bracket from a user question is a syntax error, and a hyphen silently
    becomes NOT. So we tokenise and quote every term, which loses phrase
    search and gains never crashing on real input.
    """
    terms = [t for t in _TOKEN.findall(query) if len(t) > 1]
    if not terms:
        return '""'
    return " OR ".join('"%s"' % t for t in terms)


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        self.conn.row_factory = sqlite3.Row

    def search(self, query: str, *, k: int = 10,
               doc_types: list[str] | None = None,
               exclude_sources: list[str] | None = None,
               as_of: str | None = None,
               current_only: bool = True) -> list[Hit]:
        where = ["chunks_fts MATCH ?"]
        params: list = [to_match(query)]

        if current_only:
            # The compliance control. A superseded circular is topically perfect
            # and wrong, so it must never be eligible, not merely outranked.
            where.append("d.status = 'current'")

        if as_of:
            where.append("(d.effective_from IS NULL OR d.effective_from <= ?)")
            params.append(as_of)
            where.append("(d.effective_to IS NULL OR d.effective_to > ?)")
            params.append(as_of)

        if doc_types:
            where.append("d.doc_type IN (%s)" % ",".join("?" * len(doc_types)))
            params += doc_types

        if exclude_sources:
            where.append("d.source NOT IN (%s)" % ",".join("?" * len(exclude_sources)))
            params += exclude_sources

        sql = """
            SELECT k.id, k.document_id, d.doc_type, d.title, d.source_uri,
                   k.section_path, k.text, bm25(chunks_fts) AS score
            FROM chunks_fts f
            JOIN chunks k    ON k.rowid = f.rowid
            JOIN documents d ON d.id = k.document_id
            WHERE %s
            ORDER BY score
            LIMIT ?
        """ % " AND ".join(where)
        params.append(k)

        # Coverage, not score, is what separates a real question from a junk one.
        #
        # Measured on this corpus: BM25 alone gives real questions 6.38 to 9.07
        # and junk questions 4.67 to 6.90, which overlap, so no floor on score
        # can split them. The cause is that terms are OR-ed, so a question about
        # office printers matches "office" somewhere and scores respectably.
        #
        # Coverage asks a different question: of the words you asked with, how
        # many are actually here? A real question lands most of them, a junk one
        # lands one or two. That separates cleanly where the score does not.
        terms = content_terms(query)

        hits = []
        for r in self.conn.execute(sql, params):
            body = r["text"].lower()
            cov = (sum(1 for t in terms if t in body) / len(terms)) if terms else 0.0
            hits.append(Hit(chunk_id=r["id"], document_id=r["document_id"],
                            doc_type=r["doc_type"], title=r["title"],
                            source_uri=r["source_uri"],
                            section_path=r["section_path"] or "",
                            text=r["text"], score=-float(r["score"]), coverage=cov))
        return hits

    def close(self) -> None:
        self.conn.close()


def today() -> str:
    return date.today().isoformat()
