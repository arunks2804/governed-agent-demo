"use strict";

// All server data rendered via textContent through h(); never innerHTML.
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const $ = (s) => document.querySelector(s);
const USER_LABEL = {
  "alice@acme.example": "Alice (Acme analyst)",
  "victor@acme.example": "Victor (Acme viewer)",
  "nina@northstar.example": "Nina (Northstar analyst)",
};

async function api(path, body) {
  const r = await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Requested-With": "governed-ui" },
    body: JSON.stringify(body),
  });
  return { ok: r.ok, status: r.status, data: await r.json().catch(() => ({})) };
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), 3500);
}

function dataStep(record) {
  return (record.steps || []).find((s) => ["data", "mcp", "python", "invalid_tool_call"].includes(s.kind));
}

// Reduce a run record to a verdict for one panel.
function summarize(record, env, demo) {
  const step = dataStep(record) || {};
  const out = step.output || {};
  if (demo.kind === "bypass") {
    const rep = step.bypass_report || {};
    const reached = (rep.result || "").includes("SUCCEEDED");
    return {
      cls: reached ? "reached" : "blocked",
      title: reached ? "Direct DB access SUCCEEDED" : "Direct DB access BLOCKED",
      sub: reached ? `Tenants reached: ${(rep.tenants_visible || []).join(", ")}` : (rep.blocked_by || ""),
      code: (step.input || {}).code, codeLabel: "prepared probe (not AI-generated)",
      table: rep.steps ? { columns: ["step", "ok", "detail"], rows: rep.steps.map((s) => [s.step, s.ok ? "yes" : "no", s.detail]) } : null,
    };
  }
  const denied = !step.ok;
  const sql = (step.input || {}).sql;
  if (denied) {
    const point = step.stopped_at || out.category || out.error || "policy";
    return { cls: "blocked", title: `Denied — ${point}`, sub: out.message || "", code: sql, codeLabel: "probe SQL" };
  }
  // Allowed:
  const affected = out.affected_rows;
  const tenants = out.tenants_visible || [];
  const table = out.columns ? { columns: out.columns, rows: out.rows || [] } : null;
  if (env === "unprotected") {
    if (affected != null) return { cls: "changed", title: `Data changed — ${affected} row(s) written`, sub: "On the throwaway copy (reset after).", code: sql, codeLabel: "probe SQL" };
    const leak = tenants.length > 1 || (demo.id === "cross_tenant") || (demo.id === "billing") || (demo.id === "viewer");
    return { cls: leak ? "leaked" : "ok", title: leak ? "Data exposed" : "Returned", sub: tenants.length ? `Tenants visible: ${tenants.join(", ")}` : "No identity or policy check.", code: sql, codeLabel: "probe SQL", table };
  }
  return { cls: "ok", title: "Allowed — own tenant only", sub: tenants.length ? `Tenants visible: ${tenants.join(", ")}` : "Row-level security applied.", code: sql, codeLabel: "probe SQL", table };
}

function tableEl(t) {
  const rows = (t.rows || []).slice(0, 30);
  return h("div", { class: "table-wrap", tabindex: "0" },
    h("table", {},
      h("thead", {}, h("tr", {}, t.columns.map((c) => h("th", { scope: "col" }, c)))),
      h("tbody", {}, rows.map((r) => h("tr", {}, r.map((v) => h("td", {}, v === null ? "NULL" : String(v))))))));
}

function renderProbe(panelBody, record, env, demo) {
  const v = summarize(record, env, demo);
  const kids = [h("span", { class: "src-label" }, v.codeLabel || "probe")];
  if (v.code) kids.push(h("pre", { class: "code" }, v.code));
  kids.push(h("div", { class: `verdict ${v.cls}`, "data-verdict": v.cls }, v.title, v.sub ? h("span", { class: "sub" }, v.sub) : null));
  if (v.table && v.table.rows.length) kids.push(tableEl(v.table));
  const injected = record.steps?.flatMap((s) => (s.output?.dataset?.injected_note ? [s.output.dataset.injected_note] : []))[0];
  if (injected) kids.push(h("div", { class: "injected" }, "Injected into dataset description: ", h("em", {}, injected)));
  panelBody.replaceChildren(...kids);
}

