#!/usr/bin/env python3
"""Generate the rig README visual: a terminal-cast of reconciliation in action.

One informative diagram, not decoration — a real-looking terminal showing `rig apply`
converging the repo to `rig.yaml` (created/updated/skipped) and `rig status` reporting drift
in BOTH directions. Theme-safe (its own dark terminal chrome reads on GitHub light + dark).
Output is written to docs/img/ and validated as well-formed XML.
"""
from pathlib import Path
import xml.dom.minidom as minidom

OUT = Path(__file__).resolve().parent / "img"
OUT.mkdir(parents=True, exist_ok=True)

MONO = ('font-family="SFMono-Regular,Consolas,Liberation Mono,Menlo,'
        'monospace"')

# GitHub-dark-ish ANSI palette
C = {
    "bg": "#0d1117", "bar": "#161b22", "border": "#30363d",
    "fg": "#e6edf3", "muted": "#8b949e", "prompt": "#7ee787",
    "created": "#3fb950", "updated": "#d29922", "skipped": "#6e7681",
    "modified": "#d29922", "missing": "#f85149", "extra": "#58a6ff",
    "red": "#f85149",
}


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def line(x, y, segs):
    """One terminal row: segs is a list of (text, color) tspans (monospace keeps columns)."""
    spans = "".join(f'<tspan fill="{c}">{esc(t)}</tspan>' for t, c in segs)
    return (f'<text x="{x}" y="{y}" font-size="13.5" {MONO} '
            f'xml:space="preserve">{spans}</text>')


def row(status, scolor, item, detail):
    return [(f"  {status:<9} ", scolor), (f"{item:<29}", C["fg"]), (detail, C["muted"])]


def reconcile():
    W, H = 860, 486
    b = [f'<rect x="0" y="0" width="{W}" height="{H}" rx="12" fill="{C["bg"]}" '
         f'stroke="{C["border"]}" stroke-width="1.5"/>']
    # title bar + traffic lights
    b.append(f'<path d="M0,12 a12,12 0 0 1 12,-12 h{W-24} a12,12 0 0 1 12,12 v22 h-{W} z" '
             f'fill="{C["bar"]}"/>')
    for i, col in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
        b.append(f'<circle cx="{22+i*20}" cy="17" r="6" fill="{col}"/>')
    b.append(f'<text x="{W/2}" y="22" font-size="12.5" fill="{C["muted"]}" '
             f'text-anchor="middle" {MONO}>rig — reconcile</text>')

    rows = [
        [("$ ", C["prompt"]), ("rig apply", C["fg"])],
        row("✔ created", C["created"], "skills/shell-timeouts", "→ ~/.agents/skills"),
        row("✔ created", C["created"], "agent-hooks/block-no-verify", "→ ~/.claude/hooks"),
        row("✔ created", C["created"], "ci/secret-scan", "→ .github/workflows"),
        row("✔ updated", C["updated"], "harness/claude-code", "defaultMode → auto"),
        row("· skipped", C["skipped"], "skills/naming", "already current"),
        [("  Summary: ", C["muted"]),
         ("created=12  updated=1  skipped=2  backed_up=0", C["muted"])],
        None,
        [("$ ", C["prompt"]), ("rig status", C["fg"]),
         ("    # later — a hand-edit drifted the repo", C["skipped"])],
        [("  drift detected", C["red"]), ("  (exit 3)", C["muted"])],
        [("  ~ ", C["modified"]), ("ci/secret-scan          ", C["fg"]),
         ("on disk differs from rig.yaml", C["muted"])],
        [("  - ", C["missing"]), ("skills/push-regularly   ", C["fg"]),
         ("declared, missing on disk", C["muted"])],
        [("  + ", C["extra"]), (".github/workflows/rogue.yml  ", C["fg"]),
         ("on disk, not in config", C["muted"])],
    ]
    y = 64
    for r in rows:
        if r is None:
            y += 14
            continue
        b.append(line(24, y, r))
        y += 24

    b.append(f'<text x="24" y="{H-20}" font-size="12" fill="{C["muted"]}" {MONO}>'
             f'apply converges config → disk · status reports drift BOTH ways, '
             f'never silently reconciling</text>')
    inner = "".join(b)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
            f'width="{W}" height="{H}" role="img" '
            f'aria-label="rig apply converges to rig.yaml; rig status reports drift both ways">'
            f'<title>rig apply + status — reconciliation in action</title>{inner}</svg>')


for fname, gen in [("reconcile.svg", reconcile)]:
    raw = gen()
    minidom.parseString(raw)  # validate well-formed XML
    (OUT / fname).write_text(minidom.parseString(raw).toprettyxml(indent="  "))
    print(f"wrote {fname}  ({len(raw)} bytes raw)")
