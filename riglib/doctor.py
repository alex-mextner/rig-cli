"""Doctor — detect (and optionally install) the tools rig + agent-tools need.

A "dependency" here is an external CLI binary that agent-tools content relies on
(gitleaks for secret-scan, gh for ship/CI, git always, etc.) plus rig's own optional
runtime bits (pyyaml, textual, rich). For each, doctor reports present/absent and — when the
OS package manager is known — the exact install command. In ``--yes`` mode it runs the
install commands non-interactively; otherwise it only prints them (never a destructive
install without confirmation).

Package name varies per manager, so each dependency carries a per-manager name map.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .probes import ProbeResult

from .detect import OsInfo, detect_os, install_command


@dataclass
class Dependency:
    name: str  # the binary / module to probe
    why: str  # one-line "what needs it"
    kind: str = "binary"  # "binary" | "python"
    required: bool = False  # required vs optional (optional = nice-to-have)
    # per-manager package name; falls back to ``name`` when a manager is absent here
    pkg: dict[str, str] = field(default_factory=dict)


# The dependency surface for rig + the agent-tools content it applies.
DEPENDENCIES: list[Dependency] = [
    Dependency("git", "version control + all git-hooks / dispatcher", required=True),
    Dependency("python3", "rig runtime", required=True),
    Dependency(
        "pyyaml",
        "parse/serialize rig.yaml (config cascade)",
        kind="python",
        required=True,
        pkg={"brew": "", "apt": "python3-yaml", "dnf": "python3-pyyaml", "pacman": "python-yaml"},
    ),
    Dependency(
        "gh",
        "CI gates (ship, review-threads, screenshots) + repo ops",
        pkg={"brew": "gh", "apt": "gh", "dnf": "gh", "pacman": "github-cli", "zypper": "gh"},
    ),
    Dependency(
        "gitleaks",
        "secret-scan CI gate + the secret-scan git-hook fragment",
        pkg={"brew": "gitleaks", "apt": "gitleaks", "dnf": "gitleaks", "pacman": "gitleaks"},
    ),
    Dependency(
        "lefthook",
        "per-repo git-hook templates (committed, team-wide mechanism)",
        pkg={"brew": "lefthook", "apt": "lefthook", "pacman": "lefthook"},
    ),
    Dependency(
        "textual",
        "the interactive setup wizard (rig init TUI) — a CORE dep, ships with rig",
        kind="python",
        required=True,
        pkg={"brew": "", "apt": "", "dnf": "", "pacman": "python-textual"},
    ),
    Dependency(
        "rich",
        "the `rig stats show --format tui` report — a CORE dep, ships with rig",
        kind="python",
        required=True,
        # All entries empty: always install via pip/uv into rig's own interpreter (sys.executable),
        # not via the distro package manager. A distro `python3-rich` installs into the system
        # Python, which is WRONG when rig runs inside a pipx/uv-tool venv.
        pkg={"brew": "", "apt": "", "dnf": "", "pacman": ""},
    ),
    # The daily model-freshness schedule (models:) is provisioned via the platform-native
    # scheduler: launchd (launchctl) on macOS, crontab on Linux. Both ship with the OS; this
    # entry surfaces the one rig will actually use so a stripped container without crontab is
    # flagged. The probe is for the scheduler binary the CURRENT platform uses.
    Dependency(
        "launchctl" if sys.platform == "darwin" else "crontab",
        "model-freshness daily schedule (models:) — launchd on macOS, crontab on Linux",
    ),
]


@dataclass
class DepStatus:
    dep: Dependency
    present: bool
    location: str | None
    install_cmd: list[str] | None  # the command to install it (None when unknown)
    # set only for a python-kind dep whose install_cmd targets THIS interpreter directly
    # (no system package mapping) AND that interpreter is PEP-668 externally-managed — the
    # exact shape that makes install_cmd above silently fail on a symlink/dev checkout install.
    pep668_fallback: list[str] | None = None


@dataclass
class DoctorReport:
    os: OsInfo
    statuses: list[DepStatus] = field(default_factory=list)
    #: opt-in ACTIVATION probes (``RIG_OMP_PROBE=1``) — filled by the doctor COMMAND
    #: (``diagnose()`` itself stays a pure offline report builder; the probe spawns a real
    #: model turn and must never ride along with every diagnose() caller).
    probes: list["ProbeResult"] = field(default_factory=list)

    @property
    def missing_required(self) -> list[DepStatus]:
        return [s for s in self.statuses if not s.present and s.dep.required]

    @property
    def missing_optional(self) -> list[DepStatus]:
        return [s for s in self.statuses if not s.present and not s.dep.required]


# riglib/tui/app.py's `_set_controls_enabled` calls `self.refresh_bindings()`, a Textual
# App/Screen method added between 0.60 and 0.63 — that alone would justify 0.63. But
# `compose()` also passes `tooltip=` to `Button(...)`, a constructor kwarg added later still
# (confirmed absent in 0.65.2, present starting 0.66.0 — a review finding: an earlier version
# of this floor was justified by refresh_bindings ALONE, so a user on textual 0.63–0.65 would
# still pass this gate and then crash `compose()` at wizard LAUNCH instead of mid-Apply, the
# same class of bug this floor exists to eliminate, just relocated). The EFFECTIVE floor is
# the max of every new Textual API surface introduced alongside it, not just the first one
# found. Keep this in sync with the `textual>=0.66` floor in pyproject.toml (see its comment
# for the full story, rig-cli#292 review history). Single source of truth: `riglib.cli`
# imports `textual_too_old` from here rather than duplicating the version check.
_TEXTUAL_MIN_VERSION = (0, 66)

# PEP 440 suffix grammar, ASCII-only (`[0-9]`, never the bare `\d` — see `textual_too_old`'s
# docstring for why). Each piece is independently optional; combined via named groups so
# `textual_too_old` can ask "was a pre/dev marker actually PRESENT" from the STRUCTURED
# match instead of re-searching the raw string with a permissive pattern — a review finding
# caught that a naive `re.search` for a pre-release marker over the whole suffix false-flags
# a LOCAL version label that merely happens to CONTAIN one of the single-letter markers as a
# substring (e.g. "+ubuntu-20.04" contains "b", one of this grammar's pre-release letters).
_TEXTUAL_PRERELEASE_RE_SRC = r"[.\-_]?(?:alpha|beta|preview|rc|a|b|c|pre)[.\-_]?[0-9]*"
_TEXTUAL_DEVRELEASE_RE_SRC = r"[.\-_]?dev[.\-_]?[0-9]*"
# "post"/"rev"/"r" are PEP 440's canonical AND legacy spellings for a post-release, plus the
# bare "-N" implicit form — all three sort AFTER their base release.
_TEXTUAL_POSTRELEASE_RE_SRC = r"(?:[.\-_]?(?:post|rev|r)[.\-_]?[0-9]*|-[0-9]+)"
_TEXTUAL_LOCAL_RE_SRC = r"\+[a-zA-Z0-9]+(?:[.\-_][a-zA-Z0-9]+)*"
# Group ORDER matters here, not just presence: PEP 440's canonical grammar anchors these as
# release[pre][post][dev][local] — post BEFORE dev (`packaging.version.Version` accepts a dev
# release OF a post release, e.g. "0.66.post1.dev1", spelled in exactly that order). A review
# finding (Codex + k3, round 16, independently): an earlier version of this regex anchored
# pre?dev?post?local?, so a well-formed "0.66.post1.dev1" failed `fullmatch` — the `dev` group
# had already been tried and left unconsumed by the time `post` needed the text `dev` occupies
# — and fell through to fail-closed on a version that both satisfies the floor AND is not
# remediable (the printed `textual>=0.66` upgrade command is already satisfied, so the "too
# old" verdict can never self-resolve). Matches `packaging.version.Version`'s own accepted
# grammar now, not just "one recognized modifier at a time".
_TEXTUAL_VERSION_SUFFIX_RE = re.compile(
    rf"^(?P<pre>{_TEXTUAL_PRERELEASE_RE_SRC})?"
    rf"(?P<post>{_TEXTUAL_POSTRELEASE_RE_SRC})?"
    rf"(?P<dev>{_TEXTUAL_DEVRELEASE_RE_SRC})?"
    rf"(?P<local>{_TEXTUAL_LOCAL_RE_SRC})?$",
    re.IGNORECASE,
)


def textual_too_old() -> bool:
    """True when ``textual`` is IMPORTABLE but not confidently new enough (``_TEXTUAL_MIN_VERSION``).

    ``find_spec`` (``_python_present`` below) only proves textual is importable, not that it
    is new enough — a distro package (pacman's `python-textual`) or a venv left over from
    before the floor was bumped imports fine and passes every presence check, then the wizard
    crashes mid-Apply the moment `_set_controls_enabled` calls a method that version doesn't
    have (the exact rig-cli#292 failure the floor bump targets).

    Callers fall into two groups (a review finding: an earlier version of this docstring
    claimed ALL callers were in the first group, which is wrong for the second — a
    load-bearing inaccuracy on a single-source-of-truth gate):
    - ``_python_present``, ``_tui_importable``, ``_missing_tui_deps``, and
      ``_targets_this_interpreter`` all check ``find_spec("textual")`` FIRST and only reach
      this function once that already succeeded — for them, "can't determine the version"
      (dist-info missing, unreadable, or unparseable) never means "genuinely absent" (that's
      `find_spec`'s job); it means "importable via something unusual (a `.pth`-injected
      package, a raw source checkout on `PYTHONPATH`) that we can't vouch for".
    - ``_versioned_install_spec`` calls this function with NO `find_spec` guard, and
      deliberately relies on ``PackageNotFoundError`` failing closed for a genuinely absent
      textual too — that's precisely what makes a FRESH-install command carry the version
      floor (`textual>=0.66`, not a bare `textual` a resolver could satisfy some other way).

    Either way, every metadata-reading failure (`PackageNotFoundError`, corrupt/non-UTF-8
    ``METADATA``, an oversized numeric component, etc.) fails CLOSED (``True``): refusing to
    launch the wizard (a soft degrade to a preview) is a far cheaper mistake than silently
    launching one that then crashes mid-Apply, and a versioned install spec is the correct
    "make sure this is new enough" command either way.

    Version comparison handles PEP 440's EPOCH prefix (``"N!"``, e.g. ``"1!0.1"``) first and
    absolutely: any epoch above the floor's implicit epoch 0 wins UNCONDITIONALLY, regardless
    of how small the release segment or suffix looks afterward — real packages essentially
    never use epochs, but getting this specific case wrong (rather than just refusing to
    vouch for it) would be an active regression, not merely an unhandled corner: an earlier,
    less-strict parser happened to get epoch right BY ACCIDENT, and tightening the suffix
    validation below (to close a real gap) silently broke that accident.

    The rest of the comparison is ASCII-digit-only (``[0-9]+``, not ``\\d+`` — Python's bare
    ``\\d`` and ``int()`` both accept non-ASCII Unicode decimal digits, so a hand-crafted/
    corrupt metadata value could otherwise smuggle a passing comparison past this gate) and
    validates the ENTIRE suffix after the release segment against
    ``_TEXTUAL_VERSION_SUFFIX_RE`` — a
    review finding: an earlier version only validated the suffix when the release segment
    (major, minor, micro, ...) was EXACTLY the floor, so a genuinely malformed version whose
    numeric PREFIX happened to compare above the floor (e.g. ``"999not-a-version"``,
    ``"0.66.1garbage"``) passed straight through unvalidated. The suffix is now validated
    unconditionally: only a RECOGNIZED PEP 440 modifier (pre-release, dev-release,
    post-release, local version, any combination, or none) is accepted; anything else fails
    closed regardless of how the numeric release segment compares. When the release segment
    is EXACTLY the floor (after PEP 440's trailing-zero padding — ``"0.66"`` ≡ ``"0.66.0"``),
    a present pre-release or dev-release marker still fails closed (PEP 440 orders a
    pre/dev-release strictly BEFORE its final release — ``0.66.0rc1 < 0.66.0`` — so it may
    not yet contain a feature like `Button(tooltip=...)` added late in that release's cycle);
    a post-release or local-version marker there is fine (both sort strictly AFTER).

    Known, deliberately-accepted limitation — the ONE case where a wrong verdict here is NOT
    merely a preview screen: ``find_spec`` (module RESOLUTION) and
    ``importlib.metadata.version`` (distribution METADATA lookup) are separate mechanisms
    that CAN disagree in a sufficiently unusual environment — e.g. an old textual SOURCE
    checkout earlier on ``PYTHONPATH`` shadowing import resolution while an unrelated,
    properly-installed newer textual's dist-info is also discoverable elsewhere on
    ``sys.path``. That would make this function report "new enough" for a distribution that
    isn't actually the one that gets imported — a review finding (Codex, round 18) caught an
    earlier version of this paragraph claiming EVERY wrong verdict here "never" crashes,
    which overclaims for exactly this shadowed case: this function fails OPEN here (reports
    False for the wrong module), not closed, so the wizard DOES launch and can still crash on
    the shadowed old module's missing API surface. Closing that gap fully would require
    importing textual itself to introspect the live module — which conflicts with this
    check's other explicit design goal (a cheap, side-effect-free probe that never drags
    textual into the process just to check it). Accepted as out of scope: this requires an
    actively/unusually misconfigured environment (two DIFFERENT textual installations
    simultaneously resolvable via different mechanisms on the same ``sys.path``) far beyond
    the realistic "stale venv / distro package" population rig-cli#292 targets. EVERY OTHER
    failure mode this function handles (malformed metadata, absent textual, unparseable
    versions, etc.) does fail closed and costs only a preview screen — this shadowing case is
    the sole, deliberately-accepted exception to that guarantee.
    """
    import importlib.metadata

    # EVERYTHING below — the metadata read AND the parse — is inside one try/except: a
    # review finding on an earlier version caught that only the metadata read was guarded,
    # so `int(match.group())` on a maliciously/corruptly oversized all-digit run (Python
    # rejects integer-string conversions past a length limit since the CVE-2020-10735 fix)
    # raised an uncaught ValueError straight out of this "must never throw" predicate. One
    # blanket handler is also simply more robust against whatever else `importlib.metadata`
    # or this parse could raise in the future than an ever-growing exception allowlist.
    try:
        # `Distribution.version` reads the dist-info's `Version:` header via
        # `email.message.Message.__getitem__`, which returns `None` for a MISSING header
        # rather than raising — a corrupt/hand-crafted dist-info with no `Version:` field
        # would otherwise crash the parse below with an uncaught AttributeError. Coerce to
        # "" so that falls through to the same "can't parse" → fail-closed path below.
        raw = (importlib.metadata.version("textual") or "").strip()
        # `packaging.version.Version`'s own grammar (what pip/uv actually use to decide
        # whether `textual>=0.66` is satisfied) anchors with `^\s*…\s*$` — it tolerates
        # surrounding whitespace in the `Version:` header. A review finding (Codex + GLM,
        # round 21, independently): email-parsed header VALUES can preserve a stray
        # leading/trailing space (e.g. a hand-edited or non-standard-toolchain METADATA
        # file), and without stripping it, `"0.66 "` fails every regex below (`fullmatch`
        # sees a trailing space no branch matches) and this reports "too old" for a version
        # that both satisfies the floor AND is not remediable (the printed `textual>=0.66`
        # is already satisfied per pip) — the SAME packaging-tolerance divergence class the
        # `v`-prefix fix above closes, just a different form of it. Strip once, up front, so
        # every check below sees the same normalized string regardless of source.
        #
        # PEP 440 permits an optional leading "v"/"V" on the public version identifier
        # itself (`packaging.version.Version("v0.66.0")` normalizes to "0.66.0" and
        # satisfies `textual>=0.66`) — strip it FIRST, before epoch, so "v1!0.1" and
        # "v0.66" both parse the same as their un-prefixed forms. A review finding
        # (Codex + k3, round 17, independently): real PyPI/setuptools-built dist-info
        # essentially never carries this prefix, but a hand-rolled or very old toolchain's
        # `Version:` header could, and rejecting it recreates the exact non-remediable
        # loop this parser exists to close (a printed `textual>=0.66` upgrade command that
        # pip/uv already considers satisfied, for a version that in fact clears the floor).
        if raw[:1] in ("v", "V"):
            raw = raw[1:]
        # PEP 440's EPOCH prefix ("N!", e.g. "1!0.1") sorts BEFORE and ABSOLUTELY above
        # everything else in the comparison — any non-zero epoch outranks the floor's
        # implicit epoch 0, REGARDLESS of how small the release segment or what the suffix
        # says. A review finding: an earlier, less-strict version of this parser happened
        # to get this right BY ACCIDENT (an unrecognized "!" just fell through to "strictly
        # above floor, no suffix check needed" before this function validated suffixes
        # unconditionally) — hardening the suffix validation to close the malformed-garbage
        # gap (see the release-segment comment below) silently broke that accident, so
        # epoch now needs its own explicit, correct handling rather than relying on it.
        #
        # A non-zero epoch does NOT short-circuit straight to "new enough" though — a review
        # finding (round 16): doing that BEFORE validating the rest of the string let a
        # corrupt header like "1!0.1garbage" report "new enough" on the epoch digit alone,
        # even though everything after it fails to parse. `epoch_above_floor` just records
        # the epoch's own verdict; it's only trusted once the release+suffix below ALSO
        # parses as a well-formed PEP 440 remainder (same fail-closed contract as every
        # other malformed-input path here).
        epoch_match = re.match(r"[0-9]+!", raw)
        epoch_above_floor = False
        if epoch_match is not None:
            epoch_above_floor = int(epoch_match.group()[:-1]) > 0
            raw = raw[epoch_match.end() :]  # epoch 0 == no epoch; strip and fall through
        # Extract the FULL leading release segment (major.minor.micro...) as one contiguous
        # digits-and-dots run from the START of the string — a review finding: an earlier
        # version processed dot-segments one at a time and stopped at the first non-numeric
        # ONE, which correctly captured a partial segment like "1rc1" (micro=1) but never
        # validated what came after it, so a release segment that merely LOOKED
        # strictly-above-floor (its leading digit run extracted from otherwise-garbage
        # metadata) passed through with no suffix check at all.
        release_match = re.match(r"[0-9]+(?:\.[0-9]+)*", raw)
        if release_match is None:
            return True  # doesn't even start with an ASCII digit — can't parse at all
        version_tuple = tuple(int(p) for p in release_match.group().split("."))
        suffix = raw[release_match.end() :]
        suffix_match = _TEXTUAL_VERSION_SUFFIX_RE.fullmatch(suffix)
        if suffix_match is None:
            return True  # unrecognized suffix — can't vouch for it, regardless of the
            # release segment's own comparison against the floor
        if epoch_above_floor:
            return False  # confirmed well-formed above — the epoch alone now wins outright
        # PEP 440 pads a shorter release segment with trailing zeros for comparison
        # ("0.66" == "0.66.0") — pad both tuples to the same length before comparing, or a
        # bare Python tuple compare would treat the shorter one as "less than" regardless
        # of the padding value (`(0, 66) < (0, 66, 0)` is True in raw Python, which is
        # wrong here: they're EQUAL per PEP 440).
        pad_len = max(len(version_tuple), len(_TEXTUAL_MIN_VERSION))
        padded_version = version_tuple + (0,) * (pad_len - len(version_tuple))
        padded_floor = _TEXTUAL_MIN_VERSION + (0,) * (pad_len - len(_TEXTUAL_MIN_VERSION))
        if padded_version != padded_floor:
            return padded_version < padded_floor
        # Exactly the floor (after padding): the suffix is already confirmed RECOGNIZED
        # above — only its PRE-RELEASE/DEV-RELEASE class matters now (sorts BEFORE the
        # final release; a POST-release or LOCAL-version marker sorts AFTER and is fine).
        # Read this from the STRUCTURED match's named groups, not a fresh `re.search` over
        # the raw suffix — a review finding: a single-letter pre-release marker (bare "a"/
        # "b"/"c") matched as a SUBSTRING of an unrelated local-version label (e.g. "b" in
        # "+ubuntu-20.04") when searched independently; the named-group match from the
        # SAME regex that already validated the whole suffix can't misfire that way.
        #
        # A bare dev marker (no accompanying post) sorts BEFORE the final release — a dev
        # PREVIEW of 0.66 isn't confirmed to be 0.66 yet. But a dev release OF A POST release
        # ("0.66.post1.dev1", valid per the group-order fix above) sorts AFTER the base
        # release — PEP 440 precedence is release < release.postN.devM < release.postN, so
        # it's already past the base-release doorway even before the post fully resolves.
        # `dev` therefore only counts as "not yet confirmed" when `post` is ABSENT.
        if suffix_match.group("pre") or (suffix_match.group("dev") and not suffix_match.group("post")):
            return True
        return False
    except Exception:  # noqa: BLE001 — see the docstring: every failure here fails closed
        return True


def _python_present(module: str) -> bool:
    # pyyaml's import name is "yaml"
    import_name = {"pyyaml": "yaml"}.get(module, module)
    if importlib.util.find_spec(import_name) is None:
        return False
    # a present-but-too-old textual can't actually run the wizard — report it the same as
    # absent, so doctor's "MISSING → install command" path is what the user acts on.
    if module == "textual" and textual_too_old():
        return False
    return True


def diagnose(os_info: OsInfo | None = None) -> DoctorReport:
    os_info = os_info or detect_os()
    report = DoctorReport(os=os_info)
    # computed once, not per-dep — same interpreter for every python-kind dep this call.
    is_ext_managed = externally_managed()
    for dep in DEPENDENCIES:
        if dep.kind == "python":
            present = _python_present(dep.name)
            location = "importable" if present else None
        else:
            loc = shutil.which(dep.name)
            present = loc is not None
            location = loc
        install_cmd = None if present else _install_cmd_for(dep, os_info)
        fallback = None
        # `_targets_this_interpreter` is the SAME function `_install_cmd_for` branches on
        # (not a re-derived condition compared by value) — a review finding: an earlier,
        # separately-maintained copy of this condition here drifted out of sync with a later
        # change to `_install_cmd_for`'s own branch.
        targets_this_interpreter = _targets_this_interpreter(dep, os_info.package_manager)
        if not present and is_ext_managed and install_cmd is not None and targets_this_interpreter:
            fallback = break_system_packages_command(dep.name)
        report.statuses.append(
            DepStatus(
                dep=dep,
                present=present,
                location=location,
                install_cmd=install_cmd,
                pep668_fallback=fallback,
            )
        )
    return report


def _python_install_command(package: str) -> list[str]:
    """Install ``package`` into rig's OWN interpreter (``sys.executable``).

    Prefers ``uv pip install`` — the toolchain rig users standardize on — targeting the exact
    interpreter rig runs under so the wizard's ``textual`` lands where rig will import it. This
    installs cleanly when that interpreter is a uv-/pipx-managed venv (the recommended install
    shape). It does NOT magically bypass PEP-668: on an externally-managed *system* Python both
    uv and a bare pip refuse — there the real fix is to install rig itself into a managed env
    (`pipx install rig-cli` / `uv tool install rig-cli`), not to force a system-wide install.
    Falls back to ``python -m pip install`` only when ``uv`` is absent — no worse than before.
    That fallback adds ``--user`` UNLESS ``sys.executable`` is itself a venv interpreter
    (``sys.prefix != sys.base_prefix`` — the same idiom ``externally_managed()`` uses): pip
    hard-refuses ``--user`` inside a venv ("User site-packages are not visible in this
    virtualenv"), so keeping it unconditionally would hand a pipx-managed install (rig's own
    RECOMMENDED shape — ``pipx install rig-cli`` — and every ``uv tool install`` too) a command
    that fails outright the moment ``uv`` isn't ALSO on PATH (review finding, ``textual_upgrade_command``
    surfaces this fallback directly as "the fix" for a present-but-too-old textual, not merely
    as a rarely-seen last resort). Outside a venv, ``--user`` is still needed to avoid requiring
    root against a system Python.

    ``package`` is always ``dep.name`` from ``DEPENDENCIES`` (never a bare import name) — every
    current python-kind entry's ``name`` already IS its correct pip distribution name (confirmed
    empirically: ``uv pip install --break-system-packages --dry-run`` resolves cleanly for each).
    A future python dep whose import name differs from its distribution name (e.g. Pillow imports
    as ``PIL``) would need its OWN pip name here, not its import name — this function has no way
    to tell the difference on its own.
    """
    if shutil.which("uv"):
        return ["uv", "pip", "install", "--python", sys.executable, package]
    if sys.prefix != sys.base_prefix:
        return [sys.executable, "-m", "pip", "install", package]
    return [sys.executable, "-m", "pip", "install", "--user", package]


def _versioned_install_spec(name: str) -> str:
    """The pip/uv package ARG for ``name`` — usually just the bare name, but a
    present-but-too-old ``textual`` needs a version-constrained spec (``textual>=0.66``).

    Review finding (the "remediation loop"): a present-but-old ``textual`` is reported down
    the same "MISSING → run this install command" path as a genuinely absent one (see
    ``_python_present``), but a BARE ``pip install textual`` / ``uv pip install textual``
    against an already-installed 0.55 replies "Requirement already satisfied" / "Audited 1
    package" — rc=0, nothing upgraded. The user re-runs `rig init`, gets the identical
    "missing" message, and is stuck in a loop the printed command can never actually escape.
    A version-constrained spec forces pip/uv to actually resolve and install the newer
    version pip/uv would otherwise skip.
    """
    if name == "textual" and textual_too_old():
        floor = ".".join(str(n) for n in _TEXTUAL_MIN_VERSION)
        return f"textual>={floor}"
    return name


def textual_upgrade_command() -> list[str]:
    """The plain (non-PEP-668-bypass) command to upgrade a present-but-too-old ``textual``
    in THIS interpreter.

    Public counterpart to ``break_system_packages_command``/``_for`` for the
    NON-externally-managed case: a dev-checkout install (``install.sh``'s ``~/.local/bin/rig``
    symlink) on an interpreter that does NOT carry PEP 668's marker (pyenv, the python.org
    macOS installer, asdf — none of them do) still needs a DIRECT interpreter-targeted
    upgrade, not a ``pipx install rig-cli`` / ``uv tool install rig-cli`` reinstall — that
    creates a SEPARATE managed environment that never touches the currently-running
    symlinked checkout at all. A review finding: this branch used to only ever suggest the
    reinstall commands, so the "remediation loop" (the versioned-spec fix elsewhere in this
    module exists to close) survived here — a too-old textual on a non-managed interpreter
    got "too old" as the diagnosis but no command that actually fixes it.
    """
    return _python_install_command(_versioned_install_spec("textual"))


def externally_managed() -> bool:
    """True when THIS interpreter's stdlib carries PEP 668's ``EXTERNALLY-MANAGED`` marker —
    and this interpreter is NOT itself a virtualenv.

    PEP 668 enforcement only applies OUTSIDE a venv (pip's own check exits early under
    ``running_under_virtualenv()`` before ever looking for the marker). A venv's ``sysconfig``
    stdlib path resolves to the BASE interpreter's stdlib (venvs never copy the stdlib), so a
    marker check alone would misfire for rig's own RECOMMENDED install shape — `pipx install
    rig-cli` / `uv tool install rig-cli`, both venvs — whenever the base Python happens to be a
    marker-carrying Homebrew/distro one. That would misdiagnose a genuinely broken pipx install
    as "PEP-668 refuses" and offer a `--break-system-packages --user` fallback that ALSO fails
    inside a venv (`--user` is rejected there), trading a correct diagnosis for a wrong one.
    ``sys.prefix != sys.base_prefix`` is the standard venv-detection idiom (true for venv/virtualenv/
    pipx/uv-tool installs alike) — check it FIRST so a venv always reports False regardless of
    what the base interpreter's stdlib marker says.

    This checks the currently-running interpreter (``sys.executable`` — the same one
    ``_python_install_command`` targets), so no probing of a different ``python3`` is needed.
    """
    if sys.prefix != sys.base_prefix:
        return False
    stdlib = sysconfig.get_paths().get("stdlib")
    if not stdlib:
        return False
    return (Path(stdlib) / "EXTERNALLY-MANAGED").is_file()


def break_system_packages_command(package: str) -> list[str]:
    """The same command ``_python_install_command`` would produce, plus PEP 668's bypass flag.

    Never run automatically (see the docstring above) — only OFFERED as an explicit,
    human-chosen fallback when the plain install is known to fail because this interpreter is
    externally-managed, for someone (like rig's own maintainer) intentionally installing into a
    local dev checkout's system Python rather than switching to a pipx/uv-tool managed install.
    ``package`` is routed through ``_versioned_install_spec`` — see its docstring.
    """
    spec = _versioned_install_spec(package)
    cmd = _python_install_command(spec)
    return cmd[:-1] + ["--break-system-packages", cmd[-1]]


def break_system_packages_command_for(packages: list[str]) -> list[str]:
    """Like ``break_system_packages_command``, but installs several packages in ONE command.

    ``uv pip install`` / ``pip install`` both accept multiple trailing package args, so this
    just widens the single-package command's tail instead of duplicating its shape. Each name
    is routed through ``_versioned_install_spec`` — see its docstring.
    """
    if not packages:
        return []
    specs = [_versioned_install_spec(p) for p in packages]
    cmd = _python_install_command(specs[0])
    return cmd[:-1] + ["--break-system-packages", *specs]


def _targets_this_interpreter(dep: Dependency, mgr: str | None) -> bool:
    """True when installing ``dep`` targets THIS interpreter directly (uv/pip into
    ``sys.executable``), never an OS package manager.

    Single source of truth for TWO call sites that must never drift apart (a review finding:
    they did) — ``_install_cmd_for``'s own branch, and ``diagnose()``'s PEP-668-fallback gate
    (which needs the identical fact to decide whether to compute a ``--break-system-packages``
    alternative). True for two distinct reasons:
    - no system-package mapping exists for ``mgr`` at all (the ordinary case for most
      python-kind deps on most package managers) — nothing else to route through.
    - ``dep`` is textual, genuinely IMPORTABLE (``find_spec`` succeeds — NOT merely "present
      is False", which conflates "too old" with "absent"; see below), and too old: it needs
      THIS running interpreter upgraded, not a system package touch that may not even affect
      the process about to launch the wizard (a stale venv, or a pipx/uv install shadowing an
      OS package).

    The ``find_spec`` check matters: an EARLIER version of this bypass fired on
    ``textual_too_old()`` alone, which ALSO returns ``True`` for a genuinely absent textual
    (`PackageNotFoundError` fails closed) — so it silently bypassed the pacman mapping for
    absent textual too, permanently dead-coding it and regressing pacman users from a working
    `pacman -S python-textual` to an interpreter-targeted `uv pip install`/`pip install` that
    FAILS on a PEP-668-marked system Python with no fallback offered (three independent review
    findings). Absence must keep using the OS mapping; only a CONFIRMED-present-but-old
    textual bypasses it.
    """
    if dep.kind != "python":
        return False
    if dep.name == "textual" and importlib.util.find_spec("textual") is not None and textual_too_old():
        return True
    return not dep.pkg.get(mgr or "")


def _install_cmd_for(dep: Dependency, os_info: OsInfo) -> list[str] | None:
    mgr = os_info.package_manager
    # python deps that have no system package → install into THIS interpreter (sys.executable),
    # not a bare `python3` that may be a different runtime than the one rig runs under. Prefer uv
    # (the user's toolchain) over a bare `pip` — see _python_install_command for the PEP-668 caveat.
    # Checked BEFORE the `not mgr` bail-out below: unlike a system-package install, this path
    # never needed a package manager in the first place, so an undetected `mgr` (an unrecognized
    # OS/distro) must not silently swallow it — review finding: it did, on install.sh's own
    # promised "rig doctor shows the exact command" contract for exactly this dep class.
    if dep.kind == "python":
        if _targets_this_interpreter(dep, mgr):
            return _python_install_command(_versioned_install_spec(dep.name))
        # `_targets_this_interpreter` returning False for a python dep means
        # `dep.pkg.get(mgr or "")` is truthy BY ITS OWN CONTRACT (a real system-package
        # mapping exists) — the `""` default below is `.get()`'s own required signature
        # filler, never actually reached with an empty value here (a review finding, GLM,
        # round 22: a comment explaining this exact empty-string semantic existed on the
        # pre-refactor code and was dropped when `_targets_this_interpreter` was extracted).
        return install_command(mgr, dep.pkg.get(mgr or "", ""))
    if not mgr:
        return None
    pkg = dep.pkg.get(mgr, dep.name)
    if not pkg:
        return None
    return install_command(mgr, pkg)


def bootstrap(report: DoctorReport, *, assume_yes: bool, include_optional: bool = False) -> list[tuple[str, int]]:
    """Run install commands for missing deps. Returns (dep_name, returncode) pairs.

    Only runs when ``assume_yes`` is True (the caller gates interactive confirmation).
    """
    results: list[tuple[str, int]] = []
    targets = list(report.missing_required)
    if include_optional:
        targets += report.missing_optional
    for status in targets:
        if not status.install_cmd:
            results.append((status.dep.name, 127))
            continue
        if not assume_yes:
            results.append((status.dep.name, -1))  # -1 = not run (needs confirmation)
            continue
        try:
            res = subprocess.run(status.install_cmd, timeout=600)
            results.append((status.dep.name, res.returncode))
        except (OSError, subprocess.SubprocessError):
            results.append((status.dep.name, 1))
    return results
