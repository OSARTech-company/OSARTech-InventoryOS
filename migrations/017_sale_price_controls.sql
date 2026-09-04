ALTER TABLE sale_items ADD COLUMN IF NOT EXISTS list_unit_price NUMERIC(12, 2);
ALTER TABLE sale_items ADD COLUMN IF NOT EXISTS discount_amount NUMERIC(12, 2) NOT NULL DEFAULT 0;
ALTER TABLE sale_items ADD COLUMN IF NOT EXISTS price_override_reason TEXT;

UPDATE sale_items
SET list_unit_price = unit_price
WHERE list_unit_price IS NULL;
