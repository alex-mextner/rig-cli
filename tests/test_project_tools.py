"""Repo-local project-tool provisioning for Haft, Serena, and Sverklo."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from riglib import project_tools
from riglib.actions.runner import _do_provision_project_tool, run_plan
from riglib.catalog import Catalog, Item
from riglib.config import ConfigError, LoadedConfig, validate
from riglib.drift import detect
from riglib.plan import Action, InstallPlan, build
from riglib.state import default_state


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def _quiet_cfg(project_tools_cfg: dict) -> dict:
    return {
        "version": 1,
        "skills": {"enabled": False},
        "agent_hooks": {"enabled": False},
        "git_hooks": {"dispatcher": {"enabled": False}},
        "ci": {"enabled": False},
        "mcp": {"enabled": False},
        "harness": {"enabled": False, "hook_bridge": {"enabled": False}},
        "permissions": {"enabled": False},
        "models": {"enabled": False},
        "agents_md": {"enabled": False},
        "github": {
            "ruleset": {"enabled": False},
            "merge": {"enabled": False},
            "ghas": {"enabled": False},
            "actions": {"enabled": False},
            "browser": {"enabled": False},
        },
        "tmux": {"enabled": False},
        "gitignore": {"enabled": False},
        "tg_ctl": {"enabled": False},
        "ship_delegator": {"enabled": False},
        "project_tools": project_tools_cfg,
    }


def _loaded(cfg: dict, repo: Path) -> LoadedConfig:
    validate(cfg)
    return LoadedConfig(data=cfg, repo_root=repo)


def _plan(cfg: dict, repo: Path) -> InstallPlan:
    catalog = Catalog(source=repo, items=[Item("dispatcher", "git_hooks", "", "", repo)])
    return build(_loaded(cfg, repo), catalog)


def _project_tool_actions(plan: InstallPlan) -> list[Action]:
    return [a for a in plan.actions if a.kind == "provision_project_tool"]


def test_default_scaffold_includes_project_tools():
    cfg = default_state(project_type="python")
    pt = cfg["project_tools"]
    assert pt["enabled"] is True
    assert pt["haft"]["enabled"] is True
    assert pt["serena"]["enabled"] is True
    assert pt["sverklo"]["register"] is True
    assert pt["sverklo"]["reindex"] is False


def test_project_tools_validation_rejects_bad_values():
    with pytest.raises(ConfigError, match="project_tools.haft.workflow.mode"):
        validate({"version": 1, "project_tools": {"haft": {"workflow": {"mode": "chaos"}}}})
    with pytest.raises(ConfigError, match="project_tools.serena.languages must be a list of strings"):
        validate({"version": 1, "project_tools": {"serena": {"languages": ["python", 3]}}})
    with pytest.raises(ConfigError, match="unknown project_tools.sverklo key"):
        validate({"version": 1, "project_tools": {"sverklo": {"setup_global": True}}})


def test_haft_project_id_is_stable_sha256_prefix():
    assert project_tools._haft_project_id("demo", {}) == "qnt_" + hashlib.sha256(b"demo").hexdigest()[:8]
    assert project_tools._haft_project_id("demo", {"project_id": "qnt_explicit"}) == "qnt_explicit"


def test_build_emits_project_tool_actions(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    plan = _plan(
        _quiet_cfg(
            {
                "enabled": True,
                "haft": {"enabled": True, "project_name": "demo", "project_id": "qnt_demo"},
                "serena": {"enabled": True},
                "sverklo": {"enabled": True, "register": True, "reindex": False},
            }
        ),
        repo,
    )
    items = {a.item for a in _project_tool_actions(plan)}
    assert {"haft-project", "haft-workflow", "haft-codex-mcp", "serena-project", "sverklo-register"} <= items
    assert all(a.category == "project_tools" and a.target == repo for a in _project_tool_actions(plan))


def test_apply_and_drift_for_project_tool_files(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    plan = _plan(
        _quiet_cfg(
            {
                "enabled": True,
                "haft": {"enabled": True, "project_name": "demo", "project_id": "qnt_demo"},
                "serena": {"enabled": True, "languages": ["python"]},
                "sverklo": {"enabled": False},
            }
        ),
        repo,
    )
    missing = [i for i in detect(plan).items if i.category == "project_tools"]
    assert missing and all(i.direction == "missing" for i in missing)

    report = run_plan(plan)
    assert not report.errors
    assert (repo / ".haft/project.yaml").read_text(encoding="utf-8") == "id: qnt_demo\nname: demo\n"
    assert "languages:\n- python\n" in (repo / ".serena/project.yml").read_text(encoding="utf-8")
    assert "[mcp_servers.haft]" in (repo / ".codex/config.toml").read_text(encoding="utf-8")
    assert [i for i in detect(plan).items if i.category == "project_tools"] == []

    report2 = run_plan(plan)
    assert all(r.status == "skipped" for r in report2.results)


@pytest.mark.parametrize(
    ("rel_path", "escapes"),
    [
        ("foo/bar", False),
        (".", False),
        ("foo..bar/baz", False),
        ("foo/.bar", False),
        ("", True),
        ("   ", True),
        (" foo/bar", True),
        ("foo/bar ", True),
        ("/foo", True),
        ("C:\\foo", True),
        ("../foo", True),
        ("../../foo", True),
        ("foo/../bar", True),
    ],
)
def test_path_escapes_repo_blocks_traversal(rel_path, escapes):
    assert project_tools.path_escapes_repo(rel_path) is escapes


def test_serena_language_detection_and_empty_fallback(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert project_tools._serena_languages(repo, {}) == []

    for name in ("pyproject.toml", "package.json", "go.mod", "Cargo.toml", "Package.swift"):
        (repo / name).write_text("", encoding="utf-8")

    assert project_tools._serena_languages(repo, {}) == ["python", "typescript", "go", "rust", "swift"]
    assert project_tools._serena_languages(repo, {"languages": ["go"]}) == ["go"]


def test_serena_project_name_is_yaml_double_quoted(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    text = project_tools._serena_project_yml(
        repo,
        {"project_name": ' true: [x]\n"quoted"\t\\tail'},
    )
    assert 'project_name: " true: [x]\\n\\"quoted\\"\\t\\\\tail"' in text


@pytest.mark.parametrize(
    ("raw", "quoted"),
    [
        ("", ""),
        (" true ", " true "),
        ("?:- [] {}", "?:- [] {}"),
        ('"\\\n\r\t', '\\"\\\\\\n\\r\\t'),
        ("\x01", "\\x01"),
    ],
)
def test_yaml_quote_escapes_double_quoted_scalar_content(raw, quoted):
    assert project_tools._yaml_quote(raw) == quoted


def test_codex_mcp_merge_preserves_unrelated_config(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / ".codex").mkdir()
    (repo / ".codex/config.toml").write_text(
        '[profiles.default]\nmodel = "gpt-5"\n\n[mcp_servers.haft]\ncommand = "old"\n',
        encoding="utf-8",
    )
    entry = next(e for e in project_tools.desired_entries(repo, {"haft": {"enabled": True}, "serena": {"enabled": False}, "sverklo": {"enabled": False}}) if e.item == "haft-codex-mcp")
    action = Action(
        kind="provision_project_tool",
        category="project_tools",
        item=entry.item,
        source=repo,
        target=repo,
        options=entry.to_options(),
    )
    res = _do_provision_project_tool(action, "backup")
    text = (repo / ".codex/config.toml").read_text(encoding="utf-8")
    assert res.status == "backed_up"
    assert '[profiles.default]\nmodel = "gpt-5"' in text
    assert 'command = "old"' not in text
    assert project_tools.merge_codex_mcp_section(text, entry.content) == text


def test_codex_mcp_merge_handles_empty_other_servers_and_unmarked_haft():
    section = project_tools._haft_codex_mcp_section()
    assert project_tools.merge_codex_mcp_section("", section) == section

    existing = (
        '[mcp_servers.other]\ncommand = "other"\n\n'
        '[mcp_servers.haft]\ncommand = "old"\n\n'
        '[mcp_servers.haft.env]\nOLD = "1"\n'
    )
    merged = project_tools.merge_codex_mcp_section(existing, section)
    assert '[mcp_servers.other]\ncommand = "other"' in merged
    assert 'command = "old"' not in merged
    assert 'OLD = "1"' not in merged
    assert merged.count("[mcp_servers.haft]") == 1


def test_sverklo_register_is_dry_run_gated(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv("RIG_PROJECT_TOOLS_DRY_RUN", "1")
    monkeypatch.setattr(project_tools.shutil, "which", lambda name: "/usr/bin/sverklo" if name == "sverklo" else None)

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="Registry: /tmp/registry.json\n", stderr="")

    monkeypatch.setattr(project_tools.subprocess, "run", fake_run)
    action = Action(
        kind="provision_project_tool",
        category="project_tools",
        item="sverklo-register",
        source=repo,
        target=repo,
        options={"tool": "sverklo", "operation": "register"},
    )
    res = _do_provision_project_tool(action, "backup")
    assert res.status == "skipped"
    assert "dry-run would register" in res.detail
    assert calls == [["/usr/bin/sverklo", "list"]]


def test_sverklo_reindex_is_dry_run_gated(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv("RIG_PROJECT_TOOLS_DRY_RUN", "1")
    monkeypatch.setattr(project_tools.shutil, "which", lambda name: "/usr/bin/sverklo" if name == "sverklo" else None)

    calls: list[list[str]] = []
    monkeypatch.setattr(project_tools.subprocess, "run", lambda args, **kwargs: calls.append(list(args)))
    action = Action(
        kind="provision_project_tool",
        category="project_tools",
        item="sverklo-reindex",
        source=repo,
        target=repo,
        options={"tool": "sverklo", "operation": "reindex"},
    )
    res = _do_provision_project_tool(action, "backup")
    assert res.status == "skipped"
    assert "dry-run would reindex" in res.detail
    assert calls == []


def test_sverklo_reindex_is_apply_only_and_has_no_drift(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    plan = _plan(
        _quiet_cfg(
            {
                "enabled": True,
                "haft": {"enabled": False},
                "serena": {"enabled": False},
                "sverklo": {"enabled": True, "register": False, "reindex": True},
            }
        ),
        repo,
    )
    assert [a.item for a in _project_tool_actions(plan)] == ["sverklo-reindex"]
    assert [i for i in detect(plan).items if i.category == "project_tools"] == []


def test_parse_sverklo_path_preserves_separators_inside_paths(tmp_path):
    path_with_dash = tmp_path / "repo - with dash"
    path_with_dash.mkdir()
    path_with_arrow = tmp_path / "repo — with arrow"
    path_with_arrow.mkdir()

    assert project_tools._parse_sverklo_path(str(path_with_dash)) == path_with_dash
    assert project_tools._parse_sverklo_path(str(path_with_arrow)) == path_with_arrow
    assert project_tools._parse_sverklo_path(f"demo — {path_with_dash}") == path_with_dash
    assert project_tools._parse_sverklo_path(f"demo - {path_with_arrow}") == path_with_arrow
    assert project_tools._parse_sverklo_path("Registry: /tmp/sverklo.json") is None
    assert project_tools._parse_sverklo_path("demo without path") is None


def test_sverklo_registered_matches_paths_with_delimiters(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo - with dash")
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(project_tools.shutil, "which", lambda name: "/usr/bin/sverklo" if name == "sverklo" else None)

    def fake_run(args, **kwargs):
        assert args == ["/usr/bin/sverklo", "list"]
        return SimpleNamespace(returncode=0, stdout=f"other — {other}\n{repo}\n", stderr="")

    monkeypatch.setattr(project_tools.subprocess, "run", fake_run)
    registered, detail = project_tools.sverklo_registered(repo)
    assert registered is True
    assert str(repo) in detail


def test_sverklo_missing_cli_skips(monkeypatch, tmp_path):
    """run_sverklo returns 'skipped' (not 'error') when sverklo is not on PATH.

    sverklo is optional — a cleanroom/CI environment without it installed must
    not cause `rig apply` to exit non-zero.  Only ``sverklo_registered`` (the
    drift check) returns a ``False`` / detail pair; the runner skips gracefully.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(project_tools.shutil, "which", lambda name: None)
    registered, detail = project_tools.sverklo_registered(repo)
    assert registered is False
    assert detail == "sverklo CLI not found on PATH"
    status, msg = project_tools.run_sverklo(repo, "register")
    assert status == "skipped"
    assert "sverklo CLI not found on PATH" in msg


