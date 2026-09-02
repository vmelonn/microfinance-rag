"""
Ingest. Reads sources, chunks them, writes documents and chunks.

    python -m app.ingest.pipeline --index index.db \
        --docs ../microfinance-microservices/docs ../microfinance-microservices/README.md \
        --sim sim.db

Idempotent on content hash: re-running over an unchanged file replaces nothing
and reports it as skipped, so this can be run on a loop while the simulator is
still writing.

No embeddings here. Vectors are computed by a separate offline step because the
namespace cannot host a model (PLAN.md limit 2), and because the keyword
baseline in build order step 4 must be reproducible without one.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.ingest.chunker import chunk_html, chunk_markdown, chunk_record  # noqa: E402

SCHEMA_SQL = Path(__file__).resolve().parents[2] / "db" / "migrations" / "001_schema_sqlite.sql"


def doc_id(source_uri: str) -> str:
    return hashlib.sha256(source_uri.encode("utf-8")).hexdigest()[:16]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.commit()
    return conn


class Writer:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.added = self.skipped = self.chunks = 0

    def put(self, *, source_uri: str, title: str, doc_type: str, source: str,
            chunks: list, status: str = "current", product=None,
            effective_from=None, effective_to=None, superseded_by=None) -> None:
        if not chunks:
            return
        did = doc_id(source_uri)
        digest = hashlib.sha256(
            "".join(c.content_hash for c in chunks).encode("utf-8")).hexdigest()[:16]

        row = self.conn.execute(
            "SELECT content_hash FROM documents WHERE id = ?", (did,)).fetchone()
        if row and row[0] == digest:
            self.skipped += 1
            return

        cur = self.conn.cursor()
        if row:
            cur.execute("DELETE FROM chunks WHERE document_id = ?", (did,))
            cur.execute("DELETE FROM documents WHERE id = ?", (did,))

        cur.execute(
            """INSERT INTO documents (id, doc_type, title, source_uri, source, product,
                   jurisdiction, status, effective_from, effective_to, superseded_by,
                   content_hash, ingested_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (did, doc_type, title, source_uri, source, product, None, status,
             effective_from, effective_to, superseded_by, digest,
             datetime.now(timezone.utc).isoformat()))

        cur.executemany(
            """INSERT INTO chunks (id, document_id, ordinal, section_path, text,
                   token_estimate, content_hash)
               VALUES (?,?,?,?,?,?,?)""",
            [("%s-%04d" % (did, c.ordinal), did, c.ordinal, c.section_path,
              c.text, c.token_estimate, c.content_hash) for c in chunks])

        self.added += 1
        self.chunks += len(chunks)


def classify(path: Path) -> tuple[str, str, str]:
    """
    (doc_type, source, uri) from where the file sits.

    Generated documents are addressed relative to corpus/, so a citation reads
    `sop/SOP-ORPHAN_SWITCH.md` on any machine. Absolute paths would make the
    evaluation set machine-specific, which defeats the point of it.
    """
    parts = [p.lower() for p in path.parts]
    if "sop" in parts:
        return "sop", "generated", "sop/%s" % path.name
    if "circular" in parts:
        return "circular", "generated", "circular/%s" % path.name
    return "platform_doc", "repo", ""


META = re.compile(r"\*\*(Status|Effective from|Effective to|Replaced by|Replaces):\*\*\s*"
                  r"([^|\n]+)")


def circular_meta(md: str) -> dict:
    """
    Read the header a circular carries. Without this every circular lands as
    'current' and the supersession filter has nothing to filter on, which is
    exactly the failure the corpus was built to catch.
    """
    found = {k.lower(): v.strip() for k, v in META.findall(md)}
    status = "superseded" if found.get("status", "").upper().startswith("SUPERSEDED") \
        else "current"
    return {
        "status": status,
        "effective_from": found.get("effective from") or None,
        "effective_to": found.get("effective to") or None,
        # stored as a document id so it can be joined, not as a human label
        "superseded_by": (doc_id("circular/%s.md" % found["replaced by"])
                          if found.get("replaced by") else None),
    }


def ingest_file(w: Writer, path: Path, repo_root: Path | None) -> None:
    suffix = path.suffix.lower()
    raw = path.read_text(encoding="utf-8", errors="replace")

    if suffix in (".html", ".htm"):
        chunks = chunk_html(raw)
    elif suffix in (".md", ".markdown", ".txt"):
        chunks = chunk_markdown(raw)
    else:
        return

    doc_type, source, uri = classify(path)
    if not uri:
        try:
            uri = str(path.relative_to(repo_root)).replace("\\", "/") if repo_root \
                else str(path)
        except ValueError:
            uri = str(path)

    extra = circular_meta(raw) if doc_type == "circular" else {}
    title = path.stem.replace("-", " ").replace("_", " ")
    if doc_type in ("sop", "circular"):
        first = raw.lstrip().splitlines()[0] if raw.strip() else ""
        if first.startswith("# "):
            title = first[2:].strip()

    w.put(source_uri=uri, title=title, doc_type=doc_type, source=source,
          chunks=chunks, **extra)


def ingest_sim(w: Writer, sim_db: str, limit: int | None) -> None:
    """Resolution narratives. Tagged source='sim' so the eval can exclude them."""
    src = sqlite3.connect("file:%s?mode=ro" % sim_db, uri=True)
    q = "SELECT narrative_id, title, body, written_at FROM sim_narratives ORDER BY written_at"
    if limit:
        q += " LIMIT %d" % limit
    for nid, title, body, written_at in src.execute(q):
        w.put(source_uri="sim://narrative/%s" % nid, title=title,
              doc_type="narrative", source="sim",
              chunks=chunk_record(title, body), effective_from=written_at[:10])
    src.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Build the retrieval index.")
    p.add_argument("--index", default="index.db")
    p.add_argument("--docs", nargs="*", default=[], help="files or directories")
    p.add_argument("--sim", default=None, help="simulator sqlite file")
    p.add_argument("--sim-limit", type=int, default=None)
    p.add_argument("--repo-root", default=None, help="paths are recorded relative to this")
    args = p.parse_args()

    conn = connect(args.index)
    w = Writer(conn)
    root = Path(args.repo_root).resolve() if args.repo_root else None

    for entry in args.docs:
        path = Path(entry)
        if path.is_dir():
            for f in sorted(path.rglob("*")):
                if f.is_file():
                    ingest_file(w, f, root)
        elif path.is_file():
            ingest_file(w, path, root)
        else:
            print("skipping missing path: %s" % entry, file=sys.stderr)

    if args.sim:
        if os.path.exists(args.sim):
            ingest_sim(w, args.sim, args.sim_limit)
        else:
            print("no simulator db at %s" % args.sim, file=sys.stderr)

    conn.commit()

    docs, chunks = conn.execute(
        "SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM chunks)").fetchone()
    print("added %d documents (%d chunks), skipped %d unchanged"
          % (w.added, w.chunks, w.skipped))
    print("index now holds %d documents, %d chunks" % (docs, chunks))
    for dtype, n, c in conn.execute(
            """SELECT d.doc_type, count(DISTINCT d.id), count(k.id)
               FROM documents d LEFT JOIN chunks k ON k.document_id = d.id
               GROUP BY d.doc_type ORDER BY 3 DESC"""):
        print("  %-14s %5d docs  %6d chunks" % (dtype, n, c))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
