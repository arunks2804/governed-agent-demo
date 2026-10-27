"use strict";

// All server data is rendered with textContent via h(); never innerHTML.
const state = { identity: null, scenarios: [], status: null, runs: [], mode: "llm", busy: false, current: null };

const POINT_LABELS = {
  identity: "Identity (signed token)",
  request_schema: "Request schema",
  sql_validation: "SQL validation (AST)",
  opa: "OPA policy",
  connection_selection: "Connection selection",
  postgres: "PostgreSQL (role + RLS)",
  result_limits: "Result limits",
  runner: "Runner sandbox",
};
const MODE_COPY = {
  llm: { label: "Task for the agent (live local LLM)", placeholder: "How many orders do we have in each status?",
         hint: (s) => `SQL/code generated at run time by the local model ${s?.llm?.model || ""}. Runs are labelled "Live LLM".` },
  manual_sql: { label: "SQL sent to query_data over MCP", placeholder: "SELECT status, count(*) FROM orders GROUP BY status",
                hint: () => "Goes through MCP → data service → OPA → PostgreSQL, exactly like agent-generated SQL." },
  manual_python: { label: "Python for the sandboxed runner", placeholder: "from governed import query_data\nprint(query_data(\"SELECT count(*) FROM orders\")[\"rows\"])",
                   hint: () => "Use `from governed import query_data` for data. Direct database access is blocked in the protected runner." },
};

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const $ = (sel) => document.querySelector(sel);
const fmtTime = (epochSeconds) => (epochSeconds ? new Date(epochSeconds * 1000).toLocaleTimeString() : "");

async function api(path, { method = "GET", body } = {}) {
  const opts = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
  if (method !== "GET") {
    opts.headers["Content-Type"] = "application/json";
    opts.headers["X-Requested-With"] = "governed-ui";
    opts.body = JSON.stringify(body ?? {});
  }
  try {
    const response = await fetch(path, opts);
    const data = await response.json().catch(() => ({}));
    return { ok: response.ok, status: response.status, data };
  } catch (err) {
    return { ok: false, status: 0, data: { error: "network", message: "The demo server is not reachable." } };
  }
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 3500);
}

// ------------------------------------------------------------------ status

function pill(label, cls, testid) {
  return h("li", { class: `pill ${cls}`, "data-testid": testid }, label);
}

function renderStatus() {
  const s = state.status;
  const list = $("#status");
  if (!s) { list.replaceChildren(pill("status unknown", "", "status-unknown")); return; }
  const revoked = s.revocations ? Object.keys(s.revocations) : [];
  list.replaceChildren(
    pill(`OPA ${s.opa}`, s.opa === "up" ? "up" : "down", "status-opa"),
    pill(`PostgreSQL ${s.postgres}`, s.postgres === "up" ? "up" : "down", "status-postgres"),
    pill(`Agent ${s.agent}`, s.agent === "up" ? "up" : "down", "status-agent"),
    pill(s.llm?.ready ? `LLM ${s.llm.model} (local)` : "Live LLM unavailable", s.llm?.ready ? "up" : "warn", "status-llm"),
    pill(s.runners.includes("prompt-only") ? "Prompt-only runner on" : "Prompt-only runner off",
         s.runners.includes("prompt-only") ? "warn" : "", "status-promptonly"),
    pill(revoked.length ? `Revoked: ${revoked.join(", ")}` : "Policy: default", revoked.length ? "warn" : "up", "status-policy"),
  );
  const tenant = state.identity?.tenant;
  const policy = $("#policy-state");
  if (!tenant) policy.textContent = "Sign in to manage your tenant's access.";
  else if (s.revocations === null) policy.textContent = "OPA is unreachable, so policy state is unknown. Requests are denied (fail closed).";
  else policy.textContent = revoked.includes(tenant)
    ? `${tenant}: query access REVOKED by runtime policy.`
    : `${tenant}: query access granted by policy.`;
  const promptOnly = $("#runner-select option[value='prompt-only']");
  promptOnly.disabled = !s.runners.includes("prompt-only");
  if (promptOnly.disabled && $("#runner-select").value === "prompt-only") $("#runner-select").value = "protected";
  updateControls();
}

async function refreshStatus() {
  const res = await api("/api/status");
  state.status = res.ok ? res.data : null;
  renderStatus();
}

