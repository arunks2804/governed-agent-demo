"""Code-runner resource limits, timeouts and cleanup."""

from __future__ import annotations

import json
import time

from conftest import runner_execute


def test_cpu_loop_times_out_and_is_killed() -> None:
    started = time.monotonic()
    result = runner_execute("while True:\n    pass\n", timeout_s=2)
    assert result["timed_out"] is True
    assert result["exit_code"] != 0
    assert time.monotonic() - started < 10
    assert result["leftover_processes"] == 0


def test_sleeping_code_hits_wall_clock_timeout() -> None:
    result = runner_execute("import time\ntime.sleep(60)\n", timeout_s=2)
    assert result["timed_out"] is True
    assert result["duration_ms"] < 6000


def test_memory_limit_enforced() -> None:
    result = runner_execute(
        "try:\n    b = bytearray(1024 ** 3)\n    print('ALLOCATED')\nexcept MemoryError:\n    print('MemoryError')\n",
        timeout_s=10,
    )
    assert result["stdout"].strip() == "MemoryError"


def test_process_limit_contains_fork_bomb() -> None:
    code = """
import os, json, time
children = 0
error = None
try:
    for _ in range(500):
        pid = os.fork()
        if pid == 0:
            time.sleep(30)
            os._exit(0)
        children += 1
except OSError as exc:
    error = type(exc).__name__
print(json.dumps({"children": children, "error": error}))
"""
    result = runner_execute(code, timeout_s=10)
    out = json.loads(result["stdout"].strip().splitlines()[-1])
    assert out["error"] is not None
    assert out["children"] < 32
    assert result["killed_processes"] >= out["children"]
    assert result["leftover_processes"] == 0


def test_file_size_limit_enforced() -> None:
    code = """
try:
    with open("big.bin", "wb") as fh:
        fh.write(b"x" * (20 * 1024 * 1024))
    print("WROTE")
except OSError as exc:
    print("BLOCKED", exc.errno)
"""
    result = runner_execute(code, timeout_s=10)
    assert "WROTE" not in result["stdout"]


def test_detached_background_process_is_cleaned_up() -> None:
    code = """
import subprocess
subprocess.Popen(["sleep", "300"], start_new_session=True,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("spawned")
"""
    result = runner_execute(code, timeout_s=5)
    assert result["stdout"].strip() == "spawned"
    assert result["killed_processes"] >= 1
    assert result["leftover_processes"] == 0


def test_workdir_removed_and_runs_do_not_share_state() -> None:
    first = runner_execute("open('marker.txt', 'w').write('secret-from-run-1')\nimport os\nprint(os.getcwd())")
    assert first["workdir_removed"] is True
    second = runner_execute("import os, json\nprint(json.dumps(sorted(os.listdir('/sandbox'))))")
    listing = json.loads(second["stdout"].strip())
    assert len(listing) == 1  # only the current run's own directory
    assert first["stdout"].strip().split("/")[-1] not in listing


def test_output_is_capped() -> None:
    result = runner_execute("print('x' * 300000)", timeout_s=5)
    assert result["output_truncated"] is True
    assert len(result["stdout"]) <= 64 * 1024


def test_runner_recovers_after_abuse() -> None:
    result = runner_execute("print(sum(range(10)))")
    assert result["exit_code"] == 0 and result["stdout"].strip() == "45"
