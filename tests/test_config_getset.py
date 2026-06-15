"""`rig config get|set` — the targeted read/edit-then-reconcile command.

Covers the pure dot-path engine (split/get/set/coerce in riglib.config) and the CLI
front-end (riglib.cli) end to end: get a nested key, scalar coercion on set, intermediate
key creation, the --global target, fail-closed validation, and that `set` triggers the same
plan+apply reconcile `rig apply` runs (mocked so no real install touches disk).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib import config
from riglib.cli import main


# ── pure engine: split / get / set / coerce ────────────────────────────────────────
def test_get_path_reads_nested_key():
    data = {"harness": {"mode": "auto"}, "ci": {"items": {"secret-scan": {"tier": "block"}}}}
    assert config.get_path(data, "harness.mode") == "auto"
    assert config.get_path(data, "ci.items.secret-scan.tier") == "block"


def test_get_path_missing_fails_closed():
    with pytest.raises(config.ConfigError, match="not found"):
        config.get_path({"harness": {}}, "harness.mode")


def test_get_path_through_scalar_fails_closed():
    # harness.mode is a scalar; indexing into it (harness.mode.x) is "not found", not a crash.
    with pytest.raises(config.ConfigError, match="not found"):
        config.get_path({"harness": {"mode": "auto"}}, "harness.mode.x")


def test_set_path_creates_intermediate_mappings():
    data: dict = {}
    config.set_path(data, "a.b.c", 1)
    assert data == {"a": {"b": {"c": 1}}}


def test_set_path_refuses_to_clobber_scalar_intermediate():
    with pytest.raises(config.ConfigError, match="not a mapping"):
        config.set_path({"a": 1}, "a.b", 2)


def test_split_path_rejects_empty_segments():
    with pytest.raises(config.ConfigError, match="empty segment"):
        config.split_path("a..b")
    with pytest.raises(config.ConfigError, match="empty"):
        config.split_path("")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("false", False),
        ("True", True),
        ("42", 42),
        ("-7", -7),
        ("3.14", 3.14),
        ("null", None),
        ("none", None),
        ("~", None),
        ("block", "block"),
        ("warn", "warn"),
        ("~/.agents/skills", "~/.agents/skills"),
    ],
)
def test_coerce_scalar(raw, expected):
    assert config.coerce_scalar(raw) == expected


@pytest.mark.parametrize("raw,expected", [('"true"', "true"), ("'42'", "42"), ('"a"', "a")])
def test_coerce_scalar_quote_wrap_forces_string(raw, expected):
    # a quote-wrapped keyword/number stays a literal string, never the bool/int
    out = config.coerce_scalar(raw)
    assert out == expected
    assert isinstance(out, str)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "1e3", "1_000", " 3 ", "0x10", "²", "².³"])
def test_coerce_scalar_rejects_surprising_numbers(raw):
    # conservative coercion: Python's int()/float() extras must NOT smuggle a NaN/odd int in,
    # and a Unicode superscript ('²') that str.isdigit() calls a digit but int() chokes on must
    # NOT raise — all stay strings, fail-closed.
    out = config.coerce_scalar(raw)
    assert isinstance(out, str)
    assert out == raw


# ── CLI: get ───────────────────────────────────────────────────────────────────────
def _w(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


def test_cli_get_nested_key(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "version: 1\nharness: {mode: auto, auto_mode: true}\n")
    rc = main(["config", "get", "harness.mode", "-C", str(repo)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "auto"


def test_cli_get_bool_prints_yaml_casing(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "version: 1\nharness: {auto_mode: true}\n")
    rc = main(["config", "get", "harness.auto_mode", "-C", str(repo)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "true"  # not Python's "True"


def test_cli_get_json_output(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "version: 1\nharness: {auto_mode: true}\n")
    rc = main(["config", "get", "harness.auto_mode", "-C", str(repo), "--json"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "true"  # JSON bool


def test_cli_get_json_string_is_quoted(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "version: 1\nharness: {mode: auto}\n")
    rc = main(["config", "get", "harness.mode", "-C", str(repo), "--json"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == '"auto"'  # JSON-quoted string, not bare auto


def test_cli_get_subtree_prints_yaml(tmp_path, capsys, monkeypatch):
    # `get` on a mapping prints the subtree as YAML (sort_keys=False keeps insertion order).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "version: 1\nharness: {mode: auto, auto_mode: true}\n")
    rc = main(["config", "get", "harness", "-C", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    import yaml

    assert yaml.safe_load(out) == {"mode": "auto", "auto_mode": True}
    assert out.index("mode") < out.index("auto_mode")  # insertion order preserved


def test_cli_get_json_on_subtree(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "version: 1\nharness: {mode: auto, auto_mode: true}\n")
    rc = main(["config", "get", "harness", "-C", str(repo), "--json"])
    assert rc == 0
    import json

    assert json.loads(capsys.readouterr().out) == {"mode": "auto", "auto_mode": True}


def test_cli_get_missing_path_fails_closed(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "version: 1\nharness: {mode: auto}\n")
    rc = main(["config", "get", "harness.nonexistent", "-C", str(repo)])
    assert rc == 2
    captured = capsys.readouterr()
    assert "not found" in captured.err  # diagnostics on stderr, not stdout
    assert captured.out == ""  # stdout stays clean (matters for --json piping)


def test_cli_get_missing_file_fails_closed(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["config", "get", "harness.mode", "-C", str(repo)])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_cli_get_json_error_keeps_stdout_clean(tmp_path, capsys, monkeypatch):
    # the machine-readable contract: on error, stdout carries NO partial/garbage output so a
    # `get --json | jq` pipe never chokes on a non-JSON error line.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "version: 1\nharness: {mode: auto}\n")
    rc = main(["config", "get", "harness.nope", "-C", str(repo), "--json"])
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing on stdout
    assert "not found" in captured.err


# ── CLI: set (reconcile mocked) ─────────────────────────────────────────────────────
@pytest.fixture
def _mock_apply(monkeypatch):
    """Replace the apply engine so `config set` reconcile is observable, never real.

    `_cmd_config_set` does `from .actions import run_plan`, which binds the name from the
    `riglib.actions` package namespace — so that is the seam to patch.
    """
    import riglib.actions as actions_pkg
    from riglib.actions.runner import ApplyReport

    calls: list = []

    def _fake_run_plan(plan, **_kwargs):  # signature-compatible stub
        calls.append(plan)
        return ApplyReport(results=[])

    monkeypatch.setattr(actions_pkg, "run_plan", _fake_run_plan)
    return calls


def test_cli_set_scalar_coercion_writes_bool(tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(
        repo / "rig.yaml",
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nci: {enabled: false}\n"
        "mcp: {enabled: false}\ngit_hooks: {dispatcher: {enabled: false}}\n"
        "harness: {auto_mode: true}\n",
    )
    rc = main(["config", "set", "harness.auto_mode", "false", "-C", str(repo)])
    assert rc == 0
    written = config.load(repo)
    assert written.data["harness"]["auto_mode"] is False  # real bool, not the string "false"
    assert len(_mock_apply) == 1  # reconcile ran


def test_cli_set_creates_intermediate_keys(tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(
        repo / "rig.yaml",
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n",
    )
    # ci.items.secret-scan.tier — none of items/secret-scan exist yet
    rc = main(["config", "set", "ci.items.secret-scan.tier", "warn", "-C", str(repo)])
    assert rc == 0
    written = config.load(repo)
    assert written.data["ci"]["items"]["secret-scan"]["tier"] == "warn"


def test_cli_set_validation_rejects_bad_value(tmp_path, capsys, monkeypatch, _mock_apply):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    original = "version: 1\nci: {items: {secret-scan: {tier: block}}}\n"
    _w(repo / "rig.yaml", original)
    rc = main(["config", "set", "ci.items.secret-scan.tier", "loud", "-C", str(repo)])
    assert rc == 2
    assert "tier" in capsys.readouterr().out
    # fail-closed: the bad value never reached disk
    assert (repo / "rig.yaml").read_text(encoding="utf-8") == original
    assert len(_mock_apply) == 0  # no reconcile on a rejected write


def test_cli_set_rejects_version_bool(tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply):
    # `set version true` coerces to the bool True; validate() must reject it (bool is an int
    # subclass and True == 1, so a naive isinstance check would let it through). File untouched.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    original = (
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n"
    )
    _w(repo / "rig.yaml", original)
    rc = main(["config", "set", "version", "true", "-C", str(repo)])
    assert rc == 2
    assert "version must be an int" in capsys.readouterr().out
    assert (repo / "rig.yaml").read_text(encoding="utf-8") == original
    assert len(_mock_apply) == 0


def test_cli_set_repo_refuses_when_no_config_exists(tmp_path, capsys, monkeypatch, _mock_apply):
    # a repo-local `set` with no ./rig.yaml must REFUSE (point to rig init) rather than start
    # from {} and reconcile built-in defaults onto disk — the same hazard `rig apply` guards.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    repo.mkdir()
    assert not (repo / "rig.yaml").exists()
    rc = main(["config", "set", "harness.auto_mode", "false", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "rig init" in out
    assert not (repo / "rig.yaml").exists()  # nothing was bootstrapped
    assert len(_mock_apply) == 0  # never reconciled


def test_cli_set_apply_error_keeps_config_and_returns_1(tmp_path, capsys, fake_agent_tools, monkeypatch):
    # an error DURING apply (not a build failure) is reported with rc=1 but does NOT revert the
    # already-valid config — identical to re-running `rig apply`.
    import riglib.actions as actions_pkg
    from riglib.actions.runner import ActionResult, ApplyReport

    def _erroring_run_plan(plan, **_kwargs):
        fake_action = plan.actions[0] if plan.actions else None
        results = [ActionResult(fake_action, "error", "permission denied")] if fake_action else []
        return ApplyReport(results=results)

    monkeypatch.setattr(actions_pkg, "run_plan", _erroring_run_plan)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(
        repo / "rig.yaml",
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {universal: {all: true}, by_type: {enable: [cli]}}\n"
        "agent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\nharness: {auto_mode: true}\n",
    )
    rc = main(["config", "set", "harness.auto_mode", "false", "-C", str(repo)])
    assert rc == 1  # apply reported an error
    # the valid config was NOT reverted — the edit persists
    assert config.load(repo).data["harness"]["auto_mode"] is False


def test_cli_set_no_apply_skips_reconcile(tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(
        repo / "rig.yaml",
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n"
        "harness: {auto_mode: true}\n",
    )
    rc = main(["config", "set", "harness.auto_mode", "false", "-C", str(repo), "--no-apply"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Plan:" in out  # the plan is printed
    assert "no-apply" in out
    assert len(_mock_apply) == 0  # but apply never ran
    # the write still happened
    assert config.load(repo).data["harness"]["auto_mode"] is False


def test_cli_set_refuses_to_set_removed_scope_key(tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply):
    # `set scope both` must be refused — the recommended editor never (re)introduces the removed
    # `scope` key, even though the loader still tolerates it in legacy files.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    original = (
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n"
    )
    _w(repo / "rig.yaml", original)
    rc = main(["config", "set", "scope", "both", "-C", str(repo)])
    assert rc == 2
    assert "removed setting" in capsys.readouterr().err
    assert (repo / "rig.yaml").read_text(encoding="utf-8") == original  # untouched
    assert len(_mock_apply) == 0


def test_cli_set_drops_legacy_scope_key(tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply):
    # an old config still carrying the removed `scope:` key must not have it re-emitted by a
    # successful set (we reserialize the whole file, so it would otherwise linger).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(
        repo / "rig.yaml",
        f"version: 1\nscope: both\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n"
        "harness: {auto_mode: true}\n",
    )
    rc = main(["config", "set", "harness.auto_mode", "false", "-C", str(repo)])
    assert rc == 0
    import yaml

    written = yaml.safe_load((repo / "rig.yaml").read_text(encoding="utf-8"))
    assert "scope" not in written  # the legacy key was dropped, not re-emitted
    assert written["harness"]["auto_mode"] is False


def test_cli_set_preserves_unknown_nested_key_round_trip(tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply):
    # docs claim a typo'd nested key in a non-strict section is tolerated and survives, exactly
    # like a hand-edited rig.yaml. Prove the serializer round-trip keeps it (from_dict copies
    # the whole dict — it does NOT drop unknown nested keys).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(
        repo / "rig.yaml",
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n"
        "harness: {auto_mode: true, aut_mode: true}\n",  # aut_mode is a typo (no effect)
    )
    rc = main(["config", "set", "harness.auto_mode", "false", "-C", str(repo)])
    assert rc == 0
    written = config.load(repo).data
    assert written["harness"]["auto_mode"] is False  # the real key was edited
    assert written["harness"]["aut_mode"] is True  # the typo key survived the round-trip


def test_cli_set_global_targets_xdg_config(tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    repo = tmp_path / "repo"
    # repo rig.yaml exists so the post-set reconcile has a config layer to plan from
    _w(
        repo / "rig.yaml",
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n",
    )
    rc = main(["config", "set", "defaults.on_conflict", "overwrite", "-C", str(repo), "--global"])
    assert rc == 0
    gpath = config.global_config_path()
    assert gpath.is_file()
    assert config.global_config_path() == tmp_path / ".config" / "rig" / "config.yaml"
    # the value landed in the GLOBAL file, not the repo file
    import yaml

    gdata = yaml.safe_load(gpath.read_text(encoding="utf-8"))
    assert gdata["defaults"]["on_conflict"] == "overwrite"


def test_cli_get_outside_repo_fails_soft(tmp_path, capsys, monkeypatch):
    # a non-global `get` from a plain dir (no .git, no rig.yaml) must fail closed with a clean
    # message — never a traceback (symmetric with set's broad guard).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    plain = tmp_path / "plain"
    plain.mkdir()
    rc = main(["config", "get", "harness.mode", "-C", str(plain)])
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_cli_get_global_works_outside_a_repo(tmp_path, capsys, monkeypatch):
    # `get --global` must NOT require a git repo — it reads only the global file. Point cwd at
    # a plain dir (no .git) and assert it still resolves the value.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _w(config.global_config_path(), "version: 1\ndefaults: {on_conflict: skip}\n")
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    rc = main(["config", "get", "defaults.on_conflict", "-C", str(plain), "--global"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "skip"


def test_cli_set_global_preserves_neighbors_and_stays_minimal(
    tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply
):
    # a --global set must edit ONE key and leave the global file otherwise as-is — it must NOT
    # materialize defaults (that would turn the partial global overlay into a full config and
    # change cascade semantics). Neighbor keys survive; no unexpected keys appear.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    gpath = config.global_config_path()
    _w(gpath, "version: 1\nskills: {harness_link: false}\ndefaults: {on_conflict: skip}\n")
    repo = tmp_path / "repo"
    _w(
        repo / "rig.yaml",
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n",
    )
    rc = main(["config", "set", "defaults.on_conflict", "overwrite", "-C", str(repo), "--global"])
    assert rc == 0
    import yaml

    gdata = yaml.safe_load(gpath.read_text(encoding="utf-8"))
    assert gdata["defaults"]["on_conflict"] == "overwrite"  # the edit
    assert gdata["skills"]["harness_link"] is False  # neighbor survived
    # exactly the keys that were there before — no defaults block, no ci/mcp/harness injected
    assert set(gdata.keys()) == {"version", "skills", "defaults"}


def test_cli_set_rolls_back_on_catalog_failure(tmp_path, capsys, monkeypatch, _mock_apply):
    # a value config.validate() accepts but the CATALOG rejects (a bad agent_tools_source lives
    # in plan.build(), not validate()) must roll the file back to its prior bytes — the docs
    # promise "untouched on failure".
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    original = (
        "version: 1\n"
        "agent_tools_source: /nonexistent/agent-tools\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n"
    )
    _w(repo / "rig.yaml", original)
    # on_conflict=overwrite is schema-valid, so validate() passes; the bad agent_tools_source
    # only blows up at plan build → must roll back.
    rc = main(["config", "set", "defaults.on_conflict", "overwrite", "-C", str(repo)])
    assert rc == 2
    assert (repo / "rig.yaml").read_text(encoding="utf-8") == original  # rolled back
    assert len(_mock_apply) == 0  # never reconciled


def test_cli_set_repo_file_stays_minimal(tmp_path, capsys, fake_agent_tools, monkeypatch, _mock_apply):
    # the REPO write path goes through state.write() (with header) — it must NOT materialize
    # defaults either. A set on a partial ./rig.yaml edits one key; neighbors survive and no
    # default block (mcp/agents_md/models/…) is injected.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(
        repo / "rig.yaml",
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false, all: false}\n"
        "harness: {auto_mode: true}\n",
    )
    rc = main(["config", "set", "harness.auto_mode", "false", "-C", str(repo)])
    assert rc == 0
    import yaml

    written = yaml.safe_load((repo / "rig.yaml").read_text(encoding="utf-8"))
    assert written["harness"]["auto_mode"] is False  # the edit
    assert written["skills"]["enabled"] is False  # neighbor survived
    # exactly the keys that were there — no defaults block, no models/agents_md injected
    assert set(written.keys()) == {
        "version", "agent_tools_source", "skills", "agent_hooks", "mcp",
        "git_hooks", "ci", "harness",
    }


def test_cli_set_global_fresh_file_rolled_back_on_failure(tmp_path, capsys, monkeypatch, _mock_apply):
    # the original-is-None rollback branch: a --global set that CREATES a fresh
    # ~/.config/rig/config.yaml but then fails the second gate (bad repo agent_tools_source)
    # must delete both the file AND the freshly-created rig dir.
    xdg = tmp_path / ".config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    repo = tmp_path / "repo"
    _w(  # repo config has a bad source → the post-write reconcile plan fails
        repo / "rig.yaml",
        "version: 1\nagent_tools_source: /nonexistent/agent-tools\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n",
    )
    gpath = config.global_config_path()
    assert not gpath.exists()  # no global file yet → set will CREATE it
    rc = main(["config", "set", "defaults.on_conflict", "overwrite", "-C", str(repo), "--global"])
    assert rc == 2
    assert not gpath.exists()  # the freshly-created file was removed
    assert not gpath.parent.exists()  # and the rig/ dir we created was cleaned up
    assert len(_mock_apply) == 0


def test_cli_set_global_bad_value_caught_by_second_gate(tmp_path, capsys, monkeypatch, _mock_apply):
    # prove the second (plan-build) gate validates the GLOBAL layer too: a bad agent_tools_source
    # written into the global file must fail the cascade plan build and roll the global file back,
    # even though the repo file is otherwise fine.
    xdg = tmp_path / ".config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    gpath = config.global_config_path()
    _w(gpath, "version: 1\ndefaults: {on_conflict: skip}\n")
    repo = tmp_path / "repo"
    _w(  # repo has no source → the global agent_tools_source is what the cascade resolves
        repo / "rig.yaml",
        "version: 1\nskills: {enabled: false}\nagent_hooks: {enabled: false}\n"
        "mcp: {enabled: false}\ngit_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n",
    )
    original = gpath.read_text(encoding="utf-8")
    rc = main(["config", "set", "agent_tools_source", "/nonexistent/agent-tools", "-C", str(repo), "--global"])
    assert rc == 2
    assert "not an agent-tools checkout" in capsys.readouterr().out
    assert gpath.read_text(encoding="utf-8") == original  # global file rolled back
    assert len(_mock_apply) == 0


def test_cli_config_no_subcommand_errors(tmp_path, capsys, monkeypatch):
    rc = main(["config"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "get or set" in out