// --------------------------------------------------------------- identity

async function loadIdentities() {
  const res = await api("/api/identities");
  const select = $("#identity-select");
  select.replaceChildren(...(res.data.identities || []).map((i) =>
    h("option", { value: i.sub }, `${i.name}: ${i.role} (${i.tenant})`)));
}

function renderIdentity() {
  const id = state.identity;
  $("#signed-out").hidden = !!id;
  $("#signed-in").hidden = !id;
  if (id) $("[data-testid=whoami]").replaceChildren(
    h("strong", {}, `${id.name}`), ` · ${id.role}`, h("br"), h("span", { class: "muted small mono" }, id.sub),
    h("br"), "Tenant: ", h("strong", { "data-testid": "tenant" }, id.tenant));
  renderStatus();
}

async function login() {
  const res = await api("/api/login", { method: "POST", body: { sub: $("#identity-select").value } });
  if (!res.ok) return toast(res.data.message || "Sign-in failed");
  state.identity = res.data.identity;
  renderIdentity();
  $("#logout-btn").focus();
}

async function logout() {
  await api("/api/logout", { method: "POST" });
  state.identity = null;
  renderIdentity();
  $("#identity-select").focus();
}

// -------------------------------------------------------------- scenarios

function renderScenarios() {
  const list = $("#scenario-list");
  list.replaceChildren(...state.scenarios.map((s) => h("li", { class: "scenario", "data-testid": `scenario-${s.id}` },
    h("span", { class: "title" }, s.title, " ", h("span", { class: `badge ${s.origin}` },
      s.origin === "prepared-bypass-test" ? "prepared test" : "scripted")),
    h("button", { type: "button", class: "run-btn", "data-testid": `run-${s.id}`, "data-available": String(s.available),
                  "aria-label": `Run scenario: ${s.title}`, onclick: () => runRequest({ mode: "scenario", scenario_id: s.id }) },
      "Run"),
    h("span", { class: "expected" }, s.available ? `Expected: ${s.expected}`
      : `Needs the prompt-only overlay (make up-compare). Expected: ${s.expected}`))));
  updateControls();
}

function updateControls() {
  const signedIn = !!state.identity;
  document.querySelectorAll(".run-btn").forEach((b) => {
    b.disabled = state.busy || !signedIn || b.dataset.available === "false";
  });
  for (const id of ["#revoke-btn", "#restore-btn", "[data-testid=custom-submit]"]) $(id).disabled = state.busy || !signedIn;
  $("#reset-btn").disabled = state.busy;
}

// -------------------------------------------------------------------- runs

async function runRequest(body) {
  if (state.busy) return;
  state.busy = true;
  updateControls();
  const view = $("#run-view");
  view.setAttribute("aria-busy", "true");
  view.replaceChildren(h("div", { class: "loading", "data-testid": "loading" }, h("div", { class: "spinner", "aria-hidden": "true" }),
    body.mode === "llm" ? "Running. The local LLM can take up to a minute." : "Running…"));
  const res = await api("/api/run", { method: "POST", body });
  state.busy = false;
  view.setAttribute("aria-busy", "false");
  if (res.ok) {
    state.current = res.data;
    renderRun(res.data);
    await loadRuns();
  } else {
    renderError(res);
  }
  updateControls();
  refreshStatus();
}

function renderError(res) {
  $("#run-view").replaceChildren(h("div", { "data-testid": "run-error" },
    h("h2", {}, "Request not run"),
    h("div", { class: "outcome denied" }, res.data.error || `HTTP ${res.status}`,
      h("span", { class: "sub" }, res.data.message || "")),
  ));
}

