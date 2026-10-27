#!/bin/sh
# PROMPT-ONLY / UNPROTECTED OVERLAY ONLY. Synthetic data only.
#
# Creates a deliberately over-privileged demo role ("demo_broad") used by the
# INSECURE comparison path, plus a throwaway "sandbox" schema the write demo can
# mutate and reset without touching the protected demo data in app.orders.
#
# demo_broad:
#   - BYPASSRLS, so it sees every tenant's rows (the opposite of a reader role)
#   - SELECT on all app tables, including the restricted billing_accounts
#   - full read/write on the throwaway sandbox schema
set -eu
PGPASSWORD=$(cat /run/secrets/postgres_admin_password)
export PGPASSWORD
broad_pw=$(cat /run/secrets/promptonly_broad_password)

psql -v ON_ERROR_STOP=1 --no-psqlrc -v broad_pw="$broad_pw" <<'SQL'
SELECT 'CREATE ROLE demo_broad LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'demo_broad') \gexec
ALTER ROLE demo_broad LOGIN BYPASSRLS PASSWORD :'broad_pw';
GRANT CONNECT ON DATABASE governed TO demo_broad;
GRANT USAGE ON SCHEMA app TO demo_broad;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO demo_broad;

-- Throwaway copy for the destructive write demo. Never touched by the
-- protected path, which only ever reads/writes app.* as the reader roles.
CREATE SCHEMA IF NOT EXISTS sandbox AUTHORIZATION demo_broad;
DROP TABLE IF EXISTS sandbox.orders;
DROP TABLE IF EXISTS sandbox.orders_seed;
CREATE TABLE sandbox.orders      AS TABLE app.orders;
CREATE TABLE sandbox.orders_seed AS TABLE app.orders;  -- immutable backup for reset
ALTER TABLE sandbox.orders ADD PRIMARY KEY (order_id);
ALTER TABLE sandbox.orders OWNER TO demo_broad;
ALTER TABLE sandbox.orders_seed OWNER TO demo_broad;
GRANT USAGE ON SCHEMA sandbox TO demo_broad;
GRANT SELECT, INSERT, UPDATE, DELETE ON sandbox.orders TO demo_broad;
GRANT SELECT ON sandbox.orders_seed TO demo_broad;
SQL
echo "unprotected overlay: demo_broad role + throwaway sandbox schema ready (INSECURE BY DESIGN)"
