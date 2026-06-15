"""install-skill — register the `rig` agent skill so harnesses auto-discover it.

Writes a SKILL.md (Agent Skills standard) into ``~/.agents/skills/rig/`` so Claude Code,
Codex, opencode, Gemini, and Cursor surface ``rig`` as a usable capability. Idempotent:
skips when the SKILL.md is already current. Stdlib-only.
"""

from __future__ import annotations

import os
from pathlib import Path

SKILL_NAME = "rig"
SKILL_MD = """\
---
name: rig
description: >-
  Set up a repository (and a dev machine) from a committed rig.yaml by applying
  agent-tools content — skills, agent-hooks, git-hooks/dispatcher, CI gates, MCP. Use
  when the user wants to bootstrap a repo's guardrails, reconcile it to its config,
  check for config/disk drift, or install the toolchain dependencies. Commands:
  `rig setup` (wizard or --config/--yes), `rig apply` (idempotent reconcile),
  `rig status` (two-way drift), `rig doctor` (dependency bootstrap).
metadata:
  author: alex-mextner
  repo: https://github.com/alex-mextner/rig-cli
---

# rig — the dev-environment umbrella driver

rig configures a repo from a declarative `rig.yaml` (committed by default — it is the
reproducible source of truth) by applying content from the `agent-tools` umbrella repo.

## Commands
```
rig setup                       # interactive wizard (or fallback default if no TUI)
rig setup --config rig.yaml --yes   # headless, non-interactive
rig apply                       # reconcile the repo to rig.yaml (idempotent)
rig apply --dry-run             # print the resolved plan, write nothing
rig status                      # report drift BOTH ways (config↔disk)
rig doctor                      # detect deps; --yes to install across brew/apt/dnf/pacman/zypper
rig export -o rig.yaml          # write a starter rig.yaml from detected defaults
```

## Key facts
- Config cascade: `~/.config/rig/config.yaml` (global) → `./rig.yaml` (per-repo, wins).
  Scope is by LOCATION, never a flag.
- Drift is surfaced both ways and never silently reconciled. `rig apply` converges the
  config→disk side only; disk→config extras are reported for you to decide.
- All install actions are idempotent and back up anything they replace (on_conflict:
  skip|overwrite|backup).
"""


def install_skill() -> int:
    skills_dir = Path(os.path.expanduser("~/.agents/skills")) / SKILL_NAME
    skills_dir.mkdir(parents=True, exist_ok=True)
    target = skills_dir / "SKILL.md"
    if target.is_file() and target.read_text(encoding="utf-8") == SKILL_MD:
        print(f"rig: skill already current at {target}")
        return 0
    target.write_text(SKILL_MD, encoding="utf-8")
    print(f"rig: wrote skill → {target}")
    return 0
