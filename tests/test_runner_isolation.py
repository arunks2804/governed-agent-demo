"""Network and credential isolation, tested by executing probes INSIDE the
actual protected runner (same entry point the agent uses)."""

from __future__ import annotations

import json
import socket
import textwrap
from pathlib import Path

import httpx
import pytest

from conftest import (
    EXPECTED_ROWS,
    PROMPT_ONLY_RUNNER_URL,
    RUNNER_URL,
    auth,
    runner_execute,
    secret,
    token_for,
)

PREPARED = Path(__file__).resolve().parent.parent / "prepared" / "direct_db_access.py"


def ips(host: str) -> list[str]:
    try:
        return sorted(set(socket.gethostbyname_ex(host)[2]))
    except OSError:
        return []


def probe_json(code: str, **kw) -> dict:
    result = runner_execute(textwrap.dedent(code), **kw)
    assert result["exit_code"] == 0, result["stderr"]
    return json.loads(result["stdout"].strip().splitlines()[-1])


CONNECT_PROBE = """
import json, socket, sys
targets = json.loads(sys.argv[1]) if len(sys.argv) > 1 else TARGETS
out = {}
for host, port in targets:
    try:
        with socket.create_connection((host, port), timeout=2):
            out[f"{host}:{port}"] = "CONNECTED"
    except OSError as exc:
        out[f"{host}:{port}"] = type(exc).__name__ + ": " + str(exc)
print(json.dumps(out))
"""


def connect_from_runner(targets: list[tuple[str, int]], url: str = RUNNER_URL) -> dict[str, str]:
    code = f"TARGETS = {targets!r}\n" + CONNECT_PROBE
    return probe_json(code, url=url, timeout_s=15)


@pytest.mark.parametrize("host", ["postgres", "opa", "data-service", "web"])
def test_internal_service_names_do_not_resolve(host: str) -> None:
    out = probe_json(f"""
        import json, socket
        try:
            print(json.dumps({{"ip": socket.gethostbyname({host!r})}}))
        except OSError as exc:
            print(json.dumps({{"error": str(exc)}}))
    """)
    assert "error" in out, f"{host} resolved to {out}"


KNOWN_DB_IPS = ("10.231.10.10", "10.231.20.10")  # fixed in compose.yaml / compose.prompt-only.yaml


def test_harness_confirms_known_db_ip_is_live() -> None:
    """Control: the fixed IP really is PostgreSQL (reachable from the trusted harness)."""
    with socket.create_connection(("10.231.10.10", 5432), timeout=3):
        pass


def test_known_db_ips_unreachable_from_protected_runner() -> None:
    results = connect_from_runner([(ip, 5432) for ip in KNOWN_DB_IPS])
    assert all(v != "CONNECTED" for v in results.values()), results


def test_postgres_unreachable_by_ip() -> None:
    targets = [(ip, 5432) for ip in ips("postgres")]
    assert targets, "harness should resolve postgres"
    results = connect_from_runner(targets)
    assert all(v != "CONNECTED" for v in results.values()), results


def test_opa_admin_api_unreachable_by_ip() -> None:
    targets = [(ip, 8181) for ip in ips("opa")]
    assert targets
    results = connect_from_runner(targets)
    assert all(v != "CONNECTED" for v in results.values()), results


def test_data_service_unreachable_directly() -> None:
    targets = [(ip, 8080) for ip in ips("data-service")]
    assert targets
    results = connect_from_runner(targets)
    assert all(v != "CONNECTED" for v in results.values()), results


def test_no_internet_or_host_egress() -> None:
    results = connect_from_runner(
        [
            ("1.1.1.1", 443),
            ("8.8.8.8", 53),
            ("host.docker.internal", 8080),
            ("host.docker.internal", 2375),
            ("gateway.docker.internal", 2375),
        ]
    )
    assert all(v != "CONNECTED" for v in results.values()), results


def test_bridge_gateway_not_reachable() -> None:
    out = probe_json("""
        import json, socket, struct
        gw = None
        for line in open("/proc/net/route").read().splitlines()[1:]:
            f = line.split()
            if f[1] == "00000000" and f[2] != "00000000":
                gw = socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
        res = {"gateway": gw}
        if gw:
            for port in (22, 2375, 2376, 5432, 8080, 8181):
                try:
                    with socket.create_connection((gw, port), timeout=1.5):
                        res[str(port)] = "CONNECTED"
                except OSError as exc:
                    res[str(port)] = type(exc).__name__
        print(json.dumps(res))
    """)
    assert "CONNECTED" not in out.values(), out


def test_authorized_mcp_access_works_from_runner() -> None:
    out = probe_json(
        """
        import json
        from governed import query_data
        r = query_data("SELECT tenant_id, count(*) FROM orders GROUP BY 1")
        print(json.dumps({"rows": r["rows"], "executed_as": r["executed_as"], "records": r["records"]}))
    """,
        token=token_for("acme", ctx="runner"),
    )
    assert out == {
        "rows": [["acme", EXPECTED_ROWS["acme"]]],
        "executed_as": "acme_reader",
        "records": [{"tenant_id": "acme", "count": EXPECTED_ROWS["acme"]}],
    }


def test_mcp_without_token_denied_from_runner() -> None:
    out = probe_json("""
        import json
        from governed import query_data, GovernedAccessError
        try:
            query_data("SELECT * FROM orders")
            print(json.dumps({"result": "allowed"}))
        except GovernedAccessError as exc:
            print(json.dumps({"result": "denied", "error": exc.payload.get("error")}))
    """)
    assert out == {"result": "denied", "error": "unauthenticated"}


