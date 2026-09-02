-- Local schema. Mirrors 001_schema.sql, minus the vector column.
--
-- This exists so the keyword baseline (build order step 4) can be measured
-- with no Postgres, no pgvector and no cluster. That baseline is the number
-- everything else gets compared against, so it must be cheap to reproduce.

CREATE TABLE IF NOT EXISTS documents (
    id              TEXT PRIMARY KEY,
    doc_type        TEXT NOT NULL,          -- platform_doc | narrative | sop | circular
    title           TEXT NOT NULL,
    source_uri      TEXT NOT NULL,
    source          TEXT NOT NULL,          -- repo | sim | generated
    product         TEXT,
    jurisdiction    TEXT,

    -- The compliance control. A superseded document stays indexed so we can
    -- prove the filter works, and is excluded by predicate, never by deletion.
    status          TEXT NOT NULL DEFAULT 'current',   -- current | superseded | draft
    effective_from  TEXT,
    effective_to    TEXT,
    superseded_by   TEXT REFERENCES documents(id),

    content_hash    TEXT NOT NULL,
    ingested_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id            TEXT PRIMARY KEY,
    document_id   TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal       INTEGER NOT NULL,
    section_path  TEXT,                     -- what a citation actually names
    text          TEXT NOT NULL,
    token_estimate INTEGER NOT NULL,
    content_hash  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS ix_documents_source ON documents(source);
CREATE INDEX IF NOT EXISTS ix_documents_type   ON documents(doc_type);
CREATE INDEX IF NOT EXISTS ix_chunks_document  ON chunks(document_id);

-- External-content FTS: the index points at chunks rather than copying it,
-- so there is one copy of the text and no way for the two to disagree.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    section_path,
    content='chunks',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, section_path)
    VALUES (new.rowid, new.text, new.section_path);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, section_path)
    VALUES ('delete', old.rowid, old.text, old.section_path);
END;
