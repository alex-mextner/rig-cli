"""Clean-room / Docker e2e — `rig init` as a BRAND-NEW user on a pristine machine.

What this is
------------
The unit suite (``tests/conftest.py::fake_agent_tools`` + ``tmp_path``) and ``tests/smoke.sh``
both run on the DEVELOPER'S machine: the dev's Python, the dev's ``$PATH``, and — even with a
throwaway ``$HOME`` — a host that already has rig installed, ``git`` configured, and whatever
``~/.claude`` / ``~/.agents`` history the dev accumulated. They prove the logic; they do NOT
prove that a stranger who has NEVER run rig, on a machine that has NEVER seen agent-tools, gets a
working setup from one ``rig init --apply``. That clean-room proof is what the ROADMAP asks for
(tg#3745, rig-cli#10): a fresh-environment e2e (Docker container / throwaway ``$HOME``) running
``rig init --apply`` as a brand-new user, asserting the four acceptance points below. (``rig
init`` SCAFFOLDS rig.yaml + previews the plan; ``--apply`` is the explicit one-shot that also
applies — see riglib/cli.py::cmd_setup.)

How it is reached
-----------------
This builds a ``python:3.x-slim`` image with NOTHING but git + python, copies in the rig-cli
source tree and a SELF-CONTAINED fake agent-tools checkout (generated here, mirroring
``conftest.fake_agent_tools`` — so the test needs no real checkout and runs anywhere, including
CI), creates a brand-new non-root user with a pristine ``$HOME``, and runs the REAL ``rig init``
end to end inside the container. Every assertion runs against the ACTUAL installed CLI (the
``riglib`` package, not a mock) via an in-container assertion script.

What it proves (the ROADMAP's four acceptance points, 1:1)
----------------------------------------------------------
1. SKILLS DISCOVERABLE BY THE HARNESS — each enabled skill is symlinked into ``~/.claude/skills``
   (claude-code's Skill-tool discovery dir) and the symlink RESOLVES to a real ``SKILL.md``. A
   skill that lands only in ``~/.agents/skills`` is invisible to the harness; this asserts the
   harness-link, on a HOME that started empty.
2. HOOKS / DISPATCHER / CI / AUTO-MODE INSTALLED — the agent-hook descriptors land under
   ``~/.claude/hooks``; the global git-hooks dispatcher runner is installed and executable; the
   CI workflows are written into the repo; auto-mode is provisioned (``permissions.defaultMode``
   in the harness settings file) and the agents-hooks→CC bridge is wired (``PreToolUse`` hook
   referencing ``cc_hook_bridge`` — the piece that makes the guards actually FIRE in CC).
3. IDEMPOTENT RE-APPLY — a second ``rig apply`` reports ZERO ``created``/``updated``/``backed_up``
   in its summary (a re-run on an already-converged machine is a no-op).
4. ``rig status`` CLEAN — exits 0 (no two-way drift) after apply.

Invariants
----------
- Opt-in (``RIG_CLEANROOM_E2E=1``) + auto-skip when Docker is unavailable — mirrors the real-tmux
  e2e (``test_tmux_e2e.py``). The default ``pytest -q`` stays hermetic + offline; the
  install/link/drift logic this proves is ALSO covered hermetically by the unit suite.
- The one-time image BUILD needs apt/PyPI egress (install git, ``pip install`` the package); the
  in-container RUN is fully offline — ``docker run --network none`` — and never reaches the network.
  The daemon/network categories (models cron, github ruleset, tg-ctl, tmux live activation) are
  turned OFF in the clean-room config, so ``rig status`` is deterministically clean inside a
  container with no cron / launchd / gh-remote and no egress.
- The brand-new user runs as a non-root UID with a pristine ``$HOME`` created fresh in the
  image — proving the first-run experience for someone who has never touched rig.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# This builds + runs a Docker container — real container runtime. The repo's plain
# `python -m pytest -q` is documented as fast + HERMETIC (AGENTS.md), so this clean-room e2e is
# OPT-IN via `RIG_CLEANROOM_E2E=1`: the default run stays hermetic; the install/link/drift logic
# it exercises is ALSO covered hermetically by the unit suite. Even opted in, it SKIPS (never
# fails) when no Docker is available. The acceptance gate is
# `RIG_CLEANROOM_E2E=1 pytest tests/test_cleanroom_e2e.py`.
_E2E_OPTED_IN = os.environ.get("RIG_CLEANROOM_E2E", "").strip().lower() in ("1", "true", "yes")


def _docker_available() -> bool:
    """True if a usable Docker CLI + daemon is reachable. Cheap `docker info` probe.

    The clean-room proof needs a real container; without one we skip (the unit suite covers the
    logic hermetically). ``docker info`` exits non-zero when the CLI is missing OR the daemon is
    down, so a single probe covers both. Honors ``DOCKER`` (an alternate OCI CLI, e.g. podman)."""
    docker = os.environ.get("DOCKER", "docker")
    if shutil.which(docker) is None:
        return False
    try:
        r = subprocess.run(
            [docker, "info"], capture_output=True, text=True, timeout=30
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# The opt-in gate is evaluated at COLLECTION time (cheap env-var read). The Docker-daemon probe is
# deliberately NOT in this mark — it would run a 30s-timeout `docker info` at IMPORT time, which
# the helper-test module triggers by importing this module. The daemon probe lives in the test
# BODY (a `pytest.skip` when absent), so importing this module never shells out to Docker.
pytestmark = pytest.mark.skipif(
    not _E2E_OPTED_IN,
    reason="clean-room Docker e2e is opt-in: set RIG_CLEANROOM_E2E=1",
)


def _write(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def _build_fake_agent_tools(root: Path) -> None:
    """A minimal but structurally-valid agent-tools checkout — the clean-room carrier.

    Mirrors ``conftest.fake_agent_tools`` so the container needs NO real agent-tools checkout
    (the test stays self-contained + runnable in CI). Carries exactly what the clean-room
    config below provisions: universal + cli skills, one agent-hook, the CI slots, the global
    dispatcher, and ``lib/cc_hook_bridge/dispatch.py`` (so the hook-bridge is wired, not skipped).
    """
    # universal skills
    for name in ("shell-timeouts", "naming", "push-regularly"):
        _write(
            root / "skills" / "universal" / name / "SKILL.md",
            f"---\nname: {name}\ndescription: what {name} gives you\n---\n# {name}\nbody\n",
        )
    # by-type (cli) skills
    for skill in ("lazy-imports", "self-registering-commands"):
        _write(
            root / "skills" / "by-type" / "cli" / skill / "SKILL.md",
            f"---\nname: {skill}\ndescription: cli skill {skill}\n---\n# {skill}\n",
        )

    # an agent hook (descriptor + script + readme). The descriptor's `cmd` points at the REAL
    # in-container script path (not a placeholder) so the installed descriptor references a file
    # that actually exists — an honest fixture, not a dead path.
    _write(
        root / "agent-hooks" / "block-no-verify" / "block-no-verify.pre-bash.json",
        json.dumps(
            {
                "id": "block-no-verify",
                "point": "pre-bash",
                "cmd": "/opt/agent-tools/agent-hooks/block-no-verify/block_no_verify.py",
                "on_error": "closed",
            }
        ),
    )
    # the hook script the descriptor's `cmd` points at — EXECUTABLE, so the installed hook can
    # actually run (an installed descriptor whose cmd is a non-exec file is a dead guard).
    _write(
        root / "agent-hooks" / "block-no-verify" / "block_no_verify.py",
        "#!/usr/bin/env python3\n",
        executable=True,
    )
    _write(root / "agent-hooks" / "block-no-verify" / "README.md", "# block-no-verify\nblocks bypass\n")

    # CI slots: the script-backed ones the explicit clean-room config enables (secret-scan,
    # leftover-grep, review-threads) PLUS the slots the DEFAULT scaffold (riglib/state.py)
    # references (codeql + variant, dependency-review, pr-checklist, ship) — so the no-config
    # `rig init --yes --apply` leg below is satisfiable. Mirrors conftest.fake_agent_tools.
    _write(root / "ci" / "secret-scan" / "secret-scan.yml", "name: secret-scan\n")
    _write(root / "ci" / "secret-scan" / "gitleaks.toml", "# not a workflow\n")
    _write(root / "ci" / "secret-scan" / "README.md", "# secret-scan\ngitleaks\n")
    _write(root / "ci" / "codeql" / "workflow.yml", "name: codeql\n")
    _write(root / "ci" / "codeql" / "workflow-selfgate.yml", "name: codeql-selfgate\n")
    _write(root / "ci" / "codeql" / "README.md", "# codeql\nsemantic SAST\n")
    for slot in ("dependency-review", "leftover-grep", "review-threads"):
        _write(root / "ci" / slot / "workflow.yml", f"name: {slot}\nrun: bash ci/{slot}/{slot}.sh\n")
        _write(root / "ci" / slot / f"{slot}.sh", f"#!/usr/bin/env bash\necho {slot}\n", executable=True)
        _write(root / "ci" / slot / "README.md", f"# {slot}\n")
    # pr-checklist: a real, provisionable merge gate the DEFAULT scaffold enables. Unlike the other
    # slots its companions install to FIXED paths (checklist-gate.mjs -> .github/scripts/,
    # pull_request_template.md -> .github/) rather than ci/<slot>/, so the fixture ships BOTH
    # companions to exercise that fixed-path vendoring (runner._CI_COMPANION_FIXED_PATHS) end to
    # end. Its check-run CONTEXT (job `name:` "PR Checklist") differs from the slot id — mirror the
    # real workflow's job structure so the fixture is a faithful proxy for the ruleset context map.
    _write(
        root / "ci" / "pr-checklist" / "workflow.yml",
        "name: PR Checklist\non: pull_request_target\njobs:\n  checklist:\n    name: PR Checklist\n    runs-on: ubuntu-latest\n",
    )
    _write(root / "ci" / "pr-checklist" / "checklist-gate.mjs", "// checklist gate\n")
    _write(root / "ci" / "pr-checklist" / "pull_request_template.md", "## Checklist\n- [ ] done\n")
    _write(root / "ci" / "pr-checklist" / "README.md", "# pr-checklist\nverify checkboxes\n")
    _write(root / "ci" / "ship" / "ship.sh", "#!/usr/bin/env bash\necho ship\n", executable=True)
    _write(root / "ci" / "ship" / "README.md", "# ship\nmerge gate\n")

    # the global dispatcher: runner + composers + fragments. The shell scripts are EXECUTABLE,
    # mirroring real agent-tools — so the fixture doesn't lean on rig chmod'ing them on install
    # (an unspoken coupling); the in-container `[ -x runner ]` assertion proves the installed bit.
    disp = root / "git-hooks" / "global-dispatcher"
    _write(disp / "run-global-hooks", "#!/usr/bin/env bash\necho run\n", executable=True)
    _write(disp / "install-local-hooks.sh", "#!/usr/bin/env bash\necho retrofit\n", executable=True)
    for composer in ("pre-commit", "commit-msg", "pre-push", "review-gate"):
        _write(disp / "hooks" / composer, "#!/usr/bin/env bash\nexec ../run-global-hooks\n", executable=True)
    _write(disp / "global-hooks.d" / "pre-commit" / "10-secret-scan", "#!/usr/bin/env bash\n", executable=True)
    _write(disp / "global-hooks.d" / "commit-msg" / "10-conventional-commit", "#!/usr/bin/env bash\n", executable=True)
    _write(disp / "global-hooks.d" / "pre-push" / "10-protect-main", "#!/usr/bin/env bash\n", executable=True)
    _write(disp / "README.md", "# dispatcher\nglobal hooks\n")

    # the cc_hook_bridge dispatcher the hook-bridge wiring points at (its presence is checked by
    # plan._build_hook_bridge before it registers the settings.json hooks — without it the bridge
    # is SKIPPED and the guards stay inert, so the clean-room MUST ship it to prove the wire-up).
    _write(root / "lib" / "cc_hook_bridge" / "dispatch.py", "#!/usr/bin/env python3\n")
    # the model-freshness checker the DEFAULT scaffold's `models:` schedule runs (its presence is
    # checked by plan._build_models before it provisions the cron) — needed by the no-config leg.
    _write(root / "lib" / "checker" / "model_freshness.py", "#!/usr/bin/env python3\n")


# The clean-room rig.yaml. Carries the four ROADMAP acceptance subjects ON (skills, agent_hooks,
# git_hooks dispatcher, ci, harness auto-mode + hook-bridge) and turns OFF the daemon/network
# categories (models cron, github ruleset, tg-ctl, tmux live activation, global git-excludes) so
# `rig status` is DETERMINISTICALLY clean inside a slim container with no cron/launchd/gh-remote.
# `__SRC__` is replaced with the in-container agent-tools path. Auto-mode + hook-bridge are pinned
# to the SAME user-scope settings file so both land in one place the assertions can read.
_CLEANROOM_CONFIG = """\
version: 1
agent_tools_source: __SRC__
skills:
  enabled: true
  universal: { all: true }
  by_type:   { enable: [cli] }
