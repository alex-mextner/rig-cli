#!/usr/bin/env bash
# smoke.sh — fast end-to-end check of the rig CLI surface, run in CI and locally.
# Proves: --help, doctor, a headless setup against a sample config in a throwaway repo
# (isolated HOME so nothing on the machine is touched), idempotency, status drift, and the
# pytest unit suite.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RIG="python3 $ROOT/bin/rig"

pass() { printf '  \033[32m✔\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗ %s\033[0m\n' "$1"; exit 1; }

echo "rig smoke — $ROOT"

# ── 1. --help / --version ─────────────────────────────────────────────────────
$RIG --help   >/dev/null 2>&1 || fail "rig --help"
$RIG --version >/dev/null 2>&1 || fail "rig --version"
pass "rig --help / --version"

# ── 2. doctor (informational; never fails the smoke on missing optional deps) ──
$RIG doctor >/dev/null 2>&1 || true
pass "rig doctor"

# ── 3. headless setup against a sample config, isolated HOME ──────────────────
# Locate an agent-tools checkout to apply FROM. Prefer the env override, then defaults.
SRC="${RIG_AGENT_TOOLS_SOURCE:-}"
for cand in "$SRC" "$HOME/xp/agent-tools" "$HOME/work/agent-tools" "$HOME/agent-tools"; do
  if [[ -n "$cand" && -d "$cand/skills" && -d "$cand/agent-hooks" ]]; then SRC="$cand"; break; fi
done
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
YAML

  # dry-run first (must write nothing)
  $RIG init -C "$TMP" --config "$TMP/rig.yaml" --yes --dry-run >/dev/null || fail "init --dry-run"
  [[ -d "$HOME/.agents/skills" ]] && fail "dry-run wrote skills"
  [[ -e "$HOME/.claude/skills" ]] && fail "dry-run wrote harness skill links"
  pass "rig init --dry-run wrote nothing"

  # real apply
  $RIG init -C "$TMP" --config "$TMP/rig.yaml" --yes >/dev/null || fail "init --yes"
  [[ -d "$HOME/.agents/skills" ]] || fail "skills not installed"
  [[ -f "$TMP/.github/workflows/secret-scan.yml" ]] || fail "secret-scan workflow not written"
  [[ -x "$HOME/.config/git/run-global-hooks" ]] || fail "dispatcher runner not installed"
  # harness discovery: each installed skill is symlinked into ~/.claude/skills and resolves
  one_skill="$(find "$HOME/.agents/skills" -mindepth 1 -maxdepth 1 -type d | head -1)"
  sk_name="$(basename "$one_skill")"
  [[ -L "$HOME/.claude/skills/$sk_name" ]] || fail "skill '$sk_name' not symlinked into harness dir"
  [[ -f "$HOME/.claude/skills/$sk_name/SKILL.md" ]] || fail "harness skill link does not resolve"
  pass "rig init --yes installed skills + CI + dispatcher + harness skill links"

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

  # idempotency: a second apply changes nothing (no created/updated/backed_up in summary)
  out="$($RIG apply -C "$TMP" --config "$TMP/rig.yaml" 2>&1)"
  summary="$(echo "$out" | grep '^Summary:' || true)"
  if echo "$summary" | grep -Eq "(created|updated|backed_up)=[1-9]"; then
    fail "second apply was not idempotent: $summary"
  fi
  pass "rig apply is idempotent ($summary)"

  # status reports in sync
  $RIG status -C "$TMP" --config "$TMP/rig.yaml" >/dev/null || fail "status nonzero when in sync"
  pass "rig status: in sync"
fi

# ── 4. unit suite ─────────────────────────────────────────────────────────────
echo "running pytest…"
python3 -m pytest -q "$ROOT/tests" || fail "pytest"
pass "pytest"

echo "smoke OK"