def test_no_secrets_sockets_or_credentials_visible() -> None:
    out = probe_json("""
        import json, os, glob
        res = {}
        res["env_keys"] = sorted(os.environ)
        res["secret_like_env"] = [k for k in os.environ if any(s in k.upper() for s in
                                  ("PASSWORD", "SECRET", "DATABASE", "PG", "OPA", "AWS", "DOCKER", "KUBE", "CONTROL"))]
        res["run_secrets"] = os.path.exists("/run/secrets")
        res["docker_sockets"] = [p for p in ("/var/run/docker.sock", "/run/docker.sock",
                                 "/var/run/containerd/containerd.sock", "/run/containerd/containerd.sock")
                                 if os.path.exists(p)]
        res["sock_files"] = glob.glob("/var/run/*.sock") + glob.glob("/run/*.sock")
        res["kube"] = os.path.exists("/var/run/secrets/kubernetes.io")
        res["docker_config"] = os.path.exists(os.path.expanduser("~/.docker"))
        def readable(p):
            try:
                open(p, "rb").read(1); return True
            except OSError:
                return False
        res["pid1_environ_readable"] = readable("/proc/1/environ")
        res["server_code_readable"] = readable("/srv/runner/server.py")
        res["pid1_cmdline"] = open("/proc/1/cmdline", "rb").read().decode(errors="replace")
        print(json.dumps(res))
    """)
    assert out["secret_like_env"] == []
    assert out["run_secrets"] is False
    assert out["docker_sockets"] == [] and out["sock_files"] == []
    assert out["kube"] is False and out["docker_config"] is False
    assert out["pid1_environ_readable"] is False
    assert out["server_code_readable"] is False
    assert secret("runner_control_token") not in out["pid1_cmdline"]


def test_cannot_escalate_or_write_system_paths() -> None:
    out = probe_json("""
        import json, os
        res = {"uid": os.getuid(), "euid": os.geteuid()}
        for name, fn in {
            "setuid0": lambda: os.setuid(0),
            "write_usr": lambda: open("/usr/local/lib/python3.12/site-packages/governed.py", "a").write("#"),
            "write_etc": lambda: open("/etc/passwd", "a").write("x"),
            "write_root": lambda: open("/pwned", "w").write("x"),
        }.items():
            try:
                fn(); res[name] = "ALLOWED"
            except (OSError, PermissionError) as exc:
                res[name] = type(exc).__name__
        print(json.dumps(res))
    """)
    assert out["uid"] == 20000 and out["euid"] == 20000
    assert "ALLOWED" not in out.values(), out


def test_prepared_bypass_script_blocked_in_protected_runner() -> None:
    result = runner_execute(PREPARED.read_text(), timeout_s=15)
    report = json.loads(result["stdout"].strip().splitlines()[-1])
    assert report["result"] == "DIRECT DATABASE ACCESS BLOCKED", report
    by_step = {s["step"]: s for s in report["steps"]}
    assert by_step["resolve hostname 'postgres'"]["ok"] is False
    # Not just DNS: the database's known, fixed IPs are unreachable too.
    for ip in KNOWN_DB_IPS:
        tcp = next(s for name, s in by_step.items() if name.startswith(f"TCP {ip}:5432"))
        assert tcp["ok"] is False, tcp
        assert "connected" not in tcp["detail"]
    assert by_step["database credential in environment"]["ok"] is False
    assert by_step["log in and read app.orders"]["detail"].startswith("not attempted")
    assert report["blocked_by"].startswith("network isolation")


# ------------------------------------------------------- runner API itself


def test_runner_api_requires_control_token() -> None:
    body = {"code": "print(1)"}
    assert httpx.post(f"{RUNNER_URL}/execute", json=body, timeout=10).status_code == 401
    assert httpx.post(f"{RUNNER_URL}/execute", json=body, headers=auth("nope"), timeout=10).status_code == 401


def test_runner_rejects_unknown_fields() -> None:
    response = httpx.post(
        f"{RUNNER_URL}/execute",
        json={"code": "print(1)", "env": {"X": "1"}},
        headers=auth(secret("runner_control_token")),
        timeout=10,
    )
    assert response.status_code == 422


def test_sandbox_cannot_drive_runner_api_without_token() -> None:
    out = probe_json("""
        import json, urllib.request, urllib.error
        req = urllib.request.Request("http://127.0.0.1:8000/execute", data=b'{"code": "print(1)"}',
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=3); print(json.dumps({"status": 200}))
        except urllib.error.HTTPError as exc:
            print(json.dumps({"status": exc.code}))
    """)
    assert out["status"] == 401


# ------------------------------------------------- prompt-only comparison


@pytest.mark.prompt_only
def test_prepared_bypass_script_succeeds_in_prompt_only_runner() -> None:
    """Deliberately insecure configuration: the same script reaches the DB."""
    result = runner_execute(PREPARED.read_text(), url=PROMPT_ONLY_RUNNER_URL, timeout_s=15)
    report = json.loads(result["stdout"].strip().splitlines()[-1])
    assert result["runner"] == "prompt-only"
    assert report["result"] == "DIRECT DATABASE ACCESS SUCCEEDED", report
    assert report["tenants_visible"] == ["acme", "northstar"]  # broad credential bypasses RLS
    by_step = {s["step"]: s for s in report["steps"]}
    assert by_step["TCP 10.231.20.10:5432 - promptonly_db_net (prompt-only network)"]["ok"] is True


@pytest.mark.prompt_only
def test_protected_runner_still_blocked_while_prompt_only_overlay_runs() -> None:
    targets = [(ip, 5432) for ip in ips("postgres")]
    results = connect_from_runner(targets)
    assert all(v != "CONNECTED" for v in results.values()), results
