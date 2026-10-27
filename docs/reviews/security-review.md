# Security Review — governed-agent-demo — 2026-10-12

## Summary
This demo's protected configuration is unusually well engineered: tenant isolation is anchored in PostgreSQL login-role + RLS (not session variables), identity comes only from a verified EdDSA JWT, OPA fails closed, and the sandbox runner drops privileges and isolates networks correctly. The most significant real issue found is that the sqlglot AST allowlist in `sql_validator.py` permits `WITH RECURSIVE` CTEs and unrestricted self-joins, which — combined with the complete absence of request rate-limiting on `/v1/query` — gives any authenticated tenant a way to drive sustained CPU/memory load against the single shared PostgreSQL instance, degrading or denying service to the other tenant even though row-level data isolation itself is never broken.

| Severity | Count |
|----------|-------|
| CRITICAL | 0     |
| HIGH     | 1     |
| MEDIUM   | 2     |
| LOW      | 2     |

### Attack surface
- **Public endpoints:** `web` on `127.0.0.1:${WEB_PORT:-8088}` only (not exposed beyond localhost). Internally: `agent` `/v1/run`, `/v1/scenarios`, `/v1/llm-status`, `/healthz`; `mcp-server` MCP tools `list_datasets`/`describe_dataset`/`query_data`; `data-service` `/v1/query`, `/v1/datasets`, `/v1/datasets/{name}`, `/v1/audit`, `/readyz`; `runner` `/execute`; `opa` `/v1/data/...`.
- **External inputs:** web login `sub` (closed set), `/api/policy` tenant/action, `/api/run` body (`mode`, `scenario_id`, `task`, `sql`, `code`, `runner`), free-text LLM task prompt, LLM-generated SQL/Python, manual SQL/Python from the UI, MCP tool arguments, runner `/execute` code+timeout.
- **Secrets in code:** No. All credentials are generated at setup time into `secrets/` (gitignored) and read from files/env at runtime; no hardcoded secrets found in source.
- **.env gitignored:** Yes (`secrets/`, `.env` both in `.gitignore`); `.env.example` contains no secrets.

---

## Findings

### 🔴 HIGH

