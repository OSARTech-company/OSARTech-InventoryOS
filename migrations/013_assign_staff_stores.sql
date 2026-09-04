UPDATE users u
SET store_id = s.id
FROM stores s
WHERE s.organisation_id = u.organisation_id
  AND u.store_id IS NULL
  AND s.name = 'Main Store';