agent_hooks: { all: true }
git_hooks:
  dispatcher: { enabled: true }
ci:
  items:
    secret-scan:    { enabled: true, tier: block }
    leftover-grep:  { enabled: true, tier: block }
    review-threads: { enabled: true, tier: block }
harness:
  enabled: true
  kind: claude-code
  auto_mode: true
  settings_path: ~/.claude/settings.json
models:    { enabled: false }
github:    { ruleset: { enabled: false } }
tg_ctl:    { enabled: false }
tmux:      { enabled: false }
gitignore: { enabled: false }
"""


# The in-container assertion script. Runs `rig init` as the brand-new user, then proves the four
# ROADMAP acceptance points against the REAL CLI. Pure POSIX sh + the installed `rig`; any failed
# assertion exits non-zero with a labeled message so the pytest side surfaces exactly what broke.
_ASSERT_SCRIPT = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    set -euo pipefail

    SRC=/opt/agent-tools
    REPO="$HOME/project"
    SETTINGS="$HOME/.claude/settings.json"

    fail() { printf 'CLEANROOM-FAIL: %s\\n' "$1" >&2; exit 1; }
    pass() { printf 'CLEANROOM-OK:   %s\\n' "$1"; }

    # ── pristine-machine sanity: a brand-new user must start with NOTHING rig-related ──
    [ ! -e "$HOME/.claude/skills" ] || fail "pre-existing ~/.claude/skills on a brand-new user"
    [ ! -e "$HOME/.agents/skills" ] || fail "pre-existing ~/.agents/skills on a brand-new user"
    pass "brand-new user starts with a pristine HOME (no rig artifacts)"

    # a brand-new user has NO git identity — set a throwaway one so any apply-time git call works
    # (the clean-room config keeps the global-excludes off, but a missing identity must never make
    # the run flaky). This mirrors what a real first-time user does once.
    git config --global user.email cleanroom@example.com
    git config --global user.name  cleanroom

    mkdir -p "$REPO"
    git -C "$REPO" init -q
    sed "s#__SRC__#$SRC#" /opt/cleanroom/rig.yaml > "$REPO/rig.yaml"

    # ── the brand-new user runs `rig init` (the front door) ──────────────────────────
    # `--apply` is the explicit one-shot: init SCAFFOLDS rig.yaml + previews by default, and
    # applies the plan only on --apply (or a separate `rig apply`). The clean-room proves a
    # brand-new user gets a working setup from one `rig init --apply`.
    rig init -C "$REPO" --config "$REPO/rig.yaml" --yes --apply >/opt/cleanroom/init.log 2>&1 \\
      || { cat /opt/cleanroom/init.log; fail "rig init --yes --apply exited non-zero"; }
    pass "rig init --yes --apply succeeded as a brand-new user"

    # the `gitignore` opt-out must actually suppress the global-excludes write: with it OFF, rig
    # must NOT set `core.excludesfile` globally (a silent write there would be a real bug class).
    # The dispatcher's `core.hooksPath` IS expected (acceptance point 2 below) — so this asserts the
    # specific opt-out key, not "no global writes at all".
    if git config --global --get core.excludesfile >/dev/null 2>&1; then
      git config --global --get core.excludesfile >&2
      fail "gitignore is off but rig set global core.excludesfile (the opt-out did not suppress it)"
    fi
    pass "gitignore opt-out honored: rig set no global core.excludesfile"

    # ── (1) SKILLS DISCOVERABLE BY THE HARNESS ───────────────────────────────────────
    [ -d "$HOME/.agents/skills" ] || fail "skills not installed into ~/.agents/skills"
    [ -d "$HOME/.claude/skills" ] || fail "harness skill dir ~/.claude/skills not created"
    linked=0
    for d in "$HOME/.agents/skills"/*/; do
      [ -d "$d" ] || continue
      name="$(basename "$d")"
      link="$HOME/.claude/skills/$name"
      [ -L "$link" ] || fail "skill '$name' not symlinked into the harness dir"
      [ -f "$link/SKILL.md" ] || fail "harness skill link '$name' does not resolve to a SKILL.md"
      linked=$((linked + 1))
    done
    # the fixture ships EXACTLY 5 skills (3 universal + 2 cli) and the config enables every one of
    # them, so every skill must be harness-linked — an EXACT count catches a partial link (e.g. the
    # cli skills silently missed), which a `>= N` floor would let slip through.
    [ "$linked" -eq 5 ] || fail "expected exactly 5 harness-linked skills, got $linked"
    # both classes must be present by name (not just a count): a universal one and a by-type/cli one.
    [ -f "$HOME/.claude/skills/naming/SKILL.md" ] \\
      || fail "universal skill 'naming' not harness-linked"
    [ -f "$HOME/.claude/skills/lazy-imports/SKILL.md" ] \\
      || fail "by-type(cli) skill 'lazy-imports' not harness-linked"
    pass "skills discoverable by the harness ($linked symlinks resolve in ~/.claude/skills)"

    # ── (2) HOOKS / DISPATCHER / CI / AUTO-MODE INSTALLED ─────────────────────────────
    # the SPECIFIC descriptor the fixture ships must land (not just "some .json exists" — a stray
    # package.json would satisfy that). The fixture has exactly one hook: block-no-verify.
    # `-print -quit` returns at most ONE path (no multi-line $desc that would feed grep many
    # files); `|| true` keeps a non-zero find (e.g. ~/.claude/hooks absent) from aborting the
    # script via `set -e` before the labeled failure below can fire.
    desc="$(find "$HOME/.claude/hooks" -name 'block-no-verify*.json' -print -quit 2>/dev/null || true)"
    [ -n "$desc" ] || fail "the block-no-verify agent-hook descriptor was not installed under ~/.claude/hooks"
    # read id + cmd by PARSING the json (not a textual grep, which couples to json.dumps spacing).
    # `|| fail` labels a malformed/short descriptor instead of letting `set -e` abort on a raw
    # KeyError/JSON traceback before the labeled failure can fire.
    desc_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$desc" 2>/dev/null || true)"
    [ "$desc_id" = "block-no-verify" ] || fail "installed descriptor $desc is not the block-no-verify hook (id='$desc_id')"
    # the descriptor's `cmd` must point at an EXECUTABLE file — an installed hook whose command
    # cannot run is a dead guard (the whole point of installing it is that it FIRES).
    cmd_path="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["cmd"])' "$desc" 2>/dev/null || true)"
    [ -n "$cmd_path" ] || fail "descriptor $desc has no 'cmd' field"
    [ -x "$cmd_path" ] || fail "block-no-verify cmd '$cmd_path' is not an executable file"
    pass "agent-hook descriptor (block-no-verify) installed + cmd is executable"

    runner="$HOME/.config/git/run-global-hooks"
    [ -x "$runner" ] || fail "global git-hooks dispatcher runner not installed/executable"
    # the runner existing is not enough — git must actually be pointed at the composer dir (the
    # runner's sibling hooks/ dir) via core.hooksPath, else the dispatcher is installed but inert.
    hp="$(git config --global core.hooksPath || true)"
    # normalize a possible trailing slash before comparing (tolerate `.../hooks/` vs `.../hooks`).
    [ "${hp%/}" = "$HOME/.config/git/hooks" ] \\
      || fail "core.hooksPath not wired to the dispatcher composer dir (got '$hp')"
    pass "git-hooks dispatcher installed + wired (core.hooksPath -> $hp)"

    [ -f "$REPO/.github/workflows/secret-scan.yml" ] || fail "secret-scan CI workflow not written"
    [ -f "$REPO/.github/workflows/leftover-grep.yml" ] || fail "leftover-grep CI workflow not written"
    pass "CI workflows written into the repo"

    [ -f "$SETTINGS" ] || fail "harness settings file ($SETTINGS) not written (auto-mode)"
    python3 /opt/cleanroom/check_settings.py auto_mode "$SETTINGS" \\
      || fail "auto-mode permissions.defaultMode not 'auto' in settings.json"
    pass "auto-mode provisioned (permissions.defaultMode='auto' in $SETTINGS)"

    # the agents-hooks -> Claude Code bridge: a PreToolUse hook referencing cc_hook_bridge must be
    # wired into the SAME settings file (the piece that makes the guards FIRE in CC).
    python3 /opt/cleanroom/check_settings.py hook_bridge "$SETTINGS" \\
      || fail "cc_hook_bridge PreToolUse hook not wired into settings.json"
    pass "agents-hooks -> CC bridge wired (cc_hook_bridge in PreToolUse)"

    # ── (3) IDEMPOTENT RE-APPLY ──────────────────────────────────────────────────────
    # `rig apply` is non-interactive by design (no `--yes`/wizard — only `rig init` prompts), so it
    # never blocks on the container's missing TTY. Capture WITHOUT `|| true` so the exit code is
    # real (recorded via $?), but guard the assignment so a non-zero apply does not trip `set -e`
    # and abort before the diagnostics below.
    out2="$(rig apply -C "$REPO" --config "$REPO/rig.yaml" 2>&1)" && apply_rc=0 || apply_rc=$?
    if [ "$apply_rc" -ne 0 ]; then
      printf '%s\\n' "$out2" >&2
      fail "second apply exited non-zero (rc=$apply_rc)"
    fi
    summary="$(printf '%s\\n' "$out2" | grep '^Summary:' || true)"
    # a MISSING Summary line must NOT pass vacuously (an output-format change would otherwise let
    # the idempotency check match nothing and slip through).
    if [ -z "$summary" ]; then
      printf '%s\\n' "$out2" >&2
      fail "second apply printed no 'Summary:' line — cannot verify idempotency"
    fi
    # reject any non-zero change OR error counter: a clean re-apply is all skipped/planned.
    if printf '%s\\n' "$summary" | grep -Eq '(created|updated|backed_up|error|failed)=[1-9]'; then
      printf '%s\\n' "$out2" >&2
      fail "second apply was not idempotent: $summary"
    fi
    pass "rig apply is idempotent (second apply: $summary)"

    # ── (4) rig status CLEAN ─────────────────────────────────────────────────────────
    rig status -C "$REPO" --config "$REPO/rig.yaml" >/opt/cleanroom/status.log 2>&1 \\
      || { cat /opt/cleanroom/status.log; fail "rig status exited non-zero (drift) after apply"; }
    pass "rig status: clean (exit 0) after apply"

    # ── (5) the TRUE no-config onboarding path: `rig init --yes --apply` from the default ──
    # The acceptance run above drives an explicit --config. A real brand-new user runs
    # `rig init` with NO config and gets the DEFAULT scaffold; `--yes --apply` is the explicit
    # one-shot that scaffolds AND applies it (init SCAFFOLDS + previews by default — `--apply`
    # applies). Prove that front door runs end to end and lands the core artifacts. It runs in its
    # OWN repo + HOME so it can't pollute the acceptance run; the agent-tools source comes from the
    # documented RIG_AGENT_TOOLS_SOURCE env fallback. The default scaffold turns ON daemon/network
    # categories (models cron, tmux, github ruleset) that can't fully converge in an offline slim
    # container, so this leg asserts the scaffold RUNS + writes rig.yaml + lands skills/dispatcher —
    # it does NOT assert clean status (that is the config-driven leg's job). Dry-run flags keep it
    # off the (absent) host daemons.
    SCAFFOLD_HOME="$HOME/scaffold-home"
    SCAFFOLD_REPO="$HOME/scaffold-repo"
    mkdir -p "$SCAFFOLD_HOME" "$SCAFFOLD_REPO"
    git -C "$SCAFFOLD_REPO" init -q
    HOME="$SCAFFOLD_HOME" RIG_AGENT_TOOLS_SOURCE="$SRC" RIG_SCHEDULE_DRY_RUN=1 RIG_TMUX_DRY_RUN=1 \\
      rig init -C "$SCAFFOLD_REPO" --yes --apply >/opt/cleanroom/scaffold.log 2>&1 \\
      || { cat /opt/cleanroom/scaffold.log; fail "'rig init --yes --apply' (default scaffold) exited non-zero"; }
    [ -f "$SCAFFOLD_REPO/rig.yaml" ] || fail "'rig init --yes --apply' did not scaffold a rig.yaml"
    [ -d "$SCAFFOLD_HOME/.agents/skills" ] || fail "default scaffold apply did not install skills"
    [ -d "$SCAFFOLD_HOME/.claude/skills" ] || fail "default scaffold apply did not harness-link skills"
    [ -x "$SCAFFOLD_HOME/.config/git/run-global-hooks" ] \\
      || fail "default scaffold apply did not install the git-hooks dispatcher runner"
    pass "'rig init --yes --apply' scaffolds the default config + lands skills/dispatcher"

    # ── (6) the CORE safety invariant: `rig init --yes` WITHOUT --apply must NOT apply ──
    # This is the exact regression the redesign exists to prevent (the CTO complaint: init must
    # not "do a bunch of things" with no apply signal). `--yes` writes rig.yaml but applies
    # NOTHING — prove it end-to-end on a pristine HOME so a future accidental presumptuous-apply
    # lights this up. Own repo + HOME so it can't pollute the legs above.
    NOAPPLY_HOME="$HOME/noapply-home"
    NOAPPLY_REPO="$HOME/noapply-repo"
    mkdir -p "$NOAPPLY_HOME" "$NOAPPLY_REPO"
    git -C "$NOAPPLY_REPO" init -q
    HOME="$NOAPPLY_HOME" RIG_AGENT_TOOLS_SOURCE="$SRC" RIG_SCHEDULE_DRY_RUN=1 RIG_TMUX_DRY_RUN=1 \\
      rig init -C "$NOAPPLY_REPO" --yes >/opt/cleanroom/noapply.log 2>&1 \\
      || { cat /opt/cleanroom/noapply.log; fail "'rig init --yes' (scaffold only) exited non-zero"; }
    [ -f "$NOAPPLY_REPO/rig.yaml" ] || fail "'rig init --yes' did not scaffold a rig.yaml"
    [ ! -d "$NOAPPLY_HOME/.agents/skills" ] \\
      || fail "'rig init --yes' (no --apply) APPLIED skills — presumptuous-apply regression!"
    [ ! -d "$NOAPPLY_HOME/.claude/skills" ] \\
      || fail "'rig init --yes' (no --apply) harness-linked skills — presumptuous-apply regression!"
    # `-e` (existence), NOT `-x`: a presumptuous apply that wrote a NON-executable dispatcher must
    # still trip this — the invariant is "nothing was created", not "nothing executable".
    [ ! -e "$NOAPPLY_HOME/.config/git/run-global-hooks" ] \\
      || fail "'rig init --yes' (no --apply) installed the dispatcher — presumptuous-apply regression!"
    # case-insensitive + loose so a cosmetic reword of the summary ('Nothing applied') doesn't redden it
    grep -qi "nothing.*applied" /opt/cleanroom/noapply.log \\
      || fail "'rig init --yes' output did not state nothing was applied"
    pass "'rig init --yes' (no --apply) scaffolds rig.yaml but applies NOTHING (core invariant)"

    printf 'CLEANROOM-ALL-GREEN\\n'
    """
)


