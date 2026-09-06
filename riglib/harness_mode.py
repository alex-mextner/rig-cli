"""Per-harness auto/permission-MODE registry — the one place that knows each harness's own
"don't prompt for every tool call" knob (rig-cli#355, gap 1 of rig-cli#337).

Mirrors :mod:`riglib.harness_skills` (skill discovery) and the registries in
:mod:`riglib.permissions` (allowlist / execpolicy / guard): plan, runner, drift and ``rig status``
all key off ``harness.kind`` through THIS table, so a new harness is one entry here — never a
scattered path or key literal. Every kind in :data:`riglib.harness_skills.KNOWN_HARNESS_KINDS`
is either a :data:`HARNESS_MODES` entry (rig writes its mode) or a :data:`HARNESS_MODE_NA` entry
(no such setting exists; the reason is recorded and surfaced VISIBLY — never a silent skip).

What each harness calls "auto-mode" (researched against the harness's own docs/CLI, 2026-09-06):

- **claude-code** — ``permissions.defaultMode`` in ``~/.claude/settings.json``. ``auto`` is the
  classifier-mediated auto-approve (research preview); ``default`` restores prompts. Honored
  ONLY from the user's machine settings, so auto is per-machine (see ``plan._build_harness``).
- **codex** — ``approvals_reviewer`` (root key of ``~/.codex/config.toml``; ``user`` |
  ``auto_review``, default ``user``). ``auto_review`` routes every eligible approval prompt to
  Codex's reviewer subagent instead of the user — the direct analog of claude-code's classifier
  ``auto`` (sandbox + approval_policy stay as they are). The ALTERNATIVE, ``approval_policy =
  "never"``, was rejected as the managed key: it auto-REJECTS escalations (network, outside the
  workspace), so ``gh``/``git push``/``tg`` fail fast under the default ``workspace-write``
  sandbox unless ``sandbox_workspace_write.network_access`` is also flipped — a second key and
  a sandbox change rig should not make silently. rig never touches ``approval_policy`` or
  ``sandbox_mode``. Verified against the installed ``codex-cli 0.153.4`` binary + the config
  reference (``approvals_reviewer``: "who reviews eligible approval prompts … ``auto_review``
  uses the reviewer subagent").
- **opencode** — ``permission."*"`` in ``~/.config/opencode/opencode.json``, the documented
  global default every tool inherits (``allow`` | ``ask`` | ``deny``). opencode's ``--auto``
  flag / TUI toggle is RUNTIME-only (no config key — checked the published schema), so the
  config-level auto-mode is the global ``allow``. Interactive writes ``ask``, which is
  opencode's literal prompt-everything posture (reads included) — the per-tool objects rig
  also manages (``permission.bash`` allow/deny/ask rules) still govern their own tool, so the
  pre-allowed CLIs keep running without prompts.
- **omp** — ``tools.approvalMode`` in ``~/.omp/agent/config.yml`` (``always-ask`` | ``write`` |
  ``yolo``; schema default ``yolo``). ONE owner: the existing guard-interlocked, receipt-tracked
  ``provision_harness_approval`` action (:data:`riglib.permissions.HARNESS_APPROVAL`) writes it —
  its value now follows the harness auto intent instead of a hard-coded ``yolo``. ``_build_harness``
  emits NO second writer for omp, only a note pointing at that action (an attention note when
  ``permissions.enabled: false`` leaves nothing to write it).
- **pi / commandcode** — N/A: no documented approval/auto-mode setting. Their deny/ask INTENT
  ships as the advisory instruction block (:data:`riglib.permissions.HARNESS_INSTRUCTION_POLICY`).

Stdlib-only (the repo import rule): no yaml/toml here; the runner serializes per ``format``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .harness_skills import codex_user_path, omp_agent_root

if TYPE_CHECKING:  # pragma: no cover — typing only, keeps the module import-light
    from pathlib import Path

    from .plan import InstallPlan

# The writer that owns a kind's mode key: the generic ``apply_harness`` action, or (omp) the
# permissions ``provision_harness_approval`` action that already manages the same key.
WRITER_APPLY_HARNESS = "apply_harness"
WRITER_APPROVAL = "approval"


@dataclass(frozen=True)
class HarnessMode:
    """How ONE harness expresses its auto/permission mode on disk.

    ``settings_path`` resolves the UNEXPANDED user-scope file (plan expands it); ``key_path`` is
    the nested key inside that file; ``values`` maps the auto intent (True = auto, False =
    interactive) to the harness's own value; ``auto_values`` / ``interactive_values`` are the
    harness's own mode strings that MEAN non-interactive / interactive — used to infer the intent
    from a pinned ``harness.mode`` when ``harness.auto_mode`` is absent (the live global config
    sets ``mode: auto`` only). A string in neither set declares NO intent (see
    :func:`harness_auto_intent`).
    """

    kind: str
    format: str  # json | toml | yaml
    settings_path: Callable[[], str]
    key_path: tuple[str, ...]
    values: dict[bool, str]
    auto_values: frozenset[str]
    interactive_values: frozenset[str]
    writer: str = WRITER_APPLY_HARNESS

    def dotted(self) -> str:
        return ".".join(self.key_path)


HARNESS_MODES: dict[str, HarnessMode] = {
    "claude-code": HarnessMode(
        kind="claude-code",
        format="json",
        settings_path=lambda: "~/.claude/settings.json",
        key_path=("permissions", "defaultMode"),
        values={True: "auto", False: "default"},
        auto_values=frozenset({"auto", "bypassPermissions", "dontAsk"}),
        interactive_values=frozenset({"default", "acceptEdits", "plan"}),
    ),
    "codex": HarnessMode(
        kind="codex",
        format="toml",
        settings_path=lambda: codex_user_path("config.toml"),
        key_path=("approvals_reviewer",),
        values={True: "auto_review", False: "user"},
        auto_values=frozenset({"auto_review"}),
        interactive_values=frozenset({"user"}),
    ),
    "opencode": HarnessMode(
        kind="opencode",
        format="json",
        settings_path=lambda: "~/.config/opencode/opencode.json",
        key_path=("permission", "*"),
        values={True: "allow", False: "ask"},
        auto_values=frozenset({"allow"}),
        interactive_values=frozenset({"ask", "deny"}),
    ),
    "omp": HarnessMode(
        kind="omp",
        format="yaml",
        settings_path=lambda: f"{omp_agent_root()}/config.yml",
        key_path=("tools", "approvalMode"),
        values={True: "yolo", False: "always-ask"},
        auto_values=frozenset({"yolo"}),
        interactive_values=frozenset({"always-ask", "write"}),
        writer=WRITER_APPROVAL,
    ),
}

# Kinds with NO auto/permission-mode setting at all — recorded WITH the reason so every surface
# (plan note, `rig status` row) can say "n/a: <why>" instead of silently doing nothing.
HARNESS_MODE_NA: dict[str, str] = {
    "pi": "pi has no documented approval/auto-mode setting; its deny/ask intent ships as the advisory AGENTS.md block",
    "commandcode": "commandcode has no documented approval/auto-mode setting; its deny/ask intent ships as the advisory AGENTS.md block",
}


def harness_auto_intent(h: dict, primary_kind: str) -> bool | None:
    """The auto intent a ``harness:`` block declares: True (auto), False (interactive), or None.

    A pinned ``mode:`` the PRIMARY kind knows decides (claude-code ``auto``, codex ``auto_review``,
    opencode ``allow``, omp ``yolo`` mean auto; ``default``/``acceptEdits``, ``user``, ``ask``,
    ``always-ask`` mean interactive) — it is the exact value claude-code has always written
    verbatim, overriding ``auto_mode``, so every other kind follows the SAME precedence (else
    ``auto_mode: false, mode: auto`` would leave claude-code auto and codex interactive). That one
    intent applies to EVERY configured kind — so the live global config (primary claude-code,
    ``mode: auto``, no ``auto_mode``) means auto for codex and opencode too. Without a known
    ``mode:``, an explicit ``auto_mode`` decides. A ``mode:`` string the primary kind does not know
    (``kind: codex, mode: bypassPermissions`` — a claude-code value pasted onto a codex primary) and
    no ``auto_mode`` declares NOTHING: ``None``, never a silent tighten to interactive; the plan
    says so (:func:`unknown_mode_note`). ``None`` lets each writer keep its legacy default.
    """
    mode = h.get("mode")
    spec = HARNESS_MODES.get(primary_kind)
    if mode and spec is not None:
        if str(mode) in spec.auto_values:
            return True
        if str(mode) in spec.interactive_values:
            return False
    explicit = h.get("auto_mode")
    if explicit is not None:
        return bool(explicit)
    return None


def mode_is_known(primary_kind: str, mode: object) -> bool:
    """True when ``mode`` is one of ``primary_kind``'s own mode strings (either intent)."""
    spec = HARNESS_MODES.get(primary_kind)
    return bool(spec and (str(mode) in spec.auto_values or str(mode) in spec.interactive_values))


