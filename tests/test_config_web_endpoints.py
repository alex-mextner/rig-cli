"""Tests for config-web's machine-wide HTTP endpoints (rig-cli#310):

/api/scope (no-reload tab switch), /api/drift, /api/plan (preview), /api/apply (start),
/api/apply/status (poll) -- plus the scope ALLOWLIST every one of them must enforce (never trust
an arbitrary path/scope id from the browser).
"""

from __future__ import annotations

import contextlib
import http.server
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from riglib import config_web as cw


def _small_config_body(fake_agent_tools: Path) -> str:
    return (
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {universal: {all: true}, by_type: {enable: []}}\n"
        "agent_hooks: {enabled: false}\nci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n"
        "harness: {enabled: false}\npermissions: {enabled: false}\n"
        "agents_md: {enabled: false}\ngitignore: {enabled: false}\n"
        "tmux: {enabled: false}\ntg_ctl: {enabled: false}\nmodels: {enabled: false}\n"
    )


def _isolated_repo(tmp_path: Path, fake_agent_tools: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text(_small_config_body(fake_agent_tools), encoding="utf-8")
    return repo


def _wait(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise TimeoutError("condition not met in time")


# ── scope allowlist ──────────────────────────────────────────────────────────────────────────


def test_handle_scope_fragment_rejects_unknown_scope(tmp_path, fake_agent_tools, monkeypatch):
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    code, body = app.handle_scope_fragment("/definitely/not/a/discovered/scope")
    assert code == 404
    assert body["ok"] is False


def test_handle_drift_rejects_unknown_scope(tmp_path, fake_agent_tools, monkeypatch):
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    code, body = app.handle_drift("/etc/passwd")
    assert code == 404
    assert body["ok"] is False


def test_handle_plan_preview_rejects_unknown_scope(tmp_path, fake_agent_tools, monkeypatch):
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    code, body = app.handle_plan_preview("../../somewhere-else")
    assert code == 404
    assert body["ok"] is False


def test_handle_apply_rejects_unknown_scope(tmp_path, fake_agent_tools, monkeypatch):
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    code, body = app.handle_apply({"scope": "/not/discovered", "fingerprint": "x", "skip_keys": []})
    assert code == 404
    assert body["ok"] is False


def test_handle_edit_rejects_explicit_unknown_scope(tmp_path, fake_agent_tools, monkeypatch):
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    code, body = app.handle_edit({"key": "gitignore.enabled", "value": "false", "scope": "/nope"})
    assert code == 400
    assert body["ok"] is False


def test_handle_edit_missing_scope_defaults_to_home(tmp_path, fake_agent_tools, monkeypatch):
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    code, body = app.handle_edit({"key": "gitignore.enabled", "value": "false"})
    assert code == 200
    assert body["ok"] is True


def test_render_page_bad_scope_degrades_to_home_not_error(tmp_path, fake_agent_tools, monkeypatch):
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    page = app.render_page("/totally/bogus").decode()
    assert "<title>" in page  # rendered fine, no 500/exception


# ── scope fragment + drift + plan happy paths ───────────────────────────────────────────────


def test_handle_scope_fragment_returns_areas_html(tmp_path, fake_agent_tools, monkeypatch):
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    scope_id = app.scopes()[0].id
    code, body = app.handle_scope_fragment(scope_id)
    assert code == 200
    assert body["ok"] is True
    assert "<section" in body["html"]


def test_handle_drift_reports_missing_before_apply(tmp_path, fake_agent_tools, monkeypatch):
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    scope_id = app.scopes()[0].id
    code, body = app.handle_drift(scope_id)
    assert code == 200
    assert body["ok"] is True
    assert body["in_sync"] is False


def test_handle_plan_preview_returns_tagged_actions(tmp_path, fake_agent_tools, monkeypatch):
    from riglib.action_tags import CATEGORIES

    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    scope_id = app.scopes()[0].id
    code, body = app.handle_plan_preview(scope_id)
    assert code == 200
    assert body["ok"] is True
    assert body["fingerprint"]
    for row in body["actions"]:
        assert row["tag"]["category"] in CATEGORIES


# ── apply start + poll ───────────────────────────────────────────────────────────────────────


def test_handle_apply_stale_fingerprint_refused(tmp_path, fake_agent_tools, monkeypatch):
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    scope_id = app.scopes()[0].id
    code, body = app.handle_apply({"scope": scope_id, "fingerprint": "stale", "skip_keys": []})
    assert code == 409
    assert body["ok"] is False


def test_handle_apply_and_poll_status_end_to_end(tmp_path, fake_agent_tools, monkeypatch):
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    scope_id = app.scopes()[0].id
    _, preview = app.handle_plan_preview(scope_id)

    code, start = app.handle_apply(
        {"scope": scope_id, "fingerprint": preview["fingerprint"], "skip_keys": []}
    )
    assert code == 200
    job_id = start["job_id"]

    def _done():
        _, status = app.handle_apply_status(job_id)
        return status["done"]

    _wait(_done)
    _, status = app.handle_apply_status(job_id)
    assert status["ok"] is True
    assert status["error"] is None
    assert all(a["status"] != "queued" for a in status["actions"])
    home = repo.parent / "home"
    assert (home / ".claude" / "skills").exists()


def test_handle_apply_status_unknown_job_404(tmp_path, fake_agent_tools, monkeypatch):
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    code, body = app.handle_apply_status("not-a-real-job")
    assert code == 404
    assert body["ok"] is False


# ── live server round trip (real HTTP, real handler guards) ────────────────────────────────


@contextlib.contextmanager
def _live_server(app: cw.ConfigWebApp):
    httpd = http.server.HTTPServer((cw.HOST, 0), app.make_handler())
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://{cw.HOST}:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()


def test_live_api_plan_and_apply_round_trip(tmp_path, fake_agent_tools, monkeypatch):
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    scope_id = app.scopes()[0].id
    with _live_server(app) as base:
        req = urllib.request.Request(f"{base}/api/plan?scope={scope_id}", method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=5) as resp:
            plan = json.loads(resp.read())
        assert plan["ok"] is True

        body = json.dumps(
            {"scope": scope_id, "fingerprint": plan["fingerprint"], "skip_keys": []}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/api/apply", method="POST", data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            start = json.loads(resp.read())
        assert start["ok"] is True
        job_id = start["job_id"]

        def _poll_done():
            with urllib.request.urlopen(f"{base}/api/apply/status?job={job_id}", timeout=5) as r:
                return json.loads(r.read())["done"]

        _wait(_poll_done)
        with urllib.request.urlopen(f"{base}/api/apply/status?job={job_id}", timeout=5) as r:
            final = json.loads(r.read())
        assert final["error"] is None


def test_handle_apply_busy_refused_while_job_running(tmp_path, fake_agent_tools, monkeypatch):
    import riglib.actions.runner as runner_mod

    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    scope_id = app.scopes()[0].id
    _, preview = app.handle_plan_preview(scope_id)

    release = threading.Event()
    original_run_plan = runner_mod.run_plan

    def _blocking(plan, **kwargs):
        release.wait(timeout=5)
        return original_run_plan(plan, **kwargs)

    monkeypatch.setattr(runner_mod, "run_plan", _blocking)

    code1, start1 = app.handle_apply(
        {"scope": scope_id, "fingerprint": preview["fingerprint"], "skip_keys": []}
    )
    assert code1 == 200
    code2, start2 = app.handle_apply(
        {"scope": scope_id, "fingerprint": preview["fingerprint"], "skip_keys": []}
    )
    assert code2 == 409
    assert start2["ok"] is False
    release.set()

    def _done():
        _, status = app.handle_apply_status(start1["job_id"])
        return status["done"]

    _wait(_done)


# ── second-review-pass regressions ──────────────────────────────────────────────────────────


def test_edit_on_non_home_repo_scope_writes_that_repos_rigyaml(tmp_path, fake_agent_tools, monkeypatch):
    """The headline machine-wide behavior: a server started in repo A can edit repo B's own
    ./rig.yaml -- untested until now (found in review: every prior edit test used the home scope
    or the Global scope, so a routing bug anchoring at the WRONG repo would slip through green).
    """
    from riglib.repository_registry import RepositoryEntry, RepositoryRegistry

    home_repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    (other_repo / "rig.yaml").write_text(_small_config_body(fake_agent_tools), encoding="utf-8")

    registry = RepositoryRegistry(
        roots=[str(tmp_path)],
        repositories=[
            RepositoryEntry(
                id="other", path=str(other_repo), name="other-repo", root=str(tmp_path)
            )
        ],
    )
    registry.save()  # writes to $XDG_CONFIG_HOME/rig/repositories.json (monkeypatched above)

    app = cw.ConfigWebApp(repo_root=home_repo)
    other_scope = next(s for s in app.scopes() if s.repo_root == other_repo.resolve())
    home_rigyaml_before = (home_repo / "rig.yaml").read_text(encoding="utf-8")

    code, body = app.handle_edit(
        {"key": "harness.auto_mode", "value": "true", "scope": other_scope.id}
    )

    assert code == 200, body
    assert body["ok"] is True
    assert body["file"] == str((other_repo / "rig.yaml").resolve())
    assert "auto_mode: true" in (other_repo / "rig.yaml").read_text(encoding="utf-8")
    assert (home_repo / "rig.yaml").read_text(encoding="utf-8") == home_rigyaml_before, (
        "the home repo's rig.yaml must be untouched by an edit scoped to the OTHER repo"
    )


def test_global_tab_edit_not_blocked_by_broken_home_repo_rigyaml(tmp_path, fake_agent_tools, monkeypatch):
    """A Global-tab edit must anchor at $HOME, not the server's home REPO.

    Anchoring at self.repo_root would run GATE 2 (_build_plan_gate) over the home repo's FULL
    cascade -- a malformed/broken home-repo rig.yaml would then reject every Global-tab edit for a
    reason invisible on that tab (found in review).
    """
    from riglib.config_web_scopes import GLOBAL_SCOPE_ID

    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    # break the SERVER's home repo's rig.yaml -- it must not affect a Global-only edit
    (repo / "rig.yaml").write_text("not: [valid: yaml: at all", encoding="utf-8")
    app = cw.ConfigWebApp(repo_root=repo)

    code, body = app.handle_edit(
        {"key": "gitignore.enabled", "value": "false", "scope": GLOBAL_SCOPE_ID}
    )

    assert code == 200, body
    assert body["ok"] is True
    assert body["layer"] == "GLOBAL"


def test_page_html_escapes_drift_and_plan_interpolation(tmp_path, fake_agent_tools, monkeypatch):
    """The JS render paths must escape server data before innerHTML -- structural regression pin.

    Actual browser-level XSS execution isn't testable here without a browser; this pins the
    presence of the esc() helper and that it wraps the previously-unescaped interpolations (found
    in review: i.detail/i.category/a.describe/a.target were interpolated raw).
    """
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    page = app.render_page().decode()

    assert "function esc(" in page
    assert "esc(i.direction)" in page and "esc(i.category)" in page and "esc(i.detail)" in page
    assert "esc(a.describe)" in page and "esc(a.target)" in page and "esc(a.tag.detail)" in page
    assert 'data-key="' + "' + esc(a.key) + '" in page


def test_page_known_panel_groups_caps_and_escapes(tmp_path, fake_agent_tools, monkeypatch):
    """The known-items panel renders ONE row per (container/category, origin) with the names
    capped -- the CLI's `_print_known_groups` shape -- never one row per entry: a kept allowlist
    runs to hundreds of entries and a per-entry dump would flood the panel (found in review).
    Structural pin like the esc() test above; the JS itself needs a browser.
    """
    from riglib.cli import _KNOWN_NAMES_SHOWN

    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    page = app.render_page().decode()

    assert "function knownGroups(" in page
    assert f"var KNOWN_NAMES_SHOWN = {_KNOWN_NAMES_SHOWN};" in page
    assert "knownGroups(placed)" in page and "knownGroups(kept)" in page
    assert "esc(g.where)" in page and "esc(g.label)" in page and "esc(shown)" in page
    assert "function knownRow(" not in page


def test_global_edit_not_blocked_by_broken_home_directory_rigyaml(tmp_path, fake_agent_tools, monkeypatch):
    """A Global-tab edit's GATE 2 must load $HOME's GLOBAL layer alone (include_repo=False).

    The first review pass's fix anchored the edit at Path.home() but left the plan-gate's
    default include_repo=True -- so if $HOME ITSELF has a rig.yaml (a dotfiles-repo scenario),
    every Global edit still failed for a reason invisible on that tab. This is the deeper
    regression the second review pass caught: distinct from breaking the *server's own home
    repo's* rig.yaml (already covered above) -- this breaks $HOME/rig.yaml itself.
    """
    from riglib.config_web_scopes import GLOBAL_SCOPE_ID

    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    home = repo.parent / "home"
    (home / "rig.yaml").write_text("not: [valid: yaml: at all", encoding="utf-8")
    app = cw.ConfigWebApp(repo_root=repo)

    code, body = app.handle_edit(
        {"key": "gitignore.enabled", "value": "false", "scope": GLOBAL_SCOPE_ID}
    )

    assert code == 200, body
    assert body["ok"] is True


def test_global_scope_rejects_repo_layer_key(tmp_path, fake_agent_tools, monkeypatch):
    """The Global tab must refuse a REPO-layer key, never silently write an unrelated repo file."""
    from riglib.config_web_scopes import GLOBAL_SCOPE_ID

    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    home = repo.parent / "home"
    app = cw.ConfigWebApp(repo_root=repo)

    code, body = app.handle_edit(
        {"key": "harness.auto_mode", "value": "false", "scope": GLOBAL_SCOPE_ID}
    )

    assert code == 400
    assert body["ok"] is False
    assert not (home / "rig.yaml").exists(), "must never write $HOME/rig.yaml from the Global tab"


def test_handle_edit_rejects_non_string_scope(tmp_path, fake_agent_tools, monkeypatch):
    """A present-but-malformed 'scope' (not a string) must 400, not silently default to home."""
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)

    code, body = app.handle_edit({"key": "gitignore.enabled", "value": "false", "scope": {}})

    assert code == 400
    assert body["ok"] is False


def test_plan_preview_refuses_with_no_declared_config(tmp_path, fake_agent_tools, monkeypatch):
    """/api/plan (and /api/apply) must refuse a scope with NO declared config at all -- the SAME
    guard `rig apply info` itself enforces before it will even preview a built-in-defaults plan.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    # a repo with NO rig.yaml at all, and no global config -- loaded.layers is empty
    unconfigured = tmp_path / "unconfigured"
    unconfigured.mkdir()
    app = cw.ConfigWebApp(repo_root=unconfigured)
    scope_id = app.scopes()[0].id

    code, body = app.handle_plan_preview(scope_id)
    assert code == 400
    assert body["ok"] is False

    code, body = app.handle_apply({"scope": scope_id, "fingerprint": "x", "skip_keys": []})
    assert code == 400
    assert body["ok"] is False


def test_current_scope_script_breakout_is_escaped(tmp_path, fake_agent_tools, monkeypatch):
    """A scope id containing '</script>' must not terminate the embedded <script> element."""
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)

    from riglib import config_web_scopes as scopes_mod

    malicious_id = str(repo) + "</script><script>alert(1)</script>"
    fake_scope = scopes_mod.Scope(id=malicious_id, label="evil", repo_root=repo, is_global=False)
    model = cw.build_model(repo)
    page = cw.build_html(model, [fake_scope], fake_scope)

    assert "</script><script>alert(1)</script>" not in page
    assert "\\u003c/script>\\u003cscript>alert(1)\\u003c/script>" in page


def test_current_scope_double_escape_script_breakout_is_escaped(tmp_path, fake_agent_tools, monkeypatch):
    """A '</'-only escape is INSUFFICIENT: '<!--<script>' moves the HTML tokenizer through
    script-data-escaped into script-data-DOUBLE-escaped state, where this template's own literal
    '</script>' no longer closes the element (found in review, a second pass) -- not exploitable
    as XSS (every '</' stays escaped either way), but it silently breaks ALL page JS on render.
    Escaping every '<' (not just '</') must close this path too.
    """
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)

    from riglib import config_web_scopes as scopes_mod

    tricky_id = str(repo) + "<!--<script>oops-marker-xyz"
    fake_scope = scopes_mod.Scope(id=tricky_id, label="tricky", repo_root=repo, is_global=False)
    model = cw.build_model(repo)
    page = cw.build_html(model, [fake_scope], fake_scope)

    # the raw injected sequence (with its distinguishing marker) must never appear un-escaped --
    # checked as one string so a coincidental "<!--<script>" in an unrelated code comment can't
    # produce a false pass/fail
    assert "<!--<script>oops-marker-xyz" not in page
    assert "\\u003c!--\\u003cscript>oops-marker-xyz" in page


def test_tab_href_url_encodes_scope_id_with_ampersand(tmp_path, fake_agent_tools, monkeypatch):
    """A repo path containing '&' must not truncate the tab's <a href> query string.

    html.escape() alone is attribute-safe, not QUERY-safe: an unescaped '&' in the href would
    split the query into two params, silently routing the no-JS/copy-paste/middle-click path to
    the wrong (default) scope while the tab is still labeled with the real name (found in review,
    independently by two reviewers).
    """
    from riglib import config_web_scopes as scopes_mod

    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    tricky_id = str(repo) + "&foo=bar"
    fake_scope = scopes_mod.Scope(id=tricky_id, label="tricky", repo_root=repo, is_global=False)
    model = cw.build_model(repo)
    page = cw.build_html(model, [fake_scope], fake_scope)

    # the raw id must NOT appear un-encoded inside an href="..." attribute
    assert f'href="/?scope={tricky_id}"' not in page
    assert "%26foo%3Dbar" in page  # '&' -> %26, '=' -> %3D


def test_live_csrf_guard_covers_api_plan_and_api_apply(tmp_path, fake_agent_tools, monkeypatch):
    """The shared CSRF guard must actually gate /api/plan and /api/apply through the real socket
    handler, not just the shared is_cross_site_write() unit -- a route-ordering slip in do_POST
    could silently drop the guard for one endpoint with the rest of the suite green.
    """
    repo = _isolated_repo(tmp_path, fake_agent_tools, monkeypatch)
    app = cw.ConfigWebApp(repo_root=repo)
    scope_id = app.scopes()[0].id
    with _live_server(app) as base:
        req = urllib.request.Request(
            f"{base}/api/plan?scope={scope_id}", method="POST", data=b"",
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected HTTPError 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        body = json.dumps({"scope": scope_id, "fingerprint": "x", "skip_keys": []}).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/api/apply", method="POST", data=body,
            headers={"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected HTTPError 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

