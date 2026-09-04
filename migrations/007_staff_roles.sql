ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role IN ('admin', 'owner', 'manager', 'salesperson', 'cashier'));

CREATE INDEX IF NOT EXISTS users_organisation_role_idx
    ON users (organisation_id, role);