def test_sverklo_register_and_reindex_errors_are_reported(monkeypatch, tmp_path):
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.delenv("RIG_PROJECT_TOOLS_DRY_RUN", raising=False)
    monkeypatch.delenv("RIG_SVERKLO_DRY_RUN", raising=False)
    monkeypatch.setattr(project_tools.shutil, "which", lambda name: "/usr/bin/sverklo" if name == "sverklo" else None)

    def failing_register(args, **kwargs):
        if args == ["/usr/bin/sverklo", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=17, stdout="", stderr="register failed")

    monkeypatch.setattr(project_tools.subprocess, "run", failing_register)
    assert project_tools.run_sverklo(repo, "register") == ("error", "register failed")

    def raising_run(args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(project_tools.subprocess, "run", raising_run)
    assert project_tools.run_sverklo(repo, "reindex") == ("error", "sverklo reindex failed: permission denied")


def test_project_tool_drift_reports_missing_sverklo_cli(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setattr(project_tools.shutil, "which", lambda name: None)
    plan = _plan(
        _quiet_cfg(
            {
                "enabled": True,
                "haft": {"enabled": False},
                "serena": {"enabled": False},
                "sverklo": {"enabled": True, "register": True, "reindex": False},
            }
        ),
        repo,
    )
    items = [i for i in detect(plan).items if i.category == "project_tools"]
    assert len(items) == 1
    assert items[0].direction == "missing"
    assert "sverklo CLI not found on PATH" in items[0].detail


def test_project_tool_malformed_action_is_error(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    action = Action(
        kind="provision_project_tool",
        category="project_tools",
        item="bad",
        source=repo,
        target=repo,
        options={"tool": "haft", "operation": "file"},
    )
    res = _do_provision_project_tool(action, "backup")
    assert res.status == "error"
    assert "malformed action" in res.detail


def test_project_tool_malformed_action_reports_drift(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    plan = InstallPlan([
        Action(
            kind="provision_project_tool",
            category="project_tools",
            item="bad",
            source=repo,
            target=repo,
            options={"tool": "haft", "operation": "file"},
        )
    ])
    items = [i for i in detect(plan).items if i.category == "project_tools"]
    assert len(items) == 1
    assert items[0].direction == "modified"
    assert "malformed plan" in items[0].detail


def test_project_tools_is_wired_into_registries():
    from riglib.actions.runner import _HANDLERS
    from riglib.areas import AREAS
    from riglib.config import _VALID_TOP_KEYS
    from riglib.layers import REPO, layer_for_category

    assert "project_tools" in _VALID_TOP_KEYS
    assert "provision_project_tool" in _HANDLERS
    assert any(a.key == "project_tools" and a.layer == REPO for a in AREAS)
    assert layer_for_category("project_tools") == REPO
