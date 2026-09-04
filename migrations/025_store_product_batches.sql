-- A batch belongs to one store so checkout cannot sell a lot held elsewhere.
ALTER TABLE product_batches ADD COLUMN IF NOT EXISTS store_id BIGINT REFERENCES stores(id) ON DELETE CASCADE;

UPDATE product_batches b
SET store_id = s.id
FROM stores s
WHERE s.organisation_id = b.organisation_id
  AND s.name = 'Main Store'
  AND b.store_id IS NULL;

ALTER TABLE product_batches ALTER COLUMN store_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS product_batches_store_available_idx
    ON product_batches (store_id, product_id, created_at) WHERE quantity_remaining > 0;
