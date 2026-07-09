#!/usr/bin/env bash
# smoke.sh — fast end-to-end check of the rig CLI surface, run in CI and locally.
# Proves: --help, doctor, a headless setup against a sample config in a throwaway repo
# (isolated HOME so nothing on the machine is touched), idempotency, status drift, the
# error-system v2 contract (a removed slot prints the 3-part error + the right exit code;
# a clean sample exits 0; a non-git dir does NOT nag "should be committed"), and the
# pytest unit suite.
#
# WHY a real smoke and not just pytest: two same-day prod failures were unit-GREEN but
# smoke-BROKEN — a stale `mcp.items.review` (a slot removed in agent-tools #32) lingered in a
# config and only the REAL `rig status`/`init` flow against the REAL catalog caught it. pytest
# uses a fake catalog; smoke runs the real CLI against the real agent-tools checkout.
#
# --fast  — the PRE-COMMIT subset. Runs only the seconds-cheap legs that exercise the REAL
#   catalog (`--help`/`--version`/`doctor`/`setup`-usage and the `rig status` legs: a clean
#   sample exits 0, a removed slot prints the 3-part error + exit 4, a non-git dir doesn't nag).
#   It SKIPS the heavy `rig init --yes --apply` apply (skill installs / harness symlinks / tmux / tg-ctl
#   provisioning) and the full pytest run — those belong in CI, not a per-commit local gate.
#   This is the subset wired into the repo-local pre-commit hook (see scripts/install-smoke-
#   precommit.sh) so a commit that breaks the real `rig status` flow is blocked LOCALLY, not
#   just in CI — the gap the CTO flagged (2026-06-16): smoke ran in CI but never gated commits.
set -euo pipefail

FAST=0
for arg in "$@"; do
  case "$arg" in
    --fast) FAST=1 ;;
    -h|--help)
      # Print the leading comment block only (lines 2 onward, up to the first non-`#` line —
      # the shebang is line 1, `set -euo pipefail` ends the block), stripping the `# ` prefix.
      sed -n '2,${/^#/!q;s/^# \{0,1\}//p;}' "$0"
      exit 0
      ;;
    *) echo "smoke.sh: unknown argument '$arg' (use --fast or --help)" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RIG="python3 $ROOT/bin/rig"

# Provision tg-ctl in DRY-RUN for the WHOLE smoke: tg_ctl is default-on, so the apply legs below
# would otherwise (on macOS) shell out to the real `launchctl bootstrap` and write the real
# user's launchd domain. RIG_TG_CTL_DRY_RUN writes the plist into the (HOME-isolated) path but
# NEVER touches the live launchd domain — the hard isolation rule. (The dedicated tg-ctl leg
# below relies on this too.)
export RIG_TG_CTL_DRY_RUN=1

# Resolve an agent-tools checkout ONCE, up front, against the REAL HOME — the apply leg below
# overwrites $HOME with an isolated tmp dir, so any later HOME-relative discovery would miss it.
AGENT_TOOLS="${RIG_AGENT_TOOLS_SOURCE:-}"
for cand in "$AGENT_TOOLS" "$HOME/xp/agent-tools" "$HOME/work/agent-tools" "$HOME/agent-tools"; do
  if [[ -n "$cand" && -d "$cand/skills" && -d "$cand/agent-hooks" ]]; then AGENT_TOOLS="$cand"; break; fi
done

pass() { printf '  \033[32m✔\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗ %s\033[0m\n' "$1"; exit 1; }

# ── real-catalog full-coverage helpers ───────────────────────────────────────────
# These exercise the integration seam the synthetic fixtures CAN'T: rig must discover AND
# dry-run-plan EVERY item in the REAL agent-tools catalog with zero unknown-item/slot errors.
# The fake-catalog pytest suite only carries a curated handful of slots, so a NEW real slot
# (this exact class — "unknown ci item: pr-checklist" — has bitten before) or a renamed dir
# rig can't resolve would pass pytest and only break on a live machine. This catches that drift.

