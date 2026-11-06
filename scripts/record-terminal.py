"""Record the governed-agent-demo security presentation.

Structure:
  5 narrative slides → per-level (slide + terminal) × 4 → conclusion slide

Pipeline:
  1. Synthesise 15 narration segments with Piper
  2. Compute sequential non-overlapping offsets
  3. Capture live outputs (all 4 levels + psql)
  4. Build 4 separate terminal casts (one per level)
  5. Render 10 HTML slide PNGs via Playwright
  6. Assemble 14 video segments and mix narration

Usage
    python scripts/record-terminal.py              # full narrated recording
    python scripts/record-terminal.py --silent     # video without narration
    python scripts/record-terminal.py --cast-only  # casts only, no MP4
    python scripts/record-terminal.py --from-cast  # skip capture, re-render

Prerequisites
    make up   (services must be running)
    brew install agg
    pip install piper-tts==1.8.0
    var/voices/en_GB-cori-high.onnx{,.json} present
    cd e2e && npm ci   (Playwright for slide capture)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "recordings"
AUDIO_DIR = ROOT / "var" / "demo_audio_terminal"
VOICES = ROOT / "var" / "voices"
OUT_MP4 = OUT / "agentic-security-demo.mp4"
SEGS_DIR = ROOT / "var" / "demo_segments"
CASTS_DIR = ROOT / "var" / "demo_casts"

SLIDES_HTML = ROOT / "scripts" / "presentation.html"
CAPTURE_JS  = ROOT / "scripts" / "capture-slides.js"
E2E_DIR     = ROOT / "e2e"

W = 90
SCRIPTS_DIR = ROOT / "var" / "demo_scripts"

ASCIINEMA = "/opt/homebrew/bin/asciinema"


# ── bash script templates ─────────────────────────────────────────────────────

# Reusable header for every recording script: sets up the working dir, PS1,
# and a typewrite() function that uses real printf+sleep in a bash loop.
_SCRIPT_HEADER = """\
#!/usr/bin/env bash
set -euo pipefail
cd {root}
export TERM=xterm-256color