def mode_value_for(kind: str, intent: bool | None, *, legacy_intent: bool = False) -> str:
    """The value ``kind``'s writer should converge to for ``intent``.

    ``legacy_intent`` is the INTENT assumed when nothing is declared (``None``) — omp's approval
    action keeps its historical relaxed posture (``True`` → yolo, rig-cli#202), the generic
    writers default to interactive."""
    spec = HARNESS_MODES[kind]
    return spec.values[legacy_intent if intent is None else intent]


def resolved_mode_value(kind: str, primary: str, mode: object, intent: bool | None, *, legacy_intent: bool = False) -> str:
    """The value ``kind``'s writer converges to, honouring a pinned ``harness.mode``.

    The PRIMARY kind's own ``mode:`` is written VERBATIM when it is one the kind knows (opencode
    ``deny``, omp ``write``, codex ``user``) — ``harness.mode`` is documented as the exact override,
    and reducing it to the boolean intent would silently relax an explicit ``deny`` to ``ask``.
    Every ADDITIVE kind follows the boolean intent only: the primary's string is harness-specific
    and means nothing to another harness. An unknown ``mode:`` never reaches a writer (the plan
    notes it and writes nothing), so the fallback here is the intent mapping.
    """
    if kind == primary and mode is not None and mode_is_known(kind, mode):
        return str(mode)
    return mode_value_for(kind, intent, legacy_intent=legacy_intent)


