# Code Review — governed-agent-demo — 2026-10-12

## Summary
The trust boundary (identity verification → SQL AST allowlist → OPA → per-role
connection pool → RLS) is unusually careful: fail-closed OPA client, frozen
reader-role pools with a role-identity check on every query, signed/audience/context
tokens, CSRF header check on the web BFF, and a sandboxed runner with real
resource limits and process reaping. No HIGH-confidence security hole was found
in that core path. The issues below are mostly test-reliability gaps, a couple
of resource/cleanup edge cases, and some repeated inefficient patterns
(per-request HTTP client creation) rather than correctness bugs in the
enforcement chain.

**Files reviewed:** services/data_service/* (db.py, sql_validator.py, identity.py,
opa_client.py, audit.py, main.py, catalog.py, config.py), services/agent/* (main.py,
llm.py, tools.py, tokens.py, scenarios.py), services/mcp_server/server.py,
services/runner/runner/server.py + sandbox/launch.py + sandbox/governed.py,
services/web/web/main.py + static/app.js, opa/policies/governed/authz.rego,
db/migrations/002_reader_roles_rls.sql, scripts/{gen-secrets,test,record}.sh,
Makefile, tests/{conftest,test_agent_scenarios,test_data_service,test_db_isolation,
test_fail_closed,test_opa_policy,test_policy_change,test_runner_isolation}.py,
e2e/playwright.config.ts, e2e/tests/chaos.spec.ts.

| Severity | Count |
|----------|-------|
| HIGH     | 0     |
| MEDIUM   | 5     |
| LOW      | 4     |

---

## Findings

### 🟡 MEDIUM

#### CR-001 — `Scenario.precondition` is declared metadata but not wired to any behaviour; the side effect it documents lives in a separate, hardcoded string match
- **File:** `services/agent/agent/scenarios.py` (lines 55, 172, 182); `services/web/web/main.py` (lines 268-279)
- **Category:** architecture
- **Detail:** `Scenario.precondition` is documented as `"policy_revoked" | "opa_down" | None` and is set on the `policy_revoked` and `opa_unavailable` scenarios, but nothing in `agent/main.py` or `web/main.py` reads `scenario.precondition`. The actual "revoke OPA access before this run" behaviour in `web/main.py::run()` is triggered by a separate, hardcoded check: `if body.mode == "scenario" and body.scenario_id == "policy_revoked"`. If a developer adds a new scenario with `precondition="policy_revoked"` (as the type hints suggest is supported), it silently gets no side effect, because `web/main.py` only ever compares against the literal id `"policy_revoked"`, not the `precondition` field. This is a latent trap for anyone extending `SCENARIOS`.
- **Fix:** Either drive the web handler off `scenario.precondition` instead of the id:
  ```python
  scenario = SCENARIOS.get(body.scenario_id or "")
  if scenario and scenario.precondition == "policy_revoked":
      ...
  ```
  or remove the unused `precondition` field and inline the coupling as a comment, so there is exactly one source of truth.

#### CR-002 — `test_live_llm_task_goes_through_governed_path` can pass vacuously
- **File:** `tests/test_agent_scenarios.py` (lines 174-183)
- **Category:** test-reliability
- **Detail:**
  ```python
  for step in rec["steps"]:
      if step.get("kind") == "mcp" and step.get("ok") and step.get("executed_as"):
          assert step["executed_as"] == "acme_reader"
  ```
  If the local LLM responds without making any successful `query_data` call (e.g. it only calls `list_datasets`, or every tool call fails/errors), the loop body never executes and the test passes with zero assertions having actually run. This test is marked `live_llm` (opt-in, not part of the default/CI suite) but it is the only test that exercises the real Ollama tool loop end-to-end, so a regression there (e.g. the planner stops calling `query_data` at all) would not be caught.
- **Fix:** Track whether any step satisfied the condition and fail if not:
  ```python
  checked = False
  for step in rec["steps"]:
      if step.get("kind") == "mcp" and step.get("ok") and step.get("executed_as"):
          assert step["executed_as"] == "acme_reader"
          checked = True
  assert checked, "LLM never made a successful query_data call"
  ```

#### CR-003 — `test_regrant_without_grant_option_has_no_effect` is missing the `SET TRANSACTION READ WRITE` every sibling DDL test uses (low confidence, needs a live run to confirm)
- **File:** `tests/test_db_isolation.py` (lines 113-124)
- **Category:** test-reliability
- **Detail:** `acme_reader` has `ALTER ROLE acme_reader SET default_transaction_read_only = on` (`db/migrations/002_reader_roles_rls.sql:14`), so every other test in this file that needs to execute a write-shaped statement first runs `conn.execute("SET TRANSACTION READ WRITE")` before attempting it inside `pytest.raises(errors.InsufficientPrivilege)` (see `test_writes_fail_even_in_read_write_transaction`, `test_cannot_change_policies_or_table`, `test_cannot_create_roles_or_objects`). `test_regrant_without_grant_option_has_no_effect` does not call `SET TRANSACTION READ WRITE` before `conn.execute("GRANT SELECT ON app.orders TO PUBLIC")`. PostgreSQL rejects `GRANT`/`REVOKE` in a read-only transaction with `ERROR: cannot execute GRANT in a read-only transaction` (SQLSTATE 25006) regardless of privilege — this check runs before the "no grant option, nothing happens" semantics the test is trying to exercise. If that's correct, the test either errors out (uncaught exception, not `InsufficientPrivilege`, so it would fail) or — if I'm misremembering PostgreSQL's exact statement-classification list — it already passes today, in which case this note can be discarded. I did not have a live DB to confirm directly, so flagging as low-confidence; please run it in isolation (`pytest tests/test_db_isolation.py::test_regrant_without_grant_option_has_no_effect`) to verify.
- **Fix (if confirmed broken):** add `conn.execute("SET TRANSACTION READ WRITE")` before the `GRANT`, matching the other tests in the file.

#### CR-004 — Per-request `httpx.AsyncClient` creation instead of a shared, lifespan-scoped client
- **File:** `services/web/web/main.py` (lines 169, 176, 186, 200, 235, 267, 324 — a new `httpx.AsyncClient` is opened and torn down on essentially every request handler); `services/agent/agent/tools.py` (`run_python`, line 100, a new client per tool call)
- **Category:** performance / architecture
- **Detail:** Each of `/api/scenarios`, `/api/status`, `/api/policy`, `/api/run`, `/api/reset` opens a brand-new `httpx.AsyncClient` (new connection pool, no keep-alive reuse across requests) instead of reusing one client created in a FastAPI `lifespan` handler. Under the polling the UI already does (`setInterval(refreshStatus, 5000)` in `app.js`), this means a fresh TCP connection to `agent`, `data-service` and `opa` roughly every 5 seconds per open tab, instead of reusing a pooled connection. Functionally harmless at demo scale, but it's the same anti-pattern repeated 7+ times and would show up as connection churn / FD pressure if the UI were used by more than one browser tab or the polling interval were tightened.
- **Fix:** Create one `httpx.AsyncClient` per target service in the `lifespan` context (as `data_service`/`agent` already do for their own clients: `services/data_service/data_service/opa_client.py`), store it on an app-level state object, and reuse it across requests; close it in the shutdown phase of `lifespan`.

#### CR-005 — Runner's single-execution semaphore is released, and leftover-process kill runs, in a fixed order that does not account for the handler coroutine itself being cancelled
- **File:** `services/runner/runner/server.py` (lines 186-197, 229-236)
- **Category:** resource-leak / concurrency (low confidence on reachability)
- **Detail:** `_execute()` calls `_kill_all_sandbox_processes()` only after `asyncio.wait_for(proc.wait(), timeout=req.timeout_s)` returns (normally or via `TimeoutError`). If the `_execute()` coroutine itself is cancelled before reaching that point (e.g. the ASGI server cancels the request task because the client disconnected, which Starlette/Uvicorn can do under some configurations, or under a deployment that adds a disconnect-watcher), the `finally: _slot.release()` in the `/execute` handler still runs and frees the one-at-a-time semaphore, but the orphaned sandboxed child process and its workdir are never cleaned up (the `_kill_all_sandbox_processes()` / `_remove_workdir()` calls are skipped). A subsequent request can then start executing while the previous sandboxed process is still running, violating the documented "one execution at a time: no cross-run interference" invariant, and for the `prompt-only` runner that orphaned process still holds the `DATABASE_URL` credential in its environment. I could not confirm whether the demo's uvicorn/Starlette configuration actually cancels in-flight handlers on client disconnect (by default it generally does not unless `request.is_disconnected()` is polled), so this is flagged as a latent gap rather than a confirmed live bug.
- **Fix:** Wrap the kill/cleanup steps in a `try/finally` (or `asyncio.shield` the wait + always run cleanup in a `finally`) so they run even if `_execute` is cancelled:
  ```python
  try:
      await asyncio.wait_for(proc.wait(), timeout=req.timeout_s)
  except TimeoutError:
      timed_out = True
  finally:
      killed = _kill_all_sandbox_processes()
  ```

---

### 🔵 LOW

#### CR-006 — MCP server's module-level `httpx.AsyncClient` is never closed
- **File:** `services/mcp_server/mcp_server/server.py` (line 41: `_http = httpx.AsyncClient(...)`)
- **Category:** resource-leak
- **Detail:** `_http` is created at import time and used for the lifetime of the process, but there is no `lifespan`/shutdown hook that calls `_http.aclose()`. In this process model (container exits = sockets closed by the kernel) it has no observable effect, but it is inconsistent with `data_service`'s `OpaClient.aclose()` / `Database.close()`, which are called from `lifespan` on shutdown, and would leak file descriptors/connections if this module were ever reloaded in-process (e.g. under a test harness that imports it multiple times) or run with a graceful-shutdown hook expecting clean teardown.
- **Fix:** Add a minimal Starlette lifespan to `build_app()` that closes `_http` on shutdown.

#### CR-007 — `TABLE_TO_DATASET[t]` in the query handler will raise an unhandled `KeyError` (→ bare 500) if a future approved table isn't added to the catalog
- **File:** `services/data_service/data_service/main.py` (line 218); `services/data_service/data_service/catalog.py` (line 33)
- **Category:** error-handling
- **Detail:** `datasets = sorted({TABLE_TO_DATASET[t] for t in validated.tables})` assumes every table `sql_validator.APPROVED_TABLES` allows is also present in `catalog.DATASETS`/`TABLE_TO_DATASET`. Today both sets are `{"orders"}`, so this can't currently fire, but the two allowlists are maintained in two separate files with no shared source of truth or test asserting they stay in sync; adding a second approved table to `sql_validator.py` without a matching catalog entry would turn every query against it into an unhandled 500 instead of a clean "unapproved_table"/"unknown_dataset" denial.
- **Fix:** Use `.get(t)` and treat an unmapped table as a validation failure (`raise Denied(500 or 400, ...)` with a clear message), or add a test that asserts `sql_validator.APPROVED_TABLES == set(catalog.TABLE_TO_DATASET)`.

#### CR-008 — `scripts/gen-secrets.sh` makes the Ed25519 private signing key world-readable on the host (`chmod 644`)
- **File:** `scripts/gen-secrets.sh` (line 60: `chmod 644 "$dir"/*`)
- **Category:** configuration
- **Detail:** The blanket `chmod 644 "$dir"/*` at the end applies to `jwt_private.pem` as well as the password/token files, making the private signing key readable by any local user on the host (the directory itself is `0700`, which mitigates this for other OS users without access to the demo's own user account, but any process running as the same host user — or anything that later tars up `secrets/` for backup — gets plaintext read access to the key that signs every identity in the system). This is explained as "containers run as non-root and need to read it", which is true for the *reader* passwords, but the private key is only ever read by the `web` service.
- **Fix:** Give `jwt_private.pem` its own tighter permission bits (e.g. `chmod 640` and matching group, or just `600` if the container mounts and reads as the file owner) separately from the batch `chmod 644`, since it is more sensitive than the rotating reader-role passwords.

#### CR-009 — `app.js`'s `setInterval(refreshStatus, 5000)` runs forever with no corresponding `clearInterval`
- **File:** `services/web/web/static/app.js` (line 483)
- **Category:** ux (minor)
- **Detail:** This is a single-page, never-torn-down app, so in practice there's no unmount to clean up after — not a leak in this specific app's lifecycle. Noting only because `refreshStatus` fires an unconditional `fetch` every 5s even while `state.busy` is true (a run is in flight) or the tab is backgrounded, slightly increasing load on `/api/status` (which itself fans out to 3 backend calls) during an active LLM run where the UI is already waiting up to a minute. Cosmetic.
- **Fix:** Optional: skip the tick while `state.busy` is true, or use `document.visibilityState` to pause polling when the tab is hidden.

---

## Notes on things that were checked and found solid
- `services/data_service/data_service/db.py`: one `AsyncConnectionPool` per PostgreSQL login role, `current_user` is re-verified on every query before executing caller SQL, `conn.transaction()` + `SET LOCAL statement_timeout` scoped correctly, pool errors mapped to the right `DbError` subclasses (`PoolTimeout`→503, `QueryCanceled`→504, `InsufficientPrivilege`→403). `tests/test_data_service.py::test_concurrent_mixed_tenant_requests_do_not_leak` directly exercises 80 concurrent mixed-tenant requests across 16 threads and asserts no cross-tenant leakage — good coverage for the exact pool-identity risk this architecture is most exposed to.
- `services/data_service/data_service/sql_validator.py`: AST allowlist, node/depth caps, regenerates canonical SQL from the validated tree rather than re-using caller text, pins unqualified approved tables to `app.` so `search_path` can't redirect them.
- `services/data_service/data_service/opa_client.py`: fails closed on any transport error, non-200, invalid JSON, or malformed decision shape; no caching.
- `services/runner/runner/server.py` / `sandbox/launch.py` / `sandbox/governed.py`: real uid drop, rlimits, `asyncio.Semaphore(1)` serialization, capped stdout/stderr, workdir removal, leftover-process sweep — the one edge case is CR-005 above.
- `opa/policies/governed/authz.rego`: default-deny decision, explicit cross-tenant literal check, runtime revocations re-read on every decision (no caching), reasons always populated on deny.
