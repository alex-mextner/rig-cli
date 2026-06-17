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


def test_doctor_runs(tmp_path, capsys, monkeypatch):
    from riglib import errors

    # isolate HOME so `_scan_missing_targets()` can't read the dev machine's real
    # ~/.claude/settings.json (a dead hook there would flip the exit code to MISSING_TARGET).
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "rig doctor" in out
    # 0 = all present; 1 = optional deps missing on the CI box; 127 = a REQUIRED dep absent
    # (the documented missing-dependency class — honored by the exit-code contract).
    assert rc in (0, 1, errors.EXIT_MISSING_DEP)


def test_doctor_missing_required_dep_uses_127_contract(tmp_path, capsys, monkeypatch):
    """A missing REQUIRED dep exits with the documented missing-dependency class (127).

    The --help epilog promises `127  missing dependency`; doctor must honor that (the public
    exit-code contract scripts branch on) instead of the generic non-zero. A pure --yes-less run
    so nothing is installed; only required deps are forced absent.
    """
    from riglib import doctor, errors

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # force every dependency probe to "absent" so missing_required is non-empty.
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    rc = main(["doctor"])  # no --yes → reports + advises, installs nothing
    out = capsys.readouterr().out
    assert "missing dependencies above" in out
    assert rc == errors.EXIT_MISSING_DEP


def test_setup_dryrun_default(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rc = main(["init", "-C", str(tmp_path), "--yes", "--dry-run"])
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


def test_apply_rejects_malformed_config_three_part(tmp_path, capsys, fake_agent_tools, monkeypatch):
    # roadmap §5 "it should work" = a malformed config fails LOUDLY on apply: exit 2, the 3-part
    # block, and the schema path of the offending key — not a silent partial reconcile.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = tmp_path / "rig.yaml"
    cfg.write_text(
        f"agent_tools_source: {fake_agent_tools}\nharness: {{aut_mode: true}}\n",  # typo key
        encoding="utf-8",
    )
    rc = main(["apply", "-C", str(tmp_path), "--config", str(cfg), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "unknown harness key: aut_mode" in out
    assert "schema path: harness.aut_mode" in out
    assert "fix:" in out


def test_schema_command_prints_valid_json(capsys):
    rc = main(["schema"])
    assert rc == 0
    import json

    doc = json.loads(capsys.readouterr().out)
    assert doc["$schema"].startswith("http://json-schema.org/draft-07")
    assert doc["additionalProperties"] is False
    assert "harness" in doc["properties"]


def test_schema_command_check_passes_for_committed_file(capsys):
    # the committed schema file must be in sync — `rig schema --check` is a CI-usable gate.
    rc = main(["schema", "--check"])
    assert rc == 0
    assert "in sync" in capsys.readouterr().out


def test_schema_check_fails_when_file_missing(tmp_path, capsys, monkeypatch):
    # point the resolver at a non-existent file → --check must exit 2 and say how to fix it.
    target = tmp_path / "schema" / "rig.schema.json"
    monkeypatch.setattr("riglib.config_schema.schema_file_path", lambda: target)
    rc = main(["schema", "--check"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "missing or out of sync" in out
    assert "rig schema --write" in out


def test_schema_check_fails_when_file_stale(tmp_path, capsys, monkeypatch):
    # a hand-edited / stale file (wrong bytes) is drift → --check exits 2.
    target = tmp_path / "schema" / "rig.schema.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"stale": true}\n', encoding="utf-8")
    monkeypatch.setattr("riglib.config_schema.schema_file_path", lambda: target)
    rc = main(["schema", "--check"])
    assert rc == 2
    assert "out of sync" in capsys.readouterr().out


def test_schema_write_regenerates_file(tmp_path, capsys, monkeypatch):
    # --write (re)generates the file from the registry; a follow-up --check then passes.
    from riglib import config_schema

    target = tmp_path / "schema" / "rig.schema.json"
    monkeypatch.setattr("riglib.config_schema.schema_file_path", lambda: target)
    assert not target.exists()
    rc = main(["schema", "--write"])
    assert rc == 0
    assert "wrote" in capsys.readouterr().out
    assert target.read_text(encoding="utf-8") == config_schema.render_schema_json()
    assert main(["schema", "--check"]) == 0


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
    rc = main(["init", "-C", str(tmp_path), "--dry-run"])
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
    rc = main(["init", "-C", str(repo), "--yes"])
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
    rc = main(["init", "-C", str(repo), "--config", str(template), "--yes"])
    # error-system v2: an unknown catalog item exits with the unknown-item class (4), not the
    # generic config class (2). The fail-closed guarantee is unchanged: no rig.yaml is written.
    from riglib import errors

    assert rc == errors.EXIT_UNKNOWN_ITEM
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
    rc = main(["init", "-C", str(repo), "--config", str(template), "--yes"])
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
    rc = main(["init", "-C", str(repo), "--config", str(template), "--yes"])
    assert rc == 0
    # the external template is now committed into the repo as rig.yaml
    assert (repo / "rig.yaml").is_file()


def test_status_scans_empty_ci_target_for_extras(tmp_path, capsys, fake_agent_tools, monkeypatch):
    import subprocess

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    repo.mkdir()
    # CI is the REPO layer — its extras are only scanned inside a real git repo (error-system
    # v2: a non-git dir has no repo layer), so this must be an actual repository.
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
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


def test_init_dryrun_writes_nothing(tmp_path, capsys, fake_agent_tools, monkeypatch):
    # `rig init` is the canonical onboarding front door; --dry-run prints the plan, writes nothing.
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
