"""Isolated Python runner.

Executes generated Python as the unprivileged ``sandbox`` uid with resource
limits, a wall-clock timeout, output caps and full process cleanup. The
container itself holds no database credentials and sits only on the network
shared with the MCP server, so generated code can use MCP but cannot reach
PostgreSQL, OPA, the data service, or the internet.

The server process runs as root solely to switch to the sandbox uid
(CAP_SETUID/SETGID) and kill leftover sandbox processes (CAP_KILL); all other
capabilities are dropped. Its control token arrives via the environment, which
the sandbox uid cannot read from /proc/1/environ.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import pwd
import re
import resource
import signal
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("runner")

CONTROL_TOKEN = os.environ.pop("RUNNER_CONTROL_TOKEN", "")
RUNNER_LABEL = os.environ.get("RUNNER_LABEL", "protected")
MCP_URL = os.environ.get("MCP_URL", "http://mcp-server:8000/mcp")
# Names of server env vars deliberately exposed to sandboxed code. Empty in the
# protected configuration; the prompt-only overlay sets DATABASE_URL here.
PASSTHROUGH = [n for n in os.environ.get("RUNNER_PASSTHROUGH_ENV", "").split(",") if n]
PASSTHROUGH_VALUES = {n: os.environ.pop(n) for n in PASSTHROUGH if n in os.environ}

SANDBOX_USER = "sandbox"
SANDBOX_ROOT = "/sandbox"
LAUNCHER = "/opt/sandbox/launch.py"
MAX_CODE_CHARS = 50_000
MAX_OUTPUT_BYTES = 64 * 1024
LIMITS = {
    "address_space_mb": 512,
    "cpu_seconds_max": 20,
    "max_processes": 32,
    "max_file_mb": 8,
    "max_open_files": 128,
}
WALL_TIMEOUT_S_MAX = 20.0
TRACE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_sandbox = pwd.getpwnam(SANDBOX_USER)
_cleanup_tasks: set[asyncio.Future[None]] = set()
_slot = asyncio.Semaphore(1)  # one execution at a time: no cross-run interference

app = FastAPI(title=f"Python runner ({RUNNER_LABEL})", docs_url=None, redoc_url=None)


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=MAX_CODE_CHARS)
    timeout_s: float = Field(default=10.0, gt=0, le=WALL_TIMEOUT_S_MAX)
    mcp_token: str | None = Field(default=None, max_length=4096)
    trace_id: str | None = None


def _apply_limits(cpu_seconds: int) -> None:
    mb = 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (LIMITS["address_space_mb"] * mb,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_NPROC, (LIMITS["max_processes"],) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (LIMITS["max_file_mb"] * mb,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (LIMITS["max_open_files"],) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _sandbox_pids() -> list[int]:
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/status", encoding="utf-8") as fh:
                fields = dict(line.split(":", 1) for line in fh if ":" in line)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        # Zombies are already dead; the container's init (tini) reaps them.
        if fields.get("State", "").strip().startswith("Z"):
            continue
        if int(fields.get("Uid", "-1").split()[0]) == _sandbox.pw_uid:
            pids.append(int(entry))
    return pids


def _kill_all_sandbox_processes() -> int:
    """Kill every process owned by the sandbox uid, including ones that escaped
    the process group with setsid/daemonisation."""
    killed = 0
    for _ in range(20):
        pids = _sandbox_pids()
        if not pids:
            break
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except ProcessLookupError:
                pass
        time.sleep(0.05)
    return killed


async def _read_capped(stream: asyncio.StreamReader) -> tuple[str, bool]:
    kept = bytearray()
    truncated = False
    while chunk := await stream.read(65536):
        room = MAX_OUTPUT_BYTES - len(kept)
        if room > 0:
            kept.extend(chunk[:room])
        if len(chunk) > room:
            truncated = True
    return kept.decode("utf-8", errors="replace"), truncated


async def _remove_workdir(workdir: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "/bin/rm",
        "-rf",
        "--",
        workdir,
        user=_sandbox.pw_uid,
        group=_sandbox.pw_gid,
        extra_groups=[],
        env={},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


async def _execute(req: ExecuteRequest, trace_id: str) -> dict[str, Any]:
    run_id = uuid.uuid4().hex[:12]
    workdir = f"{SANDBOX_ROOT}/{run_id}"
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": workdir,
        "LANG": "C.UTF-8",
        "MALLOC_ARENA_MAX": "2",
        "MCP_URL": MCP_URL,
        "TRACE_ID": trace_id,
        "RUNNER_LABEL": RUNNER_LABEL,
        **PASSTHROUGH_VALUES,
    }
    if req.mcp_token:
        env["MCP_TOKEN"] = req.mcp_token
    cpu = min(int(req.timeout_s) + 1, LIMITS["cpu_seconds_max"])

    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "/usr/local/bin/python3",
        "-I",
        LAUNCHER,
        workdir,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=SANDBOX_ROOT,
        user=_sandbox.pw_uid,
        group=_sandbox.pw_gid,
        extra_groups=[],
        start_new_session=True,
        preexec_fn=lambda: _apply_limits(cpu),
    )
    try:
        assert proc.stdin and proc.stdout and proc.stderr
        proc.stdin.write(req.code.encode())
        proc.stdin.close()
        readers = asyncio.gather(_read_capped(proc.stdout), _read_capped(proc.stderr))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=req.timeout_s)
        except TimeoutError:
            timed_out = True
        killed = _kill_all_sandbox_processes()
        try:
            (stdout, out_trunc), (stderr, err_trunc) = await asyncio.wait_for(readers, timeout=5)
        except TimeoutError:
            stdout, out_trunc, stderr, err_trunc = "", True, "", True
        await proc.wait()
        await _remove_workdir(workdir)
        leftover = len(_sandbox_pids())
    except BaseException:
        # Request cancelled (e.g. client disconnect) or failed mid-run: never leave
        # sandbox processes running or a work directory behind.
        _kill_all_sandbox_processes()
        _cleanup_tasks.add(task := asyncio.ensure_future(_remove_workdir(workdir)))
        task.add_done_callback(_cleanup_tasks.discard)
        raise

    exit_code = proc.returncode
    signal_name = None
    if exit_code is not None and exit_code < 0:
        signal_name = signal.Signals(-exit_code).name
    return {
        "runner": RUNNER_LABEL,
        "run_id": run_id,
        "trace_id": trace_id,
        "exit_code": exit_code,
        "signal": signal_name,
        "timed_out": timed_out,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "stdout": stdout,
        "stderr": stderr,
        "output_truncated": out_trunc or err_trunc,
        "killed_processes": killed,
        "leftover_processes": leftover,
        "workdir_removed": not os.path.exists(workdir),
        "limits": {**LIMITS, "wall_timeout_s": req.timeout_s, "wall_timeout_s_max": WALL_TIMEOUT_S_MAX},
        "exposed_env": sorted(k for k in env if k != "MCP_TOKEN"),
    }


@app.post("/execute")
async def execute(req: ExecuteRequest, request: Request) -> Any:
    supplied = request.headers.get("authorization", "")
    if not CONTROL_TOKEN or not hmac.compare_digest(supplied.encode(), f"Bearer {CONTROL_TOKEN}".encode()):
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthenticated"})
    trace_id = req.trace_id if req.trace_id and TRACE_RE.match(req.trace_id) else f"run-{uuid.uuid4().hex[:8]}"
    try:
        await asyncio.wait_for(_slot.acquire(), timeout=30)
    except TimeoutError:
        return JSONResponse(status_code=429, content={"ok": False, "error": "runner_busy"})
    try:
        result = await _execute(req, trace_id)
    finally:
        _slot.release()
    logger.info(
        json.dumps(
            {
                "service": f"runner-{RUNNER_LABEL}",
                "enforcement_point": "runner",
                "trace_id": trace_id,
                "exit_code": result["exit_code"],
                "timed_out": result["timed_out"],
                "duration_ms": result["duration_ms"],
                "killed_processes": result["killed_processes"],
            }
        )
    )
    return {"ok": True, **result}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "runner": RUNNER_LABEL}
