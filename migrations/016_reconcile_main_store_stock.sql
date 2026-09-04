-- Before store-specific updates were introduced, sales and movements changed only
-- products.stock_quantity. Main Store is reconciled once to that existing balance.
UPDATE store_inventory si
SET quantity = GREATEST(p.stock_quantity - COALESCE(other_stores.quantity, 0), 0), updated_at = NOW()
FROM products p
JOIN stores s ON s.organisation_id = p.organisation_id AND s.name = 'Main Store'
LEFT JOIN (
    SELECT product_id, SUM(quantity) AS quantity
    FROM store_inventory
    WHERE store_id NOT IN (SELECT id FROM stores WHERE name = 'Main Store')
    GROUP BY product_id
) other_stores ON other_stores.product_id = p.id
WHERE si.store_id = s.id AND si.product_id = p.id;