function renderAgent(panelBody, record, env, demo) {
  // Append the live-AI result below the probe.
  const steps = (record.steps || []).filter((s) => ["data", "mcp"].includes(s.kind));
  const lastSql = [...steps].reverse().map((s) => (s.input || {}).sql).find(Boolean);
  const leaked = steps.some((s) => (s.output?.tenants_visible || []).some((t) => t !== "acme"));
  const block = h("div", { class: "ai-answer", "data-testid": "ai-result" },
    h("span", { class: "src-label" }, `live AI — ${demo.user.split("@")[0]}`),
    lastSql ? h("pre", { class: "code" }, lastSql) : null,
    h("div", {}, "Outcome: ", h("strong", {}, record.outcome || "—"), env === "unprotected" && leaked ? " · other-tenant data seen" : ""),
    record.final_answer ? h("div", { class: "muted small" }, "Answer: " + record.final_answer.slice(0, 220)) : null);
  panelBody.append(block);
}

async function runDemo(demo, source, btn) {
  const spin = h("span", { class: "spinner-sm" });
  if (btn) { btn.disabled = true; btn.append(" "); btn.append(spin); }
  for (const env of demo.environments) {
    const body = document.querySelector(`[data-panel="${demo.id}:${env}"]`);
    if (source === "probe") body.replaceChildren(h("span", { class: "idle" }, "Running probe…"));
    else body.append(h("div", { class: "muted small" }, "Running live AI…"));
    const res = await api("/api/demo-run", { demo_id: demo.id, environment: env, source });
    if (!res.ok) { body.replaceChildren(h("div", { class: "verdict blocked" }, "Run error", h("span", { class: "sub" }, res.data.message || res.status))); continue; }
    if (source === "probe") renderProbe(body, res.data, env, demo);
    else renderAgent(body, res.data, env, demo);
  }
  if (btn) { btn.disabled = false; spin.remove(); }
}

function demoCard(demo) {
  const panels = h("div", { class: "panels" },
    demo.environments.map((env) =>
      h("div", { class: `panel ${env}` },
        h("h3", {}, env === "unprotected" ? "Unprotected" : "Protected"),
        h("div", { class: "body", "data-panel": `${demo.id}:${env}` },
          h("span", { class: "idle" }, "Not run yet.")))));
  const probeBtn = h("button", { class: "primary", "data-testid": `run-${demo.id}`, onclick: (e) => runDemo(demo, "probe", e.currentTarget) }, "Run probe");
  const controls = [probeBtn];
  if (demo.kind !== "bypass") {
    controls.push(h("button", { "data-testid": `ai-${demo.id}`, onclick: (e) => runDemo(demo, "agent", e.currentTarget) }, "Run live AI"));
  }
  return h("section", { class: "demo card", id: `demo-${demo.id}`, "data-testid": `demo-${demo.id}` },
    h("div", { class: "demo-head" }, h("span", { class: "demo-num" }, "Demo " + demo.n), h("h2", {}, demo.title)),
    h("p", { class: "risk" }, demo.risk),
    h("p", { class: "prompt" }, h("span", { class: "who" }, USER_LABEL[demo.user] + ": "), "“" + demo.prompt + "”"),
    h("div", { class: "controls" }, controls),
    panels,
    h("p", { class: "takeaway" }, h("strong", {}, "Expected: "), demo.expected));
}

async function init() {
  const res = await fetch("/api/demos").then((r) => r.json()).catch(() => null);
  if (!res) { toast("Cannot reach the demo server."); return; }
  $("#env-note").textContent = res.unprotected_available
    ? "Both environments are running."
    : "Unprotected environment is OFF — run `make up` to enable the comparison.";
  const demos = res.unprotected_available ? res.demos : res.demos.map((d) => ({ ...d, environments: d.environments.filter((e) => e === "protected") }));
  $("#demos").replaceChildren(...demos.map(demoCard));
  // Expose a driver for the recording.
  window.__runDemo = (id, source) => runDemo(demos.find((d) => d.id === id), source || "probe");
}
document.addEventListener("DOMContentLoaded", init);
