"""config-web — a local web UI to VIEW and EDIT the reconciled rig config.

What this is
------------
A self-contained, dependency-free web front-end over the SAME config engine that ``rig
setup`` (the wizard) and ``rig config get|set`` drive: :mod:`riglib.schema` (the option
REGISTRY) + :mod:`riglib.config` (the cascade loader, the dot-path set, the layer files).
It shows every reconciled AREA with its current effective value (the cascade of the global
``~/.config/rig/config.yaml`` + the repo ``./rig.yaml``), tagged with the layer that OWNS
each option, and lets the user change a value in the browser — routing the write to the
owning layer's file exactly like the wizard, then re-validating fail-closed.

How it is reached at runtime
----------------------------
``riglib.cli`` registers a ``config-web`` subcommand whose lifecycle (run/start/stop/status/
enable/disable) is wired through the shared ``agenttools_service`` library (the one service
manager every long-running server in the ecosystem shares — review dashboard, tg-ctl, the
daemon-supervisor). ``run`` serves :class:`ConfigWebApp` in the foreground; ``start`` daemonizes
it; ``enable`` installs an OS autostart (launchd / systemd --user) AND starts it now. The bare
``rig config-web`` (no subcommand) prints HELP and NEVER launches a server. The HTTP layer is
stdlib ``http.server`` (mirroring ``riglib.stats.render.web``): no CDN, no JS framework, inline
CSS, a tiny vanilla-JS fetch for the inline edit POST.

Invariants
----------
- **One config engine, three front-ends.** The view model and the edit write both go through
  :mod:`riglib.schema` (``AREAS`` / ``effective_value`` / ``coerce`` / ``writable_layer_for_category``)
  and :mod:`riglib.config` (``read_yaml_file`` / ``set_path`` / ``validate``). config-web adds NO
  parallel config logic — a new option in the registry shows up here for free, and an edit lands
  in the same file the wizard / ``config set`` would write.
- **Edits route to the OWNING layer.** A REPO option writes ``./rig.yaml``; a GLOBAL-only option
  (``gitignore`` / ``tg_ctl`` / ``tmux``) writes ``~/.config/rig/config.yaml`` — the same routing
  ``writable_layer_for_category`` enforces for the wizard, keeping a machine-wide block out of a
  committed repo file.
- **Fail-closed on write.** A value is coerced per the option's kind, written to a COPY of the
  target file's dict, the merged result is re-:func:`~riglib.config.validate`'d, and only then is
  the file rewritten. A bad value leaves the file untouched and returns an error to the browser.
- **Bind localhost only — but that is NOT a browser security boundary.** The server binds
  ``127.0.0.1`` (never ``0.0.0.0``); still, ANY web page the user visits can ``fetch`` a
  loopback URL, so a write is additionally gated by a same-origin/CSRF check
  (:func:`is_cross_site_write`, plus an ``application/json`` content-type requirement) — a
  ``cross-site`` ``Sec-Fetch-Site`` or a foreign ``Origin`` is refused 403. A non-browser CLI
  client (curl, the tests) sends neither header and is allowed; the threat model is a hostile
  web page, not local tooling. config-web does NOT run ``rig apply``: it edits the declared
  config; the user reconciles with ``rig apply`` (the same separation ``config set --no-apply``
  offers), so a stray browser tab can never converge the machine on its own.
- **A handler never leaks a traceback.** A malformed config on GET → 500 with a readable message
  (not a severed socket / blank page); a late write ``OSError`` on POST → 500 JSON; a busy bind
  port → a clean actionable ``OSError`` from :meth:`ConfigWebApp.serve`.

Stdlib-only at import time (the repo rule): ``yaml`` and ``http.server`` are imported lazily
inside the functions that need them, so importing this module stays dependency-light.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from . import config as cfg
from . import schema
from .layers import GLOBAL, REPO

# The interface (host + the rendered title) is shared by the HTTP server and the tests, so it is
# defined once. Localhost-only by invariant — see the module docstring.
HOST = "127.0.0.1"
DEFAULT_PORT = 8787
PAGE_TITLE = "rig config-web"

# Cap an /edit request body: a single {key,value} edit is tiny (well under 1 KiB). Binding to
# localhost does NOT stop a malicious page the user visits from POSTing to us (the browser will),
# so an unbounded `rfile.read(Content-Length)` is a trivial memory-DoS vector — reject anything
# implausibly large rather than allocate it. 64 KiB is generous for the legitimate payload.
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


def build_model(repo_root: Path) -> ConfigModel:
    """Read the cascaded config for ``repo_root`` and project it into the page :class:`ConfigModel`.

    Uses the SAME cascade loader the rest of rig uses (global then repo), and the SAME registry
    (:mod:`riglib.schema`) the wizard reads — so every reconciled area + option shows here with
    its live effective value, no parallel field list. ``effective_value`` applies the
    block-presence gating (a feature ``rig apply`` would skip reads as OFF, not its default), so
    the page never advertises a setting the reconciler ignores.
    """
    loaded = cfg.load(repo_root)
    merged = loaded.data
    areas: list[AreaView] = []
    for area in schema.AREAS:
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
    )


# ── the edit write (routed to the owning layer, fail-closed) ────────────────────────────────
class EditError(ValueError):
    """A rejected edit (unknown key, bad value, validation failure) — surfaced to the browser."""


def _target_path(repo_root: Path, option: schema.Option) -> Path:
    """The file an edit to ``option`` is written to — the owning layer, like the wizard's routing."""
    if option.layer == GLOBAL:
        return cfg.global_config_path()
    return cfg.repo_config_path(repo_root)


