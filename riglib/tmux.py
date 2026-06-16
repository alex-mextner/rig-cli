"""tmux configuration — PURE planning of the rig-managed tmux artifacts.

What this is
------------
rig MANAGES tmux config declaratively from ``rig.yaml`` (CTO direction, ROADMAP §5b),
MIGRATING an existing hand-written ``~/.tmux.conf`` instead of clobbering it. This module is
the analog of :mod:`riglib.schedule`: it is **stdlib-only and effect-free** — it computes
WHAT the artifacts should be (the generated ``rig.tmux.conf`` text, the two cc-save/cc-restore
scripts, the boot launchd plist, the ``~/.tmux.conf`` import line / managed-block splice). The
effectful writes live in ``actions/runner.py`` (the one-engine apply path), and drift detection
diffs the desired artifact against disk in ``drift.py``. Three consumers (plan, apply, drift)
share THIS so the desired state never drifts between them.

How it is reached
-----------------
``plan._build_tmux`` reads the ``tmux:`` config block, calls :func:`build_tmux` to resolve a
:class:`TmuxPlan`, and emits ``provision_tmux`` actions. ``runner._do_provision_tmux`` renders
the artifacts from the same plan and writes them. ``drift._check_tmux`` re-renders and diffs.

Invariants
----------
- **Ordering is GUARANTEED.** continuum's ``run-shell …/continuum.tmux`` init is emitted LAST
  among plugin inits, AFTER resurrect's init AND after the Moshi ``status-right`` tweak.
- **The Moshi tweak is opt-in** (``tmux.moshi.enabled``) and, when on, is emitted BEFORE
  continuum init so it can never wipe continuum's autosave hook.
- **rig only ever rewrites ITS OWN region** — the generated ``rig.tmux.conf`` wholesale (import
  mode) or the text between the sentinel markers (block mode). User-written lines are untouched.

Past bug this fixes
-------------------
The machine's stale-session-on-reboot bug: a hand-written ``~/.tmux.conf`` ran
``if-shell '[ -n "$MOSHI_CLIENT" ]' { set -g status-right '' }`` at the END of the file —
AFTER ``run-shell …/continuum.tmux``. tmux-continuum's autosave timer lives in ``status-right``,
so the Moshi tweak wiped it → continuum silently stopped saving → a reboot restored a
weeks-stale session. Generating the region lets rig pin the order and end the bug.
"""

from __future__ import annotations

import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

# The managed-block sentinels (fallback "block" apply mode). rig replaces ONLY the text
# between these markers — conda-init style — so a re-apply never disturbs the user's lines.
BLOCK_BEGIN = "# === rig-managed (tmux) BEGIN ==="
BLOCK_END = "# === rig-managed (tmux) END ==="

# The default resurrect process list. NOTE: ``claude`` is deliberately NOT here when cc_restore
# is on — resurrect would restart the pane as a bare ``claude`` (a NEW default session) BEFORE
# the cc-restore hook runs, and cc-restore (which only resumes a FRESH SHELL, never clobbering a
# running claude) would then skip it, leaving the wrong session. So cc-restore owns the exact
# ``claude --resume <id>`` and resurrect just brings the SHELL back. ``claude`` is added to this
# list ONLY when cc_restore is OFF (the user opted out of exact-id resume → resurrect's own
# best-effort bare-``claude`` restore is the documented fallback). The rest are common dev
# processes; ``~`` prefixes tell resurrect to match a process whose name merely CONTAINS the
# token (resurrect's tilde convention).
DEFAULT_RESURRECT_PROCESSES = ["ssh", "psql", "mysql", "sqlite3"]

# continuum's default autosave cadence (minutes). 15 is continuum's own default; we make it
# explicit so a re-apply pins it rather than relying on the plugin default drifting.
DEFAULT_SAVE_INTERVAL = 15

# The canonical single-session name for the anti-sprawl attach-or-create entry. A reconnect
# re-attaches THIS session instead of spawning a duplicate.
DEFAULT_SESSION = "main"

