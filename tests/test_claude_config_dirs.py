"""rig-cli#368 — claude-code writes fan out to every claude-rotate config dir.

Claude Code reads its user-scope settings from ``$CLAUDE_CONFIG_DIR``; claude-rotate starts
every interactive session with ``CLAUDE_CONFIG_DIR=~/.claude-accounts/account-N``. A rig that
writes hooks/permissions/auto-mode only into ``~/.claude/settings.json`` leaves those sessions
with zero guards while ``rig status`` stays green. These tests pin: discovery, the plan
fan-out (one action per target, per writer), apply + idempotent re-apply, drift that NAMES the
file, and the ``rig doctor`` check. Hermetic: a fake HOME with fake ``account-*`` dirs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from riglib import errors
from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.claude_config_dirs import (
    config_dir_gaps,
    discover_claude_config_dirs,
    doctor_config_dirs,
    fan_out_settings,
    managed_settings_files,
)
from riglib.config import LoadedConfig, load_harness_fan_out
from riglib.drift import detect
from riglib.plan import build

WRITERS = ("apply_harness", "provision_permissions", "register_hook_bridge")


def _fake_home(tmp_path: Path, accounts: tuple[str, ...] = ("account-0", "account-1", "account-2")) -> Path:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude-accounts").mkdir()
    for name in accounts:
        (home / ".claude-accounts" / name).mkdir(parents=True)
    # the launcher keeps two FILES beside the dirs — discovery must skip them
    (home / ".claude-accounts" / "current").write_text("account-0\n")
    (home / ".claude-accounts" / "rotate.log").write_text("")
    return home


def _config(home: Path, repo: Path, fake_agent_tools: Path, **harness_extra) -> LoadedConfig:
    return LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "ship_delegator": {"enabled": False},
            "harness": {
                "kind": "claude-code",
                "auto_mode": True,
                "settings_path": str(home / ".claude" / "settings.json"),
                **harness_extra,
            },
            "permissions": {"kind": "claude-code", "tools": ["git"], "deny": [], "ask": []},
        },
        repo_root=repo,
    )


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = _fake_home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


# ── discovery ────────────────────────────────────────────────────────────────────────────────
def test_discovery_returns_account_dirs_only_natural_sorted(tmp_path):
    home = _fake_home(tmp_path, ("account-10", "account-2", "account-0"))
    (home / ".claude-accounts" / "account-notadir").write_text("")
    found = discover_claude_config_dirs(home)
    assert [d.name for d in found] == ["account-0", "account-2", "account-10"]


def test_discovery_survives_non_ascii_digit_suffix(tmp_path):
    # "²".isdigit() is True but int("²") raises — such a dir must sort as non-numeric, not crash
    home = _fake_home(tmp_path, ("account-1", "account-²", "account-0"))
    assert [d.name for d in discover_claude_config_dirs(home)] == ["account-0", "account-1", "account-²"]


def test_discovery_without_accounts_dir_is_empty(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    assert discover_claude_config_dirs(home) == []


def test_fan_out_only_for_user_scope_primary(tmp_path):
    home = _fake_home(tmp_path, ("account-0", "account-1"))
    primary = home / ".claude" / "settings.json"
    targets = fan_out_settings(primary, {}, home)
    assert [t.path for t in targets] == [
        primary,
        home / ".claude-accounts" / "account-0" / "settings.json",
        home / ".claude-accounts" / "account-1" / "settings.json",
    ]
    assert [t.label for t in targets] == [None, "account-0", "account-1"]
    # a repo-local (project-scope) settings file is read regardless of CLAUDE_CONFIG_DIR — no fan-out
    project = tmp_path / "repo" / ".claude" / "settings.json"
    assert [t.path for t in fan_out_settings(project, {}, home)] == [project]


def test_fan_out_explicit_paths_and_opt_out(tmp_path):
    home = _fake_home(tmp_path, ("account-0",))
    primary = home / ".claude" / "settings.json"
    extra = tmp_path / "other-dir" / "settings.json"
    harness = {"settings_paths": [str(extra), str(primary)], "discover_config_dirs": False}
    targets = fan_out_settings(primary, harness, home)
    assert [t.path for t in targets] == [primary, extra]  # primary deduped, discovery off
    assert targets[1].label == "other-dir"


# ── plan fan-out ─────────────────────────────────────────────────────────────────────────────
def _actions_by_kind(plan, kind):
    return [a for a in plan.actions if a.kind == kind]


def test_plan_emits_one_action_per_config_dir_per_writer(home, repo, fake_agent_tools):
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_config(home, repo, fake_agent_tools), cat, project_type="unknown")
    expected = {
        home / ".claude" / "settings.json",
        home / ".claude-accounts" / "account-0" / "settings.json",
        home / ".claude-accounts" / "account-1" / "settings.json",
        home / ".claude-accounts" / "account-2" / "settings.json",
    }
    for kind in WRITERS:
        acts = _actions_by_kind(plan, kind)
        assert {a.target for a in acts} == expected, kind
        assert all(a.options.get("kind") == "claude-code" for a in acts), kind
        labelled = sorted(a.item for a in acts if "@" in a.item)
        base = "hook-bridge" if kind == "register_hook_bridge" else "claude-code"
        assert labelled == [f"{base}@account-0", f"{base}@account-1", f"{base}@account-2"], kind
        assert base in {a.item for a in acts}  # the primary keeps the bare item


def test_plan_expands_relative_and_tilde_settings_paths(home, repo, fake_agent_tools):
    # explicit extras resolve like settings_path: `~` → HOME, relative → the repo root
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _config(
        home, repo, fake_agent_tools,
        settings_paths=["~/extra-dir/settings.json", "local-dir/settings.json"],
        discover_config_dirs=False,
    )
    plan = build(cfg, cat, project_type="unknown")
    targets = [a.target for a in _actions_by_kind(plan, "apply_harness")]
    assert targets == [
        home / ".claude" / "settings.json",
        home / "extra-dir" / "settings.json",
        repo / "local-dir" / "settings.json",
    ]
    assert all(t.is_absolute() for t in targets)


def test_plan_fan_out_can_be_disabled(home, repo, fake_agent_tools):
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_config(home, repo, fake_agent_tools, discover_config_dirs=False), cat, project_type="unknown")
    for kind in WRITERS:
        assert [a.target for a in _actions_by_kind(plan, kind)] == [home / ".claude" / "settings.json"], kind


# ── apply + idempotency ──────────────────────────────────────────────────────────────────────
def _managed(settings: Path) -> dict:
    data = json.loads(settings.read_text())
    bridge = sum(
        1
        for blocks in data.get("hooks", {}).values()
        for block in blocks
        for h in block.get("hooks", [])
        if "cc_hook_bridge" in h.get("command", "")
    )
    return {
        "mode": data.get("permissions", {}).get("defaultMode"),
        "allow": data.get("permissions", {}).get("allow", []),
        "bridge_hooks": bridge,
    }


def test_apply_writes_every_config_dir_and_reapply_is_noop(home, repo, fake_agent_tools):
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_config(home, repo, fake_agent_tools), cat, project_type="unknown")
    # account-1 has a pre-existing settings.json with unrelated keys — they must survive
    acct1 = home / ".claude-accounts" / "account-1" / "settings.json"
    acct1.write_text(json.dumps({"theme": "dark", "model": "opus"}))

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    for name in ("account-0", "account-1", "account-2"):
        state = _managed(home / ".claude-accounts" / name / "settings.json")
        assert state["mode"] == "auto", name
        assert state["bridge_hooks"] > 0, name
        assert any(e.startswith("Bash(git") for e in state["allow"]), name
    assert _managed(home / ".claude" / "settings.json")["mode"] == "auto"
    kept = json.loads(acct1.read_text())
    assert kept["theme"] == "dark" and kept["model"] == "opus"

    again = run_plan(build(_config(home, repo, fake_agent_tools), cat, project_type="unknown"))
    assert not again.errors
    changed = [r for r in again.results if r.action.kind in WRITERS and r.status != "skipped"]
    assert changed == [], [(r.action.item, r.status, r.detail) for r in changed]


# ── drift names the file ─────────────────────────────────────────────────────────────────────
def test_drift_names_the_config_dir_missing_the_managed_keys(home, repo, fake_agent_tools):
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_config(home, repo, fake_agent_tools), cat, project_type="unknown")
    assert not run_plan(plan).errors
    assert detect(plan).in_sync

    acct2 = home / ".claude-accounts" / "account-2" / "settings.json"
    data = json.loads(acct2.read_text())
    del data["hooks"]
    del data["permissions"]["defaultMode"]
    acct2.write_text(json.dumps(data))

    report = detect(plan)
    drifted = [i for i in report.items if i.direction in ("missing", "modified")]
    assert drifted, "drift must surface"
    assert {i.target for i in drifted} == {acct2}
    assert {i.item for i in drifted} >= {"claude-code@account-2", "hook-bridge@account-2"}


# ── rig doctor ───────────────────────────────────────────────────────────────────────────────
def _write_settings(path: Path, *, bridge: bool, mode: str | None) -> None:
    data: dict = {"permissions": {"allow": ["Bash(git:*)"]}}
    if mode:
        data["permissions"]["defaultMode"] = mode
    if bridge:
        data["hooks"] = {
            "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "python3 -m cc_hook_bridge Stop"}]}]
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_doctor_inventories_env_and_account_dirs_and_flags_gaps(tmp_path):
    home = _fake_home(tmp_path, ("account-0", "account-1"))
    _write_settings(home / ".claude" / "settings.json", bridge=True, mode="auto")
    _write_settings(home / ".claude-accounts" / "account-0" / "settings.json", bridge=True, mode="auto")
    # account-1 exists but has no managed keys — this is the rig-cli#368 shape
    _write_settings(home / ".claude-accounts" / "account-1" / "settings.json", bridge=False, mode=None)
    env_dir = tmp_path / "env-config"
    env_dir.mkdir()

    rows = doctor_config_dirs(home, {"CLAUDE_CONFIG_DIR": str(env_dir)})
    assert [(r.role, r.config_dir.name) for r in rows] == [
        ("default", ".claude"), ("env", "env-config"), ("account", "account-0"), ("account", "account-1"),
    ]
    assert rows[0].bridge_hooks == 1 and rows[0].default_mode == "auto"
    assert rows[1].exists is False

    gaps = config_dir_gaps(rows)
    by_dir = {g.row.config_dir.name: g for g in gaps}
    assert set(by_dir) == {"env-config", "account-1"}
    assert "0 rig hook-bridge hooks" in by_dir["account-1"].what
    # the fix must CONVERGE that dir: apply reaches account dirs, but never an ad-hoc env dir
    assert "rig apply commit" in by_dir["account-1"].fix
    assert "harness.settings_paths" in by_dir["env-config"].fix
    assert str(env_dir / "settings.json") in by_dir["env-config"].fix


def test_doctor_env_dir_equal_to_default_is_not_duplicated(tmp_path):
    home = _fake_home(tmp_path, ())
    rows = doctor_config_dirs(home, {"CLAUDE_CONFIG_DIR": str(home / ".claude")})
    assert [r.role for r in rows] == ["default"]


def test_doctor_honors_discover_opt_out_and_settings_paths(tmp_path):
    # discover_config_dirs: false → account dirs are listed as UNMANAGED (no apply-commit advice);
    # settings_paths entries are managed targets, same as the plan
    home = _fake_home(tmp_path, ("account-0",))
    _write_settings(home / ".claude" / "settings.json", bridge=True, mode="auto")
    _write_settings(home / ".claude-accounts" / "account-0" / "settings.json", bridge=False, mode=None)
    extra = tmp_path / "extra-dir" / "settings.json"
    _write_settings(extra, bridge=False, mode=None)
    harness = {"discover_config_dirs": False, "settings_paths": [str(extra)]}

    rows = doctor_config_dirs(home, {}, harness)
    assert [(r.role, r.config_dir.name) for r in rows] == [
        ("default", ".claude"), ("configured", "extra-dir"), ("unmanaged-account", "account-0"),
    ]
    gaps = {g.row.role: g for g in config_dir_gaps(rows)}
    assert "rig apply commit" in gaps["configured"].fix
    assert "discover_config_dirs: true" in gaps["unmanaged-account"].fix


def test_doctor_manual_default_mode_without_bridge_is_not_a_gap(tmp_path):
    # a hand-set defaultMode/allowlist in ~/.claude is NOT proof rig manages this machine —
    # only a cc_hook_bridge hook is; without one, an untouched account dir is no gap
    home = _fake_home(tmp_path, ("account-0",))
    _write_settings(home / ".claude" / "settings.json", bridge=False, mode="auto")
    _write_settings(home / ".claude-accounts" / "account-0" / "settings.json", bridge=False, mode=None)
    assert config_dir_gaps(doctor_config_dirs(home, {})) == []


def test_doctor_malformed_settings_is_always_a_gap(tmp_path):
    home = _fake_home(tmp_path, ("account-0",))
    _write_settings(home / ".claude" / "settings.json", bridge=False, mode=None)
    (home / ".claude-accounts" / "account-0" / "settings.json").write_text("{not json")
    gaps = config_dir_gaps(doctor_config_dirs(home, {}))
    assert len(gaps) == 1 and "malformed" in gaps[0].what


def test_managed_settings_files_ignores_the_shell_env(tmp_path, monkeypatch):
    # the config-less drift default is filesystem-only: CLAUDE_CONFIG_DIR never changes it
    home = _fake_home(tmp_path, ("account-0",))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert managed_settings_files(home) == [
        home / ".claude" / "settings.json",
        home / ".claude-accounts" / "account-0" / "settings.json",
    ]


def test_cmd_doctor_exits_drift_when_a_config_dir_lacks_managed_hooks(tmp_path, monkeypatch, capsys):
    home = _fake_home(tmp_path, ("account-0",))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _write_settings(home / ".claude" / "settings.json", bridge=True, mode="auto")
    _write_settings(home / ".claude-accounts" / "account-0" / "settings.json", bridge=False, mode=None)
    from riglib import doctor as doctor_module
    from riglib.cli import main

    monkeypatch.setattr(doctor_module, "DEPENDENCIES", [])
    monkeypatch.chdir(tmp_path)
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == errors.EXIT_DRIFT, out
    assert "account-0/settings.json" in out
    assert "CLAUDE_CONFIG_DIR" in out
    assert "rig apply commit" in out


def test_cmd_doctor_honors_opt_out_from_a_config_that_is_otherwise_broken(tmp_path, monkeypatch, capsys):
    # the global config carries an unknown key elsewhere (the rig-cli#369 shape) — doctor still
    # reads the fan-out keys leniently, so discover_config_dirs: false turns the gap advice
    # into "enable discovery", and exits 0 (an opted-out dir is not managed drift)
    home = _fake_home(tmp_path, ("account-0",))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _write_settings(home / ".claude" / "settings.json", bridge=True, mode="auto")
    _write_settings(home / ".claude-accounts" / "account-0" / "settings.json", bridge=False, mode=None)
    gcfg = home / ".config" / "rig" / "config.yaml"
    gcfg.parent.mkdir(parents=True)
    gcfg.write_text("harness:\n  discover_config_dirs: false\nskills:\n  bogus_key_from_the_future: 1\n")
    from riglib import doctor as doctor_module
    from riglib.cli import main

    monkeypatch.setattr(doctor_module, "DEPENDENCIES", [])
    monkeypatch.chdir(tmp_path)
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "discover_config_dirs: false" in out, out
    assert "discover_config_dirs: true" in out  # the fix line
    assert rc == errors.EXIT_DRIFT, out  # still loud: that session runs no guard


# ── lenient fan-out loader + validation ──────────────────────────────────────────────────────
def test_load_harness_fan_out_is_lenient(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    assert load_harness_fan_out(repo) == {}  # no files at all
    gcfg = home / ".config" / "rig" / "config.yaml"
    gcfg.parent.mkdir(parents=True)
    gcfg.write_text("harness:\n  settings_paths: ['~/a/settings.json']\nunknown_top_level: true\n")
    (repo / "rig.yaml").write_text("harness:\n  discover_config_dirs: false\n")
    assert load_harness_fan_out(repo) == {"settings_paths": ["~/a/settings.json"], "discover_config_dirs": False}
    gcfg.write_text("harness:\n  settings_paths: ['not-json.txt']\n")
    assert load_harness_fan_out(repo) == {}  # invalid fan-out keys → defaults, never a crash
    gcfg.write_text(": : not yaml [")
    assert load_harness_fan_out(repo) == {}


@pytest.mark.parametrize(
    "harness_extra, key",
    [
        ({"settings_paths": ["~/x/settings.txt"]}, "settings_paths"),
        ({"settings_paths": [""]}, "settings_paths"),
        ({"settings_paths": "~/x/settings.json"}, "settings_paths"),
        ({"discover_config_dirs": "false"}, "discover_config_dirs"),
    ],
)
def test_validation_fails_closed_on_bad_fan_out_keys(tmp_path, harness_extra, key):
    from riglib.config import ConfigError, validate

    data = {"harness": {"kind": "claude-code", **harness_extra}}
    with pytest.raises(ConfigError) as exc:
        validate(data)
    assert key in str(exc.value)


def test_fan_out_labels_are_unique_on_collision(tmp_path):
    home = _fake_home(tmp_path, ("account-2",))
    primary = home / ".claude" / "settings.json"
    a = tmp_path / "one" / "shared" / "settings.json"
    b = tmp_path / "two" / "shared" / "settings.json"
    c = tmp_path / "three" / "account-2" / "settings.json"  # collides with the discovered dir
    targets = fan_out_settings(primary, {"settings_paths": [str(a), str(b), str(c)]}, home)
    labels = [t.label for t in targets]
    assert len(labels) == len(set(labels)), labels
    assert labels[1] == "shared" and labels[2] == str(b)
    assert labels[3] == "account-2" and labels[4] == str(home / ".claude-accounts" / "account-2" / "settings.json")


# ── PR review threads (rig-cli#374) ──────────────────────────────────────────────────────────
def test_fan_out_keys_readable_without_pyyaml():
    # `rig doctor` is documented to run with zero third-party imports — the two fan-out keys
    # must come out of a config file even when PyYAML is absent (flow list, block list, bool)
    from riglib.config import _fan_out_keys_without_yaml

    flow = "skills:\n  enabled: true\nharness:\n  kind: claude-code\n  settings_paths: ['~/a/settings.json', \"~/b/settings.json\"]  # x\n  discover_config_dirs: false\nmcp:\n  enabled: false\n"
    assert _fan_out_keys_without_yaml(flow) == {
        "settings_paths": ["~/a/settings.json", "~/b/settings.json"],
        "discover_config_dirs": False,
    }
    block = "harness:\n  settings_paths:\n    - ~/a/settings.json\n    - '~/b/settings.json'\n  hook_bridge:\n    enabled: true\n  discover_config_dirs: true\n"
    assert _fan_out_keys_without_yaml(block) == {
        "settings_paths": ["~/a/settings.json", "~/b/settings.json"],
        "discover_config_dirs": True,
    }
    assert _fan_out_keys_without_yaml("skills:\n  known: [x]\n") == {}


def test_load_harness_fan_out_falls_back_when_pyyaml_is_missing(tmp_path, monkeypatch):
    from riglib import config as config_module

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    gcfg = home / ".config" / "rig" / "config.yaml"
    gcfg.parent.mkdir(parents=True)
    gcfg.write_text("harness:\n  discover_config_dirs: false\n  settings_paths: [~/x/settings.json]\n")
    repo = tmp_path / "repo"
    repo.mkdir()

    def no_yaml(path):
        raise ImportError("No module named 'yaml'")

    monkeypatch.setattr(config_module, "read_yaml_file", no_yaml)
    assert load_harness_fan_out(repo) == {"discover_config_dirs": False, "settings_paths": ["~/x/settings.json"]}


def test_cmd_doctor_reads_the_repo_layer_from_a_subdirectory(tmp_path, monkeypatch, capsys):
    import subprocess

    home = _fake_home(tmp_path, ("account-0",))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _write_settings(home / ".claude" / "settings.json", bridge=True, mode="auto")
    _write_settings(home / ".claude-accounts" / "account-0" / "settings.json", bridge=False, mode=None)
    repo = tmp_path / "repo"
    (repo / "sub" / "dir").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "rig.yaml").write_text("harness:\n  discover_config_dirs: false\n")
    from riglib import doctor as doctor_module
    from riglib.cli import main

    monkeypatch.setattr(doctor_module, "DEPENDENCIES", [])
    monkeypatch.chdir(repo / "sub" / "dir")
    main(["doctor"])
    out = capsys.readouterr().out
    assert "discover_config_dirs: false" in out, out  # the repo-root rig.yaml opt-out was honoured


def test_doctor_malformed_default_settings_is_a_gap_even_alone(tmp_path, monkeypatch, capsys):
    home = _fake_home(tmp_path, ())
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (home / ".claude" / "settings.json").write_text("{not json")
    rows = doctor_config_dirs(home, {})
    gaps = config_dir_gaps(rows)
    assert len(gaps) == 1 and gaps[0].row.role == "default" and "malformed" in gaps[0].what
    from riglib import doctor as doctor_module
    from riglib.cli import main

    monkeypatch.setattr(doctor_module, "DEPENDENCIES", [])
    monkeypatch.chdir(tmp_path)
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == errors.EXIT_DRIFT, out
    assert "malformed" in out and "repair the JSON" in out
