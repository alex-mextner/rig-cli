"""Tests for `rig setup` (the interactive config wizard) + `rig config get|set`.

Exercises production code, not mocks: the real option registry (riglib.schema), the real
wizard loop (riglib.setup_wizard.run_setup driven by scripted input + an injected apply fn that
calls the real engine), and the real `rig config` CLI handlers writing real YAML to disk. The
autouse `_isolate_home`/`_isolate_scheduler` fixtures keep HOME/XDG and the scheduler off the
real machine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib import schema, setup_wizard
from riglib.cli import main


# ── the option registry (single source of truth) ────────────────────────────────────────
def test_registry_keys_match_their_category():
    for opt in schema.all_options():
        assert opt.key.split(".", 1)[0] == opt.category
        assert opt.hint, f"{opt.key} has no hint"


def test_registry_covers_every_status_area():
    # the wizard must show what is enabled across ALL reconciled areas (the `rig status` rows).
    cats = {a.category for a in schema.AREAS}
    expected = {
        "skills", "agent_hooks", "git_hooks", "ci", "mcp", "harness",
        "models", "agents_md", "github", "tmux", "gitignore", "tg_ctl",
    }
    assert cats == expected


def test_global_only_categories_route_to_global_not_repo():
    # the footgun guard: a machine-wide, never-scaffolded block must NOT be writable into a
    # committed repo rig.yaml. These three are documented global-only.
    for cat in ("gitignore", "tg_ctl", "tmux"):
        assert schema.writable_layer_for_category(cat) == schema.GLOBAL
    # harness/models/git_hooks are status-grouped GLOBAL but the scaffold writes them into the
    # committed repo rig.yaml, so an EDIT of their value belongs in the repo file.
    for cat in ("harness", "models", "git_hooks", "skills", "ci", "mcp", "github", "agents_md"):
        assert schema.writable_layer_for_category(cat) == schema.REPO


def test_json_schema_is_emitted_from_registry():
    doc = schema.json_schema()
    assert doc["$schema"].startswith("http://json-schema.org/draft-07")
    # a nested key lands as a nested object schema with the hint as its description
    auto = doc["properties"]["harness"]["properties"]["auto_mode"]
    assert auto["type"] == "boolean"
    assert "auto-accept" in auto["description"].lower()


def test_coerce_bool_and_int_and_reject_bad():
    o_bool = schema.option_for_key("harness.auto_mode")
    assert schema.coerce(o_bool, "no") is False
    assert schema.coerce(o_bool, "yes") is True
    with pytest.raises(ValueError):
        schema.coerce(o_bool, "maybe")
    o_int = schema.option_for_key("github.ruleset.required_reviews")
    assert schema.coerce(o_int, "2") == 2
    with pytest.raises(ValueError):
        schema.coerce(o_int, "lots")


def test_effective_value_falls_back_to_default():
    o = schema.option_for_key("skills.enabled")
    assert schema.effective_value(o, {}) is True  # absent → default
    assert schema.effective_value(o, {"skills": {"enabled": False}}) is False


def test_effective_value_absent_block_presence_gated_block_shows_off():
    # A sparse rig.yaml that OMITS an entire block whose plan builder treats an absent block as
    # INACTIVE (harness, models, git_hooks) must NOT be reported as enabled — `rig apply` would
    # skip it, so claiming it is on is a lie. (Regression for the PR-34 codex P2 finding.)
    for key in ("harness.enabled", "harness.auto_mode", "models.enabled",
                "git_hooks.dispatcher.enabled"):
        o = schema.option_for_key(key)
        assert schema.effective_value(o, {}) is False, key
    # but a PRESENT block with a missing leaf still defaults that leaf (the block IS active):
    o_auto = schema.option_for_key("harness.auto_mode")
    assert schema.effective_value(o_auto, {"harness": {"enabled": True}}) is True
    # and a default-ON category whose absent block apply STILL activates keeps its default:
    for key in ("skills.enabled", "ci.enabled", "agents_md.enabled", "gitignore.enabled"):
        o = schema.option_for_key(key)
        assert schema.effective_value(o, {}) is True, key


# ── non-interactive `rig setup` → USAGE, never a half-wizard ─────────────────────────────
def test_setup_non_interactive_prints_usage(monkeypatch, capsys):
    # force the no-TTY branch regardless of how the test runner is wired.
    monkeypatch.setattr(setup_wizard, "is_interactive", lambda: False)
    rc = main(["setup"])
    out = capsys.readouterr().out
    assert rc == 0
    # it points at the core commands, and does NOT pretend to be a wizard prompt
    assert "rig init" in out
    assert "rig apply" in out
    assert "rig config get" in out
    assert "rig config set" in out
    assert "non-interactive" in out.lower()


def test_is_interactive_requires_both_tty(monkeypatch):
    class _S:
        def __init__(self, tty):
            self._tty = tty

        def isatty(self):
            return self._tty

    monkeypatch.setattr(setup_wizard.sys, "stdin", _S(True))
    monkeypatch.setattr(setup_wizard.sys, "stdout", _S(False))
    assert setup_wizard.is_interactive() is False
    monkeypatch.setattr(setup_wizard.sys, "stdout", _S(True))
    assert setup_wizard.is_interactive() is True


# ── render_state shows every area, its enabled flag, and the inline hints ─────────────────
def test_render_state_shows_areas_values_and_hints():
    data = {"harness": {"auto_mode": False}, "skills": {"enabled": True}}
    rendered = setup_wizard.render_state(data)
    # area titles + the inline hint text are present
    assert "skills" in rendered
    assert "harness / auto-mode" in rendered
    assert "auto-accept tool calls" in rendered.lower()
    # the changed value reflects the config (auto_mode off), defaults show for the rest
    assert "auto_mode" in rendered
    # global-only areas are tagged GLOBAL, repo areas REPO
    assert "[GLOBAL]" in rendered
    assert "[REPO]" in rendered


# ── interactive wizard loop (scripted input, real engine via injected apply) ──────────────
def _make_repo(tmp_path: Path) -> Path:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    (repo / "rig.yaml").write_text(
        "version: 1\nharness: {enabled: true, auto_mode: true}\n", encoding="utf-8"
    )
    return repo


def _index_of(key: str) -> str:
    for i, o in enumerate(schema.all_options(), 1):
        if o.key == key:
            return str(i)
    raise AssertionError(key)


def test_wizard_edits_repo_option_into_repo_yaml(tmp_path):
    repo = _make_repo(tmp_path)
    applied = []
    answers = iter([_index_of("harness.auto_mode"), "no", "q", "y"])
    rc = setup_wizard.run_setup(
        repo,
        apply_fn=lambda root: (applied.append(root) or 0),
        input_fn=lambda _prompt: next(answers),
        out=lambda _s: None,
    )
    assert rc == 0
    assert applied == [repo]  # the change triggered an apply
    import yaml

    data = yaml.safe_load((repo / "rig.yaml").read_text())
    assert data["harness"]["auto_mode"] is False  # written to the REPO file


def test_wizard_edits_global_only_option_into_global_config_not_repo(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    gdir = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(gdir))
    answers = iter([_index_of("tg_ctl.enabled"), "no", "q", "y"])
    setup_wizard.run_setup(
        repo,
        apply_fn=lambda _root: 0,
        input_fn=lambda _prompt: next(answers),
        out=lambda _s: None,
    )
    import yaml

    # tg_ctl is global-only → written to the global config, NEVER into the committed repo file
    gcfg = yaml.safe_load((gdir / "rig" / "config.yaml").read_text())
    assert gcfg["tg_ctl"]["enabled"] is False
    repo_data = yaml.safe_load((repo / "rig.yaml").read_text())
    assert "tg_ctl" not in repo_data


def test_wizard_apply_command_uses_the_real_engine(tmp_path):
    # selecting (a)pply with no pending change still runs apply once and returns its rc.
    repo = _make_repo(tmp_path)
    calls = []
    answers = iter(["a"])
    rc = setup_wizard.run_setup(
        repo,
        apply_fn=lambda root: (calls.append(root) or 7),
        input_fn=lambda _prompt: next(answers),
        out=lambda _s: None,
    )
    assert rc == 7  # the apply fn's rc is propagated
    assert calls == [repo]


def test_wizard_change_then_menu_apply(tmp_path):
    # change a value, then pick (a)pply from the menu → the edit is written AND apply runs.
    repo = _make_repo(tmp_path)
    calls = []
    answers = iter([_index_of("harness.auto_mode"), "no", "a"])
    rc = setup_wizard.run_setup(
        repo,
        apply_fn=lambda root: (calls.append(root) or 0),
        input_fn=lambda _prompt: next(answers),
        out=lambda _s: None,
    )
    assert rc == 0
    assert calls == [repo]  # (a)pply from the menu ran apply once
    import yaml

    assert yaml.safe_load((repo / "rig.yaml").read_text())["harness"]["auto_mode"] is False


def test_wizard_propagates_non_zero_apply_rc(tmp_path):
    # a change then apply that FAILS must propagate the apply rc (not swallow it / not print
    # "run rig apply" — the user asked to apply and it ran).
    repo = _make_repo(tmp_path)
    out_lines = []
    answers = iter([_index_of("harness.auto_mode"), "no", "a"])
    rc = setup_wizard.run_setup(
        repo,
        apply_fn=lambda _root: 5,
        input_fn=lambda _prompt: next(answers),
        out=lambda s: out_lines.append(s),
    )
    assert rc == 5  # the failing apply rc is returned verbatim
    assert not any("run `rig apply` to converge" in ln for ln in out_lines)


def test_wizard_quit_without_changes_does_not_apply(tmp_path):
    repo = _make_repo(tmp_path)
    calls = []
    answers = iter(["q"])
    rc = setup_wizard.run_setup(
        repo,
        apply_fn=lambda root: (calls.append(root) or 0),
        input_fn=lambda _prompt: next(answers),
        out=lambda _s: None,
    )
    assert rc == 0
    assert calls == []  # nothing changed → no apply prompt, no apply


def test_wizard_rejects_invalid_value_and_leaves_config_untouched(tmp_path):
    repo = _make_repo(tmp_path)
    before = (repo / "rig.yaml").read_text()
    out_lines = []
    # set models.schedule.time to a malformed value → validation rejects, config unchanged
    answers = iter([_index_of("models.schedule.time"), "99:99", "q"])
    setup_wizard.run_setup(
        repo,
        apply_fn=lambda _root: 0,
        input_fn=lambda _prompt: next(answers),
        out=lambda s: out_lines.append(s),
    )
    assert (repo / "rig.yaml").read_text() == before  # fail-closed: nothing written
    assert any("rejected" in ln for ln in out_lines)


# ── `rig config get|set` (the headless counterpart) ──────────────────────────────────────
def test_config_get_reads_cascaded_value(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    rc = main(["config", "get", "harness.auto_mode", "-C", str(repo)])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "true"


def test_config_get_absent_key_falls_back_to_default(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    rc = main(["config", "get", "skills.enabled", "-C", str(repo)])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "true"  # default when absent from config


def test_config_get_int_option_renders_bare_number(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    rc = main(["config", "get", "github.ruleset.required_reviews", "-C", str(repo)])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "0"  # int default, machine-readable bare number


def test_config_get_returns_global_override_of_repo_key(tmp_path, capsys, monkeypatch):
    # the cascade's core behavior: a repo key set in the GLOBAL config (and absent from the repo
    # rig.yaml) is what `config get` returns.
    gdir = tmp_path / "xdg-ovr"
    (gdir / "rig").mkdir(parents=True)
    (gdir / "rig" / "config.yaml").write_text(
        "harness: {auto_mode: false}\n", encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(gdir))
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    (repo / "rig.yaml").write_text("version: 1\n", encoding="utf-8")  # no harness key in repo
    rc = main(["config", "get", "harness.auto_mode", "-C", str(repo)])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "false"  # the global override wins over the registry default


def test_config_set_repo_key_writes_repo_yaml(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    rc = main(["config", "set", "harness.auto_mode", "no", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "REPO" in out
    import yaml

    data = yaml.safe_load((repo / "rig.yaml").read_text())
    assert data["harness"]["auto_mode"] is False


def test_config_set_global_only_key_writes_global_config(tmp_path, capsys, monkeypatch):
    repo = _make_repo(tmp_path)
    gdir = tmp_path / "xdg2"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(gdir))
    rc = main(["config", "set", "gitignore.enabled", "no", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "GLOBAL" in out
    import yaml

    gcfg = yaml.safe_load((gdir / "rig" / "config.yaml").read_text())
    assert gcfg["gitignore"]["enabled"] is False
    assert "gitignore" not in yaml.safe_load((repo / "rig.yaml").read_text())


def test_config_set_global_flag_forces_global_for_repo_key(tmp_path, capsys, monkeypatch):
    repo = _make_repo(tmp_path)
    gdir = tmp_path / "xdg3"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(gdir))
    rc = main(["config", "set", "harness.auto_mode", "no", "--global", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "GLOBAL" in out
    import yaml

    gcfg = yaml.safe_load((gdir / "rig" / "config.yaml").read_text())
    assert gcfg["harness"]["auto_mode"] is False


def test_config_get_unknown_key_exits_config_class(tmp_path, capsys):
    from riglib import errors

    repo = _make_repo(tmp_path)
    rc = main(["config", "get", "bogus.key", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == errors.EXIT_CONFIG
    assert "unknown config key" in out


def test_config_set_valid_string_value(tmp_path, capsys):
    import yaml

    repo = _make_repo(tmp_path)
    rc = main(["config", "set", "models.schedule.time", "09:30", "-C", str(repo)])
    assert rc == 0
    data = yaml.safe_load((repo / "rig.yaml").read_text())
    assert data["models"]["schedule"]["time"] == "09:30"


def test_config_set_unknown_key_exits_config_class(tmp_path, capsys):
    from riglib import errors

    repo = _make_repo(tmp_path)
    rc = main(["config", "set", "bogus.key", "x", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == errors.EXIT_CONFIG
    assert "unknown config key" in out


def test_config_set_invalid_value_fails_closed(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    before = (repo / "rig.yaml").read_text()
    rc = main(["config", "set", "models.schedule.time", "25:00", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "error" in out.lower()
    assert (repo / "rig.yaml").read_text() == before  # nothing written


def test_config_no_action_prints_help(capsys):
    rc = main(["config"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "get" in out and "set" in out


# ── regression guards the review flagged ────────────────────────────────────────────────
def test_writable_layer_agrees_with_the_scaffold():
    """A category writable to REPO must be one the default scaffold commits into rig.yaml.

    This is the guard against silent divergence: if a new GLOBAL-only block is added but not to
    `_GLOBAL_ONLY_CATEGORIES`, the wizard would route it into a committed repo rig.yaml. Cross-
    check every registered category against what `state.default_state` actually scaffolds.
    """
    from riglib.layers import layer_for_category
    from riglib.state import default_state

    scaffolded = set(default_state())
    for area in schema.AREAS:
        if schema.writable_layer_for_category(area.category) == schema.REPO:
            # a REPO-writable category is either scaffolded into rig.yaml, or default-on at plan
            # level and a genuine repo artifact (agents_md — a file IN the repo).
            assert area.category in scaffolded or area.category == "agents_md", area.category
        else:
            # a GLOBAL-only category must NOT be scaffolded into the committed repo file.
            assert area.category not in scaffolded, area.category
    # REVERSE guard: every registered category the SCAFFOLD does NOT commit into rig.yaml AND that
    # layers.py groups as GLOBAL must be routed GLOBAL-only — otherwise the wizard would silently
    # write a machine-wide block into a committed repo file (the footgun the routing exists for).
    # (agents_md is the documented exception: GLOBAL-display is REPO above, but it is a repo file.)
    for area in schema.AREAS:
        if (
            area.category not in scaffolded
            and area.category != "agents_md"
            and layer_for_category(area.category) == schema.GLOBAL
        ):
            assert schema.writable_layer_for_category(area.category) == schema.GLOBAL, area.category


def test_config_set_global_only_leaves_repo_file_untouched(tmp_path, capsys, monkeypatch):
    repo = _make_repo(tmp_path)
    before = (repo / "rig.yaml").read_text()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg4"))
    main(["config", "set", "tg_ctl.boot", "no", "-C", str(repo)])
    assert (repo / "rig.yaml").read_text() == before  # repo file never touched for a global key


def test_config_set_against_absent_global_file_succeeds(tmp_path, capsys, monkeypatch):
    # a new global config must validate + write, and get the canonical version: 1 seeded in.
    repo = _make_repo(tmp_path)
    gdir = tmp_path / "fresh-xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(gdir))
    assert not (gdir / "rig" / "config.yaml").exists()
    rc = main(["config", "set", "gitignore.enabled", "no", "-C", str(repo)])
    assert rc == 0
    import yaml

    written = yaml.safe_load((gdir / "rig" / "config.yaml").read_text())
    assert written["gitignore"]["enabled"] is False
    assert written["version"] == 1  # a brand-new file is canonical


def test_config_set_global_flag_on_already_global_key_is_noop_passthrough(tmp_path, capsys, monkeypatch):
    # --global on an already-global-only key is accepted (a no-op) and still writes the global file.
    repo = _make_repo(tmp_path)
    gdir = tmp_path / "xdg-gg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(gdir))
    rc = main(["config", "set", "tg_ctl.enabled", "no", "--global", "-C", str(repo)])
    assert rc == 0
    import yaml

    assert yaml.safe_load((gdir / "rig" / "config.yaml").read_text())["tg_ctl"]["enabled"] is False


def test_effective_value_with_non_dict_intermediate_returns_default():
    # a malformed config where an intermediate is a scalar must fall back to the default, not raise.
    o = schema.option_for_key("harness.auto_mode")
    assert schema.effective_value(o, {"harness": "oops"}) is True


def test_setup_interactive_path_through_main_applies(tmp_path, monkeypatch, capsys, fake_agent_tools):
    """Drive the REAL `main(["setup"])` interactive branch: it must build a working apply call.

    Forces the TTY branch, feeds scripted answers via a patched input, and lets the genuine
    cmd_apply run against the fake agent-tools catalog + an isolated HOME — proving the apply
    namespace cmd_setup_wizard builds (through the real `apply` subparser) carries every attribute
    cmd_apply reads (the regression the review flagged). Picking `(a)pply` first means a single
    input call, so it can't outrun the script and trip pytest's captured-stdin guard.
    """
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = _make_repo(tmp_path)
    # a self-contained config so the real apply does almost nothing but still runs end to end.
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\n"
        "ci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nharness: {enabled: false}\n"
        "models: {enabled: false}\ngithub: {ruleset: {enabled: false}}\n"
        "agents_md: {enabled: false}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(setup_wizard, "is_interactive", lambda: True)
    # `a` (apply) as the very first answer → exactly one input call, then the real cmd_apply runs.
    answers = iter(["a"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    rc = main(["setup", "-C", str(repo)])
    assert rc == 0  # the real apply ran to completion through the parser-built namespace
    out = capsys.readouterr().out
    assert "Plan:" in out  # cmd_apply printed its plan — proof the engine actually ran


def test_wizard_int_option_prompt_and_write(tmp_path):
    repo = _make_repo(tmp_path)
    answers = iter([_index_of("github.ruleset.required_reviews"), "2", "q", "y"])
    setup_wizard.run_setup(
        repo,
        apply_fn=lambda _root: 0,
        input_fn=lambda _prompt: next(answers),
        out=lambda _s: None,
    )
    import yaml

    data = yaml.safe_load((repo / "rig.yaml").read_text())
    assert data["github"]["ruleset"]["required_reviews"] == 2


def test_wizard_invalid_menu_and_blank_value_branches(tmp_path):
    repo = _make_repo(tmp_path)
    out_lines = []
    # "99" = out-of-range menu; then a real option with a blank value (keep); then quit.
    answers = iter(["99", _index_of("harness.auto_mode"), "", "q"])
    setup_wizard.run_setup(
        repo,
        apply_fn=lambda _root: 0,
        input_fn=lambda _prompt: next(answers),
        out=lambda s: out_lines.append(s),
    )
    joined = "\n".join(out_lines)
    assert "not a valid selection" in joined
    assert "unchanged" in joined


def test_wizard_coerce_error_branch_keeps_config(tmp_path):
    repo = _make_repo(tmp_path)
    before = (repo / "rig.yaml").read_text()
    out_lines = []
    # "maybe" is not a valid bool → coerce raises ValueError → reported, config untouched.
    answers = iter([_index_of("harness.auto_mode"), "maybe", "q"])
    setup_wizard.run_setup(
        repo,
        apply_fn=lambda _root: 0,
        input_fn=lambda _prompt: next(answers),
        out=lambda s: out_lines.append(s),
    )
    assert (repo / "rig.yaml").read_text() == before
    assert any("expected yes/no" in ln for ln in out_lines)


def test_wizard_pending_change_then_decline_apply(tmp_path):
    repo = _make_repo(tmp_path)
    calls = []
    out_lines = []
    # change a value, quit, then DECLINE the apply prompt → saved but not applied.
    answers = iter([_index_of("harness.auto_mode"), "no", "q", "n"])
    rc = setup_wizard.run_setup(
        repo,
        apply_fn=lambda root: (calls.append(root) or 0),
        input_fn=lambda _prompt: next(answers),
        out=lambda s: out_lines.append(s),
    )
    assert rc == 0
    assert calls == []  # declined → no apply
    assert any("run `rig apply` to converge" in ln for ln in out_lines)
    import yaml

    assert yaml.safe_load((repo / "rig.yaml").read_text())["harness"]["auto_mode"] is False


def test_wizard_eof_mid_prompt_exits_cleanly(tmp_path):
    repo = _make_repo(tmp_path)
    out_lines = []

    def _eof(_prompt):
        raise EOFError

    rc = setup_wizard.run_setup(
        repo,
        apply_fn=lambda _root: 0,
        input_fn=_eof,
        out=lambda s: out_lines.append(s),
    )
    assert rc == 0  # a closed terminal exits cleanly, no traceback
    assert any("aborted" in ln for ln in out_lines)


def test_wizard_interrupt_during_apply_is_not_swallowed(tmp_path):
    # the apply call runs OUTSIDE the EOF/KeyboardInterrupt guard: a Ctrl-C mid-apply must
    # propagate (the disk is in an unknown state), not be mislabeled "aborted — no changes".
    repo = _make_repo(tmp_path)
    answers = iter(["a"])

    def _apply(_root):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        setup_wizard.run_setup(
            repo,
            apply_fn=_apply,
            input_fn=lambda _prompt: next(answers),
            out=lambda _s: None,
        )


def test_coerce_enum_branch():
    # the registry has no enum option today; exercise the enum coerce path with a synthetic one.
    o = schema.Option(
        key="skills.mode", category="skills", kind=schema.KIND_ENUM,
        default="a", hint="x", choices=("a", "b"),
    )
    assert schema.coerce(o, "b") == "b"
    with pytest.raises(ValueError):
        schema.coerce(o, "z")


def test_set_path_refuses_to_clobber_a_non_mapping_intermediate():
    # a user's scalar where a mapping is expected must NOT be silently destroyed.
    data = {"harness": "TODO"}
    with pytest.raises(ValueError):
        schema.set_path(data, "harness.auto_mode", False)
    assert data == {"harness": "TODO"}  # untouched
    # a None / absent intermediate IS created (the normal path)
    fresh: dict = {}
    schema.set_path(fresh, "harness.auto_mode", False)
    assert fresh == {"harness": {"auto_mode": False}}


def test_config_set_rejects_non_mapping_intermediate_in_file(tmp_path, capsys):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    (repo / "rig.yaml").write_text("version: 1\nharness: oops\n", encoding="utf-8")
    before = (repo / "rig.yaml").read_text()
    rc = main(["config", "set", "harness.auto_mode", "no", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "error" in out.lower()
    assert (repo / "rig.yaml").read_text() == before  # nothing clobbered


def test_config_set_against_malformed_yaml_fails_closed(tmp_path, capsys):
    from riglib import errors

    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    # a syntactically broken YAML file — set must report cleanly, not dump a traceback or overwrite.
    bad = "version: 1\nharness: {enabled: true\n"  # unbalanced brace
    (repo / "rig.yaml").write_text(bad, encoding="utf-8")
    rc = main(["config", "set", "harness.auto_mode", "no", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == errors.EXIT_CONFIG
    assert "error" in out.lower()
    assert (repo / "rig.yaml").read_text() == bad  # the unparseable file is never overwritten


def test_wizard_against_malformed_yaml_exits_gracefully(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    bad = "version: 1\nharness: {enabled: true\n"  # unbalanced brace → malformed
    (repo / "rig.yaml").write_text(bad, encoding="utf-8")
    out_lines = []
    # the wizard can't render state from an unparseable config — it must exit cleanly (no traceback)
    # with a clear message and the fix, NOT block on a prompt it can never reach.
    rc = setup_wizard.run_setup(
        repo,
        apply_fn=lambda _root: 0,
        input_fn=lambda _prompt: pytest.fail("must not prompt with a malformed config"),
        out=lambda s: out_lines.append(s),
    )
    assert rc == 2
    assert any("cannot read current config" in ln for ln in out_lines)
    assert (repo / "rig.yaml").read_text() == bad  # untouched


def test_config_set_against_non_dict_yaml_fails_closed(tmp_path, capsys):
    # valid YAML but the WRONG SHAPE (a bare list) must not be silently overwritten.
    from riglib import errors

    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    wrong = "- a\n- b\n"
    (repo / "rig.yaml").write_text(wrong, encoding="utf-8")
    rc = main(["config", "set", "harness.auto_mode", "no", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == errors.EXIT_CONFIG
    assert "mapping" in out.lower()
    assert (repo / "rig.yaml").read_text() == wrong  # not destroyed


def test_config_get_against_malformed_yaml_fails_closed(tmp_path, capsys):
    from riglib import errors

    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    (repo / "rig.yaml").write_text("version: 1\nharness: {enabled: true\n", encoding="utf-8")
    rc = main(["config", "get", "harness.auto_mode", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == errors.EXIT_CONFIG
    assert "error" in out.lower()


def test_load_layer_config_empty_file_is_empty_layer(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("", encoding="utf-8")
    assert setup_wizard.load_layer_config(p) == {}
    assert setup_wizard.load_layer_config(tmp_path / "absent.yaml") == {}  # absent → {}


def test_config_set_repo_key_with_no_existing_rig_yaml(tmp_path, capsys):
    import subprocess

    import yaml

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    assert not (repo / "rig.yaml").exists()
    rc = main(["config", "set", "harness.auto_mode", "no", "-C", str(repo)])
    assert rc == 0
    data = yaml.safe_load((repo / "rig.yaml").read_text())
    assert data["harness"]["auto_mode"] is False  # the new file was created + written
    assert data["version"] == 1  # canonical version seeded into the fresh file


def test_config_get_global_only_key_from_non_repo_cwd(tmp_path, capsys, monkeypatch):
    # a global-only key is readable from a plain (non-git) dir — repo_root just falls back to cwd.
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-nr"))
    rc = main(["config", "get", "gitignore.enabled", "-C", str(plain)])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "true"  # the documented default, read without a repo
