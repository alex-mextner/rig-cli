"""``rig setup`` — the interactive config wizard (and its non-interactive USAGE fallback).

Two distinct behaviours, decided by whether a real TTY is attached:

**Interactive (a TTY on both stdin and stdout).** A plain-terminal wizard (no textual
dependency — it degrades nowhere and works on every machine) that:

1. SHOWS what is currently enabled/configured across ALL reconciled areas — the same areas
   ``rig status`` covers (skills, agent-hooks, git-hooks, CI, MCP, harness/auto-mode, the
   model-freshness schedule, AGENTS.md, the GitHub ruleset, tmux, the global git-excludes
   block, the tg-ctl daemon) — read from the cascaded config (global + repo);
2. lets the user CHANGE any registered option, routing each to its OWNING layer file — REPO
   options to the repo ``rig.yaml``, GLOBAL options to ``~/.config/rig/config.yaml`` (so a
   global-only block like ``gitignore``/``tg_ctl`` is never written into a committed repo file);
   each option is shown with its inline HINT (the why/how, next to the toggle);
3. APPLIES (``rig apply``) so the change takes effect on the spot.

**Non-interactive (piped / no TTY).** Prints USAGE help for the core commands — ``init``,
``apply``, ``config get|set`` — and exits 0. It deliberately does NOT run a half-wizard with
no way to answer prompts (the roadmap's explicit requirement).

The option list + hints come from :mod:`riglib.schema` (the single source of truth). The apply
step reuses the SAME engine as ``rig apply`` (``plan.build`` + ``actions.run_plan``) — never a
forked executor.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any, Callable

from . import schema
from .layers import REPO

# Non-interactive USAGE text. A module constant (not inlined) so a test can assert on it and the
# wording has one home. Names the core commands the roadmap says setup degrades to a pointer for.
NON_INTERACTIVE_USAGE = """\
rig setup — interactive configuration wizard

  Run `rig setup` from a terminal (a TTY) to open the wizard: it shows what is enabled across
  every reconciled area, lets you change options in the local rig.yaml AND the global
  ~/.config/rig/config.yaml, and applies the change on the spot.

  This is a non-interactive run (input is piped / not a terminal), so there is nothing to
  prompt. Use the core commands directly instead:

    rig init                       first-run onboarding: scaffold rig.yaml + wire the catalog in
    rig apply                      reconcile the repo to rig.yaml (idempotent)
    rig config get <dot.path>      read one nested key   (e.g. rig config get harness.auto_mode)
    rig config set <dot.path> <v>  write one key, then reconcile  (--global / --no-apply)

  See `rig <command> --help` for each command's flags, and docs/config-schema.md for the
  full schema.
"""


def is_interactive() -> bool:
    """A real wizard needs a TTY on BOTH ends — stdin to read answers, stdout to draw prompts.

    A piped or redirected run (CI, ``echo … | rig setup``, ``rig setup > log``) is treated as
    non-interactive so it degrades to USAGE rather than blocking on an unanswerable prompt.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def _layer_path(option: schema.Option, repo_yaml: Path, global_yaml: Path) -> Path:
    """The config FILE an option's value is written to, by its owning layer."""
    return repo_yaml if option.layer == REPO else global_yaml


def load_layer_config(path: Path) -> dict[str, Any]:
    """Parse one YAML config file (a single rig.yaml layer) to a dict. Fail-closed on a bad file.

    The single reader the wizard AND ``rig config get|set`` share, so the two never drift on how a
    layer file is parsed. An ABSENT file is ``{}`` (an edit then starts a fresh canonical doc). But
    a file that EXISTS and is unusable is fail-closed — it raises :class:`riglib.config.ConfigError`
    (mirroring ``config._load_yaml``) so both call sites' ConfigError handlers report it cleanly and
    never OVERWRITE content we couldn't safely round-trip:

    - syntactically broken YAML (``yaml.YAMLError``) or an unreadable file (``OSError``);
    - valid YAML of the WRONG SHAPE — a bare list/scalar (e.g. ``- a\\n- b``). Silently treating it
      as ``{}`` and writing back would destroy the user's content; raise instead.

    An EMPTY file parses to ``None`` → ``{}`` (a blank config is a legitimately empty layer). Lazy
    yaml import keeps the module stdlib-only.
    """
    if not path.is_file():
        return {}
    import yaml  # lazy: keep module import stdlib-only

    from .config import ConfigError

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        # a non-UTF-8 / unreadable file gets the clean diagnostic, not a raw decode traceback.
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if data is None:
        return {}  # an empty file is an empty layer
    if not isinstance(data, dict):
        raise ConfigError(
            f"config {path} must be a YAML mapping, got {type(data).__name__} "
            "(fix it before editing — rig will not overwrite it)"
        )
    return data


