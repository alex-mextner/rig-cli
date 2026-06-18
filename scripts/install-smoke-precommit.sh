#!/bin/sh
# install-smoke-precommit.sh — wire this repo's fast-smoke gate into the LOCAL pre-commit hook.
#
# Installs a thin shim at <git-dir>/hooks/pre-commit that execs the TRACKED gate
# (scripts/smoke-precommit-hook.sh), so `bash tests/smoke.sh --fast` runs before every commit
# IN THIS WORKING COPY — the local half of the CTO's 2026-06-16 requirement (smoke already
# gated CI; this gates the developer's machine too). Idempotent: re-running is a no-op.
#
# The hook is per-clone and untracked, so this MUST be run once per checkout/worktree (CI does
# not need it — CI runs the full smoke directly). `rig apply` can run it for you; here it is a
# standalone script so a contributor without rig can wire the gate by hand.
#
# USAGE: scripts/install-smoke-precommit.sh [REPO_DIR]   (defaults to the current repo)
set -eu

MARKER="rig-smoke-precommit"   # idempotency marker embedded in the installed shim

REPO="${1:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
[ -n "$REPO" ] || { echo "install-smoke-precommit: not in a git repo and no REPO_DIR given" >&2; exit 2; }
[ -e "$REPO/.git" ] || { echo "install-smoke-precommit: $REPO is not a git repo" >&2; exit 2; }
REPO="$(cd "$REPO" && pwd)"

GATE="$REPO/scripts/smoke-precommit-hook.sh"
[ -f "$GATE" ] || { echo "install-smoke-precommit: gate script missing at $GATE" >&2; exit 2; }

# Resolve the hooks dir against the ABSOLUTE git-dir so this is correct for a normal repo, a
# worktree (.git is a file → git-dir lives elsewhere), and a submodule. Honor a repo-local
# core.hooksPath if one is set; otherwise the standard <git-dir>/hooks.
GIT_DIR="$(git -C "$REPO" rev-parse --absolute-git-dir 2>/dev/null || true)"
[ -n "$GIT_DIR" ] || { echo "install-smoke-precommit: cannot resolve git dir for $REPO" >&2; exit 2; }
HOOKS_PATH="$(git -C "$REPO" config --local --get core.hooksPath 2>/dev/null || true)"
# git returns the value VERBATIM, so a leading ~ (a perfectly legal core.hooksPath) arrives
# unexpanded — resolve it to $HOME before use, or `$REPO/~/…` nonsense gets created.
case "$HOOKS_PATH" in
  "~")   HOOKS_PATH="$HOME" ;;
  "~/"*) HOOKS_PATH="$HOME/${HOOKS_PATH#"~/"}" ;;
