CREATE TABLE IF NOT EXISTS stock_closings (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organisation_id BIGINT NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    store_id BIGINT NOT NULL REFERENCES stores(id) ON DELETE RESTRICT,
    closing_date DATE NOT NULL DEFAULT CURRENT_DATE,
    notes TEXT,
    closed_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS stock_closing_items (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    closing_id BIGINT NOT NULL REFERENCES stock_closings(id) ON DELETE CASCADE,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    expected_quantity NUMERIC(12, 2) NOT NULL,
    counted_quantity NUMERIC(12, 2) NOT NULL CHECK (counted_quantity >= 0),
    difference_quantity NUMERIC(12, 2) NOT NULL,
    difference_reason TEXT
);

CREATE INDEX IF NOT EXISTS stock_closings_organisation_date_idx ON stock_closings (organisation_id, closing_date DESC);