# The boot launchd label (macOS). Reverse-DNS per Apple convention; one identity for the
# plist Label + filename stem so install/drift/remove all key off it. Distinct from the
# model-freshness label so the two agents never collide.
DEFAULT_BOOT_LABEL = "ai.hyperide.tmux-boot"

# Default install locations (HOME-relative; resolved per-machine at apply time).
DEFAULT_CONF_PATH = "~/.tmux.conf"
DEFAULT_GENERATED_DIR = "~/.config/rig/tmux"

# The default apply mechanism: import (a single source-file line in ~/.tmux.conf) is preferred
# over the managed-block splice. Lives here with every other tmux default.
DEFAULT_APPLY_MODE = "import"

# The two managed cc scripts' basenames (live in the generated dir).
CC_SAVE_NAME = "cc-save.sh"
CC_RESTORE_NAME = "cc-restore.sh"
CC_MAP_NAME = "cc-sessions.map"
ATTACH_NAME = "tmux-attach.sh"
RIG_CONF_NAME = "rig.tmux.conf"

# The base backup suffix for the migrated ~/.tmux.conf. The runner writes a UNIQUE timestamped
# backup (`.rig-bak-<UTC>`) on every migrating apply (see runner._timestamped_backup_path), so a
# later apply after a hand-edit keeps its OWN restore point instead of skipping — no state lost.
BACKUP_SUFFIX = ".rig-bak"


