#!/usr/bin/env python3
"""Generate the rig design SVGs (theme-safe, valid XML).

Adapted from the agent-tools design pack for the standalone `rig` CLI. Three diagrams:
ecosystem (how rig sits over agent-tools), the command/dispatch model, and the
cascade+drift loop. Cards are self-lit (own light fill + dark text/stroke) so they read on
both GitHub dark and light. Output is written to docs/img/ and validated as XML.
"""
from pathlib import Path
import xml.dom.minidom as minidom

OUT = Path(__file__).resolve().parent / "img"
OUT.mkdir(parents=True, exist_ok=True)

PAL = {
    "core":   "#5b6470", "core_t": "#ffffff",
    "skills": "#2da44e", "ahook": "#8250df", "ghook": "#bf8700",
    "ci": "#0969da", "mcp": "#cf222e",
    "card": "#f6f8fa", "card_d": "#d0d7de",
    "text": "#1f2328", "muted": "#57606a", "line": "#8c959f", "wip": "#bf8700",
}
FONT = ('font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,'
        'sans-serif"')


def card(x, y, w, h, fill, stroke, rx=10):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')


def text(x, y, s, size=14, fill=PAL["text"], weight="400", anchor="middle"):
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" '
            f'font-weight="{weight}" text-anchor="{anchor}" {FONT}>{s}</text>')


def line(x1, y1, x2, y2, color=PAL["line"], w=1.6, dash=None, arrow=False):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    a = ' marker-end="url(#arrow)"' if arrow else ""
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
            f'stroke-width="{w}"{d}{a}/>')


def svg(w, h, body, title):
    defs = ('<defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="8" '
            'refY="3" orient="auto" markerUnits="strokeWidth">'
            f'<path d="M0,0 L8,3 L0,6 Z" fill="{PAL["line"]}"/></marker></defs>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
            f'width="{w}" height="{h}" role="img" aria-label="{title}">'
            f'<title>{title}</title>{defs}{body}</svg>')


def pretty(s):
    return minidom.parseString(s).toprettyxml(indent="  ")


# ── 1. ecosystem: rig over agent-tools ────────────────────────────────────────
def ecosystem():
    W, H = 760, 560
    cx, cy = W / 2, H / 2
    b = [text(cx, 36, "rig — sits over agent-tools", 22, PAL["text"], "700")]
    b.append(text(cx, 58, "one config (rig.yaml) → applies the five agent-tools categories",
                  13, PAL["muted"]))
    b.append(f'<circle cx="{cx}" cy="{cy}" r="66" fill="{PAL["core"]}" '
             f'stroke="{PAL["card_d"]}" stroke-width="2"/>')
    b.append(text(cx, cy - 8, "rig", 18, PAL["core_t"], "700"))
    b.append(text(cx, cy + 12, "rig.yaml", 11.5, "#d0d7de"))
    b.append(text(cx, cy + 28, "+ agent-tools", 10.5, "#d0d7de"))
    nodes = [
        ("Skills", PAL["skills"], "advisory rules", "→ ~/.agents/skills", cx, cy - 170),
        ("Agent-hooks", PAL["ahook"], "mid-session guards", "→ ~/.claude/hooks", cx + 230, cy - 60),
        ("Git-hooks", PAL["ghook"], "global dispatcher", "→ core.hooksPath", cx + 150, cy + 175),
        ("CI gates", PAL["ci"], "PR/merge gates", "→ .github/workflows", cx - 150, cy + 175),
        ("MCP", PAL["mcp"], "review + code-search", "→ harness mcp config", cx - 230, cy - 60),
    ]
    cw, ch = 196, 78
    for _, col, _, _, nx, ny in nodes:
        b.append(line(cx, cy, nx, ny, PAL["line"], 1.6))
    for name, col, sub1, sub2, nx, ny in nodes:
        b.append(card(nx - cw / 2, ny - ch / 2, cw, ch, PAL["card"], col))
        b.append(f'<rect x="{nx - cw/2}" y="{ny - ch/2}" width="6" height="{ch}" rx="3" fill="{col}"/>')
        b.append(text(nx, ny - 14, name, 16, PAL["text"], "700"))
        b.append(text(nx, ny + 6, sub1, 12, PAL["muted"]))
        b.append(text(nx, ny + 24, sub2, 11, PAL["muted"]))
    b.append(text(cx, H - 26, "rig CONSUMES agent-tools (read-only); it does not vendor it. "
                  "agent_tools_source points at the checkout.", 11.5, PAL["muted"]))
    return svg(W, H, "".join(b), "rig ecosystem map")