function outcomeBanner(rec) {
  const steps = toolSteps(rec);
  const first = steps.find((s) => !s.ok) || steps[0];
  const report = steps.find((s) => s.bypass_report)?.bypass_report;
  let title = "";
  let sub = "";
  switch (rec.outcome) {
    case "allowed": {
      const roles = [...new Set(steps.map((s) => s.executed_as || s.output?.stdout?.match(/executed as (\w+)/)?.[1]).filter(Boolean))];
      title = "Allowed: data returned through MCP";
      sub = roles.length ? `Executed as ${roles.join(", ")}. Row-level security applied to every query.` : "";
      break;
    }
    case "denied": {
      const point = first?.stopped_at;
      title = `Denied at ${POINT_LABELS[point] || point || "enforcement point"}`;
      sub = first?.output?.message || first?.output?.error || "";
      break;
    }
    case "blocked":
      title = "Blocked: direct database access failed";
      sub = report?.blocked_by ? `Blocked by ${report.blocked_by}.` : "";
      break;
    case "bypassed":
      title = "Bypassed: the INSECURE prompt-only configuration reached PostgreSQL directly";
      sub = `Tenants visible to this ${rec.identity?.tenant} user's code: ${(report?.tenants_visible || []).join(", ")}. Only the prompt stood in the way.`;
      break;
    default:
      title = "No tool calls were made";
  }
  return h("div", { class: `outcome ${rec.outcome}`, "data-testid": "outcome", "data-outcome": rec.outcome }, title,
    sub ? h("span", { class: "sub" }, sub) : null);
}

const toolSteps = (rec) => (rec.steps || []).filter((s) => ["mcp", "python", "invalid_tool_call"].includes(s.kind));

function routeFor(step, audit) {
  const nodes = [{ label: "Agent", detail: step.generated_by === "llm" ? "LLM tool call" : step.generated_by, cls: "allow" }];
  const enforcement = step.kind === "mcp" ? (step.output?.enforcement || []) : audit;
  const outcome = (points) => {
    const hits = enforcement.filter((e) => points.includes(e.enforcement_point));
    if (!hits.length) return null;
    return hits.some((e) => e.outcome === "deny" || e.outcome === "error") ? "deny" : "allow";
  };
  const chain = () => {
    const service = outcome(["identity", "request_schema", "sql_validation", "connection_selection"]);
    const opa = outcome(["opa"]);
    const pg = outcome(["postgres", "result_limits"]);
    const role = enforcement.find((e) => e.enforcement_point === "postgres")?.executed_as
      || enforcement.find((e) => e.enforcement_point === "postgres")?.db_role;
    nodes.push({ label: "MCP server", detail: "forwards identity", cls: service ? "allow" : "skipped" });
    nodes.push({ label: "Data service", detail: "token · schema · SQL AST", cls: service || "skipped" });
    nodes.push({ label: "OPA", detail: "authorization", cls: opa || "skipped" });
    nodes.push({ label: "PostgreSQL", detail: role ? `${role} + RLS` : "reader role + RLS", cls: pg || "skipped" });
  };
  if (step.kind === "mcp") {
    if (step.stopped_at === "mcp") nodes.push({ label: "MCP server", detail: "unavailable", cls: "deny" });
    else chain();
  } else if (step.kind === "python") {
    nodes.push({ label: `Runner (${step.runner})`, detail: step.runner === "protected" ? "MCP-only network" : "has DB network + credential",
                 cls: step.runner === "protected" ? "allow" : "insecure" });
    if (step.bypass_report) {
      const ok = step.bypass_report.result.includes("SUCCEEDED");
      const tcp = step.bypass_report.steps.filter((s) => s.step.startsWith("TCP "));
      const detail = ok ? "reached, RLS bypassed"
        : `name unresolvable · ${tcp.filter((s) => !s.ok).length}/${tcp.length} known IPs unreachable`;
      nodes.push({ label: "PostgreSQL (direct)", detail, cls: ok ? "insecure" : "deny" });
    } else if (audit.length) {
      chain();
    }
  }
  const items = [];
  nodes.forEach((n, i) => {
    if (i) items.push(h("li", { class: "arrow", "aria-hidden": "true" }, "→"));
    items.push(h("li", { class: n.cls, "data-state": n.cls },
      h("span", { class: "node-label" }, n.label), h("span", { class: "node-detail" }, n.detail || "")));
  });
  return h("ol", { class: "route", "data-testid": "route", "aria-label": "Execution route" }, items);
}

