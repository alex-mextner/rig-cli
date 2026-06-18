#!/bin/sh
# smoke-precommit-hook.sh — the rig-cli repo-local pre-commit GATE.
#
# WHAT it does, in order, blocking the commit if any step is non-zero:
#   1. `bash tests/smoke.sh --fast` — the seconds-cheap REAL-catalog smoke subset (rig
#      --help/--version/doctor/setup-usage + the `rig status` regression legs: a clean
#      sample exits 0, a removed slot prints the 3-part error + exit 4, a non-git dir
#      doesn't nag). This is the LOCAL half of the CTO's 2026-06-16 requirement: smoke
#      already ran in CI, but a commit that broke the real `rig status` flow (a stale
#      `mcp.items.review`, a dead slot) was caught only AFTER push. Now it's blocked here.
#   2. the global git-hook dispatcher (secret-scan + future global hooks) — but ONLY when
#      this hook is invoked DIRECTLY by git (raw-git repo). When a global core.hooksPath
#      composer is active it runs THIS hook first and the dispatcher itself afterwards, so
#      chaining here too would double-run secret-scan. We detect the composer and defer to
#      it (no double run); a raw repo with no composer still gets the dispatcher from here.
#
# WHY a tracked script (not inline in .git/hooks): .git/hooks is per-clone and untracked, so
# the LOGIC must live in the tree to be reviewed, tested, and kept in sync. The installed
# .git/hooks/pre-commit is a thin shim that execs this file (see install-smoke-precommit.sh).
#
# Bypass (last resort, discouraged): SKIP_RIG_SMOKE=1 git commit …  | git commit --no-verify.
set -eu

# Repo root — correct for normal repos, worktrees, and submodules.
REPO="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[ -n "$REPO" ] || exit 0   # not in a work tree (e.g. bare) — nothing to gate.

# --- 1. the fast smoke subset (only when this repo actually ships it). ----------------
SMOKE="$REPO/tests/smoke.sh"
if [ -x "$SMOKE" ] || [ -f "$SMOKE" ]; then
  if [ "${SKIP_RIG_SMOKE:-}" = "1" ]; then
    echo "smoke-precommit: SKIP_RIG_SMOKE=1 → skipping the fast smoke gate" >&2
  else
    echo "smoke-precommit: running fast smoke (rig status real-catalog gate)…" >&2
    # CRITICAL: git exports GIT_DIR / GIT_INDEX_FILE / GIT_WORK_TREE / GIT_PREFIX into the hook's
    # environment (they point at THIS commit's repo + staged index). smoke spawns its own `git`
    # and `rig status` against throwaway repos it creates; if those inherited vars leak in, the
    # spawned git resolves the WRONG repo/index and a leg like the clean-sample `rig status`
    # fails (exit 3) for a reason that has nothing to do with the staged change. Strip the whole
    # GIT_* hook env for the smoke run so it is hermetic (the env-leak surfaced via dogfooding).
    if ! env -u GIT_DIR -u GIT_INDEX_FILE -u GIT_WORK_TREE -u GIT_PREFIX \
            -u GIT_OBJECT_DIRECTORY -u GIT_COMMON_DIR -u GIT_NAMESPACE \
            bash "$SMOKE" --fast; then
      echo "smoke-precommit: BLOCKED — fast smoke failed. Fix the rig CLI flow above, or" >&2
      echo "                 bypass with SKIP_RIG_SMOKE=1 (discouraged) / --no-verify." >&2
      exit 1
    fi
  fi
fi

# --- 2. chain the global dispatcher iff NO ONE ELSE will run it for us. ----------------
# Two ways the dispatcher is already covered, so we must NOT run it again (a double secret
# scan):
#   (a) a global core.hooksPath COMPOSER is active — it runs $git_dir/hooks/pre-commit (this
#       shim) FIRST and the dispatcher itself afterwards; or
#   (b) we were PREPENDED into a pre-existing foreign hook that already chains the dispatcher
#       — the installer sets RIG_SMOKE_GATE_CHAIN_DISPATCHER=0 in that case and the foreign
#       body (which runs after us) owns the dispatcher.
DISP="${XDG_CONFIG_HOME:-$HOME/.config}/git/run-global-hooks"
COMPOSER_DIR="$(dirname "$DISP")/hooks"   # ~/.config/git/hooks — where the composer lives

# canon <dir> — echo the canonical (symlink/~/.. resolved) directory path, or "" if it does not
# exist. Used so the composer match is robust to ~, $HOME-vs-XDG, and symlinked equivalents
# (a literal string compare would miss them → a double secret-scan, which the dedup must avoid).
canon() {
  case "$1" in
    "~")   set -- "$HOME" ;;
    "~/"*) set -- "$HOME/${1#"~/"}" ;;
  esac
  [ -n "$1" ] || { printf '\n'; return; }
  ( CDPATH='' cd -- "$1" 2>/dev/null && pwd -P ) || printf '\n'
}

HOOKS_PATH="$(git -C "$REPO" config --get core.hooksPath 2>/dev/null || true)"
composer_active=0
if [ -n "$HOOKS_PATH" ]; then
  _hp_real="$(canon "$HOOKS_PATH")"
  _composer_real="$(canon "$COMPOSER_DIR")"
  # The composer is "active" (so WE must not also chain the dispatcher) ONLY when core.hooksPath
  # IS the composer dir AND that dir's pre-commit ACTUALLY invokes the dispatcher. Checking the
  # dir match alone is not enough: a pre-commit that does not run run-global-hooks (or a bare
  # `exit 0`) would make us skip our chain and DROP secret-scan. Fail SAFE — require proof the
  # composer runs it (a non-comment line referencing run-global-hooks); else we keep chaining.
  if [ -n "$_hp_real" ] && [ "$_hp_real" = "$_composer_real" ] \
     && [ -x "$COMPOSER_DIR/pre-commit" ] \
     && grep -Eq '^[[:space:]]*[^#[:space:]].*run-global-hooks' "$COMPOSER_DIR/pre-commit" 2>/dev/null; then
    composer_active=1
  fi
fi
if [ "${RIG_SMOKE_GATE_CHAIN_DISPATCHER:-1}" = "1" ] \
   && [ "$composer_active" -eq 0 ] && [ -x "$DISP" ]; then
  "$DISP" pre-commit "$@" || exit $?
fi

exit 0
