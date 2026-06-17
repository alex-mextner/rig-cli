"""rig status — GLOBAL vs REPO layer separation, non-git handling, prominent no-rig.yaml.

The live-run bugs this fixes (from the spec):
- status mixed machine-wide GLOBAL drift (skills/hooks/harness/mcp from
  ~/.config/rig/config.yaml) with REPO drift (this repo's CI/symlinks from ./rig.yaml) in one
  flat dump → group by LAYER, each item naming WHICH config file declares it;
- in a non-git dir (e.g. ~) status printed "no rig.yaml in this repo (should be committed)" →
  it must show ONLY the global layer + "(not a git repository — repo layer N/A)";
- a real repo with no committed rig.yaml buried the fix above a 49-line drift dump → make it
  PROMINENT with "run `rig init`";
- the "extras (on-disk, not declared) are NEVER deleted by apply" reassurance must be LOUD.

These drive the CLI through `main(["status", ...])` and assert on captured stdout + exit code.
All offline (no agent-tools apply leg needed — drift detection runs against the fake catalog).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from riglib import errors
from riglib.cli import main
from riglib.layers import GLOBAL, REPO, layer_for_category


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Isolate HOME for every test in this module.

    `rig status`/`rig doctor` unconditionally call `_scan_missing_targets()`, which reads the
    real `~/.claude/settings.json` via `expanduser`. Without an isolated HOME, a dev machine
    whose real settings.json has a hook pointing at a now-gone script would make `dead_targets`
    non-empty — flipping a "clean repo → exit 0" test into EXIT_MISSING_TARGET and a "non-git →
    0/drift" test out of its expected set. An empty tmp HOME has no settings.json, so the scan
    finds nothing and the tests assert only what they mean to. Runs before each test body, so a
    test that needs a populated HOME (e.g. the GLOBAL-drift case) still overrides it.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _git_repo(path: Path) -> Path:
    """Make ``path`` a real git repo (status uses `git rev-parse`, so a bare `.git` dir is not
    enough — it must be an actual repository for env.is_git_repo to be True)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    return path


# ── category → layer classification ───────────────────────────────────────────────
def test_category_layer_classification():
    # GLOBAL: machine-wide artifacts declared in ~/.config/rig/config.yaml
    for cat in (
        "skills", "agent_hooks", "mcp", "harness", "models", "git_hooks", "gitignore",
        "tmux", "tg_ctl",
    ):
        assert layer_for_category(cat) == GLOBAL, cat
    # REPO: this repo's artifacts declared in ./rig.yaml
    for cat in ("ci", "agents_md", "github"):
        assert layer_for_category(cat) == REPO, cat


# ── non-git dir: no "should be committed", only the global layer ──────────────────
def test_status_non_git_dir_no_should_be_committed(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    # a plain dir with NO .git — the motivating case is running status in ~
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    rc = main(["status", "-C", str(plain)])
    out = capsys.readouterr().out
    assert "should be committed" not in out  # the bug: never say this outside a git repo
    assert "not a git repository" in out.lower()
    assert "repo layer" in out.lower()  # "... repo layer N/A"
    # it still ran (showed the global layer), not a hard error
    assert rc in (0, errors.EXIT_DRIFT)


def test_status_non_git_dir_ignores_local_rigyaml_repo_layer(
    tmp_path, capsys, fake_agent_tools, monkeypatch
):
    """A rig.yaml in a plain directory is not a repo layer.

    The ROADMAP wording is "show ONLY the global layer" outside a git repo. A local rig.yaml in
    a non-git directory must therefore not drive the summary, otherwise `rig status -C ~/scratch`
    would still behave like that directory had a committed repo config.
    """
    xdg = tmp_path / "xdg"
    (xdg / "rig").mkdir(parents=True)
    global_cfg = xdg / "rig" / "config.yaml"
    global_cfg.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nmodels: {enabled: false}\n"
        "tmux: {enabled: false}\ngitignore: {enabled: false}\ntg_ctl: {enabled: false}\n"
        "permissions: {enabled: false}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    local_cfg = plain / "rig.yaml"
    local_cfg.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\nskills: {{all: true}}\n",
        encoding="utf-8",
    )

    rc = main(["status", "-C", str(plain)])
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "not a git repository" in out.lower()
    assert str(global_cfg) in out
    assert str(local_cfg) not in out
    layer_line = next(line for line in out.splitlines() if line.strip().startswith("config layers:"))
    assert f"global:{global_cfg}" in layer_line
    assert f"repo:{local_cfg}" not in layer_line
    assert "repo-scoped areas are N/A outside a git repository" in out
    assert "in sync — config and disk agree" in out