def write_layer_config(path: Path, data: dict[str, Any]) -> None:
    """Serialize a config dict back to a YAML layer file (creating parent dirs). Lazy yaml import.

    The single writer the wizard and ``rig config set`` share (counterpart to
    :func:`load_layer_config`).
    """
    import yaml  # lazy

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False), encoding="utf-8")


def render_state(loaded_data: dict[str, Any], *, color: Callable[[str, str], str] | None = None) -> str:
    """Render "what is enabled across all reconciled areas" from the cascaded config.

    One line per area showing its enabled/value summary plus its layer tag, then the area's
    options indented beneath with their current value and inline hint. ``loaded_data`` is the
    already-cascaded (global+repo) config dict; absent keys fall back to registry defaults.
    Pure string assembly so a test can assert on the exact text without a terminal.
    """
    def _c(_code: str, s: str) -> str:
        return color(_code, s) if color else s

    lines: list[str] = [_c("1", "rig setup — current configuration (all reconciled areas)"), ""]
    for area in schema.AREAS:
        layer_tag = _c("2", f"[{area.layer}]")
        lines.append(f"{_c('1', area.title)}  {layer_tag}")
        lines.append(f"  {_c('2', area.blurb)}")
        for opt in area.options:
            val = schema.effective_value(opt, loaded_data)
            shown = _fmt_value(val)
            mark = _c("32", "on") if val is True else (_c("2", "off") if val is False else _c("36", shown))
            leaf = opt.key.split(".", 1)[1] if "." in opt.key else opt.key
            lines.append(f"    {leaf:<28} {mark}")
            lines.append(f"      {_c('2', opt.hint)}")
        lines.append("")
    return "\n".join(lines)


def _fmt_value(val: Any) -> str:
    if val is True:
        return "on"
    if val is False:
        return "off"
    return str(val)


