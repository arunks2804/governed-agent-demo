-- 002: tenant reader roles and row-level security.
-- Tenant isolation is bound to the *login role*, not to a session variable,
-- so nothing the caller can SET changes which rows are visible.

CREATE ROLE acme_reader LOGIN PASSWORD :'acme_reader_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 20;
CREATE ROLE northstar_reader LOGIN PASSWORD :'northstar_reader_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 20;

-- Defence in depth for every session of these roles. The data service also
-- runs each query in a READ ONLY transaction with SET LOCAL statement_timeout.
ALTER ROLE acme_reader SET default_transaction_read_only = on;
ALTER ROLE acme_reader SET statement_timeout = '5s';
ALTER ROLE acme_reader SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE acme_reader SET search_path = app;
ALTER ROLE northstar_reader SET default_transaction_read_only = on;
ALTER ROLE northstar_reader SET statement_timeout = '5s';
ALTER ROLE northstar_reader SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE northstar_reader SET search_path = app;

-- Minimum privileges: connect, see the app schema, read app.orders. Nothing else.
GRANT CONNECT ON DATABASE :"dbname" TO acme_reader, northstar_reader;
GRANT USAGE ON SCHEMA app TO acme_reader, northstar_reader;
GRANT SELECT ON app.orders TO acme_reader, northstar_reader;

ALTER TABLE app.orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.orders FORCE ROW LEVEL SECURITY;

CREATE POLICY acme_reader_isolation ON app.orders
    AS PERMISSIVE FOR SELECT TO acme_reader
    USING (tenant_id = 'acme');

CREATE POLICY northstar_reader_isolation ON app.orders
    AS PERMISSIVE FOR SELECT TO northstar_reader
    USING (tenant_id = 'northstar');

-- billing_accounts: RLS on with no policies = deny-all for anyone who is not
-- the owner or a superuser, in addition to having no grants.
ALTER TABLE app.billing_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.billing_accounts FORCE ROW LEVEL SECURITY;
