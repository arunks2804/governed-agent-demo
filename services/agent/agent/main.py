"""Agent service.

Runs a task for an already-authenticated user. Three sources of SQL/code, all
labelled in the returned trace and all executed through the same tools:

  scenario  - scripted plans / prepared bypass tests (deterministic, not LLM output)
  llm       - a local model (Ollama) generates SQL and Python at run time
  manual    - SQL or Python typed by the user in the UI
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .llm import LlmUnavailable, OllamaPlanner, attempted_direct_access
from .scenarios import ORIGIN_LABELS, SCENARIOS
from .tokens import TokenChecker, TokenError
from .tools import ToolBox

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("agent")

SECRETS = Path(os.environ.get("SECRETS_DIR", "/run/secrets"))
MCP_URL = os.environ.get("MCP_URL", "http://mcp-server:8000/mcp")
RUNNER_URLS = {"protected": os.environ.get("RUNNER_URL", "http://runner:8000")}
if os.environ.get("PROMPT_ONLY_RUNNER_URL"):
    RUNNER_URLS["prompt-only"] = os.environ["PROMPT_ONLY_RUNNER_URL"]
INSECURE_DATA_SERVICE_URL = os.environ.get("INSECURE_DATA_SERVICE_URL", "")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3:8b")
TENANT_NAMES = {"acme": "Acme", "northstar": "Northstar"}
TRACE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

checker = TokenChecker(SECRETS / "jwt_public.pem")
runner_control_token = (SECRETS / "runner_control_token").read_text().strip()
planner = (
    OllamaPlanner(os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434"), LLM_MODEL)
    if LLM_PROVIDER == "ollama"
    else None
)

app = FastAPI(title="Governed agent", docs_url=None, redoc_url=None)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_token: str = Field(max_length=4096)
    runner_token: str = Field(max_length=4096)
    mode: Literal["scenario", "llm", "manual_sql", "manual_python"]
    scenario_id: str | None = None
    task: str | None = Field(default=None, max_length=2000)
    sql: str | None = Field(default=None, max_length=20000)
    code: str | None = Field(default=None, max_length=50000)
    runner: Literal["protected", "prompt-only"] = "protected"
    # Which data path the agent uses. "unprotected" needs the insecure overlay.
    environment: Literal["protected", "unprotected"] = "protected"
    # Experiment-only: weaken the access rules in the system prompt. The web UI
    # never sends this, so the app always runs with "strict".
    prompt_variant: Literal["strict", "tenant_restricted", "soft", "none"] = "strict"
    trace_id: str | None = None


def _outcome(steps: list[dict[str, Any]]) -> str:
    tool_steps = [s for s in steps if s.get("kind") in ("mcp", "data", "python", "invalid_tool_call")]
    for step in tool_steps:
        report = _bypass_report(step)
        if report:
            return "bypassed" if "SUCCEEDED" in report.get("result", "") else "blocked"
    if any(not s.get("ok") for s in tool_steps):
        return "denied"
    return "allowed" if tool_steps else "no_tool_calls"


def _bypass_report(step: dict[str, Any]) -> dict[str, Any] | None:
    if step.get("kind") != "python":
        return None
    import json

    for line in reversed((step.get("output", {}).get("stdout") or "").strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("script") == "direct_db_access.py":
            step["bypass_report"] = data
            return data
    return None


def _error(status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": error, "message": message})


@app.post("/v1/run")
async def run(req: RunRequest, request: Request) -> Any:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return _error(401, "unauthenticated", "missing agent token")
    try:
        identity = checker.check(header[7:], req.data_token, req.runner_token)
    except TokenError as exc:
        return _error(401, "unauthenticated", str(exc))

    trace_id = req.trace_id if req.trace_id and TRACE_RE.match(req.trace_id) else f"run-{uuid.uuid4().hex[:12]}"
    own = identity.tenant
    other = next(t for t in TENANT_NAMES if t != own)
    record: dict[str, Any] = {
        "ok": True,
        "trace_id": trace_id,
        "mode": req.mode,
        "identity": {"sub": identity.sub, "tenant": own, "name": identity.name},
        "llm": None,
        "scenario": None,
        "final_answer": None,
    }

    if req.environment == "unprotected" and not INSECURE_DATA_SERVICE_URL:
        return _error(
            409,
            "unprotected_unavailable",
            "the unprotected environment needs the insecure overlay (make up, not make up-protected)",
        )
    toolbox = ToolBox(
        mcp_url=MCP_URL,
        runner_urls=RUNNER_URLS,
        runner_control_token=runner_control_token,
        data_token=req.data_token,
        runner_token=req.runner_token,
        trace_id=trace_id,
        environment=req.environment,
        insecure_url=INSECURE_DATA_SERVICE_URL or None,
    )
    record["environment"] = req.environment
    steps: list[dict[str, Any]] = []
    async with toolbox:
        if req.mode == "scenario":
            scenario = SCENARIOS.get(req.scenario_id or "")
            if scenario is None:
                return _error(404, "unknown_scenario", "unknown scenario")
            if scenario.requires_runner and scenario.requires_runner not in RUNNER_URLS:
                return _error(
                    409, "runner_unavailable", "this scenario needs the prompt-only overlay (make up-compare)"
                )
            record.update(origin=scenario.origin, task=scenario.task, scenario=scenario.public())
            for step in scenario.steps:
                rendered = step.render(own, other)
                if step.kind == "sql":
                    result = await toolbox.call_query_data(rendered)
                else:
                    result = await toolbox.run_python(rendered, runner=step.runner)
                result["generated_by"] = "prepared" if scenario.origin == "prepared-bypass-test" else "scripted"
                steps.append(result)
        elif req.mode in ("manual_sql", "manual_python"):
            record.update(origin="manual")
            if req.mode == "manual_sql":
                if not req.sql:
                    return _error(422, "missing_sql", "sql is required")
                record["task"] = "Manual SQL"
                result = await toolbox.call_query_data(req.sql)
            else:
                if not req.code:
                    return _error(422, "missing_code", "code is required")
                if req.runner not in RUNNER_URLS:
                    return _error(409, "runner_unavailable", f"runner '{req.runner}' is not running")
                record["task"] = f"Manual Python in the {req.runner} runner"
                result = await toolbox.run_python(req.code, runner=req.runner)
                result["direct_access_markers"] = attempted_direct_access(req.code)
            result["generated_by"] = "user"
            steps.append(result)
        else:
            if planner is None:
                return _error(503, "llm_disabled", "live LLM is disabled (LLM_PROVIDER=none)")
            if not req.task:
                return _error(422, "missing_task", "task is required")
            if req.runner not in RUNNER_URLS:
                return _error(409, "runner_unavailable", f"runner '{req.runner}' is not running")
            record.update(
                origin="live-llm",
                task=req.task,
                llm={
                    "provider": "ollama",
                    "model": LLM_MODEL,
                    "runner": req.runner,
                    "prompt_variant": req.prompt_variant,
                    "environment": req.environment,
                },
            )
            try:
                result = await planner.run(req.task, TENANT_NAMES[own], toolbox, req.runner, req.prompt_variant)
            except LlmUnavailable as exc:
                return _error(
                    503,
                    "llm_unavailable",
                    f"{exc}. Is Ollama running with model {LLM_MODEL}? (ollama pull {LLM_MODEL})",
                )
            steps.extend(result["steps"])
            record["final_answer"] = result["final_answer"]

    record["origin_label"] = ORIGIN_LABELS[record["origin"]]
    record["steps"] = steps
    record["outcome"] = _outcome(steps)
    logger.info(
        '{"service": "agent", "trace_id": "%s", "mode": "%s", "outcome": "%s", "steps": %d}',
        trace_id,
        req.mode,
        record["outcome"],
        len(steps),
    )
    return record


@app.get("/v1/scenarios")
async def scenarios() -> dict[str, Any]:
    return {
        "scenarios": [
            {**s.public(), "available": not s.requires_runner or s.requires_runner in RUNNER_URLS}
            for s in SCENARIOS.values()
        ]
    }


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "runners": sorted(RUNNER_URLS),
        "llm": {"provider": LLM_PROVIDER, "model": LLM_MODEL if planner else None},
    }


@app.get("/v1/llm-status")
async def llm_status() -> dict[str, Any]:
    ready = await planner.available() if planner else False
    return {"provider": LLM_PROVIDER, "model": LLM_MODEL if planner else None, "ready": ready}