def delegated_note(kind: str, *, written: bool) -> str:
    """The plan note for a kind whose mode key is owned by the permissions approval action.

    ``written`` is whether the plan ACTUALLY carries that action (the caller checks the plan's
    actions, never re-derives the approval builder's gate). ``written=False`` carries the
    ``skipped`` marker so ``rig apply`` elevates it: the config asked for a mode nobody writes —
    ``permissions.enabled: false``, or ``kind`` absent from the permissions kinds.
    """
    spec = HARNESS_MODES[kind]
    if written:
        return (
            f"harness: {kind} auto-mode ({spec.dotted()} in {spec.settings_path()}) is written by "
            "the permissions approval action (guard-interlocked), not a separate harness write"
        )
    return (
        f"harness: auto-mode write skipped — {kind}'s {spec.dotted()} is written by the permissions "
        f"approval action, which this config does not emit for {kind} (permissions disabled, or "
        f"{kind} not among the permissions kinds)"
    )


def unknown_mode_note(primary_kind: str, mode: object) -> str:
    """The plan note when ``harness.mode`` is not one of the primary kind's own values and no
    ``auto_mode`` decides: no other kind can follow it, so nothing is written for them (``skipped``
    marker → elevated). A claude-code PRIMARY still writes its own ``mode:`` verbatim (the user owns
    that string); the per-kind :func:`unknown_mode_kind_note` names each kind left unmanaged."""
    spec = HARNESS_MODES[primary_kind]
    return (
        f"harness: auto-mode write skipped — mode '{mode}' is not a known {primary_kind} value "
        f"(auto: {', '.join(sorted(spec.auto_values))}; interactive: "
        f"{', '.join(sorted(spec.interactive_values))}), so no other kind can follow it; "
        "set harness.auto_mode or a known mode"
    )


# The per-kind prefix of :func:`unknown_mode_kind_note` — ``rig status`` recovers the kind's row
# from it (the block-level :func:`unknown_mode_note` names no kind, so it cannot stand for one).
_UNKNOWN_MODE_KIND_PREFIX = "harness: {kind} auto-mode not written — harness.mode '"
_UNKNOWN_MODE_KIND_SUFFIX = " — {key} left as is"


def unknown_mode_kind_note(kind: str, primary: str, mode: object) -> str:
    """The per-kind companion of :func:`unknown_mode_note`: ``kind`` is left unmanaged because the
    block's ``mode:`` is not a value the primary kind knows. Informational (the block-level note
    already carries the elevated ``skipped`` marker and the valid values) — but every affected kind
    says so, instead of a contradictory "not declared (no harness.mode)" while ``mode:`` IS set."""
    return (
        _UNKNOWN_MODE_KIND_PREFIX.format(kind=kind)
        + f"{mode}' is not a {primary} value"
        + _UNKNOWN_MODE_KIND_SUFFIX.format(key=HARNESS_MODES[kind].dotted())
    )


def undeclared_note(kind: str) -> str:
    """The plan note for a kind whose mode rig could write but the config declares no intent for
    (no ``auto_mode``, no ``mode``): nothing is written — a skills-only additive kind must not get
    its posture changed silently. Informational (no attention marker): the default config shape."""
    spec = HARNESS_MODES[kind]
    return (
        f"harness: {kind} auto-mode not declared (no harness.auto_mode / harness.mode) — "
        f"{spec.dotted()} in {spec.settings_path()} left as is; set harness.auto_mode to manage it"
    )


# The exact substring ``rig apply``'s note-attention classifier (riglib.cli._NOTE_ATTENTION_MARKERS)
# matches the DECLARED n/a note on — ONE literal imported there, never re-typed.
NA_NOTE_MARKER = "no auto/permission-mode setting"


