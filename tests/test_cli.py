"""CLI dispatch smoke tests — exercise the argparse front-end end to end."""

from __future__ import annotations

from riglib.cli import main


def test_help_runs(capsys):
    import pytest

    # argparse exits with SystemExit(0) on --help; the help text must still print.
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "umbrella driver" in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "rig — the dev-environment" in out


def test_doctor_runs(capsys):
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "rig doctor" in out
    assert rc in (0, 1)  # 1 if optional deps missing on the CI box


def test_setup_dryrun_default(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rc = main(["setup", "-C", str(tmp_path), "--yes", "--dry-run"])
    out = capsys.readouterr().out
    assert "Plan:" in out
    assert rc == 0
    assert not (tmp_path / "rig.yaml").exists()  # dry-run writes nothing


def test_apply_dryrun_with_config(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = tmp_path / "rig.yaml"
    cfg.write_text(
        f"agent_tools_source: {fake_agent_tools}\n"
        "skills: {universal: {all: true}, by_type: {enable: [cli]}}\n"
        "agent_hooks: {enabled: false}\nci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n",
        encoding="utf-8",
    )
    rc = main(["apply", "-C", str(tmp_path), "--config", str(cfg), "--dry-run"])
    out = capsys.readouterr().out
    assert "Plan:" in out
    assert "skills/shell-timeouts" in out
    assert rc == 0


def test_setup_dryrun_never_launches_wizard(tmp_path, capsys, fake_agent_tools, monkeypatch):
    # even though the wizard import would succeed here, --dry-run must stay headless.
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    called = {"wizard": False}

    def _boom(_root):
        called["wizard"] = True
        return 0

    monkeypatch.setattr("riglib.tui.app.run_wizard", _boom, raising=False)
    rc = main(["setup", "-C", str(tmp_path), "--dry-run"])
    assert rc == 0
    assert not called["wizard"]
    assert "Plan:" in capsys.readouterr().out


def test_setup_default_refuses_existing_config(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    existing = "version: 1\n# my customized config\nskills: {enabled: false}\n"
    (repo / "rig.yaml").write_text(existing, encoding="utf-8")
    rc = main(["setup", "-C", str(repo), "--yes"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "already exists" in out
    assert (repo / "rig.yaml").read_text() == existing  # not clobbered


def test_setup_failclosed_leaves_no_config(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """A config that fails catalog validation must not leave a committed rig.yaml behind."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    repo.mkdir()
    template = tmp_path / "template.yaml"
    template.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "ci: {items: {nonexistent_gate: {enabled: true}}}\n",
        encoding="utf-8",
    )
    rc = main(["setup", "-C", str(repo), "--config", str(template), "--yes"])
    assert rc == 2
    assert not (repo / "rig.yaml").exists()  # fail-closed: no invalid config written


def test_setup_external_config_backs_up_existing(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    old = "version: 1\n# customized\nskills: {enabled: false}\nagent_hooks: {enabled: false}\n" \
          "ci: {enabled: false}\nmcp: {enabled: false}\ngit_hooks: {dispatcher: {enabled: false}}\n"
    (repo / "rig.yaml").write_text(old, encoding="utf-8")
    template = tmp_path / "template.yaml"
    template.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\nskills: {{enabled: false}}\n"
        "agent_hooks: {enabled: false}\nci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n",
        encoding="utf-8",
    )
    rc = main(["setup", "-C", str(repo), "--config", str(template), "--yes"])
    assert rc == 0
    # the old config was backed up, not silently discarded
    assert any(p.name.startswith("rig.yaml.rig-bak-") for p in repo.iterdir())


def test_setup_with_external_config_persists_repo_yaml(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    template = tmp_path / "template.yaml"
    template.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {universal: {all: true}, by_type: {enable: [cli]}}\n"
        "agent_hooks: {enabled: false}\nci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n",
        encoding="utf-8",
    )
    rc = main(["setup", "-C", str(repo), "--config", str(template), "--yes"])
    assert rc == 0
    # the external template is now committed into the repo as rig.yaml
    assert (repo / "rig.yaml").is_file()


def test_status_scans_empty_ci_target_for_extras(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "rogue.yml").write_text("name: rogue\n", encoding="utf-8")
    cfg = repo / "rig.yaml"
    # ci enabled but all:false → zero CI actions, yet an undeclared workflow exists
    cfg.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: true, all: false}\n",
        encoding="utf-8",
    )
    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert "rogue" in out  # disk→config extra surfaced despite no CI actions
    assert rc == 3  # drift detected


def test_apply_refuses_without_config(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()  # no rig.yaml, no global config
    rc = main(["apply", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "no rig.yaml" in out
    assert not (tmp_path / "home" / ".agents").exists()  # HOME untouched


def test_apply_relative_config_resolves_under_C(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {universal: {all: true}, by_type: {enable: [cli]}}\n"
        "agent_hooks: {enabled: false}\nci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n",
        encoding="utf-8",
    )
    # relative --config must resolve against -C repo, not the test's cwd
    rc = main(["apply", "-C", str(repo), "--config", "rig.yaml", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Plan:" in out


def test_init_and_setup_share_one_engine_dryrun(tmp_path, capsys, fake_agent_tools, monkeypatch):
    # `rig init` is the canonical onboarding front door; `setup` is its back-compat alias —
    # they dispatch to one engine, so init must build the same plan setup would.
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rc = main(["init", "-C", str(tmp_path), "--yes", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Plan:" in out
    assert not (tmp_path / "rig.yaml").exists()  # dry-run writes nothing


def test_init_default_scaffold_includes_harness_auto_mode(tmp_path, capsys, fake_agent_tools, monkeypatch):
    # the default scaffold `rig init --yes` writes recommends auto-mode ON (the front door
    # provisions autonomy, made safe by the agent-hook guards it also installs).
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["init", "-C", str(repo), "--yes"])
    assert rc == 0
    written = (repo / "rig.yaml").read_text(encoding="utf-8")
    assert "harness:" in written
    assert "auto_mode: true" in written


def test_export_writes_file(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    out_path = tmp_path / "rig.yaml"
    rc = main(["export", "-C", str(tmp_path), "-o", str(out_path)])
    assert rc == 0
    assert out_path.is_file()
    assert "version: 1" in out_path.read_text(encoding="utf-8")
    # refuses to overwrite without --force
    rc2 = main(["export", "-C", str(tmp_path), "-o", str(out_path)])
    assert rc2 == 2
