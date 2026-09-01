"""internal-dev daemon auto-reload — PURE planning + rendering of the post-commit hook.

What this is
------------
When a rig-ecosystem tool repo (rig, tg-cli, review-cli, …) is developed IN PLACE — the
checked-out files ARE the running binary (a live symlink) or a long-running daemon reads
them — a code change only takes effect after the daemon is restarted. This block wires that
restart to the commit: a repo that opts in gets a ``post-commit`` git hook that, when a commit
touches the configured daemon-source paths, runs a GRACEFUL reload command (``tg-ctl restart``
by default — Part 1 makes that reload drop no channel). So committing a change to tg-cli's
daemon auto-reloads the running daemon with zero manual restart.

This is a PER-REPO, COMMITTED concern (the enablement + the source paths travel with the repo's
``rig.yaml``, reproducibly, exactly like ``agent_hooks.worktree_only``) — so the block lives in
the REPO layer, NOT the global config. Only the reload COMMAND is machine-shaped, and it carries
a sensible default, so the whole block stays repo-owned.

How it is reached
-----------------
``plan._build_internal_dev`` reads the ``internal_dev:`` block; when ``auto_reload_on_commit`` is
truthy it emits ONE ``install_dev_reload_hook`` action carrying the resolved paths + command.
``runner._do_install_dev_reload_hook`` renders + writes the repo-local ``<git-dir>/hooks/
post-commit`` and (when a global ``core.hooksPath`` composer shadows it) ensures a generic
``post-commit`` composer trampoline exists. ``drift._check_internal_dev`` re-renders and diffs.

The composer gap (why a bare repo-local hook is not enough)
-----------------------------------------------------------
When git's ``core.hooksPath`` is set (the rig global-hook dispatcher sets it machine-wide), git
runs ONLY that dir's hooks and SHADOWS every repo's ``.git/hooks/*``. The agent-tools dispatcher
composer ships ``pre-commit``/``commit-msg``/``pre-push`` — but NO ``post-commit`` — so under the
composer a repo-local ``post-commit`` never fires. This module therefore also renders a generic
``post-commit`` COMPOSER that trampolines the shadowed ``<git-dir>/hooks/post-commit`` and then
the ``run-global-hooks post-commit`` dispatcher fragments (mirroring how the ``pre-commit``
composer trampolines the repo-local pre-commit). rig writes it only when a composer is actually
active, so a raw-``.git/hooks`` repo is untouched.

Invariants
----------
- **Idempotent, marker-keyed.** Both artifacts carry a version sentinel (:data:`HOOK_MARKER` /
  :data:`COMPOSER_MARKER`); a re-apply with identical content is a no-op, a differing prior is
  backed up per ``on_conflict``.
- **The reload never blocks a commit.** post-commit runs AFTER the commit is recorded; the hook
  reports and swallows a failed reload (a broken daemon must not wedge the developer's commit).
- **Dry-run gate (:data:`DRY_RUN_ENV`).** The hook reads it at FIRE time and skips the real
  reload; the runner reads it at APPLY time and skips the machine-global composer write (the one
  live/global mutation). The unit suite + smoke set it so tests/CI never fire ``tg-ctl restart``
  nor touch the real global hooks dir.

Stdlib-only (``subprocess``/``shlex``/``pathlib``): safe to import at module load.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The default graceful reload command. tg-ctl's `restart` is graceful (durable deferred-message
# queue + cooperative SIGTERM drain) so a reload drops no inbound channel — see Part 1 in tg-cli.
DEFAULT_RELOAD_COMMAND = "tg-ctl restart"

# Version sentinels embedded in each generated artifact. Drift + idempotency key off the FULL
# rendered content, but the marker lets a human (and a grep) tell a rig-managed hook from a
# hand-written one, and bumps if the template changes shape.
HOOK_MARKER = "rig-dev-reload-hook: v1"
COMPOSER_MARKER = "rig-dev-reload-composer: v1"

# The env var that neutralizes the live reload (hook, fire time) + the global composer write
# (runner, apply time). Mirrors RIG_TG_CTL_DRY_RUN / RIG_TMUX_DRY_RUN.
DRY_RUN_ENV = "RIG_DEV_RELOAD_DRY_RUN"


@dataclass(frozen=True)
class DevReloadPlan:
    """The desired post-commit auto-reload state, fully resolved. Pure data, no I/O."""

    repo_root: Path
    daemon_source_paths: tuple[str, ...]
    reload_command: str

    def render_hook(self) -> str:
        """The repo-local ``post-commit`` hook: reload the daemon when a commit touches a
        daemon-source path.

        The paths + command are embedded shell-safely (``shlex.quote``) at render time, so the
        hook is self-contained and needs no runtime YAML parse. Path patterns are matched as
        POSIX ``case`` globs against each changed file (repo-relative, from ``git diff-tree``);
        in ``case`` a ``*`` spans ``/`` too, so ``src/daemon/*`` matches nested files.
        """
        patterns_blob = shlex.quote("\n".join(self.daemon_source_paths))
        reload_cmd = shlex.quote(self.reload_command)
        return _HOOK_TEMPLATE.format(
            marker=HOOK_MARKER,
            dry_env=DRY_RUN_ENV,
            patterns=patterns_blob,
            reload_command=reload_cmd,
        )


def build_dev_reload(
    *,
    repo_root: Path,
    daemon_source_paths: list[str] | tuple[str, ...],
    reload_command: str = DEFAULT_RELOAD_COMMAND,
) -> DevReloadPlan:
    """Resolve a :class:`DevReloadPlan` from the (already-validated) ``internal_dev`` block."""
    paths = tuple(str(p) for p in daemon_source_paths if str(p).strip())
    cmd = str(reload_command).strip() or DEFAULT_RELOAD_COMMAND
    return DevReloadPlan(repo_root=Path(repo_root), daemon_source_paths=paths, reload_command=cmd)


def resolve_git_dir(repo_root: Path) -> Path:
    """The COMMON git dir for ``repo_root`` — correct for a LINKED worktree, where hooks live.

    Git hooks are not per-worktree: a linked worktree (``git worktree add``, which is how this
    very repo's own agent checkouts under ``.claude/worktrees/*`` are made) reads
    ``<hooks>`` from the repo's COMMON dir, not from its own private
    ``<main>/.git/worktrees/<name>/`` administrative dir. ``--absolute-git-dir`` returns the
    latter (correct for e.g. ``info/exclude``, which IS per-worktree) — using it here would
    install the hook where git never looks, silently. ``--git-common-dir`` is the shared dir
    every worktree's hooks resolve through (absent a ``core.hooksPath`` override, handled
    separately by :func:`composer_post_commit_path`). Falls back to ``<repo_root>/.git`` when
    git is unavailable (a non-worktree repo only — a linked worktree's ``.git`` is a FILE, so
    this fallback is a last resort, not worktree-safe).
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        )
        resolved = out.stdout.strip()
        if resolved:
            p = Path(resolved)
            return p if p.is_absolute() else Path(repo_root) / p
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(repo_root) / ".git"


def post_commit_hook_path(repo_root: Path) -> Path:
    """The repo-local ``post-commit`` hook path (``<git-dir>/hooks/post-commit``)."""
    return resolve_git_dir(repo_root) / "hooks" / "post-commit"


def effective_hooks_path(repo_root: Path) -> Path | None:
    """The repo's effective ``core.hooksPath`` (repo-local or global), expanded, or ``None``.

    ``None`` means git looks in ``<git-dir>/hooks`` (a repo-local hook fires directly). A value
    means a composer shadows the repo-local hooks — and, since the agent-tools composer ships no
    ``post-commit``, the repo-local ``post-commit`` will NOT fire without our trampoline.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--get", "core.hooksPath"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = out.stdout.strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def composer_post_commit_path(repo_root: Path) -> Path | None:
    """Where the generic ``post-commit`` composer trampoline belongs, or ``None`` when not needed.

    Returns a path ONLY when a composer is active (``core.hooksPath`` set to a dir OTHER than the
    repo-local ``<git-dir>/hooks``) AND that dir is the rig/agent-tools dispatcher composer layout
    (a sibling ``run-global-hooks`` exists). Otherwise ``None``: a raw-``.git/hooks`` repo needs
    no composer, and an unrelated ``core.hooksPath`` (e.g. a lefthook dir) is not ours to touch.
    """
    hooks_path = effective_hooks_path(repo_root)
    if hooks_path is None:
        return None
    local_hooks = resolve_git_dir(repo_root) / "hooks"
    if _same_dir(hooks_path, local_hooks):
        return None
    if not (hooks_path.parent / "run-global-hooks").exists():
        return None
    return hooks_path / "post-commit"


def render_post_commit_composer() -> str:
    """The generic ``post-commit`` COMPOSER: run the shadowed repo-local hook, then the dispatcher.

    Repo-agnostic (same bytes in every wired repo): the repo-specific reload logic lives in
    ``<git-dir>/hooks/post-commit``; this only restores the shadowed-by-core.hooksPath call to it,
    plus the ``run-global-hooks post-commit`` fragments. post-commit is informational — git ignores
    its exit status — so it never blocks and swallows a child failure.
    """
    return _COMPOSER_TEMPLATE.format(marker=COMPOSER_MARKER)


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


_HOOK_TEMPLATE = """\
#!/bin/sh
# rig-managed post-commit — graceful dev daemon auto-reload.
# GENERATED by rig (internal_dev.auto_reload_on_commit). Do NOT edit by hand; `rig apply`
# reconciles this file. See docs/config-schema.md#internal_dev.
# {marker}
#
# On a commit that touches a configured daemon-source path, run the graceful reload command so
# the running daemon picks up the new code with no manual restart. Runs AFTER the commit is
# recorded (post-commit) and NEVER fails the commit.
set -eu
# -f: disable shell pathname expansion. Without it, `for p in $patterns` (below) expands each
# glob AGAINST THE WORKING TREE before `case` ever sees it — e.g. `src/daemon/*` becomes the
# literal on-disk entries `src/daemon/loop.ts src/daemon/sub` (and `*` does NOT span `/` in
# shell pathname expansion, unlike in `case`), so a nested changed file never matches. `case`
# does its OWN glob matching on `$p`, which is what this hook actually relies on.
set -f

# The daemon-source path patterns (POSIX `case` globs; a `*` spans `/`). A commit whose changed
# files match ANY pattern triggers the reload.
patterns={patterns}
reload_command={reload_command}

# 1. which files did THIS commit touch? --root: a parentless (repo-initial) commit otherwise
# prints nothing from plain `diff-tree HEAD`, so the very first commit could never reload.
changed="$(git diff-tree --no-commit-id --name-only -r --root HEAD 2>/dev/null || true)"
[ -n "$changed" ] || exit 0

# 2. does any changed file match a daemon-source pattern?
matched=0
oldifs="$IFS"
IFS='
'
for f in $changed; do
  [ -n "$f" ] || continue
  for p in $patterns; do
    [ -n "$p" ] || continue
    case "$f" in
      $p) matched=1 ;;
    esac
    [ "$matched" -eq 1 ] && break
  done
  [ "$matched" -eq 1 ] && break
done
IFS="$oldifs"
[ "$matched" -eq 1 ] || exit 0

# 3. dry-run gate — tests / CI never fire a real reload.
if [ -n "${{{dry_env}:-}}" ]; then
  echo "rig dev-reload: {dry_env} set — would run: $reload_command" >&2
  exit 0
fi

# 4. the reload command must be available (no-op with a note when it is not on PATH).
cmd_name="${{reload_command%% *}}"
if ! command -v "$cmd_name" >/dev/null 2>&1; then
  echo "rig dev-reload: '$cmd_name' not on PATH — skipping graceful reload" >&2
  exit 0
fi

# 5. graceful reload — never fail the commit (post-commit is informational).
echo "rig dev-reload: daemon source changed — running: $reload_command" >&2
$reload_command || echo "rig dev-reload: '$reload_command' failed (non-fatal)" >&2
exit 0
"""


_COMPOSER_TEMPLATE = """\
#!/bin/sh
# rig-managed post-commit COMPOSER (core.hooksPath = this dir).
# GENERATED by rig. Do NOT edit by hand.
# {marker}
#
# A global core.hooksPath shadows each repo's own .git/hooks/post-commit, and the agent-tools
# dispatcher composer ships NO post-commit — so without this, repo-local post-commit hooks never
# fire. This generic trampoline restores them: run the shadowed repo-local hook, then the
# global-hooks.d/post-commit dispatcher fragments. post-commit is informational (git ignores the
# exit status), so it never blocks and swallows a child failure.
# --git-common-dir (not --absolute-git-dir): hooks are shared across a repo's worktrees, living
# in the COMMON dir — a linked worktree's own private git-dir is the wrong place to look.
git_dir="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || exit 0
[ -n "$git_dir" ] || exit 0
HOOK_DIR="$(dirname "$0")"

local_hook="$git_dir/hooks/post-commit"
if [ -x "$local_hook" ] && [ "$local_hook" != "$0" ]; then
  "$local_hook" "$@" || true
fi

if [ -x "$HOOK_DIR/../run-global-hooks" ]; then
  "$HOOK_DIR/../run-global-hooks" post-commit "$@" || true
fi
exit 0
"""
