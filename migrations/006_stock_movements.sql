CREATE TABLE IF NOT EXISTS stock_movements (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organisation_id BIGINT NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    movement_type TEXT NOT NULL CHECK (movement_type IN ('stock_in', 'sale', 'return', 'adjustment')),
    quantity_change NUMERIC(12, 2) NOT NULL CHECK (quantity_change <> 0),
    notes TEXT,
    created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS stock_movements_organisation_created_idx
    ON stock_movements (organisation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS stock_movements_product_created_idx
    ON stock_movements (product_id, created_at DESC);
