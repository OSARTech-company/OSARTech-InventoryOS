CREATE TABLE IF NOT EXISTS store_inventory (
    store_id BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    quantity NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (store_id, product_id)
);

INSERT INTO store_inventory (store_id, product_id, quantity)
SELECT s.id, p.id, p.stock_quantity
FROM products p
JOIN stores s ON s.organisation_id = p.organisation_id AND s.name = 'Main Store'
ON CONFLICT (store_id, product_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS stock_transfers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organisation_id BIGINT NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    from_store_id BIGINT NOT NULL REFERENCES stores(id) ON DELETE RESTRICT,
    to_store_id BIGINT NOT NULL REFERENCES stores(id) ON DELETE RESTRICT,
    quantity NUMERIC(12, 2) NOT NULL CHECK (quantity > 0),
    notes TEXT,
    created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (from_store_id <> to_store_id)
);

CREATE INDEX IF NOT EXISTS store_inventory_product_id_idx ON store_inventory (product_id);
CREATE INDEX IF NOT EXISTS stock_transfers_organisation_created_idx ON stock_transfers (organisation_id, created_at DESC);
