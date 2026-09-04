ALTER TABLE sale_items ADD COLUMN IF NOT EXISTS cost_unit_price NUMERIC(12, 2);

-- Historical sales did not preserve cost at sale time. This is the best
-- available starting estimate, and new sales save their own immutable snapshot.
UPDATE sale_items si
SET cost_unit_price = p.cost_price
FROM products p
WHERE si.product_id = p.id AND si.cost_unit_price IS NULL;
