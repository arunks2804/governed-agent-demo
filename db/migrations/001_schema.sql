-- 001: schema and tables, owned by a NOLOGIN owner role.
-- Reader roles never own tables, so FORCE ROW LEVEL SECURITY plus
-- non-ownership means RLS always applies to them.

CREATE ROLE orders_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

-- Lock down defaults: nobody gets implicit CONNECT/TEMP on the database or
-- CREATE on the public schema.
REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA app AUTHORIZATION orders_owner;
REVOKE ALL ON SCHEMA app FROM PUBLIC;

CREATE TABLE app.orders (
    order_id      bigint        PRIMARY KEY,
    tenant_id     text          NOT NULL CHECK (tenant_id IN ('acme', 'northstar')),
    customer_name text          NOT NULL,
    amount        numeric(12,2) NOT NULL CHECK (amount >= 0),
    status        text          NOT NULL CHECK (status IN ('pending', 'shipped', 'delivered', 'cancelled', 'refunded')),
    order_date    date          NOT NULL
);
CREATE INDEX orders_tenant_date_idx ON app.orders (tenant_id, order_date);

-- A table outside the approved dataset. Readers have no grant on it; used to
-- prove that both SQL validation and database privileges block it.
CREATE TABLE app.billing_accounts (
    tenant_id      text PRIMARY KEY,
    iban_synthetic text NOT NULL,
    credit_limit   numeric(12,2) NOT NULL
);

ALTER TABLE app.orders OWNER TO orders_owner;
ALTER TABLE app.billing_accounts OWNER TO orders_owner;
REVOKE ALL ON ALL TABLES IN SCHEMA app FROM PUBLIC;