typewrite() {{
    local text="$1"
    local delay="${{2:-0.04}}"
    local i
    for (( i=0; i<${{#text}}; i++ )); do
        printf '%s' "${{text:$i:1}}"
        sleep "$delay"
    done
}}

prompt() {{
    printf '\\033[32mgoverned-agent-demo\\033[0m:\\033[36m~\\033[0m $ '
}}

clear
printf '\\n'
"""

# docker compose run without -T so the container inherits the PTY from asciinema
_COMPOSE = (
    "docker compose"
    " --file compose.yaml"
    " --file compose.prompt-only.yaml"
    " --profile test"
    " run --rm --no-deps"
    f" -v {ROOT}/cli:/cli"
    " --entrypoint python"
    " tests /cli/agent_demo.py"
)


def write_level_script(level: str) -> Path:
    """Write a real bash script that records one enforcement level via asciinema."""
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    cmd = f"python cli/agent_demo.py --level cross_tenant --only-level {level}"
    script = SCRIPTS_DIR / f"record_{level}.sh"
    script.write_text(
        _SCRIPT_HEADER.format(root=ROOT)
        + f"""\
prompt
typewrite '{cmd}'
printf '\\n\\n'
sleep 0.5

# Real execution — Qwen LLM, OPA, PostgreSQL all run here
{_COMPOSE} --level cross_tenant --only-level {level}

printf '\\n'
prompt
sleep 8
"""
    )
    script.chmod(0o755)
    return script


def _sq(s: str) -> str:
    """Escape a string for use inside a bash single-quoted argument."""
    return s.replace("'", "'\\''")


def write_l4_script(psql_super_sql: str, psql_reader_sql: str) -> Path:
    """Write a real bash script for L4: three phases showing RLS holds at every layer."""
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    cmd_l4 = "python cli/agent_demo.py --level cross_tenant --only-level L4"
    # Display strings shown via typewrite — single-quote-escaped for bash
    tw_sup = _sq(f"docker compose exec postgres psql -U postgres -d governed -c '{psql_super_sql}'")
    tw_rdr = _sq(
        f"docker compose exec postgres psql -U postgres -d governed"
        f" -c 'SET SESSION AUTHORIZATION acme_reader; {psql_reader_sql}'"
    )
    # Actual exec uses double-quoted SQL — safe because SQL has no $, backticks, or "
    exec_sup = f'docker compose exec postgres psql -U postgres -d governed -c "{psql_super_sql}"'
    exec_rdr = (
        'docker compose exec postgres psql -U postgres -d governed'
        f' -c "SET SESSION AUTHORIZATION acme_reader; {psql_reader_sql}"'
    )
    script = SCRIPTS_DIR / "record_L4.sh"
    script.write_text(
        _SCRIPT_HEADER.format(root=ROOT)
        + f"""\
# ── PHASE 1: direct RLS query via agent_demo (reader role, FORCE RLS) ────────
prompt
typewrite '{cmd_l4}'
printf '\\n\\n'
sleep 0.5
{_COMPOSE} --level cross_tenant --only-level L4
printf '\\n'
sleep 8

# ── PHASE 2: bypass everything — connect directly as postgres superuser ───────
printf '\\033[32m  PHASE 2 — bypassing agent + MCP + OPA, connecting as PostgreSQL superuser:\\033[0m\\n\\n'
sleep 0.4
prompt
typewrite '{tw_sup}'
printf '\\n'
sleep 0.4
{exec_sup}
printf '\\n\\033[33m  ^ Superuser bypasses FORCE RLS — both tenants visible.\\033[0m\\n\\n'
sleep 8

# ── PHASE 3: same direct connection, switch to acme_reader (FORCE RLS active) ─
printf '\\033[32m  PHASE 3 — same direct connection, acme_reader role (FORCE RLS applied):\\033[0m\\n\\n'
sleep 0.4
prompt
typewrite '{tw_rdr}'
printf '\\n'
sleep 0.4
{exec_rdr}
printf '\\n\\033[32m  (0 rows) — query ran, database returned nothing. RLS in the storage engine.\\033[0m\\n\\n'
sleep 12

printf '\\033[32m  ✓  BLOCKED at every level — agent path, direct psql, no matter how deep you go.\\033[0m\\n\\n'
prompt
sleep 5
"""
    )
    script.chmod(0o755)
    return script


def record_cast(script: Path, cast_path: Path) -> None:
    """Run asciinema rec with the given bash script and save the cast."""
    print(f"  Recording {cast_path.name} via asciinema rec…", flush=True)
    subprocess.run(
        [
            ASCIINEMA, "rec",
            "--output-format", "asciicast-v2",
            "--overwrite",
            "--window-size", f"{W}x32",
            "--command", f"bash {script}",
            str(cast_path),
        ],
        check=True,
        cwd=ROOT,
    )
    print(f"    → recorded: {cast_path.name}")


# ── narration ─────────────────────────────────────────────────────────────────

NARRATION_TEXTS = [
    ("title",
     "This is a technical demonstration of agentic AI security. "
     "We show why a single control is not enough — and what it takes to enforce a real boundary."),

    ("problem",
     "AI agents now translate natural language into structured database queries on behalf of users. "
     "This creates a new attack surface. "
     "OWASP lists prompt injection as the number one risk for large language model applications. "
     "An agent that is instructed to restrict access can also be instructed to ignore that restriction."),

    ("threat",
     "There are two independent threat vectors. "
     "Agents can misbehave through hallucination, adversarial prompt injection, or indirect injection "
     "via documents the agent processes. "
     "Separately, humans can misbehave — a malicious insider, a stolen token, or a direct call to the backend "
     "that bypasses the agent entirely. "
     "A system relying only on the agent's cooperation fails against both."),

    ("proposition",
     "The answer is defense in depth — controls enforced at every independent layer. "
     "You can add as many layers as your threat model demands. "
     "This demo shows three: verified identity at the request boundary, "
     "policy enforcement at the service layer, and data isolation at the database. "
     "Each layer holds independently. Bypass one — the next still blocks you."),

    ("overview",
     "We run the same cross-tenant query across four enforcement configurations. "
     "An Acme analyst requests Northstar order data — a multi-tenant access violation. "
     "The first two configurations fail to block it. The last two succeed."),

    ("L1_intro",
     "Level one: prompt only. "
     "The model is given a system prompt instructing it not to access other tenants' data. "
     "No controls exist at the service layer or the database. "
     "This is the default architecture for most production AI agents deployed today. "
     "The model's cooperation is the only thing standing between the request and the data."),

    ("L1_demo",
     "Watch the terminal. "
     "The cross-tenant request is submitted. "
     "The model receives the instruction and runs the query against Northstar's data. "
     "The system prompt said not to cross tenants — the model misbehaved. "
     "Look at the result: 45 Northstar orders, sixty-seven thousand dollars returned in full. "
     "There was nothing at the service or database layer to stop it. "
     "A human injecting a cross-tenant request, or a compromised agent, produces the same outcome. "
     "The system prompt alone is not a security boundary."),

    ("L2_intro",
     "Level two: advisory tool. "
     "The model now has access to a policy-check tool it can call before executing a query. "
     "The tool returns a recommendation. "
     "This pattern is common in AI agent design and is often presented as a safety measure."),

    ("L2_demo",
     "Watch what happens. "
     "The advisory tool is called. It evaluates the request and returns: not recommended. "
     "The model acknowledges this — and runs the query anyway. "
     "The same 45 Northstar rows are returned. "
     "This is the fundamental flaw in advisory-only designs: "
     "the decision was left to the model, and the model decided to proceed. "
     "A human injecting a request directly to the service produces the same result — "
     "because the advisory was never enforced at the service or database layer."),

    ("L3_intro",
     "Level three: backend enforcement with Open Policy Agent. "
     "Every request now carries a signed JWT that cryptographically binds the caller's tenant identity. "
     "OPA evaluates the identity and the query at the service boundary before any SQL is issued. "
     "The agent cannot modify this check."),

    ("L3_demo",
     "Watch the terminal. "
     "The cross-tenant request arrives at the service boundary with a signed JWT. "
     "Open Policy Agent evaluates the verified identity: caller is Acme, query targets Northstar. "
     "The policy rule is clear — this request violates tenant isolation. "
     "The request is denied before any SQL reaches the database. "
     "The agent had no opportunity to override this — the check runs at the service layer, "
     "independent of the model. "
     "Even a fully compromised agent, or a direct backend call, hits this boundary."),

    ("L4_intro",
     "Level four: PostgreSQL row-level security with FORCE RLS enforced. "
     "This is the backstop — it holds even if OPA is down, a token is forged, "
     "or the application layer is bypassed entirely. "
     "The database itself enforces the tenant boundary."),

    ("L4_demo",
     "The cross-tenant query through the agent returns an empty result — the RLS filter is working. "
     "But now watch what happens when we go further. "
     "We connect directly to PostgreSQL, bypassing the agent, the MCP server, and OPA entirely. "
     "As the database superuser, every tenant is visible — Acme and Northstar rows both returned. "
     "Now we switch to the acme_reader role — the application-facing role with FORCE RLS applied. "
     "We run the exact same query for Northstar data. "
     "Zero rows. "
     "Not rejected by policy — the query ran. But the database returned nothing. "
     "Row-level security is enforced below the application, below the agent, below every service layer. "
     "There is no application code to bypass — the boundary is in the storage engine itself."),

    ("conclusion",
     "The results are unambiguous. "
     "Levels one and two returned Northstar data. "
     "Levels three and four blocked it — and level four held even with a direct database connection. "
     "The difference is not the model. It is where enforcement is placed."),

    ("takeaways",
     "For any AI agent operating on sensitive or multi-tenant data, "
     "three controls are required. "
     "Verified identity at the request boundary. "
     "Policy enforcement at the service layer, independent of the agent. "
     "Data isolation at the database, below the application. "
     "Prompt controls guide a cooperative model. "
     "They are not a security boundary."),
]


def synth_narration(voice: str, speed: float) -> list[tuple[str, Path, float]]:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    for f in AUDIO_DIR.glob("seg_*.wav"):
        f.unlink()

    from piper import PiperVoice, SynthesisConfig  # type: ignore[import]
    v = PiperVoice.load(str(VOICES / f"{voice}.onnx"))
    cfg = SynthesisConfig(length_scale=speed, noise_scale=0.6, noise_w_scale=0.75)

    result: list[tuple[str, Path, float]] = []
    for i, (label, text) in enumerate(NARRATION_TEXTS, start=1):
        path = AUDIO_DIR / f"seg_{i:02d}.wav"
        with wave.open(str(path), "wb") as wf:
            v.synthesize_wav(text, wf, syn_config=cfg)
        with wave.open(str(path)) as wf:
            dur = wf.getnframes() / wf.getframerate()
        result.append((label, path, dur))
        print(f"  [{i:02d}] {label:12s}  {dur:5.1f}s  {text[:50]}…")
    return result


DEMO_LABELS = {"L1_demo", "L2_demo", "L3_demo", "L4_demo"}


def sequential_offsets(
    segs: list[tuple[str, Path, float]],
    start: float = 0.5,
    gap: float = 1.0,
    demo_gap: float = 4.0,
) -> list[tuple[float, Path]]:
    """Compute non-overlapping audio offsets.

    demo_gap is used after demo narrations so the shell remains visible
    for several seconds before the next level's intro slide appears.
    """
    offsets, t = [], start
    for label, path, dur in segs:
        offsets.append((t, path))
        t += dur + (demo_gap if label in DEMO_LABELS else gap)
    return offsets


# ── slide capture ─────────────────────────────────────────────────────────────

def capture_slide_pngs(slide_nums: list[int]) -> dict[int, Path]:
    SEGS_DIR.mkdir(parents=True, exist_ok=True)
    specs, pngs = [], {}
    for n in slide_nums:
        png = SEGS_DIR / f"slide_{n:02d}.png"
        specs.append({"url": f"file://{SLIDES_HTML}?n={n}", "output": str(png)})
        pngs[n] = png
    print(f"  Rendering {len(slide_nums)} slides via Playwright…", flush=True)
    subprocess.run(["node", str(CAPTURE_JS), json.dumps(specs)],
                   check=True, cwd=str(E2E_DIR))
    return pngs


def png_to_mp4(png: Path, duration: float, out: Path) -> None:
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-framerate", "25", "-i", str(png),
        "-t", f"{duration:.3f}",
        "-vf", ("scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black"),
        "-c:v", "libx264", "-crf", "20", "-preset", "fast",
        "-pix_fmt", "yuv420p", "-r", "25", "-movflags", "+faststart",
        str(out),
    ], check=True)


# ── video assembly ────────────────────────────────────────────────────────────

def cast_to_mp4(cast_path: Path, duration: float, out: Path) -> None:
    gif = out.with_suffix(".gif")
    # --idle-time-limit 5: compress LLM-thinking pauses to at most 5 s each
    subprocess.run([
        "agg", "--font-size", "20", "--theme", "monokai", "--speed", "1.0",
        "--idle-time-limit", "5", "--last-frame-duration", "1",
        str(cast_path), str(gif),
    ], check=True)
    # tpad clones the last terminal frame to fill the full duration — this is
    # needed because agg skips hold() events (ANSI resets produce no visual
    # change) so the GIF is shorter than the cast's wall-clock length.
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(gif), "-t", f"{duration:.3f}",
        "-vf", (f"tpad=stop_mode=clone:stop_duration={duration + 1},"
                "scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black"),
        "-c:v", "libx264", "-crf", "22", "-preset", "slow",
        "-pix_fmt", "yuv420p", "-r", "25", "-movflags", "+faststart",
        str(out),
    ], check=True)
    gif.unlink(missing_ok=True)


def concat_and_mix(
    segments: list[Path],
    narration: list[tuple[float, Path]],
    out: Path,
) -> None:
    concat_txt = SEGS_DIR / "concat.txt"
    with concat_txt.open("w") as fh:
        for p in segments:
            fh.write(f"file '{p}'\n")

    silent = out.with_name(out.stem + ".silent.mp4")
    print(f"  ffmpeg: concat {len(segments)} segments…", flush=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_txt),
        "-c", "copy", "-movflags", "+faststart", str(silent),
    ], check=True)

    if not narration:
        silent.rename(out)
        print(f"  Video (silent): {out}")
        return

    print(f"  ffmpeg: mixing {len(narration)} narration segments…", flush=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent)]
    for _, p in narration:
        cmd += ["-i", str(p)]
    filters, labels = [], []
    for i, (offset, _) in enumerate(narration, start=1):
        ms = max(0, int(offset * 1000))
        filters.append(f"[{i}:a]aresample=48000,adelay={ms}|{ms}[a{i}]")
        labels.append(f"[a{i}]")
    filters.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:normalize=0,"
          "loudnorm=I=-16:TP=-1.5:LRA=11[aout]"
    )
    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", "-shortest",
        str(out),
    ]
    subprocess.run(cmd, check=True)
    silent.unlink(missing_ok=True)
    print(f"  Video (narrated): {out}")


# ── entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--silent",    action="store_true")
    ap.add_argument("--cast-only", action="store_true")
    ap.add_argument("--from-cast", action="store_true")
    ap.add_argument("--voice",  default="en_GB-cori-high")
    ap.add_argument("--speed",  type=float, default=1.15)
    a = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    SEGS_DIR.mkdir(parents=True, exist_ok=True)
    CASTS_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # ── 1. narration ─────────────────────────────────────────────────────────
    segs: list[tuple[str, Path, float]] = []
    offsets: list[tuple[float, Path]] = []
    total_audio = 0.0

    if not a.silent:
        print("Synthesising narration…", flush=True)
        segs = synth_narration(a.voice, a.speed)
        offsets = sequential_offsets(segs, start=0.5, gap=1.5)
        total_audio = offsets[-1][0] + segs[-1][2]
        print(f"  Total audio: {total_audio:.1f}s  ({len(segs)} segments, no overlaps)")

    off: dict[str, float] = {}
    if segs:
        for (label, _, _), (t, _) in zip(segs, offsets):
            off[label] = t
    else:
        labels = [l for l, _ in NARRATION_TEXTS]
        for i, label in enumerate(labels):
            off[label] = 0.5 + i * 11.0

    # ── 2. segment durations from narration offsets ───────────────────────────
    # Video order: slide(1-5), [slide(6)+demoL1], [slide(7)+demoL2],
    #              [slide(8)+demoL3], [slide(9)+demoL4], slide(10)
    total = total_audio if total_audio else off["takeaways"] + 15.0
    durs = {
        "slide_01": off["problem"]   - 0,
        "slide_02": off["threat"]    - off["problem"],
        "slide_03": off["proposition"] - off["threat"],
        "slide_04": off["overview"]  - off["proposition"],
        "slide_05": off["L1_intro"]  - off["overview"],
        "slide_06": off["L1_demo"]   - off["L1_intro"],
        "demo_L1":  off["L2_intro"]  - off["L1_demo"],
        "slide_07": off["L2_demo"]   - off["L2_intro"],
        "demo_L2":  off["L3_intro"]  - off["L2_demo"],
        "slide_08": off["L3_demo"]   - off["L3_intro"],
        "demo_L3":  off["L4_intro"]  - off["L3_demo"],
        "slide_09": off["L4_demo"]   - off["L4_intro"],
        "demo_L4":  off["conclusion"] - off["L4_demo"],
        "slide_10": total - off["conclusion"] + 5.0,
    }

    print("\nSegment durations:")
    for k, d in durs.items():
        print(f"  {k:12s}: {d:.1f}s")

    # ── 3. record each level in a real shell via asciinema rec ───────────────
    if not a.from_cast:
        print("\nRecording live execution via asciinema rec…", flush=True)

        # L1 / L2 / L3: each gets its own bash script + asciinema session
        for lvl in ("L1", "L2", "L3"):
            script = write_level_script(lvl)
            record_cast(script, CASTS_DIR / f"demo_{lvl}.cast")

        # L4: three-phase bash script (agent path + direct superuser + acme_reader)
        psql_sup_sql = "SELECT tenant_id, count(*) AS rows, round(sum(amount),2) AS total FROM app.orders GROUP BY tenant_id ORDER BY tenant_id;"
        psql_rdr_sql = "SELECT tenant_id, count(*) AS rows FROM app.orders WHERE tenant_id = 'northstar' GROUP BY tenant_id;"
        script_l4 = write_l4_script(psql_sup_sql, psql_rdr_sql)
        record_cast(script_l4, CASTS_DIR / "demo_L4.cast")

    if a.cast_only:
        print(f"\nDone in {time.time()-t0:.0f}s  →  {CASTS_DIR}")
        return

    # ── 5. render slides ──────────────────────────────────────────────────────
    print("\nRendering slides…", flush=True)
    slide_pngs = capture_slide_pngs(list(range(1, 11)))

    slide_mp4s: dict[int, Path] = {}
    for n in range(1, 11):
        key = f"slide_{n:02d}"
        mp4 = SEGS_DIR / f"{key}.mp4"
        print(f"  slide {n:02d} → {durs[key]:.1f}s", flush=True)
        png_to_mp4(slide_pngs[n], durs[key], mp4)
        slide_mp4s[n] = mp4

    # ── 6. convert demo casts ─────────────────────────────────────────────────
    print("\nConverting demo casts…", flush=True)
    demo_mp4s: dict[str, Path] = {}
    for level in ("L1", "L2", "L3", "L4"):
        cast = CASTS_DIR / f"demo_{level}.cast"
        mp4  = SEGS_DIR  / f"demo_{level}.mp4"
        dur  = durs[f"demo_{level}"]
        print(f"  demo_{level} → {dur:.1f}s", flush=True)
        cast_to_mp4(cast, dur, mp4)
        demo_mp4s[level] = mp4

    # ── 7. assemble final video ───────────────────────────────────────────────
    # Order: slide1..5, slide6+demoL1, slide7+demoL2, slide8+demoL3, slide9+demoL4, slide10
    all_segs = (
        [slide_mp4s[1], slide_mp4s[2], slide_mp4s[3], slide_mp4s[4], slide_mp4s[5]]
        + [slide_mp4s[6],  demo_mp4s["L1"]]
        + [slide_mp4s[7],  demo_mp4s["L2"]]
        + [slide_mp4s[8],  demo_mp4s["L3"]]
        + [slide_mp4s[9],  demo_mp4s["L4"]]
        + [slide_mp4s[10]]
    )

    print("\nStitching and mixing narration…", flush=True)
    concat_and_mix(all_segs, offsets, OUT_MP4)
    print(f"\nDone in {time.time()-t0:.0f}s  →  {OUT_MP4}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