def test_status_non_git_explicit_config_still_reports_global_areas_only(
    tmp_path, capsys, fake_agent_tools, monkeypatch
):
    """An explicit config can drive GLOBAL areas outside git, but REPO areas stay N/A."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    cfg = tmp_path / "explicit.yaml"
    cfg.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {all: true}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nmodels: {enabled: false}\n"
        "tmux: {enabled: false}\ngitignore: {enabled: false}\ntg_ctl: {enabled: false}\n"
        "permissions: {enabled: false}\n"
        "ci: {enabled: true, all: false, items: {codeql: {enabled: true}}}\n",
        encoding="utf-8",
    )

    rc = main(["status", "-C", str(plain), "--config", str(cfg)])
    out = capsys.readouterr().out

    assert rc == errors.EXIT_DRIFT
    assert "not a git repository" in out.lower()
    assert f"config:{cfg}" in out
    assert "skills: drift" in out.lower()
    assert "repo-scoped areas are N/A outside a git repository" in out
    assert "CI gates" not in out
    assert "REPO — this repository" not in out


def test_status_non_git_explicit_repo_only_config_says_repo_declarations_are_na(
    tmp_path, capsys, fake_agent_tools, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    cfg = tmp_path / "repo-only.yaml"
    cfg.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nmodels: {enabled: false}\n"
        "tmux: {enabled: false}\ngitignore: {enabled: false}\ntg_ctl: {enabled: false}\n"
        "permissions: {enabled: false}\n"
        "ci: {enabled: true, all: false, items: {codeql: {enabled: true}}}\n",
        encoding="utf-8",
    )

    rc = main(["status", "-C", str(plain), "--config", str(cfg)])
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "not a git repository" in out.lower()
    assert "repo-scoped areas are N/A outside a git repository" in out
    assert "CI gates" not in out
    assert "REPO — this repository" not in out


# ── non-git dir, catalog UNRESOLVABLE: still reports "not a git repository" ────────
def test_status_non_git_dir_without_resolvable_catalog(tmp_path, capsys, monkeypatch):
    """The smoke-only regression: in a non-git dir where the agent-tools checkout cannot be
    resolved (fresh machine / running in ~ with no source), the catalog scan fails — but that
    feeds only the REPO layer, which a non-git dir doesn't have. status must NOT die with the
    catalog error; it must still report "not a git repository" and exit 0.

    The other non-git test pins RIG_AGENT_TOOLS_SOURCE so the catalog resolves; this one removes
    every source (env + default candidates) to drive the CatalogError fallback path explicitly.
    """
    import riglib.catalog as catalog

    monkeypatch.delenv("RIG_AGENT_TOOLS_SOURCE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    # point every default candidate at empties so resolve_source finds no checkout anywhere
    monkeypatch.setattr(catalog, "_DEFAULT_SOURCE_CANDIDATES", (str(tmp_path / "nope"),))
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    rc = main(["status", "-C", str(plain)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not a git repository" in out.lower()
    assert "should be committed" not in out
    # the catalog error must NOT have leaked through
    assert "could not locate an agent-tools checkout" not in out


# ── real repo, no committed rig.yaml: PROMINENT fix ───────────────────────────────
def test_status_real_repo_no_rigyaml_prominent_init_hint(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = _git_repo(tmp_path / "repo")  # a real git repo, but no rig.yaml
    main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    # prominent: names the fix command, not a one-liner buried in drift
    assert "rig init" in out
    assert "no committed rig.yaml" in out.lower() or "no rig.yaml" in out.lower()


# ── layer grouping + per-item config-file provenance ──────────────────────────────
def test_status_groups_drift_by_layer_with_provenance(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = _git_repo(tmp_path / "repo")
    # an undeclared CI workflow on disk → a REPO-layer extra
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "rogue.yml").write_text("name: rogue\n", encoding="utf-8")
    cfg = repo / "rig.yaml"
    cfg.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: true, all: false}\n",
        encoding="utf-8",
    )
    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == errors.EXIT_DRIFT
    # the REPO layer heading appears (the CI extra + agents_md are REPO-layer drift). GLOBAL is
    # only printed when there IS global drift — HOME is isolated here, so there is none.
    assert "REPO — this repository" in out
    # the CI extra is reported and names the declaring REPO config file (provenance)
    assert "rogue" in out
    assert str(cfg) in out  # provenance: which config file declares this layer


# ── GLOBAL-layer drift names the GLOBAL config file ───────────────────────────────
def test_status_global_layer_drift_names_global_config(tmp_path, capsys, fake_agent_tools, monkeypatch):
    # an isolated HOME with an undeclared MCP server on disk → a GLOBAL-layer extra, and a
    # global config file so its provenance can be named.
    home = tmp_path / "home"
    (home / ".claude" / "mcp").mkdir(parents=True)
    (home / ".claude" / "mcp" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"rogue-server": {"command": "x"}}}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    xdg = tmp_path / "xdg"
    (xdg / "rig").mkdir(parents=True)
    gcfg = xdg / "rig" / "config.yaml"
    gcfg.write_text(f"version: 1\nagent_tools_source: {fake_agent_tools}\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    repo = _git_repo(tmp_path / "repo")
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\nagents_md: {enabled: false}\n",
        encoding="utf-8",
    )
    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == errors.EXIT_DRIFT
    assert "GLOBAL — machine-wide" in out
    assert "rogue-server" in out
    assert str(gcfg) in out  # the GLOBAL config file is named as the declaring layer


# ── reassurance is LOUD ───────────────────────────────────────────────────────────
def test_status_extras_never_deleted_reassurance_is_loud(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = _git_repo(tmp_path / "repo")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "rogue.yml").write_text("name: rogue\n", encoding="utf-8")
    cfg = repo / "rig.yaml"
    cfg.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: true, all: false}\n",
        encoding="utf-8",
    )
    main(["status", "-C", str(repo)])
    out = capsys.readouterr().out.lower()
    # the guarantee must be explicit: apply NEVER deletes on-disk-not-declared items
    assert "never" in out and "delete" in out
    assert "extra" in out


# ── reassurance also LOUD in `rig apply --help` ───────────────────────────────────
def test_apply_help_says_apply_never_deletes(capsys):
    import pytest

    with pytest.raises(SystemExit):
        main(["apply", "--help"])
    out = capsys.readouterr().out.lower()
    assert "never" in out and "delete" in out  # the apply-won't-nuke reassurance


# ── exit codes documented in --help (structured-exit-codes skill) ─────────────────
def test_help_documents_exit_codes(capsys):
    import pytest

    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "exit codes:" in out
    assert "4" in out and "unknown item" in out
    assert "6" in out and "not a git repository" in out


# ── exit-code precedence: a dead target outranks drift ────────────────────────────
def test_status_missing_target_outranks_drift(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """When `rig status` finds BOTH a dead hook target AND config↔disk drift, it must exit with
    EXIT_MISSING_TARGET (5), not EXIT_DRIFT (3) — the missing target must not be MASKED by drift.

    A dead hook script fails at runtime (a generic "PreToolUse error"), so it's the more urgent,
    more actionable class. Both findings are still PRINTED; only the single-valued exit code
    reflects the higher-severity class so a CI script keying on the stable contract sees it.
    """
    home = tmp_path / "home"
    gone = tmp_path / "gone-hook.py"  # referenced by a hook but never created
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 {gone}"}]}
        ]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = _git_repo(tmp_path / "repo")
    # REPO-layer drift: an undeclared CI workflow on disk while ci is enabled-but-empty.
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "rogue.yml").write_text("name: rogue\n", encoding="utf-8")
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: true, all: false}\n"
        "agents_md: {enabled: false}\n",
        encoding="utf-8",
    )
    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    # the missing-target class wins the exit code even though drift is ALSO present
    assert rc == errors.EXIT_MISSING_TARGET
    # both problems are surfaced to the user
    assert str(gone) in out  # the dead hook target is printed
    assert "rogue" in out  # the drift is printed too
    assert "precedence" in out.lower()  # the exit-code precedence is explained


# ── a clean in-sync repo still exits 0 ────────────────────────────────────────────
def test_status_clean_repo_exits_zero(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = _git_repo(tmp_path / "repo")
    cfg = repo / "rig.yaml"
    # everything OFF, including the default-on categories that would otherwise drift: agents_md
    # (the symlink), gitignore (the GLOBAL core.excludesfile block), and permissions (the harness
    # command allowlist) — all on by default.
    cfg.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n"
        "agents_md: {enabled: false}\ngitignore: {enabled: false}\npermissions: {enabled: false}\n",
        encoding="utf-8",
    )
    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "in sync" in out.lower()
