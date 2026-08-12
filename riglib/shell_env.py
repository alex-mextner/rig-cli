"""Shell environment variables — rig-managed exported vars, GLOBAL (machine-wide).

What this is
------------
Some vars (``COLORTERM``, a proxy, a tool flag) need to be visible to EVERY shell
invocation on the machine — not just an interactive login shell, but also a
mosh/SSH non-interactive command shell (``ssh host 'some-command'``), a cron/launchd
job, or ``zsh -l -c '...'``. zsh's own startup order makes ``~/.zshenv`` the only file
sourced unconditionally in every mode (login or not, interactive or not) — ``~/.zshrc``
is interactive-only and ``~/.zprofile``/``~/.zlogin`` are login-only — so that is the
default ``rc_path`` here. A hand-written ``~/.zshenv`` commonly carries a comment to
this effect for Homebrew's own PATH setup: "mosh/SSH non-interactive command shells
only source .zshenv".

Two artifacts, mirroring ``riglib.tmux``'s "own a generated file, splice one import
line" shape (the SAME idiom, deliberately — see ``docs/config-schema.md#env``):

- ``<generated_dir>/rig.env.sh`` — the rig-owned file (wholesale rewrite each apply),
  one ``export KEY=value`` line per ``vars`` entry, sorted by key for a stable diff.
- ``rc_path`` — carries exactly ONE ``source '<generated file>'`` line, appended if
  absent; every other line already in the file (a user's own Homebrew/cargo/bun setup,
  say) is left untouched. Unlike tmux, there is no dual import/block mode and no
  neutralization of unrelated inline content — a single exported var has no equivalent
  of tmux's plugin/continuum ``@`` declarations that need detecting and superseding.

All rendering here is stdlib-only + effect-free; the effectful write lives in
``actions/runner.py`` (``_do_provision_env``), and ``drift.py`` (``_check_env``) diffs
the desired artifacts against disk — the same three-consumer split as ``riglib.tmux``.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_RC_PATH = "~/.zshenv"
DEFAULT_GENERATED_DIR = "~/.config/rig/env"
GENERATED_FILE_NAME = "rig.env.sh"

# A POSIX shell identifier. The ONE canonical definition — ``config.py`` imports THIS constant
# (rather than keeping its own byte-identical copy, review finding: two copies risk drifting
# apart) for its `config.validate`-time rejection; `render_env_file` below re-asserts it again
# on every key at RENDER time — the defense-in-depth re-check that matters is WHERE the check
# runs (this module is the one that actually writes the unquoted key into executable shell
# text, so its own safety must not depend on every caller having gone through
# `config.validate` first), not where the regex literal lives.
ENV_VAR_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_HEADER = (
    "# rig-managed shell environment — GENERATED, do not hand-edit; `rig apply` rewrites\n"
    "# this file wholesale. Edit the `env:` block in ~/.config/rig/config.yaml instead (this\n"
    "# is GLOBAL, machine-wide config — not a per-repo rig.yaml), then re-apply. (rig owns\n"
    "# this file; your shell rc sources it.)\n"
)


@dataclass(frozen=True)
class ShellEnvPlan:
    """The desired shell-env-managed state, fully resolved. Pure data, no I/O.

    Unlike ``riglib.tmux.TmuxPlan`` (which keeps ``home`` because several of its artifact
    paths — resurrect/plugins dirs — are HOME-anchored independently of ``rc_path``/
    ``generated_dir``), this plan has no artifact that needs HOME after ``rc_path`` and
    ``generated_dir`` are already resolved, so there is no ``home`` field to keep in sync.
    """

    rc_path: Path
    generated_dir: Path
    vars: dict[str, str] = field(default_factory=dict)

    @property
    def generated_file_path(self) -> Path:
        return self.generated_dir / GENERATED_FILE_NAME

    def render_env_file(self) -> str:
        """The rig-owned generated file body — one ``export KEY=value`` per var.

        Keys are sorted for a deterministic, diff-friendly render (``rig apply``/``rig
        status`` must agree byte-for-byte regardless of dict insertion order). Values are
        shell-quoted (``shlex.quote``) so a value containing spaces/quotes/``$`` is safe —
        the generated file is ``source``d as a real shell script, not just read as text.

        Keys are interpolated UNQUOTED (a shell identifier needs no quoting) — ``key`` is
        therefore checked against ``ENV_VAR_KEY_PATTERN`` FIRST. ``config.validate`` already
        rejects an invalid key before a plan is ever built, but this is the module that
        actually emits executable shell text, so it re-asserts its own invariant rather than
        trusting every possible caller (defense in depth, per review) — raises ``ValueError``
        rather than silently emitting a key that could inject arbitrary shell.
        """
        lines = [_HEADER.rstrip("\n"), ""]
        for key in sorted(self.vars):
            if not ENV_VAR_KEY_PATTERN.match(key):
                raise ValueError(
                    f"env var key {key!r} is not a valid shell identifier "
                    "(refusing to interpolate it unquoted into rig.env.sh)"
                )
            lines.append(f"export {key}={shlex.quote(self.vars[key])}")
        lines.append("")  # trailing newline
        return "\n".join(lines)

    def import_line(self) -> str:
        """The single ``source <generated file>`` line ``rc_path`` carries.

        ``shlex.quote``d — NOT hand-placed single quotes. ``generated_dir`` (and HOME) may
        contain a space, and for an ordinary path ``shlex.quote`` renders it bare (no quoting
        needed); for anything containing a space it wraps in single quotes; for a path that
        itself contains a single quote (an edge case, but ``generated_dir`` is configurable —
        including, per review, from a repo's own committed ``rig.yaml``) hand-placed quotes
        would let it break OUT of the quoting and inject arbitrary shell text into the most
        privileged startup file on the machine. ``shlex.quote`` handles every case correctly;
        hand-placed quoting does not (review finding — this diverges from ``riglib.tmux``'s
        ``TmuxPlan.import_line``, which still hand-quotes and shares the same latent hole,
        out of scope to fix here).
        """
        return f"source {shlex.quote(str(self.generated_file_path))}"


def build_shell_env(
    *,
    repo_home: Path,
    rc_path: str | Path = DEFAULT_RC_PATH,
    generated_dir: str | Path = DEFAULT_GENERATED_DIR,
    vars: dict | None = None,
) -> ShellEnvPlan:
    """Resolve the desired :class:`ShellEnvPlan` from the (already-validated) ``env`` config block.

    ``repo_home`` is the resolved HOME (the caller passes ``Path.home()`` or a test tmp HOME).
    HOME-relative ``rc_path``/``generated_dir`` are expanded against it — a bare ``~``/``~/...``
    expansion ONLY, deliberately NOT ``$XDG_CONFIG_HOME``-aware like plan.py's own
    ``expand_user_path`` (an earlier version tried to match that special case here too, so a
    caller falling back to bare defaults would resolve identically to the plan builder — but
    that made ``repo_home`` no longer fully control resolution, since the AMBIENT
    ``XDG_CONFIG_HOME`` env var would silently override it even in a pure unit test explicitly
    passing an unrelated ``repo_home``; reverted). The plan builder (``_build_env`` in
    ``riglib/plan.py``) is the ONE place that resolves ``~/.config`` -> ``$XDG_CONFIG_HOME`` and
    bakes the ABSOLUTE result into ``Action.options`` — see ``env_plan_from_action`` in
    ``riglib/actions/runner.py``, which REQUIRES those options rather than re-deriving a default
    here, so there is no second resolver to disagree with the first (review finding).

    An empty/absent ``vars`` mapping yields a plan that renders a header-only generated file and
    a harmless import line — never an error (mirrors ``tmux.build_tmux``'s "empty block -> safe
    defaults").
    """
    vars = vars or {}

    def _expand(p: str | Path) -> Path:
        s = str(p)
        if s == "~":
            return repo_home
        if s.startswith("~/"):
            return repo_home / s[2:]
        return Path(s)

    return ShellEnvPlan(
        rc_path=_expand(rc_path),
        generated_dir=_expand(generated_dir),
        vars={str(k): str(v) for k, v in vars.items()},
    )


def is_rig_env_import_line(line: str, import_line: str, generated_name: str) -> bool:
    """True if ``line`` is rig's OWN ``source <generated file>`` import (current, or a stale one
    pointing at an old ``generated_dir``) — so it can be recognized/dropped. A comment or an
    unrelated line that merely mentions the path is NOT matched (the line must actually BE a
    ``source``/``.`` directive naming the generated file).

    Parsed with ``shlex.split(..., comments=True)`` — real shell tokenizing, not a naive
    ``.split()`` + quote-strip — so a QUOTED path (containing a space, or even a quote
    character; see :meth:`ShellEnvPlan.import_line`) is parsed correctly, and a trailing
    comment (``source '.../rig.env.sh'  # rig``) does not fool the argument extraction into
    treating the comment text as part of the path (review finding: the naive parser saw the
    comment as part of the arg, so `Path(arg).name` was never the bare generated filename and
    a stale line with a trailing comment silently survived every cleanup pass forever).

    KNOWN LIMITATION (accepted, matches rig's own model — mirrors the identical basename/suffix
    tradeoff `riglib.tmux` already accepts for its own plugin-init matching): the match is on
    ``generated_name`` (the bare filename, ``rig.env.sh``) ALONE, not the full ``generated_dir``
    path. A user's own, unrelated ``source ~/mystuff/rig.env.sh`` — a coincidentally same-named
    file rig never wrote — would be (mis)classified as a stale rig import and dropped. Given the
    filename is rig-specific and the collision requires a user to independently choose that
    exact name, this is treated as an acceptable, documented tradeoff rather than a bug to chase
    (review, round 8) — narrowing the match to `generated_dir`-relative paths would also then
    fail to recognize genuinely stale lines from an OLD `generated_dir` (the case this function
    exists to catch in the first place), which is the more common and more consequential miss.
    """
    s = line.strip()
    if s == import_line:
        return True
    if not s or s.startswith("#"):
        return False
    try:
        parts = shlex.split(s, comments=True)
    except ValueError:
        return False  # unbalanced quote or similar malformed shell syntax — not our line
    if len(parts) == 2 and parts[0] in ("source", "."):
        return Path(parts[1]).name == generated_name
    return False


def desired_rc_text(existing: str, plan: ShellEnvPlan) -> str:
    """The desired ``rc_path`` text — POSITION-TOLERANT, and byte-preserving of every line
    rig does not own.

    If the CURRENT import line already appears anywhere in ``existing`` (not necessarily at
    the end — a user may have moved it above their own exports on purpose, e.g. so their own
    values win), that line's POSITION is left exactly where the user put it. Unlike tmux's
    import-line splice — which always re-appends at the very end, because tmux's ordering
    guarantee genuinely depends on position (continuum's ``run-shell`` must be LAST) — a plain
    exported var has no such ordering hazard, so rig has no reason to fight the user's
    placement (review: an earlier end-anchoring version silently moved a user-relocated line
    back on every apply, forever, with no way to opt out).

    A STALE copy (an old ``generated_dir``) is ALWAYS dropped, even when the current line is
    ALSO already present elsewhere — position-tolerance for the current line must not become an
    excuse to leave an orphaned old ``rig.env.sh`` silently sourced forever (review finding: an
    earlier version returned ``existing`` verbatim the moment ANY current-line match was found,
    before the stale-drop ran, so a stale-plus-current combination never got cleaned up).

    When the current line is absent everywhere (first apply, or only a stale copy existed),
    the current import is appended once at the end — and every OTHER line is preserved with its
    ORIGINAL bytes: ``splitlines(keepends=True)`` keeps each line's own terminator (so a CRLF
    file stays CRLF, and a trailing blank line survives), rather than a lossy
    split-on-``\\n``-then-rejoin-with-``\\n`` round-trip that would silently convert line
    endings and drop a trailing blank line (review finding).

    Pure + idempotent: calling this again on its own output is a no-op.
    """
    import_line = plan.import_line()
    generated_name = plan.generated_file_path.name
    lines = existing.splitlines(keepends=True) if existing else []
    current_present = any(ln.strip() == import_line for ln in lines)

    if current_present:
        # keep the current line's position; drop only a coexisting STALE copy, if any.
        kept = [
            ln for ln in lines
            if ln.strip() == import_line
            or not is_rig_env_import_line(ln, import_line, generated_name)
        ]
        return "".join(kept)

    kept = [ln for ln in lines if not is_rig_env_import_line(ln, import_line, generated_name)]
    body = "".join(kept)
    if body and not body.endswith(("\n", "\r")):
        body += "\n"
    return body + import_line + "\n"
