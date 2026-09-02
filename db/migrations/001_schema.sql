-- Postgres schema. The deployment target.
--
-- Requires pgvector, so this runs against pgvector/pgvector:pg16 and NOT
-- against the platform's postgres:16-alpine, which has no vector extension.
-- See PLAN.md limit 3: stand up a second Postgres rather than swapping the
-- image underneath the ledger.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id              TEXT PRIMARY KEY,
    doc_type        TEXT NOT NULL,
    title           TEXT NOT NULL,
    source_uri      TEXT NOT NULL,
    source          TEXT NOT NULL,
    product         TEXT,
    jurisdiction    TEXT,

    -- Filtered on before similarity is ever computed. Superseded rows stay
    -- indexed deliberately, so the eval can prove the filter is doing its job.
    status          TEXT NOT NULL DEFAULT 'current',
    effective_from  DATE,
    effective_to    DATE,
    superseded_by   TEXT REFERENCES documents(id),

    content_hash    TEXT NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
    id             TEXT PRIMARY KEY,
    document_id    TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal        INTEGER NOT NULL,
    section_path   TEXT,
    text           TEXT NOT NULL,
    token_estimate INTEGER NOT NULL,
    content_hash   TEXT NOT NULL,

    -- Written offline. The cluster only ever reads this column, because a
    -- sentence-transformers container does not fit the namespace quota.
    embedding      vector(1024),

    tsv            tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);

CREATE INDEX IF NOT EXISTS ix_documents_status  ON documents(status);
CREATE INDEX IF NOT EXISTS ix_documents_source  ON documents(source);
CREATE INDEX IF NOT EXISTS ix_documents_dates   ON documents(effective_from, effective_to);
CREATE INDEX IF NOT EXISTS ix_chunks_document   ON chunks(document_id);

CREATE INDEX IF NOT EXISTS ix_chunks_tsv ON chunks USING GIN (tsv);

-- Built after the first bulk load, not before: HNSW on an empty table then
-- filled row by row is far slower than filling then indexing.
CREATE INDEX IF NOT EXISTS ix_chunks_embedding ON chunks
    USING hnsw (embedding vector_cosine_ops);
