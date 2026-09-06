"""config-web — a local, machine-wide web console to VIEW, EDIT, and APPLY the reconciled rig
config (rig-cli#310).

What this is
------------
A self-contained, dependency-free web front-end over the SAME config + apply engine that ``rig
setup`` (the wizard), ``rig config get|set``, and ``rig apply`` itself drive: :mod:`riglib.schema`
(the option REGISTRY), :mod:`riglib.config` (the cascade loader, the dot-path set, the layer
files), and :mod:`riglib.plan` / :mod:`riglib.actions.runner` (``plan.build`` + ``run_plan``, the
SAME plan/execute engine ``rig apply``/``rig init`` use — never a forked executor). One browser
tab per rig-managed repo (discovered via :mod:`riglib.config_web_scopes`, read-only over the
existing machine-local repository registry) plus a dedicated GLOBAL tab
(``~/.config/rig/config.yaml`` alone, no repo overlay). Each tab shows every reconciled AREA with
its live effective value, a DRIFT panel (:mod:`riglib.config_web_plan`'s ``compute_scope_drift`` —
the same two-way ``rig status`` engine), and lets the user change a value in the browser — routing
the write to the owning layer's file exactly like the wizard, then re-validating fail-closed.

An edit no longer silently stops at the config file. It surfaces a PLAN PREVIEW (mirroring ``rig
apply info``, each planned action carrying a visual tag from :mod:`riglib.action_tags` naming what
kind of change it is, exactly where on disk/remotely it lands, and whether it affects a running
agent or the human's own workflow), lets the user confirm the whole plan or skip individual
actions, then applies the SELECTED actions with live per-phase progress
(:class:`~riglib.config_web_plan.ApplyJobStore`, reusing ``run_plan``'s existing
``on_start``/``progress`` callbacks — the same mechanism PR #306 added for the ``rig init`` TUI's
live Apply screen). Exactly one apply job may run process-wide at a time, and a stale preview
(config changed since the browser fetched it) is refused rather than silently applied.

How it is reached at runtime
----------------------------
``riglib.cli`` registers a ``config-web`` subcommand whose lifecycle (run/start/stop/status/
enable/disable) is wired through the shared ``agenttools_service`` library (the one service
manager every long-running server in the ecosystem shares — review dashboard, tg-ctl, the
daemon-supervisor). ``run`` serves :class:`ConfigWebApp` in the foreground; ``start`` daemonizes
it; ``enable`` installs an OS autostart (launchd / systemd --user) AND starts it now. The bare
``rig config-web`` (no subcommand) prints HELP and NEVER launches a server. The HTTP layer is
stdlib ``http.server`` (mirroring ``riglib.stats.render.web``): no CDN, no JS framework, inline
CSS, vanilla-JS ``fetch`` calls against the small JSON API below.

Invariants
----------
- **One config + apply engine, several front-ends.** The view model and the edit write go through
  :mod:`riglib.schema` (``AREAS`` / ``effective_value`` / ``coerce`` / ``writable_layer_for_category``)
  and :mod:`riglib.config` (``read_yaml_file`` / ``set_path`` / ``validate``); the plan preview and
  apply go through :mod:`riglib.plan` (``build``) and :mod:`riglib.actions.runner` (``run_plan``) —
  config-web adds NO parallel plan builder or executor. Filtering the SELECTED actions into a new
  ``InstallPlan`` before calling ``run_plan`` is not a fork of the executor — it is the same call,
  over a subset of the same plan.
- **Every multi-repo endpoint goes through the scope ALLOWLIST.** ``/edit``, ``/api/scope``,
  ``/api/drift``, ``/api/plan``, ``/api/apply`` all resolve a request's ``scope`` id against
  :func:`~riglib.config_web_scopes.discover_scopes`'s result for THIS server instance
  (:meth:`ConfigWebApp._resolve_scope`) — an unrecognized id is refused (404/400), never treated
  as an arbitrary filesystem path.
- **A stale plan is refused, never silently applied.** ``/api/plan`` returns a fingerprint (a hash
  of the ordered action list — kind/category/item/target/source/options, plus ``on_conflict``, so
  it changes even when the action IDENTITIES stay the same but their content/behavior would
  differ); ``/api/apply`` rebuilds the plan fresh and 409s if the fingerprint no longer matches —
  the config changed since the browser previewed it.
- **Edits route to the OWNING layer.** A REPO option writes that scope's ``./rig.yaml``; a
  GLOBAL-only option (``gitignore`` / ``tg_ctl`` / ``tmux`` / ``mode`` / ``spotlight``) writes
  ``~/.config/rig/config.yaml`` — the same routing ``writable_layer_for_category`` enforces for
  the wizard, keeping a machine-wide block out of a committed repo file.
- **Fail-closed on write.** A value is coerced per the option's kind, written to a COPY of the
  target file's dict, the merged result is re-:func:`~riglib.config.validate`'d, and only then is
  the file rewritten. A bad value leaves the file untouched and returns an error to the browser.
- **Bind localhost only — but that is NOT a browser security boundary.** The server binds
  ``127.0.0.1`` (never ``0.0.0.0``); still, ANY web page the user visits can ``fetch`` a
  loopback URL, so every mutating/compute-triggering POST (``/edit``, ``/api/plan``,
  ``/api/apply``) is gated by the SAME same-origin/CSRF check (:func:`is_cross_site_write`, plus
  an ``application/json`` content-type requirement for the endpoints that carry a body) — a
  ``cross-site`` ``Sec-Fetch-Site`` or a foreign ``Origin`` is refused 403. A non-browser CLI
  client (curl, the tests) sends neither header and is allowed; the threat model is a hostile web
  page, not local tooling.
- **A handler never leaks a traceback.** A malformed config on GET → 500 with a readable message
  (not a severed socket / blank page); a late write ``OSError`` on POST → 500 JSON; a busy bind
  port → a clean actionable ``OSError`` from :meth:`ConfigWebApp.serve`.

Stdlib-only at import time (the repo rule): ``yaml`` and ``http.server`` are imported lazily
inside the functions that need them, so importing this module stays dependency-light.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from . import config as cfg
from . import schema
from .config_web_plan import ApplyJobStore
from .layers import GLOBAL, REPO
from .provenance import KNOWN_NAMES_SHOWN

# The interface (host + the rendered title) is shared by the HTTP server and the tests, so it is
# defined once. Localhost-only by invariant — see the module docstring.
HOST = "127.0.0.1"
DEFAULT_PORT = 8787
PAGE_TITLE = "rig config-web"

# Cap any JSON POST body (rig-cli#310: shared by /edit, /api/apply — a single {key,value} edit or
# an apply's {scope,fingerprint,skip_keys} is tiny, well under 1 KiB even with dozens of skip
# keys). Binding to localhost does NOT stop a malicious page the user visits from POSTing to us
# (the browser will), so an unbounded `rfile.read(Content-Length)` is a trivial memory-DoS vector
# — reject anything implausibly large rather than allocate it. 64 KiB is generous for the
# legitimate payload. Kept as MAX_EDIT_BODY_BYTES (not renamed) — it's still a public test-facing
# name (tests/test_config_web.py) and renaming buys nothing besides churn.
MAX_EDIT_BODY_BYTES = 64 * 1024


def is_cross_site_write(headers: Any, *, bound_port: int | None = None) -> bool:
    """True when a POST looks like a CROSS-SITE browser request that must be refused.

    config-web binds ``127.0.0.1``, but that is NOT a security boundary in a browser: ANY web
    page the user visits can ``fetch('http://127.0.0.1:8787/edit', …)`` and (with a ``text/plain``
    body) dodge the CORS preflight, letting an attacker rewrite the user's ``rig.yaml`` /
    ``~/.config/rig/config.yaml`` — a classic DNS-rebinding / CSRF-against-localhost attack. We
    refuse a write unless it is same-origin:

    - ``Sec-Fetch-Site`` (sent by every modern browser, NOT forgeable by page JS) must be
      ``same-origin`` / ``none`` when present — a ``cross-site`` / ``same-site`` value is rejected.
    - A present ``Origin`` must EXACTLY match our own origin. "Same-origin" is scheme + host +
      PORT, so when ``bound_port`` is known an ``Origin`` on a different loopback port
      (``http://127.0.0.1:9999`` — another local service the attacker controls) is rejected, not
      just a foreign host. A non-``http`` scheme is rejected too.

    A non-browser client (curl, the test socket) sends neither header and is allowed — the threat
    model is a hostile *web page*, not local CLI tooling. Fail-closed only on a header that
    actively indicates a cross-site browser request.
    """
    sec = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if sec and sec not in ("same-origin", "none"):
        return True
    origin = (headers.get("Origin") or "").strip()
    if origin:
        from urllib.parse import urlparse

        parsed = urlparse(origin)
        if parsed.scheme != "http":
            return True
        if parsed.hostname not in (HOST, "localhost"):
            return True
        # exact-port match: same-origin is scheme+host+PORT. When we DON'T know our own bound port
        # (bound_port is None — a caller forgot to pass it, or a non-standard server), FAIL CLOSED
        # and reject any present Origin rather than silently waving the port check through. A
        # default http Origin omits the port → urlparse yields None → treat as 80, which never
        # matches our loopback dev port, so it is rejected.
        if bound_port is None:
            return True
        origin_port = parsed.port if parsed.port is not None else 80
        if origin_port != bound_port:
            return True
    return False


def is_allowed_host(headers: Any) -> bool:
    """True when the request's ``Host`` header names our loopback (a DNS-rebinding guard).

    Binding ``127.0.0.1`` stops remote TCP, but a page on ``http://evil.test`` whose DNS the
    attacker controls can be made to resolve to ``127.0.0.1`` and reach us; the browser then sends
    ``Host: evil.test``. The CSRF/Origin guard only protects WRITES — a GET of the config HTML (or a
    POST with a foreign Host) would still be served, letting the attacker page read the config
    cross-origin. So EVERY request must carry a loopback ``Host``. A missing Host (HTTP/1.0, raw
    client) is allowed — only a present, foreign hostname is rejected. The port, if present, is
    ignored here (the CSRF port check covers writes); we gate on the HOSTNAME only.
    """
    host = (headers.get("Host") or "").strip()
    if not host:
        return True  # no Host (HTTP/1.0 / a bare client) — not a rebinding browser request
    # The server binds AF_INET (IPv4) only, so the Host is always `name` or `name:port` — strip a
    # trailing `:port` and compare the hostname. (No IPv6 bracket handling: we never bind `::1`, so
    # a bracketed `[::1]` Host would be a non-loopback request to this IPv4 server and is rejected.)
    hostname = host.rsplit(":", 1)[0] if ":" in host else host
    return hostname in (HOST, "localhost")


# ── view model ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FieldView:
    """One editable option as the page renders it: the live value + where an edit would land."""

    key: str
    kind: str
    value: Any
    default: Any
    hint: str
    choices: tuple[str, ...]
    layer: str  # GLOBAL / REPO — the file an edit to THIS field is written to

    @property
    def layer_file(self) -> str:
        """A human label for the owning file (matches the wizard's layer routing)."""
        return "~/.config/rig/config.yaml" if self.layer == GLOBAL else "./rig.yaml"


@dataclass(frozen=True)
class AreaView:
    """One reconciled area (a ``rig status`` row) with its editable fields."""

    category: str
    title: str
    blurb: str
    layer: str
    fields: tuple[FieldView, ...]


@dataclass(frozen=True)
class ConfigModel:
    """The whole page model: the cascaded config's areas + provenance of the two layer files."""

    areas: tuple[AreaView, ...]
    repo_root: Path
    global_path: Path
    repo_path: Path
    global_present: bool
    repo_present: bool
    global_only: bool = False


def build_model(repo_root: Path, *, global_only: bool = False) -> ConfigModel:
    """Read the cascaded config for ``repo_root`` and project it into the page :class:`ConfigModel`.

    Uses the SAME cascade loader the rest of rig uses (global then repo), and the SAME registry
    (:mod:`riglib.schema`) the wizard reads — so every reconciled area + option shows here with
    its live effective value, no parallel field list. ``effective_value`` applies the
    block-presence gating (a feature ``rig apply`` would skip reads as OFF, not its default), so
    the page never advertises a setting the reconciler ignores.

    ``global_only=True`` is the Global scope tab (rig-cli#310): loads ``~/.config/rig/config.yaml``
    ALONE via ``cfg.load(repo_root, include_repo=False)`` — no repo overlay — and renders only the
    areas whose WRITABLE layer is GLOBAL (:attr:`~riglib.schema.Area.layer`, i.e.
    ``schema.writable_layer_for_category`` — the machine-wide-only categories the scaffold never
    writes into a committed repo file: gitignore/spotlight/tg_ctl/tmux/mode). ``repo_root`` is
    still required as the loader's anchor even though its repo layer is skipped.

    KNOWN, PRE-EXISTING design tension (flagged in review, not new here): this VIEW model uses
    the WRITABLE layer (narrow — only gitignore/spotlight/tg_ctl/tmux/mode), while
    :func:`riglib.config_web_plan.build_scope_plan`'s Global-scope PLAN filter uses the broader
    STATUS layer (:func:`riglib.layers.layer_for_category`, which also counts skills/agent_hooks/
    harness/permissions/models/git_hooks/env/tools as GLOBAL — they're machine-wide ARTIFACTS
    even though the scaffold writes their VALUE into the committed repo file). So a repo's
    ``harness.auto_mode`` can appear as an ``apply_harness`` action in the Global tab's plan/apply
    without a matching field in the Global tab's VIEW to edit it from. This is the SAME
    distinction ``riglib/schema.py`` already documents and ``rig status`` already exhibits for a
    non-git cwd (it shows global-artifact drift there too, with no repo layer to edit from either)
    — config-web inherits it rather than introduces it. Not resolved here: reconciling the two
    layer classifications into one is a real design decision (which is more surprising: hiding
    settings the Global tab CAN reconcile, or offering to reconcile settings it can't show?), left
    for a follow-up rather than decided unilaterally in this pass.
    """
    loaded = cfg.load(repo_root, include_repo=not global_only)
    merged = loaded.data
    areas: list[AreaView] = []
    for area in schema.AREAS:
        if global_only and area.layer != GLOBAL:
            continue
        fields = tuple(
            FieldView(
                key=opt.key,
                kind=opt.kind,
                value=schema.effective_value(opt, merged),
                default=opt.default,
                hint=opt.hint,
                choices=opt.choices,
                layer=opt.layer,
            )
            for opt in area.options
        )
        areas.append(
            AreaView(
                category=area.category,
                title=area.title,
                blurb=area.blurb,
                layer=area.layer,
                fields=fields,
            )
        )
    return ConfigModel(
        areas=tuple(areas),
        repo_root=repo_root,
        global_path=cfg.global_config_path(),
        repo_path=cfg.repo_config_path(repo_root),
        global_present=cfg.global_config_path().is_file(),
        repo_present=cfg.repo_config_path(repo_root).is_file(),
        global_only=global_only,
    )


# ── the edit write (routed to the owning layer, fail-closed) ────────────────────────────────
class EditError(ValueError):
    """A rejected edit (unknown key, bad value, validation failure) — surfaced to the browser."""


def _target_path(repo_root: Path, option: schema.Option) -> Path:
    """The file an edit to ``option`` is written to — the owning layer, like the wizard's routing."""
    if option.layer == GLOBAL:
        return cfg.global_config_path()
    return cfg.repo_config_path(repo_root)


def apply_edit(
    repo_root: Path, key: str, raw_value: str, *, include_repo: bool = True
) -> dict[str, Any]:
    """Coerce + write ONE option's value to its owning layer file, fail-closed. Returns a summary.

    ``include_repo=False`` (config-web's Global scope) makes GATE 2 validate ``repo_root``'s
    GLOBAL layer ALONE, with no repo overlay — matching
    :func:`riglib.config_web_plan.build_scope_plan`'s Global branch. Anchoring a Global edit's
    gate at the default ``include_repo=True`` cascade would let an UNRELATED repo's ``rig.yaml``
    at ``repo_root`` (e.g. a stray ``rig.yaml`` at ``$HOME`` for a dotfiles user) reject the edit
    for a reason invisible on the Global tab, which never renders or intends to touch that file
    (caught in review).

    Mirrors ``rig config set`` / the wizard's write path, scoped to a single layer file, with the
    SAME two gates ``_cmd_config_set`` uses so the web UI cannot persist a config the CLI would
    reject:

    1. Resolve the option in the registry (unknown key → :class:`EditError`).
    2. Coerce the raw string to the option's typed value (bad value → :class:`EditError`).
    3. Refuse a REPO edit when ``./rig.yaml`` does not exist yet (same guard as ``config set``:
       editing from ``{}`` would let built-in defaults mutate disk with no committed source of
       truth — ``rig init`` must create the file first). A GLOBAL edit MAY create the machine-wide
       file, so it is not guarded.
    4. GATE 1 — schema validation of the whole edited tree (enum/type checks).
    5. Write the file, then GATE 2 — build the plan from the on-disk cascade (catalog-backed:
       a bad ``agent_tools_source`` / unknown CI item lives here, not in :func:`config.validate`).
       Any failure ROLLS the file back to its exact prior bytes and raises :class:`EditError`, so
       the web UI never leaves a written-but-unreconcilable config behind — identical to the CLI.

    This function itself only edits the DECLARED config; it does not run ``rig apply`` (the same
    separation ``config set --no-apply`` offers). Reconciling to disk is a SEPARATE, explicit step
    the browser offers right after a successful edit — the plan-preview → confirm/skip → live-
    progress apply flow (:mod:`riglib.config_web_plan`), never triggered automatically by a write.
    """
    option = schema.option_for_key(key)
    if option is None:
        raise EditError(f"unknown config option {key!r}")
    try:
        value = schema.coerce(option, raw_value)
    except ValueError as exc:
        raise EditError(str(exc)) from exc

    target = _target_path(repo_root, option)
    # Refuse a repo-local edit when ./rig.yaml is absent — the same guard `config set` enforces:
    # reconciling from {} would let defaults mutate disk with no committed source of truth. (A
    # GLOBAL edit may legitimately create the machine-wide file, so it is exempt.)
    if option.layer == REPO and not target.is_file():
        raise EditError(
            f"no {target} — run `rig init` (or `rig export -o rig.yaml`) first; "
            "config-web edits an existing committed config, it does not bootstrap one."
        )

    # Read the single owning-layer file (an absent GLOBAL file starts from an empty mapping — the
    # wizard creates the global config the same way). NOT the cascade: an edit lands in exactly one
    # file, the layer that owns the option. A MALFORMED existing file raises ConfigError — surface
    # it as a clean EditError (a stale browser tab editing a since-broken file must get JSON, not a
    # severed socket).
    try:
        data = cfg.read_yaml_file(target) if target.is_file() else {}
    except cfg.ConfigError as exc:
        raise EditError(f"cannot read {target}: {exc}") from exc
    # Drop the removed legacy `scope` key (mirrors `_cmd_config_set` + config.load): we re-serialize
    # the WHOLE file, so leaving it would re-emit a setting the schema no longer recognizes — a
    # browser edit must never (re)introduce dead config.
    data.pop("scope", None)
    try:
        cfg.set_path(data, key, value)
    except cfg.ConfigError as exc:  # an existing non-mapping intermediate (e.g. `harness: "a string"`)
        raise EditError(str(exc)) from exc

    # GATE 1 — fail-closed schema validation of the whole edited tree before touching disk. A bad
    # combination (an out-of-range enum coercion let through, a type the validator rejects) aborts
    # here, leaving the file exactly as it was.
    try:
        cfg.validate(data)
    except cfg.ConfigError as exc:
        raise EditError(f"rejected by config validation: {exc}") from exc

    # Capture prior bytes BEFORE writing so the write itself OR GATE 2 can fully ROLL BACK, exactly
    # like `_cmd_config_set`. The file must be byte-identical on ANY failure — so the write is
    # INSIDE the try (a partial/truncated write_text on a full disk is rolled back too, not left).
    original = target.read_text(encoding="utf-8") if target.is_file() else None

    def _rollback() -> None:
        if original is None:
            target.unlink(missing_ok=True)  # we created the file; remove our partial write
        else:
            target.write_text(original, encoding="utf-8")  # restore prior contents

    try:
        _write_layer(target, data, option.layer)
        # For a GLOBAL edit, validate the written global file IN ISOLATION first (mirroring
        # `config set --global`'s `_validate_layer_in_isolation`): the cascade plan below merges a
        # repo overlay over the global layer, which can MASK a catalog-backed error in the global
        # file (a repo rig.yaml overriding the just-broken key). Check it alone so a globally-broken
        # config never persists just because THIS repo happens to override it.
        if option.layer == GLOBAL:
            _validate_layer_in_isolation(target)
        # GATE 2 — build the plan from the on-disk cascade (catalog-backed validation the schema
        # check can't do: a bad agent_tools_source, an unknown CI item). A failure means the edit
        # is unreconcilable → roll the file back and reject.
        _build_plan_gate(repo_root, include_repo=include_repo)
    except OSError:
        # an IO failure (a partial/truncated write_text on a full disk, a permissions error) —
        # roll back the file, then re-raise the OSError so handle_edit maps it to a 500 (a
        # server-side problem, distinct from a user-rejected config which is a 400 EditError).
        _rollback()
        raise
    except Exception as exc:  # noqa: BLE001 — any plan/catalog validation failure: roll back + reject
        _rollback()
        raise EditError(f"rejected by config reconcile check: {exc}") from exc

    return {
        "key": key,
        "value": value,
        "layer": option.layer,
        "file": str(target),
    }


def _write_layer(path: Path, data: dict[str, Any], layer: str) -> None:
    """Serialize ``data`` to its owning-layer file, BYTE-IDENTICAL to what ``rig config set`` writes.

    Both front-ends must round-trip a layer file the same way, so this reuses the SAME
    :class:`~riglib.state.SetupState` serializer ``_cmd_config_set`` uses — not a parallel
    ``yaml.safe_dump`` that would drift:

    - REPO  → :meth:`SetupState.write` — the committed-source-of-truth header + the YAML body.
    - GLOBAL → :meth:`SetupState.to_yaml` — a plain machine-wide dump, no repo header (the global
      ``~/.config/rig/config.yaml`` is not a committed file).

    Like ``config set`` (and the underlying ``yaml.safe_dump``), this does NOT preserve a hand-
    authored file's inline-flow style or comments — the canonical block form is re-emitted. That
    is the existing CLI behaviour, shared here on purpose rather than reinvented. Lazy import of
    state keeps this module import-light.
    """
    from .state import SetupState  # lazy: keeps module import light, mirrors the repo rule

    path.parent.mkdir(parents=True, exist_ok=True)
    state = SetupState.from_dict(data)
    if layer == GLOBAL:
        path.write_text(state.to_yaml(), encoding="utf-8")
    else:
        state.write(path)


def _build_plan_gate(repo_root: Path, *, include_repo: bool = True) -> None:
    """Build the reconcile plan from the on-disk cascade — the second, catalog-backed gate.

    This is GATE 2 in :func:`apply_edit`, mirroring ``_cmd_config_set``: it loads the cascaded
    config (global, plus repo IF ``include_repo`` — INCLUDING the just-written edit), scans the
    agent-tools catalog, and builds the plan. It does NOT execute anything (config-web never runs
    ``rig apply``) — it only proves the edited config can be reconciled. Catalog-backed errors a
    pure-schema :func:`config.validate` cannot catch (a bad ``agent_tools_source``, an unknown
    CI/MCP item) surface here; the caller rolls the file back on any exception. ``include_repo=
    False`` (config-web's Global scope) validates the GLOBAL layer alone, exactly like
    :func:`riglib.config_web_plan.build_scope_plan`'s Global branch — see :func:`apply_edit`'s
    docstring for why this must NOT default to the full cascade there. Heavy modules are imported
    lazily so this module stays import-light.
    """
    from .catalog import Catalog
    from .detect import detect_environment
    from .plan import build

    env = detect_environment(repo_root.resolve())
    loaded = cfg.load(env.repo_root, include_repo=include_repo)
    catalog = Catalog.scan(loaded.agent_tools_source)
    build(loaded, catalog, project_type=env.project_type)


def _validate_layer_in_isolation(layer_path: Path) -> None:
    """Validate ONE config file alone (no cascade) — reused from the CLI's `config set --global`.

    A GLOBAL edit is otherwise only checked by the merged cascade (:func:`_build_plan_gate`), where
    a repo ``rig.yaml`` overriding the just-broken global key masks the breakage so a globally-
    broken config still persists. The CLI solves this with ``cli._validate_layer_in_isolation``;
    config-web reuses that SAME function (lazy import to avoid a cli↔config_web import cycle) rather
    than duplicate the logic, so the two surfaces validate a global edit identically.
    """
    from .cli import _validate_layer_in_isolation as _cli_isolated  # lazy: breaks the import cycle

    _cli_isolated(layer_path)


# ── HTML rendering (self-contained, no external assets) ─────────────────────────────────────
def _fmt_value(value: Any) -> str:
    """Render a value in YAML/CLI casing (true/false/null), not Python repr — matches `config get`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (list, dict)):
        # a non-scalar default/value: emit inline YAML (``[mon, tue]``), not a Python repr
        # (``['mon', 'tue']`` with single quotes) — keeps the "matches `config get`" invariant.
        # The wizard registry exposes only scalar options today, so this is defense-in-depth for
        # any future list/dict-valued field rather than a path hit in practice.
        import yaml  # lazy, like the rest of the module

        return yaml.safe_dump(value, default_flow_style=True).strip()
    return str(value)


def _control_value(key: str, value: Any) -> str:
    """The HTML control value for a committed config value."""
    option = schema.option_for_key(key)
    if option is not None and option.kind == schema.KIND_ENUM and option.default is None and value is None:
        return ""
    return _fmt_value(value)


def _field_control(f: FieldView) -> str:
    """The input control for one field, keyed by kind (bool→toggle, enum→select, else→text)."""
    val = _fmt_value(f.value)
    key_attr = html.escape(f.key, quote=True)
    if f.kind == schema.KIND_BOOL:
        checked = " checked" if f.value is True else ""
        return (
            f'<label class="switch"><input type="checkbox" data-key="{key_attr}" '
            f'data-kind="bool"{checked} onchange="edit(this)"><span class="slider"></span></label>'
        )
    if f.kind == schema.KIND_ENUM:
        if f.default is None:
            selected = " selected" if f.value is None else ""
            null_option = f'<option value=""{selected}>(fan-out / unpinned)</option>'
        else:
            null_option = ""
        opts = "".join(
            f'<option value="{html.escape(c, quote=True)}"'
            f'{" selected" if c == val else ""}>{html.escape(c)}</option>'
            for c in f.choices
        )
        return (
            f'<select data-key="{key_attr}" data-kind="enum" onchange="edit(this)">'
            f"{null_option}{opts}</select>"
        )
    input_type = "number" if f.kind == schema.KIND_INT else "text"
    return (
        f'<input type="{input_type}" class="txt" value="{html.escape(val, quote=True)}" '
        f'data-key="{key_attr}" data-kind="{html.escape(f.kind, quote=True)}" '
        f'onchange="edit(this)">'
    )


def _layer_badge(layer: str) -> str:
    cls = "repo" if layer == REPO else "global"
    label = "repo" if layer == REPO else "global"
    return f'<span class="badge {cls}" title="edits land in this layer">{label}</span>'


def _field_row(f: FieldView) -> str:
    is_default = f.value == f.default
    default_note = (
        "" if is_default else f' · default <code>{html.escape(_fmt_value(f.default))}</code>'
    )
    return (
        '<div class="field">'
        f'<div class="field-head"><code class="key">{html.escape(f.key)}</code>'
        f'{_layer_badge(f.layer)}'
        f'<span class="ctl">{_field_control(f)}</span></div>'
        f'<div class="hint">{html.escape(f.hint)}{default_note}</div>'
        '</div>'
    )


def _area_section(a: AreaView) -> str:
    rows = "".join(_field_row(f) for f in a.fields)
    return (
        '<section class="area">'
        f'<h2>{html.escape(a.title)} {_layer_badge(a.layer)}</h2>'
        f'<p class="blurb">{html.escape(a.blurb)}</p>'
        f'{rows}</section>'
    )


def _subtitle(model: ConfigModel) -> str:
    """The provenance line under the tab bar: which files this scope reads/writes."""
    if model.global_only:
        global_state = "present" if model.global_present else "absent (created on first edit)"
        return (
            f'global-only scope · <code>{html.escape(str(model.global_path))}</code> '
            f'({global_state}) · no repo overlay<br>'
            'edits route here directly; reconcile with <code>rig apply commit</code>'
        )
    repo_state = "present" if model.repo_present else "absent (run `rig init`)"
    global_state = "present" if model.global_present else "absent (created on first global edit)"
    return (
        f'checkout <code>{html.escape(str(model.repo_root))}</code><br>'
        f'rig.yaml <code>{html.escape(str(model.repo_path))}</code> ({repo_state}) · '
        f'global <code>{html.escape(str(model.global_path))}</code> ({global_state})<br>'
        'edits route to the owning layer; reconcile with <code>rig apply commit</code>'
    )


def areas_fragment(model: ConfigModel) -> str:
    """Just the area sections + subtitle — the HTML swapped in by a JS tab switch (no page reload)."""
    sections = "".join(_area_section(a) for a in model.areas)
    return f'<p class="sub">{_subtitle(model)}</p>{sections}'


def _tab_bar(scopes: list, active_id: str) -> str:
    """The scope tab bar (rig-cli#310) — one tab per rig-managed repo + the Global tab.

    Each tab is a real ``<a href>`` (a bare page still works with JS disabled / for curl) AND
    carries ``data-scope`` for the JS-driven no-reload switch (:func:`areas_fragment` fetched via
    ``/api/scope``).
    """
    from urllib.parse import quote as _urlquote

    tabs = []
    for scope in scopes:
        cls = "tab active" if scope.id == active_id else "tab"
        # A repo path can legally contain &, #, %, +, ? (all valid in a Unix directory name).
        # html.escape alone is only ATTRIBUTE-safe, not QUERY-safe — url-quote the id first (a
        # `&`-containing path would otherwise silently truncate the query / route to the wrong
        # scope for the no-JS/copy-paste/middle-click path — found in review, independently by
        # two reviewers), THEN html.escape the already-percent-encoded result for the attribute.
        href = f"/?scope={html.escape(_urlquote(scope.id, safe=''), quote=True)}"
        label = "Global" if scope.is_global else scope.label
        tabs.append(
            f'<a class="{cls}" href="{href}" data-scope="{html.escape(scope.id, quote=True)}" '
            f'onclick="return switchScope(event, this)">{html.escape(label)}</a>'
        )
    return f'<nav class="tabs">{"".join(tabs)}</nav>'


def build_html(
    model: ConfigModel, scopes: list | None = None, active_scope: Any | None = None
) -> str:
    """The whole page as one string (tested directly, no socket — mirrors stats.render.web).

    ``scopes``/``active_scope`` (rig-cli#310) render the machine-wide tab bar; omitted (the
    single-repo call some direct tests still use), the page renders exactly as before — no tab
    bar, one scope's areas.
    """
    active_id = active_scope.id if active_scope is not None else ""
    tab_bar = _tab_bar(scopes, active_id) if scopes else ""
    body = areas_fragment(model)
    # Precomputed OUTSIDE the f-string: Python <3.12 forbids a backslash inside an f-string
    # ``{expression}`` part (PEP 701 only lifts this in 3.12+), and this repo supports 3.10+
    # (pyproject.toml requires-python) — the escaped-JSON expression below needs a literal
    # ``\\u003c`` (see the CURRENT_SCOPE comment further down for WHY), which broke CI on
    # 3.10/3.11 while passing locally on a newer interpreter (found by CI, not local pytest).
    current_scope_json = json.dumps(active_id).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(PAGE_TITLE)}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font: 14px/1.5 -apple-system, system-ui, sans-serif; background:#16181c; color:#e8eaed;
          margin:0; padding:24px; max-width:920px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  h2 {{ font-size:15px; margin:0 0 4px; display:flex; align-items:center; gap:8px; }}
  .sub {{ color:#9aa0a6; margin:0 0 20px; }}
  .sub code {{ color:#cdd1d6; }}
  .area {{ background:#202124; border:1px solid #2d2f34; border-radius:10px; padding:16px 18px;
           margin-bottom:16px; }}
  .blurb {{ color:#9aa0a6; margin:0 0 12px; font-size:13px; }}
  .field {{ padding:10px 0; border-top:1px solid #2a2c31; }}
  .field:first-of-type {{ border-top:none; }}
  .field-head {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
  code.key {{ color:#8ab4f8; font-size:13px; }}
  .ctl {{ margin-left:auto; }}
  .hint {{ color:#9aa0a6; font-size:12px; margin-top:4px; }}
  .hint code {{ color:#cdd1d6; }}
  .badge {{ font-size:10px; text-transform:uppercase; letter-spacing:.5px; padding:2px 6px;
            border-radius:4px; font-weight:600; }}
  .badge.repo {{ background:#1e3a5f; color:#8ab4f8; }}
  .badge.global {{ background:#3d2f1e; color:#fbbc04; }}
  input.txt, select {{ background:#16181c; color:#e8eaed; border:1px solid #3c4043;
            border-radius:6px; padding:5px 8px; font:13px system-ui; min-width:120px; }}
  .switch {{ position:relative; display:inline-block; width:40px; height:22px; }}
  .switch input {{ opacity:0; width:0; height:0; }}
  .slider {{ position:absolute; cursor:pointer; inset:0; background:#3c4043; border-radius:22px;
             transition:.15s; }}
  .slider:before {{ position:absolute; content:""; height:16px; width:16px; left:3px; bottom:3px;
             background:#e8eaed; border-radius:50%; transition:.15s; }}
  .switch input:checked + .slider {{ background:#34a853; }}
  .switch input:checked + .slider:before {{ transform:translateX(18px); }}
  #toast {{ position:fixed; bottom:20px; left:50%; transform:translateX(-50%); padding:10px 18px;
            border-radius:8px; font-size:13px; opacity:0; transition:.2s; pointer-events:none;
            max-width:80vw; z-index:50; }}
  #toast.ok {{ background:#1e3a2a; color:#81c995; border:1px solid #34a853; }}
  #toast.err {{ background:#3a1e1e; color:#f28b82; border:1px solid #ea4335; }}
  #toast.show {{ opacity:1; }}
  .tabs {{ display:flex; gap:4px; flex-wrap:wrap; margin-bottom:10px; border-bottom:1px solid #2d2f34;
           padding-bottom:0; }}
  .tab {{ color:#9aa0a6; text-decoration:none; padding:8px 14px; border-radius:8px 8px 0 0;
          font-size:13px; border:1px solid transparent; border-bottom:none; }}
  .tab.active {{ color:#e8eaed; background:#202124; border-color:#2d2f34; font-weight:600; }}
  .tab:hover {{ color:#e8eaed; }}
  .layout {{ display:flex; gap:20px; align-items:flex-start; }}
  #areas {{ flex:1 1 auto; min-width:0; }}
  #side {{ flex:0 0 300px; position:sticky; top:16px; }}
  .panel {{ background:#202124; border:1px solid #2d2f34; border-radius:10px; padding:14px 16px;
            margin-bottom:16px; font-size:13px; }}
  .panel h2 {{ margin:0 0 8px; }}
  .panel button {{ background:#2d2f34; color:#e8eaed; border:1px solid #3c4043; border-radius:6px;
                   padding:4px 10px; font-size:12px; cursor:pointer; }}
  .panel button:hover {{ background:#3c4043; }}
  .drift-item {{ padding:6px 0; border-top:1px solid #2a2c31; font-size:12px; }}
  .drift-item:first-child {{ border-top:none; }}
  .drift-dir {{ font-size:9px; text-transform:uppercase; padding:1px 5px; border-radius:4px;
                margin-right:6px; }}
  .drift-dir.missing {{ background:#3d2f1e; color:#fbbc04; }}
  .drift-dir.extra {{ background:#1e3a5f; color:#8ab4f8; }}
  .drift-dir.modified {{ background:#3a1e1e; color:#f28b82; }}
  .in-sync {{ color:#81c995; }}
  #apply-cta {{ position:fixed; bottom:20px; right:20px; background:#8ab4f8; color:#16181c;
                border:none; border-radius:8px; padding:10px 18px; font-size:13px; font-weight:600;
                cursor:pointer; box-shadow:0 2px 10px rgba(0,0,0,.4); z-index:40; }}
  #apply-cta[hidden] {{ display:none; }}
  #plan-modal {{ position:fixed; inset:0; background:rgba(0,0,0,.6); display:flex;
                 align-items:center; justify-content:center; z-index:60; }}
  #plan-modal[hidden] {{ display:none; }}
  #plan-card {{ background:#202124; border:1px solid #2d2f34; border-radius:12px; padding:20px;
                width:min(720px, 92vw); max-height:82vh; overflow:auto; }}
  .modal-header {{ display:flex; align-items:center; justify-content:space-between; }}
  #plan-close-x {{ background:none; border:none; color:#9aa0a6; font-size:22px; line-height:1;
                    cursor:pointer; padding:0 4px; }}
  #plan-close-x:hover {{ color:#e8eaed; }}
  .plan-row {{ display:flex; align-items:flex-start; gap:10px; padding:10px 0;
               border-top:1px solid #2a2c31; }}
  .plan-row:first-of-type {{ border-top:none; }}
  .plan-tag {{ font-size:10px; text-transform:uppercase; letter-spacing:.3px; padding:2px 6px;
               border-radius:4px; white-space:nowrap; background:#1e3a5f; color:#8ab4f8; }}
  .plan-audience {{ font-size:10px; color:#9aa0a6; }}
  .plan-target {{ font-size:11px; color:#9aa0a6; font-family:ui-monospace,monospace; word-break:break-all; }}
  .plan-status {{ font-size:11px; font-weight:600; }}
  .plan-status.queued {{ color:#9aa0a6; }}
  .plan-status.running {{ color:#fbbc04; }}
  .plan-status.created,.plan-status.updated,.plan-status.backed_up {{ color:#81c995; }}
  .plan-status.skipped {{ color:#9aa0a6; }}
  .plan-status.error {{ color:#f28b82; }}
  #plan-actions {{ display:flex; gap:10px; margin-top:16px; }}
  #plan-actions button {{ flex:1; padding:10px; border-radius:8px; border:none; font-size:13px;
                          font-weight:600; cursor:pointer; }}
  #plan-apply-btn {{ background:#34a853; color:#0b1f12; }}
  #plan-cancel-btn {{ background:#2d2f34; color:#e8eaed; }}
</style></head><body>
<h1>{html.escape(PAGE_TITLE)}</h1>
{tab_bar}
<div class="layout">
<div id="areas">{body}</div>
<div id="side">
  <section class="panel" id="drift-panel">
    <h2>Drift <button onclick="loadDrift()">refresh</button></h2>
    <div id="drift-body">loading…</div>
  </section>
</div>
</div>
<button id="apply-cta" hidden onclick="openPlan()">Review &amp; apply changes</button>
<div id="plan-modal" hidden>
  <div id="plan-card">
    <div class="modal-header"><h2>Plan preview</h2>
      <button id="plan-close-x" onclick="closePlan()" aria-label="Close">&times;</button></div>
    <div id="plan-body">loading…</div>
    <div id="plan-actions" hidden>
      <button id="plan-cancel-btn" onclick="closePlan()">Cancel</button>
      <button id="plan-apply-btn" onclick="submitApply()">Apply selected</button>
    </div>
  </div>
</div>
<div id="toast"></div>
<script>
// json.dumps() does not escape "<", so a scope id containing "</script>" would otherwise
// terminate this element early on EVERY page render. An earlier version only replaced "</" —
// insufficient: a directory name containing "<!--<script>" moves the HTML tokenizer through
// script-data-escaped into script-data-DOUBLE-escaped state, where this template's own literal
// "</script>" no longer closes the element either (it only toggles state) — not exploitable as
// XSS (every "</" is still escaped), but it silently kills ALL page JS on that scope's render
// (found in review, a second pass). Escaping EVERY "<" closes both the plain and the
// double-escape path in one rule.
var CURRENT_SCOPE = {current_scope_json};
var PENDING_PLAN = null;  // {{fingerprint, actions}} from the last /api/plan fetch
var APPLY_POLL = null;

// Every drift/plan field below comes from the SERVER (repo directory names, catalog item names,
// action targets) — NOT trusted input. innerHTML-interpolate ONLY through this escaper (found in
// review: an unescaped repo/target string could inject a <script> that runs at this origin and
// drives same-origin /edit or /api/apply, which the CSRF guard does NOT catch since it only
// rejects CROSS-site requests).
function esc(s) {{
  return String(s).replace(/[&<>"']/g, function(c) {{
    return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c];
  }});
}}

function toast(msg, ok) {{
  var t = document.getElementById('toast');
  t.textContent = msg; t.className = (ok ? 'ok' : 'err') + ' show';
  setTimeout(function() {{ t.className = t.className.replace(' show', ''); }}, 2600);
}}
function seedCommitted(root) {{
  root.querySelectorAll('[data-key]').forEach(function(el) {{
    el.dataset.committed = (el.getAttribute('data-kind') === 'bool') ? (el.checked ? 'true' : 'false') : el.value;
  }});
}}
// remember each control's last-committed state so a REJECTED edit can revert the visible value
// (the server rolled the file back, so the page must not keep showing the unsaved value).
function revert(el) {{
  if (el.getAttribute('data-kind') === 'bool') {{ el.checked = (el.dataset.committed === 'true'); }}
  else {{ el.value = (el.dataset.committed === undefined ? el.value : el.dataset.committed); }}
}}
function markPending() {{
  document.getElementById('apply-cta').hidden = false;
}}
async function edit(el) {{
  var key = el.getAttribute('data-key');
  var kind = el.getAttribute('data-kind');
  var value = (kind === 'bool') ? (el.checked ? 'true' : 'false') : el.value;
  try {{
    var r = await fetch('/edit', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{key: key, value: value, scope: CURRENT_SCOPE}})
    }});
    var data = await r.json();
    if (r.ok && data.ok) {{
      el.dataset.committed = (kind === 'bool') ? value : (data.control_value ?? data.value);  // new committed state
      toast(key + ' = ' + data.value + '  →  ' + data.file, true);
      markPending();  // the write landed but is NOT yet applied to disk -- offer to review+apply
    }} else {{
      revert(el);  // server rejected + rolled back → restore the control to its committed value
      toast('rejected: ' + (data.error || r.status), false);
    }}
  }} catch (e) {{ revert(el); toast('request failed: ' + e, false); }}
}}
seedCommitted(document);

// -- scope tab switching (no page reload) ------------------------------------------------------
async function switchScope(evt, a) {{
  evt.preventDefault();
  var scope = a.getAttribute('data-scope');
  try {{
    var r = await fetch('/api/scope?scope=' + encodeURIComponent(scope));
    var data = await r.json();
    if (!r.ok || !data.ok) {{ toast('could not load scope: ' + (data.error || r.status), false); return; }}
    document.getElementById('areas').innerHTML = data.html;
    seedCommitted(document.getElementById('areas'));
    document.querySelectorAll('.tab').forEach(function(t) {{ t.classList.remove('active'); }});
    a.classList.add('active');
    CURRENT_SCOPE = scope;
    history.pushState({{}}, '', '/?scope=' + encodeURIComponent(scope));
    document.getElementById('apply-cta').hidden = true;
    loadDrift();
  }} catch (e) {{ toast('request failed: ' + e, false); }}
  return false;
}}

// -- drift panel ---------------------------------------------------------------------------------
async function loadDrift() {{
  var body = document.getElementById('drift-body');
  body.textContent = 'loading…';
  try {{
    var r = await fetch('/api/drift?scope=' + encodeURIComponent(CURRENT_SCOPE));
    var data = await r.json();
    if (!r.ok || data.ok === false) {{ body.textContent = 'drift check failed: ' + (data.error || r.status); return; }}
    var rows = data.items.map(function(i) {{
      return '<div class="drift-item"><span class="drift-dir ' + esc(i.direction) + '">' + esc(i.direction) +
        '</span>' + esc(i.category) + '/' + esc(i.item) + '<div class="plan-target">' + esc(i.target) + '</div>' +
        '<div>' + esc(i.detail) + '</div></div>';
    }}).join('');
    // missing_targets (a dead hook reference) is a SEPARATE, higher-severity class from ordinary
    // config<->disk drift -- rendering only `items` would silently say "in sync" while a hook
    // command points at a file that's gone (found in review: rig status shows this as a distinct
    // "missing targets" block for exactly this reason).
    var missing = (data.missing_targets || []).map(function(m) {{
      return '<div class="drift-item"><span class="drift-dir missing">missing target</span>' +
        esc(m.what) + '<div>' + esc(m.why) + '</div><div class="plan-target">fix: ' + esc(m.fix) + '</div></div>';
    }}).join('');
    // known items (rig-cli#357): on disk, not rig-managed, origin named -- informational, never
    // drift. Rendered under their own heading so a clean scope with tool-installed skills still
    // reads as in sync (the payload's `in_sync` ignores them, and so must this panel).
    // One row per (container-or-category, origin label) with the member names joined and capped
    // -- the SAME shape the CLI prints (`_print_known_groups`, `_KNOWN_NAMES_SHOWN`): a kept
    // allowlist runs to hundreds of entries, and one <div> per entry would flood the panel.
    var KNOWN_NAMES_SHOWN = {KNOWN_NAMES_SHOWN};
    function knownName(k) {{
      return !k.kept && k.by && k.by !== k.name ? k.name + ' (by ' + k.by + ')' : k.name;
    }}
    function knownGroups(list) {{
      var groups = {{}}, order = [];
      list.forEach(function(k) {{
        var where = k.kept ? k.container : k.category;
        var key = where + ' :: ' + k.label;
        if (!groups[key]) {{ groups[key] = {{where: where, label: k.label, kept: k.kept, names: []}}; order.push(key); }}
        groups[key].names.push(knownName(k));
      }});
      return order.map(function(key) {{
        var g = groups[key];
        var shown = g.names.slice(0, KNOWN_NAMES_SHOWN).join(', ');
        if (g.names.length > KNOWN_NAMES_SHOWN) {{ shown += ' … and ' + (g.names.length - KNOWN_NAMES_SHOWN) + ' more'; }}
        return '<div class="drift-item"><span class="drift-dir known">' + (g.kept ? 'kept' : 'known') + '</span>' +
          esc(g.where) + ' (' + g.names.length + ')<div class="plan-target">' + esc(g.label) + '</div><div>' + esc(shown) + '</div></div>';
      }}).join('');
    }}
    // the CLI's two headings: placed items vs permission additions (never removed)
    var placed = (data.known || []).filter(function(k) {{ return !k.kept; }});
    var kept = (data.known || []).filter(function(k) {{ return k.kept; }});
    var knownBlock = '';
    if (placed.length) {{
      knownBlock += '<div class="in-sync">known, not managed by rig (' + placed.length + ') — informational, not drift</div>' + knownGroups(placed);
    }}
    if (kept.length) {{
      knownBlock += '<div class="in-sync">your additions, kept (' + kept.length + ') — beyond the rig baseline; rig never removes them</div>' + knownGroups(kept);
    }}
    if (!rows && !missing) {{ body.innerHTML = '<div class="in-sync">✓ in sync — config and disk agree</div>' + knownBlock; return; }}
    body.innerHTML = missing + rows + knownBlock;
    // pre-existing drift (declared but never applied, or applied then hand-edited away) must
    // offer the SAME apply entry point an edit does -- previously the CTA only appeared right
    // after a successful edit, so a scope with drift on first load / tab switch had no way to
    // reach the plan/apply flow without making an unrelated change first (found in review).
    document.getElementById('apply-cta').hidden = false;
  }} catch (e) {{ body.textContent = 'drift check failed: ' + e; }}
}}
loadDrift();

// -- plan preview / confirm-skip / live apply -----------------------------------------------------
function closePlan() {{
  document.getElementById('plan-modal').hidden = true;
  if (APPLY_POLL) {{ clearInterval(APPLY_POLL); APPLY_POLL = null; }}
}}
async function openPlan() {{
  var modal = document.getElementById('plan-modal');
  var bodyEl = document.getElementById('plan-body');
  var actionsEl = document.getElementById('plan-actions');
  modal.hidden = false;
  actionsEl.hidden = true;
  bodyEl.textContent = 'building plan…';
  try {{
    var r = await fetch('/api/plan?scope=' + encodeURIComponent(CURRENT_SCOPE), {{method: 'POST'}});
    var data = await r.json();
    if (!r.ok || data.ok === false) {{ bodyEl.textContent = 'could not build plan: ' + (data.error || r.status); return; }}
    PENDING_PLAN = data;
    if (!data.actions.length) {{
      bodyEl.innerHTML = '<div class="in-sync">nothing to apply — already in sync</div>';
      return;
    }}
    bodyEl.innerHTML = data.actions.map(function(a) {{
      return '<div class="plan-row" data-key="' + esc(a.key) + '">' +
        '<input type="checkbox" checked data-skip-toggle>' +
        '<div style="flex:1"><div><span class="plan-tag">' + esc(a.tag.label) + '</span> ' +
        '<span class="plan-audience">(' + esc(a.tag.audience) + ')</span></div>' +
        '<div>' + esc(a.describe) + '</div>' +
        '<div class="plan-target">' + esc(a.target) + '</div>' +
        '<div class="plan-target">' + esc(a.tag.detail) + '</div>' +
        '<div class="plan-status queued" data-status>queued</div></div></div>';
    }}).join('');
    actionsEl.hidden = false;
  }} catch (e) {{ bodyEl.textContent = 'could not build plan: ' + e; }}
}}
async function submitApply() {{
  if (!PENDING_PLAN) return;
  var skip = [];
  document.querySelectorAll('.plan-row').forEach(function(row) {{
    var cb = row.querySelector('[data-skip-toggle]');
    if (!cb.checked) skip.push(row.getAttribute('data-key'));
  }});
  document.getElementById('plan-actions').hidden = true;
  try {{
    var r = await fetch('/api/apply', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{scope: CURRENT_SCOPE, fingerprint: PENDING_PLAN.fingerprint, skip_keys: skip}})
    }});
    var data = await r.json();
    if (!r.ok || data.ok === false) {{
      // a refusal (e.g. 409 stale plan) -- re-show the actions so the user isn't stuck behind a
      // dimmed overlay with only the header × to escape (found in review); the header × ALSO
      // always works regardless, as the reliable fallback for every other path.
      document.getElementById('plan-actions').hidden = false;
      toast('apply refused: ' + (data.error || r.status), false);
      return;
    }}
    pollApply(data.job_id);
  }} catch (e) {{
    document.getElementById('plan-actions').hidden = false;
    toast('apply failed: ' + e, false);
  }}
}}
function pollApply(jobId) {{
  APPLY_POLL = setInterval(async function() {{
    try {{
      var r = await fetch('/api/apply/status?job=' + encodeURIComponent(jobId));
      var data = await r.json();
      if (!r.ok) {{ clearInterval(APPLY_POLL); toast('lost apply progress: ' + r.status, false); return; }}
      data.actions.forEach(function(a) {{
        var row = document.querySelector('.plan-row[data-key="' + CSS.escape(a.key) + '"]');
        if (!row) return;
        var st = row.querySelector('[data-status]');
        st.textContent = a.status + (a.detail ? ' — ' + a.detail : '');
        st.className = 'plan-status ' + a.status;
      }});
      if (data.done) {{
        clearInterval(APPLY_POLL); APPLY_POLL = null;
        toast(data.error ? ('apply error: ' + data.error) : 'applied', !data.error);
        document.getElementById('apply-cta').hidden = true;
        loadDrift();
        // the modal must never stay open, dimmed, and unclosable after a run finishes (found in
        // review: submitApply hides #plan-actions, whose Cancel button is the only closer) -- on
        // success just close it; on a partial/total failure keep it open but re-show the actions
        // (with the header × always available too) so the user can see per-action detail.
        if (data.error) {{ document.getElementById('plan-actions').hidden = false; }}
        else {{ closePlan(); }}
      }}
    }} catch (e) {{ clearInterval(APPLY_POLL); toast('lost apply progress: ' + e, false); }}
  }}, 500);
}}
</script>
</body></html>"""


# ── the HTTP application (stdlib http.server) ───────────────────────────────────────────────
@dataclass
class ConfigWebApp:
    """The config-web HTTP application — a MACHINE-WIDE console (rig-cli#310): one browser tab per
    rig-managed repo (discovered via :mod:`riglib.config_web_scopes`) plus the Global scope, a
    drift panel per active tab, and an interactive plan-preview → confirm/skip → live-progress
    apply flow that runs through the SAME shared engine ``rig apply``/``rig init`` use
    (:mod:`riglib.config_web_plan`, never a forked executor).

    ``serve`` binds a localhost ``http.server`` and blocks until interrupted. Every view is
    rebuilt PER REQUEST so a config change (from the browser, the CLI, or a hand-edit) is
    reflected on refresh/poll without a restart. ``repo_root`` is the HOME scope — the repo
    config-web was started against; it is always the first tab (see
    :func:`~riglib.config_web_scopes.discover_scopes`).
    """

    repo_root: Path
    job_store: ApplyJobStore = field(default_factory=ApplyJobStore)

    # ── scope resolution (the allowlist EVERY multi-repo endpoint must go through) ────────────
    def scopes(self) -> list[Any]:
        from .config_web_scopes import discover_scopes

        return discover_scopes(self.repo_root)

    def _resolve_scope(
        self, scope_id: str | None, *, default_on_missing: bool = False, scopes: list[Any] | None = None
    ) -> Any | None:
        """Resolve a request's scope id against the discovered allowlist.

        Returns ``None`` for an id that was given but does not resolve (the caller must refuse
        the request — never fall back silently for an EXPLICIT bad id). When ``scope_id`` is
        absent/empty and ``default_on_missing`` is set, returns the home scope instead (keeps
        ``/edit`` back-compatible with callers that never sent a ``scope`` field). Pass an
        already-fetched ``scopes`` list (e.g. from :meth:`render_page`) to avoid re-reading the
        repository registry twice per request.
        """
        from .config_web_scopes import default_scope, resolve_scope

        if scopes is None:
            scopes = self.scopes()
        if not scope_id:
            return default_scope(scopes) if default_on_missing else None
        return resolve_scope(scopes, scope_id)

    # ── page / fragment rendering ───────────────────────────────────────────────────────────────
    def render_page(self, scope_id: str | None = None) -> bytes:
        from .config_web_scopes import default_scope

        scopes = self.scopes()
        # An unresolvable ?scope= (unlike /edit's explicit-bad-id 400) degrades to the home tab —
        # a GET must always render SOMETHING, never error on a stale/copy-pasted URL. This is
        # NOT the same "default_on_missing" case _resolve_scope handles for a MISSING id (that
        # flag doesn't cover a present-but-invalid one), so the two-step fallback stays explicit
        # here rather than folding into one _resolve_scope call.
        scope = self._resolve_scope(scope_id, scopes=scopes) or default_scope(scopes)
        model = build_model(scope.repo_root or self.repo_root, global_only=scope.is_global)
        return build_html(model, scopes, scope).encode("utf-8")

    def handle_scope_fragment(self, scope_id: str | None) -> tuple[int, dict[str, Any]]:
        """``/api/scope`` — the area-sections HTML for a JS-driven, no-page-reload tab switch."""
        scope = self._resolve_scope(scope_id)
        if scope is None:
            return 404, {"ok": False, "error": f"unknown scope {scope_id!r}"}
        try:
            model = build_model(scope.repo_root or self.repo_root, global_only=scope.is_global)
        except Exception as exc:  # noqa: BLE001 — malformed config on disk, never a raw 500 traceback
            return 500, {"ok": False, "error": f"could not load scope: {exc}"}
        return 200, {"ok": True, "html": areas_fragment(model), "label": scope.label}

    # ── the edit write (routed to the owning layer, fail-closed) ───────────────────────────────
    def handle_edit(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Apply one edit from a POST body. Returns ``(http_status, json_body)``.

        An optional ``scope`` field (rig-cli#310) selects WHICH repo's ``rig.yaml`` a REPO-layer
        edit is validated/written against; absent (older callers, or a GLOBAL-only edit on the
        home scope) defaults to the home scope. A PRESENT but non-string ``scope`` (a malformed
        client payload) is a 400, not a silent fall-through to the default (caught in review).

        The GLOBAL scope may edit ONLY global-layer options: ``apply_edit`` itself routes a
        GLOBAL option to the global file regardless of anchor, but nothing previously stopped a
        Global-tab request from naming a REPO-layer key (``harness.auto_mode`` etc.), which
        ``apply_edit`` would then happily write into ``$HOME/rig.yaml`` if one happened to exist
        — an unrelated, hidden repo config the Global tab never renders and promises no overlay
        of (caught in review). Rejected here, before ``apply_edit`` ever runs.

        The ANCHOR also matters independent of the above: it decides which repo's cascade GATE 2
        (``_build_plan_gate``) validates against. A Global-tab edit anchors at ``$HOME`` WITH
        ``include_repo=False`` (matching :func:`riglib.config_web_plan.build_scope_plan`'s Global
        scope) — anchoring at the full cascade would let a broken/malformed ``$HOME/rig.yaml``
        (or the server's home repo's, before this fix) reject every Global-tab edit for a reason
        invisible on that tab (caught in review, two passes).
        """
        key = payload.get("key")
        raw = payload.get("value")
        scope_id = payload.get("scope")
        if not isinstance(key, str) or not isinstance(raw, str):
            return 400, {"ok": False, "error": "edit requires string 'key' and 'value'"}
        if scope_id is not None and not isinstance(scope_id, str):
            return 400, {"ok": False, "error": "'scope' must be a string"}
        scope = self._resolve_scope(scope_id, default_on_missing=True)
        if scope is None:
            return 400, {"ok": False, "error": f"unknown scope {scope_id!r}"}
        if scope.is_global:
            option = schema.option_for_key(key)
            if option is not None and option.layer != GLOBAL:
                return 400, {
                    "ok": False,
                    "error": f"{key!r} is a repo-layer option — it cannot be edited from the "
                    "Global scope",
                }
        anchor = scope.repo_root if scope.is_repo else Path.home()
        try:
            result = apply_edit(anchor, key, raw, include_repo=not scope.is_global)
        except EditError as exc:
            return 400, {"ok": False, "error": str(exc)}
        except OSError as exc:
            # a LATE failure (permissions, disk full, read-only FS, mkdir denied) after the value
            # passed coercion + validation — return a clean 500 JSON, never sever the connection
            # with a bare traceback the browser shows as "request failed".
            return 500, {"ok": False, "error": f"could not write config file: {exc}"}
        return 200, {
            "ok": True,
            "key": result["key"],
            "value": _fmt_value(result["value"]),
            "control_value": _control_value(result["key"], result["value"]),
            "layer": result["layer"],
            "file": result["file"],
        }

    # ── drift panel ──────────────────────────────────────────────────────────────────────────
    def handle_drift(self, scope_id: str | None) -> tuple[int, dict[str, Any]]:
        """``/api/drift`` — the SAME two-way drift engine ``rig status`` uses, for one scope."""
        from .config_web_plan import build_scope_plan, compute_scope_drift

        scope = self._resolve_scope(scope_id)
        if scope is None:
            return 404, {"ok": False, "error": f"unknown scope {scope_id!r}"}
        try:
            scope_plan = build_scope_plan(scope)
            payload = compute_scope_drift(scope_plan)
        except Exception as exc:  # noqa: BLE001 — a broken catalog/config must not 500-traceback
            return 500, {"ok": False, "error": f"drift check failed: {exc}"}
        payload["ok"] = True
        return 200, payload

    # ── plan preview + interactive apply ────────────────────────────────────────────────────────
    def handle_plan_preview(self, scope_id: str | None) -> tuple[int, dict[str, Any]]:
        """``/api/plan`` — the SAME plan.build() `rig apply info` uses, tagged per action."""
        from .config_web_plan import NoConfigLayerError, build_scope_plan, preview_payload

        scope = self._resolve_scope(scope_id)
        if scope is None:
            return 404, {"ok": False, "error": f"unknown scope {scope_id!r}"}
        try:
            scope_plan = build_scope_plan(scope)
            payload = preview_payload(scope_plan)
        except NoConfigLayerError as exc:
            return 400, {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — a broken catalog/config must not 500-traceback
            return 500, {"ok": False, "error": f"could not build plan: {exc}"}
        payload["ok"] = True
        return 200, payload

    def handle_apply(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """``/api/apply`` — start an apply job for the SELECTED actions of a previewed plan.

        Verifies the echoed ``fingerprint`` against a freshly rebuilt plan (409 on mismatch — the
        config changed since the browser previewed it, nothing is applied) and that no OTHER
        apply job is running process-wide (409). The accepted job runs through the real
        ``riglib.actions.runner.run_plan`` in a background thread — see
        :class:`~riglib.config_web_plan.ApplyJobStore`.
        """
        from .config_web_plan import ApplyBusyError, NoConfigLayerError, PlanStaleError

        scope_id = payload.get("scope")
        fingerprint = payload.get("fingerprint")
        skip_keys = payload.get("skip_keys")
        if not isinstance(scope_id, str) or not isinstance(fingerprint, str):
            return 400, {"ok": False, "error": "apply requires string 'scope' and 'fingerprint'"}
        if skip_keys is None:
            skip_keys = []
        if not isinstance(skip_keys, list) or not all(isinstance(k, str) for k in skip_keys):
            return 400, {"ok": False, "error": "'skip_keys' must be a list of strings"}
        scope = self._resolve_scope(scope_id)
        if scope is None:
            return 404, {"ok": False, "error": f"unknown scope {scope_id!r}"}
        try:
            job = self.job_store.start(
                scope, expected_fingerprint=fingerprint, skip_keys=set(skip_keys)
            )
        except (PlanStaleError, ApplyBusyError) as exc:
            return 409, {"ok": False, "error": str(exc)}
        except NoConfigLayerError as exc:
            return 400, {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — a broken catalog/config must not 500-traceback
            return 500, {"ok": False, "error": f"could not start apply: {exc}"}
        return 200, {"ok": True, "job_id": job.id}

    def handle_apply_status(self, job_id: str | None) -> tuple[int, dict[str, Any]]:
        """``/api/apply/status`` — poll one apply job's live per-action progress."""
        if not job_id:
            return 400, {"ok": False, "error": "missing 'job' query parameter"}
        job = self.job_store.get(job_id)
        if job is None:
            return 404, {"ok": False, "error": f"unknown apply job {job_id!r}"}
        return 200, {
            "ok": True,
            "scope": job.scope_id,
            "fingerprint": job.fingerprint,
            "done": job.done,
            "error": job.error,
            "actions": [
                {
                    "key": row.key, "kind": row.kind, "category": row.category, "item": row.item,
                    "describe": row.describe, "status": row.status, "detail": row.detail,
                }
                for row in job.actions
            ],
        }

    def make_handler(self) -> type:
        """Build the ``BaseHTTPRequestHandler`` subclass bound to this app's GET/POST behaviour.

        Extracted from :meth:`serve` so the live-socket TEST drives the SAME handler the server
        runs — path whitelist (``/`` / ``/index.html`` / ``/edit``, else 404), the CSRF guard, the
        ``application/json`` requirement, the body cap, the GET/POST error mapping. A test that
        hand-rolled its own handler would leave every one of those guards uncovered (the bug a
        prior version of the test had). ``http.server`` is imported lazily, keeping the module
        import-light.
        """
        import http.server

        app = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            # Bound the time any one request can hold the SINGLE-threaded server: a slow-loris
            # client trickling a (capped-size) body would otherwise stall every other request
            # forever. BaseHTTPRequestHandler.timeout sets the socket read timeout per connection.
            timeout = 15

            def _send(
                self, code: int, body: bytes, ctype: str, *, no_store: bool = False
            ) -> None:
                # On ANY non-2xx we may not have drained the request body (a rejected oversize/415
                # POST returns before reading it). With HTTP/1.1 keep-alive an unread body would be
                # reparsed as the NEXT request line and desync the connection — so close it on every
                # error response. (The page only issues one fetch per edit, so this costs nothing.)
                if code >= 300:
                    self.close_connection = True
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if no_store:
                    self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _send_json(self, code: int, body: dict[str, Any]) -> None:
                # Every JSON response is either always-fresh (drift/plan/apply-status) or an
                # explicit edit result — never something a browser should cache on its own
                # heuristics, especially the 500ms-polled /api/apply/status (found in review).
                self._send(
                    code, json.dumps(body).encode("utf-8"), "application/json; charset=utf-8",
                    no_store=True,
                )

            def _reject_foreign_host(self) -> bool:
                # DNS-rebinding guard on EVERY request (GET included): a foreign Host that resolves
                # to loopback is refused, so an attacker page can't read the config HTML or POST an
                # edit under a rebind. Returns True (and sends 403) when the host is not loopback.
                if not is_allowed_host(self.headers):
                    self._send(403, b"forbidden host", "text/plain; charset=utf-8")
                    return True
                return False

            def _route_and_query(self) -> tuple[str, dict[str, str]]:
                from urllib.parse import parse_qs, urlsplit

                split = urlsplit(self.path)
                params = {k: v[-1] for k, v in parse_qs(split.query).items()}
                return split.path, params

            def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
                if self._reject_foreign_host():
                    return
                route, params = self._route_and_query()
                if route in ("/", "/index.html"):
                    try:
                        page = app.render_page(params.get("scope"))
                    except Exception as exc:  # noqa: BLE001
                        # a malformed/invalid rig.yaml makes cfg.load raise — return a readable
                        # 500 instead of letting the exception escape do_GET (which closes the
                        # socket with no response, leaving the user a blank page and no diagnostic).
                        msg = f"config-web could not load the config: {exc}"
                        self._send(500, msg.encode("utf-8"), "text/plain; charset=utf-8")
                        return
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if route == "/api/scope":
                    code, body = app.handle_scope_fragment(params.get("scope"))
                    self._send_json(code, body)
                    return
                if route == "/api/drift":
                    # /api/drift is a GET (so it's easy for the JS to call on tab-load), but it
                    # triggers the SAME heavy compute as /api/plan (a full catalog scan + plan
                    # build + disk walk) — apply the SAME CSRF guard the mutating POSTs use, so a
                    # hostile page can't turn a blind cross-site GET into a compute-DoS against
                    # this single-threaded server (found in review). A local CLI client (curl,
                    # the tests) sends neither guard header and is unaffected.
                    if self._is_cross_site():
                        self._send_json(403, {"ok": False, "error": "cross-site request refused"})
                        return
                    code, body = app.handle_drift(params.get("scope"))
                    self._send_json(code, body)
                    return
                if route == "/api/apply/status":
                    code, body = app.handle_apply_status(params.get("job"))
                    self._send_json(code, body)
                    return
                self._send(404, b"not found", "text/plain; charset=utf-8")

            def _is_cross_site(self) -> bool:
                # CSRF / localhost-rebinding guard: refuse a write that a browser flags as
                # cross-site, so a hostile page the user visits can't drive one (see
                # is_cross_site_write). A local CLI client (curl/test) sends no such header. Pass
                # our bound port so an Origin on a DIFFERENT loopback port (another local service)
                # is a mismatch, not a match — same-origin is scheme+host+port.
                # The (host, port) the socket actually bound to. The stdlib types server_address
                # loosely (a generic socket address: tuple | str | Buffer); for our AF_INET TCP
                # server it is always a (host, port) tuple, so cast to that shape and read port[1].
                host_port = cast("tuple[str, int]", self.server.server_address)
                bound_port = host_port[1]
                return is_cross_site_write(self.headers, bound_port=bound_port)

            def _validated_content_length(self) -> int | None:
                """Parse + validate Content-Length. Returns ``None`` (and already sent 400/413)
                on a malformed/negative/oversize value — the caller must just return without
                sending anything else. Shared by :meth:`_drain_body` and :meth:`_read_json_body`
                so the cap/validation logic lives in exactly one place (found duplicated, in
                review).

                Parsed defensively: a missing header is 0; a MALFORMED value ("abc") must not let
                ``int()`` escape the handler; a NEGATIVE value must be rejected
                (``rfile.read(-1)`` reads until EOF — it would bypass the size cap AND block the
                single-threaded server on a slow/withheld body). Fail closed → 400/413.
                """
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "invalid Content-Length"})
                    return None
                if length < 0:
                    self._send_json(400, {"ok": False, "error": "invalid Content-Length"})
                    return None
                if length > MAX_EDIT_BODY_BYTES:
                    # reject (don't allocate) an implausibly large body — a memory-DoS guard.
                    self._send_json(413, {"ok": False, "error": "request body too large"})
                    return None
                return length

            def _reject_nonempty_body(self) -> bool:
                """For a route that expects NO body at all: validate Content-Length and reject a
                non-empty one WITHOUT reading it (never call ``rfile.read`` here — see the caller
                for why). Returns ``False`` (having already sent 400/413) on a malformed,
                oversize, OR merely non-empty Content-Length; ``True`` only for an absent/zero
                one. The caller must just return without sending anything else on ``False``.
                """
                length = self._validated_content_length()
                if length is None:
                    return False
                if length:
                    self._send_json(400, {"ok": False, "error": "this route does not accept a request body"})
                    return False
                return True

            def _read_json_body(self) -> tuple[bool, dict[str, Any]]:
                """Read + parse a JSON POST body under the shared guards. Returns ``(ok, payload)``.

                On failure this ALREADY sent the error response (400/413/415) and the second
                element is always ``{}`` (never a real payload) — the caller must just return
                without sending anything else.
                """
                ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if ctype != "application/json":
                    self._send_json(
                        415, {"ok": False, "error": "Content-Type must be application/json"}
                    )
                    return False, {}
                length = self._validated_content_length()
                if length is None:
                    return False, {}
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send_json(400, {"ok": False, "error": "invalid JSON body"})
                    return False, {}
                return True, (payload if isinstance(payload, dict) else {})

            def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
                if self._reject_foreign_host():
                    return
                route, params = self._route_and_query()
                if route not in ("/edit", "/api/plan", "/api/apply"):
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                # every mutating/compute-triggering POST shares the same CSRF guard — a hostile
                # page the user visits must not be able to drive an edit, a plan build, OR an
                # apply just by getting the browser to POST here.
                if self._is_cross_site():
                    self._send_json(403, {"ok": False, "error": "cross-site write refused"})
                    return
                if route == "/api/plan":
                    # /api/plan carries no body (the JS client sends none). REJECT rather than
                    # drain a non-empty one: draining means calling the blocking `rfile.read
                    # (length)`, which a client can trickle one byte every few seconds to hold the
                    # single-threaded server's only thread hostage well past the 15s PER-READ
                    # socket timeout (an inactivity timeout, not a whole-request deadline) — found
                    # in review as a slow-loris DoS the earlier "drain it" fix reintroduced. A 400
                    # response closes the connection (`_send`'s own `code >= 300` rule), so no
                    # keep-alive desync risk from leaving a body unread here either.
                    if not self._reject_nonempty_body():
                        return
                    code, body = app.handle_plan_preview(params.get("scope"))
                    self._send_json(code, body)
                    return
                ok, payload = self._read_json_body()
                if not ok:
                    return
                if route == "/edit":
                    code, body = app.handle_edit(payload)
                else:  # /api/apply
                    code, body = app.handle_apply(payload)
                self._send_json(code, body)

            def log_message(self, *args: Any) -> None:  # silence the default stderr access log
                return

        return _Handler

    def serve(self, *, port: int = DEFAULT_PORT, open_browser: bool = False) -> int:
        """Bind localhost and serve until interrupted. Returns the bound port. Blocks (foreground).

        ``port=0`` lets the OS pick a free port (used by tests). ``http.server`` / ``webbrowser``
        are imported lazily here so the module stays import-light.
        """
        import http.server
        import threading
        import webbrowser

        try:
            httpd = http.server.HTTPServer((HOST, port), self.make_handler())
        except OSError as exc:
            # the most common bind failure is a busy port (EADDRINUSE) — surface a clean,
            # actionable message instead of an uncaught traceback escaping the daemon/serve verb.
            raise OSError(
                f"config-web could not bind {HOST}:{port}: {exc}. "
                f"Is another instance already running? Try a different --port, or "
                f"`rig config-web status` / `stop`."
            ) from exc
        bound = httpd.server_address[1]
        url = f"http://{HOST}:{bound}/"
        print(f"rig config-web — serving at {url}  (Ctrl-C to stop)")
        # keep the open-browser Timer so a Ctrl-C inside its 0.4s window can cancel it — otherwise
        # it fires after server_close() and opens a tab pointing at a now-dead URL.
        timer = threading.Timer(0.4, lambda: webbrowser.open(url)) if open_browser else None
        if timer is not None:
            timer.start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")
        finally:
            if timer is not None:
                timer.cancel()
            httpd.server_close()
        return bound