# Write an "everything-on" config so the planner is forced to resolve every catalog item.
# $1 dest path, $2 agent-tools source. by_type enables every kind found on disk (so every
# by-type skill is pulled in); the provisioning categories (github/tmux/tg_ctl/models) are off
# — they touch the network/daemon and aren't part of catalog DISCOVERY, which is all we assert.
_write_full_coverage_config() {
  local dest="$1" src="$2" kinds
  kinds="$(for d in "$src"/skills/by-type/*/; do [[ -d "$d" ]] && basename "$d"; done | paste -sd, -)"
  cat > "$dest" <<YAML
version: 1
agent_tools_source: $src
skills:
  universal: { all: true }
  by_type: { enable: [${kinds}] }
agent_hooks: { all: true }
ci: { all: true }
mcp: { enabled: true }
git_hooks:
  dispatcher: { enabled: true }
github: { ruleset: { enabled: false }, merge: { enabled: false }, ghas: { enabled: false }, actions: { enabled: false }, browser: { enabled: false } }
tmux: { enabled: false }
tg_ctl: { enabled: false }
models: { enabled: false }
gitignore: { enabled: false }
agents_md: { enabled: false }
ship_delegator: { enabled: false }
permissions: { enabled: false }
YAML
}

# Assert every ci/<slot>/ dir present on disk in the real catalog appears in the dry-run plan,
# so a slot rig's SCANNER silently drops (discovered-on-disk but absent-from-plan) is caught —
# not just a config naming a slot that doesn't exist. $1 plan output file, $2 agent-tools source.
# Fails VACUOUS coverage too: if ci/ is gone/empty (a renamed or deleted catalog dir), the loop
# would otherwise find nothing and pass hollow — exactly the drift this leg must catch loudly.
_assert_every_ci_slot_planned() {
  local plan_out="$1" src="$2" slot found=0
  for d in "$src"/ci/*/; do
    [[ -d "$d" ]] || continue
    slot="$(basename "$d")"
    found=$((found + 1))
    # FIXED-string match (`-F`), not a regex: a slot name with a regex metachar (`.`/`+`/`*`)
    # would otherwise match as a PATTERN and could false-pass. Anchor on the action-line BULLET
    # (`• ci/<slot> `): plan action lines are `  • ci/<slot> → <target>`, and a target PATH that
    # happens to contain `ci/<slot>` (e.g. `→ …/ci/x/…`) never carries the `• ci/` bullet prefix —
    # so a slot that dropped as a SOURCE but appears inside another line's target can't false-pass.
    grep -qF "• ci/$slot " "$plan_out" \
      || fail "real-catalog coverage: ci slot '$slot' on disk but NOT in the plan (scanner dropped it?)"
  done
  [[ "$found" -gt 0 ]] \
    || fail "real-catalog coverage: NO ci slots found under $src/ci (renamed/removed catalog dir? coverage would be vacuous)"
}

# Dry-run-plan the whole real catalog and assert: exit 0, no "unknown" item/slot error, nothing
# written, and every ci slot covered. $1 throwaway HOME, $2 repo dir, $3 agent-tools source.
_real_catalog_full_coverage() {
  local cov_home="$1" repo="$2" src="$3"
  local cfg="$repo/full-coverage.yaml" out rc ci_count
  # Make the throwaway HOME real (so any HOME-relative read rig does resolves under it, never the
  # dev's real home) and stamp a git identity on the throwaway repo (so a leg that touches git
  # can't fail with an unrelated "please tell me who you are"). dry-run writes nothing regardless.
  mkdir -p "$cov_home"
  git -C "$repo" config user.email cov@rig.test
  git -C "$repo" config user.name  rig-coverage
  _write_full_coverage_config "$cfg" "$src"
  set +e
  # --plan forces the FULL per-action listing: the default condenses a large plan to a per-carrier
  # summary, but the coverage scan below greps each `• ci/<slot>` action line, so it needs the
  # full list (this leg is a machine consumer of the plan, exactly what `--plan` is for).
  out="$(HOME="$cov_home" RIG_TMUX_DRY_RUN=1 $RIG apply -C "$repo" --config "$cfg" --dry-run --plan 2>&1)"
  rc=$?
  set -e
  [[ "$rc" -eq 0 ]] || { echo "$out" | tail -8; fail "real-catalog coverage: dry-run exit $rc != 0 (rig can't plan the real catalog)"; }
  # ERE (`-E`), NOT BRE `\|`: `\|` is a GNU-only extension — BSD grep on macOS (smoke runs there
  # too) treats it as a literal pipe, so a BRE alternation would silently never match and this
  # detector — the exact "unknown ci item" guard — would be dead on macOS. The rc!=0 check above
  # is the primary backstop (rig exits 4 on unknown items); this catches a hypothetical
  # warn-and-exit-0 regression, so it must actually fire on every platform.
  echo "$out" | grep -Eqi "unknown .*item|unknown .*slot|unknown ci" \
    && fail "real-catalog coverage: an 'unknown item/slot' error against the REAL catalog"
  echo "$out" | grep -q "dry-run: nothing written" || fail "real-catalog coverage: dry-run claims it wrote something"
  printf '%s\n' "$out" > "$repo/full-coverage.plan"
  _assert_every_ci_slot_planned "$repo/full-coverage.plan" "$src"
  ci_count="$(grep -c '• ci/' "$repo/full-coverage.plan" || true)"
  pass "rig dry-run-plans the FULL real catalog (zero unknown items; $ci_count ci slots covered)"
}

echo "rig smoke — $ROOT"

# ── 1. --help / --version ─────────────────────────────────────────────────────
$RIG --help   >/dev/null 2>&1 || fail "rig --help"
$RIG --version >/dev/null 2>&1 || fail "rig --version"
pass "rig --help / --version"

# ── 2. doctor (informational; never fails the smoke on missing optional deps) ──
$RIG doctor >/dev/null 2>&1 || true
pass "rig doctor"

# ── 2b. setup with no TTY → prints USAGE for init/apply/config (never a half-wizard) ──
# The smoke runs non-interactively (piped), so `rig setup` must degrade to the usage pointer.
setup_out="$($RIG setup 2>&1 < /dev/null)" || fail "rig setup (non-interactive)"
grep -q "rig config get" <<<"$setup_out" || fail "rig setup non-interactive: missing config-usage pointer"
grep -q "non-interactive" <<<"$setup_out" || fail "rig setup non-interactive: not flagged non-interactive"
pass "rig setup (no TTY) → usage for init/apply/config"

# ── 3. headless setup against a sample config, isolated HOME ──────────────────
# Apply FROM the agent-tools checkout resolved up top (before HOME gets isolated).
SRC="$AGENT_TOOLS"
if [[ -z "$SRC" || ! -d "$SRC/skills" ]]; then
  printf '  \033[33m○ skip\033[0m apply smoke — no agent-tools checkout found (set RIG_AGENT_TOOLS_SOURCE)\n'
else
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  export HOME="$TMP/home"; mkdir -p "$HOME"
  # exercise the tmux catalog area through the real CLI but NEVER touch the live machine: the
  # dry-run guard writes the on-disk artifacts (config + boot script + plist) into the throwaway
  # HOME while skipping every live step (no plugin clone, no `launchctl load`, no resurrect save).
  export RIG_TMUX_DRY_RUN=1
  git config --global user.email smoke@rig.test
  git config --global user.name  rig-smoke
  ( cd "$TMP" && git init -q )

  # The CLEAN sample. NOTE: it intentionally carries NO `mcp.items.review` — that slot was
  # REMOVED in agent-tools #32 (review is a CLI+skill, not an MCP). A config still naming it is
  # exercised SEPARATELY below (the removed-slot leg), where it must fail with the 3-part error.
  cat > "$TMP/rig.yaml" <<YAML
version: 1
agent_tools_source: $SRC
skills:
  universal: { all: true }
  by_type:   { enable: [cli] }
agent_hooks: { all: true }
git_hooks:
  dispatcher: { enabled: true }
ci:
  items:
    secret-scan: { enabled: true, tier: block }
    ship:        { enabled: true, install_to: ~/bin, gh_alias: false }
tmux:
  enabled: true
  apply: import
  resurrect: { processes: [ssh, psql], capture_pane_contents: true }
  continuum: { restore: true, save_interval: 15, boot: true }
  cc_restore: { enabled: true }
  anti_sprawl: { enabled: true, session: main }
  boot: { enabled: true }
  login_shell: { enabled: true }
gitignore:
  enabled: true
YAML
  # the review MCP server is only declarable when the agent-tools checkout actually carries it
  # (a README-only mcp/ dir — an incomplete checkout — would make `review` an unknown item and
  # fail the whole apply on an env unrelated to what this smoke proves). Include it only if present.
  if [[ -d "$SRC/mcp/review" ]]; then
    cat >> "$TMP/rig.yaml" <<YAML
mcp:
  items:
    review: { enabled: true, command: "review --mcp" }
YAML
  fi

  # ── HEAVY apply legs (skipped under --fast: they install skills / harness symlinks / tmux /
  #    tg-ctl and take seconds-to-minutes; CI runs them, the pre-commit subset does not). The
  #    REAL-catalog `rig status` legs below (clean/removed-slot/non-git) DO run under --fast —
  #    they are the cheap regression guard the pre-commit gate exists for. ──────────────────────
  if [[ $FAST -eq 0 ]]; then
  # dry-run first (must write nothing)
  $RIG init -C "$TMP" --config "$TMP/rig.yaml" --yes --dry-run >/dev/null || fail "init --dry-run"
  [[ -d "$HOME/.agents/skills" ]] && fail "dry-run wrote skills"
  [[ -e "$HOME/.claude/skills" ]] && fail "dry-run wrote harness skill links"
  pass "rig init --dry-run wrote nothing"

  # real apply — `--apply` is the explicit one-shot (init SCAFFOLDS + previews by default; the
  # plan is applied only on --apply, or via a separate `rig apply`).
  $RIG init -C "$TMP" --config "$TMP/rig.yaml" --yes --apply >/dev/null || fail "init --yes --apply"
  [[ -d "$HOME/.agents/skills" ]] || fail "skills not installed"
  [[ -f "$TMP/.github/workflows/secret-scan.yml" ]] || fail "secret-scan workflow not written"
  [[ -x "$HOME/.config/git/run-global-hooks" ]] || fail "dispatcher runner not installed"
  # harness discovery: each installed skill is symlinked into ~/.claude/skills and resolves
  one_skill="$(find "$HOME/.agents/skills" -mindepth 1 -maxdepth 1 -type d | head -1)"
  sk_name="$(basename "$one_skill")"
  [[ -L "$HOME/.claude/skills/$sk_name" ]] || fail "skill '$sk_name' not symlinked into harness dir"
  [[ -f "$HOME/.claude/skills/$sk_name/SKILL.md" ]] || fail "harness skill link does not resolve"
  pass "rig init --yes --apply installed skills + CI + dispatcher + harness skill links"

  # permissions (default-ON): the command allowlist lands in the harness settings.json with our
  # ecosystem CLIs + safe dev tools pre-allowed (Bash(<tool>:*)), AND the deny/ask rule baselines
  # (rig-cli#100 — the outer belt) land alongside it. This is the security-sensitive default-on
  # path — assert it actually wrote, not just that status is green.
  CCSET="$HOME/.claude/settings.json"
  [[ -f "$CCSET" ]] || fail "permissions: harness settings.json not written"
  # structural check (robust to JSON formatting), not a brittle grep of the pretty-printed text
  python3 - "$CCSET" "$ROOT" <<'PY' || fail "permissions: allow tools / deny+ask baselines not in settings.json"
import json, sys
# Import the checkout under test, not any installed riglib from the developer's environment.
sys.path.insert(0, sys.argv[2])
from riglib.permissions import DEFAULT_TOOLS
perms = json.load(open(sys.argv[1])).get("permissions", {})
allow = set(perms.get("allow", []))
# the full default set — our ecosystem CLIs + the safe external dev tools
missing = {f"Bash({t}:*)" for t in DEFAULT_TOOLS} - allow
# the deny/ask baselines (spot-check one loud rule each — the exact list is unit-tested)
if "Bash(sudo rm:*)" not in set(perms.get("deny", [])):
    missing.add("deny: Bash(sudo rm:*)")
if "Bash(pkill:*)" not in set(perms.get("ask", [])):
    missing.add("ask: Bash(pkill:*)")
sys.exit(0 if not missing else (print("missing:", sorted(missing)) or 1))
PY
  pass "rig init --yes pre-allowed our CLIs + dev tools and asserted the deny/ask baselines"

  # tmux v2: the managed config + boot script (DEFECT 1) land on disk (dry-run skips only the
  # LIVE steps — plugin clone / launchctl load — not the artifact writes).
  [[ -f "$HOME/.config/rig/tmux/rig.tmux.conf" ]] || fail "tmux: rig.tmux.conf not generated"
  [[ -f "$HOME/.config/rig/tmux/tmux-boot.sh" ]]  || fail "tmux: boot script (DEFECT 1) not generated"
  grep -Eq '^[[:space:]]*[^#].*new-session -d' "$HOME/.config/rig/tmux/tmux-boot.sh" \
    || fail "tmux: boot script must use 'new-session -d', not a bare start-server (DEFECT 1)"
  # require the real directive, not a comment that merely mentions it (codex finding)
  grep -Eq '^[[:space:]]*set -g default-command' "$HOME/.config/rig/tmux/rig.tmux.conf" \
    || fail "tmux: login-shell 'set -g default-command' (DEFECT 3) not in generated config"
  pass "rig init --yes generated tmux v2 config + boot script (login-shell, new-session -d)"

  # ── global git-excludes (core.excludesfile) — clean-machine path ────────────────
  # core.excludesfile was UNSET in this isolated HOME, so apply must (1) set it to the XDG
  # default and (2) write the rig-managed block there. Everything stays under the throwaway
  # HOME; the real global git config is never touched.
  excludes_val="$(git config --global core.excludesfile || true)"
  [[ "$excludes_val" == "~/.config/git/ignore" ]] || fail "core.excludesfile not set to XDG default (got '$excludes_val')"
  excludes_file="$HOME/.config/git/ignore"
  [[ -f "$excludes_file" ]] || fail "global excludes file not written at $excludes_file"
  grep -q "rig-managed" "$excludes_file" || fail "rig-managed marker missing from global excludes file"
  grep -q "\.claude/worktrees/" "$excludes_file" || fail "worktrees entry missing from global excludes file"
  pass "rig init --yes set core.excludesfile + wrote the rig-managed global-excludes block"

  # idempotency: a second apply changes nothing (no created/updated/backed_up in summary)
  out="$($RIG apply -C "$TMP" --config "$TMP/rig.yaml" 2>&1)"
  summary="$(echo "$out" | grep '^Summary:' || true)"
  if echo "$summary" | grep -Eq "(created|updated|backed_up)=[1-9]"; then
    fail "second apply was not idempotent: $summary"
  fi
  pass "rig apply is idempotent ($summary)"

  # the second apply must not have duplicated the managed block: exactly one begin + one end.
  marker_count="$(grep -c "rig-managed" "$excludes_file" || true)"
  [[ "$marker_count" -eq 2 ]] || fail "global-excludes block churned/duplicated on re-apply (markers=$marker_count, want 2)"
  pass "global-excludes block is byte-stable across re-apply (markers=2)"

  # status reports in sync
  $RIG status -C "$TMP" --config "$TMP/rig.yaml" >/dev/null || fail "status nonzero when in sync"
  pass "rig status: in sync"
  fi  # end HEAVY apply legs (--fast skips to the cheap real-catalog status legs below)

  # ── error-system v2: a removed slot prints the 3-part error + exit code 4 ──────
  # This is the exact regression class that was unit-green but smoke-broken: a stale
  # `mcp.items.review` (removed in agent-tools #32) must fail LOUDLY against the REAL catalog.
  DEAD="$TMP/dead.yaml"
  cat > "$DEAD" <<YAML
version: 1
agent_tools_source: $SRC
skills: { enabled: false }
agent_hooks: { enabled: false }
ci: { enabled: false }
git_hooks: { dispatcher: { enabled: false } }
mcp:
  items:
    review: { enabled: true, command: "review --mcp" }
YAML
  # capture both the rendered error and the exit code (don't let `set -e` mask the code).
  set +e
  dead_out="$($RIG status -C "$TMP" --config "$DEAD" 2>&1)"
  dead_rc=$?
  set -e
  echo "$dead_out" | grep -qi "removed mcp slot"          || fail "removed-slot: no WHAT line"
  echo "$dead_out" | grep -q  "#32"                       || fail "removed-slot: WHY does not name the removal PR"
  echo "$dead_out" | grep -qi "remove .*mcp.items.review" || fail "removed-slot: FIX does not say how to remove it"
  echo "$dead_out" | grep -q  "$DEAD"                     || fail "removed-slot: error does not name the offending config file"
  [[ "$dead_rc" -eq 4 ]]                                  || fail "removed-slot: exit code $dead_rc != 4 (unknown-item class)"
  pass "rig status: removed slot → 3-part error + exit 4"

  # ── error-system v2: a CLEAN sample exits 0 (no false drift) ──────────────────
  CLEAN="$TMP/clean.yaml"
  cat > "$CLEAN" <<YAML
version: 1
agent_tools_source: $SRC
skills: { enabled: false }
agent_hooks: { enabled: false }
ci: { enabled: false }
git_hooks: { dispatcher: { enabled: false } }
mcp: { enabled: false }
agents_md: { enabled: false }
ship_delegator: { enabled: false }
gitignore: { enabled: false }
tg_ctl: { enabled: false }
permissions: { enabled: false }
YAML
  CLEANREPO="$TMP/clean-repo"; mkdir -p "$CLEANREPO"; ( cd "$CLEANREPO" && git init -q )
  cp "$CLEAN" "$CLEANREPO/rig.yaml"
  # a PRISTINE HOME/XDG: the earlier `rig init --yes` populated $TMP/home with skills+hooks, so
  # reusing it would show them as drift against this all-disabled config. A clean sample must be
  # judged against a clean machine.
  set +e
  HOME="$TMP/clean-home" XDG_CONFIG_HOME="$TMP/clean-xdg" $RIG status -C "$CLEANREPO" >/dev/null 2>&1
  clean_rc=$?
  set -e
  [[ "$clean_rc" -eq 0 ]] || fail "clean sample: exit code $clean_rc != 0"
  pass "rig status: clean sample → exit 0"

  # ── error-system v2: a NON-GIT dir does NOT nag "should be committed" ─────────
  # IMPORTANT: $TMP itself was `git init`-ed above, so a dir UNDER it ($TMP/nongit) is still
  # *inside* that work tree (git walks up to $TMP/.git) — env.is_git_repo would be True and this
  # would not exercise the non-git path at all. Use a sibling mktemp dir outside any repo.
  NONGIT="$(mktemp -d)"; trap 'rm -rf "$TMP" "$NONGIT"' EXIT   # a plain dir, no .git, no repo above
  set +e
  nongit_out="$(HOME="$TMP/clean-home" XDG_CONFIG_HOME="$TMP/clean-xdg" $RIG status -C "$NONGIT" 2>&1)"
  set -e
  echo "$nongit_out" | grep -qi "should be committed" && fail "non-git: still nags 'should be committed'"
  echo "$nongit_out" | grep -qi "not a git repository" || fail "non-git: does not say 'not a git repository'"
  pass "rig status: non-git dir → no 'should be committed', repo layer N/A"

  # ── real-catalog FULL coverage: discover + dry-run-plan EVERY catalog item ────────
  # The regression class the synthetic fixtures miss: a new slot / renamed dir rig can't
  # resolve. Runs in BOTH modes (incl. --fast) — it's a dry-run (writes nothing), so it's the
  # cheap real-catalog guard the pre-commit gate is for. Uses fresh HOME/repo so the earlier
  # apply legs' artifacts can't perturb the plan; it asserts only DISCOVERY + planning.
  COVREPO="$TMP/cov-repo"; mkdir -p "$COVREPO"; ( cd "$COVREPO" && git init -q )
  _real_catalog_full_coverage "$TMP/cov-home" "$COVREPO" "$SRC"
fi

# ── 3b. tg-ctl inbound-daemon LaunchAgent provisioning (macOS, dry-run isolated) ──
# Proves the tg_ctl boot LaunchAgent is provisioned by `rig apply` with the plist landing under
# the ISOLATED HOME and NO real `launchctl` call (RIG_TG_CTL_DRY_RUN, exported at the top).
# macOS-only (launchd); skipped off darwin and when no agent-tools checkout is found (rig's plan
# build requires a source even when carrier categories are off). Uses the source resolved up top.
# A real `rig apply` → HEAVY, so the --fast pre-commit subset skips it (CI still runs it).
if [[ $FAST -ne 0 ]]; then
  printf '  \033[33m○ skip\033[0m tg-ctl leg — --fast (apply leg; runs in CI)\n'
elif [[ "$(uname -s)" != "Darwin" ]]; then
  printf '  \033[33m○ skip\033[0m tg-ctl leg — not macOS (launchd-only)\n'
elif [[ -z "$AGENT_TOOLS" || ! -d "$AGENT_TOOLS/skills" ]]; then
  printf '  \033[33m○ skip\033[0m tg-ctl leg — no agent-tools checkout (set RIG_AGENT_TOOLS_SOURCE)\n'
else
  TG_TMP="$(mktemp -d)"
  TG_HOME="$TG_TMP/home"; mkdir -p "$TG_HOME"
  ( cd "$TG_TMP" && git init -q )
  # a FOCUSED tg-ctl config: every other default-on category off (incl. models, whose cron would
  # otherwise shell out to launchctl) so the leg exercises ONLY tg-ctl.
  cat > "$TG_TMP/rig.yaml" <<YAML
version: 1
agent_tools_source: $AGENT_TOOLS
skills:      { enabled: false }
agent_hooks: { enabled: false }
ci:          { enabled: false }
mcp:         { enabled: false }
agents_md:   { enabled: false }
github:      { ruleset: { enabled: false } }
models:      { enabled: false }
tg_ctl:
  enabled: true
  bun_path: /usr/bin/true
  tg_ctl_path: $TG_HOME/.files/bin/tg-ctl
  config_dir: $TG_HOME/.config/tg-cli
YAML
  PLIST="$TG_HOME/Library/LaunchAgents/ai.hyperide.tg-ctl.plist"
  HOME="$TG_HOME" $RIG apply -C "$TG_TMP" --config "$TG_TMP/rig.yaml" >/dev/null 2>&1 \
    || { rm -rf "$TG_TMP"; fail "tg-ctl apply (dry-run) nonzero"; }
  [[ -f "$PLIST" ]] || { rm -rf "$TG_TMP"; fail "tg-ctl plist not written under isolated HOME"; }
  grep -q "<string>ai.hyperide.tg-ctl</string>" "$PLIST" \
    || { rm -rf "$TG_TMP"; fail "tg-ctl plist missing Label"; }
  # idempotency: a second apply against the now-current plist must change NOTHING — the summary
  # must carry no non-zero created/updated/backed_up count. (We do NOT assert `rig status` in-sync
  # here: under dry-run the plist is written but never bootstrapped, and drift's loaded-state check
  # queries the REAL launchd domain — a machine-dependent result the smoke can't control. The
  # deterministic drift/in-sync coverage lives in test_tg_ctl.py with the launchctl seams stubbed.)
  out="$(HOME="$TG_HOME" $RIG apply -C "$TG_TMP" --config "$TG_TMP/rig.yaml" 2>&1)"
  summary="$(echo "$out" | grep '^Summary:' || true)"
  if echo "$summary" | grep -Eq "(created|updated|backed_up)=[1-9]"; then
    rm -rf "$TG_TMP"; fail "tg-ctl second apply was not idempotent: $summary"
  fi
  rm -rf "$TG_TMP"
  pass "rig provisions tg-ctl boot LaunchAgent (dry-run, isolated, idempotent)"
fi

# ── 4. unit suite ─────────────────────────────────────────────────────────────
# Run pytest the way the repo actually runs it: prefer `uv run --with pytest` (the documented
# command — README/AGENTS.md), so a machine whose bare `python3` is a clean interpreter without
# pytest installed still runs the suite. Fall back to `python3 -m pytest` when uv is absent.
# Skipped under --fast: the full suite (~20s) is a CI gate, not a per-commit local one — the
# pre-commit subset's job is the REAL-catalog `rig status` regression guard, kept seconds-cheap.
if [[ $FAST -ne 0 ]]; then
  printf '  \033[33m○ skip\033[0m pytest — --fast (full unit suite runs in CI)\n'
  echo "smoke OK (--fast)"
  exit 0
fi
echo "running pytest…"
if command -v uv >/dev/null 2>&1; then
  uv run --with pytest python -m pytest -q "$ROOT/tests" || fail "pytest"
else
  python3 -m pytest -q "$ROOT/tests" || fail "pytest"
fi
pass "pytest"

echo "smoke OK"
