CREATE TABLE IF NOT EXISTS organisations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS organisation_id BIGINT
    REFERENCES organisations(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS users_organisation_id_idx
    ON users (organisation_id);
