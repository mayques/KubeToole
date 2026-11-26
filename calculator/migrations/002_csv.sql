CREATE TABLE uploads (
    id UUID PRIMARY KEY, user_id TEXT NOT NULL REFERENCES accounts,
    name TEXT NOT NULL, size_bytes BIGINT NOT NULL CHECK (size_bytes BETWEEN 1 AND 500000000),
    received_bytes BIGINT NOT NULL DEFAULT 0 CHECK (received_bytes >= 0 AND received_bytes <= size_bytes),
    state TEXT NOT NULL CHECK (state IN ('receiving','uploaded','validating','ready','rejected')),
    job_id UUID UNIQUE REFERENCES jobs,
    rows BIGINT, columns INTEGER, error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX uploads_user_idx ON uploads(user_id, created_at DESC);
CREATE UNIQUE INDEX one_upload_per_user ON uploads(user_id)
    WHERE state IN ('receiving','uploaded','validating');
ALTER TABLE artifacts ADD COLUMN csv_checked BOOLEAN NOT NULL DEFAULT FALSE;
CREATE TABLE download_tickets (
    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES accounts,
    kind TEXT NOT NULL CHECK (kind IN ('upload','artifact')), file_id UUID NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '5 minutes'
);
CREATE INDEX download_expiry_idx ON download_tickets(expires_at);
