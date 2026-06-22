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


def _corrupt_repo(tmp_path):
    """Build a throwaway non-bare git repo and corrupt its core.bare → true."""
    import subprocess

    repo = tmp_path / "broken"
    repo.mkdir()
    for args in (["init", "-q", "."], ["config", "core.bare", "true"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return repo


def test_doctor_flags_core_bare_corruption(tmp_path, capsys, monkeypatch):
    """A corrupted cwd checkout (core.bare=true on a work tree) exits with the repo-corrupt class.

    Doctor's dependency picture is irrelevant — repo corruption is the top-precedence failure, so
    it must win the exit code even if every dep is present.
    """
    from riglib import errors

    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # isolate ~/.claude scan
    repo = _corrupt_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "core.bare=true" in out
    assert "git is broken there" in out
    assert rc == errors.EXIT_REPO_CORRUPT


def test_doctor_fix_repairs_core_bare(tmp_path, capsys, monkeypatch):
    """`rig doctor --fix` repairs the corruption and then exits 0 (clean).

    Deps are mocked present so the exit code reflects the repair alone — otherwise a missing
    required dep on the host would route to the 127 branch and the assertion would pass vacuously.
    """
    import subprocess

    from riglib import doctor

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor, "_python_present", lambda name: True)
    repo = _corrupt_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = main(["doctor", "--fix"])
    out = capsys.readouterr().out
    assert "fixed:" in out
    val = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "core.bare"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert val == "false"
    assert rc == 0  # corruption repaired AND deps present → fully clean


def test_doctor_repo_corrupt_outranks_missing_required_dep(tmp_path, capsys, monkeypatch):
    """The CORE precedence claim: repo corruption (exit 7) beats a missing required dep (127).

    Forces a missing required dep AND a corrupted cwd checkout — the exit code must be 7, not the
    127 missing-dependency class, because a broken .git makes every other check unreliable.
    """
    from riglib import doctor, errors

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)  # every binary "absent"
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    repo = _corrupt_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = main(["doctor"])  # no --yes → would otherwise exit 127 for the missing required deps
    out = capsys.readouterr().out
    assert "core.bare=true" in out
    assert rc == errors.EXIT_REPO_CORRUPT  # 7 wins over 127


def _git_hookless(repo, *args):
    import subprocess

    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
         "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


def test_doctor_fix_never_touches_legit_bare_repo_worktree(tmp_path, capsys, monkeypatch):
    """End-to-end destructive-trap guard: `rig doctor --fix` run from a worktree of a GENUINE bare
    repo must NOT flag it and must NOT rewrite the bare repo's shared core.bare (which would break
    that legitimate setup)."""
    from riglib import errors

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git_hookless(seed, "init", "-q", "-b", "main", ".")
    (seed / "f").write_text("x", encoding="utf-8")
    _git_hookless(seed, "add", "f")
    _git_hookless(seed, "commit", "-qm", "i")
    bare = tmp_path / "repo.git"
    _git_hookless(tmp_path, "init", "-q", "-b", "main", "--bare", str(bare))
    _git_hookless(seed, "push", "-q", str(bare), "main")
    wt = tmp_path / "bare-wt"
    _git_hookless(bare, "worktree", "add", "-q", str(wt), "main")

    monkeypatch.chdir(wt)
    rc = main(["doctor", "--fix"])
    out = capsys.readouterr().out
    assert "corrupted git config" not in out  # not flagged
    assert "fixed:" not in out  # nothing repaired
    # the genuine bare repo's core.bare is untouched (still true → still a valid bare repo)
    assert _git_hookless(bare, "config", "--get", "core.bare").stdout.strip() == "true"
    assert rc != errors.EXIT_REPO_CORRUPT


def test_doctor_fix_then_missing_dep_releases_precedence_to_127(tmp_path, capsys, monkeypatch):
    """After --fix REPAIRS the corruption, the exit code must reflect the NEXT problem (a missing
    required dep → 127), not stay at 7 — the repo-corrupt precedence is released once it's fixed."""
    from riglib import doctor, errors

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)  # required deps absent
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    repo = _corrupt_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = main(["doctor", "--fix"])  # repairs core.bare, then surfaces the missing deps
    out = capsys.readouterr().out
    assert "fixed:" in out
    assert rc == errors.EXIT_MISSING_DEP  # 127, not 7 — corruption gone, deps now govern