@dataclass(frozen=True)
class TmuxPlan:
    """The desired tmux-managed state, fully resolved. Pure data, no I/O.

    ``apply_mode`` is "import" (preferred — ``~/.tmux.conf`` sources the generated file) or
    "block" (fallback — a sentinel-fenced managed block spliced into ``~/.tmux.conf``).
    """

    home: Path
    apply_mode: str  # "import" | "block"
    conf_path: Path  # ~/.tmux.conf
    generated_dir: Path  # ~/.config/rig/tmux
    resurrect_processes: list[str]
    capture_pane_contents: bool
    continuum_restore: bool
    continuum_boot: bool
    save_interval: int
    moshi_enabled: bool
    cc_restore_enabled: bool
    anti_sprawl_enabled: bool
    anti_sprawl_session: str
    boot_enabled: bool
    boot_label: str

    # ── resolved artifact paths ──────────────────────────────────────────────────────
    @property
    def rig_conf_path(self) -> Path:
        return self.generated_dir / RIG_CONF_NAME

    @property
    def cc_save_path(self) -> Path:
        return self.generated_dir / CC_SAVE_NAME

    @property
    def cc_restore_path(self) -> Path:
        return self.generated_dir / CC_RESTORE_NAME

    @property
    def cc_map_path(self) -> Path:
        return self.generated_dir / CC_MAP_NAME

    @property
    def attach_path(self) -> Path:
        return self.generated_dir / ATTACH_NAME

    def managed_scripts(self) -> list[tuple[Path, str]]:
        """The (path, body) pairs apply writes and drift checks — ONE source so they can't
        diverge: cc-save + cc-restore always, the attach-or-create entry when anti-sprawl is on.
        """
        scripts = [
            (self.cc_save_path, self.render_cc_save()),
            (self.cc_restore_path, self.render_cc_restore()),
        ]
        if self.anti_sprawl_enabled:
            scripts.append((self.attach_path, self.render_attach_script()))
        return scripts

    @property
    def boot_plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{self.boot_label}.plist"

    @property
    def backup_path(self) -> Path:
        return self.conf_path.with_name(self.conf_path.name + BACKUP_SUFFIX)

    # ── the generated tmux config body ───────────────────────────────────────────────
    def render_rig_conf(self) -> str:
        """The rig-owned ``rig.tmux.conf`` — generated with GUARANTEED ordering.

        Section order is load-bearing:
          1. plugin DECLARATIONS (tpm + resurrect + continuum)
          2. resurrect/continuum OPTIONS (processes incl. claude, capture-pane, restore,
             save-interval, boot)
          3. cc-restore resurrect HOOKS (post-save / post-restore → the managed scripts)
          4. the Moshi ``status-right`` tweak (opt-in) — BEFORE any plugin init
          5. resurrect ``run-shell`` init
          6. continuum ``run-shell`` init  ← LAST, so nothing after it wipes its hook
          7. tpm init (must be the very last line per tpm's own contract)
        """
        # cc-restore owns the exact `claude --resume <id>` (resurrect brings the shell back), so
        # `claude` goes in @resurrect-processes ONLY when cc-restore is OFF (fallback to
        # resurrect's own bare-claude restore). If the user EXPLICITLY listed `claude` in
        # tmux.resurrect.processes, honor that (their choice), even with cc-restore on.
        procs_list = list(self.resurrect_processes)
        if not self.cc_restore_enabled and "claude" not in procs_list:
            procs_list = ["claude", *procs_list]
        procs = " ".join(_resurrect_token(p) for p in procs_list)
        out: list[str] = [
            "# rig-managed tmux configuration — GENERATED from rig.yaml. Do not hand-edit;",
            "# `rig apply` rewrites this file wholesale. Edit the `tmux:` block in rig.yaml",
            "# instead, then re-apply. (rig owns this file; your ~/.tmux.conf sources it.)",
            "",
            "# ── plugins (tpm + resurrect + continuum) ─────────────────────────────────",
            "set -g @plugin 'tmux-plugins/tpm'",
            "set -g @plugin 'tmux-plugins/tmux-resurrect'",
            "set -g @plugin 'tmux-plugins/tmux-continuum'",
            "",
            "# ── resurrect / continuum options ─────────────────────────────────────────",
            f"set -g @resurrect-processes '{procs}'",
        ]
        # Emit the EXPLICIT on/off for every modeled boolean — never just-omit-when-false. A
        # migrated conf may still carry a live inline `@continuum-restore 'on'`; since the
        # generated file is sourced AFTER it, an explicit `'off'` here is what actually disables
        # the option (omitting it would let the stale inline `'on'` win, and drift wouldn't see it).
        out.append(f"set -g @resurrect-capture-pane-contents '{'on' if self.capture_pane_contents else 'off'}'")
        out.append(f"set -g @continuum-restore '{'on' if self.continuum_restore else 'off'}'")
        out.append(f"set -g @continuum-save-interval '{self.save_interval}'")
        # @continuum-boot is emitted ONLY as the on/off that matches rig's OWN boot mechanism.
        # CRITICAL: `@continuum-boot 'on'` makes continuum install its OWN unmanaged boot artifact
        # (the iTerm-coupled `Tmux.Start.plist` on macOS / a systemd user unit on Linux) — exactly
        # the mechanism rig's launchd agent REPLACES. So when rig manages boot (`boot.enabled`),
        # we keep continuum's boot OFF (rig's agent brings tmux up; `@continuum-restore` restores
        # into it) — never letting continuum write a SECOND, untracked boot path. When `boot` is
        # disabled, it's likewise off (the user owns boot). The legacy `continuum.boot` knob is a
        # NO-OP for this reason (documented); rig's launchd agent is the single boot path.
        out.append("set -g @continuum-boot 'off'")

        if self.cc_restore_enabled:
            # The hook VALUE is exec'd by resurrect as a SHELL command, so the script path needs
            # SHELL quoting (shlex.quote) inside it — a generated_dir/HOME with a space would
            # otherwise be split and the hook would never run. We then wrap that in a tmux
            # double-quoted option value. `shlex.quote` of a space path yields '…/cc-save.sh'
            # (single-quoted), safe inside the tmux "…" value.
            save_cmd = shlex.quote(str(self.cc_save_path))
            restore_cmd = shlex.quote(str(self.cc_restore_path))
            out += [
                "",
                "# ── cc-restore: per-window Claude Code resume by exact session id ──────────",
                "# resurrect fires these AFTER it saves / restores; the managed scripts record",
                "# each claude pane's cwd→session-id map and relaunch `claude --resume <id>`.",
                f'set -g @resurrect-hook-post-save-all "{save_cmd}"',
                f'set -g @resurrect-hook-post-restore-all "{restore_cmd}"',
            ]

        if self.moshi_enabled:
            out += [
                "",
                "# ── Moshi (iOS client) tweaks — opt-in; placed BEFORE continuum init ───────",
                "# Clearing status-right keeps the Moshi app's swipe-to-change-window gesture",
                "# working. CRITICAL: this runs BEFORE continuum's run-shell init below, so",
                "# continuum's autosave hook (which it installs INTO status-right) is NOT wiped",
                "# — the root-cause fix for the stale-session-on-reboot bug.",
                "set-option -ga update-environment ' MOSHI_CLIENT'",
                "if-shell '[ -n \"$MOSHI_CLIENT\" ]' {",
                "  set -g status-left ''",
                "  set -g status-right ''",
                "}",
            ]

        out += [
            "",
            "# ── plugin init (ORDER MATTERS: resurrect, then continuum LAST) ────────────",
            "run-shell ~/.tmux/plugins/tmux-resurrect/resurrect.tmux",
            "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux",
            "",
            "# tpm init — must be the very last line (tpm's own contract).",
            "run '~/.tmux/plugins/tpm/tpm'",
            "",
        ]
        return "\n".join(out)

    # ── the import line spliced into ~/.tmux.conf (import mode) ───────────────────────
    def import_line(self) -> str:
        """The single ``source-file`` line ``~/.tmux.conf`` carries in import mode.

        The path is single-quoted: ``generated_dir`` (and HOME) may contain a space, and tmux
        would otherwise parse ``source-file /Users/A B/…`` as multiple arguments and not source
        the file. tmux treats single quotes as literal grouping, so this is safe for any path.
        """
        return f"source-file '{self.rig_conf_path}'"

    # ── the cc-save / cc-restore managed scripts ─────────────────────────────────────
    def render_cc_save(self) -> str:
        """cc-save: record each claude pane's cwd → newest Claude Code session id for that cwd.

        Encoding (VERIFIED on a real machine, see module/test docs): the projects dir name is
        the cwd with every ``/`` AND ``.`` replaced by ``-`` (so ``/Users/u/.files`` →
        ``-Users-u--files``). The newest ``*.jsonl`` (by mtime) under that dir is the latest
        session id. The map (window/pane → cwd → session_id) is read back by cc-restore.

        Known limitation (per-cwd, not strictly per-pane): the Claude Code session id is not
        exposed per tmux pane, so two ``claude`` panes in the SAME cwd both map to that cwd's
        newest session id — cc-restore would resume both into it. Per-window exact resume holds
        when each claude pane is in a distinct cwd (the common case); same-cwd panes share the
        newest session. Documented in docs/config-schema.md#tmux.
        """
        map_file = self.cc_map_path
        projects = "$HOME/.claude/projects"
        return f"""#!/usr/bin/env bash
# rig-managed: cc-save — GENERATED by rig from rig.yaml. Do not hand-edit.
# Records, for every tmux pane currently running `claude`, a map line:
#   <session>:<window>.<pane><TAB><cwd><TAB><claude-session-id>
# so cc-restore can relaunch `claude --resume <id>` in the right window after a reboot.
# Encoding: the ~/.claude/projects/<enc> dir name is the pane cwd with every '/' and '.'
# replaced by '-' (verified against real on-disk dirs).
# Limitation: the session id is per-CWD (newest jsonl), not strictly per-pane — two claude
# panes in the same cwd share that cwd's newest session id.
set -euo pipefail

MAP_FILE="{map_file}"
PROJECTS="{projects}"

encode_cwd() {{
  # every '/' AND '.' -> '-'  (tr maps both classes to a single dash)
  printf '%s' "$1" | tr './' '--'
}}

newest_session_id() {{
  # newest *.jsonl (by mtime) under the encoded projects dir -> its basename without .jsonl.
  # NB: no `ls -t … | head -n1` — under `set -o pipefail` head can close the pipe early and the
  # SIGPIPE'd ls fails the whole pipeline, silently dropping the pane. Take the first line of the
  # captured ls output instead (no pipe), so a long file list never SIGPIPEs.
  local dir="$1" listing newest
  [ -d "$dir" ] || return 1
  listing=$(ls -t "$dir"/*.jsonl 2>/dev/null) || return 1
  newest=${{listing%%$'\\n'*}}   # first line = newest by mtime
  [ -n "$newest" ] || return 1
  basename "$newest" .jsonl
}}

: > "$MAP_FILE"
# iterate every pane; emit a map line only for panes whose command is `claude`.
tmux list-panes -a -F '#{{session_name}}:#{{window_index}}.#{{pane_index}}	#{{pane_current_command}}	#{{pane_current_path}}' \\
  | while IFS=$'\\t' read -r addr cmd cwd; do
      case "$cmd" in
        claude|*/claude) ;;
        *) continue ;;
      esac
      enc=$(encode_cwd "$cwd")
      sid=$(newest_session_id "$PROJECTS/$enc") || continue
      printf '%s\\t%s\\t%s\\n' "$addr" "$cwd" "$sid" >> "$MAP_FILE"
    done
"""

    def render_cc_restore(self) -> str:
        """cc-restore: after a reboot, resume each mapped window's Claude Code session.

        For each map line, if the target pane is a FRESH shell (its current command is a
        shell, not already `claude`), send ``claude --resume <id>``. A stale/missing id falls
        back to ``claude --continue`` (most-recent session in that cwd) so a reboot is never
        left with a dead pane; a pane already running claude is skipped (never clobbered).
        """
        map_file = self.cc_map_path
        projects = "$HOME/.claude/projects"
        return f"""#!/usr/bin/env bash
# rig-managed: cc-restore — GENERATED by rig from rig.yaml. Do not hand-edit.
# After resurrect/continuum restores the windows, relaunch Claude Code per mapped window by
# its EXACT recorded session id (`claude --resume <id>`). Only into a FRESH shell pane —
# never on top of a running claude. Stale/missing id -> fall back to `claude --continue`.
set -euo pipefail

MAP_FILE="{map_file}"
PROJECTS="{projects}"
[ -f "$MAP_FILE" ] || exit 0

encode_cwd() {{ printf '%s' "$1" | tr './' '--'; }}

id_is_live() {{
  # the recorded id still has a session file under the cwd's encoded projects dir.
  local cwd="$1" sid="$2"
  [ -f "$PROJECTS/$(encode_cwd "$cwd")/$sid.jsonl" ]
}}

while IFS=$'\\t' read -r addr cwd sid; do
  [ -n "$addr" ] || continue
  # The target pane must still EXIST (resurrect may not have recreated it) — a missing -t would
  # make send-keys fail and, under `set -e`, abort the whole restore. display-message returns
  # empty for a missing pane (the `|| true` keeps set -e happy); an empty cur → skip the entry.
  cur=$(tmux display-message -p -t "$addr" '#{{pane_current_command}}' 2>/dev/null || true)
  [ -n "$cur" ] || continue
  # Resume ONLY into a fresh SHELL pane: skip a pane already running claude (don't clobber the
  # session) AND skip any non-shell command (vim/less/…) so we never type into someone's editor.
  case "$cur" in
    sh|bash|zsh|fish|dash|ksh|tcsh|-sh|-bash|-zsh) ;;   # a fresh login/interactive shell → ok
    *) continue ;;                                       # claude, an editor, a build, … → leave it
  esac
  # shell-quote the runtime values before they enter the send-keys command line: a cwd with a
  # space or a metacharacter (or a tampered map) must NEVER break or inject into the command.
  qcwd=$(printf '%q' "$cwd")
  qsid=$(printf '%q' "$sid")
  if id_is_live "$cwd" "$sid"; then
    tmux send-keys -t "$addr" "cd $qcwd && claude --resume $qsid" Enter
  else
    # stale/missing id: resume the most-recent session in that cwd instead of crashing.
    tmux send-keys -t "$addr" "cd $qcwd && claude --continue" Enter
  fi
done < "$MAP_FILE"
"""

    # ── anti-sprawl attach-or-create entry ───────────────────────────────────────────
    def render_attach_script(self) -> str:
        """An attach-or-create entry: re-attach the ONE canonical session, never duplicate.

        The machine hit a duplicate "session 3" because a Moshi/iTerm reconnect ran a bare
        ``tmux`` (which spawns a NEW session) instead of attaching the existing one. Sourcing
        this from the login shell (documented, not auto-wired — rig never edits the user's
        shell rc) makes a reconnect deterministic: attach ``<session>`` if it exists, else
        create it. One canonical session, no sprawl.
        """
        # shell-quote the configured session name at generation time so a name with a space or
        # a shell metacharacter can never break or inject into the generated script.
        s = shlex.quote(self.anti_sprawl_session)
        return f"""#!/usr/bin/env bash
# rig-managed: tmux attach-or-create — GENERATED by rig from rig.yaml. Do not hand-edit.
# Re-attach the ONE canonical session instead of spawning a duplicate (anti-sprawl). Wire it
# from your login shell, e.g.  [ -z "$TMUX" ] && exec ~/.config/rig/tmux/tmux-attach.sh
set -euo pipefail
SESSION={s}
if tmux has-session -t "$SESSION" 2>/dev/null; then
  exec tmux attach-session -t "$SESSION"
else
  exec tmux new-session -s "$SESSION"
fi
"""

    # ── the boot launchd plist (macOS) ───────────────────────────────────────────────
    def render_boot_plist(self) -> str:
        """A launchd agent that brings tmux up at login so continuum can restore.

        Less iTerm-coupled than the old ``osx_iterm_start_tmux.sh`` approach: it simply starts
        a detached tmux server (``tmux start-server``) at load; continuum (``@continuum-boot
        on`` + ``@continuum-restore on``) then restores the saved session into it. ``KeepAlive``
        is false — we only need it to fire once at login.
        """
        tmux_bin = _resolve_tmux_bin()
        # plistlib gives idiomatic, escape-safe XML (no hand-rolled string concat / injection).
        import plistlib

        # Honor a non-default conf path: tmux only auto-loads ~/.tmux.conf, so a custom
        # conf_path must be passed via `-f` or the login server starts WITHOUT the managed
        # config (continuum/resurrect options never set → no restore). Default path needs no -f.
        args = [tmux_bin]
        if self.conf_path != self.home / ".tmux.conf":
            args += ["-f", str(self.conf_path)]
        args.append("start-server")
        payload = {
            "Label": self.boot_label,
            "ProgramArguments": args,
            "RunAtLoad": True,
            "KeepAlive": False,
        }
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML).decode("utf-8")


