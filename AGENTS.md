# AGENTS.md — rig-cli

Rules for agents working in this repo. English only (no Cyrillic anywhere in repo docs).

## What this is

`rig` is the dev-environment umbrella driver: a standalone Python CLI that sets up a repo
from a committed `rig.yaml` by applying `agent-tools` content (skills, agent-hooks,
git-hook dispatcher, CI gates, MCP) and provisioning the agent harness's auto/permission
mode. It is a peer to `tg-cli` / `review-cli`, not part of `agent-tools` — it *consumes*
agent-tools read-only.

**`rig init` and `rig apply` are the two real commands.** `init` is first-run onboarding
(no config yet → scaffold `rig.yaml` + wire the catalog in, walking the user through it);
`apply` is the steady-state declarative reconcile (config exists → converge the disk to it).
They are distinct, NOT synonyms. Interactivity (full TUI / semi / non-interactive `--yes`) is
**orthogonal** to the command — both `init` and `apply` run in any of the three modes,
decided by TTY + config + flags. `init` is the canonical onboarding command (the front door).
(The old `setup` back-compat alias has been removed — `init` is the one onboarding command.)

## Hard rules

- **Stdlib-only at import time.** Every `riglib/*` module imports only the standard library
  when loaded. Heavy/optional deps — `yaml`, `textual` — are imported lazily inside the
  function that needs them. `rig --help` and `rig doctor` must run with zero third-party
  imports. Do not add a top-level `import yaml`/`import textual`.
- **One engine, two front-ends.** `rig init` (wizard) and `rig apply` must share the same
  `plan.build` + `actions.run_plan`. Never fork the executor for the TUI. If you add a
  capability, add it to the headless engine first and let the wizard call it.
- **Harness auto-mode is provisioned through the reconciler, like every other target.** The
  `harness:` block flows config → `plan.build` (one `apply_harness` action) → `run_plan`
  (`actions/runner.py::_do_apply_harness`), writing only the managed permission key into the
  harness settings JSON, idempotent + backup-on-conflict, with drift surfaced by `rig
  status`. Recommend `auto_mode: true` by default — it is safe *because* the agent-hook
  guards (incl. `block-raw-pr-merge`) are installed in the same apply. claude-code is
  implemented; opencode is documented-but-reserved (validation fails closed on it).
- **`rig.yaml` is committed by default.** It is the reproducible source of truth. Do not
  add an "is rig.yaml optional?" flag. Global config lives at `~/.config/rig/config.yaml`;
  per-repo `rig.yaml` overrides it; scope is by location, never a flag.
- **Drift is surfaced both ways, never silently reconciled.** `rig apply` converges
  config→disk only. `disk→config` extras are reported, never auto-deleted.
- **Actions are idempotent and backup-noted.** A re-apply with the same config changes
  nothing (copies skip-if-identical, `core.hooksPath` checks current value, MCP merges are
  keyed). Anything replaced is backed up per `on_conflict` (skip|overwrite|backup) and the
  restore path recorded in the result. Fail-closed on validation; fail-explicit on IO.
- **Agent-hook `cmd` is always written absolute.** The `agents-hooks/v1` runner rejects
  relative paths; the install action rewrites the `/ABSOLUTE/PATH/TO/...` placeholder to
  the real script path in the agent-tools checkout.
- **Never mutate a LIVE running service in a way that disrupts an active session.** rig prepares
  on-disk artifacts; the user reloads their config. The `tmux` block writes `rig.tmux.conf` + the
  managed scripts + the boot script + a boot launchd plist and wires `~/.tmux.conf`, but NEVER
  runs `tmux source-file` against the user's live server (that would re-apply config under their
  feet). **The tmux LIVE ACTIVATION is the deliberate exception** (a clean machine must end up
  FULLY working with zero manual steps, CTO 2026-06-16): on `rig apply` rig also clones the
  plugins, creates `~/.tmux/resurrect`, `launchctl load -w`s the BOOT agent, takes a first
  `resurrect save`, and cleans continuum's stale boot. These are SAFE for an active session — the
  boot agent's script is idempotent (`has-session` → exit 0, never spawns a duplicate or touches
  existing panes), and a first `resurrect save` is read-only w.r.t. the live session. It mirrors
  the `models` schedule exception (a non-interactive launchd agent is safe to (re)load). Gate the
  whole activation behind `RIG_TMUX_DRY_RUN` (the unit suite + CI set it). Migration backs up the
  original (`~/.tmux.conf.rig-bak-<UTC>`, timestamped) and never overwrites an existing backup.

## The integration seam (agent-tools)

`riglib/catalog.py` is the only module that knows the agent-tools on-disk layout. It scans
a checkout (`agent_tools_source` → `$RIG_AGENT_TOOLS_SOURCE` → default candidates) into a
flat `Item` registry. If agent-tools changes its layout, fix it *here* — nothing else
should hard-code agent-tools paths.

## Tests

- `python -m pytest -q` — the unit suite. Fast, hermetic; uses a fake agent-tools checkout
  (`tests/conftest.py::fake_agent_tools`) and `tmp_path` — tests never touch the real HOME
  or a real agent-tools checkout. The autouse guards `RIG_TMUX_DRY_RUN=1` /
  `_isolate_scheduler` keep the tmux live-activation + the scheduler out of the suite.
- `RIG_TMUX_E2E=1 python -m pytest -q tests/test_tmux_e2e.py` — the **opt-in** real-tmux e2e
  (the acceptance gate for the tmux reboot cycle: it drives a REAL tmux server on a private
  `-L` socket and clones the real plugins, so it needs tmux + git + network). It is OFF in the
  default `pytest` run to keep that hermetic; the tmux BFS / artifact logic it proves is ALSO
  covered hermetically by the unit suite (`test_pane_has_claude_*` etc.). Auto-skips offline.
- `bash tests/smoke.sh` — end-to-end: `--help`, `doctor`, a headless `init` against a
  sample config in a throwaway repo with an isolated `HOME`, idempotency, status, pytest.
  Needs a real agent-tools checkout (`RIG_AGENT_TOOLS_SOURCE`); self-skips the apply leg
  without one. The init leg sets `RIG_TMUX_DRY_RUN=1` so the tmux artifacts land without the
  live activation.
- Add a test with every behavior change. TDD red-first is the house style.

## Style

- Conventional commits.
- English-only code, comments, and docs.
- No dead code, no underscore-prefixed unused params, no `as-unknown-as` escape hatches.
- Keep `cli.py` thin (argparse + dispatch); behavior lives in the sibling modules.
