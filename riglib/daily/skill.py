"""``rig daily install-skill`` — register a Claude-Code-discoverable ``daily`` skill.

Mirrors ``riglib/install.py``'s own ``rig`` skill registration exactly (same
``install_named_skill`` worker: SKILL.md into ``~/.agents/skills/daily/`` + symlinked
into every registered skills-dir harness) — this is the SAME mechanism every other
personal CLI in this ecosystem (``tg``, ``review``, ``draw``) uses for its own
``install-skill``, just reusing rig's own multi-harness linking table
(``riglib/harness_skills.py``) instead of re-deriving one.
"""

from __future__ import annotations

from ..install import install_named_skill

SKILL_NAME = "daily"
SKILL_MD = """\
---
name: daily
description: >-
  Generate a categorized "what shipped" report of merged pull requests, ready to paste
  into a Slack Daily channel. Pulls real merged-PR facts via `gh pr list` — never an
  LLM call, never a ticket status alone. Use when asked for a daily/standup update,
  a "what shipped" summary, or a Slack report of recent merges. e.g. `rig daily`.
metadata:
  author: alex-mextner
  repo: https://github.com/alex-mextner/rig-cli
---

# rig daily — merged-PR "what shipped" report

Reports merged pull requests since the last run (or `--since`), grouped into
Security / Infra-CI / Performance / Product-UX / Other, one plain-language fact per
line, ticket/PR reference last in parentheses. Paste the output directly into Slack.

## Invocation
```
rig daily                          # since the last run (first run: last 24h)
rig daily --since 48h              # relative window, read-only (does not save state)
rig daily --since 2026-08-18T00:00:00Z   # explicit UTC timestamp, read-only
rig daily --repo owner/name        # repeatable; overrides the configured repo list
rig daily --dry-run                # print the report, don't advance the saved watermark
```

## Key facts
- Source of truth is `gh pr list --state merged` — a ticket's Linear status is NEVER
  used to decide "did this ship"; only an actual merged PR counts.
- Default repos: `hyperide/hyper-saas`, `hyperide/hyper-ext-e2e`. Override persistently
  in `~/.config/rig/daily.yaml` (`repos: [...]`) or per-run with `--repo`.
- State (the last reported merge time, PER REPO) lives at
  `~/.config/rig/daily-state.json`. A plain `rig daily` run advances each repo's own
  watermark to the newest PR it just reported FOR THAT REPO, so the next run never
  repeats it; a repo that failed to fetch, or a newly-added repo with no watermark yet,
  never borrows another repo's cursor. `--since` is always read-only. If every
  configured repo fails to fetch, the command exits non-zero instead of printing a
  misleading "no PRs" report.
- No AI/LLM call: title/category extraction is mechanical (conventional-commit
  prefix stripped, trailing `(HYP-NNNN)`/`(#NNN)` stripped and re-combined at the end).
"""


def install_skill() -> int:
    return install_named_skill(SKILL_NAME, SKILL_MD)
