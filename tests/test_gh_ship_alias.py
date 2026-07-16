"""Machine-global ``gh ship`` alias provisioning — the ``gh_ship_alias`` area.

The DURABLE half of "gh ship must just work": rig provisions the ``gh ship`` gh alias (previously
HAND-SET, so it silently vanished on a clean machine / gh reset) as a PORTABLE dispatcher that
runs the per-repo delegator ``.claude/scripts/pr-ship.sh`` (or the canonical fallback via the
machine env file). Covers: the constant, portable expansion; a behavioral proof the expansion is
valid POSIX ``sh`` and reaches the delegator/fallback; the shared resolve classification; plan
gating; the idempotent runner handler (dry-run, create/update/ok/skip/no_gh); drift parity; and
the area/layer registration.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from riglib import gh_ship_alias as gsa
from riglib.actions.runner import _do_provision_gh_ship_alias
from riglib.areas import AREAS
from riglib.drift import DriftReport, _check_gh_ship_alias
from riglib.layers import GLOBAL, layer_for_category
from riglib.plan import Action, build


def _action() -> Action:
    return Action(
        kind="provision_gh_ship_alias",
        category=gsa.GH_SHIP_ALIAS_CATEGORY,
        item="alias",
        source=Path("/src"),
        target=Path("/repo"),
        options={},
    )


def _loaded(cfg: dict, repo: Path):
    from riglib.config import LoadedConfig

    return LoadedConfig(data=cfg, repo_root=repo)


# ── the expansion: portable constant, valid dispatcher ──────────────────────────────
def test_expansion_is_shell_alias_and_portable():
    body = gsa.gh_ship_alias_expansion()
    assert body.startswith("!"), "a gh SHELL alias must start with '!'"
    assert ".claude/scripts/pr-ship.sh" in body, "must dispatch to the per-repo delegator"
    assert "AGENT_TOOLS_ROOT" in body, "must have the canonical fallback"
    # PORTABLE CONSTANT: no machine-specific absolute path baked in (the delegator + env file carry
    # the machine specifics). A concrete home path would make a re-apply rewrite on every machine.
    assert "/Users/" not in body and "/home/" not in body


def test_expansion_execs_the_repo_delegator(tmp_path):
    # Behavioral proof the expansion is valid POSIX sh AND reaches the delegator: a repo whose
    # .claude/scripts/pr-ship.sh echoes a marker + its args must be exec'd with the passed args.
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q"], cwd=(repo.mkdir(parents=True) or repo), check=True)
    deleg = repo / ".claude" / "scripts" / "pr-ship.sh"
    deleg.parent.mkdir(parents=True)
    deleg.write_text('#!/bin/sh\necho "DELEGATOR:$*"\n', encoding="utf-8")
    deleg.chmod(0o755)
    body = gsa.gh_ship_alias_expansion()[1:]  # strip the gh '!' shell marker
    res = subprocess.run(
        ["sh", "-c", body, "sh", "123", "--yes"],
        cwd=repo, capture_output=True, text=True, timeout=15,
    )
    assert res.stdout.strip() == "DELEGATOR:123 --yes", res.stderr


def test_expansion_falls_back_to_canonical_via_agent_tools_root(tmp_path):
    # Outside a managed repo (no delegator), the dispatcher resolves ci/ship/ship.sh via
    # AGENT_TOOLS_ROOT and execs it with the args.
    at = tmp_path / "agent-tools"
    ship = at / "ci" / "ship" / "ship.sh"
    ship.parent.mkdir(parents=True)
    ship.write_text('#!/bin/sh\necho "CANONICAL:$*"\n', encoding="utf-8")
    ship.chmod(0o755)
    body = gsa.gh_ship_alias_expansion()[1:]
    res = subprocess.run(
        ["sh", "-c", body, "sh", "42"],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "AGENT_TOOLS_ROOT": str(at)},
    )
    assert res.stdout.strip() == "CANONICAL:42", res.stderr


def test_expansion_exits_127_when_unresolvable(tmp_path):
    body = gsa.gh_ship_alias_expansion()[1:]
    res = subprocess.run(
        ["sh", "-c", body, "sh"],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},  # no delegator, no AGENT_TOOLS_ROOT
    )
    assert res.returncode == 127
    assert "gh ship" in res.stderr


# ── resolve: the shared apply/drift classification ──────────────────────────────────
def test_resolve_no_gh(monkeypatch):
    monkeypatch.setattr(gsa, "gh_available", lambda: False)
    assert gsa.resolve_gh_ship_alias().state == "no_gh"


def test_resolve_create_update_ok(monkeypatch):
    monkeypatch.setattr(gsa, "gh_available", lambda: True)
    monkeypatch.setattr(gsa, "_read_ship_alias", lambda: None)
    assert gsa.resolve_gh_ship_alias().state == "create"
    monkeypatch.setattr(gsa, "_read_ship_alias", lambda: "!something-else")
    r = gsa.resolve_gh_ship_alias()
    assert r.state == "update" and r.current == "!something-else"
    monkeypatch.setattr(gsa, "_read_ship_alias", gsa.gh_ship_alias_expansion)
    assert gsa.resolve_gh_ship_alias().state == "ok"


def test_read_ship_alias_round_trips_gh_config(tmp_path, monkeypatch):
    # IDEMPOTENCE regression: `_read_ship_alias` must return the YAML-stored expansion BYTE-EXACTLY
    # (parsing `gh alias list`'s display format — surrounding quotes + doubled internal quotes —
    # made a just-written alias resolve to `update` forever, so apply rewrote it every run).
    pytest.importorskip("yaml")
    import yaml

    gh_dir = tmp_path / "gh"
    gh_dir.mkdir()
    monkeypatch.setenv("GH_CONFIG_DIR", str(gh_dir))
    desired = gsa.gh_ship_alias_expansion()
    (gh_dir / "config.yml").write_text(
        yaml.safe_dump({"aliases": {"ship": desired, "co": "pr checkout"}}), encoding="utf-8"
    )
    assert gsa._read_ship_alias() == desired, "the stored alias must round-trip byte-exactly"
    monkeypatch.setattr(gsa, "gh_available", lambda: True)
    assert gsa.resolve_gh_ship_alias().state == "ok"


# ── plan gating ─────────────────────────────────────────────────────────────────────
def test_plan_includes_gh_ship_alias_by_default(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {"version": 1, "agent_tools_source": str(fake_agent_tools)}
    plan = build(_loaded(cfg, repo), Catalog.scan(str(fake_agent_tools)), project_type="cli")
    assert len([a for a in plan.actions if a.kind == "provision_gh_ship_alias"]) == 1


def test_plan_omits_alias_when_delegator_disabled(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {
        "version": 1,
        "agent_tools_source": str(fake_agent_tools),
        "ship_delegator": {"enabled": False},
    }
    plan = build(_loaded(cfg, repo), Catalog.scan(str(fake_agent_tools)), project_type="cli")
    assert not [a for a in plan.actions if a.kind == "provision_gh_ship_alias"]


def test_plan_alias_target_is_gh_config(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {"version": 1, "agent_tools_source": str(fake_agent_tools)}
    plan = build(_loaded(cfg, repo), Catalog.scan(str(fake_agent_tools)), project_type="cli")
    alias = [a for a in plan.actions if a.kind == "provision_gh_ship_alias"]
    assert alias and alias[0].target == gsa.gh_config_path(), "target must be gh's config, not repo root"


def test_plan_ci_ship_gh_alias_emits_alias_even_without_delegator(fake_agent_tools, tmp_path):
    # A ci `ship` item with gh_alias:true must provision the alias EVEN IF ship_delegator is off —
    # and via the same single action (no second, unconditional writer in install_ci).
    from riglib.catalog import Catalog

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {
        "version": 1,
        "agent_tools_source": str(fake_agent_tools),
        "ship_delegator": {"enabled": False},
        "ci": {"items": {"ship": {"enabled": True, "gh_alias": True}}},
    }
    plan = build(_loaded(cfg, repo), Catalog.scan(str(fake_agent_tools)), project_type="cli")
    assert len([a for a in plan.actions if a.kind == "provision_gh_ship_alias"]) == 1
    assert not [a for a in plan.actions if a.kind == "provision_ship_delegator"]


def test_plan_emits_exactly_one_alias_when_both_paths_request(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {  # delegator default-on AND ci ship gh_alias on
        "version": 1,
        "agent_tools_source": str(fake_agent_tools),
        "ci": {"items": {"ship": {"enabled": True, "gh_alias": True}}},
    }
    plan = build(_loaded(cfg, repo), Catalog.scan(str(fake_agent_tools)), project_type="cli")
    assert len([a for a in plan.actions if a.kind == "provision_gh_ship_alias"]) == 1, "no duplicate writer"


def test_install_ci_ship_does_not_set_alias_live(tmp_path, monkeypatch):
    # The removed second writer: a ci ship install with gh_alias:true must NOT call the live setter
    # (the dedicated provision_gh_ship_alias action owns it, honoring on_conflict + idempotence).
    from riglib.actions.runner import _do_install_ci

    src = tmp_path / "ci-ship"
    src.mkdir()
    (src / "ship.sh").write_text("#!/bin/sh\necho ship\n", encoding="utf-8")
    calls = {"n": 0}
    monkeypatch.setattr(gsa, "set_gh_ship_alias", lambda: calls.__setitem__("n", calls["n"] + 1) or 0)
    act = Action(
        kind="install_ci", category="ci", item="ship", source=src,
        target=tmp_path / "bin", options={"slot": "ship", "gh_alias": True},
    )
    res = _do_install_ci(act, "backup")
    assert res.status in ("created", "updated")
    assert calls["n"] == 0, "install_ci must not set the alias directly"


# ── read-failure handling: never traceback, never clobber ───────────────────────────
def test_unreadable_gh_config_resolves_unknown_and_handler_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(gsa, "gh_available", lambda: True)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path))
    # binary/undecodable bytes at the config path → UnicodeDecodeError must NOT escape (would
    # traceback `rig status`); resolve → unknown; apply → skipped (never clobbers a broken config).
    (tmp_path / "config.yml").write_bytes(b"\xff\xfe\x00 not utf8")
    assert gsa.resolve_gh_ship_alias().state == "unknown"

    g = __import__("riglib.gh_ship_alias", fromlist=["x"])
    monkeypatch.setattr(g, "set_gh_ship_alias", lambda: (_ for _ in ()).throw(AssertionError("must not write")))
    monkeypatch.delenv("RIG_GH_ALIAS_DRY_RUN", raising=False)
    res = _do_provision_gh_ship_alias(_action(), "backup")
    assert res.status == "skipped" and "unreadable" in res.detail


def test_malformed_yaml_gh_config_resolves_unknown(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    monkeypatch.setattr(gsa, "gh_available", lambda: True)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.yml").write_text("aliases: [unclosed\n", encoding="utf-8")
    assert gsa.resolve_gh_ship_alias().state == "unknown"


# ── safe env-file contract in the dispatcher (mirror the delegator) ─────────────────
def test_dispatcher_explicit_agent_tools_root_wins_over_env_file(tmp_path):
    # An explicit $AGENT_TOOLS_ROOT must WIN — the env file is not sourced (pointing a shell at
    # another checkout needs no re-apply), exactly like the delegator.
    explicit = tmp_path / "explicit"
    (explicit / "ci" / "ship").mkdir(parents=True)
    (explicit / "ci" / "ship" / "ship.sh").write_text('#!/bin/sh\necho "EXPLICIT:$*"\n', encoding="utf-8")
    (explicit / "ci" / "ship" / "ship.sh").chmod(0o755)
    cfgdir = tmp_path / ".config" / "agent-tools"
    cfgdir.mkdir(parents=True)
    (cfgdir / "env").write_text(f'AGENT_TOOLS_ROOT="{tmp_path / "OTHER"}"\n', encoding="utf-8")
    body = gsa.gh_ship_alias_expansion()[1:]
    res = subprocess.run(
        ["sh", "-c", body, "sh", "9"],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "AGENT_TOOLS_ROOT": str(explicit)},
    )
    assert res.stdout.strip() == "EXPLICIT:9", res.stderr


def test_dispatcher_refuses_symlinked_env_file(tmp_path):
    # A SYMLINKED env file must NOT be sourced (rig refuses to manage a symlink there; the runtime
    # draws the same line). With no delegator, no explicit root, and only a symlinked env → 127.
    at = tmp_path / "agent-tools"
    (at / "ci" / "ship").mkdir(parents=True)
    (at / "ci" / "ship" / "ship.sh").write_text('#!/bin/sh\necho hi\n', encoding="utf-8")
    (at / "ci" / "ship" / "ship.sh").chmod(0o755)
    cfgdir = tmp_path / ".config" / "agent-tools"
    cfgdir.mkdir(parents=True)
    real = tmp_path / "real-env"
    real.write_text(f'AGENT_TOOLS_ROOT="{at}"\n', encoding="utf-8")
    (cfgdir / "env").symlink_to(real)
    body = gsa.gh_ship_alias_expansion()[1:]
    res = subprocess.run(
        ["sh", "-c", body, "sh"],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert res.returncode == 127, "symlinked env file must be refused → unresolved → 127"


# ── runner handler: idempotent, dry-run guarded, conflict-honoring ──────────────────
def _stub_resolve(monkeypatch, state, current=None):
    monkeypatch.setattr(
        runner_gsa := __import__("riglib.gh_ship_alias", fromlist=["x"]),
        "resolve_gh_ship_alias",
        lambda: gsa.GhAliasResolution(state, current, gsa.gh_ship_alias_expansion()),
    )
    return runner_gsa


def test_handler_no_gh_skips(monkeypatch):
    _stub_resolve(monkeypatch, "no_gh")
    res = _do_provision_gh_ship_alias(_action(), "backup")
    assert res.status == "skipped" and "gh` CLI not found" in res.detail


def test_handler_ok_skips(monkeypatch):
    _stub_resolve(monkeypatch, "ok", gsa.gh_ship_alias_expansion())
    assert _do_provision_gh_ship_alias(_action(), "backup").status == "skipped"


def test_handler_dry_run_never_writes(monkeypatch):
    g = _stub_resolve(monkeypatch, "create")
    called = {"n": 0}
    monkeypatch.setattr(g, "set_gh_ship_alias", lambda: called.__setitem__("n", called["n"] + 1) or 0)
    monkeypatch.setenv("RIG_GH_ALIAS_DRY_RUN", "1")
    res = _do_provision_gh_ship_alias(_action(), "backup")
    assert res.status == "skipped" and "dry-run" in res.detail
    assert called["n"] == 0, "dry-run must not call the live `gh alias set`"


def test_handler_creates(monkeypatch):
    g = _stub_resolve(monkeypatch, "create")
    monkeypatch.setattr(g, "set_gh_ship_alias", lambda: 0)
    monkeypatch.delenv("RIG_GH_ALIAS_DRY_RUN", raising=False)
    res = _do_provision_gh_ship_alias(_action(), "backup")
    assert res.status == "created"


def test_handler_updates_notes_old_value(monkeypatch):
    g = _stub_resolve(monkeypatch, "update", "!old-body")
    monkeypatch.setattr(g, "set_gh_ship_alias", lambda: 0)
    monkeypatch.delenv("RIG_GH_ALIAS_DRY_RUN", raising=False)
    res = _do_provision_gh_ship_alias(_action(), "backup")
    assert res.status == "updated" and "!old-body" in res.detail


def test_handler_skip_conflict_leaves_user_alias(monkeypatch):
    g = _stub_resolve(monkeypatch, "update", "!user-custom")
    calls = {"n": 0}
    monkeypatch.setattr(g, "set_gh_ship_alias", lambda: calls.__setitem__("n", calls["n"] + 1) or 0)
    monkeypatch.delenv("RIG_GH_ALIAS_DRY_RUN", raising=False)
    res = _do_provision_gh_ship_alias(_action(), "skip")
    assert res.status == "skipped" and "!user-custom" in res.detail
    assert calls["n"] == 0, "on_conflict=skip must not overwrite a user's alias"


def test_handler_set_failure_is_soft_error(monkeypatch):
    g = _stub_resolve(monkeypatch, "create")
    monkeypatch.setattr(g, "set_gh_ship_alias", lambda: 1)
    monkeypatch.delenv("RIG_GH_ALIAS_DRY_RUN", raising=False)
    res = _do_provision_gh_ship_alias(_action(), "backup")
    assert res.status == "error" and "gh alias set` failed" in res.detail


# ── drift parity ────────────────────────────────────────────────────────────────────
def test_drift_missing_and_modified(monkeypatch):
    monkeypatch.setattr(gsa, "gh_available", lambda: True)
    monkeypatch.setattr(gsa, "_read_ship_alias", lambda: None)
    rep = DriftReport()
    _check_gh_ship_alias(_action(), rep)
    assert [i for i in rep.items if i.direction == "missing" and i.category == "gh_ship_alias"]

    monkeypatch.setattr(gsa, "_read_ship_alias", lambda: "!different")
    rep2 = DriftReport()
    _check_gh_ship_alias(_action(), rep2)
    assert [i for i in rep2.items if i.direction == "modified" and i.category == "gh_ship_alias"]


def test_drift_clean_when_ok_or_no_gh(monkeypatch):
    monkeypatch.setattr(gsa, "gh_available", lambda: True)
    monkeypatch.setattr(gsa, "_read_ship_alias", gsa.gh_ship_alias_expansion)
    rep = DriftReport()
    _check_gh_ship_alias(_action(), rep)
    assert not rep.items
    monkeypatch.setattr(gsa, "gh_available", lambda: False)
    rep2 = DriftReport()
    _check_gh_ship_alias(_action(), rep2)
    assert not rep2.items, "gh absent → no phantom drift (apply/status parity)"


# ── registries ──────────────────────────────────────────────────────────────────────
def test_area_and_layer_registered():
    assert layer_for_category("gh_ship_alias") == GLOBAL
    assert any(a.key == "gh_ship_alias" and a.layer == GLOBAL for a in AREAS)