def _resurrect_token(name: str) -> str:
    """Quote a resurrect process token if it contains whitespace; keep tilde matches as-is.

    resurrect's process list is a single space-separated string; a token with a space (e.g.
    ``~rails server``) must be quoted. ``claude`` and bare names pass through.
    """
    if any(c.isspace() for c in name):
        return f'"{name}"'
    return name


# Common tmux install locations, checked when PATH resolution fails (a non-interactive
# `rig apply` env may have a bare PATH). Ordered: Apple-silicon brew, Intel brew, system.
_TMUX_FALLBACK_PATHS = ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux")


def _resolve_tmux_bin() -> str:
    """The tmux binary path for the boot plist: prefer PATH, else the first existing common
    location (Intel /usr/local, Apple-silicon /opt/homebrew, system /usr/bin) — never a blind
    Apple-silicon hard-code that points at nothing on an Intel/Linux box (codex P2). Falls back
    to the Apple-silicon path only if none exist (so the plist is still well-formed)."""
    found = shutil.which("tmux")
    if found:
        return found
    for cand in _TMUX_FALLBACK_PATHS:
        if Path(cand).exists():
            return cand
    return _TMUX_FALLBACK_PATHS[0]


def build_tmux(
    *,
    repo_home: Path,
    apply_mode: str = "import",
    conf_path: str | Path = DEFAULT_CONF_PATH,
    generated_dir: str | Path = DEFAULT_GENERATED_DIR,
    resurrect: dict | None = None,
    continuum: dict | None = None,
    moshi: dict | None = None,
    cc_restore: dict | None = None,
    anti_sprawl: dict | None = None,
    boot: dict | None = None,
) -> TmuxPlan:
    """Resolve the desired :class:`TmuxPlan` from the (already-validated) tmux config block.

    ``repo_home`` is the resolved HOME (the caller passes ``Path.home()`` or a test tmp HOME).
    HOME-relative ``conf_path`` / ``generated_dir`` are expanded against it. Every nested
    knob defaults to the safe, root-cause-fixing value; an empty block yields the full default
    config (claude in resurrect, capture-pane on, continuum restore+boot, Moshi off, cc-restore
    on, anti-sprawl on, boot on).
    """
    resurrect = resurrect or {}
    continuum = continuum or {}
    moshi = moshi or {}
    cc_restore = cc_restore or {}
    anti_sprawl = anti_sprawl or {}
    boot = boot or {}

    def _expand(p: str | Path) -> Path:
        s = str(p)
        if s == "~":
            return repo_home
        if s.startswith("~/"):
            return repo_home / s[2:]
        return Path(s)

    def _knob(block: dict, key: str, default):
        # A YAML key PRESENT with no value parses as None; for a scalar knob that means "use the
        # default" (so `save_interval:` / `enabled:` with no value can't crash `int(None)` /
        # silently flip a bool). An ABSENT key also uses the default. Any real value is honored.
        v = block.get(key, default)
        return default if v is None else v

    # `processes` is special: an EXPLICIT empty list `[]` is honored (the user cleared matching),
    # but a null / absent key falls back to the default. So distinguish None from [].
    procs = resurrect.get("processes", DEFAULT_RESURRECT_PROCESSES)
    if procs is None:
        procs = DEFAULT_RESURRECT_PROCESSES

    return TmuxPlan(
        home=repo_home,
        apply_mode=apply_mode,
        conf_path=_expand(conf_path),
        generated_dir=_expand(generated_dir),
        resurrect_processes=list(procs),
        capture_pane_contents=bool(_knob(resurrect, "capture_pane_contents", True)),
        continuum_restore=bool(_knob(continuum, "restore", True)),
        continuum_boot=bool(_knob(continuum, "boot", True)),
        save_interval=int(_knob(continuum, "save_interval", DEFAULT_SAVE_INTERVAL)),
        moshi_enabled=bool(_knob(moshi, "enabled", False)),
        cc_restore_enabled=bool(_knob(cc_restore, "enabled", True)),
        anti_sprawl_enabled=bool(_knob(anti_sprawl, "enabled", True)),
        anti_sprawl_session=str(_knob(anti_sprawl, "session", DEFAULT_SESSION)),
        boot_enabled=bool(_knob(boot, "enabled", True)),
        boot_label=str(_knob(boot, "label", DEFAULT_BOOT_LABEL)),
    )


