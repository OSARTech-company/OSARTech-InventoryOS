CREATE TABLE IF NOT EXISTS customers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organisation_id BIGINT NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    phone TEXT,
    email TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT customers_organisation_phone_unique UNIQUE (organisation_id, phone)
);

ALTER TABLE sales ADD COLUMN IF NOT EXISTS customer_id BIGINT
    REFERENCES customers(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS customers_organisation_id_idx ON customers (organisation_id);
CREATE INDEX IF NOT EXISTS sales_customer_id_idx ON sales (customer_id);
