"""Tests for `rig config-web` — the web UI over the reconciled config engine.

Covers the VIEW model (cascade -> areas -> effective values with owning-layer tags), the EDIT
write (routed to the owning layer file, coerced + fail-closed), the rendered HTML, the HTTP
app's edit handler, a live socket round-trip, and the CLI surface (bare command -> HELP; the
service descriptor built against the SHARED agenttools-service manager; the lib-absent path ->
a structured missing-dependency error, exit 127).

The service-lifecycle machinery itself (run/start/stop/status/enable/disable + launchd/systemd
autostart) is NOT re-tested here — it lives in (and is covered by) the shared
``agenttools_service`` library. These tests only cover the BRIDGE: the Service descriptor
config-web builds, the bare-command HELP contract, and the lib-absent fail-closed path.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from riglib import config as cfg
from riglib import config_web as cw
from riglib import config_web_service as cws
from riglib import errors
from riglib.layers import GLOBAL, REPO

# Is the shared service lib importable in THIS environment? CI installs `.[test]` only (no
# agenttools-service — it is NOT a declared dependency, neither lib is on PyPI), so the lib is
# absent there and the lib-absent path is exercised. A local dev checkout that has done
# `pip install -e <agent-tools>/lib/agenttools_daemon <agent-tools>/lib/agenttools_service` has it,
# so the lib-present tests run instead. Each service-seam test guards on this rather than assuming.
try:  # pragma: no cover - environment probe
    import agenttools_service as _svc_mod  # noqa: F401

    _HAVE_SERVICE_LIB = True
except ImportError:  # pragma: no cover - environment probe
    _HAVE_SERVICE_LIB = False


def _write_repo_config(repo_root: Path, body: str) -> None:
    (repo_root / "rig.yaml").write_text(body, encoding="utf-8")


def _editable_repo(repo_root: Path, fake_agent_tools: Path, body_tail: str = "") -> None:
    """A repo config an EDIT can pass GATE 2 against: it pins ``agent_tools_source`` at the fake
    catalog so ``apply_edit``'s catalog-backed plan build resolves (the test HOME has no real
    ~/xp/agent-tools). ``body_tail`` appends extra YAML (e.g. a harness block) under ``version``.
    """
    repo_root.mkdir(exist_ok=True)
    (repo_root / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n{body_tail}", encoding="utf-8"
    )


# -- view model ----------------------------------------------------------------------------
def test_build_model_reflects_repo_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_config(repo, "version: 1\nharness:\n  auto_mode: false\n")
    model = cw.build_model(repo)

    # every registered area shows up (no parallel field list -- driven by the schema registry)
    cats = {a.category for a in model.areas}
    assert {"skills", "harness", "ci", "tg_ctl", "gitignore"} <= cats

    harness = next(a for a in model.areas if a.category == "harness")
    auto = next(f for f in harness.fields if f.key == "harness.auto_mode")
    # the repo file set it false -> the live (effective) value is false, not the registry default
    assert auto.value is False
    assert auto.layer == REPO  # harness writes to the committed repo file
    kinds = next(f for f in harness.fields if f.key == "harness.kinds")
    assert kinds.value == []
    assert kinds.layer == REPO


def test_build_model_permissions_kind_absent_is_unpinned_with_harness_fanout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_config(
        repo,
        "version: 1\nharness:\n  kind: claude-code\n  kinds: [opencode]\npermissions:\n  enabled: true\n",
    )

    model = cw.build_model(repo)
    permissions = next(a for a in model.areas if a.category == "permissions")
    kind = next(f for f in permissions.fields if f.key == "permissions.kind")

    assert kind.value is None
    assert kind.default is None
    assert kind.layer == REPO


def test_apply_edit_permissions_kind_empty_clears_pin(tmp_path, fake_agent_tools):
    repo = tmp_path / "repo"
    _editable_repo(
        repo,
        fake_agent_tools,
        "harness:\n  kind: claude-code\n  kinds: [opencode]\npermissions:\n"
        "  enabled: true\n  kind: claude-code\n",
    )

    result = cw.apply_edit(repo, "permissions.kind", "")

    assert result["value"] is None
    data = cfg.read_yaml_file(repo / "rig.yaml")
    assert data["permissions"]["kind"] is None


def test_apply_edit_permissions_kind_null_overrides_global_pin(tmp_path, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    gpath = cfg.global_config_path()
    gpath.parent.mkdir(parents=True, exist_ok=True)
    gpath.write_text(
        "version: 1\npermissions: {enabled: true, kind: claude-code}\n",
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    _editable_repo(
        repo,
        fake_agent_tools,
        "harness:\n  kind: claude-code\n  kinds: [opencode]\npermissions:\n"
        "  enabled: true\n",
    )

    result = cw.apply_edit(repo, "permissions.kind", "")

    assert result["value"] is None
    assert cfg.read_yaml_file(repo / "rig.yaml")["permissions"]["kind"] is None
    assert cfg.load(repo).data["permissions"]["kind"] is None
    assert cfg.read_yaml_file(gpath)["permissions"]["kind"] == "claude-code"


def test_apply_edit_nullable_rejects_null_intermediate_without_clobber(tmp_path, fake_agent_tools):
    repo = tmp_path / "repo"
    original = (
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "permissions: null\n"
    )
    _editable_repo(repo, fake_agent_tools)
    (repo / "rig.yaml").write_text(original, encoding="utf-8")

    with pytest.raises(cw.EditError):
        cw.apply_edit(repo, "permissions.kind", "")

    assert (repo / "rig.yaml").read_text(encoding="utf-8") == original


def test_build_model_global_only_field_tagged_global(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_config(repo, "version: 1\n")
    model = cw.build_model(repo)
    tg = next(a for a in model.areas if a.category == "tg_ctl")
    enabled = next(f for f in tg.fields if f.key == "tg_ctl.enabled")
    # tg_ctl is a GLOBAL-only block -- an edit must land in the global config, never the repo file
    assert enabled.layer == GLOBAL
    assert enabled.layer_file == "~/.config/rig/config.yaml"


def test_build_model_paths_and_presence(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_config(repo, "version: 1\n")
    model = cw.build_model(repo)
    assert model.repo_present is True
    assert model.repo_path == repo / "rig.yaml"
    # no global config written under the isolated XDG home yet
    assert model.global_present is False


# -- edit write (routed to the owning layer, fail-closed) ----------------------------------
def test_apply_edit_writes_repo_layer(tmp_path, fake_agent_tools):
    repo = tmp_path / "repo"
    _editable_repo(repo, fake_agent_tools, "harness:\n  auto_mode: true\n")

    result = cw.apply_edit(repo, "harness.auto_mode", "false")
    assert result["layer"] == REPO
    assert result["value"] is False
    # the change landed in the repo file, and only there
    data = cfg.read_yaml_file(repo / "rig.yaml")
    assert data["harness"]["auto_mode"] is False


def test_apply_edit_global_only_writes_global_not_repo(tmp_path, fake_agent_tools):
    repo = tmp_path / "repo"
    _editable_repo(repo, fake_agent_tools)
    # the autouse home isolation points XDG_CONFIG_HOME under a throwaway home; assert the global
    # config is created THERE and the repo file is left untouched.
    result = cw.apply_edit(repo, "tg_ctl.enabled", "false")
    assert result["layer"] == GLOBAL
    gpath = cfg.global_config_path()
    assert gpath.is_file()
    assert cfg.read_yaml_file(gpath)["tg_ctl"]["enabled"] is False
    # the GLOBAL-only edit never touched the committed repo file
    assert "tg_ctl" not in cfg.read_yaml_file(repo / "rig.yaml")


def test_apply_edit_coerces_int(tmp_path, fake_agent_tools):
    repo = tmp_path / "repo"
    _editable_repo(repo, fake_agent_tools)
    result = cw.apply_edit(repo, "github.ruleset.required_reviews", "2")
    assert result["value"] == 2
    assert cfg.read_yaml_file(repo / "rig.yaml")["github"]["ruleset"]["required_reviews"] == 2


def test_apply_edit_repo_absent_is_refused(tmp_path):
    # a REPO edit must refuse when ./rig.yaml does not exist yet — same guard as `config set`
    # (editing from {} would let defaults mutate disk with no committed source of truth).
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(cw.EditError, match="rig init"):
        cw.apply_edit(repo, "harness.auto_mode", "false")
    assert not (repo / "rig.yaml").exists()


def test_apply_edit_reconcile_gate_rolls_back(tmp_path):
    # GATE 2: a config that passes schema validation but FAILS the catalog-backed plan build (an
    # unresolvable agent_tools_source) must roll the file back byte-for-byte and reject. The edit
    # never persists, exactly like `config set`'s rollback.
    repo = tmp_path / "repo"
    repo.mkdir()
    bad = tmp_path / "nope-agent-tools"  # a checkout that does not exist → Catalog.scan fails
    original = (
        f"version: 1\nagent_tools_source: {bad}\nharness:\n  auto_mode: true\n"
    )
    (repo / "rig.yaml").write_text(original, encoding="utf-8")
    with pytest.raises(cw.EditError, match="reconcile check"):
        cw.apply_edit(repo, "harness.auto_mode", "false")
    # the file is byte-for-byte unchanged — the rollback restored it
    assert (repo / "rig.yaml").read_text(encoding="utf-8") == original


def test_apply_edit_global_validated_in_isolation_even_if_repo_overrides(tmp_path, fake_agent_tools):
    # a GLOBAL edit must be validated IN ISOLATION (like `config set --global`): a broken global
    # value (a bad agent_tools_source) must be rejected + rolled back even when the repo rig.yaml
    # overrides agent_tools_source with a VALID one — otherwise the cascade masks the breakage and a
    # globally-broken config persists, failing in every OTHER repo. The repo points at the VALID
    # fake catalog so the cascade GATE 2 passes; only the per-layer isolation gate can catch the
    # bad global source — proving it runs.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n", encoding="utf-8"
    )
    # seed a global config that pins a BAD (nonexistent) agent_tools_source, then edit a global key
    bad = tmp_path / "nope-global-checkout"
    gpath = cfg.global_config_path()
    gpath.parent.mkdir(parents=True, exist_ok=True)
    gpath.write_text(f"version: 1\nagent_tools_source: {bad}\n", encoding="utf-8")
    before = gpath.read_text(encoding="utf-8")
    with pytest.raises(cw.EditError):
        cw.apply_edit(repo, "tg_ctl.enabled", "false")
    # the isolation gate caught the bad GLOBAL source and rolled the global file back
    assert gpath.read_text(encoding="utf-8") == before


def test_apply_edit_repo_write_uses_setupstate_header(tmp_path, fake_agent_tools):
    # config-web must serialize a layer file BYTE-FOR-BYTE the way `rig config set` does (one
    # writer, no parallel safe_dump): a REPO edit goes through SetupState.write, so the file
    # carries the committed-source-of-truth header.
    from riglib.state import SetupState

    repo = tmp_path / "repo"
    _editable_repo(repo, fake_agent_tools, "harness:\n  auto_mode: true\n")
    cw.apply_edit(repo, "harness.auto_mode", "false")

    written = (repo / "rig.yaml").read_text(encoding="utf-8")
    # the SetupState committed-source header is present (a leading `# yaml-language-server` modeline
    # may precede it, so check membership, not startswith — same header `config set` writes).
    assert "# rig.yaml" in written
    # and the body is exactly the canonical SetupState serialization of the edited data
    expected_body = SetupState.from_dict(cfg.read_yaml_file(repo / "rig.yaml")).to_yaml()
    assert expected_body in written


def test_apply_edit_global_write_is_headerless(tmp_path, fake_agent_tools):
    # A GLOBAL-only edit writes the machine-wide ~/.config/rig/config.yaml, which is NOT a
    # committed file — so it gets the plain SetupState.to_yaml dump, no repo header (mirroring
    # `config set --global`).
    repo = tmp_path / "repo"
    _editable_repo(repo, fake_agent_tools)
    cw.apply_edit(repo, "tg_ctl.enabled", "false")
    gtext = cfg.global_config_path().read_text(encoding="utf-8")
    assert not gtext.startswith("# rig.yaml")
    assert "tg_ctl:" in gtext


def test_apply_edit_drops_legacy_scope_key(tmp_path, fake_agent_tools):
    # `scope` is a removed key the cascade no longer uses; an edit must NOT re-emit it (mirroring
    # `config set` / config.load), so a browser edit to an OLD config that still carries `scope`
    # strips it on write rather than persisting dead config.
    repo = tmp_path / "repo"
    _editable_repo(repo, fake_agent_tools, "scope: repo\nharness:\n  auto_mode: true\n")
    cw.apply_edit(repo, "harness.auto_mode", "false")
    written = cfg.read_yaml_file(repo / "rig.yaml")
    assert "scope" not in written
    assert written["harness"]["auto_mode"] is False


def test_apply_edit_unknown_key_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_config(repo, "version: 1\n")
    with pytest.raises(cw.EditError, match="unknown config option"):
        cw.apply_edit(repo, "harness.not_a_real_key", "true")


def test_apply_edit_bad_bool_rejected_and_file_untouched(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    original = "version: 1\nharness:\n  auto_mode: true\n"
    _write_repo_config(repo, original)
    with pytest.raises(cw.EditError, match="expected yes/no"):
        cw.apply_edit(repo, "harness.auto_mode", "maybe")
    # fail-closed: the file is byte-for-byte unchanged
    assert (repo / "rig.yaml").read_text(encoding="utf-8") == original


def test_apply_edit_validation_failure_leaves_file_untouched(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    original = "version: 1\n"
    _write_repo_config(repo, original)
    # required_reviews must be an int >= 0 -- a negative is coerced fine by the registry (int) but
    # rejected by config.validate, so the write must abort with the file untouched.
    with pytest.raises(cw.EditError, match="config validation"):
        cw.apply_edit(repo, "github.ruleset.required_reviews", "-1")
    assert (repo / "rig.yaml").read_text(encoding="utf-8") == original


# -- HTML rendering ------------------------------------------------------------------------
def test_build_html_contains_areas_and_controls(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_config(repo, "version: 1\nharness:\n  auto_mode: false\n")
    page = cw.build_html(cw.build_model(repo))
    assert cw.PAGE_TITLE in page
    # an option key and its hint are rendered
    assert "harness.auto_mode" in page
    # the bool control is a checkbox toggle; auto_mode=false -> not checked
    assert 'data-key="harness.auto_mode"' in page
    # layer badges present
    assert "badge repo" in page or "badge global" in page
    # localhost-only invariant communicated; the edit JS endpoint is wired
    assert "/edit" in page


def test_field_control_escapes_text_value():
    # a text field whose live value carries HTML metacharacters must be escaped in the control
    # (validation may never allow such a value through the cascade, but the renderer must be safe
    # against any value it is handed -- defense in depth against a reflected-XSS footgun).
    f = cw.FieldView(
        key="models.schedule.time",
        kind="str",
        value="<script>alert(1)</script>",
        default="12:00",
        hint="daily run time",
        choices=(),
        layer=REPO,
    )
    control = cw._field_control(f)
    assert "<script>" not in control
    assert "&lt;script&gt;" in control


def test_enum_control_with_none_default_renders_unpinned_option():
    f = cw.FieldView(
        key="permissions.kind",
        kind="enum",
        value=None,
        default=None,
        hint="permission target",
        choices=("claude-code", "opencode"),
        layer=REPO,
    )

    control = cw._field_control(f)

    assert '<option value="" selected>(fan-out / unpinned)</option>' in control
    assert '<option value="claude-code"' in control


def test_field_row_escapes_hint():
    f = cw.FieldView(
        key="x.y", kind="bool", value=True, default=True,
        hint="a <em>hinted</em> & risky thing", choices=(), layer=GLOBAL,
    )
    row = cw._field_row(f)
    assert "<em>hinted</em>" not in row
    assert "&lt;em&gt;hinted&lt;/em&gt;" in row
    assert "&amp; risky" in row


# -- HTTP app edit handler -----------------------------------------------------------------
def test_app_handle_edit_ok(tmp_path, fake_agent_tools):
    repo = tmp_path / "repo"
    _editable_repo(repo, fake_agent_tools, "harness:\n  auto_mode: true\n")
    app = cw.ConfigWebApp(repo_root=repo)
    code, body = app.handle_edit({"key": "harness.auto_mode", "value": "false"})
    assert code == 200
    assert body["ok"] is True
    assert body["value"] == "false"
    assert body["layer"] == REPO
    assert cfg.read_yaml_file(repo / "rig.yaml")["harness"]["auto_mode"] is False


def test_app_handle_edit_nullable_enum_returns_select_control_value(tmp_path, fake_agent_tools):
    repo = tmp_path / "repo"
    _editable_repo(
        repo,
        fake_agent_tools,
        "harness:\n  kind: claude-code\n  kinds: [opencode]\npermissions:\n"
        "  enabled: true\n  kind: claude-code\n",
    )
    app = cw.ConfigWebApp(repo_root=repo)

    code, body = app.handle_edit({"key": "permissions.kind", "value": ""})

    assert code == 200
    assert body["value"] == "null"
    assert body["control_value"] == ""
    data = cfg.read_yaml_file(repo / "rig.yaml")
    assert data["permissions"]["kind"] is None

    code, rejected = app.handle_edit({"key": "permissions.kind", "value": "not-a-harness"})

    assert code == 400
    assert rejected["ok"] is False
    assert cfg.read_yaml_file(repo / "rig.yaml")["permissions"]["kind"] is None


def test_app_handle_edit_rejects_bad_value(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_config(repo, "version: 1\n")
    app = cw.ConfigWebApp(repo_root=repo)
    code, body = app.handle_edit({"key": "harness.auto_mode", "value": "maybe"})
    assert code == 400
    assert body["ok"] is False
    assert "expected yes/no" in body["error"]


def test_app_handle_edit_requires_string_fields(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_config(repo, "version: 1\n")
    app = cw.ConfigWebApp(repo_root=repo)
    code, body = app.handle_edit({"key": "harness.auto_mode", "value": True})
    assert code == 400
    assert body["ok"] is False


def test_app_render_page_roundtrips_edit(tmp_path, fake_agent_tools):
    """A GET after an edit reflects the new value -- the model is rebuilt per request."""
    repo = tmp_path / "repo"
    _editable_repo(repo, fake_agent_tools, "harness:\n  auto_mode: true\n")
    app = cw.ConfigWebApp(repo_root=repo)
    # before: the toggle is checked (auto_mode true)
    assert 'data-key="harness.auto_mode" data-kind="bool" checked' in app.render_page().decode()
    app.handle_edit({"key": "harness.auto_mode", "value": "false"})
    # after: a fresh page no longer carries the checked attribute for that field
    page = app.render_page().decode()
    assert 'data-key="harness.auto_mode" data-kind="bool" checked' not in page


# -- live server (end-to-end over a real socket, exercising the REAL handler guards) ---------
import contextlib  # noqa: E402
import http.server  # noqa: E402
import threading  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402


@contextlib.contextmanager
def _live_server(repo: Path):
    """Run the REAL ConfigWebApp handler on an OS-picked port; yield the base URL.

    Uses ``app.make_handler()`` — the SAME handler ``serve()`` runs — so the path whitelist, the
    CSRF guard, the Content-Type requirement, and the body cap are actually under test (a hand-
    rolled handler would bypass all of them). ``serve()`` itself blocks on ``serve_forever``, so
    the test owns the ``HTTPServer`` to shut it down deterministically.
    """
    app = cw.ConfigWebApp(repo_root=repo)
    httpd = http.server.HTTPServer((cw.HOST, 0), app.make_handler())
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://{cw.HOST}:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_serve_get_and_edit_over_socket(tmp_path, fake_agent_tools):
    repo = tmp_path / "repo"
    _editable_repo(repo, fake_agent_tools, "harness:\n  auto_mode: true\n")
    with _live_server(repo) as base:
        html = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        assert cw.PAGE_TITLE in html
        req = urllib.request.Request(
            base + "/edit",
            data=json.dumps({"key": "harness.auto_mode", "value": "false"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
        assert resp["ok"] is True and resp["value"] == "false"
    # the edit persisted to the repo file
    assert cfg.read_yaml_file(repo / "rig.yaml")["harness"]["auto_mode"] is False


def test_serve_unknown_path_is_404(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_config(repo, "version: 1\n")
    with _live_server(repo) as base:
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(base + "/secrets", timeout=5)
        assert ei.value.code == 404


def test_serve_rejects_cross_site_write(tmp_path):
    # a hostile page (Sec-Fetch-Site: cross-site) must NOT be able to drive an edit, even though
    # the server is bound to localhost — this is the CSRF/DNS-rebinding guard.
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_config(repo, "version: 1\nharness:\n  auto_mode: true\n")
    with _live_server(repo) as base:
        req = urllib.request.Request(
            base + "/edit",
            data=json.dumps({"key": "harness.auto_mode", "value": "false"}).encode(),
            headers={"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 403
    # the file was NOT modified by the refused write
    assert cfg.read_yaml_file(repo / "rig.yaml")["harness"]["auto_mode"] is True


def test_serve_rejects_non_json_content_type(tmp_path):
    # a "simple" cross-site POST can only send text/plain (it dodges the CORS preflight); requiring
    # application/json closes that hole. 415 Unsupported Media Type.
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_config(repo, "version: 1\nharness:\n  auto_mode: true\n")
    with _live_server(repo) as base:
        req = urllib.request.Request(
            base + "/edit",
            data=json.dumps({"key": "harness.auto_mode", "value": "false"}).encode(),
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 415
    assert cfg.read_yaml_file(repo / "rig.yaml")["harness"]["auto_mode"] is True


def test_serve_rejects_oversized_body(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_config(repo, "version: 1\n")
    with _live_server(repo) as base:
        big = b'{"key":"x","value":"' + b"a" * (cw.MAX_EDIT_BODY_BYTES + 10) + b'"}'
        req = urllib.request.Request(
            base + "/edit", data=big,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 413


def test_serve_rejects_cross_port_origin(tmp_path):
    # same-origin is scheme+host+PORT: an Origin on a DIFFERENT loopback port (another local
    # service the attacker controls) must be refused, not accepted because the host matches.
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_config(repo, "version: 1\nharness:\n  auto_mode: true\n")
    with _live_server(repo) as base:
        req = urllib.request.Request(
            base + "/edit",
            data=json.dumps({"key": "harness.auto_mode", "value": "false"}).encode(),
            headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:59999"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 403


def test_allowed_host_unit():
    # DNS-rebinding guard: a foreign Host (resolving to loopback) is rejected; loopback hosts and a
    # missing Host (HTTP/1.0 / raw client) are allowed.
    assert cw.is_allowed_host({"Host": "127.0.0.1:8787"}) is True
    assert cw.is_allowed_host({"Host": "localhost:8787"}) is True
    assert cw.is_allowed_host({"Host": "127.0.0.1"}) is True
    assert cw.is_allowed_host({}) is True  # no Host → bare client, allowed
    assert cw.is_allowed_host({"Host": "evil.test:8787"}) is False
    assert cw.is_allowed_host({"Host": "rig.local"}) is False
    # the IPv4-only server rejects a bracketed IPv6 literal (it never binds ::1)
    assert cw.is_allowed_host({"Host": "[::1]:8787"}) is False


def test_serve_rejects_foreign_host_on_get_and_post(tmp_path):
    # a DNS-rebinding page sends a foreign Host that resolves to 127.0.0.1; both GET and POST must
    # 403 it, so the attacker page can neither read the config HTML nor drive an edit.
    import socket

    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_config(repo, "version: 1\n")
    with _live_server(repo) as base:
        host, port = cw.HOST, int(base.rsplit(":", 1)[1])
        for method, path, extra in (("GET", "/", ""), ("POST", "/edit", "Content-Type: application/json\r\n")):
            body = b'{"key":"x","value":"y"}' if method == "POST" else b""
            raw = (
                f"{method} {path} HTTP/1.1\r\nHost: evil.test:{port}\r\n{extra}"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode() + body
            with socket.create_connection((host, port), timeout=5) as s:
                s.sendall(raw)
                resp = s.recv(4096).decode(errors="replace")
            assert "403" in resp.split("\r\n", 1)[0], (method, resp[:80])


def test_cross_site_sec_fetch_same_site_is_refused():
    # per the docstring, Sec-Fetch-Site: same-site (a different-PORT local attacker, same eTLD+1)
    # must be refused — only same-origin / none are accepted.
    assert cw.is_cross_site_write({"Sec-Fetch-Site": "same-site"}, bound_port=8787) is True
    assert cw.is_cross_site_write({"Sec-Fetch-Site": "same-origin"}, bound_port=8787) is False
    assert cw.is_cross_site_write({"Sec-Fetch-Site": "none"}, bound_port=8787) is False


def test_serve_bind_failure_raises_clean_oserror(tmp_path):
    # binding a busy port must surface a clean, actionable OSError from serve(), never a bare
    # traceback escaping the daemon. Occupy a port, then serve() on it must raise.
    import socket

    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_config(repo, "version: 1\n")
    occupied = socket.socket()
    occupied.bind((cw.HOST, 0))
    occupied.listen(1)
    busy_port = occupied.getsockname()[1]
    try:
        app = cw.ConfigWebApp(repo_root=repo)
        with pytest.raises(OSError, match="could not bind"):
            app.serve(port=busy_port)
    finally:
        occupied.close()


def test_cross_site_origin_port_logic_unit():
    # the same-origin port comparison directly: same port ok, different port / non-http rejected.
    same = {"Origin": "http://127.0.0.1:8787"}
    other = {"Origin": "http://127.0.0.1:9999"}
    https = {"Origin": "https://127.0.0.1:8787"}
    assert cw.is_cross_site_write(same, bound_port=8787) is False
    assert cw.is_cross_site_write(other, bound_port=8787) is True
    assert cw.is_cross_site_write(https, bound_port=8787) is True
    # no Origin / no Sec-Fetch (a CLI client) is allowed
    assert cw.is_cross_site_write({}, bound_port=8787) is False
    # FAIL-CLOSED: an Origin present but bound_port unknown (None) is rejected, never waved through
    assert cw.is_cross_site_write(same, bound_port=None) is True
    # but a no-Origin CLI client is still allowed even when bound_port is unknown
    assert cw.is_cross_site_write({}, bound_port=None) is False
    # an Origin with no explicit port (→ port 80) never matches our dev port → rejected
    assert cw.is_cross_site_write({"Origin": "http://127.0.0.1"}, bound_port=8787) is True


def test_serve_rejects_malformed_content_length(tmp_path):
    # a non-numeric Content-Length must not let int() escape the handler → clean 400, not a crash.
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_config(repo, "version: 1\n")
    with _live_server(repo) as base:
        # urllib computes Content-Length from data, so drive a raw socket to send a bogus header.
        import socket

        host, port = cw.HOST, int(base.rsplit(":", 1)[1])
        body = b'{"key":"harness.auto_mode","value":"false"}'
        raw = (
            f"POST /edit HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Content-Type: application/json\r\nContent-Length: abc\r\n\r\n"
        ).encode() + body
        with socket.create_connection((host, port), timeout=5) as s:
            s.sendall(raw)
            resp = s.recv(4096).decode(errors="replace")
        assert "400" in resp.split("\r\n", 1)[0]


def test_get_on_malformed_config_returns_500_not_blank(tmp_path):
    # an invalid rig.yaml must surface a readable 500, not escape do_GET (which would close the
    # socket and leave the browser a blank page with no diagnostic).
    repo = tmp_path / "repo"
    repo.mkdir()
    # a bad enum value config.validate rejects → build_model raises → handler must map to 500.
    _write_repo_config(repo, "version: 1\nharness:\n  kind: not-a-real-harness\n")
    with _live_server(repo) as base:
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(base + "/", timeout=5)
        assert ei.value.code == 500
        assert b"could not load the config" in ei.value.read()


def test_handle_edit_oserror_maps_to_500_and_rolls_back(tmp_path, monkeypatch):
    # a write IO failure (disk full / permissions) must map to a clean 500 JSON (server-side
    # problem, not a bad request) AND leave the file byte-for-byte unchanged — never a partial
    # write or a severed connection.
    repo = tmp_path / "repo"
    repo.mkdir()
    original = "version: 1\nharness:\n  auto_mode: true\n"
    _write_repo_config(repo, original)

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(cw, "_write_layer", _boom)
    app = cw.ConfigWebApp(repo_root=repo)
    code, body = app.handle_edit({"key": "harness.auto_mode", "value": "false"})
    assert code == 500
    assert body["ok"] is False
    assert "disk full" in body["error"]
    # rolled back: the file is exactly as it was before the failed edit
    assert (repo / "rig.yaml").read_text(encoding="utf-8") == original


# -- service seam + CLI surface ------------------------------------------------------------
def test_bare_config_web_prints_help_never_launches(capsys):
    from riglib.cli import main

    rc = main(["config-web"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "usage: rig config-web" in out
    # the lifecycle verbs are documented in the help
    assert "run" in out and "enable" in out and "disable" in out


def test_register_exposes_config_web_subcommand_and_verb():
    from riglib.cli import build_parser

    parser = build_parser()
    # a bare `config-web` routes to the config-web command with no verb selected
    ns = parser.parse_args(["config-web"])
    assert ns.command == "config-web"
    assert getattr(ns, "config_web_verb", None) is None
    # a lifecycle verb is captured in `config_web_verb`
    ns2 = parser.parse_args(["config-web", "enable"])
    assert ns2.command == "config-web"
    assert ns2.config_web_verb == "enable"


def test_building_parser_does_not_import_service_lib():
    # parser construction runs on EVERY `rig` invocation (incl. `rig --help`); it must NOT import
    # the optional sibling `agenttools_service` (the lazy-import invariant). Build the parser in a
    # subprocess with the lib evicted from sys.modules and assert it was not (re)imported.
    import subprocess
    import sys

    code = (
        "import sys;"
        "sys.modules.pop('agenttools_service', None);"
        "from riglib.cli import build_parser;"
        "build_parser();"
        "print('IMPORTED' if 'agenttools_service' in sys.modules else 'clean')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(cws._riglib_parent())},
    )
    assert out.stdout.strip().endswith("clean"), out.stdout + out.stderr


def test_repo_root_resolves_git_root_from_subdir(tmp_path):
    # `rig config-web` run from a SUBDIR must serve the repo ROOT's rig.yaml, not subdir/rig.yaml —
    # _repo_root resolves the git root like config set / setup. Without a git repo it falls back to
    # the dir itself.
    import argparse as _ap
    import subprocess

    repo = tmp_path / "repo"
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "rig.yaml").write_text("version: 1\n", encoding="utf-8")
    args = _ap.Namespace(cwd=str(sub))
    assert cws._repo_root(args) == repo.resolve()


def test_target_args_before_verb_are_not_clobbered():
    # `rig config-web -C /repo --port 9000 status` must keep -C/--port from the config-web level;
    # the per-verb subparser copies use default=SUPPRESS so they don't overwrite the parent value.
    # This guards the bug the lib-present branch fixes AND the lib-absent fallback (both must use
    # suppress_default=True). The test runs against whichever branch the env exercises.
    from riglib.cli import build_parser

    parser = build_parser()
    ns = parser.parse_args(["config-web", "-C", "/some/repo", "--port", "9000", "status"])
    assert ns.config_web_verb == "status"
    assert ns.cwd == "/some/repo"
    assert ns.port == 9000
    # the natural post-verb form must resolve to the same dests
    ns2 = parser.parse_args(["config-web", "status", "-C", "/other", "--port", "9100"])
    assert ns2.cwd == "/other"
    assert ns2.port == 9100
    # the INTERNAL _serve verb must also preserve a pre-verb --port (its subparser is suppress_default
    # too) — the daemon argv puts --port after _serve, but the parent-first form must not clobber it.
    ns3 = parser.parse_args(["config-web", "--port", "9000", "_serve"])
    assert ns3.config_web_verb == cws.SERVE_VERB
    assert ns3.port == 9000


def test_invalid_port_is_rejected_at_parse_time():
    # a bad --port (0, out of range, non-numeric) must fail at PARSE time with a clean usage error
    # (SystemExit 2), never reach the server as a raw OverflowError/ValueError.
    from riglib.cli import build_parser

    parser = build_parser()
    for bad in ("0", "70000", "-1", "abc"):
        with pytest.raises(SystemExit):
            parser.parse_args(["config-web", "start", "--port", bad])
    # a valid port parses fine
    ns = parser.parse_args(["config-web", "start", "--port", "9000"])
    assert ns.port == 9000


def test_serve_verb_routes_to_app_serve_not_lifecycle(monkeypatch, tmp_path):
    # the internal _serve verb must run the foreground server (ConfigWebApp.serve) directly,
    # bypassing the service manager — it is what run/start/enable exec, never a lifecycle op.
    from riglib.cli import build_parser

    calls: dict = {}

    def _fake_serve(self, *, port=cw.DEFAULT_PORT, open_browser=False):
        calls["port"] = port
        calls["repo_root"] = self.repo_root
        calls["open_browser"] = open_browser
        return port

    monkeypatch.setattr(cw.ConfigWebApp, "serve", _fake_serve)
    args = build_parser().parse_args(
        ["config-web", cws.SERVE_VERB, "--port", "9321", "-C", str(tmp_path)]
    )
    rc = cws.dispatch_cli(args)
    assert rc == 0
    assert calls["port"] == 9321
    assert calls["repo_root"] == tmp_path.resolve()
    # the daemon target never auto-opens a browser
    assert calls["open_browser"] is False


def test_serve_argv_is_absolute_python_and_internal_verb(tmp_path):
    # the daemon's foreground command must start with an ABSOLUTE interpreter (launchd runs with a
    # minimal PATH) and target the INTERNAL _serve verb with the captured repo root + port. It uses
    # a `-c` bootstrap (NOT `-m riglib`) so a SYMLINK-installed rig — where riglib is not on the
    # default sys.path — still imports the package from a daemon's arbitrary cwd.
    argv = cws._serve_argv(tmp_path, 9123)
    assert argv[0] == sys.executable
    assert Path(argv[0]).is_absolute()
    assert argv[1] == "-c"
    # the bootstrap prepends the riglib parent dir to sys.path and calls the CLI main
    assert "sys.path.insert" in argv[2] and "from riglib.cli import main" in argv[2]
    assert str(cws._riglib_parent()) in argv[2]
    assert argv[3:5] == ["config-web", cws.SERVE_VERB]
    assert "--port" in argv and "9123" in argv
    assert "-C" in argv and str(tmp_path) in argv


def test_serve_argv_actually_starts_a_server(tmp_path, fake_agent_tools):
    # end-to-end: the daemon argv, run as a SUBPROCESS from an unrelated cwd, must actually boot the
    # server (proving the `-c` bootstrap finds riglib without a pip install / a `rig` on PATH).
    import socket
    import subprocess
    import time
    import urllib.request

    repo = tmp_path / "repo"
    _editable_repo(repo, fake_agent_tools)
    # pick a free port deterministically
    with socket.socket() as s:
        s.bind((cw.HOST, 0))
        port = s.getsockname()[1]
    argv = cws._serve_argv(repo, port)
    # run from a cwd with NO riglib in it, with a minimal env (no PYTHONPATH leak), mirroring a
    # launchd job — the bootstrap must still import riglib.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.Popen(argv, cwd=str(tmp_path), env=env)
    try:
        url = f"http://{cw.HOST}:{port}/"
        deadline = time.time() + 10
        body = None
        while time.time() < deadline:
            try:
                body = urllib.request.urlopen(url, timeout=1).read().decode()
                break
            except OSError:
                time.sleep(0.2)
        assert body is not None and cw.PAGE_TITLE in body
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.skipif(not _HAVE_SERVICE_LIB, reason="agenttools-service not installed")
def test_build_service_descriptor_against_real_lib(tmp_path):
    # with the lib present, the descriptor binds the internal _serve argv, the port/host, and the
    # rig tool namespace -- so the lib derives a `com.agenttools.rig.config-web-<hash>` label.
    svc = cws.build_service(tmp_path, port=9123)
    # the service name is PER-REPO (base + a short repo-path hash) so different repos never collide
    assert svc.name == cws._service_name(tmp_path)
    assert svc.name.startswith(cws.SERVICE_NAME + "-")
    assert svc.tool == cws.SERVICE_TOOL
    assert svc.port == 9123
    assert svc.host == cw.HOST
    assert svc.argv[0] == sys.executable
    assert cws.SERVE_VERB in svc.argv
    # the lib's derived identity uses the rig tool namespace + the per-repo name
    assert svc.label == f"com.agenttools.rig.{svc.name}"
    assert svc.url == f"http://{cw.HOST}:9123"


def test_service_name_is_per_repo_and_slug_safe(tmp_path):
    # two different repo roots get different service names (no pidfile/autostart collision); the
    # name stays slug-safe so the lib can derive a valid launchd label / systemd unit.
    a = cws._service_name(tmp_path / "repo-a")
    b = cws._service_name(tmp_path / "repo-b")
    assert a != b
    assert a.startswith("config-web-")
    assert all(c.isalnum() or c in "_.-" for c in a)
    # stable: same repo → same name across calls
    assert cws._service_name(tmp_path / "repo-a") == a


@pytest.mark.skipif(not _HAVE_SERVICE_LIB, reason="agenttools-service not installed")
def test_dispatch_status_through_real_lib(tmp_path, monkeypatch):
    # lib-present coverage of the lifecycle bridge: dispatch_cli → svc_mod.run_action(manager, verb)
    # must run a real lifecycle verb. `status` on a never-started service reports "stopped" (exit 3
    # per the lib's contract), not an AttributeError — proving run_action is the right entry point.
    import argparse as _ap

    args = _ap.Namespace(config_web_verb="status", cwd=str(tmp_path), port=cw.DEFAULT_PORT,
                         _config_web_parser=None)
    rc = cws.dispatch_cli(args)
    # nothing running → a NON-zero "not running" exit. Assert != 0 (not the literal 3) so the test
    # doesn't pin an external lib's exact exit-code constant — the point is the bridge reached
    # run_action and ran the verb, not an AttributeError.
    assert rc != 0


def test_serve_verb_busy_port_is_structured_error(tmp_path):
    # `rig config-web _serve`/`run` on an occupied port must raise a STRUCTURED RigError (rendered
    # as the what/why/fix block + exit 2 by cli.main), not a bare OSError traceback.
    import argparse as _ap
    import socket

    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_config(repo, "version: 1\n")
    occupied = socket.socket()
    occupied.bind((cw.HOST, 0))
    occupied.listen(1)
    busy = occupied.getsockname()[1]
    try:
        args = _ap.Namespace(config_web_verb=cws.SERVE_VERB, cwd=str(repo), port=busy,
                             _config_web_parser=None)
        with pytest.raises(errors.RigError) as ei:
            cws.dispatch_cli(args)
        assert ei.value.exit_code == errors.EXIT_CONFIG
        assert "could not start" in ei.value.what
    finally:
        occupied.close()


@pytest.mark.skipif(_HAVE_SERVICE_LIB, reason="agenttools-service IS installed locally")
def test_config_web_verb_without_lib_is_missing_dep(capsys):
    # In an environment WITHOUT the shared lib (e.g. CI's `.[test]` install), a lifecycle verb
    # fails closed with a structured missing-dependency error (exit 127) + install guidance --
    # never an ImportError crash.
    from riglib.cli import main

    rc = main(["config-web", "status"])
    out = capsys.readouterr().out
    assert rc == errors.EXIT_MISSING_DEP
    assert "agenttools-service" in out
    # the install hint that reaches the user must target rig's interpreter (end-to-end wiring of
    # _INSTALL_HINT through MissingDepError's `fix:` line), not a bare untargeted `uv pip install`.
    assert "uv pip install --python" in out
    assert shlex.quote(sys.executable) in out  # quote-safe: matches the printed (shlex-quoted) path


@pytest.mark.skipif(_HAVE_SERVICE_LIB, reason="agenttools-service IS installed locally")
def test_load_service_module_raises_missing_dep_when_absent():
    with pytest.raises(errors.MissingDepError, match="agenttools-service"):
        cws._load_service_module()


def test_install_hint_targets_rig_interpreter_via_uv():
    """The config-web missing-dep hint must direct uv at rig's OWN interpreter (`--python
    <sys.executable>`), not a bare `uv pip install` that fails outside a venv / mutates the wrong
    one. Guards against a regression back to the un-targeted form."""
    hint = cws._INSTALL_HINT
    assert hint.startswith("uv pip install --python ")
    assert shlex.quote(sys.executable) in hint  # the exact interpreter rig runs under (quote-safe)
    # editable installs of BOTH nested libs, never a bare untargeted `uv pip install -e`.
    assert "-e <agent-tools>/lib/agenttools_daemon" in hint
    assert "-e <agent-tools>/lib/agenttools_service" in hint
    assert "uv pip install -e" not in hint
