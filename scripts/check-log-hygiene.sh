#!/usr/bin/env bash
# Fail if any secret value (passwords, API tokens, signing key material) appears
# in any container's logs. JWTs are also checked by their header prefix.
set -uo pipefail
cd "$(dirname "$0")/.."
export COMPOSE_FILE="${COMPOSE_FILE:-compose.yaml:compose.prompt-only.yaml}"

logs=$(docker compose logs --no-color 2>/dev/null)
leaks=0
for f in secrets/postgres_admin_password secrets/acme_reader_password secrets/northstar_reader_password \
         secrets/promptonly_broad_password secrets/audit_token secrets/runner_control_token \
         secrets/opa_data_service_token secrets/opa_admin_token; do
  value=$(cat "$f")
  if grep -qF -- "$value" <<<"$logs"; then
    echo "LEAK: value of $f found in container logs"
    leaks=1
  fi
done
key_body=$(sed -n 2p secrets/jwt_private.pem)
if grep -qF -- "$key_body" <<<"$logs"; then echo "LEAK: JWT signing key in logs"; leaks=1; fi
# Every EdDSA JWT minted by the demo IdP starts with this base64url header.
if grep -qE 'eyJhbGciOiJFZERTQSIs[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.' <<<"$logs"; then
  echo "LEAK: a JWT appears in container logs"
  leaks=1
fi
lines=$(wc -l <<<"$logs" | tr -d ' ')
if [[ $leaks -eq 0 ]]; then echo "log hygiene OK ($lines log lines scanned)"; fi
exit $leaks
