#!/usr/bin/env bash
# Live agentic-AI security demo in the terminal (real Ollama + PostgreSQL).
#   scripts/demo.sh --list
#   scripts/demo.sh --demo cross_tenant          # one demo, both environments
#   scripts/demo.sh --demo all                   # full walkthrough
#   scripts/demo.sh --level cross_tenant         # 4-level enforcement comparison
#   scripts/demo.sh --level all                  # all level comparisons
#   scripts/demo.sh --prompt "..." --user alice  # your own prompt
#   scripts/demo.sh --demo write --env unprotected
set -euo pipefail
cd "$(dirname "$0")/.."
export COMPOSE_FILE="${COMPOSE_FILE:-compose.yaml:compose.prompt-only.yaml}"
exec docker compose --profile test run --rm --no-deps -v "$PWD/cli:/cli" \
  --entrypoint python tests /cli/agent_demo.py "${@:---demo all}"