# A tiny in-container settings.json checker (staged as a file, not an inline heredoc, so its
# Python body never inherits the bash script's indentation). `auto_mode` asserts
# `permissions.defaultMode == 'auto'`; `hook_bridge` asserts a `cc_hook_bridge` command is wired
# into a PreToolUse hook. Exits non-zero with a message on any miss.
_CHECK_SETTINGS = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import sys


    def _section(data, key):
        # tolerate a malformed settings file: a non-dict section is treated as absent, so the
        # checks below return a clean labeled miss instead of an AttributeError traceback.
        sect = data.get(key, {})
        return sect if isinstance(sect, dict) else {}


    def main() -> int:
        which, path = sys.argv[1], sys.argv[2]
        try:
            data = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"cannot read settings JSON {path}: {exc}", file=sys.stderr)
            return 3
        if not isinstance(data, dict):
            print(f"settings JSON {path} is not an object", file=sys.stderr)
            return 3
        if which == "auto_mode":
            mode = _section(data, "permissions").get("defaultMode")
            if mode != "auto":
                print(f"defaultMode={mode!r} (want 'auto')", file=sys.stderr)
                return 1
            return 0
        if which == "hook_bridge":
            # walk the real CC settings shape: PreToolUse is a list of matcher groups, each with a
            # `hooks` list of {type:command, command:...}. Assert at least one entry's COMMAND
            # references the bridge — not a bare substring anywhere (a path/comment would false-pass).
            pre = _section(data, "hooks").get("PreToolUse", [])
            pre = pre if isinstance(pre, list) else []
            found = any(
                "cc_hook_bridge" in str(hook.get("command", ""))
                for group in pre
                if isinstance(group, dict)
                for hook in group.get("hooks", [])
                if isinstance(hook, dict)
            )
            if not found:
                print("no cc_hook_bridge command wired into a PreToolUse hook", file=sys.stderr)
                return 1
            return 0
        print(f"unknown check {which!r}", file=sys.stderr)
        return 2


    if __name__ == "__main__":
        raise SystemExit(main())
    """
)


# The Dockerfile. `python:3.12-slim` + git, a brand-new non-root user with a pristine HOME, the
# rig-cli source installed as the `rig` CLI (from the COPY'd local path, plus its one runtime dep
# pyyaml from PyPI), the fake agent-tools checkout, the clean-room config, and the assertion
# script. The BUILD needs apt/PyPI egress; the RUN is offline (`docker run --network none`).
_DOCKERFILE = textwrap.dedent(
    """\
    FROM python:3.12-slim

    RUN apt-get update \\
        && apt-get install -y --no-install-recommends git ca-certificates \\
        && rm -rf /var/lib/apt/lists/*

    # a brand-new, non-root user with a pristine HOME — the "stranger who has never run rig"
    RUN useradd --create-home --shell /bin/bash newuser

    # install rig from the copied source (the real package, exposed as the `rig` console script)
    COPY rig-cli /opt/rig-cli
    RUN pip install --no-cache-dir /opt/rig-cli

    # the clean-room carrier + config + assertion script (world-readable; agent-tools stays
    # root-owned). /opt/cleanroom is chown'd -R to newuser so assert.sh can write its logs
    # (init.log, status.log, scaffold.log) there at run time, now and if it grows more.
    COPY agent-tools /opt/agent-tools
    COPY cleanroom /opt/cleanroom
    RUN chmod -R a+rX /opt/agent-tools /opt/cleanroom \\
        && chmod +x /opt/cleanroom/assert.sh \\
        && chown -R newuser /opt/cleanroom

    USER newuser
    WORKDIR /home/newuser
    ENV HOME=/home/newuser
    ENTRYPOINT ["/opt/cleanroom/assert.sh"]
    """
)


def _docker() -> str:
    return os.environ.get("DOCKER", "docker")


def _stage_build_context(ctx: Path) -> None:
    """Lay out the Docker build context: rig-cli source, fake agent-tools, config, assert, Dockerfile."""
    # rig-cli source — copy ONLY the explicit inputs `pip install` needs, via an ALLOWLIST (not a
    # denylist). A denylist over the whole worktree can leak an untracked local file (a stray
    # secret, a build artifact) into the Docker build context + cache; an allowlist sends exactly
    # the package inputs and nothing else. `__pycache__`/`*.pyc` are stripped from copied dirs so
    # the copy is deterministic and small.
    rig_dst = ctx / "rig-cli"
    rig_dst.mkdir(parents=True, exist_ok=True)
    _strip_caches = shutil.ignore_patterns("__pycache__", "*.pyc")
    # `pyproject.toml` + `riglib/` are REQUIRED for `pip install`; the rest are nice-to-have. A
    # missing required input is a hard error here (a clear message), not a silent skip that only
    # surfaces as an obscure `docker build` failure later.
    required = {"pyproject.toml", "riglib"}
    optional = ("README.md", "LICENSE", "uv.lock", "bin")
    for rel in sorted(required) + list(optional):
        src = REPO_ROOT / rel
        if not src.exists():
            if rel in required:
                raise FileNotFoundError(
                    f"clean-room build context is missing a REQUIRED rig-cli input: {src} "
                    "(a refactor moved/renamed it — update _stage_build_context's allowlist)"
                )
            continue
        if src.is_dir():
            shutil.copytree(src, rig_dst / rel, ignore=_strip_caches)
        else:
            shutil.copy2(src, rig_dst / rel)

    _build_fake_agent_tools(ctx / "agent-tools")

    cleanroom = ctx / "cleanroom"
    cleanroom.mkdir(parents=True, exist_ok=True)
    _write(cleanroom / "rig.yaml", _CLEANROOM_CONFIG)
    _write(cleanroom / "assert.sh", _ASSERT_SCRIPT, executable=True)
    _write(cleanroom / "check_settings.py", _CHECK_SETTINGS, executable=True)

    _write(ctx / "Dockerfile", _DOCKERFILE)


def _run(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    """Run a docker command, turning a hang into a LABELED failure (not a bare TimeoutExpired
    traceback) with whatever output was captured before the timeout fired."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        raise AssertionError(
            f"`{' '.join(cmd)}` did not finish within {timeout}s\nSTDOUT:\n{out}\nSTDERR:\n{err}"
        ) from exc


def test_cleanroom_rig_init_as_brand_new_user(tmp_path: Path) -> None:
    """Build a clean-room image and run `rig init` as a brand-new user; assert the 4 ROADMAP points.

    The whole proof lives in the in-container ``assert.sh`` — it runs the REAL ``rig`` CLI end to
    end on a pristine HOME and prints ``CLEANROOM-ALL-GREEN`` only when all four acceptance points
    hold. This test asserts the container exits 0 AND that sentinel is present (so a silent
    early-exit can't pass).
    """
    # daemon probe in the BODY (not the module mark) so importing this module never shells out to
    # Docker — skip (never fail) when no container runtime is reachable, even when opted in.
    if not _docker_available():
        pytest.skip("no usable Docker daemon (set DOCKER for an alternate OCI CLI)")
    docker = _docker()
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    _stage_build_context(ctx)

    tag = f"rig-cleanroom:{uuid.uuid4().hex[:12]}"
    try:
        build = _run([docker, "build", "-t", tag, str(ctx)], timeout=900)
        assert build.returncode == 0, f"docker build failed:\nSTDOUT:\n{build.stdout}\nSTDERR:\n{build.stderr}"

        # run with no network: the clean-room proof must hold OFFLINE (a brand-new user on a
        # plane). The config disables every network/daemon category, so the run needs nothing.
        run = _run(
            [docker, "run", "--rm", "--network", "none", tag],
            timeout=600,
        )
        combined = f"STDOUT:\n{run.stdout}\nSTDERR:\n{run.stderr}"
        assert run.returncode == 0, f"clean-room run failed (exit {run.returncode}):\n{combined}"
        assert "CLEANROOM-ALL-GREEN" in run.stdout, f"missing all-green sentinel:\n{combined}"
    finally:
        # never leak the throwaway image (best-effort; ignore failures, incl. a cleanup timeout —
        # a slow `image rm` must not mask the real assertion above).
        try:
            subprocess.run([docker, "image", "rm", "-f", tag], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass
