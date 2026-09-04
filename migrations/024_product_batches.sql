-- Keep purchase lots separate when their selling or cost prices differ.
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS selling_price NUMERIC(12, 2);

CREATE TABLE IF NOT EXISTS product_batches (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organisation_id BIGINT NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    purchase_id BIGINT REFERENCES purchases(id) ON DELETE SET NULL,
    quantity_remaining NUMERIC(12, 2) NOT NULL CHECK (quantity_remaining >= 0),
    unit_cost NUMERIC(12, 2) NOT NULL CHECK (unit_cost >= 0),
    selling_price NUMERIC(12, 2) NOT NULL CHECK (selling_price >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS product_batches_available_idx
    ON product_batches (product_id, created_at) WHERE quantity_remaining > 0;

ALTER TABLE sale_items ADD COLUMN IF NOT EXISTS product_batch_id BIGINT REFERENCES product_batches(id) ON DELETE SET NULL;

INSERT INTO product_batches (organisation_id, product_id, quantity_remaining, unit_cost, selling_price)
SELECT organisation_id, id, stock_quantity, cost_price, selling_price
FROM products
WHERE stock_quantity > 0
  AND NOT EXISTS (SELECT 1 FROM product_batches b WHERE b.product_id = products.id);
