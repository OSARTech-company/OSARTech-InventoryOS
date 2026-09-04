ALTER TABLE product_batches ADD COLUMN IF NOT EXISTS batch_code TEXT;
ALTER TABLE product_batches ADD COLUMN IF NOT EXISTS expiry_date DATE;
ALTER TABLE sale_items ADD COLUMN IF NOT EXISTS base_quantity NUMERIC(12, 4);

CREATE TABLE IF NOT EXISTS sale_returns (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organisation_id BIGINT NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    sale_item_id BIGINT NOT NULL REFERENCES sale_items(id) ON DELETE RESTRICT,
    product_batch_id BIGINT NOT NULL REFERENCES product_batches(id) ON DELETE RESTRICT,
    quantity NUMERIC(12, 4) NOT NULL CHECK (quantity > 0),
    base_quantity NUMERIC(12, 4) NOT NULL CHECK (base_quantity > 0),
    reason TEXT NOT NULL,
    created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS product_batches_expiry_idx
    ON product_batches (organisation_id, expiry_date) WHERE quantity_remaining > 0 AND expiry_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS sale_returns_sale_item_idx ON sale_returns (sale_item_id);