esac
case "$HOOKS_PATH" in
  # Unset OR the conventional ".git/hooks" → use the RESOLVED git-dir hooks. This is correct for
  # a normal repo AND a worktree/submodule, where "$REPO/.git" is a FILE (not a dir) and the real
  # hooks live under the absolute git-dir, so "$REPO/.git/hooks" would be a broken path.
  ""|".git/hooks") HOOKS_DIR="$GIT_DIR/hooks" ;;
  /*)              HOOKS_DIR="$HOOKS_PATH" ;;
  *)               HOOKS_DIR="$REPO/$HOOKS_PATH" ;;
esac
mkdir -p "$HOOKS_DIR"
HOOK="$HOOKS_DIR/pre-commit"

# Refuse to mangle a SYMLINKED pre-commit (husky / pre-commit-framework / dotfiles often link
# it): an in-place prepend would either detach the link (mv) or rewrite the target's file. The
# wiring belongs in the manager's own config there — bail loudly rather than silently break it.
if [ -L "$HOOK" ]; then
  echo "install-smoke-precommit: $HOOK is a SYMLINK (a hook manager owns it). Refusing to" >&2
  echo "  modify it. Wire 'bash tests/smoke.sh --fast' into that manager's pre-commit instead." >&2
  exit 3
fi

# Already wired? (marker present) → no-op.
if [ -f "$HOOK" ] && grep -q "$MARKER" "$HOOK" 2>/dev/null; then
  echo "install-smoke-precommit: pre-commit already wired ($HOOK) — no-op"
  exit 0
fi

if [ ! -f "$HOOK" ]; then
  # No existing hook: write a self-contained shim that EXECs the tracked gate (the gate owns
  # the dispatcher-chaining decision; nothing runs after it, so exec is correct).
  {
    printf '#!/bin/sh\n'
    printf '# %s — runs the tracked fast-smoke gate (scripts/smoke-precommit-hook.sh).\n' "$MARKER"
    printf '# Keep this shim TINY; the logic is tracked so it can be reviewed/tested. Re-run\n'
    printf '# scripts/install-smoke-precommit.sh after a fresh clone/worktree to (re)install it.\n'
    printf '_repo="$(git rev-parse --show-toplevel 2>/dev/null || true)"\n'
    printf 'if [ -n "$_repo" ] && [ -f "$_repo/scripts/smoke-precommit-hook.sh" ]; then\n'
    printf '  exec sh "$_repo/scripts/smoke-precommit-hook.sh" "$@"\n'
    printf 'fi\n'
    # Reached ONLY when the gate file is missing (e.g. a stale checkout) — then there is nothing
    # to run, so pass. The exec above replaces this process on the normal path; the gate''s own
    # non-zero exit blocks the commit, so this exit 0 is never the success-path fallthrough.
    printf '# only reached if the gate script is absent (stale checkout) — nothing to gate, pass.\n'
    printf 'exit 0\n'
  } > "$HOOK"
  chmod +x "$HOOK"
  echo "install-smoke-precommit: installed pre-commit gate ($HOOK)"
else
  # An existing FOREIGN hook: PREPEND our gate so it runs FIRST, then fall through to the
  # existing body. We do NOT exec, so the existing body still runs after us.
  #
  # We can only safely inline shell into a SHELL hook. A hook with a non-shell shebang
  # (python/node/ruby) would be corrupted by prepended `sh` syntax, so refuse it and tell the
  # user to wire the gate by hand.
  shebang="$(head -n1 "$HOOK" 2>/dev/null)"
  case "$shebang" in
    "#!"*/sh|"#!"*/sh" "*|"#!"*/bash|"#!"*/bash" "*|"#!"*"/env sh"*|"#!"*"/env bash"*) : ;;
    "#!"*)
      echo "install-smoke-precommit: existing $HOOK has a non-shell shebang ($shebang)." >&2
      echo "  Refusing to inline shell into it. Add 'bash tests/smoke.sh --fast' to that hook" >&2
      echo "  yourself (or remove it and re-run this installer)." >&2
      exit 3
      ;;
    *) : ;;  # no shebang → treat as /bin/sh (git's default), safe to inline
  esac
  # Dispatcher chaining in the prepend case: we let the gate KEEP chaining the global dispatcher
  # (the default) and do NOT try to guess whether the foreign body already owns it. Statically
  # deciding "does this hook actually invoke run-global-hooks?" from text is unreliable (a
  # mention in an echo / a dead branch / a helper indirection all mislead), and the two error
  # modes are NOT symmetric: skipping our chain when the foreign hook does NOT run the dispatcher
  # DROPS secret-scan (a security hole), whereas chaining when it DOES run it merely runs the
  # read-only scan twice (slower, harmless). So we fail SAFE — always chain. The composer case
  # (a global core.hooksPath that runs the dispatcher itself) is still de-duped, but reliably,
  # inside the gate via a path-canonicalized core.hooksPath check — not by grepping the hook.
  tmp="$(mktemp "${TMPDIR:-/tmp}/rigsmoke.XXXXXX")"
  # Clean up BOTH scratch files on any exit/interrupt so a partial write can't leak (and never
  # gets mv'd into place — the mv is the last step, after a successful awk).
  trap 'rm -f "$tmp" "$tmp.body"' EXIT INT TERM
  {
    printf '# %s — fast-smoke gate, prepended ahead of the pre-existing hook below.\n' "$MARKER"
    printf '_repo="$(git rev-parse --show-toplevel 2>/dev/null || true)"\n'
    printf 'if [ -n "$_repo" ] && [ -f "$_repo/scripts/smoke-precommit-hook.sh" ]; then\n'
    printf '  sh "$_repo/scripts/smoke-precommit-hook.sh" "$@" || exit $?\n'
    printf 'fi\n'
  } > "$tmp.body"
  # Where to insert our block: AFTER a shebang on line 1 (keep it line 1), else at the very TOP
  # (a no-shebang hook whose first line is real code — e.g. `exit 0` — would otherwise run before
  # our gate and skip it entirely).
  if [ "${shebang#"#!"}" != "$shebang" ]; then
    INSERT_AFTER=1   # line 1 is a shebang → insert after it
  else
    INSERT_AFTER=0   # no shebang → insert before the first line
  fi
  awk -v bodyfile="$tmp.body" -v after="$INSERT_AFTER" '
    BEGIN { if (after == 0) { while ((getline line < bodyfile) > 0) print line; close(bodyfile) } }
    NR==1 && after==1 { print; while ((getline line < bodyfile) > 0) print line; close(bodyfile); next }
    { print }
  ' "$HOOK" > "$tmp"
  mv "$tmp" "$HOOK"
  chmod +x "$HOOK"
  echo "install-smoke-precommit: prepended pre-commit gate into existing hook ($HOOK)"
fi
