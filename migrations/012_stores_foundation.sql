CREATE TABLE IF NOT EXISTS stores (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organisation_id BIGINT NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    address TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT stores_organisation_name_unique UNIQUE (organisation_id, name)
);

INSERT INTO stores (organisation_id, name)
SELECT o.id, 'Main Store'
FROM organisations o
WHERE NOT EXISTS (
    SELECT 1 FROM stores s WHERE s.organisation_id = o.id
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS store_id BIGINT
    REFERENCES stores(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS stores_organisation_id_idx ON stores (organisation_id);
CREATE INDEX IF NOT EXISTS users_store_id_idx ON users (store_id);
