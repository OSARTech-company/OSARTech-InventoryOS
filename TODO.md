# InventoryOS Roadmap

## Completed foundation

- [x] Landing page and responsive InventoryOS login design
- [x] PostgreSQL connection configuration via `.env`
- [x] Initial database schema for `users` and `access_requests`
- [x] Password-hash login flow, sessions, logout, and protected dashboard route
- [x] Request Access form that saves submissions to PostgreSQL

## Do next: access management

- [ ] Create an administrator role and an initial administrator account
- [ ] Build an admin page to view pending access requests
- [ ] Add approve and reject actions for access requests
- [ ] Create a user account when an access request is approved
- [ ] Display request status, applicant details, and submission date
- [ ] Prevent duplicate pending requests for the same email address
- [ ] Display total number of Buisness Owner active, inactive, available

## Authentication and security

- [✓] Build the Forgot Password page and reset-password flow
- [✓] Send password-reset emails through a configured email provider
- [✓] Add CSRF protection to every form
- [✓] Require a production `FLASK_SECRET_KEY`; never use the development fallback in deployment
- [✓] Add login rate limiting and account-lockout protection
- [✓] Add server-side validation for all submitted data
- [✓] Configure secure cookies, HTTPS, and production-safe Flask settings
- [✓] Add audit logs for sign-ins, account approvals, and important data changes

## Core inventory database design

- [ ] Add organisations/businesses and link users to an organisation
- [ ] Add user roles and permissions (owner, manager, staff)
- [ ] Add products, product categories, units, and stock levels
- [ ] Add suppliers and purchase records
- [ ] Add customers and sales records
- [ ] Add stock movements for purchases, sales, returns, adjustments, and transfers
- [ ] Add low-stock thresholds and inventory alerts
- [ ] Create database migrations for every schema change

## Application pages

- [ ] Replace the temporary dashboard with inventory summaries and quick actions
- [ ] Build product list, product create/edit, and product-detail pages
- [ ] Build stock-in and stock-adjustment workflows
- [ ] Build purchase and supplier management pages
- [ ] Build sales and customer management pages
- [ ] Build reports for stock value, sales, purchases, and profit/loss
- [ ] Add search, filters, pagination, and export to CSV where useful

## Quality and deployment

- [ ] Add automated tests for login, access requests, permissions, and database operations
- [ ] Add error pages for 403, 404, and 500 responses
- [ ] Add a development seed command for safe sample data
- [ ] Create a deployment configuration for PostgreSQL, environment variables, and static files
- [ ] Add backups and a database recovery plan
- [ ] Add project documentation: setup, environment variables, and administrator guide
