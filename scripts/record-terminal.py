"""Record the governed-agent-demo security presentation (Playwright + Chromium, Piper voiceover).

Structure
  Opening slides (problem, proposition, agenda)
  → Chapter 1 · L1 Prompt Only       — no enforcement
  → Chapter 2 · L2 Advisory Tool     — advisory, not enforcement
  → Chapter 3 · L3 Backend OPA       — enforced at the service boundary
  → Chapter 4 · L4 PostgreSQL RLS    — enforced at the database
  → Closing summary

Usage
    python scripts/record-terminal.py                     # → recordings/agentic-security-demo.mp4
    python scripts/record-terminal.py --no-video          # flow test only (no audio, no mp4)
    python scripts/record-terminal.py --silent            # video without narration
    python scripts/record-terminal.py --skip-capture      # reuse previous Docker capture

Prerequisites
    make up                                                # services must be running
    pip install piper-tts==1.8.0 playwright
    python -m playwright install chromium
    # voice files already at var/voices/en_GB-cori-high.onnx{,.json}
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "recordings"
AUDIO = ROOT / "var" / "demo_audio_terminal"
VOICES = ROOT / "var" / "voices"
CAPTURE_FILE = ROOT / "var" / "terminal_capture.txt"
COMPOSE = ["docker", "compose", "--profile", "test"]

# ─── ANSI → HTML ────────────────────────────────────────────────────────────

_ANSI_CSS: dict[str, str] = {
    "1":  "font-weight:700",
    "2":  "opacity:.50",
    "31": "color:#f87171",
    "32": "color:#4ade80",
    "33": "color:#fbbf24",
    "34": "color:#60a5fa",
    "35": "color:#c084fc",
    "36": "color:#22d3ee",
    "90": "color:#6b7280",
}
_ANSI_RE = re.compile(r"\033\[([0-9;]*)m")


def ansi_to_html(text: str) -> str:
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    depth = [0]

    def _sub(m: re.Match) -> str:
        codes = [c for c in m.group(1).split(";") if c]
        if not codes or codes == ["0"]:
            out = "</span>" * depth[0]
            depth[0] = 0
            return out
        styles = [_ANSI_CSS[c] for c in codes if c in _ANSI_CSS]
        if styles:
            depth[0] += 1
            return f'<span style="{";".join(styles)}">'
        return ""

    result = _ANSI_RE.sub(_sub, safe)
    result += "</span>" * depth[0]
    return result


# ─── overlay CSS + JS ───────────────────────────────────────────────────────

OVERLAY_JS = r"""
(() => {
  if (document.getElementById('demo-style')) return;
  const st = document.createElement('style'); st.id = 'demo-style';
  st.textContent = `
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap');
  body { margin:0; background:#0d1117; }
  #demo-slide {
    position:fixed; inset:0; z-index:99990; display:flex; align-items:center;
    justify-content:center; background:radial-gradient(1200px 700px at 20% 10%,
    #1a1040 0%,#0d1117 55%,#060a14 100%); color:#f8fafc;
    font-family:Inter,-apple-system,'Segoe UI',sans-serif;
    opacity:0; transition:opacity .5s ease; pointer-events:none; }
  #demo-slide.show { opacity:1; }
  #demo-slide .inner { width:1600px; }
  #demo-slide .kicker { color:#a5b4fc; font-size:22px; letter-spacing:.14em;
    text-transform:uppercase; font-weight:600; margin-bottom:18px; }
  #demo-slide h1 { font-size:72px; line-height:1.08; margin:0 0 28px;
    letter-spacing:-.02em; font-weight:800; }
  #demo-slide h2 { font-size:48px; line-height:1.15; margin:0 0 34px; font-weight:750; }
  #demo-slide p.lead { font-size:30px; color:#cbd5e1; line-height:1.5;
    margin:0; max-width:1450px; }
  #demo-slide ul { list-style:none; padding:0; margin:0; display:grid; gap:18px; }
  #demo-slide li { font-size:30px; color:#e2e8f0; line-height:1.35;
    padding-left:46px; position:relative; }
  #demo-slide li::before { content:''; position:absolute; left:0; top:12px;
    width:18px; height:18px; border-radius:5px; background:#818cf8; }
  #demo-slide li b { color:#fff; }
  #demo-slide .agenda { display:grid; grid-template-columns:1fr 1fr; gap:20px 60px; }
  #demo-slide .agenda div { font-size:30px; color:#e2e8f0; }
  #demo-slide .agenda div b { display:inline-flex; align-items:center;
    justify-content:center; width:52px; height:52px; border-radius:12px;
    background:#4f46e5; color:#fff; margin-right:16px; font-size:26px;
    font-weight:700; flex-shrink:0; }
  #demo-slide .chapter-num { font-size:140px; font-weight:800; color:#818cf8;
    line-height:1; margin-bottom:8px; }
  #demo-slide .tag { display:inline-block; padding:6px 18px; border-radius:999px;
    font-size:20px; font-weight:700; margin-bottom:18px; }
  #demo-slide .tag.unsafe { background:#7f1d1d; color:#fca5a5; }
  #demo-slide .tag.safe   { background:#14532d; color:#86efac; }
  #demo-slide .tbl { border-collapse:collapse; width:100%; margin-top:16px;
    font-size:26px; }
  #demo-slide .tbl th { text-align:left; color:#a5b4fc; font-size:19px;
    text-transform:uppercase; letter-spacing:.08em; padding:10px 16px;
    border-bottom:1px solid rgba(165,180,252,.35); }
  #demo-slide .tbl td { padding:14px 16px; border-bottom:1px solid rgba(148,163,184,.18);
    color:#e2e8f0; }
  #demo-slide .tbl td.safe  { color:#4ade80; font-weight:700; }
  #demo-slide .tbl td.unsafe { color:#f87171; font-weight:700; }
  #demo-slide .note { margin-top:40px; font-size:22px; color:#fcd34d;
    border-left:4px solid #f59e0b; padding-left:18px; }
  #demo-caption { pointer-events:none; position:fixed; left:50%; bottom:26px;
    transform:translateX(-50%); z-index:99995; background:rgba(15,23,42,.92);
    color:#fff; padding:12px 28px; border-radius:12px;
    font:600 21px Inter,-apple-system,sans-serif; max-width:1550px;
    text-align:center; box-shadow:0 8px 30px rgba(0,0,0,.4); }
  #demo-chip { pointer-events:none; position:fixed; right:36px; top:36px;
    z-index:99995; background:#4f46e5; color:#fff; padding:8px 18px;
    border-radius:999px; font:700 16px Inter,-apple-system,sans-serif;
    box-shadow:0 6px 18px rgba(79,70,229,.4); }
  #demo-terminal { position:fixed; inset:0; z-index:99992; background:#0d1117;
    overflow:hidden; opacity:0; transition:opacity .4s ease; pointer-events:none; }
  #demo-terminal .term-inner { position:absolute; inset:0; padding:36px 52px;
    overflow:hidden; font:15.5px/1.7 'JetBrains Mono',Menlo,'Courier New',monospace;
    color:#e6edf3; white-space:pre-wrap; word-break:break-all; }
  #demo-terminal.show { opacity:1; }
  `;
  document.head.appendChild(st);
  const sl = document.createElement('div'); sl.id = 'demo-slide';
  sl.innerHTML = '<div class="inner"></div>'; document.body.appendChild(sl);
  const tm = document.createElement('div'); tm.id = 'demo-terminal';
  tm.innerHTML = '<div class="term-inner"></div>'; document.body.appendChild(tm);
})()
"""


# ─── Narrator ───────────────────────────────────────────────────────────────

class Narrator:
    def __init__(self, voice: str, enabled: bool, speed: float) -> None:
        self.enabled = enabled
        self.segments: list[tuple[float, Path]] = []
        self.t0 = time.monotonic()
        self.n = 0
        AUDIO.mkdir(parents=True, exist_ok=True)
        if enabled:
            from piper import PiperVoice, SynthesisConfig  # type: ignore[import]
            self.voice = PiperVoice.load(str(VOICES / f"{voice}.onnx"))
            self.cfg = SynthesisConfig(length_scale=speed, noise_scale=0.6, noise_w_scale=0.75)
            for f in AUDIO.glob("seg_*.wav"):
                f.unlink()

    def synth(self, text: str) -> tuple[Path | None, float]:
        if not self.enabled:
            return None, min(1.5, 0.018 * len(text))
        self.n += 1
        path = AUDIO / f"seg_{self.n:02d}.wav"
        with wave.open(str(path), "wb") as w:
            self.voice.synthesize_wav(text, w, syn_config=self.cfg)  # type: ignore[attr-defined]
        with wave.open(str(path), "rb") as r:
            return path, r.getnframes() / r.getframerate()

    def start(self, path: Path | None) -> float:
        now = time.monotonic()
        if path is not None:
            self.segments.append((now - self.t0, path))
        return now


# ─── Scene ──────────────────────────────────────────────────────────────────

class Scene:
    def __init__(self, demo: "Demo", text: str, caption: str | None = None, pad: float = 0.3) -> None:
        self.demo, self.text, self.caption, self.pad = demo, text, caption, pad

    def __enter__(self) -> "Scene":
        path, self.dur = self.demo.narrator.synth(self.text)
        if self.caption is not None:
            self.demo.caption(self.caption)
        self.started = self.demo.narrator.start(path)
        return self

    def __exit__(self, *exc: object) -> bool:
        if exc[0] is None:
            remaining = self.dur + self.pad - (time.monotonic() - self.started)
            if remaining > 0:
                self.demo.page.wait_for_timeout(int(remaining * 1000 * self.demo.hold))
        return False


# ─── Demo ───────────────────────────────────────────────────────────────────

class Demo:
    def __init__(self, page: Page, narrator: Narrator, hold: float = 1.0) -> None:
        self.page, self.narrator, self.hold = page, narrator, hold

    def install(self) -> None:
        self.page.evaluate(OVERLAY_JS)

    def caption(self, text: str | None) -> None:
        self.page.evaluate(
            """t => { let el = document.getElementById('demo-caption');
            if (!t) { el && el.remove(); return; }
            if (!el) { el = document.createElement('div'); el.id = 'demo-caption'; document.body.appendChild(el); }
            el.textContent = t; }""",
            text,
        )

    def chip(self, text: str | None) -> None:
        self.page.evaluate(
            """t => { let el = document.getElementById('demo-chip');
            if (!t) { el && el.remove(); return; }
            if (!el) { el = document.createElement('div'); el.id = 'demo-chip'; document.body.appendChild(el); }
            el.textContent = t; }""",
            text,
        )

    def slide(self, html: str | None) -> None:
        if html is None:
            self.page.evaluate("() => document.getElementById('demo-slide').classList.remove('show')")
            self.page.wait_for_timeout(560)
            return
        self.page.evaluate(
            """h => { const s = document.getElementById('demo-slide'); s.querySelector('.inner').innerHTML = h;
            s.classList.add('show'); }""",
            html,
        )
        self.page.wait_for_timeout(600)

    def terminal(self, html: str | None, scroll_to: float = 0.0) -> None:
        """Show or hide the terminal overlay. html=None hides it."""
        if html is None:
            self.page.evaluate("() => document.getElementById('demo-terminal').classList.remove('show')")
            self.page.wait_for_timeout(450)
            return
        self.page.evaluate(
            """([h, s]) => {
              const t = document.getElementById('demo-terminal');
              t.querySelector('.term-inner').innerHTML = h;
              t.classList.add('show');
              if (s > 0) { const inner = t.querySelector('.term-inner');
                inner.scrollTop = inner.scrollHeight * s; }
            }""",
            [html, scroll_to],
        )
        self.page.wait_for_timeout(450)

    def chapter(self, num: int, title: str, subtitle: str, tag: str) -> None:
        self.caption(None)
        self.terminal(None)
        safe_class = "safe" if tag == "SAFE" else "unsafe"
        with Scene(self, f"Chapter {num}. {title}.", pad=0.5):
            self.slide(
                f'<div class="chapter-num">{num}</div>'
                f'<span class="tag {safe_class}">{tag}</span>'
                f'<h2>{title}</h2>'
                f'<p class="lead">{subtitle}</p>'
            )
        self.slide(None)
        self.chip(f"{num} · {title}")


# ─── Pre-capture ─────────────────────────────────────────────────────────────

def capture_demo_output() -> str:
    """Run the level demo inside the test container and return its ANSI output."""
    print("  Capturing demo output from Docker…", flush=True)
    CAPTURE_FILE.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            *COMPOSE, "run", "--rm", "--no-deps",
            "-v", f"{ROOT}/cli:/cli",
            "--entrypoint", "python",
            "tests", "/cli/agent_demo.py", "--level", "all",
        ],
        capture_output=True, text=True,
        env={**__import__("os").environ, "COMPOSE_FILE": "compose.yaml:compose.prompt-only.yaml"},
    )
    output = result.stdout + result.stderr
    CAPTURE_FILE.write_text(output)
    print(f"  Captured {len(output):,} chars → {CAPTURE_FILE.name}", flush=True)
    return output


def extract_level_block(capture: str, level_id: str) -> str:
    """Extract the lines for one level (L1/L2/L3/L4) from the full capture."""
    lines = capture.splitlines()
    in_block = False
    collecting: list[str] = []
    next_level = {"L1": "L2", "L2": "L3", "L3": "L4", "L4": None}[level_id]

    marker = f"  [{level_id}]"
    end_marker = f"  [{next_level}]" if next_level else "  Summary:"

    for line in lines:
        stripped = re.sub(r"\033\[[0-9;]*m", "", line)
        if not in_block and marker in stripped:
            in_block = True
        if in_block:
            if end_marker and end_marker in stripped:
                break
            collecting.append(line)

    return "\n".join(collecting)


def scenario_block(capture: str, scenario: str) -> str:
    """Return the full output block for one scenario (cross_tenant / viewer)."""
    label = {"cross_tenant": "LEVEL COMPARISON: cross_tenant",
             "viewer": "LEVEL COMPARISON: viewer"}[scenario]
    lines = capture.splitlines()
    collecting: list[str] = []
    in_block = False
    for line in lines:
        stripped = re.sub(r"\033\[[0-9;]*m", "", line)
        if not in_block and label in stripped:
            in_block = True
        if in_block:
            collecting.append(line)
            # stop at the next ### header (different scenario) or end of file
            if len(collecting) > 3 and re.search(r"###.*###", stripped):
                collecting.pop()
                break
    return "\n".join(collecting)


# ─── main recording ──────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> Path | None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = OUT / "_raw_terminal"
    shutil.rmtree(tmp, ignore_errors=True)

    # ── pre-capture ──────────────────────────────────────────────────────────
    if args.skip_capture and CAPTURE_FILE.exists():
        print("  Reusing previous capture.", flush=True)
        capture = CAPTURE_FILE.read_text()
    else:
        capture = capture_demo_output()

    def term_html(level_id: str) -> str:
        block = extract_level_block(capture, level_id)
        if not block.strip():
            return f"<span style='color:#f87171'>(no output captured for {level_id})</span>"
        return ansi_to_html(block)

    # ── Playwright recording ─────────────────────────────────────────────────
    narr_enabled = not args.no_video and not args.silent
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx_args: dict = {"viewport": {"width": 1920, "height": 1080}, "device_scale_factor": 1}
        if not args.no_video:
            ctx_args.update(record_video_dir=str(tmp), record_video_size={"width": 1920, "height": 1080})
        ctx = browser.new_context(**ctx_args)
        page = ctx.new_page()
        narrator = Narrator(args.voice, narr_enabled, args.speed)
        d = Demo(page, narrator, 0.05 if args.no_video else 1.0)

        page.set_content("""<!DOCTYPE html><html><head>
            <meta charset="utf-8">
            <style>html,body{margin:0;padding:0;background:#0d1117;width:1920px;height:1080px}</style>
        </head><body></body></html>""")
        page.wait_for_timeout(400)
        d.install()

        # ════════════════════════════════════════ opening
        d.slide(
            '<div class="kicker">Agentic AI Security</div>'
            '<h1>Why Prompts Are Not<br>Security Boundaries</h1>'
            '<p class="lead">A demonstration across four enforcement configurations — '
            'from no controls to cryptographically enforced identity, policy, '
            'and database-level row security.</p>'
        )
        with Scene(d, "This is a demonstration of agentic A.I. security. "
                      "Specifically: why the agent's instructions are insufficient as a security boundary, "
                      "and where authorization must actually be enforced.", pad=0.5):
            pass

        d.slide(
            '<div class="kicker">The Problem</div>'
            '<h2>AI agents now act on data with real consequences</h2>'
            '<ul>'
            '<li>They run structured queries against live databases on behalf of authenticated users</li>'
            '<li>User intent is expressed in natural language — not in verified credentials</li>'
            '<li>A manipulated prompt, an injected instruction, or a direct backend call bypasses any instruction-level control</li>'
            '<li><b>The agent\'s compliance with its instructions is not the same as enforcement</b></li>'
            '</ul>'
        )
        with Scene(d,
            "AI agents now perform real operations on behalf of users: they run structured queries against "
            "live databases, retrieve sensitive records, and in some configurations write back to production systems. "
            "Each of those operations is constrained today, if at all, by the agent's instructions. "
            "That is an insufficient control. "
            "A manipulated prompt, an injected instruction in retrieved data, or a direct call to the backend "
            "bypasses it entirely. "
            "The agent's compliance is not the same as enforcement.",
            pad=0.4,
        ):
            pass

        d.slide(
            '<div class="kicker">The Approach</div>'
            '<h2>Authorization must be enforced at the backend, independently of the agent</h2>'
            '<ul>'
            '<li><b>Identity</b> — verified by a cryptographically signed token, not by what the agent was told</li>'
            '<li><b>Policy</b> — every query evaluated against the user\'s authorised scope before it reaches the database</li>'
            '<li><b>Row-level security</b> — enforced at PostgreSQL, independent of the service layer</li>'
            '<li><b>Defence in depth</b> — four independent layers, each enforceable on its own</li>'
            '</ul>'
        )
        with Scene(d,
            "The correct approach is to enforce authorization independently of the agent, at the service boundary and "
            "at the database. "
            "Identity must be verified by a cryptographically signed token, not by a prompt claim. "
            "Every query must be evaluated against policy before it reaches the database. "
            "And at the database layer, row-level security provides a final control that holds even if the "
            "layers above it are compromised.",
            pad=0.4,
        ):
            pass

        d.slide(
            '<div class="kicker">In this demonstration</div>'
            '<h2>One cross-tenant query — four enforcement configurations</h2>'
            '<div class="agenda">'
            '<div><b>L1</b> Prompt Only — instructions, no service controls</div>'
            '<div><b>L2</b> Advisory Tool — the agent may consult but can ignore</div>'
            '<div><b>L3</b> Backend OPA — enforced at the service boundary</div>'
            '<div><b>L4</b> PostgreSQL RLS — enforced at the database</div>'
            '</div>'
            '<div class="note">'
            'User: Alice (Acme tenant) · Query: cross-tenant read of Northstar order data · '
            'All output comes from the running services.'
            '</div>'
        )
        with Scene(d,
            "We test a single cross-tenant query — an Acme user requesting Northstar data — "
            "across four enforcement configurations. "
            "Level one: no controls beyond the system prompt. "
            "Level two: an advisory tool the agent may consult, but can ignore. "
            "Level three: backend enforcement through verified identity and Open Policy Agent. "
            "Level four: PostgreSQL reader roles and row-level security as the final backstop.",
            pad=0.4,
        ):
            pass
        d.slide(None)

        # ════════════════════════════════════════ Chapter 1 · L1
        d.chapter(1, "L1 — Prompt Only", "No service controls. Instructions are the only barrier.", "UNSAFE")
        with Scene(d,
            "Level one. No service controls are in place. "
            "The agent's system prompt instructs it not to access other tenants' data. "
            "A compliant model may refuse when asked directly. "
            "But the same SQL query sent to the backend — without going through the model — "
            "executes without restriction. "
            "Here we can see Northstar's order data returned in full. "
            "The system prompt was not consulted. "
            "There is no mechanism connecting the prompt to the backend execution.",
            "L1 · No enforcement — query sent directly to backend",
            pad=0.5,
        ):
            d.terminal(term_html("L1"))

        # ════════════════════════════════════════ Chapter 2 · L2
        d.chapter(2, "L2 — Advisory Tool", "Advisory is not enforcement. The query succeeds regardless.", "UNSAFE")
        with Scene(d,
            "Level two. The agent is given an advisory check tool. "
            "Before running a query, it may call this tool to receive a recommendation. "
            "The tool correctly identifies this as a cross-tenant request and returns: not recommended. "
            "However, the tool's response is advisory only — it is not enforced. "
            "The data path is unchanged. "
            "The query succeeds and Northstar's data is returned, "
            "exactly as in level one. "
            "An advisory recommendation that the agent can ignore is not a security control.",
            "L2 · Advisory says NOT_RECOMMENDED — query succeeds regardless",
            pad=0.5,
        ):
            d.terminal(term_html("L2"))

        # ════════════════════════════════════════ Chapter 3 · L3
        d.chapter(3, "L3 — Backend OPA", "Verified identity and policy, enforced at the service boundary.", "SAFE")
        with Scene(d,
            "Level three. Every query now passes through an enforcement chain before reaching the database. "
            "The user's identity is extracted from a cryptographically signed token — "
            "it cannot be overridden by what the agent was told. "
            "Open Policy Agent evaluates the request against the authorised scope for that tenant. "
            "The query is denied. "
            "The enforcement point is named in the response: O.P.A. "
            "The agent cannot bypass this by rephrasing its instructions, "
            "because the enforcement is independent of the agent entirely.",
            "L3 · DENIED by OPA — identity from signed token, not from prompt",
            pad=0.5,
        ):
            d.terminal(term_html("L3"))

        # ════════════════════════════════════════ Chapter 4 · L4
        d.chapter(4, "L4 — PostgreSQL RLS", "Database-level row security — the final backstop.", "SAFE")
        with Scene(d,
            "Level four. We connect directly to PostgreSQL as the acme reader role, "
            "bypassing the service layer entirely. "
            "This represents a worst case: the service layer is absent or compromised. "
            "The database's row-level security policy applies at the query level. "
            "The cross-tenant filter returns no rows. "
            "The acme reader role sees only Acme's data, regardless of what SQL was submitted. "
            "This control is independent of the application, of O.P.A., and of the agent. "
            "It holds on its own.",
            "L4 · Direct DB connection as acme_reader — RLS filters at the database",
            pad=0.5,
        ):
            d.terminal(term_html("L4"))

        # ════════════════════════════════════════ closing
        d.caption(None)
        d.chip(None)
        d.terminal(None)
        d.slide(
            '<div class="kicker">Summary</div>'
            '<h2>Where enforcement holds — and where it does not</h2>'
            '<table class="tbl">'
            '<tr><th>Level</th><th>Configuration</th><th>Cross-tenant query result</th><th>Verdict</th></tr>'
            '<tr><td>L1</td><td>Prompt only</td><td>Northstar data returned</td><td class="unsafe">UNSAFE</td></tr>'
            '<tr><td>L2</td><td>Advisory tool</td><td>Advisory ignored — data returned</td><td class="unsafe">UNSAFE</td></tr>'
            '<tr><td>L3</td><td>Backend OPA + verified identity</td><td>Denied at service boundary</td><td class="safe">SAFE</td></tr>'
            '<tr><td>L4</td><td>PostgreSQL reader role + RLS</td><td>Zero rows — filtered at database</td><td class="safe">SAFE</td></tr>'
            '</table>'
            '<div class="note">'
            'Prompts are instructions. Permissions require enforcement.'
            '</div>'
        )
        with Scene(d,
            "In summary. "
            "Level one and level two — prompt instructions and advisory tools — do not prevent a cross-tenant read. "
            "Level three and level four — backend policy enforcement and database row-level security — do. "
            "The architecture that holds is: verified identity from a signed token, "
            "policy evaluated at the service boundary, "
            "and row-level security at the database as the final control. "
            "Prompts are instructions. Permissions require enforcement.",
            pad=1.5,
        ):
            pass

        raw = page.video.path() if not args.no_video else None
        ctx.close()
        browser.close()

    if args.no_video:
        print("Flow test complete (no video produced).")
        return None

    out = OUT / "agentic-security-demo.mp4"
    return mux(Path(raw), narrator.segments, out)


# ─── ffmpeg mux ──────────────────────────────────────────────────────────────

def mux(video: Path, segments: list[tuple[float, Path]], out: Path) -> Path:
    if not segments:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
             "-c:v", "libx264", "-crf", "22", "-preset", "slow",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-r", "25", str(out)],
            check=True,
        )
        return out
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video)]
    for _, path in segments:
        cmd += ["-i", str(path)]
    filters, labels = [], []
    for i, (offset, _) in enumerate(segments, start=1):
        ms = max(0, int(offset * 1000))
        filters.append(f"[{i}:a]aresample=48000,adelay={ms}|{ms}[a{i}]")
        labels.append(f"[a{i}]")
    filters.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=11[aout]"
    )
    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "libx264", "-crf", "28", "-preset", "veryslow",
        "-tune", "stillimage", "-pix_fmt", "yuv420p", "-r", "25",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        "-shortest", str(out),
    ]
    subprocess.run(cmd, check=True)
    shutil.rmtree(video.parent, ignore_errors=True)
    return out


# ─── entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Record the governed-agent-demo security presentation.")
    ap.add_argument("--no-video", action="store_true", help="flow test only — no audio, no MP4")
    ap.add_argument("--silent", action="store_true", help="record video without narration")
    ap.add_argument("--skip-capture", action="store_true", help="reuse previous Docker capture")
    ap.add_argument("--voice", default="en_GB-cori-high", help="Piper voice name (without .onnx)")
    ap.add_argument("--speed", type=float, default=1.0, help="Piper length_scale (>1 is slower)")
    a = ap.parse_args()
    t0 = time.time()
    try:
        out = run(a)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
    elapsed = time.time() - t0
    if out:
        print(f"\nDone in {elapsed:.0f}s  →  {out}")
    else:
        print(f"\nDone in {elapsed:.0f}s")
