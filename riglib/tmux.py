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
- **rig writes ITS OWN region, and NEUTRALIZES the rig-owned init it now replaces.** rig owns the
  generated ``rig.tmux.conf`` wholesale (import mode) or the text between the sentinel markers
  (block mode). In the user's ``~/.tmux.conf`` it leaves every personal pref and every third-party
  ``@plugin`` LIVE, but comments out (``# rig-migrated (now in rig.tmux.conf): …``) the rig-OWNED
  plugin/continuum/resurrect init lines it re-emits itself — the three rig ``@plugin`` decls, all
  ``@continuum-*`` / ``@resurrect-*`` options, the plugin-init ``run-shell``s, tpm's ``run``, and
  the Moshi wipe (see :func:`neutralize_inline_rig_lines`). This is what stops the DOUBLE-INIT.

Past bugs this fixes
--------------------
1. The stale-session-on-reboot bug: a hand-written ``~/.tmux.conf`` ran
   ``if-shell '[ -n "$MOSHI_CLIENT" ]' { set -g status-right '' }`` at the END of the file —
   AFTER ``run-shell …/continuum.tmux``. tmux-continuum's autosave timer lives in ``status-right``,
   so the Moshi tweak wiped it → continuum silently stopped saving → a reboot restored a
   weeks-stale session. Generating the region lets rig pin the order and end the bug.
2. The DOUBLE-INIT bug (live machine 2026-06-18): migration only APPENDED rig's ``source-file``
   line and left the user's pre-existing tpm/resurrect/continuum init LIVE above it. That old
   ``run-shell …/continuum.tmux`` fired continuum-restore BEFORE rig's sourced config set the
   login-shell ``default-command`` → restored panes spawned non-login (``~/.zprofile`` skipped),
   and the old ``@continuum-boot 'on'`` / ``@resurrect-processes '…'`` fought rig's clean values.
   Migration now NEUTRALIZES that rig-owned init so only rig's correctly-ordered tail runs.
"""

from __future__ import annotations

import os
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

# DEFECT 6: the tmux plugins rig installs on a CLEAN machine (empty ~/.tmux/plugins). The
# `@plugin` declarations in rig.tmux.conf only RESOLVE once these are cloned; tpm itself reads
# them. ONE source of truth — ``{dir_name: (clone_url, real_entrypoint)}`` — consumed by the
# activation (clone-if-missing), the completeness check (is this a full checkout?), and drift,
# so they can NEVER disagree on the entrypoint. NB the entrypoint is the PLUGIN's own basename,
# NOT the repo dir name: tmux-resurrect ships ``resurrect.tmux`` (not ``tmux-resurrect.tmux``),
# tmux-continuum ships ``continuum.tmux``, tpm ships ``tpm``. The generated
# ``run-shell ~/.tmux/plugins/<dir>/<entry>`` lines use these exact names, so the completeness
# check MUST match them or a REAL checkout is judged partial and re-cloned every apply (the
# entrypoint-name-drift bug review caught).
#
# TRUST / UPDATE CONTRACT (explicit — these ARE the canonical upstream repos tpm itself clones):
# the activation does a one-shot ``git clone --depth 1`` of each repo's DEFAULT BRANCH the first
# time it is MISSING, and then NEVER touches it again (an existing complete checkout is left
# exactly as-is — the user owns plugin updates via tpm's own ``prefix + U``). rig deliberately
# does NOT pin a commit SHA: tpm's whole model is "clone the canonical plugin repos", a pinned SHA
# would silently rot and diverge from what every other tpm user runs, and the user can already pin
# via their own tooling. So the stored value is the repo URL (not ``url@sha``); the contract is
# "install the canonical plugin if absent, never auto-upgrade" — NOT "pin an exact ref".
PLUGINS = {
    "tpm": ("https://github.com/tmux-plugins/tpm", "tpm"),
    "tmux-resurrect": ("https://github.com/tmux-plugins/tmux-resurrect", "resurrect.tmux"),
    "tmux-continuum": ("https://github.com/tmux-plugins/tmux-continuum", "continuum.tmux"),
}

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
# The boot entrypoint the launchd agent runs. DEFECT 1: the agent must NOT run a bare
# `tmux start-server` (an EMPTY server — tmux loads ~/.tmux.conf only on the FIRST session, so
# continuum-restore never fires). This script does `tmux new-session -d` (which loads the conf
# → the sourced rig.tmux.conf → continuum → restore) so a cold boot actually comes up restored.
BOOT_NAME = "tmux-boot.sh"

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
    login_shell_enabled: bool
    login_shell: str  # "" → resolve $SHELL at config-eval time in the shell; else a literal path

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

    @property
    def boot_script_path(self) -> Path:
        return self.generated_dir / BOOT_NAME

    def managed_scripts(self) -> list[tuple[Path, str]]:
        """The (path, body) pairs apply writes and drift checks — ONE source so they can't
        diverge: cc-save + cc-restore always, the attach-or-create entry when anti-sprawl is on,
        the boot script when boot is on (the launchd agent's entrypoint — DEFECT 1).
        """
        scripts = [
            (self.cc_save_path, self.render_cc_save()),
            (self.cc_restore_path, self.render_cc_restore()),
        ]
        if self.anti_sprawl_enabled:
            scripts.append((self.attach_path, self.render_attach_script()))
        if self.boot_enabled:
            scripts.append((self.boot_script_path, self.render_boot_script()))
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
        # DEFECT 3 (the reboot bug): resurrect restores panes with a NON-login shell
        # (its `default-command ''`), so ~/.zprofile (PATH, etc.) is NOT sourced → restored panes
        # have a broken env. Set a LOGIN-shell default-command so every (new AND restored) pane
        # sources the full login env. Default-on; configurable via tmux.login_shell.
        #
        # CRITICAL (live-cycle bug): the shell path is a CONCRETE path resolved at GENERATION time,
        # NOT a tmux `${SHELL}` reference. tmux expands `${VAR}` itself in a double-quoted option
        # value but does NOT support the `${VAR:-default}` bashism — `set -g default-command
        # "${SHELL:-/bin/sh} -l"` makes tmux abort the WHOLE source-file with "invalid environment
        # variable" at that line, so continuum/tpm/everything after it never loads → an empty,
        # config-less server (caught only by the REAL e2e, never by a parse-check). So we bake the
        # path. DETERMINISM: the plan resolves the shell ONCE (plan._build_tmux) and bakes the
        # concrete path into the action — so render does NOT read $SHELL/FS here and rig.tmux.conf
        # is identical across applies/status regardless of the ambient $SHELL (review Medium: a
        # per-render resolve made drift flap). The `or resolve_login_shell()` is only a fallback
        # for a direct build_tmux() with an empty shell (tests); the real path is plan-baked.
        if self.login_shell_enabled:
            shell = self.login_shell or resolve_login_shell()
            # tmux's `default-command` takes ONE option value, which tmux then runs as a SHELL
            # command (`sh -c "<value>"`). So the value must be a SINGLE tmux argument whose INNER
            # text, when the shell parses it, is `<quoted-path> -l`. We therefore: shell-quote the
            # path (so a path with a space stays one argv for the inner shell), append ` -l`, and
            # wrap the whole thing in tmux double-quotes so tmux sees it as one value. e.g. a plain
            # path → set -g default-command "/bin/zsh -l"; a spaced path →
            # set -g default-command "'/Apps/My Shell/zsh' -l". A bare path without metachars stays
            # unquoted by shlex (readable common case); only a risky path gets the inner quotes.
            command = f"{shlex.quote(shell)} -l"
            out.append(f'set -g default-command "{command}"')
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

        DEFECT 2 (the reboot bug): Claude Code does NOT show up as ``claude`` in tmux's
        ``pane_current_command`` — it shows as its VERSION string (e.g. ``2.1.178``), and the
        REAL ``claude`` process is a CHILD of the pane's shell. Filtering on
        ``pane_current_command == claude`` therefore matched NOTHING → the map stayed empty → cc
        never resumed. So cc-save now walks the pane's process TREE: it takes ``pane_pid`` and
        recursively descends children (``ps -eo pid,ppid,args``) looking for the ``claude``
        process. A pane with a ``claude`` descendant is a cc pane.

        WHY the match is on the EXECUTABLE PATH, not just the basename ``claude`` (the 2026-06-17
        incident): Claude Code installs as a SYMLINK ``~/.local/bin/claude`` →
        ``…/claude/versions/<version>``, and the real executable FILE is named by its VERSION
        (``2.1.179``). Launched via the ``claude`` symlink the kernel keeps the invoked name
        ``claude`` — but launched by the RESOLVED path the process's name is the version string,
        NOT ``claude``. A basename-only ``claude`` / ``*/claude`` match misses THAT process exactly
        as the old command-string filter did → the map stays empty → cc never resumes after a
        reboot (the live incident). So the tree walk reads the full command line (``ps -eo args``),
        takes argv[0] (the executable = the line up to the first space), and matches THAT against:
        ``claude`` / ``*/claude`` (symlink launch) OR a path under ``…/claude/versions/``
        (direct-path launch of the versioned binary). Reading ``args`` — not ``comm`` (the
        basename-only, 15-char-truncated value on Linux) — makes the versioned PATH visible on both
        macOS and Linux. Keying on argv[0] ONLY (not the whole args line) is load-bearing: a
        ``claude`` / ``claude/versions/`` token appearing in an ARGUMENT (``vim claude.md``,
        ``grep -r x …/claude/versions/``, ``cp /opt/claude /tmp``) must NOT mark the pane as cc —
        whole-line matching would write a bogus cc-map entry. (Limitations, both accepted: (a) an
        install path with a SPACE truncates argv[0] at the space — isolating a spaced argv[0] from
        ``ps args`` is not possible, and the whole-line match that would cover it reintroduces the
        false positives; the default ``~/.local/share/claude/versions/<v>`` path has no space. (b) a
        WRAPPER launch that rewrites argv[0] — ``npx claude`` / ``node …/cli.js`` / a shell function
        — puts the real claude in argv[1+], so the pane is missed; the canonical installs this fix
        targets exec the binary directly (``claude`` symlink or the versioned path), so argv[0] is
        the claude executable. Matching argv[1+] would resurrect the argument false-positives.)

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
# Records, for every tmux pane whose process TREE contains a `claude` process, a map line:
#   <session>:<window>.<pane><TAB><cwd><TAB><claude-session-id>
# so cc-restore can relaunch `claude --resume <id>` in the right window after a reboot.
# WHY a tree walk and not `pane_current_command == claude`: Claude Code shows up in
# `pane_current_command` as its VERSION (e.g. 2.1.178); the real `claude` process is a CHILD of
# the pane's shell. So we descend the pane PID's children and match the `claude` process.
# WHY argv[0]'s PATH, not just basename `claude`: cc installs as a symlink
# ~/.local/bin/claude -> .../claude/versions/<version>; launched by the RESOLVED path the process
# name is the version (2.1.179), NOT `claude`. So we read the full `args` (argv[0] is the full
# path on macOS AND Linux, unlike `comm` which is the truncated basename on Linux), take argv[0]
# (up to the first space), and match `claude`/`*/claude` OR a path under `.../claude/versions/`.
# Matching argv[0] ONLY (not the whole args) stops a `claude`/`claude/versions/` token in an
# ARGUMENT (`grep .../claude/versions/`, `vim claude.md`) from a bogus match.
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

# Snapshot the whole process table ONCE (pid ppid args) — walking the tree per pane against a
# live `ps` each time would race; one snapshot is consistent and cheap. We capture `args` (the
# full command line: executable path + argv), NOT `comm`, for PORTABILITY: macOS `comm` is the
# full executable PATH, but LINUX `comm` is the 15-char-truncated BASENAME with no path — so the
# versioned-binary install (.../claude/versions/<version>, basename = the VERSION) is INVISIBLE
# to a comm match on Linux (comm would read `2.1.179`, no path). `args` carries the full path on
# BOTH platforms, so the `.../claude/versions/` segment is matchable everywhere. The `read -r pid
# ppid rest` below puts the WHOLE remaining line (the args) into `rest`, so variable-width argv
# never breaks the 2-field key parse.
PS_SNAPSHOT=$(ps -eo pid=,ppid=,args= 2>/dev/null || true)

pane_has_claude() {{
  # BFS over the descendants of the pane's pid; return 0 if any descendant IS a `claude` process.
  local root="$1"
  local -a queue=("$root")
  local pid ppid rest exe cur
  while [ "${{#queue[@]}}" -gt 0 ]; do
    cur="${{queue[0]}}"
    queue=("${{queue[@]:1}}")
    # scan the snapshot for: (a) `cur`'s own command, and (b) `cur`'s direct children to enqueue.
    while read -r pid ppid rest; do
      [ -n "$pid" ] || continue
      if [ "$pid" = "$cur" ]; then
        # `rest` is the full command line: argv[0] (the EXECUTABLE) then its args. We match the
        # EXECUTABLE ONLY — `exe=${{rest%% *}}` is argv[0] up to the first space — NOT the whole
        # `rest`. Matching the whole line would let a `claude`/`claude/versions/` token appearing
        # in an ARGUMENT false-positive (`grep -r x .../claude/versions/`, `cp /opt/claude /tmp`,
        # `vim claude.md`) and write a bogus cc-map entry. Keying on argv[0] is the documented
        # contract. (Limitation: an install path containing a SPACE truncates argv[0] here — but
        # isolating a spaced argv[0] is impossible from `ps args` alone, and tolerating it would
        # require the whole-line match that reintroduces the false positives, so we accept the rare
        # spaced-install miss over false positives for everyone. The default install path
        # ~/.local/share/claude/versions/<v> has no space.)
        exe=${{rest%% *}}
        case "$exe" in
          # a binary/symlink named `claude` (the symlink-launch case: argv[0] basename is `claude`).
          claude|*/claude) return 0 ;;
          # direct-path launch of the VERSIONED binary (e.g. ~/.local/share/claude/versions/2.1.179
          # — its name is the VERSION, not `claude`). Matchable on macOS AND Linux because we read
          # the full `args` PATH for argv[0], not `comm` (Linux `comm` is the truncated basename,
          # no path). The leading `*/` requires `claude/versions/` to be a real path segment, so
          # `…/notclaude/versions/…` never matches.
          */claude/versions/*) return 0 ;;
        esac
      fi
      if [ "$ppid" = "$cur" ]; then
        queue+=("$pid")
      fi
    done <<< "$PS_SNAPSHOT"
  done
  return 1
}}

: > "$MAP_FILE"
# iterate every pane; emit a map line only for panes whose process tree contains `claude`.
tmux list-panes -a -F '#{{session_name}}:#{{window_index}}.#{{pane_index}}	#{{pane_pid}}	#{{pane_current_path}}' \\
  | while IFS=$'\\t' read -r addr pane_pid cwd; do
      pane_has_claude "$pane_pid" || continue
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

    # ── the boot script (DEFECT 1: load the conf via a real session, then restore) ────
    def render_boot_script(self) -> str:
        """The launchd agent's entrypoint: bring tmux up AT LOGIN with the config LOADED.

        DEFECT 1 (the reboot bug): the old plist ran ``tmux start-server`` directly, which starts
        a server WITHOUT loading ~/.tmux.conf or any plugin (tmux sources the conf only on the
        FIRST session), so ``@continuum-restore`` never fired → an EMPTY server → ``tmux ls`` said
        "no server running" after login. This script instead creates a detached session
        (``tmux new-session -d``), which DOES load the conf → the sourced ``rig.tmux.conf`` →
        continuum's ``run-shell`` init → (with ``@continuum-restore on``) the saved session is
        restored INTO the server. Idempotent: if the canonical session already exists (a warm
        login), it does nothing rather than spawn a duplicate (anti-sprawl at boot).
        """
        tmux_bin = _resolve_tmux_bin()
        session = shlex.quote(self.anti_sprawl_session)
        # A non-default conf must be passed via `-f` (tmux only auto-loads ~/.tmux.conf), else the
        # boot session starts WITHOUT the managed config → continuum/resurrect never set → no
        # restore. Default path needs no -f. shlex.quote the path (a HOME with a space).
        f_arg = ""
        if self.conf_path != self.home / ".tmux.conf":
            f_arg = f" -f {shlex.quote(str(self.conf_path))}"
        tmux_q = shlex.quote(tmux_bin)
        return f"""#!/usr/bin/env bash
# rig-managed: tmux boot — GENERATED by rig from rig.yaml. Do not hand-edit.
# The launchd agent runs THIS at login. It creates a detached session (which loads ~/.tmux.conf
# → the sourced rig.tmux.conf → continuum), so `@continuum-restore on` restores the saved
# session into the server. Merely starting a bare server would NOT load the conf (it loads only
# on the first session) → continuum never fires → "no server running" after login.
set -euo pipefail
TMUX_BIN={tmux_q}
SESSION={session}
# already up (warm login) → do nothing (no duplicate session — anti-sprawl at boot).
if "$TMUX_BIN" has-session -t "$SESSION" 2>/dev/null; then
  exit 0
fi
# create the detached session — this loads the conf and triggers continuum-restore.
"$TMUX_BIN"{f_arg} new-session -d -s "$SESSION"
"""

    # ── the boot launchd plist (macOS) ───────────────────────────────────────────────
    def render_boot_plist(self) -> str:
        """A launchd agent that runs the boot SCRIPT at login so continuum can restore.

        DEFECT 1: the plist's single program argument is the generated boot script (NOT a bare
        ``tmux start-server`` — that starts an EMPTY server with no conf/plugins loaded, so
        continuum-restore never fires). The script does ``tmux new-session -d`` to load the conf
        and trigger the restore. ``KeepAlive`` is false — we only need it to fire once at login;
        ``rig apply`` ``launchctl load -w``s it so it is enabled across reboots.
        """
        # plistlib gives idiomatic, escape-safe XML (no hand-rolled string concat / injection).
        import plistlib

        payload = {
            "Label": self.boot_label,
            "ProgramArguments": [str(self.boot_script_path)],
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


def resolve_login_shell() -> str:
    """The user's login shell as a CONCRETE path, resolved from a STABLE source.

    Baked into the action at PLAN time, NOT a tmux ``${SHELL}`` reference (tmux's option-value env
    expansion rejects ``${VAR:-default}`` and a bare ``${SHELL}`` is fragile under launchd — a
    wrong ref aborts the WHOLE source-file with "invalid environment variable", so continuum/tpm
    never load).

    CRITICAL — resolve from the PASSWD DATABASE, not ``$SHELL`` (review P1): ``apply`` and
    ``status`` each rebuild a FRESH plan, so resolving from the volatile ``$SHELL`` env var would
    bake a DIFFERENT shell when the two run under different environments (``SHELL=/bin/bash rig
    apply`` then ``SHELL=/usr/bin/fish rig status``, or a launchd/cron status with no $SHELL) →
    permanent flapping drift. ``pwd.getpwuid(getuid()).pw_shell`` is the user's REAL login shell
    from the system account database — IDENTICAL across every invocation regardless of the ambient
    env. We use it first; only if it is unavailable/empty do we fall back to ``$SHELL``, then
    ``/bin/zsh`` (macOS default), then ``/bin/sh`` (always present).
    """
    try:
        import pwd

        passwd_shell = pwd.getpwuid(os.getuid()).pw_shell
        if passwd_shell.startswith("/"):
            return passwd_shell
    except (KeyError, OSError, ImportError, AttributeError):
        pass  # no passwd entry / non-POSIX — fall through to the env/default chain.
    env_shell = os.environ.get("SHELL", "")
    if env_shell.startswith("/"):
        return env_shell
    if Path("/bin/zsh").exists():
        return "/bin/zsh"
    return "/bin/sh"


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
    login_shell: dict | None = None,
) -> TmuxPlan:
    """Resolve the desired :class:`TmuxPlan` from the (already-validated) tmux config block.

    ``repo_home`` is the resolved HOME (the caller passes ``Path.home()`` or a test tmp HOME).
    HOME-relative ``conf_path`` / ``generated_dir`` are expanded against it. Every nested
    knob defaults to the safe, root-cause-fixing value; an empty block yields the full default
    config (claude in resurrect, capture-pane on, continuum restore+boot, Moshi off, cc-restore
    on, anti-sprawl on, boot on, login-shell default-command on).
    """
    resurrect = resurrect or {}
    continuum = continuum or {}
    moshi = moshi or {}
    cc_restore = cc_restore or {}
    anti_sprawl = anti_sprawl or {}
    boot = boot or {}
    login_shell = login_shell or {}

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
        login_shell_enabled=bool(_knob(login_shell, "enabled", True)),
        # An explicit shell override is used verbatim; "" means "resolve $SHELL in the shell at
        # pane-spawn time" (the safe default — the login server inherits the user's $SHELL).
        login_shell=str(_knob(login_shell, "shell", "")),
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


# The marker rig prepends when it neutralizes (comments out) an inline line, so a re-apply
# recognizes its own work (idempotent) and a human sees what rig disabled and why.
NEUTRALIZE_PREFIX = "# rig-migrated (now in rig.tmux.conf): "

# The THREE plugins rig owns and re-declares + re-inits in rig.tmux.conf — the `@plugin` SPEC
# (`<org>/<repo>`) DERIVED from each PLUGINS url, so this can never drift from the canonical repos
# rig actually clones/inits (one source of truth — a review finding: a hardcoded copy would
# silently desync if a plugin's url/org moved). An inline `set -g @plugin '<owned>'` is a
# DUPLICATE of rig's own declaration → neutralized; any OTHER `@plugin` (tmux-sensible, tmux-yank,
# a third-party) is the USER's and stays LIVE (rig's tpm, run at the END of rig.tmux.conf, loads
# it). Matched WITH the closing quote (`'<spec>'`) so neither `tmux-plugins/tmux-sensible` NOR a
# fork `tmux-plugins/tmux-resurrect-fork` is swept by a bare-prefix substring check.
_RIG_OWNED_PLUGIN_SPECS = tuple(
    "/".join(url.rstrip("/").split("/")[-2:]) for url, _entry in PLUGINS.values()
)

# The rig-owned plugin INIT entrypoints, as the `run-shell <path>` / `run '<path>'` directives
# end (the path under ~/.tmux/plugins/<dir>/<entry>). A hand-written init names one of these as
# the run COMMAND; we match the command's SUFFIX so `~/…`, `$HOME/…`, and absolute forms all hit,
# while a path appearing only as a nested ARGUMENT of another command does not (see
# `_run_command_target`). The `<dir>/<entry>` pairs come from PLUGINS (one source of truth).
#
# KNOWN LIMITATION (accepted, matches rig's own model): the suffix is the DEFAULT
# `~/.tmux/plugins/` location — an init from a custom/XDG plugin dir
# (`~/.config/tmux/plugins/<dir>/<entry>`) is NOT matched, so that init line stays live. This is
# consistent: rig CLONES plugins into `~/.tmux/plugins` and its generated `rig.tmux.conf` inits
# them from that exact path, so rig does not model XDG plugin dirs at all. (The `@plugin` decls
# and `@continuum-*`/`@resurrect-*` options of such a user ARE still neutralized — they are
# path-independent.) Pinned by `test_neutralize_known_limitation_xdg_plugin_init_stays_live`.
_PLUGIN_INIT_ENTRYPOINTS = tuple(
    f".tmux/plugins/{name}/{entry}" for name, (_url, entry) in PLUGINS.items()
)


def _strip_managed_block(conf_text: str) -> str:
    """Drop the lines inside rig's managed block (block apply mode — between :data:`BLOCK_BEGIN` /
    :data:`BLOCK_END`, sentinels included), leaving ONLY the user's own region. That block is
    rig's GENERATED config (it carries live ``@continuum-*`` / ``run-shell …continuum.tmux`` by
    design); it is not a migration target, so the backup/migration decision must ignore it."""
    out, in_block = [], False
    for line in conf_text.splitlines(keepends=True):
        s = line.strip()
        if s == BLOCK_BEGIN:
            in_block = True
            continue
        if s == BLOCK_END:
            in_block = False
            continue
        if not in_block:
            out.append(line)
    return "".join(out)


def has_inline_rig_settings(conf_text: str) -> bool:
    """True iff migrating ``conf_text`` would actually NEUTRALIZE a live user line — i.e. the
    user's own region still carries a rig-owned plugin/continuum/resurrect init or the Moshi
    wipe that :func:`neutralize_inline_rig_lines` comments out.

    Single source of truth: it is defined AS "neutralization changes the user's region", so the
    backup gate can never disagree with what neutralization does. This matters for idempotency —
    a personal pref that merely MENTIONS a rig token but is NOT neutralized (e.g.
    ``set-option -ga update-environment ' MOSHI_CLIENT'``, which migration deliberately keeps
    live) must NOT keep reporting "migratable" forever, or a re-apply would back the conf up on
    EVERY run though nothing changes. rig's own managed block (block mode) is excluded first via
    :func:`_strip_managed_block` (its live ``@continuum-*`` lines are rig's, not the user's).

    Used by the first-apply migration to decide whether to back the original up before wiring the
    managed region; a commented-out mention never triggers it (neutralization is a no-op on it).
    """
    user_region = _strip_managed_block(conf_text)
    return neutralize_inline_rig_lines(user_region) != user_region


def _is_moshi_status_wipe(line: str) -> bool:
    """A single-line ``set -g status-left/right ''`` (the Moshi wipe that causes the bug)."""
    s = line.strip()
    if not s or s.startswith("#"):
        return False
    return ("status-right" in s or "status-left" in s) and (
        s.endswith("''") or s.endswith('""')
    )


# The two tmux verbs that SET a user option (`set`, `set-option`, and the `set-window-option` /
# `setw` window variants). A rig-owned option is neutralized ONLY when it is the OPTION BEING SET
# by one of these — never when an `@continuum-…` / `@resurrect-…` token merely appears inside a
# VALUE (a `status-right '#(… @continuum-save-interval)'`) or a keybinding (`bind r run-shell
# 'tmux set @continuum-boot on'`). Anchoring to the directive is what keeps those user lines live.
_SET_VERBS = ("set-option", "set-window-option", "setw", "set")


def _set_option_name(s: str) -> str | None:
    """If ``s`` is a tmux option-SET directive, return the option name it sets, else ``None``.

    tmux option-set grammar: ``<set-verb> [-flags…] <option-name> [value]``. We take the verb,
    then the FIRST non-flag token after it as the option name (tmux flags are single-dash, e.g.
    ``-g`` / ``-ga`` / ``-gq``; a value never begins with a single ``-``). So ``set -g @plugin
    'x'`` → ``@plugin`` and ``set -ga @continuum-restore 'on'`` → ``@continuum-restore``, while a
    ``status-right`` whose VALUE contains ``@continuum-…`` returns ``status-right`` (not the
    embedded token) and a ``bind``/``run-shell`` line returns ``None`` (not a set verb).

    KNOWN LIMITATION (accepted, same class as the documented set-hook/XDG ones): tmux accepts
    UNAMBIGUOUS command ABBREVIATIONS (``set-o``, ``set-opt``, ``run-s``). We match the full verbs
    (+ the common ``setw`` alias) by exact equality; an abbreviated verb returns ``None`` → the
    line stays live. Canonical / generated configs spell verbs in full, so this is a rare hand-edit
    edge, not the targeted path."""
    parts = s.split()
    if len(parts) < 2 or parts[0] not in _SET_VERBS:
        return None
    for tok in parts[1:]:
        if tok.startswith("-") and not tok.startswith("--"):
            continue  # a single-dash flag (-g/-ga/-gq/…)
        return tok
    return None


def _set_option_value(s: str) -> str | None:
    """The VALUE token of a set directive — the token right AFTER the option name (e.g.
    ``set -g @plugin 'tmux-plugins/tpm'`` → ``'tmux-plugins/tpm'``), or ``None`` if absent.
    Used only for ``@plugin`` (a single-token spec, quoted or bare); options with multi-word
    values are matched by NAME, not value, so the simple next-token read is enough here."""
    parts = s.split()
    seen_name = False
    for tok in parts[1:]:
        if tok.startswith("-") and not tok.startswith("--"):
            continue  # a single-dash flag
        if not seen_name:
            seen_name = True  # this token is the option NAME; the next non-flag is the value
            continue
        return tok
    return None


def _is_rig_owned_legacy_line(line: str) -> bool:
    """True if ``line`` is a rig-OWNED plugin/continuum/resurrect init that rig now re-emits
    itself in ``rig.tmux.conf`` — so a hand-written copy is a stale DUPLICATE to neutralize.

    The set is exactly what ``render_rig_conf`` regenerates (and only that):
      - ``set -g @plugin '<tmux-plugins/{tpm,tmux-resurrect,tmux-continuum}>'`` — the three rig
        plugins, matched on the QUOTED spec (closing quote anchored) so a fork like
        ``tmux-plugins/tmux-resurrect-fork`` is NOT swept. A THIRD-PARTY ``@plugin``
        (``tmux-sensible``, ``tmux-yank``, …) is the user's and is NOT matched (rig's tpm loads it).
      - ``set -g @continuum-*`` / ``set -g @resurrect-*`` — every continuum/resurrect option,
        but ONLY when it is the option BEING SET by a set/set-option directive (not a token inside
        a value or keybinding — see :func:`_set_option_name`). rig emits its OWN value for every
        such option (the generated file is sourced AFTER ~/.tmux.conf), so a surviving inline
        ``@continuum-restore 'on'`` / ``@resurrect-processes 'ssh … rails …'`` would otherwise win
        or fight rig's clean values. rig OWNS the whole resurrect/continuum surface, so it
        neutralizes ALL of them — including options it does not itself re-emit
        (``@resurrect-strategy-vim``, ``@continuum-boot-options``): they are recoverable from the
        timestamped ``.rig-bak-<UTC>`` backup, and leaving them live re-introduces the very
        ordering/duplication this fix removes. (Set ``tmux:`` knobs in ``rig.yaml`` to re-add a
        value rig models; an unmodeled option you truly need belongs in a SEPARATE file you
        ``source-file`` yourself — rig never touches another file. A bare re-add inside
        ``~/.tmux.conf`` is re-neutralized on the next apply, by design — which is also why this
        function is safe to run on EVERY reconcile, not only the first migration: idempotent.)
      - the plugin INIT lines: ``run-shell …/tmux-resurrect/resurrect.tmux``,
        ``run-shell …/tmux-continuum/continuum.tmux`` and tpm's ``run[-shell] '…/tpm/tpm'`` —
        matched on the run-shell/run COMMAND (its sole argument), NOT a substring, so a command
        that merely mentions the plugin path as a nested ARGUMENT (``run-shell "tar …
        …/continuum.tmux"``) is NOT swept (see :func:`_run_command_target`; over-reach is worse
        than under-reach). These are the ACTIVELY harmful ones — the live machine's double-init bug
        (2026-06-18): the hand-written ``run-shell …/continuum.tmux`` runs continuum-restore BEFORE rig's
        appended ``source-file`` sets the login-shell ``default-command``, so restored panes
        spawn NON-login and ~/.zprofile is skipped. rig re-runs these inits in the pinned order
        at the END of its sourced file, so the inline copies must go.

    Matched on the STRIPPED text; a comment / an already-neutralized line is handled by the
    caller (``_comment`` is idempotent). Personal prefs (mouse, history-limit, key bindings,
    ``update-environment MOSHI_CLIENT``, a real ``status-right`` value even one that PRINTS a
    continuum option, …) never match here.

    KNOWN LIMITATION (accepted): an init that runs the plugin via an INDIRECTION rig does not
    model — e.g. ``set-hook -g session-created 'run-shell …/continuum.tmux'`` or a shell function
    wrapping it — is NOT recognized (the ``run-shell`` is a quoted VALUE, not the directive), so a
    user with that exotic setup keeps the double-init. The canonical tpm setup this fix targets
    inits plugins with bare ``run-shell``/``run`` lines, which ARE caught. Pinned live by
    ``test_neutralize_known_limitation_set_hook_init_stays_live`` so a future widening is deliberate.
    """
    s = line.strip()
    if not s or s.startswith("#"):
        return False
    opt = _set_option_name(s)
    if opt is not None:
        # a set/set-option directive — neutralize only when the OPTION it sets is rig-owned.
        if opt == "@plugin":
            # compare the EXACT spec value (quotes stripped) so a fork `…/tmux-resurrect-fork`
            # is not swept, AND a valid UNQUOTED `set -g @plugin tmux-plugins/tpm` still matches.
            value = _set_option_value(s)
            return value is not None and value.strip("'\"") in _RIG_OWNED_PLUGIN_SPECS
        return opt.startswith("@continuum-") or opt.startswith("@resurrect-")
    # the plugin INIT lines: `run-shell <plugin>` / `run '<plugin>'`. Match the run-shell/run
    # COMMAND (its sole argument), NOT a substring — so a backup command that merely MENTIONS the
    # plugin path as a nested arg (`run-shell "tar czf bak ~/.tmux/plugins/.../continuum.tmux"`)
    # is NOT swept (over-reach is worse than under-reach: it changes a line the user didn't ask to).
    target = _run_command_target(s)
    if target is not None:
        return any(target.endswith(entry) for entry in _PLUGIN_INIT_ENTRYPOINTS)
    return False


def _run_command_target(s: str) -> str | None:
    """The COMMAND a ``run-shell``/``run`` directive runs, with surrounding quotes stripped, or
    ``None`` if ``s`` is not such a directive. A plugin init is ``run-shell <path>`` / ``run
    '<path>'`` — the path is the WHOLE command. We return that command so the caller can match the
    plugin entrypoint as the command itself (``…/continuum.tmux``), never as a nested argument of
    an unrelated command (``run-shell "tar … …/continuum.tmux"``, whose command is ``tar``)."""
    parts = s.split(None, 1)
    if not parts or parts[0] not in ("run-shell", "run"):
        return None
    if len(parts) == 1:
        return ""
    arg = parts[1].strip()
    # drop a single leading `-b` (background) flag — the canonical plugin init has no flags, but
    # tolerate `-b` so `run-shell -b <plugin>` still matches. Other flags (`-t <pane>`, `-C`) do
    # not appear on a plugin init and are not stripped (a line carrying them is not a plugin init).
    if arg.startswith("-b"):
        arg = arg[2:].strip()
    # strip ONE balanced pair of surrounding quotes; a path with no metachars is often unquoted.
    if len(arg) >= 2 and arg[0] in "'\"" and arg[-1] == arg[0]:
        arg = arg[1:-1]
    # the command is the first whitespace-delimited token of the (unquoted) argument; a plugin
    # init is a bare path with no args, so the token IS the path.
    return arg.split()[0] if arg.split() else ""


def _iter_scoped_lines(conf_text: str):
    """Yield ``(line, protected)`` for each line, where ``protected`` is True when the line sits
    in a region neutralization must NOT touch:

    - INSIDE rig's own managed block (block apply mode — between :data:`BLOCK_BEGIN` /
      :data:`BLOCK_END`, sentinels included): that region is rig's GENERATED config (live
      ``@plugin`` / ``@continuum-*`` / ``run-shell …continuum.tmux`` by DESIGN); commenting it
      would corrupt the config rig just wrote.
    - INSIDE a user ``if-shell '…' { … }`` conditional (brace depth > 0, the opening/closing brace
      lines included): commenting only an INNER line would leave a dangling/empty brace body (a
      parse hazard) and break the user's deliberate condition — so a rig-owned line a user GUARDED
      is left LIVE (under-reach, which the module prefers to over-reach).

    Brace depth is STRUCTURAL — a non-comment line that ENDS with ``{`` opens a level, a line that
    IS exactly ``}`` closes one (full NESTING, so an inner ``}`` does not prematurely re-expose the
    outer block — the nested-brace bug a boolean flag had). A decorative ``{`` inside a quoted
    value (``status-left "…{"``) or a comment never opens a level (it does not END the line as the
    sole token), and a single-line ``{ … }`` neither ends with ``{`` nor is a bare ``}`` → depth
    stays 0 (its inner directive is a normal top-level line)."""
    in_block = False
    depth = 0
    for line in conf_text.splitlines(keepends=True):
        s = line.strip()
        if s == BLOCK_BEGIN:
            in_block = True
            yield line, True
            continue
        if s == BLOCK_END:
            in_block = False
            yield line, True
            continue
        if in_block:
            yield line, True
            continue
        opening = s.endswith("{") and not s.startswith("#")
        # a closer is a line whose FIRST token is `}` — matches a bare `}`, `} # end moshi`, or
        # `} set -g x` (a brace plus a trailing directive), so a non-bare close still decrements
        # depth (else every later rig-owned init would be judged "inside" and silently kept live →
        # the double-init survives — review). A `}` inside a VALUE (`set -g x "}"`) starts with the
        # set verb, not `}`, so it never false-closes.
        closing = s.startswith("}")
        # the brace LINES themselves are protected too (they bound the conditional).
        protected = depth > 0 or opening
        yield line, protected
        if opening:
            depth += 1
        elif closing and depth > 0:
            depth -= 1


def _neutralize_legacy_plugin_lines(conf_text: str) -> str:
    """Comment out every rig-OWNED plugin/continuum/resurrect init line (see
    :func:`_is_rig_owned_legacy_line`) in the USER's UNPROTECTED region, idempotently, prefixed
    with :data:`NEUTRALIZE_PREFIX`. Protected regions (rig's managed block, a user ``{ … }``
    conditional) are skipped — see :func:`_iter_scoped_lines`.

    A third-party ``@plugin`` and every personal pref are left untouched. Re-running on an
    already-migrated file is a no-op (``_comment`` skips a line already commented).
    """
    out: list[str] = []
    for line, protected in _iter_scoped_lines(conf_text):
        out.append(_comment(line) if not protected and _is_rig_owned_legacy_line(line) else line)
    return "".join(out)


def neutralize_inline_rig_lines(conf_text: str) -> str:
    """Neutralize the rig-OWNED inline lines so the sourced ``rig.tmux.conf`` is authoritative.

    Two passes, both commenting with :data:`NEUTRALIZE_PREFIX` (idempotent, backed by the
    timestamped ``~/.tmux.conf.rig-bak-<UTC>``):

    1. :func:`_neutralize_legacy_plugin_lines` — the three rig-owned ``@plugin`` decls, every
       ``@continuum-*`` / ``@resurrect-*`` option, and the plugin INIT lines
       (``run-shell …/resurrect.tmux`` / ``…/continuum.tmux`` / tpm's ``run '…/tpm/tpm'``). These
       are the DOUBLE-INIT the live machine hit (2026-06-18): the hand-written ``run-shell
       …/continuum.tmux`` fires continuum-restore BEFORE rig's appended ``source-file`` sets the
       login-shell ``default-command`` → restored panes spawn non-login; the old
       ``@continuum-boot 'on'`` / ``@resurrect-processes '…'`` fight rig's clean values. rig
       re-runs these inits in the pinned order at the END of its sourced file, so the inline
       copies must be neutralized.
    2. the Moshi ``status-left``/``status-right`` wipe (the original root-cause line): if it runs
       AFTER the user's own continuum init it wipes continuum's autosave hook → the stale-session
       bug. The ``if-shell '[ -n "$MOSHI_CLIENT" ]' { … }`` block (multi- or single-line) is
       commented WHOLE (braces included); a BARE ``set -g status-right ''`` is commented only at
       top level (a bare wipe a user guarded inside their OWN ``{ … }`` is left live).

    PRESERVED (never matched): a THIRD-PARTY ``@plugin`` (``tmux-sensible``, ``tmux-yank`` — rig's
    tpm loads it), ``update-environment MOSHI_CLIENT`` (a pref, not the wipe), a real
    ``status-right`` value, and every personal pref (mouse, history-limit, key bindings, …). Both
    passes skip rig's own managed block (block apply mode) AND a user ``{ … }`` conditional (full
    structural nesting), so they never touch rig's generated config nor leave a dangling brace —
    correctness does NOT rely on the splice overwriting the interior.
    """
    return _neutralize_moshi_wipe(_neutralize_legacy_plugin_lines(conf_text))


def _neutralize_moshi_wipe(conf_text: str) -> str:
    """Comment out the Moshi ``status-left``/``status-right`` wipe (the original root-cause line):
    the ``if-shell '[ -n "$MOSHI_CLIENT" ]' { … }`` block (multi- or single-line) or a bare
    ``set -g status-right ''``.

    The Moshi if-shell BLOCK is commented WHOLE (its own braces included → never a dangling brace),
    and its inner braces are consumed by the block scan so they don't disturb the surrounding
    structural depth. A BARE wipe is commented only OUTSIDE rig's managed block AND outside a user
    ``{ … }`` conditional — a bare ``status-right ''`` a user GUARDED in their own non-Moshi
    condition is left LIVE (commenting just it would dangle the brace), mirroring
    :func:`_neutralize_legacy_plugin_lines`."""
    lines = conf_text.splitlines(keepends=True)
    out: list[str] = []
    i, n = 0, len(lines)
    in_block = False
    depth = 0  # structural depth of USER `{ … }` conditionals (NOT rig's managed block)
    while i < n:
        raw = lines[i]
        s = raw.strip()
        if s == BLOCK_BEGIN:
            in_block = True
            out.append(raw)
            i += 1
            continue
        if s == BLOCK_END:
            in_block = False
            out.append(raw)
            i += 1
            continue
        if in_block:
            out.append(raw)
            i += 1
            continue
        # a Moshi if-shell brace block guarding $MOSHI_CLIENT that sets status-left/right —
        # neutralize the WHOLE block (the inline wipe is the bug). Multi-line `{ … }`; the scan
        # consumes to the matching close. Depth is STRUCTURAL (a line ending with `{` opens, a line
        # whose first token is `}` closes), the SAME rule as _iter_scoped_lines — so a literal
        # `{`/`}` inside a value (`status-left "x{"`) never over-consumes to EOF.
        if s.startswith("if-shell") and "MOSHI_CLIENT" in s and s.endswith("{"):
            block, j, bdepth = [raw], i + 1, 1
            while j < n and bdepth > 0:
                inner = lines[j].strip()
                block.append(lines[j])
                if inner.endswith("{") and not inner.startswith("#"):
                    bdepth += 1
                elif inner.startswith("}"):
                    bdepth -= 1
                j += 1
            joined = "".join(block)
            wipe = "status-right" in joined or "status-left" in joined
            if wipe:
                out.extend(_comment(b) for b in block)
            else:
                out.extend(block)
            i = j
            continue
        # a single-line Moshi if-shell whose inline body sets status-left/right (its own `{ … }`
        # is balanced on the one line, so it never opens a user conditional).
        single_moshi = (
            s.startswith("if-shell")
            and "MOSHI_CLIENT" in s
            and ("status-right" in s or "status-left" in s)
        )
        # a bare wipe is neutralized only at top level (depth 0) — a wipe a user guarded inside
        # their OWN `{ … }` stays live (no dangling brace). The Moshi single-line case above is
        # self-contained, so it is safe to comment regardless of depth.
        bare_wipe = _is_moshi_status_wipe(raw) and depth == 0
        out.append(_comment(raw) if single_moshi or bare_wipe else raw)
        # track USER brace depth structurally (same rule as _iter_scoped_lines: a closer is any
        # line whose first token is `}`, so `} # end` / `} set …` still decrement).
        if s.endswith("{") and not s.startswith("#"):
            depth += 1
        elif s.startswith("}") and depth > 0:
            depth -= 1
        i += 1
    return "".join(out)


def _comment(line: str) -> str:
    """Prefix a line with the neutralize marker, idempotently (don't double-comment)."""
    if line.lstrip().startswith(NEUTRALIZE_PREFIX) or line.lstrip().startswith("#"):
        return line
    nl = "\n" if line.endswith("\n") else ""
    return NEUTRALIZE_PREFIX + line.rstrip("\n") + nl
