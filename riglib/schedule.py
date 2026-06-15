"""Model-freshness schedule — PURE planning of the daily cron artifact.

The CTO's #3685 direction: rig should install a cron that runs the agent-tools model-currency
checker **once a day, e.g. at noon**, and on `rig init` AND `rig apply` should **check whether
the cron is installed and install it if missing** ("проверять есть ли крон и устанавливать").

Cross-platform:
  - **macOS → launchd**. A ``~/Library/LaunchAgents/<label>.plist`` loaded via ``launchctl``
    is the native, supported "cron" on macOS (cron is deprecated/unmanaged there).
  - **Linux → crontab**. A single managed crontab line, fenced by a SENTINEL comment so it is
    idempotent (re-apply finds it by sentinel) and removable.

This module is **stdlib-only and effect-free**: it computes WHAT the artifact should be (the
plist XML / the crontab line, the install paths, the platform branch). The effectful
``launchctl load`` / ``crontab`` writes live in ``actions/runner.py`` (the one-engine apply
path), and drift detection diffs the desired artifact against disk in ``drift.py``. Three
consumers (plan, apply, drift) share THIS so the desired state never drifts between them.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# The default daily run time — NOON, per the CTO. Hour/minute the launchd plist + crontab line
# both encode. Override via rig.yaml ``models.schedule.time: "HH:MM"``.
DEFAULT_HOUR = 12
DEFAULT_MINUTE = 0

# The launchd label / sentinel identity. Reverse-DNS per Apple convention; the SAME string is
# the plist Label, the plist filename stem, and the crontab sentinel — one identity across
# platforms so install/drift/remove all key off it.
DEFAULT_LABEL = "ai.hyperide.model-freshness"

# The crontab sentinel comment. A managed line is the comment line + the cron line; the
# comment lets a re-apply find (and an uninstall remove) exactly rig's line, never a user's.
CRON_SENTINEL_PREFIX = "# rig-managed:"


@dataclass(frozen=True)
class SchedulePlan:
    """The desired daily-schedule state, platform-resolved. Pure data, no I/O.

    ``platform`` is "launchd" (macOS) or "crontab" (Linux/other). ``checker_cmd`` is the
    fully-resolved argv the schedule runs (``python3 .../lib/checker/model_freshness.py``).
    """

    platform: str  # "launchd" | "crontab"
    label: str
    hour: int
    minute: int
    checker_cmd: list[str]
    plist_path: Path | None = None  # launchd only
    log_path: Path | None = None  # launchd only (StandardOut/ErrorPath)

    @property
    def human_time(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    # ── launchd artifact ──────────────────────────────────────────────────────────────
    def plist_xml(self) -> str:
        """The launchd plist XML for a daily StartCalendarInterval run at hour:minute."""
        args = "".join(f"    <string>{_xml_escape(a)}</string>\n" for a in self.checker_cmd)
        log = self.log_path or (Path.home() / "Library" / "Logs" / f"{self.label}.log")
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "<dict>\n"
            "  <key>Label</key>\n"
            f"  <string>{_xml_escape(self.label)}</string>\n"
            "  <key>ProgramArguments</key>\n"
            "  <array>\n"
            f"{args}"
            "  </array>\n"
            "  <key>StartCalendarInterval</key>\n"
            "  <dict>\n"
            "    <key>Hour</key>\n"
            f"    <integer>{self.hour}</integer>\n"
            "    <key>Minute</key>\n"
            f"    <integer>{self.minute}</integer>\n"
            "  </dict>\n"
            "  <key>StandardOutPath</key>\n"
            f"  <string>{_xml_escape(str(log))}</string>\n"
            "  <key>StandardErrorPath</key>\n"
            f"  <string>{_xml_escape(str(log))}</string>\n"
            "  <key>RunAtLoad</key>\n"
            "  <false/>\n"
            "</dict>\n"
            "</plist>\n"
        )

    # ── crontab artifact ──────────────────────────────────────────────────────────────
    def crontab_lines(self) -> list[str]:
        """The two managed crontab lines: the sentinel comment + the schedule line.

        The sentinel embeds the label so a re-apply finds rig's exact pair (idempotent) and
        an uninstall removes only it. The cron schedule is ``MIN HOUR * * *`` (daily).
        """
        cmd = " ".join(_sh_quote(a) for a in self.checker_cmd)
        return [
            f"{CRON_SENTINEL_PREFIX} {self.label}",
            f"{self.minute} {self.hour} * * * {cmd}",
        ]


def detect_platform() -> str:
    """"launchd" on macOS, "crontab" elsewhere. Override with ``RIG_SCHEDULE_PLATFORM``
    (test seam — lets the launchd/crontab branches be exercised on either host)."""
    forced = os.environ.get("RIG_SCHEDULE_PLATFORM", "").strip().lower()
    if forced in ("launchd", "crontab"):
        return forced
    return "launchd" if sys.platform == "darwin" else "crontab"


def default_checker_path(agent_tools_source: str | Path | None) -> Path | None:
    """The model_freshness.py path inside the given agent-tools checkout (None if unknown).

    rig consumes agent-tools READ-ONLY; the schedule runs the checker FROM that checkout, so
    the command path is anchored on the resolved ``agent_tools_source``.
    """
    if not agent_tools_source:
        return None
    return Path(agent_tools_source) / "lib" / "checker" / "model_freshness.py"


def build_schedule(
    *,
    checker_path: Path,
    hour: int = DEFAULT_HOUR,
    minute: int = DEFAULT_MINUTE,
    label: str = DEFAULT_LABEL,
    platform: str | None = None,
    python: str | None = None,
) -> SchedulePlan:
    """Resolve the desired :class:`SchedulePlan` for this machine.

    ``python`` defaults to ``python3`` (resolved on PATH at run time by launchd/cron, not
    pinned to rig's interpreter — the checker is stdlib-first and runs under any python3).
    """
    plat = platform or detect_platform()
    py = python or "python3"
    cmd = [py, str(checker_path)]
    plist_path = None
    log_path = None
    if plat == "launchd":
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        log_path = Path.home() / "Library" / "Logs" / f"{label}.log"
    return SchedulePlan(
        platform=plat,
        label=label,
        hour=hour,
        minute=minute,
        checker_cmd=cmd,
        plist_path=plist_path,
        log_path=log_path,
    )


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _sh_quote(s: str) -> str:
    """Single-quote a crontab argv token (crontab runs the line via /bin/sh).

    `%` is NOT in the safe set: in a crontab line a literal `%` is special (the text after the
    first unescaped `%` is fed to the command on stdin, and `%` becomes a newline). A token
    containing `%` therefore must be quoted/escaped, never passed bare.
    """
    if s and all(c.isalnum() or c in "@+=:,./-_" for c in s):
        return s
    # single-quote the token AND escape any `%` (crontab treats `%` specially even inside
    # single quotes — it must be backslash-escaped).
    return "'" + s.replace("'", "'\\''").replace("%", "\\%") + "'"