# ── pure ~/.tmux.conf splicing (block apply mode) ─────────────────────────────────────────
def splice_managed_block(original: str, body: str) -> str:
    """Replace ONLY the text between the sentinel markers in ``original`` with ``body``.

    - block absent → append a fresh sentinel-fenced block (user lines untouched).
    - block present → replace its inner body in place, keeping every line before AND after the
      markers exactly as the user has them (conda-init style). Idempotent on identical body.
    """
    block = f"{BLOCK_BEGIN}\n{body}\n{BLOCK_END}"
    begin = original.find(BLOCK_BEGIN)
    end = original.find(BLOCK_END)
    if begin != -1 and end != -1 and end > begin:
        before = original[:begin]
        after = original[end + len(BLOCK_END):]
        return before + block + after
    # absent → append (ensure a separating newline).
    sep = "" if original == "" or original.endswith("\n") else "\n"
    return original + sep + block + "\n"


# rig-owned settings whose presence inline means the original ~/.tmux.conf is migratable.
_INLINE_MARKERS = (
    "tmux-resurrect",
    "tmux-continuum",
    "@resurrect-",
    "@continuum-",
    "tmux/plugins/tpm",
    "MOSHI_CLIENT",
)

# The marker rig prepends when it neutralizes (comments out) an inline line, so a re-apply
# recognizes its own work (idempotent) and a human sees what rig disabled and why.
NEUTRALIZE_PREFIX = "# rig-migrated (now in rig.tmux.conf): "


