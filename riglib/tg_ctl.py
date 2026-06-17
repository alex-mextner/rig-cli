"""tg-ctl boot provisioning — PURE planning of the rig-managed LaunchAgent.

What this is
------------
``tg-ctl`` (``~/.files/bin/tg-ctl``, a Bun script) is the INBOUND control daemon for tg-cli:
it long-polls Telegram and injects replies into the agent's tmux pane, renders agent
questions as Telegram buttons, and does voice->text. The daemon command is ``tg-ctl run``.
Config lives in ``~/.config/tg-cli/`` (.env + config.yaml); the daemon reads it itself.

rig provisions this daemon as a macOS LaunchAgent so it auto-starts at login/boot — exactly
like rig already does for the tmux boot service (see :mod:`riglib.tmux`). This module is the
analog of that one: it is **stdlib-only and effect-free** — it computes WHAT the LaunchAgent
plist should be (the generated ``ai.hyperide.tg-ctl.plist`` XML). The effectful writes (write
the plist, back up a differing prior, ``launchctl bootstrap``/``bootout`` in the gui domain,
remove the stale predecessor) live in ``actions/runner.py`` (the one-engine apply path), and
drift detection diffs the desired artifact against disk in ``drift.py``. Three consumers
(plan, apply, drift) share THIS so the desired state never drifts between them.

How it is reached
-----------------
``plan._build_tg_ctl`` reads the ``tg_ctl:`` config block, calls :func:`build_tg_ctl` to
resolve a :class:`TgCtlPlan`, and emits a ``provision_tg_ctl`` action. ``runner.
_do_provision_tg_ctl`` renders the plist from the same plan and writes + (re)loads it.
``drift._check_tg_ctl`` re-renders and diffs.

Invariants
----------
- **Byte-exact plist.** ``render_plist`` emits ``plistlib`` XML with ``sort_keys=False`` so the
  key order matches the hand-created, WORKING live plist on the CTO's machine. With sorted keys
  ``rig apply`` would spuriously rewrite the live file every run (NOT idempotent). Key order is
  load-bearing for the no-op contract — do not reorder the payload dict.
- **gui-domain (re)load.** apply (re)loads the agent via ``launchctl bootout``/``bootstrap``
  in the per-user ``gui/<uid>`` domain (the modern replacement for ``load``/``unload``), so a
  changed plist is picked up without a reboot.
- **Stale predecessor removal.** The dead predecessor service ``com.ultra.codex-tg-bot`` is
  ``bootout``'d and its plist removed (timestamped backup) on reconcile.

This is a per-MACHINE concern (one inbound daemon per machine), so the ``tg_ctl:`` block
belongs in the GLOBAL layer (``~/.config/rig/config.yaml``), like ``harness``/``tmux``/
``git_hooks`` — NOT a committed repo ``rig.yaml``.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

# The boot launchd label (macOS). Reverse-DNS per Apple convention; one identity for the plist
# Label + filename stem so install/drift/remove all key off it. Distinct from the tmux-boot
# label (ai.hyperide.tmux-boot) so the two agents never collide.
DEFAULT_BOOT_LABEL = "ai.hyperide.tg-ctl"

# The dead predecessor service rig must tear down on reconcile (the old codex-tg-bot daemon
# that tg-ctl replaced). bootout + remove its plist (with a timestamped backup).
STALE_PREDECESSOR_LABEL = "com.ultra.codex-tg-bot"

# Default tg-ctl install location (HOME-relative; resolved per-machine). The symlink at
# ~/.files/bin/tg-ctl points at the checked-out Bun script; launchd runs it via bun.
DEFAULT_TG_CTL_PATH = "~/.files/bin/tg-ctl"

# Default tg-cli config dir (honoring $TG_CTL_CONFIG_DIR). The launchd logs land here next to
# the daemon's own .env/config.yaml so everything tg-ctl is in one place.
DEFAULT_CONFIG_DIR = "~/.config/tg-cli"

# The launchd log basenames (under the config dir). Match the live, working plist.
OUT_LOG_NAME = "launchd.tg-ctl.out.log"
ERR_LOG_NAME = "launchd.tg-ctl.err.log"

# Common bun install locations, checked when PATH resolution fails (a non-interactive
# `rig apply` env may have a bare PATH). Ordered: the user's ~/.bun, Apple-silicon brew,
# Intel brew. ``~`` is expanded against the resolved HOME at render time.
_BUN_FALLBACK_PATHS = ("~/.bun/bin/bun", "/opt/homebrew/bin/bun", "/usr/local/bin/bun")

# The PATH the daemon runs with — a sane login PATH that finds bun, brew tools, the user's
# ~/.local/bin, and the system dirs. Matches the live working plist. ``{bun_dir}`` is the
# directory of the resolved bun binary so the daemon can re-exec bun if needed.
_DAEMON_PATH_TEMPLATE = "{bun_dir}:/opt/homebrew/bin:{home}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


@dataclass(frozen=True)
class TgCtlPlan:
    """The desired tg-ctl LaunchAgent state, fully resolved. Pure data, no I/O."""

    home: Path
    enabled: bool
    boot_enabled: bool
    boot_label: str
    bun_path: Path
    tg_ctl_path: Path
    config_dir: Path

    # ── resolved artifact paths ──────────────────────────────────────────────────────
    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{self.boot_label}.plist"

    @property
    def stale_plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{STALE_PREDECESSOR_LABEL}.plist"

    @property
    def out_log_path(self) -> Path:
        return self.config_dir / OUT_LOG_NAME

    @property
    def err_log_path(self) -> Path:
        return self.config_dir / ERR_LOG_NAME

    @property
    def working_directory(self) -> Path:
        # the daemon runs from the dir holding the tg-ctl script (matches the live plist).
        return self.tg_ctl_path.parent

    @property
    def daemon_path_env(self) -> str:
        return _DAEMON_PATH_TEMPLATE.format(bun_dir=str(self.bun_path.parent), home=str(self.home))

    # ── the generated LaunchAgent plist ──────────────────────────────────────────────
    def render_plist(self) -> str:
        """The rig-owned ``ai.hyperide.tg-ctl.plist`` XML.

        plistlib gives idiomatic, escape-safe XML (no hand-rolled string concat / injection).
        ``sort_keys=False`` is LOAD-BEARING: it preserves the insertion order below, matching
        the hand-created, WORKING live plist byte-for-byte so ``rig apply`` is a true no-op
        against it (sorted keys would reorder Label/ProgramArguments/EnvironmentVariables/… and
        force a spurious rewrite every run). Do NOT reorder this dict.
        """
        import plistlib

        payload = {
            "Label": self.boot_label,
            "ProgramArguments": [str(self.bun_path), str(self.tg_ctl_path), "run"],
            "WorkingDirectory": str(self.working_directory),
            "EnvironmentVariables": {
                "PATH": self.daemon_path_env,
                "HOME": str(self.home),
            },
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "StandardOutPath": str(self.out_log_path),
            "StandardErrorPath": str(self.err_log_path),
        }
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")


def _resolve_bun_bin(home: Path) -> Path:
    """The bun binary path for the LaunchAgent: prefer PATH, else the first existing common
    location (~/.bun, Apple-silicon /opt/homebrew, Intel /usr/local). Falls back to ~/.bun/bin
    when none exist (so the plist is still well-formed). HOME-relative fallbacks expand against
    the resolved ``home`` so a test tmp HOME never points at the real ~/.bun.
    """
    found = shutil.which("bun")
    if found:
        return Path(found)
    for cand in _BUN_FALLBACK_PATHS:
        p = _expand_home(cand, home)
        if p.exists():
            return p
    return _expand_home(_BUN_FALLBACK_PATHS[0], home)


def _expand_home(p: str | Path, home: Path) -> Path:
    """Expand a ``~``/``~/...`` path against the resolved ``home`` (never the real one)."""
    s = str(p)
    if s == "~":
        return home
    if s.startswith("~/"):
        return home / s[2:]
    return Path(s)


def build_tg_ctl(
    *,
    home: Path,
    enabled: bool = True,
    boot: bool = True,
    boot_label: str = DEFAULT_BOOT_LABEL,
    bun_path: str | Path | None = None,
    tg_ctl_path: str | Path = DEFAULT_TG_CTL_PATH,
    config_dir: str | Path = DEFAULT_CONFIG_DIR,
) -> TgCtlPlan:
    """Resolve the desired :class:`TgCtlPlan` from the (already-validated) tg_ctl config block.

    ``home`` is the resolved HOME (the caller passes ``Path.home()`` or a test tmp HOME). The
    bun path is discovered (``which bun`` -> ~/.bun fallback) unless an explicit ``bun_path`` is
    given. ``tg_ctl_path`` / ``config_dir`` are HOME-relative by default and honor an explicit
    override (the runner passes the ``$TG_CTL_CONFIG_DIR``-resolved dir when set).
    """
    resolved_bun = _expand_home(bun_path, home) if bun_path else _resolve_bun_bin(home)
    return TgCtlPlan(
        home=home,
        enabled=bool(enabled),
        boot_enabled=bool(boot),
        boot_label=str(boot_label),
        bun_path=resolved_bun,
        tg_ctl_path=_expand_home(tg_ctl_path, home),
        config_dir=_expand_home(config_dir, home),
    )
