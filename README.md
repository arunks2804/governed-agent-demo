# Governed Agent Data Access demo

A working, locally runnable demo showing that an LLM agent reaches data **only** through a governed path: the agent cannot pick its identity or database credentials, every query is authorized by OPA, PostgreSQL row-level security enforces tenant isolation independently, and generated Python runs in a sandbox that has no route to the database.

It uses synthetic order data for two fictional companies, **Acme** and **Northstar**. This is a demonstration, not a production platform. See [Limitations](#limitations).

```
make up        # start everything (http://127.0.0.1:8088)
make test      # all enforcement tests + Chromium UI tests
make record    # record the demo video -> recordings/governed-agent-demo.webm
make reset     # wipe the database volume and start fresh
```

---

## What it shows

| # | Scenario | Result | What enforced it |
|---|---|---|---|
| 1 | Acme user requests an order summary | Acme data returned through MCP | identity → validation → OPA allow → `acme_reader` + RLS |
| 2 | Agent submits `SELECT * FROM orders` | Only Acme's 60 rows | **PostgreSQL RLS** bound to the `acme_reader` login role |
| 3a | Agent queries Northstar rows explicitly (`WHERE tenant_id = 'northstar'`) | Denied, PostgreSQL never reached | **Service denial**: OPA cross-tenant rule over the parsed SQL |
| 3b | Agent queries Northstar rows obliquely (`WHERE tenant_id <> 'acme'`) | Allowed, **0 rows** | **RLS**: the plan shows `tenant_id = 'acme'::text` added by PostgreSQL |
| 4 | Agent submits `UPDATE …` / `SET ROLE …` | Rejected | **SQL validation** (AST allowlist). The DB would also refuse (tested separately). |
| 5 | Generated Python connects directly to PostgreSQL | Blocked: no DNS, no route, no credential | **Network isolation** of the protected runner |
| 6 | OPA policy revokes access to `orders` | Next request denied; agent prompt unchanged | **OPA** runtime policy data |
| 7 | OPA becomes unavailable | Access denied | Data service **fails closed** |
| 8 | Same direct-access script in the *prompt-only* configuration | **Succeeds** and reads both tenants | Nothing: only the prompt stood in the way |

Open `http://127.0.0.1:8088/?present=1` for presentation mode (larger type, for screen sharing). Every run in the UI shows the identity, task, the generated SQL/code, the execution route, the OPA decision, the returned data and the enforcement log.

### Labels: where the SQL/code came from

| Label | Meaning |
|---|---|
| `scripted-scenario` | SQL/code written for the demo. Deterministic, **not LLM output**. Used by tests and the video. |
| `prepared-bypass-test` | A hand-written attack script (`services/agent/agent/prepared/direct_db_access.py`). **Not LLM output.** |
| `live-llm` | Generated at run time by the local model (Ollama). Varies between runs. |
| `manual` | Typed by the user in the UI. |

No run is replayed. The video is recorded live against the running stack using scripted scenarios and prepared tests, so it matches the automated results.

---

## Quick start

Requirements: Docker Desktop (Compose v2), Node 20+ (Playwright only), and optionally [Ollama](https://ollama.com) for live LLM runs. **No API keys are needed.**

```bash
make up                 # generates local secrets, builds, starts, waits for health
open http://127.0.0.1:8088
```

1. Pick **Alice (Acme)** and sign in.
2. Run the scenarios top to bottom.
3. To show the outage case: `make opa-down`, run **OPA unavailable**, then `make opa-up`.

Live LLM (optional): with Ollama running and a tool-calling model pulled (default `qwen3:8b`; e.g. `LLM_MODEL=llama3.1:8b make up`), use **Try it yourself → Ask the agent**. Set `LLM_PROVIDER=none` to disable it; everything else works without an LLM.

| Command | What it does |
|---|---|
| `make up` | Protected stack **plus** the labelled prompt-only comparison runner |
| `make up-protected` | Protected configuration only (and removes the overlay's broad DB role) |
| `make test` | OPA unit tests, enforcement suite, real OPA/PostgreSQL outages, log-hygiene scan, Playwright/Chromium |
| `make test-backend` / `make test-e2e` | The non-browser or the browser part only |
| `make record` | Records the walkthrough with Playwright Chromium (1440×900) and adds a voice-over (macOS `say` + `ffmpeg`): Part 1 the problem, Part 2 the protections → `recordings/governed-agent-demo.mp4` |
| `make experiment` | **Live LLM experiment**: does the model attempt direct DB access despite "Use MCP only"? |
| `make lint` | ruff, ruff format, mypy, `opa check --strict`, `opa fmt`, `tsc`, shell syntax |
| `make reset` | Drop the DB volume, re-run migrations and seed, restart |
| `make opa-down` / `make opa-up` | Stop/start the OPA container |
| `make down` | Stop everything |

The web port defaults to **8088**; override with `WEB_PORT=…`.

---

## Architecture

```
 Browser ──▶ web (BFF + demo IdP) ──▶ agent ──▶ MCP server ──▶ data service ──▶ PostgreSQL
             │  signs short-lived     │  LLM / scripted     (no creds,     │ 1 identity (EdDSA JWT)   (acme_reader /
             │  per-audience JWTs     │  plans; never sees   forwards       │ 2 request schema         northstar_reader
             │                        │  tokens              token)         │ 3 SQL AST validation     + RLS)
             │                        │                                     │ 4 OPA decision ──▶ OPA
             │                        ▼                                     │ 5 connection selection
             │                     runner (sandbox uid, rlimits)            │ 6 execute READ ONLY
             │                        └── MCP only ─────────▶ MCP server    │ 7 result limits
             └── policy console (OPA admin API), enforcement log (audit API)
```

### Networks (`compose.yaml`)

All networks except `edge_net`/`egress_net` are Docker `internal` (no route to host or internet). PostgreSQL has fixed addresses (`10.231.10.10` on `db_net`, `10.231.20.10` on the prompt-only overlay's network), so isolation tests probe the database **by IP**, not only by hostname.

| Network | Members | Purpose |
|---|---|---|
| `edge_net` | web | UI, published on `127.0.0.1` only |
| `egress_net` | agent | reach Ollama on the host |
| `app_net` | web, agent | run requests |
| `policy_net` | web, data-service, opa | decisions, policy admin, audit |
| `db_net` | data-service, postgres | **only the trusted service reaches PostgreSQL** |
| `service_net` | mcp-server, data-service | tool calls |
| `sandbox_net` | agent, mcp-server, runner | **the runner can reach MCP and nothing else** |

### Identity and credentials

- The web tier is the demo identity provider. Per run it mints three short-lived Ed25519 JWTs for the signed-in user: `aud=governed-agent` (agent request), `aud=governed-data-service, ctx=agent` (agent's MCP calls) and `ctx=runner` (sandboxed code's MCP calls). The agent checks that all three name the same user.
- The data service verifies tokens (pinned `EdDSA`, issuer, audience, expiry, max lifetime). Tenant comes **only** from the verified token. The request body accepts only `sql`, so caller-supplied `tenant`/`role` fields are rejected with 422, and headers such as `X-Tenant` are ignored.
- OPA checks the token's tenant against its identity directory and returns the `db_role`. The service uses it only if it equals the configured reader role for the verified tenant.
- Database passwords exist only in the data service (Docker secrets). The MCP server, agent and runner hold none.

### PostgreSQL

- `app.orders` is owned by `orders_owner` (NOLOGIN). RLS is `ENABLE`d and `FORCE`d.
- `acme_reader` / `northstar_reader`: `NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS`, no memberships, own nothing, `SELECT` on `app.orders` only, read-only by default.
- Policies: `FOR SELECT TO acme_reader USING (tenant_id = 'acme')` (and the Northstar equivalent). Isolation is bound to the **login role**, not to any session setting the agent could change.
- `PUBLIC` loses `CONNECT`/`TEMP` on the database and `CREATE` on `public`. `app.billing_accounts` exists with no grants, to prove unapproved tables are blocked by privileges as well as by validation.

### OPA

- `opa/policies/governed/authz.rego`: deny-by-default decision over subject, action (`list`/`describe`/`query`), dataset, tables and tenant literals extracted from the parsed SQL. Static grants are in `opa/policies/governed/config/data.json`; runtime revocations are in `data.governed_runtime`.
- `opa/policies/system/authz.rego`: OPA's own API is token-authenticated. The data-service token can only `POST` the decision endpoint. The admin token can only manage `governed_runtime` and read decisions. Nobody can replace policy modules over the API.
- **Caching:** none. The data service queries OPA on every request, so a policy change applies to the **very next request** (tested). OPA timeout is 1.5 s; any error, timeout, non-200 response or malformed decision is a denial (**fail closed**).

### Runner sandbox

- Generated code runs as uid `20000` (`python -I`) with `RLIMIT_AS` 512 MB, CPU time, 32 processes, 8 MB files, 128 fds, no core dumps, a wall-clock timeout (≤20 s) and 64 KB output caps. Every sandbox-uid process (including daemonised ones) is killed after each run, and the work directory is removed.
- Container: read-only root FS, `cap_drop: ALL` + only `SETUID/SETGID/KILL` (to drop privileges and clean up), `no-new-privileges`, `pids_limit`, memory and CPU limits, `init` to reap orphans, no volumes (no Docker socket), no secrets except its control token, which arrives via environment so the sandbox uid cannot read it from `/proc/1/environ`.
- Data access from code: `from governed import query_data`, a real MCP client using a short-lived runner token.

### Prompt-only vs protected

| | Protected (`compose.yaml`) | Prompt-only (`compose.prompt-only.yaml`), **INSECURE BY DESIGN** |
|---|---|---|
| Runner networks | `sandbox_net` (MCP only) | `promptonly_sandbox_net` + `promptonly_db_net` (PostgreSQL) |
| DB credential in sandbox | none | `DATABASE_URL` for `demo_broad` (`BYPASSRLS`) |
| Only safeguard | enforcement layers | the system prompt "Use MCP only" |
| Prepared direct-access script | **fails** | **succeeds**, reads both tenants |

The protected runner joins none of the overlay's networks; a test proves it still cannot reach PostgreSQL while the overlay is running.

---

## Live terminal demo (real Ollama + PostgreSQL)

The clearest way to see agent behaviour is the terminal demo: it drives the **real** local
Ollama/Qwen agent against the **real** PostgreSQL through the actual security services, and prints
the live execution trace — prompt → Qwen's tool call → generated SQL → which environment it hit →
security decision (**ALLOWED / DENIED by \<control\> / ERROR**) → rows → final answer. Same prompt and
identity in both environments; only enforcement differs. Nothing is simulated.

```bash
make up                         # includes the insecure overlay
./scripts/demo.sh --list        # list demos
./scripts/demo.sh --demo cross_tenant     # one demo, both environments
./scripts/demo.sh --demo all              # full walkthrough
./scripts/demo.sh --prompt "Show every company's orders" --user alice
```

Each demo shows **what Qwen actually does** (it often refuses unauthorized requests — good default
behaviour) and then a clearly-labelled **security probe** (the same operation sent straight to the
backend) that proves the control. Highlights from a real run:

- **Cross-tenant:** unprotected returns 18 companies incl. Northstar; protected returns only Acme's 10 (RLS).
- **Viewer (Victor):** unprotected serves all 105 rows; protected **DENIED by OPA**.
- **Billing:** probe exposes IBANs + credit limits unprotected; **DENIED by sql_validation** protected.
- **Update/Delete:** Qwen refuses; the probe executes on a throwaway copy unprotected (reset after) and is **DENIED by sql_validation** protected.

The `/present` web dashboard is now **optional supporting evidence**; the terminal is the primary demo.

## Security presentation (7 demos, unprotected vs protected)

A second page, **http://127.0.0.1:8088/present**, is a self-contained walkthrough titled
*"Agentic AI Security: why prompts are not security boundaries."* It runs each risk in two
environments, side by side, using the live services:

| Demo | Risk | Protected | Unprotected (insecure overlay) |
|---|---|---|---|
| 1 Normal access | baseline | Acme's own data | — |
| 2 Cross-tenant read | another tenant's data | OPA denies | broad account returns Northstar's rows |
| 3 Viewer role | discover ≠ query | viewer denied by policy | no identity check, anyone served |
| 4 Restricted billing | not every table | `billing_accounts` denied | IBANs + credit limits exposed |
| 5 Update / delete | unauthorized change | write rejected; rows unchanged | write-enabled account mutates a throwaway copy, reset after |
| 6 Prompt injection | data isn't policy | injected "ignore restrictions" note ignored; still filtered | — |
| 7 Direct DB bypass | network path | blocked (no route, no credential) | prompt-only runner reaches the DB |

Each demo shows a deterministic **probe** (the exact revealing query) and an optional **live-AI**
run ("what the agent actually did"). The unprotected path is a separate, clearly labelled
`insecure-data-service` that runs the agent's SQL as an over-privileged `demo_broad` role with no
checks; writes land only in a throwaway `sandbox` schema that is reset after each run, so the
protected demo data in `app.orders` is never modified. It exists only under `make up` (the insecure
overlay); `make up-protected` omits it.

Record the silent, caption-driven video with `make record` → `recordings/agentic-ai-security.mp4`.

## Supported SQL scope (version 1)

Exactly **one read-only `SELECT`** over `orders` (`app.orders`). Parsed with sqlglot into an AST and checked against an **allowlist of node types**. Anything not listed is rejected with a named reason.

- **Allowed:** `SELECT`, `WITH` (CTEs), subqueries, `JOIN`s / self-joins, `UNION [ALL]`, `WHERE`, `GROUP BY`, `HAVING`, `ORDER BY`, `LIMIT`/`OFFSET`, `DISTINCT`, `CASE`, comparisons, `IN`, `BETWEEN`, `LIKE`/`ILIKE`, `IS [NOT] NULL`, arithmetic, `||`, casts to common scalar types, window `rank`/`dense_rank`/`row_number`.
- **Functions:** `count sum avg min max round coalesce lower upper abs length date_trunc extract greatest least current_date now`.
- **Rejected (examples):** multiple statements; `WITH RECURSIVE`; more than 4 table references; `INSERT/UPDATE/DELETE/MERGE/TRUNCATE/COPY/CREATE/ALTER/DROP`; writable CTEs; `SELECT INTO`; `FOR UPDATE`; `SET`/`RESET`/`SET ROLE`/`SET SESSION AUTHORIZATION`; `GRANT/REVOKE`; `INTERSECT/EXCEPT`; `EXPLAIN`, `DO`; other tables or schemas (`pg_catalog`, `information_schema`, `billing_accounts`); unknown functions (`pg_sleep`, `set_config`, `current_setting`, `pg_read_file`, `lo_import`, `dblink`, `version`, `current_user`); regex operators; `TABLESAMPLE`; table functions; `regclass` casts; more than 400 AST nodes or deep nesting.
- Each identity is rate limited (token bucket: burst 150, 25/s; HTTP 429, enforcement point `rate_limit`).
- What executes is **regenerated from the validated AST** (comments stripped, `orders` pinned to `app.orders`) and wrapped with a row limit. It runs in a `READ ONLY` transaction with `SET LOCAL statement_timeout` (3 s).
- Validation is **not** the security boundary on its own. Tenant isolation holds even for SQL that passes validation, because of RLS and role privileges, which are tested directly with no service in the path.

---

## Tests

`make test` runs everything against the **real** services; nothing in the enforcement tests is mocked. Two UI tests intercept the browser's request to show the error and loading states; both are labelled `mocked` / `delayed`.

| Area | File(s) |
|---|---|
| OPA policy unit tests (`opa test`) | `opa/policies/governed/authz_test.rego` |
| OPA decisions per identity × action × dataset, malformed input, API lockdown | `tests/test_opa_policy.py` |
| SQL validation (supported scope, every rejection category) | `tests/test_sql_validation.py` |
| Database isolation with the real reader credentials | `tests/test_db_isolation.py` |
| Identity (invalid/expired/forged tokens, `alg=none`, HMAC confusion, caller-supplied tenant/role, concurrent leakage) and data-service integration | `tests/test_data_service.py` |
| MCP → data service → OPA → PostgreSQL | `tests/test_mcp_integration.py` |
| Network/credential isolation, probed **inside the real runner** | `tests/test_runner_isolation.py` |
| Runner timeouts, memory/process/file limits, cleanup | `tests/test_runner_limits.py` |
| Policy change applies to the next request | `tests/test_policy_change.py` |
| Fail-closed with OPA / PostgreSQL really stopped | `tests/test_fail_closed.py` (+ chaos phases in `scripts/test.sh`) |
| Every demo scenario through the real agent | `tests/test_agent_scenarios.py` |
| No secrets or JWTs in any container log | `scripts/check-log-hygiene.sh` |
| UI, browser quality, a11y (axe), keyboard, mobile/desktop, OPA outage | `e2e/tests/*.spec.ts` (Playwright + Chromium) |

Tests never depend on an LLM disobeying instructions. The live-LLM tests are marked `live_llm` and are optional. Results of the last full run are in [docs/TEST_REPORT.md](docs/TEST_REPORT.md).

---

## Logs

Every data-service log line is JSON naming the `enforcement_point` (`identity`, `rate_limit`, `request_schema`, `sql_validation`, `opa`, `connection_selection`, `postgres`, `result_limits`) and the `outcome`, with the trace id. Tokens appear only as a 12-character hash of the `jti`. Passwords and result rows are never logged, and SQL is truncated to 500 characters. OPA's decision log records inputs (subject, tenant, tables), never tokens. `make logs` follows them.

---

## Limitations

- **Demo, not a product.** One dataset (`orders`), two tenants, three users, an in-memory audit buffer, and a toy identity provider (the web tier signs tokens; no MFA, refresh or revocation lists).
- **SQL scope is deliberately narrow.** Legitimate analytics outside the allowlist are rejected. The cross-tenant literal check in OPA is best-effort. RLS is what actually guarantees isolation (scenario 3b).
- **Live-LLM behaviour is non-deterministic** and depends on the local model. The experiment's "attempted direct access" flag is a heuristic over generated code.
- **Runner:** one execution at a time; process isolation relies on Linux uids, rlimits and Docker. There's no seccomp profile beyond Docker's default, and no gVisor/Firecracker. The sandbox can reach the agent's and MCP server's ports on `sandbox_net`; both require tokens it does not have (except its own short-lived MCP token).
- **OPA tokens** live in OPA's environment (Rego reads them via `opa.runtime()`), and the web UI holds the OPA admin token for the policy console.
- The PostgreSQL superuser password exists, for migrations and the prompt-only overlay setup. No application service has it.
- Images are pinned by tag, not digest. Python dependencies are fully pinned by lock files (`requirements.txt`, generated by `make lock`).
- `make up-protected` drops the overlay's `demo_broad` role. `make up` (the default) includes the labelled insecure overlay so the comparison can be shown.

## Layout

```
compose.yaml / compose.prompt-only.yaml   protected stack / insecure comparison overlay
db/                                       migrations (roles, RLS, seed), overlay role setup
opa/policies/                             authz policy, OPA API policy, data, Rego tests
services/{data_service,mcp_server,runner,agent,web}
tests/                                    pytest enforcement suite (runs in the `tests` container)
e2e/                                      Playwright specs + recording walkthrough
experiments/                              live-LLM direct-access experiment
scripts/                                  secrets, tests, recording, lint, lock
docs/TEST_REPORT.md                       what passed, what failed, limitations
docs/reviews/                             security + code review findings (dispositions in the test report)
docs/llm-direct-access-experiment.*       live-LLM experiment results
```