function decisionFor(step, audit) {
  const source = step.kind === "mcp" ? (step.output?.enforcement || []) : audit;
  const opa = [...source].reverse().find((e) => e.enforcement_point === "opa");
  if (!opa) {
    return h("p", { class: "small muted", "data-testid": "decision" },
      step.kind === "python" && !source.length
        ? "No governed data request was made, so OPA was not consulted."
        : `OPA not consulted: stopped earlier at ${POINT_LABELS[step.stopped_at] || step.stopped_at}.`);
  }
  const rows = [
    ["Decision", h("span", { class: `tag-${opa.outcome}` }, opa.outcome.toUpperCase())],
    ["Action", opa.action],
    ["Reasons", (opa.reasons || []).join("; ") || opa.reason || "-"],
    ["DB role", opa.db_role || "-"],
    ["Row limit", opa.row_limit ?? "-"],
    ["Policy version", opa.policy_version || "-"],
  ];
  return h("dl", { class: "decision", "data-testid": "decision" }, rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v ?? "-")]));
}

function dataTable(columns, rows, testid = "result-table") {
  const shown = rows.slice(0, 50);
  return h("div", {},
    h("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": "Returned data" },
      h("table", { "data-testid": testid },
        h("thead", {}, h("tr", {}, columns.map((c) => h("th", { scope: "col" }, c)))),
        h("tbody", {}, shown.map((r) => h("tr", {}, r.map((v) => h("td", {}, v === null ? "NULL" : String(v)))))))),
    rows.length > shown.length ? h("p", { class: "small muted" }, `Showing ${shown.length} of ${rows.length} rows.`) : null);
}

function resultFor(step) {
  const out = step.output || {};
  if (step.kind === "mcp") {
    if (out.columns) {
      return h("div", {},
        h("p", { class: "small", "data-testid": "row-count" }, `${out.row_count} row(s)${out.truncated ? " (truncated by policy row limit)" : ""} · executed as `,
          h("strong", {}, out.executed_as)),
        out.rls_evidence?.length ? h("p", { class: "small muted", "data-testid": "rls-evidence" },
          "Row-level security: PostgreSQL added ", h("code", {}, out.rls_evidence[0].policy_predicate),
          ` to ${out.rls_evidence.length} table scan(s) for role ${out.executed_as}. Plan filter: `,
          h("code", {}, out.rls_evidence[0].plan_condition)) : null,
        dataTable(out.columns, out.rows));
    }
    if (out.datasets) return h("pre", { class: "code" }, JSON.stringify(out.datasets, null, 2));
    if (out.dataset) return h("pre", { class: "code" }, JSON.stringify(out.dataset.columns, null, 2));
    return h("p", { class: "small", "data-testid": "no-data" }, "No data returned.");
  }
  if (step.kind === "python") {
    const parts = [];
    if (step.bypass_report) {
      parts.push(dataTable(["step", "ok", "detail"], step.bypass_report.steps.map((s) => [s.step, s.ok ? "yes" : "no", s.detail]), "bypass-table"));
    }
    parts.push(h("p", { class: "small" }, `exit code ${out.exit_code ?? "-"}${out.timed_out ? " · TIMED OUT" : ""} · ${out.duration_ms ?? "?"} ms · runner: ${step.runner}`));
    if (out.stdout && !step.bypass_report) parts.push(h("pre", { class: "code", "data-testid": "stdout" }, out.stdout));
    if (out.stderr) parts.push(h("pre", { class: "code", "data-testid": "stderr" }, out.stderr));
    if (out.error) parts.push(h("p", { class: "tag-deny" }, out.error));
    if (step.direct_access_markers?.length) {
      parts.push(h("p", { class: "small tag-truncated", "data-testid": "direct-markers" },
        `Direct-access markers in this code (heuristic label only): ${step.direct_access_markers.join(", ")}`));
    }
    return h("div", {}, parts);
  }
  return h("p", { class: "tag-deny" }, "The model made an invalid tool call.");
}

