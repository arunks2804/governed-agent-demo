#!/usr/bin/env bash
# Lint and type-check everything with pinned tool versions. Exits non-zero on findings.
set -uo pipefail
cd "$(dirname "$0")/.."
failed=0
docker build -q -f tools/Dockerfile.lint -t governed-demo/lint:local . >/dev/null || exit 1
run() { docker run --rm -v "$PWD:/src" -w /src governed-demo/lint:local "$@"; }

echo "== ruff"; run ruff check . || failed=1
echo "== ruff format (check)"; run ruff format --check . || failed=1
for svc in data_service insecure_data_service mcp_server runner agent web; do
  echo "== mypy services/$svc"
  run sh -c "cd services/$svc && mypy --config-file /src/pyproject.toml $svc" || failed=1
done
echo "== mypy runner sandbox helper"; run mypy --config-file pyproject.toml services/runner/sandbox/governed.py || failed=1
echo "== opa check --strict / fmt"
docker run --rm -v "$PWD/opa/policies:/policies:ro" openpolicyagent/opa:1.21.2-static check --strict /policies || failed=1
[[ -z "$(docker run --rm -v "$PWD/opa/policies:/policies:ro" openpolicyagent/opa:1.21.2-static fmt --list /policies)" ]] \
  || { echo "rego files need opa fmt"; failed=1; }
echo "== tsc (e2e)"; (cd e2e && npx tsc --noEmit) || failed=1
echo "== shell syntax"; for f in scripts/*.sh db/initdb/*.sh db/promptonly/*.sh; do bash -n "$f" || failed=1; done
[[ $failed -eq 0 ]] && echo "lint OK" || echo "lint FAILED"
exit $failed