def test_doctor_repo_corrupt_outranks_missing_target(tmp_path, capsys, monkeypatch):
    """Precedence: repo corruption (exit 7) also beats a missing-target (exit 5).

    Plants a dead hook reference in the harness settings.json AND a corrupted cwd checkout; the
    exit code must be 7, matching the help epilog's "doctor exits 7 ahead of any other class".
    """
    import json

    from riglib import errors

    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    # a hook command pointing at a script that does not exist → a missing-target finding
    settings = {
        "hooks": {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": str(tmp_path / "gone" / "hook.py")}]}
            ]
        }
    }
    (claude / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    repo = _corrupt_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "missing targets" in out  # the dead target IS reported …
    assert "core.bare=true" in out  # … alongside the corruption …
    assert rc == errors.EXIT_REPO_CORRUPT  # … but repo-corrupt (7) wins the exit code over 5


def test_doctor_fix_failure_advises_manual_not_rerun(tmp_path, capsys, monkeypatch):
    """When --fix is given but the repair FAILS, advise the manual fix, not a useless `--fix` re-run."""
    from riglib import core_bare, errors

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(core_bare, "fix_core_bare", lambda finding: False)  # simulate a write failure
    repo = _corrupt_repo(tmp_path)
    monkeypatch.chdir(repo)
    rc = main(["doctor", "--fix"])
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "re-run `rig doctor --fix`" not in out  # the misleading advice must NOT appear
    assert rc == errors.EXIT_REPO_CORRUPT


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


# ── version: pyproject is the single source of truth, no drift (rig-cli#70) ───────────


def _pyproject_project_version() -> str:
    """Read `[project] version` straight from the repo's pyproject.toml — an INDEPENDENT
    parse (not via `riglib._version`) so the drift guard also catches a resolver bug."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    body = text.split("[project]", 1)[1].split("\n[", 1)[0]
    match = re.search(r"""^version\s*=\s*['"]([^'"]+)['"]""", body, re.MULTILINE)
    assert match is not None, "pyproject [project] version not found"
    return match.group(1)


def _force_checkout_resolution(monkeypatch) -> None:
    """Force the live-checkout code path: make `importlib.metadata` report rig-cli absent.

    rig actually runs from its repo via the `bin/rig` sys.path shim (no installed dist
    metadata), so the pyproject fallback is the production path. CI, however, often has
    rig-cli pip/uv-installed, where `_dist_version` would short-circuit on possibly-stale
    editable metadata — making the drift guard pass/fail for an install-state reason rather
    than real drift. Forcing PackageNotFoundError pins the test to the path rig truly uses.
    """
    from importlib.metadata import PackageNotFoundError

    from riglib import _version

    def _raise(_name):
        raise PackageNotFoundError

    monkeypatch.setattr(_version, "_dist_version", _raise)


def _version_output(capsys, monkeypatch) -> str:
    """Run `rig --version` and return the printed version token (argparse exits 0).

    `riglib.__version__` is resolved once at package import — before any test monkeypatch —
    so re-resolve it now against the (possibly patched) `_version` and patch the value the
    CLI reads, so `--version` reflects the path under test.
    """
    import pytest

    import riglib
    from riglib import _version as _vmod

    monkeypatch.setattr(riglib, "__version__", _vmod.resolve_version())
    # cli imported `__version__` by value (`from . import __version__`); patch its binding.
    monkeypatch.setattr("riglib.cli.__version__", _vmod.resolve_version())

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out.strip()
    # argparse prints `rig <version>`.
    assert out.startswith("rig "), out
    return out.split(" ", 1)[1].strip()


def test_version_prints(capsys, monkeypatch):
    printed = _version_output(capsys, monkeypatch)
    assert printed  # non-empty


def test_version_matches_pyproject_no_drift(capsys, monkeypatch):
    """THE durable guard: `rig --version` must equal pyproject `[project] version`.

    Pinned to the live-checkout path (PackageNotFoundError forced) so it measures REAL
    drift, not local install state. A hardcoded `__version__` literal that nobody bumps
    (the rig-cli#70 bug) cannot drift from pyproject if this passes; reintroduce a literal,
    or bump pyproject without the dynamic read flowing through, and this fails.
    """
    _force_checkout_resolution(monkeypatch)
    assert _version_output(capsys, monkeypatch) == _pyproject_project_version()


def test_version_is_not_the_stale_literal(capsys, monkeypatch):
    """The old permanently-stale `0.1.0` literal is gone — the bump landed."""
    _force_checkout_resolution(monkeypatch)
    assert _version_output(capsys, monkeypatch) != "0.1.0"


def test_resolve_version_falls_back_to_pyproject_in_checkout(monkeypatch):
    """In a live checkout (no installed dist metadata), `resolve_version()` parses pyproject.

    rig runs from its repo via the `bin/rig` sys.path shim, so `importlib.metadata` raises
    PackageNotFoundError; the pyproject fallback must produce the real version, not the
    `0.0.0+unknown` sentinel.
    """
    from riglib import _version

    _force_checkout_resolution(monkeypatch)
    assert _version.resolve_version() == _pyproject_project_version()
    assert _version.resolve_version() != _version._UNKNOWN
    # The independent parser also matches (covers a resolver-vs-parser disagreement).
    assert _version._version_from_pyproject() == _pyproject_project_version()


def test_parse_project_version_scoped_and_array_robust():
    """The pyproject `version` parser is scoped to `[project]` and survives `[`-arrays.

    Two ways a naive parse breaks: (a) a `version` key in another table is read by mistake;
    (b) a multi-line array whose continuation line starts with `[` prematurely ends the
    `[project]` table. Both must resolve to the real `[project] version`.
    """
    from riglib import _version

    toml = (
        "[build-system]\n"
        'version = "99.0.0"\n'  # decoy: not the project version
        "\n"
        "[project]\n"
        'name = "rig-cli"\n'
        "classifiers = [\n"
        '    ["a", "b"],\n'  # continuation line starting with `[` — must NOT end the table
        "]\n"
        'version = "0.2.0"\n'
        "\n"
        "[tool.x]\n"
        'version = "1.2.3"\n'  # decoy after the table
    )
    assert _version._parse_project_version(toml) == "0.2.0"
    # No `[project]` table at all → None (resolver then falls to the sentinel).
    assert _version._parse_project_version("[tool.x]\nversion = '1.0.0'\n") is None