function enforcementLog(rec) {
  const rows = [...(rec.audit || [])].map((e) => {
    const detail = Object.entries(e).filter(([k]) => !["ts", "service", "trace_id", "enforcement_point", "outcome"].includes(k))
      .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(" | ") : v}`).join("  ");
    return [fmtTime(e.ts), "data-service", POINT_LABELS[e.enforcement_point] || e.enforcement_point, e.outcome, detail];
  });
  for (const s of toolSteps(rec).filter((s) => s.kind === "python")) {
    const o = s.output || {};
    rows.push(["", `runner (${s.runner})`, POINT_LABELS.runner,
      o.timed_out ? "timeout" : o.exit_code === 0 ? "exit 0" : `exit ${o.exit_code}`,
      `sandbox uid · limits: ${o.limits ? `${o.limits.address_space_mb}MB, ${o.limits.max_processes} procs, ${o.limits.wall_timeout_s}s` : "-"} · leftover processes killed: ${o.killed_processes ?? 0} · exposed env: ${(o.exposed_env || []).join(", ")}`]);
    if (s.bypass_report) {
      for (const b of s.bypass_report.steps) rows.push(["", "network", b.step, b.ok ? "reached" : "blocked", b.detail]);
    }
  }
  if (!rows.length) return h("p", { class: "small muted" }, "No enforcement entries.");
  const table = h("table", { "data-testid": "enforcement-log" },
    h("thead", {}, h("tr", {}, ["Time", "Service", "Enforcement point", "Outcome", "Detail"].map((c) => h("th", { scope: "col" }, c)))),
    h("tbody", {}, rows.map((r) => h("tr", {},
      h("td", { class: "mono" }, r[0]), h("td", {}, r[1]), h("td", {}, r[2]),
      h("td", { class: `tag-${r[3] === "allow" || r[3] === "exit 0" || r[3] === "reached" ? "allow" : r[3] === "truncated" ? "truncated" : "deny"}` }, r[3]),
      h("td", { class: "small" }, r[4])))));
  return h("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": "Enforcement log" }, table);
}

function renderRun(rec) {
  const view = $("#run-view");
  const id = rec.identity || {};
  const steps = toolSteps(rec);
  view.replaceChildren(...[
    h("div", { class: "run-head" },
      h("h2", { "data-testid": "run-task" }, rec.task || "Run"),
      h("span", { class: `badge ${rec.origin}`, "data-testid": "origin-badge" }, rec.origin)),
    h("dl", { class: "meta" },
      h("dt", {}, "Identity"), h("dd", { "data-testid": "run-identity" }, `${id.name || ""} · ${id.sub} · tenant ${id.tenant}`),
      h("dt", {}, "Source"), h("dd", { "data-testid": "origin-label" }, rec.origin_label),
      rec.llm ? [h("dt", {}, "LLM"), h("dd", {}, `${rec.llm.model} via ${rec.llm.provider} (local) · runner ${rec.llm.runner}`)] : null,
      h("dt", {}, "Trace"), h("dd", { class: "mono" }, `${rec.trace_id} · ${fmtTime(rec.ran_at)} · ${rec.duration_ms ?? "?"} ms`)),
    (rec.events || []).map((e) => h("p", { class: "event", "data-testid": "event" }, `Policy change before this run: ${e.detail} (${e.applied ? "applied" : "FAILED"})`)),
    outcomeBanner(rec),
    steps.map((step, i) => h("section", { class: "step", "data-testid": "step" },
      h("h3", {}, `Step ${i + 1} · ${step.tool}${step.kind === "python" ? ` · runner ${step.runner}` : " · via MCP"} · ${step.generated_by === "llm" ? "LLM-generated" : step.generated_by === "user" ? "typed by user" : step.generated_by === "prepared" ? "prepared test script" : "scripted"}`),
      h("h3", {}, "Execution route"), routeFor(step, rec.audit || []),
      step.kind === "mcp" && step.input?.sql ? [h("h3", {}, "Generated SQL"), h("pre", { class: "code", "data-testid": "sql" }, step.input.sql),
        step.output?.canonical_sql ? h("p", { class: "small muted" }, "Executed (canonical, regenerated from the AST): ", h("code", {}, step.output.canonical_sql)) : null] : null,
      step.kind === "mcp" && !step.input?.sql ? [h("h3", {}, "Tool input"), h("pre", { class: "code" }, JSON.stringify(step.input))] : null,
      step.kind === "python" ? [h("h3", {}, "Generated code"), h("pre", { class: "code", "data-testid": "code" }, step.input?.code || "")] : null,
      h("h3", {}, "Authorization decision"), decisionFor(step, rec.audit || []),
      h("h3", {}, "Result"), resultFor(step))),
    rec.final_answer ? [h("h3", {}, "Agent answer (LLM)"), h("p", { "data-testid": "final-answer" }, rec.final_answer)] : null,
    h("h3", {}, "Enforcement log"), enforcementLog(rec),
    h("details", {}, h("summary", {}, "Raw run record (JSON)"), h("pre", { class: "code" }, JSON.stringify(rec, null, 2))),
  ].flat(Infinity).filter((node) => node !== null && node !== undefined && node !== false));
  view.focus({ preventScroll: true });
}

async function loadRuns() {
  const res = await api("/api/runs");
  state.runs = res.ok ? res.data.runs : [];
  $("#history").replaceChildren(...state.runs.map((r) => h("li", {},
    h("button", { type: "button", onclick: () => renderRun(r) }, `${fmtTime(r.ran_at)} · ${r.task} · ${r.identity?.tenant}`),
    " ", h("span", { class: `badge ${r.origin}` }, r.outcome))));
}

// ------------------------------------------------------------ custom input

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll("[role=tab]").forEach((t) => {
    const selected = t.dataset.mode === mode;
    t.setAttribute("aria-selected", String(selected));
    t.tabIndex = selected ? 0 : -1;
    if (selected) $("#custom-form").setAttribute("aria-labelledby", t.id);
  });
  const copy = MODE_COPY[mode];
  $("#custom-label").textContent = copy.label;
  $("#custom-input").placeholder = copy.placeholder;
  $("#custom-hint").textContent = copy.hint(state.status);
  $("#runner-row").hidden = mode === "manual_sql";
}

function onTabKey(event) {
  const tabs = [...document.querySelectorAll("[role=tab]")];
  const index = tabs.indexOf(event.target);
  if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
    : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  tabs[next].focus();
  setMode(tabs[next].dataset.mode);
}

function submitCustom(event) {
  event.preventDefault();
  const value = $("#custom-input").value.trim() || $("#custom-input").placeholder;
  const runner = $("#runner-select").value;
  if (state.mode === "llm") runRequest({ mode: "llm", task: value, runner });
  else if (state.mode === "manual_sql") runRequest({ mode: "manual_sql", sql: value });
  else runRequest({ mode: "manual_python", code: value, runner });
}

// ------------------------------------------------------------------ policy

async function changePolicy(action) {
  const tenant = state.identity?.tenant;
  if (!tenant) return;
  const res = await api("/api/policy", { method: "POST", body: { action, tenant } });
  toast(res.ok ? (action === "revoke" ? `OPA: ${tenant} query access revoked. The next request will be denied.` : `OPA: ${tenant} access restored.`)
    : res.data.message || "Policy change failed");
  refreshStatus();
}

async function resetDemo() {
  const res = await api("/api/reset", { method: "POST" });
  state.current = null;
  $("#run-view").replaceChildren(h("div", { class: "empty", "data-testid": "empty" },
    h("h2", {}, "Demo reset"), h("p", { class: "muted" }, "Policy restored to defaults and run history cleared.")));
  await loadRuns();
  await refreshStatus();
  toast(res.ok ? "Demo reset" : "Reset failed");
}

// -------------------------------------------------------------------- init

async function init() {
  // Presentation mode (?present=1): larger type for recordings and screen sharing.
  if (new URLSearchParams(location.search).has("present")) document.body.classList.add("present");
  $("#login-btn").addEventListener("click", login);
  $("#logout-btn").addEventListener("click", logout);
  $("#revoke-btn").addEventListener("click", () => changePolicy("revoke"));
  $("#restore-btn").addEventListener("click", () => changePolicy("restore"));
  $("#reset-btn").addEventListener("click", resetDemo);
  $("#custom-form").addEventListener("submit", submitCustom);
  document.querySelectorAll("[role=tab]").forEach((t) => {
    t.addEventListener("click", () => setMode(t.dataset.mode));
    t.addEventListener("keydown", onTabKey);
  });
  await loadIdentities();
  const [session, scenarios] = await Promise.all([api("/api/session"), api("/api/scenarios")]);
  state.identity = session.data.identity || null;
  state.scenarios = scenarios.ok ? scenarios.data.scenarios : [];
  renderScenarios();
  renderIdentity();
  await refreshStatus();
  setMode("llm");
  await loadRuns();
  setInterval(refreshStatus, 5000);
}

document.addEventListener("DOMContentLoaded", init);
