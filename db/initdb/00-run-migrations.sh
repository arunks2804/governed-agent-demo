#!/usr/bin/env bash
# Runs once, on first initialisation of the data volume, as the bootstrap
# superuser. Reader passwords come from Docker secrets and are passed to psql
# as variables so they never appear in a migration file.
set -euo pipefail

acme_pw=$(cat /run/secrets/acme_reader_password)
northstar_pw=$(cat /run/secrets/northstar_reader_password)

for f in /migrations/*.sql; do
  echo "applying migration $(basename "$f")"
  psql -v ON_ERROR_STOP=1 --no-psqlrc \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v acme_reader_password="$acme_pw" \
    -v northstar_reader_password="$northstar_pw" \
    -v dbname="$POSTGRES_DB" \
    -f "$f"
done