def na_note(kind: str, *, declared: bool) -> str:
    """The plan note for a kind without any mode setting — always VISIBLE, never silent.

    ``declared`` (the config set ``auto_mode``/``mode``, so it asked for something rig cannot
    write for this kind) carries :data:`NA_NOTE_MARKER` and is elevated by ``rig apply``;
    undeclared is informational — a plain ``kind: pi`` must not raise an alarm on every apply.
    """
    if declared:
        return (
            f"harness: kind '{kind}' has {NA_NOTE_MARKER} to write "
            f"(n/a: {HARNESS_MODE_NA[kind]})"
        )
    return f"harness: {kind} auto-mode n/a ({HARNESS_MODE_NA[kind]}) — nothing to write"


@dataclass(frozen=True)
class HarnessModeRow:
    """One ``rig status`` line: what rig manages (or cannot) for a configured kind."""

    kind: str
    key: str | None
    value: str | None
    path: Path | None
    note: str
    # the plan action that owns the write — drift for the omp row is filed under the permissions
    # approval action's (category, item), NOT under ("harness", kind); status matches on these.
    category: str | None = None
    item: str | None = None


def harness_mode_rows(plan: InstallPlan) -> list[HarnessModeRow]:
    """Derive the per-kind mode rows from the plan alone (actions + notes), in plan order.

    ``apply_harness`` actions carry the direct writes; ``provision_harness_approval`` carries the
    omp write (its ``mode_value`` option); the N/A kinds come from their plan notes. No config
    access: the plan is the single source both ``apply`` and ``status`` render from.
    """
    rows: list[HarnessModeRow] = []
    seen: set[str] = set()
    for a in plan.actions:
        if a.kind == "apply_harness":
            kind = str(a.options.get("kind", "claude-code"))
            note = ""
        elif a.kind == "provision_harness_approval" and "mode_value" in a.options:
            kind = str(a.options.get("kind", ""))
            note = "written by the permissions approval action (guard-interlocked)"
        else:
            continue
        spec = HARNESS_MODES.get(kind)
        if spec is None:
            continue  # a reporting path must never crash on an action whose kind has no mode entry
        rows.append(HarnessModeRow(
            kind, spec.dotted(), str(a.options.get("mode_value", "")), a.target, note,
            category=a.category, item=a.item,
        ))
        seen.add(kind)
    for note in plan.notes:
        row = _row_for_note(note, seen)
        if row is not None:
            rows.append(row)
            seen.add(row.kind)
    return rows


def _row_for_note(note: str, seen: set[str]) -> HarnessModeRow | None:
    """The status row a plan note stands for (a kind with no owning action), or ``None``."""
    for kind, reason in HARNESS_MODE_NA.items():
        if kind not in seen and note in (na_note(kind, declared=True), na_note(kind, declared=False)):
            return HarnessModeRow(kind, None, None, None, f"n/a: {reason}")
    for kind, spec in HARNESS_MODES.items():
        if kind in seen:
            continue
        if spec.writer == WRITER_APPROVAL and note == delegated_note(kind, written=False):
            return HarnessModeRow(kind, spec.dotted(), None, None,
                                  "skipped: owned by the permissions approval action, which is disabled or not emitted for this kind by this config")
        if spec.writer == WRITER_APPROVAL and note == delegated_note(kind, written=True):
            # the note claims delegation but no approval action carried the write (the actions
            # loop would have seen it) — degrade to a visible row, never drop the kind
            return HarnessModeRow(kind, spec.dotted(), None, None,
                                  "delegated to the permissions approval action, but the plan carries no such action — nothing writes it")
        if note == undeclared_note(kind):
            return HarnessModeRow(kind, spec.dotted(), None, None,
                                  "not declared (no harness.auto_mode / harness.mode) — not managed")
        prefix = _UNKNOWN_MODE_KIND_PREFIX.format(kind=kind)
        suffix = _UNKNOWN_MODE_KIND_SUFFIX.format(key=spec.dotted())
        if note.startswith(prefix) and note.endswith(suffix):
            # COUPLED to unknown_mode_kind_note's exact shape: "<prefix><mode>' is not a <primary>
            # value<suffix>" — the row keeps the middle clause; slicing on the KNOWN suffix (never
            # splitting on " — ") keeps a user mode that itself contains " — " intact;
            # test_unknown_mode_row_text_is_exact pins the rendered row, so a rewording fails loud
            return HarnessModeRow(kind, spec.dotted(), None, None,
                                  "not written — harness.mode '" + note[len(prefix):-len(suffix)] + " — not managed")
    return None