def apply_edit(repo_root: Path, key: str, raw_value: str) -> dict[str, Any]:
    """Coerce + write ONE option's value to its owning layer file, fail-closed. Returns a summary.

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

    config-web edits the DECLARED config; it does not run ``rig apply`` — the user reconciles
    explicitly (the same separation ``config set --no-apply`` offers).
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
        _build_plan_gate(repo_root)
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


def _build_plan_gate(repo_root: Path) -> None:
    """Build the reconcile plan from the on-disk cascade — the second, catalog-backed gate.

    This is GATE 2 in :func:`apply_edit`, mirroring ``_cmd_config_set``: it loads the cascaded
    config (global + repo, INCLUDING the just-written edit), scans the agent-tools catalog, and
    builds the plan. It does NOT execute anything (config-web never runs ``rig apply``) — it only
    proves the edited config can be reconciled. Catalog-backed errors a pure-schema
    :func:`config.validate` cannot catch (a bad ``agent_tools_source``, an unknown CI/MCP item)
    surface here; the caller rolls the file back on any exception. Heavy modules are imported
    lazily so this module stays import-light.
    """
    from .catalog import Catalog
    from .detect import detect_environment
    from .plan import build

    env = detect_environment(repo_root.resolve())
    loaded = cfg.load(env.repo_root)
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


def build_html(model: ConfigModel) -> str:
    """The whole page as one string (tested directly, no socket — mirrors stats.render.web)."""
    sections = "".join(_area_section(a) for a in model.areas)
    repo_state = "present" if model.repo_present else "absent (run `rig init`)"
    global_state = "present" if model.global_present else "absent (created on first global edit)"
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
            max-width:80vw; }}
  #toast.ok {{ background:#1e3a2a; color:#81c995; border:1px solid #34a853; }}
  #toast.err {{ background:#3a1e1e; color:#f28b82; border:1px solid #ea4335; }}
  #toast.show {{ opacity:1; }}
</style></head><body>
<h1>{html.escape(PAGE_TITLE)}</h1>
<p class="sub">view + edit the reconciled rig config · repo <code>{html.escape(str(model.repo_root))}</code><br>
repo <code>{html.escape(str(model.repo_path))}</code> ({repo_state}) ·
global <code>{html.escape(str(model.global_path))}</code> ({global_state})<br>
edits route to the owning layer; reconcile with <code>rig apply</code></p>
{sections}
<div id="toast"></div>
<script>
function toast(msg, ok) {{
  var t = document.getElementById('toast');
  t.textContent = msg; t.className = (ok ? 'ok' : 'err') + ' show';
  setTimeout(function() {{ t.className = t.className.replace(' show', ''); }}, 2600);
}}
// remember each control's last-committed state so a REJECTED edit can revert the visible value
// (the server rolled the file back, so the page must not keep showing the unsaved value).
function revert(el) {{
  if (el.getAttribute('data-kind') === 'bool') {{ el.checked = (el.dataset.committed === 'true'); }}
  else {{ el.value = (el.dataset.committed === undefined ? el.value : el.dataset.committed); }}
}}
async function edit(el) {{
  var key = el.getAttribute('data-key');
  var kind = el.getAttribute('data-kind');
  var value = (kind === 'bool') ? (el.checked ? 'true' : 'false') : el.value;
  try {{
    var r = await fetch('/edit', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{key: key, value: value}})
    }});
    var data = await r.json();
    if (r.ok && data.ok) {{
      el.dataset.committed = (kind === 'bool') ? value : (data.control_value ?? data.value);  // new committed state
      toast(key + ' = ' + data.value + '  →  ' + data.file, true);
    }} else {{
      revert(el);  // server rejected + rolled back → restore the control to its committed value
      toast('rejected: ' + (data.error || r.status), false);
    }}
  }} catch (e) {{ revert(el); toast('request failed: ' + e, false); }}
}}
// seed each control's committed baseline from its initial server-rendered value
document.querySelectorAll('[data-key]').forEach(function(el) {{
  el.dataset.committed = (el.getAttribute('data-kind') === 'bool') ? (el.checked ? 'true' : 'false') : el.value;
}});
</script>
</body></html>"""


