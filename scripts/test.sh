#!/usr/bin/env bash
# Full enforcement test run against the real stack. Exits non-zero on any failure.
#
# Phases:
#   1. OPA policy unit tests (opa test)
#   2. pytest: DB isolation, SQL validation, OPA, identity, data service, MCP,
#      runner network/credential isolation, runner limits, agent scenarios
#   3. chaos: OPA really stopped  -> fail-closed tests -> OPA restarted
#   4. chaos: PostgreSQL stopped  -> fail-clean tests  -> PostgreSQL restarted
#   5. log hygiene: no secret/token values in any container log
#   6. Playwright + Chromium UI tests (skip with SKIP_E2E=1)
set -uo pipefail
cd "$(dirname "$0")/.."
export COMPOSE_FILE="${COMPOSE_FILE:-compose.yaml:compose.prompt-only.yaml}"
mkdir -p reports
results=()
failed=0

record() { # name, exit code
  if [[ $2 -eq 0 ]]; then results+=("PASS  $1"); else results+=("FAIL  $1 (exit $2)"); failed=1; fi
}

wait_ready() { # field value
  for _ in $(seq 1 60); do
    if docker compose exec -T web python -c "import json,sys,urllib.request; s=json.load(urllib.request.urlopen('http://127.0.0.1:8080/api/status')); sys.exit(0 if s.get('$1')=='$2' else 1)" 2>/dev/null; then return 0; fi
    sleep 1
  done
  echo "timed out waiting for $1=$2" >&2
  return 1
}

pytest_run() { # junit-name, extra args...
  local name=$1; shift
  docker compose --profile test run --rm --no-deps tests tests -q -rs -p no:cacheprovider \
    --junitxml="/reports/pytest-$name.xml" "$@"
}

echo "== building test image"
docker compose --profile test build -q tests || exit 1

echo "== 1. OPA policy unit tests"
docker run --rm -v "$PWD/opa/policies:/policies:ro" openpolicyagent/opa:1.21.2-static test /policies -v \
  >reports/opa-test.txt 2>&1
rc=$?; tail -1 reports/opa-test.txt; record "opa policy unit tests" $rc

echo "== 2. enforcement suite (normal phase)"
pytest_run main -m "not live_llm" "$@"; record "pytest enforcement suite" $?

echo "== 3. chaos: OPA unavailable"
docker compose stop opa >/dev/null 2>&1
CHAOS=opa_down pytest_run opa-down -m "chaos_opa_down"; record "fail-closed with OPA stopped" $?
docker compose start opa >/dev/null 2>&1
wait_ready opa up; record "OPA recovered" $?

echo "== 4. chaos: PostgreSQL unavailable"
docker compose stop postgres >/dev/null 2>&1
CHAOS=db_down pytest_run db-down -m "chaos_db_down"; record "fail-clean with PostgreSQL stopped" $?
docker compose start postgres >/dev/null 2>&1
wait_ready postgres up; record "PostgreSQL recovered" $?

echo "== 5. log hygiene"
./scripts/check-log-hygiene.sh; record "no secrets in container logs" $?

if [[ "${SKIP_E2E:-0}" != "1" ]]; then
  echo "== 6. Playwright + Chromium"
  (cd e2e && npx playwright test); record "playwright chromium suite" $?
else
  results+=("SKIP  playwright chromium suite (SKIP_E2E=1)")
fi

echo
echo "================ summary ================"
printf '%s\n' "${results[@]}" | tee reports/summary.txt
exit $failed