#### SEC-001 — `sql_validator.py` allowlist permits `WITH RECURSIVE` and unbounded self-joins, and there is no rate limiting, enabling a shared-PostgreSQL DoS across tenants
- **File:** `services/data_service/data_service/sql_validator.py` (the `_STRUCTURE` allowlist, lines ~56–78, includes `"With"`/`"CTE"`/`"Union"` without checking the `With.recursive` flag); exploitable via `POST /v1/query` (`services/data_service/data_service/main.py` line ~213) and `services/mcp_server/mcp_server/server.py` `query_data` tool, which have no per-identity rate limit.
- **Category:** injection / availability
- **OWASP:** A03 (also a resource-exhaustion / DoS gap)
- **Attack scenario:** An authenticated Acme user (e.g. `alice@acme.example`, who only has legitimate `query` rights on `orders`) submits as `sql`:
  ```sql
  WITH RECURSIVE t(n) AS (
    SELECT 1 FROM app.orders
    UNION ALL
    SELECT n + 1 FROM t WHERE n < 100000000
  )
  SELECT count(*) FROM t
  ```
  I verified this against the real `validate_sql()` function (copied the module and ran it with the pinned `sqlglot==30.22.0`): it is **accepted** and regenerated verbatim as canonical SQL to execute. `exp.With`/`exp.CTE`/`exp.Union`/`exp.Select` are all on the structural allowlist, and the validator never inspects the `recursive` argument of the `With` node, so `WITH RECURSIVE` parses identically to a non-recursive CTE as far as the allowlist is concerned. (A pure `WITH RECURSIVE` with no real table at all, e.g. `SELECT 1 UNION ALL SELECT n+1 FROM t ...`, is correctly rejected only because it never touches `app.orders` — trivially defeated by adding `FROM app.orders` to the base case, as shown above.)
  The query is bounded only by the data service's `SET LOCAL statement_timeout` (3000ms) and the role's `statement_timeout='5s'` default, so each call burns CPU for a few seconds before being cancelled. Because `/v1/query` and the MCP `query_data` tool have no per-subject or per-IP rate limiting anywhere in `main.py`, `mcp_server/server.py`, or `agent/tools.py`, the same identity (or a scripted agent run) can fire this query continuously/concurrently (each role's connection pool allows up to 4 concurrent connections) to keep PostgreSQL's CPU pinned. Since Acme and Northstar share one PostgreSQL instance/container, this degrades query latency and availability for the other tenant — undermining the demo's core "governed access can't hurt other tenants" claim, even though it cannot read the other tenant's rows (RLS is still correctly enforced on the recursive query's base term).
  Unrestricted self-joins have the same flavor but lower impact given the tiny seed dataset (e.g. `SELECT * FROM orders o CROSS JOIN orders o2 CROSS JOIN orders o3 CROSS JOIN orders o4` is also accepted by the validator).
- **Fix:** Reject recursive CTEs explicitly, and add a cheap query-cost/complexity cap and basic rate limiting:
  ```python
  # sql_validator.py, inside validate_sql(), right after parsing root:
  for with_node in root.find_all(exp.With):
      if with_node.args.get("recursive"):
          raise SqlRejected("unsupported_syntax", "WITH RECURSIVE is not supported")
  ```
  Additionally, cap the number of distinct table references per query (e.g. reject if `len(list(root.find_all(exp.Table))) > N`) to bound self-join blowup, and add a per-identity token-bucket rate limiter in `data_service/main.py` (`/v1/query`) — e.g. `slowapi` or a simple in-memory limiter keyed by `identity.sub`, returning 429 before the query ever reaches `run_select`. Given the single-tenant-per-role-pool design, also consider per-role PostgreSQL resource limits (`ALTER ROLE ... SET statement_timeout` is already present; additionally set `work_mem` low and consider cgroup/connection-count based throttling) so one tenant's expensive query class cannot starve another tenant's shared instance.

---

### 🟡 MEDIUM

#### SEC-002 — `/api/policy` lets any authenticated demo user revoke or restore *any* tenant's access, not just their own
- **File:** `services/web/web/main.py` lines 225–246 (`PolicyRequest`/`policy()`)
- **Category:** access-control
- **OWASP:** A01
- **Attack scenario:** The only check in `policy()` is `if not _session(request): return _error(401, ...)` — it verifies the caller is logged in as *some* demo identity, but never checks that `body.tenant` matches `IDENTITIES[sub]["tenant"]`. `victor@acme.example` (deliberately modeled as a catalog-only "viewer" with no `query` grant, per `opa/policies/governed/config/data.json`) can log in and `POST /api/policy {"action":"revoke","tenant":"northstar"}` to shut off Northstar's `query` access via OPA's runtime data, or `{"action":"restore","tenant":"northstar"}` to turn it back on — actions that have nothing to do with Victor's own tenant or grants. This is a cross-tenant authorization gap: any of the three demo logins can mutate policy for a tenant they do not belong to.
  This is plausibly intentional as a shared "policy console" demo feature (there is only one global policy document and no per-tenant admin concept), so I am flagging it at MEDIUM confidence rather than HIGH — but it does undermine the "governed access respects tenant boundaries" narrative if a viewer can unilaterally revoke a *different* tenant's access, and it would be a real IDOR/privilege-escalation bug in anything derived from this demo for production use.
- **Fix:** If the policy console is meant to be an operator/admin-only control plane (most likely intent), require a distinct admin session rather than any of the three demo user logins — e.g. introduce an `is_admin` flag on the identity and check `if not sub or not IDENTITIES[sub].get("is_admin"): return _error(403, ...)`. If it is meant to let a tenant manage only its own access, enforce `if body.tenant != IDENTITIES[sub]["tenant"]: return _error(403, "forbidden", "cannot modify another tenant's policy")`.

#### SEC-003 — No application-level rate limiting on any service
- **File:** `services/data_service/data_service/main.py`, `services/mcp_server/mcp_server/server.py`, `services/agent/agent/main.py`, `services/web/web/main.py` (grepped for `limiter`/`rate_limit`/`slowapi` across `services/` — none found)
- **Category:** misconfiguration / availability
- **OWASP:** A05
- **Attack scenario:** Beyond the recursive-CTE case in SEC-001, a caller with a valid token can call `/v1/query` or `/api/run` (web) in a tight loop with cheap-but-still-costly queries (e.g. large `CROSS JOIN`s under the 8000-char/400-node caps) or repeatedly re-mint runner executions, consuming the 4-connection-per-role pool and the single-slot runner semaphore (`asyncio.Semaphore(1)` in `runner/server.py`), starving other legitimate requests for the same tenant. This is a lower-severity variant of SEC-001 that exists independent of the CTE bypass.
- **Fix:** Add a lightweight rate limiter (e.g. `slowapi` keyed on `identity.sub`/tenant) in front of `/v1/query` and `/v1/run`, with a sensible demo-appropriate limit (e.g. 10 req/s burst, 60/min sustained).

---

### 🔵 LOW

#### SEC-004 — `services/data_service/data_service/audit.py` trusts `point in ENFORCEMENT_POINTS` but logs arbitrary keyword `detail` to console/stdout without redaction beyond SQL truncation
- **File:** `services/data_service/data_service/audit.py` lines 36–49
- **Category:** misconfiguration (informational)
- **OWASP:** A09 (logging) — defence-in-depth note only; no secret was found flowing into this path today.
- **Attack scenario:** None observed currently — call sites (`main.py`) only pass structured, non-secret fields (`reason`, `category`, `sql` via `truncate_sql`, `db_role`, etc.), and tokens/passwords are never passed as `detail`. This is purely a "keep it that way" note: because `AuditLog.record(**detail)` logs any keyword argument verbatim (`json.dumps(entry, ...)`) to stdout and keeps it in the in-memory ring buffer exposed via `/v1/audit`, a future call site that accidentally logs a raw header or token would leak it both to container logs and to anyone holding the static `audit_token`.
- **Fix:** Add an explicit denylist/allowlist assertion in `AuditLog.record` (e.g. reject keys named `token`, `password`, `authorization`, `secret`) as a defence-in-depth guard against future regressions, since this is the one place structured fields are not individually typed.

#### SEC-005 — `opa/policies/governed/config/data.json` committed identities use plaintext, guessable-looking emails as the only subject identifier, with no binding to a stronger credential inside OPA itself
- **File:** `opa/policies/governed/config/data.json`
- **Category:** informational / defence-in-depth
- **OWASP:** A01
- **Attack scenario:** None directly exploitable — the actual authorization boundary is the EdDSA-signed JWT `sub`/`tenant` claims verified in `identity.py`/`TokenVerifier`, not the OPA config file by itself, and OPA's own API is protected by bearer tokens (`system/authz.rego`) requiring ≥32-char tokens compared with `hmac`-safe... actually `token_ok` uses a plain `==` via `input.identity == expected`, not constant-time — see below for why this is low severity in context.
- **Note (low-confidence, informational only):** `opa/policies/system/authz.rego`'s `token_ok` does a direct Rego equality comparison (`input.identity == expected`) rather than a constant-time comparison. Rego/OPA's evaluation isn't guaranteed constant-time for string equality, so in theory this could be a timing side-channel on the admin/data-service tokens. In practice this is extremely low risk here (OPA is only reachable from internal Docker networks never exposed to the host or internet, and the tokens are 48-char high-entropy secrets), so this is flagged LOW/informational rather than HIGH, and only worth fixing opportunistically.
- **Fix:** Not urgent given the network isolation, but if hardening further: compare token hashes (e.g. compare `crypto.sha256(input.identity)` against a precomputed hash of the expected token) to remove the timing channel, mirroring the `hmac.compare_digest` pattern already used correctly in `data_service/main.py` (`/v1/audit`) and `runner/server.py` (`/execute`).

---

## Notes on things deliberately NOT flagged
- `compose.prompt-only.yaml`'s `runner-promptonly` with `BYPASSRLS`/broad DB credential and `RUNNER_PASSTHROUGH_ENV: DATABASE_URL` is the intended, clearly-labelled insecure comparison overlay. I confirmed it does **not** leak into `compose.yaml` (the protected `runner` service has no `RUNNER_PASSTHROUGH_ENV`, no DB network membership, and `promptonly_db_net`/`promptonly_sandbox_net` are only declared in the overlay file), so it is out of scope per the review brief.
- RLS/grants in `db/migrations/002_reader_roles_rls.sql` are correctly bound to login roles (not `SET`-table session variables), `FORCE ROW LEVEL SECURITY` is set, and `billing_accounts` has RLS enabled with no policies (default deny) plus no grants — this is sound defence in depth and not a finding.
- JWT handling (`identity.py`, `tokens.py`, `web/main.py`) pins `EdDSA`, requires `exp/iat/nbf/sub/aud/iss/jti`, checks token lifetime against a configured max, and binds agent/data/runner tokens to the same `sub`+`tenant` — no confused-deputy path found between web→agent→MCP→data-service.
- CORS is not configured at all in `web/main.py` (no `CORSMiddleware`), which combined with the custom `X-Requested-With: governed-ui` CSRF check and `SameSite=Strict` cookies is an adequate, same-origin-only design — not a finding.
- CSP, `X-Content-Type-Options`, `Referrer-Policy`, `Cache-Control: no-store` are all set; frontend uses `textContent`, not `innerHTML` — no XSS sink found.
- Dependencies in all `requirements.txt` files are fully pinned (generated by `scripts/lock-deps.sh`); no unpinned ranges found in the services reviewed.
