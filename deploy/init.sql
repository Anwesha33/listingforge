-- Schema for ListingForge.
--
-- The catalog service owns these tables; the moderation service reads the
-- embedding column for duplicate detection. That shared-table coupling is a
-- deliberate simplification for a single-repo demo and is called out in the
-- README as the first thing to split if this grew.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS listings (
    id                BIGSERIAL PRIMARY KEY,

    -- Natural key from the seller's own system. Ingestion is idempotent on
    -- this pair: a retried upload updates the existing row rather than
    -- creating a second listing, which is what makes at-least-once delivery
    -- from the seller's side safe.
    seller_id         TEXT        NOT NULL,
    seller_sku        TEXT        NOT NULL,

    raw_title         TEXT        NOT NULL,
    raw_description   TEXT,

    -- Populated by the enrichment worker.
    normalized_title  TEXT,
    description       TEXT,
    category          TEXT,
    attributes        JSONB       NOT NULL DEFAULT '{}'::jsonb,

    status            TEXT        NOT NULL DEFAULT 'DRAFT',
    review_reasons    JSONB       NOT NULL DEFAULT '[]'::jsonb,

    -- Set when moderation finds a near-duplicate already in the catalog.
    duplicate_of      BIGINT      REFERENCES listings(id),
    duplicate_score   REAL,

    -- Optimistic locking. Two workers can finish out of order; the version
    -- check is what stops a slow enrichment result from overwriting a newer
    -- moderation decision.
    version           INTEGER     NOT NULL DEFAULT 0,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT listings_seller_sku_unique UNIQUE (seller_id, seller_sku),
    CONSTRAINT listings_status_valid CHECK (
        status IN ('DRAFT','ENRICHING','ENRICHED','NEEDS_REVIEW','PUBLISHED','REJECTED','FAILED')
    )
);

-- Embeddings live in their own table so the vector index does not bloat the
-- row store, and so the embedding model version can change without a
-- destructive migration of the listings table.
CREATE TABLE IF NOT EXISTS listing_embeddings (
    listing_id  BIGINT PRIMARY KEY REFERENCES listings(id) ON DELETE CASCADE,
    model       TEXT   NOT NULL,
    embedding   vector(768) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS listings_status_idx  ON listings (status);
CREATE INDEX IF NOT EXISTS listings_seller_idx  ON listings (seller_id);

-- HNSW over cosine distance.
--
-- IVFFlat was the first choice here and was wrong in a way worth recording.
-- IVFFlat is an approximate index that partitions vectors into `lists`
-- centroids and, by default, probes exactly one of them. Built at bootstrap on
-- an empty table its centroids are meaningless, and with lists=100 over a small
-- catalogue the single probed list is usually empty -- so the duplicate query
-- returned zero rows while an exact scan found a 0.93-similarity match. A
-- duplicate check that silently finds nothing is the worst failure this service
-- has, because it looks exactly like "no duplicates exist".
--
-- HNSW needs no training pass, is built incrementally as rows arrive, and
-- behaves correctly from the first row onwards. It costs more memory and a
-- slower build, which is the right trade for a correctness property.
CREATE INDEX IF NOT EXISTS listing_embeddings_vec_idx
    ON listing_embeddings USING hnsw (embedding vector_cosine_ops);

-- Audit trail of every state transition. A moderation decision that cannot be
-- explained after the fact is not worth much to the humans reviewing it.
CREATE TABLE IF NOT EXISTS listing_events (
    id          BIGSERIAL PRIMARY KEY,
    listing_id  BIGINT      NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT        NOT NULL,
    actor       TEXT        NOT NULL,
    detail      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS listing_events_listing_idx ON listing_events (listing_id, created_at);