def run_setup(
    repo_root: Path,
    *,
    apply_fn: Callable[[Path], int],
    input_fn: Callable[[str], str] | None = None,
    out: Callable[[str], None] = print,
    color: Callable[[str, str], str] | None = None,
) -> int:
    """Run the interactive wizard against ``repo_root``.

    ``apply_fn`` runs the real ``rig apply`` over the repo (injected so the wizard reuses the
    one engine and a test can assert it was/was not invoked). ``input_fn``/``out`` are injected
    so the loop is driven by scripted input in tests without a pseudo-terminal. ``input_fn``
    defaults to the LIVE ``input`` builtin resolved at call time (not bound at def time), so a
    test/harness that patches ``builtins.input`` is honored. Returns the apply exit code, or 0 if
    the user quit without applying.
    """
    if input_fn is None:
        # resolve the builtin lazily so a patched builtins.input is picked up (a def-time default
        # of `input` would capture the original builtin and ignore the patch).
        input_fn = lambda prompt: input(prompt)  # noqa: E731 — tiny indirection, intentional
    from . import errors
    from .config import ConfigError, global_config_path, load, repo_config_path

    repo_yaml = repo_config_path(repo_root)
    global_yaml = global_config_path()

    # show the live, cascaded state first (global + repo) — same areas as `rig status`. A malformed
    # existing config must fail gracefully here (a clear message + the fix), not a startup traceback.
    try:
        loaded = load(repo_root)
    except ConfigError as exc:
        out(f"  cannot read current config: {exc}")
        out("  fix the malformed config file above, then re-run `rig setup`.")
        return errors.EXIT_CONFIG
    out(render_state(loaded.data, color=color))

    options = schema.all_options()
    pending_apply = False
    want_apply = False
    # A closed terminal (Ctrl-D / Ctrl-C mid-PROMPT) must exit cleanly, not dump a traceback. The
    # guard wraps ONLY the interactive input gathering — the apply call is OUTSIDE it, so a Ctrl-C
    # during the (potentially slow) real `rig apply` is NOT swallowed and mislabeled "no changes".
    try:
        while True:
            out("")
            out(_menu(options, color))
            choice = input_fn("select an option to change [number], (a)pply, (q)uit: ").strip().lower()
            if choice in ("q", "quit", ""):
                break
            if choice in ("a", "apply"):
                want_apply = True
                break
            if not choice.isdigit() or not (1 <= int(choice) <= len(options)):
                out("  not a valid selection")
                continue
            opt = options[int(choice) - 1]
            # the option's OWNING layer file (read fresh below so we never stomp an unrelated edit).
            path = _layer_path(opt, repo_yaml, global_yaml)
            cascaded = load(repo_root)
            current = schema.effective_value(opt, cascaded.data)
            out("")
            out(f"  {opt.key}   (writes {opt.layer} → {path})")
            out(f"    {opt.hint}")
            out(f"    current: {_fmt_value(current)}")
            prompt = _value_prompt(opt)
            raw = input_fn(prompt).strip()
            if raw == "":
                out("  unchanged")
                continue
            try:
                value = schema.coerce(opt, raw)
            except ValueError as exc:
                out(f"  {exc}")
                continue
            # stage + validate the prospective layer doc BEFORE writing — never leave a bad config
            # on disk. The whole read→seed→set→validate is fail-closed on the DOCUMENTED failure
            # modes: a malformed existing layer file (ConfigError from load_layer_config), a
            # non-mapping intermediate (ValueError from set_path), and a schema violation
            # (ConfigError from validate). Anything else (a registry/validator bug) must propagate
            # to the top-level handler, NOT be mislabeled to the user as a "rejected" bad input.
            try:
                # deep-copy so set_path's in-place nested mutation can't leak into a cached dict.
                candidate = copy.deepcopy(load_layer_config(path))
                # a brand-new layer file gets the canonical `version: 1` first (matches the scaffold).
                if not candidate:
                    candidate["version"] = 1
                schema.set_path(candidate, opt.key, value)
                _validate_layer(candidate)
            except (ValueError, ConfigError) as exc:
                out(f"  rejected: {exc}")
                continue
            write_layer_config(path, candidate)
            pending_apply = True
            out(f"  set {opt.key} = {_fmt_value(value)} in {path}")

        # if the user didn't pick (a)pply but made edits, offer to apply now.
        if not want_apply and pending_apply:
            ans = input_fn("apply the changes now? [Y/n]: ").strip().lower()
            want_apply = ans in ("", "y", "yes")
    except (EOFError, KeyboardInterrupt):
        out("")
        if pending_apply:
            out("  aborted — changes already saved to config; run `rig apply` to converge.")
        else:
            out("  aborted — no changes made.")
        return 0

    # APPLY runs OUTSIDE the interrupt guard — a Ctrl-C here must surface, not be misreported.
    if want_apply:
        return apply_fn(repo_root)
    if pending_apply:
        out("  changes saved to config; run `rig apply` to converge.")
    return 0


def _validate_layer(data: dict[str, Any]) -> None:
    """Fail-closed validation of a single layer doc before it is written (reuses config.validate)."""
    from .config import validate

    validate(data)


def _menu(options: list[schema.Option], color: Callable[[str, str], str] | None) -> str:
    def _c(_code: str, s: str) -> str:
        return color(_code, s) if color else s

    lines = [_c("1", "options (number to change):")]
    for i, opt in enumerate(options, 1):
        lines.append(f"  {i:>2}. {opt.key}  {_c('2', '[' + opt.layer + ']')}")
    return "\n".join(lines)


def _value_prompt(opt: schema.Option) -> str:
    if opt.kind == schema.KIND_BOOL:
        return "    new value [yes/no] (blank = keep): "
    if opt.kind == schema.KIND_ENUM:
        return f"    new value {list(opt.choices)} (blank = keep): "
    if opt.kind == schema.KIND_INT:
        return "    new value [integer] (blank = keep): "
    return "    new value (blank = keep): "


def print_non_interactive_usage(out: Callable[[str], None] = print) -> int:
    """Print the non-interactive USAGE pointer (the no-TTY degradation) and return 0."""
    out(NON_INTERACTIVE_USAGE)
    return 0


__all__ = [
    "NON_INTERACTIVE_USAGE",
    "is_interactive",
    "render_state",
    "run_setup",
    "print_non_interactive_usage",
    "load_layer_config",
    "write_layer_config",
]
