CREATE TABLE IF NOT EXISTS product_units (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    quantity_in_base NUMERIC(12, 4) NOT NULL CHECK (quantity_in_base > 0),
    selling_price NUMERIC(12, 2) NOT NULL CHECK (selling_price >= 0),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT product_units_product_label_unique UNIQUE (product_id, label)
);

ALTER TABLE sale_items ADD COLUMN IF NOT EXISTS unit_label TEXT;

INSERT INTO product_units (product_id, label, quantity_in_base, selling_price)
SELECT id, unit, 1, selling_price FROM products
ON CONFLICT (product_id, label) DO NOTHING;

UPDATE sale_items si SET unit_label = p.unit FROM products p
WHERE si.product_id = p.id AND si.unit_label IS NULL;