def has_inline_rig_settings(conf_text: str) -> bool:
    """True if a NON-comment line of the hand-written conf names a rig-owned setting to migrate.

    Used by the first-apply migration to decide whether to back the original up before wiring
    the managed region. Scans only live (non-comment) lines so a commented-out mention (e.g.
    ``# I disabled tmux-resurrect``) does not trigger a spurious backup. A plain user conf with
    no live rig settings is left alone except for the single import line / managed block.
    """
    for line in conf_text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if any(marker in s for marker in _INLINE_MARKERS):
            return True
        # a BARE Moshi `set -g status-right ''` (not wrapped in a MOSHI_CLIENT if-shell, so the
        # marker scan above misses it) is still something migration will NEUTRALIZE — so it must
        # trigger the pre-neutralize backup too, or the original is lost (the backup contract).
        if _is_moshi_status_wipe(line):
            return True
    return False


def _is_moshi_status_wipe(line: str) -> bool:
    """A single-line ``set -g status-left/right ''`` (the Moshi wipe that causes the bug)."""
    s = line.strip()
    if not s or s.startswith("#"):
        return False
    return ("status-right" in s or "status-left" in s) and (
        s.endswith("''") or s.endswith('""')
    )


def neutralize_inline_rig_lines(conf_text: str) -> str:
    """Neutralize ONLY the inline Moshi ``status-left``/``status-right`` wipe — nothing else.

    The root-cause completion, kept deliberately NARROW (a lesson from a real migration: an
    over-broad neutralize silently dropped user-tuned options rig does not model, like
    ``@resurrect-strategy-vim`` / ``@continuum-boot-options``). The ONLY inline line that is
    actively harmful is the Moshi ``status-right ''`` wipe: if it runs AFTER the user's own
    continuum init it wipes continuum's autosave hook → the stale-session bug. rig's sourced
    ``rig.tmux.conf`` re-runs the plugin inits in the correct order LAST (continuum prepends its
    hook to whatever ``status-right`` is), so leaving the user's other rig-adjacent lines live is
    harmless — tmux just re-applies them, and rig's correctly-ordered tail wins. So we comment
    out ONLY the wipe (the ``if-shell '[ -n "$MOSHI_CLIENT" ]' { … }`` block that sets
    status-left/right, or a bare ``set -g status-right ''``), prefixed with
    :data:`NEUTRALIZE_PREFIX`; the full original is in a timestamped ``~/.tmux.conf.rig-bak-<UTC>``. Everything else
    — plugin decls, resurrect/continuum options (modeled or not), the inits — stays LIVE.
    Idempotent: an already-neutralized line is left as-is.
    """
    lines = conf_text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        # a Moshi if-shell block guarding $MOSHI_CLIENT that sets status-left/right: neutralize
        # the whole brace block (the inline wipe is the bug). Multi-line `{ … }` only; a
        # single-line variant is caught by the bare-wipe rule below.
        if (
            stripped.startswith("if-shell")
            and "MOSHI_CLIENT" in stripped
            and stripped.endswith("{")
        ):
            block = [raw]
            j = i + 1
            depth = 1
            while j < n and depth > 0:
                block.append(lines[j])
                depth += lines[j].count("{") - lines[j].count("}")
                j += 1
            joined = "".join(block)
            if "status-right" in joined or "status-left" in joined:
                out.extend(_comment(b) for b in block)
                i = j
                continue
            out.extend(block)
            i = j
            continue
        # a single-line Moshi if-shell whose inline body sets status-left/right (e.g.
        # `if-shell '[ -n "$MOSHI_CLIENT" ]' { set -g status-right '' }`).
        if (
            stripped.startswith("if-shell")
            and "MOSHI_CLIENT" in stripped
            and ("status-right" in stripped or "status-left" in stripped)
        ):
            out.append(_comment(raw))
            i += 1
            continue
        # a bare single-line Moshi status wipe.
        if _is_moshi_status_wipe(raw):
            out.append(_comment(raw))
        else:
            out.append(raw)
        i += 1
    return "".join(out)


def _comment(line: str) -> str:
    """Prefix a line with the neutralize marker, idempotently (don't double-comment)."""
    if line.lstrip().startswith(NEUTRALIZE_PREFIX) or line.lstrip().startswith("#"):
        return line
    nl = "\n" if line.endswith("\n") else ""
    return NEUTRALIZE_PREFIX + line.rstrip("\n") + nl
