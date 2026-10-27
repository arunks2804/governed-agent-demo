# Governed Agent Data Access demo - one-command entry points.
SHELL := /bin/bash
FULL := compose.yaml:compose.prompt-only.yaml
PROTECTED := compose.yaml

.DEFAULT_GOAL := help

help: ## Show targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-16s %s\n", $$1, $$2}'

secrets: ## Generate local demo secrets (idempotent)
	@./scripts/gen-secrets.sh

up: secrets ## Start the full demo: protected stack + labelled prompt-only comparison runner
	COMPOSE_FILE=$(FULL) docker compose up -d --build --wait
	@echo "UI: http://127.0.0.1:$${WEB_PORT:-8088}"

up-protected: secrets ## Start the protected configuration only (no prompt-only runner)
	COMPOSE_FILE=$(PROTECTED) docker compose up -d --build --wait --remove-orphans
	-COMPOSE_FILE=$(PROTECTED) docker compose exec -T postgres psql -U postgres -d governed -qc \
	  "DROP OWNED BY demo_broad; DROP ROLE demo_broad;" 2>/dev/null
	@echo "UI: http://127.0.0.1:$${WEB_PORT:-8088}"

down: ## Stop everything (keeps the database volume)
	COMPOSE_FILE=$(FULL) docker compose --profile test down --remove-orphans

reset: ## Recreate from scratch: drop the DB volume, re-run migrations + seed, restart
	COMPOSE_FILE=$(FULL) docker compose --profile test down -v --remove-orphans
	$(MAKE) up

test: ## Run every enforcement test + Chromium UI tests against the running stack
	./scripts/test.sh

test-backend: ## Enforcement tests only (no browser)
	SKIP_E2E=1 ./scripts/test.sh

test-e2e: e2e/node_modules ## Playwright + Chromium UI tests only
	cd e2e && npx playwright test

demo: ## Live terminal agent demo (real Ollama + PostgreSQL). Use ARGS="--demo write"
	./scripts/demo.sh $(ARGS)

level: ## 4-level enforcement comparison (L1 prompt-only → L4 PostgreSQL RLS). Use ARGS="--level cross_tenant"
	./scripts/demo.sh --level $(or $(LEVEL),all)

record: e2e/node_modules ## Record the "Agentic AI Security" presentation (recordings/agentic-ai-security.mp4)
	./scripts/record-present.sh

record-walkthrough: e2e/node_modules ## Record the narrated single-tenant walkthrough
	./scripts/record.sh

record-security: ## Record the narrated security presentation (recordings/agentic-security-demo.mp4)
	python scripts/record-terminal.py $(ARGS)

record-security-test: ## Dry-run the recording flow without producing a video
	python scripts/record-terminal.py --no-video

e2e/node_modules:
	cd e2e && npm ci && npx playwright install chromium

experiment: ## LIVE LLM experiment: does the model try direct DB access despite "Use MCP only"?
	COMPOSE_FILE=$(FULL) docker compose --profile test build -q tests
	COMPOSE_FILE=$(FULL) docker compose --profile test run --rm --no-deps -e TRIALS=$${TRIALS:-2} \
	  --entrypoint python tests experiments/llm_direct_access_probe.py

opa-down: ## Simulate an OPA outage (requests are then denied)
	COMPOSE_FILE=$(FULL) docker compose stop opa

opa-up: ## Restore OPA
	COMPOSE_FILE=$(FULL) docker compose start opa

logs: ## Follow enforcement-relevant logs
	COMPOSE_FILE=$(FULL) docker compose logs -f data-service mcp-server runner opa agent

lint: ## Lint + type-check Python, TypeScript, and Rego
	./scripts/lint.sh

lock: ## Re-resolve pinned Python dependencies
	./scripts/lock-deps.sh

.PHONY: demo level experiment help secrets up up-protected down reset test test-backend test-e2e record record-walkthrough record-security record-security-test opa-down opa-up logs lint lock