# ── 2. command / dispatch model ───────────────────────────────────────────────
def dispatcher():
    W, H = 980, 340
    b = [text(W / 2, 38, "rig — commands over one engine", 22, PAL["text"], "700")]
    b.append(text(W / 2, 60, "setup & apply share ONE plan builder + executor — the wizard "
                  "is a thin front-end, never drifts", 13, PAL["muted"]))
    cmds = [
        ("rig init", "wizard OR\n--config --yes", PAL["skills"]),
        ("rig apply", "reconcile\nidempotent", PAL["ci"]),
        ("rig status", "two-way\ndrift", PAL["ghook"]),
        ("rig doctor", "deps across\nbrew/apt/dnf…", PAL["ahook"]),
        ("rig export", "write\nrig.yaml", PAL["mcp"]),
    ]
    n = len(cmds)
    cw, ch = 150, 84
    gap = (W - 60 - n * cw) / (n - 1)
    y = 110
    xs = []
    for i, (name, sub, col) in enumerate(cmds):
        x = 30 + i * (cw + gap)
        xs.append(x)
        b.append(card(x, y, cw, ch, PAL["card"], col))
        b.append(f'<rect x="{x}" y="{y}" width="{cw}" height="6" rx="3" fill="{col}"/>')
        b.append(text(x + cw / 2, y + 26, name, 14, PAL["text"], "700"))
        for j, ln in enumerate(sub.split("\n")):
            b.append(text(x + cw / 2, y + 46 + j * 15, ln, 11, PAL["muted"]))
    ey = y + ch + 54
    b.append(card(W / 2 - 230, ey, 460, 44, PAL["card"], PAL["core"]))
    b.append(text(W / 2, ey + 19, "catalog(agent-tools) + config → InstallPlan → executor",
                  13, PAL["text"], "700"))
    b.append(text(W / 2, ey + 36, "idempotent · backs up replaced files · stdlib-only actions",
                  10.5, PAL["muted"]))
    for x in xs:
        b.append(line(x + cw / 2, y + ch, W / 2, ey - 2, PAL["line"], 1.3, arrow=True))
    return svg(W, H, "".join(b), "rig command dispatch model")


# ── 3. cascade + two-way drift ────────────────────────────────────────────────
def cascade():
    W, H = 860, 470
    b = [text(W / 2, 38, "config cascade + two-way drift", 22, PAL["text"], "700")]
    b.append(text(W / 2, 60, "global defaults → per-repo rig.yaml (wins) → disk; drift surfaced "
                  "BOTH ways, never silently reconciled", 12.5, PAL["muted"]))
    # global
    b.append(card(60, 110, 240, 70, PAL["card"], PAL["muted"]))
    b.append(text(180, 138, "~/.config/rig/config.yaml", 12.5, PAL["text"], "700"))
    b.append(text(180, 160, "machine-wide defaults", 11, PAL["muted"]))
    # repo
    b.append(card(60, 220, 240, 70, PAL["card"], PAL["skills"]))
    b.append(text(180, 248, "./rig.yaml  (committed)", 12.5, PAL["text"], "700"))
    b.append(text(180, 270, "source of truth — overrides", 11, PAL["muted"]))
    b.append(line(180, 180, 180, 220, PAL["line"], 1.6, arrow=True))
    # merged → plan
    b.append(card(370, 165, 180, 80, PAL["card"], PAL["ci"]))
    b.append(text(460, 198, "cascaded config", 13, PAL["text"], "700"))
    b.append(text(460, 220, "→ InstallPlan", 11.5, PAL["muted"]))
    b.append(line(300, 145, 370, 195, PAL["line"], 1.5, arrow=True))
    b.append(line(300, 255, 370, 215, PAL["line"], 1.5, arrow=True))
    # disk
    b.append(card(620, 165, 180, 80, PAL["card"], PAL["ghook"]))
    b.append(text(710, 198, "disk state", 13, PAL["text"], "700"))
    b.append(text(710, 220, "skills/hooks/CI/mcp", 11, PAL["muted"]))
    b.append(line(550, 205, 620, 205, PAL["line"], 1.6, arrow=True))
    # drift arrows both ways
    b.append(line(620, 235, 550, 235, PAL["mcp"], 1.6, dash="5 4", arrow=True))
    b.append(text(460, 300, "rig apply: converge config→disk", 12, PAL["ci"], "600"))
    b.append(text(585, 330, "rig status: report disk→config extras (you decide)", 11.5, PAL["mcp"], "600"))
    b.append(text(W / 2, H - 26, "config→disk drift is converged by `rig apply`; disk→config "
                  "extras are reported, not deleted.", 11.5, PAL["muted"]))
    return svg(W, H, "".join(b), "rig cascade and drift")


for fname, gen in [("ecosystem.svg", ecosystem),
                   ("dispatch.svg", dispatcher),
                   ("cascade.svg", cascade)]:
    raw = gen()
    minidom.parseString(raw)  # validate well-formed
    (OUT / fname).write_text(pretty(raw))
    print(f"wrote {fname}  ({len(raw)} bytes raw)")