# ── the HTTP application (stdlib http.server) ───────────────────────────────────────────────
@dataclass
class ConfigWebApp:
    """The config-web HTTP application. Constructed with a repo root; serves GET (view) + POST (edit).

    ``serve`` binds a localhost ``http.server`` and blocks until interrupted. The page model is
    rebuilt PER GET so a config change (from the browser, the CLI, or a hand-edit) is reflected on
    refresh without a restart. POST ``/edit`` applies one edit and returns JSON.
    """

    repo_root: Path

    def render_page(self) -> bytes:
        return build_html(build_model(self.repo_root)).encode("utf-8")

    def handle_edit(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Apply one edit from a POST body. Returns ``(http_status, json_body)``."""
        key = payload.get("key")
        raw = payload.get("value")
        if not isinstance(key, str) or not isinstance(raw, str):
            return 400, {"ok": False, "error": "edit requires string 'key' and 'value'"}
        try:
            result = apply_edit(self.repo_root, key, raw)
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

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                # On ANY non-2xx we may not have drained the request body (a rejected oversize/415
                # POST returns before reading it). With HTTP/1.1 keep-alive an unread body would be
                # reparsed as the NEXT request line and desync the connection — so close it on every
                # error response. (The page only issues one fetch per edit, so this costs nothing.)
                if code >= 300:
                    self.close_connection = True
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_json(self, code: int, body: dict[str, Any]) -> None:
                self._send(
                    code, json.dumps(body).encode("utf-8"), "application/json; charset=utf-8"
                )

            def _reject_foreign_host(self) -> bool:
                # DNS-rebinding guard on EVERY request (GET included): a foreign Host that resolves
                # to loopback is refused, so an attacker page can't read the config HTML or POST an
                # edit under a rebind. Returns True (and sends 403) when the host is not loopback.
                if not is_allowed_host(self.headers):
                    self._send(403, b"forbidden host", "text/plain; charset=utf-8")
                    return True
                return False

            def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
                if self._reject_foreign_host():
                    return
                if self.path not in ("/", "/index.html"):
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                try:
                    page = app.render_page()
                except Exception as exc:  # noqa: BLE001
                    # a malformed/invalid rig.yaml makes cfg.load raise — return a readable 500
                    # instead of letting the exception escape do_GET (which closes the socket with
                    # no response, leaving the user a blank page and no diagnostic).
                    msg = f"config-web could not load the config: {exc}"
                    self._send(500, msg.encode("utf-8"), "text/plain; charset=utf-8")
                    return
                self._send(200, page, "text/html; charset=utf-8")

            def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
                if self._reject_foreign_host():
                    return
                if self.path != "/edit":
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                # CSRF / localhost-rebinding guard: refuse a write that a browser flags as
                # cross-site, so a hostile page the user visits can't drive an edit (see
                # is_cross_site_write). A local CLI client (curl/test) sends no such header. Pass our
                # bound port so an Origin on a DIFFERENT loopback port (another local service) is a
                # mismatch, not a match — same-origin is scheme+host+port.
                # The (host, port) the socket actually bound to. The stdlib types server_address
                # loosely (a generic socket address: tuple | str | Buffer); for our AF_INET TCP
                # server it is always a (host, port) tuple, so cast to that shape and read port[1].
                host_port = cast("tuple[str, int]", self.server.server_address)
                bound_port = host_port[1]
                if is_cross_site_write(self.headers, bound_port=bound_port):
                    self._send_json(403, {"ok": False, "error": "cross-site write refused"})
                    return
                # Require an application/json content type. A "simple" cross-site POST can only set
                # text/plain (which dodges the CORS preflight) — demanding JSON closes that hole and
                # rejects a malformed client cleanly.
                ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if ctype != "application/json":
                    self._send_json(
                        415, {"ok": False, "error": "Content-Type must be application/json"}
                    )
                    return
                # Parse Content-Length defensively: a missing header is 0; a MALFORMED value
                # ("abc") must not let int() escape the handler; a NEGATIVE value must be rejected
                # (rfile.read(-1) reads until EOF — it would bypass the size cap AND block the
                # single-threaded server on a slow/withheld body). Fail closed → 400.
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "invalid Content-Length"})
                    return
                if length < 0:
                    self._send_json(400, {"ok": False, "error": "invalid Content-Length"})
                    return
                if length > MAX_EDIT_BODY_BYTES:
                    # reject (don't allocate) an implausibly large body — a memory-DoS guard.
                    self._send_json(413, {"ok": False, "error": "request body too large"})
                    return
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send_json(400, {"ok": False, "error": "invalid JSON body"})
                    return
                code, body = app.handle_edit(payload if isinstance(payload, dict) else {})
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
