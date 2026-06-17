"""install-skill — register the `rig` agent skill so harnesses auto-discover it.

Writes a SKILL.md (Agent Skills standard) into ``~/.agents/skills/rig/`` AND symlinks that
skill into the harness's discovery dir (claude-code: ``~/.claude/skills``) so Claude Code
actually lists/loads ``rig`` — a skill in ``~/.agents/skills`` alone is invisible to the
harness. This is the same harness-link rig maintains for every skill it installs via
``apply``; keeping ``install-skill`` consistent means ``rig`` itself is discoverable right
after ``install.sh``. Idempotent: skips when SKILL.md is current and the link is correct;
never clobbers a real (non-symlink) dir already at the harness path. Stdlib-only.
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
  check for config/disk drift, change a single setting, or install the toolchain
  dependencies. Commands: `rig init` (wizard or --config/--yes), `rig apply` (idempotent
  reconcile), `rig status` (two-way drift), `rig config get|set` (read/change one key, then
  reconcile), `rig doctor` (dependency bootstrap).
metadata:
  author: alex-mextner
  repo: https://github.com/alex-mextner/rig-cli
---

# rig — the dev-environment umbrella driver

rig configures a repo from a declarative `rig.yaml` (committed by default — it is the
reproducible source of truth) by applying content from the `agent-tools` umbrella repo.

## Commands
```
rig init                        # interactive wizard (or fallback default if no TUI)
rig init --config rig.yaml --yes    # headless, non-interactive
rig apply                       # reconcile the repo to rig.yaml (idempotent)
rig apply --dry-run             # print the resolved plan, write nothing
rig status                      # report drift BOTH ways (config↔disk)
rig config get harness.auto_mode  # read one nested key (--global for ~/.config/rig/config.yaml)
rig config set harness.auto_mode false   # write one key, then reconcile (the apply engine)
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
    else:
        target.write_text(SKILL_MD, encoding="utf-8")
        print(f"rig: wrote skill → {target}")
    _link_into_harness(skills_dir)
    return 0


# The harness skill-discovery dir for claude-code (mirrors plan._HARNESS_SKILL_DIRS — kept in
# sync; install-skill is the standalone bootstrap path that runs before any rig.yaml exists,
# so it can't read a config and just uses the default harness).
_HARNESS_SKILL_DIR = "~/.claude/skills"


def _link_into_harness(installed_skill_dir: Path) -> None:
    """Symlink the installed rig skill into the harness discovery dir (idempotent).

    Mirrors the ``link_skill_harness`` apply action: a correct symlink is a no-op, a wrong one
    is re-pointed, and a REAL (non-symlink) dir/file already there is left untouched (never
    clobber hand-authored content). Best-effort: a failure here is printed, not fatal — the
    SKILL.md write already succeeded.
    """
    dest = installed_skill_dir.resolve()
    harness_dir = Path(os.path.expanduser(_HARNESS_SKILL_DIR))
    # no self-link when the agents skill dir IS the harness dir (~/.agents/skills == harness)
    if harness_dir.resolve() == installed_skill_dir.parent.resolve():
        return
    link = harness_dir / SKILL_NAME
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            if link.resolve() == dest:
                print(f"rig: harness link already current → {link}")
                return
            link.unlink()
            link.symlink_to(dest)
            print(f"rig: re-pointed harness link → {link}")
            return
        if link.exists():
            print(f"rig: a real dir/file exists at {link} — left untouched (not a rig symlink)")
            return
        link.symlink_to(dest)
        print(f"rig: linked into harness dir → {link}")
    except OSError as exc:
        print(f"rig: warning — could not create harness skill link {link}: {exc}")
