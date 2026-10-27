#!/usr/bin/env bash
# Generate local-only demo secrets. Idempotent: existing files are kept.
# Nothing generated here is ever committed (see .gitignore).
set -euo pipefail

cd "$(dirname "$0")/.."
dir=secrets
mkdir -p "$dir"
chmod 700 "$dir"

rand() { openssl rand -hex 24; }

write_if_missing() {
  local name=$1 value=$2
  if [[ ! -s "$dir/$name" ]]; then
    printf '%s' "$value" >"$dir/$name"
    echo "created $dir/$name"
  fi
}

write_if_missing postgres_admin_password "$(rand)"
write_if_missing acme_reader_password "$(rand)"
write_if_missing northstar_reader_password "$(rand)"
write_if_missing promptonly_broad_password "$(rand)"
write_if_missing audit_token "$(rand)"
write_if_missing runner_control_token "$(rand)"

if [[ ! -s "$dir/jwt_private.pem" ]]; then
  openssl genpkey -algorithm ED25519 -out "$dir/jwt_private.pem" 2>/dev/null
  openssl pkey -in "$dir/jwt_private.pem" -pubout -out "$dir/jwt_public.pem"
  echo "created $dir/jwt_private.pem + jwt_public.pem"
fi

# OPA reads its API tokens from its environment (opa.runtime().env), so these
# two live in an env file that only the opa and web/data-service containers load.
if [[ ! -s "$dir/opa.env" ]]; then
  {
    echo "OPA_DATA_SERVICE_TOKEN=$(rand)"
    echo "OPA_ADMIN_TOKEN=$(rand)"
  } >"$dir/opa.env"
  echo "created $dir/opa.env"
fi
# Split the OPA tokens into files so services can mount only the one they need.
grep '^OPA_DATA_SERVICE_TOKEN=' "$dir/opa.env" | cut -d= -f2- | tr -d '\n' >"$dir/opa_data_service_token"
grep '^OPA_ADMIN_TOKEN=' "$dir/opa.env" | cut -d= -f2- | tr -d '\n' >"$dir/opa_admin_token"

# The runner gets its control token through its environment, not a file: the
# sandbox uid cannot read /proc/1/environ of the root-owned server process,
# whereas a bind-mounted secret file would be world-readable inside the container.
printf 'RUNNER_CONTROL_TOKEN=%s\n' "$(cat "$dir/runner_control_token")" >"$dir/runner.env"

# Prompt-only overlay ONLY: the deliberately broad credential handed to the
# insecure comparison runner. Never loaded by the protected configuration.
printf 'DATABASE_URL=postgresql://demo_broad:%s@postgres:5432/governed?connect_timeout=3\n' \
  "$(cat "$dir/promptonly_broad_password")" >"$dir/promptonly.env"

# Container processes run as non-root users and read these via bind-mounted
# compose secrets, so they must be readable inside the containers. The secrets
# directory itself stays 0700 on the host.
chmod 644 "$dir"/*
echo "secrets ready in $dir/"
