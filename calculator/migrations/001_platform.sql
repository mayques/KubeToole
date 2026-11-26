-- Additive migration: retain the existing calculation history and IDs.
CREATE TABLE IF NOT EXISTS calculations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id TEXT NOT NULL, expression TEXT NOT NULL,
    result DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS calculations_user_id_idx ON calculations (user_id, created_at DESC);
CREATE TABLE accounts (
    user_id TEXT PRIMARY KEY,
    used_bytes BIGINT NOT NULL DEFAULT 0 CHECK (used_bytes >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Logical calculator payload: UTF-8 expression plus eight bytes for its numeric result.
INSERT INTO accounts (user_id, used_bytes)
SELECT user_id, SUM(octet_length(expression) + 8) FROM calculations GROUP BY user_id;
CREATE TABLE jobs (
    id UUID PRIMARY KEY, user_id TEXT NOT NULL REFERENCES accounts,
    tool_id TEXT NOT NULL, inputs JSONB NOT NULL,
    input_bytes BIGINT NOT NULL CHECK (input_bytes >= 0),
    token_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('launching','pending','running','cancelling','succeeded','failed','cancelled')),
    outcome TEXT CHECK (outcome IN ('succeeded','failed')),
    result JSONB, result_bytes BIGINT NOT NULL DEFAULT 0 CHECK (result_bytes >= 0),
    error TEXT, cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    claimed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), finished_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX one_active_job_per_user ON jobs(user_id)
WHERE state IN ('launching','pending','running','cancelling');
CREATE TABLE artifacts (
    id UUID PRIMARY KEY, job_id UUID NOT NULL REFERENCES jobs,
    name TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('output','temp')),
    reserved_bytes BIGINT NOT NULL DEFAULT 0 CHECK (reserved_bytes >= 0),
    size_bytes BIGINT, complete BOOLEAN NOT NULL DEFAULT FALSE,
    CHECK (size_bytes IS NULL OR size_bytes BETWEEN 0 AND reserved_bytes)
);
CREATE INDEX artifacts_job_idx ON artifacts(job_id);
CREATE TABLE inline_results (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES accounts, tool_id TEXT NOT NULL,
    result JSONB NOT NULL, size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
