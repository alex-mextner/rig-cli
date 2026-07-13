"""Tests for `rig setup` — the interactive config wizard + its schema/option registry.

Exercises production code, not mocks: the real option registry (riglib.schema) and the real
wizard loop (riglib.setup_wizard.run_setup driven by scripted input + an injected apply fn that
calls the real engine, writing real YAML to disk). The autouse `_isolate_home`/`_isolate_scheduler`
fixtures keep HOME/XDG and the scheduler off the real machine.

The user-facing `rig config get|set <dot.path>` CLI is the dot-path editor (read/edit one key by
dotted path, then reconcile) and is covered separately in tests/test_config_getset.py — it is a
DIFFERENT surface from the wizard's schema-key engine tested here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib import config, schema, setup_wizard
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
        "skills", "agent_hooks", "git_hooks", "ci", "mcp", "harness", "permissions",
        "mode", "models", "agents_md", "github", "tmux", "gitignore", "spotlight", "tg_ctl",
        "linters", "project_tools",
    }
    assert cats == expected


def test_global_only_categories_route_to_global_not_repo():
    # the footgun guard: a machine-wide, never-scaffolded block must NOT be writable into a
    # committed repo rig.yaml. These are documented global-only.
    for cat in ("gitignore", "tg_ctl", "tmux", "mode"):
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


def test_coerce_harness_kinds_list():
    o = schema.option_for_key("harness.kinds")
    assert o is not None
    assert o.kind == schema.KIND_LIST
    assert o.layer == schema.REPO
    assert schema.coerce(o, "codex, opencode") == ["codex", "opencode"]
    assert schema.coerce(o, '["codex", "opencode"]') == ["codex", "opencode"]
    assert schema.coerce(o, "") == []
    with pytest.raises(ValueError):
        schema.coerce(o, "[codex, 42]")
    with pytest.raises(ValueError):
        schema.coerce(o, "codex, bogus")
    with pytest.raises(ValueError):
        schema.coerce(o, '["codex", "bogus"]')


def test_effective_value_falls_back_to_default():
    o = schema.option_for_key("skills.enabled")
    assert schema.effective_value(o, {}) is True  # absent → default
    assert schema.effective_value(o, {"skills": {"enabled": False}}) is False


def test_permissions_kind_absent_stays_unpinned_for_harness_fanout():
    o = schema.option_for_key("permissions.kind")
    assert o is not None
    assert o.default is None
    assert schema.effective_value(
        o,
        {"harness": {"kind": "claude-code", "kinds": ["opencode"]}, "permissions": {"enabled": True}},
    ) is None
    assert schema.effective_value(o, {"permissions": {"kind": "opencode"}}) == "opencode"
    assert schema.coerce(o, "") is None
    assert schema.coerce(o, "null") is None


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


def test_github_ruleset_secure_defaults_in_schema():
    """required_conversation_resolution and dismiss_stale_reviews must be in the schema registry.

    Codex P2 finding: these keys were being applied by `rig apply` but were invisible in `rig setup`
    / `rig config-web` because they were missing from schema.py's Option registry. Regression guard.
    """
    conv = schema.option_for_key("github.ruleset.required_conversation_resolution")
    dismiss = schema.option_for_key("github.ruleset.dismiss_stale_reviews")
    assert conv is not None, "required_conversation_resolution missing from schema registry"
    assert dismiss is not None, "dismiss_stale_reviews missing from schema registry"
    # Both should default to True (secure-default-on policy)
    assert conv.default is True
    assert dismiss.default is True


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
    # REPO-writable areas that are default-ON at plan level and genuine repo artifacts, but carry NO
    # scaffolded default content (so the scaffold can't pre-write them): agents_md (a file IN the
    # repo) and linters (config files declared per-repo — there is no sensible default item to seed).
    _repo_unscaffolded_ok = {"agents_md", "linters", "permissions"}
    for area in schema.AREAS:
        if schema.writable_layer_for_category(area.category) == schema.REPO:
            assert area.category in scaffolded or area.category in _repo_unscaffolded_ok, area.category
        else:
            # a GLOBAL-only category must NOT be scaffolded into the committed repo file.
            assert area.category not in scaffolded, area.category
    # REVERSE guard: every registered category the SCAFFOLD does NOT commit into rig.yaml AND that
    # layers.py groups as GLOBAL must be routed GLOBAL-only — otherwise the wizard would silently
    # write a machine-wide block into a committed repo file (the footgun the routing exists for).
    # agents_md is a repo file; permissions is still accepted repo-locally for compatibility even
    # though status displays the reconciled harness settings under GLOBAL.
    for area in schema.AREAS:
        if (
            area.category not in scaffolded
            and area.category not in {"agents_md", "permissions"}
            and layer_for_category(area.category) == schema.GLOBAL
        ):
            assert schema.writable_layer_for_category(area.category) == schema.GLOBAL, area.category


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


def test_coerce_nullable_enum_prefers_real_choice_over_null_token():
    o = schema.Option(
        key="example.mode", category="example", kind=schema.KIND_ENUM,
        default=None, hint="x", choices=("none", "unset", "real"),
    )
    assert schema.coerce(o, "none") == "none"
    assert schema.coerce(o, "unset") == "unset"
    assert schema.coerce(o, "") is None
    assert schema.coerce(o, "~") is None
    with pytest.raises(ValueError):
        schema.coerce(o, "fan-out")


def test_set_path_refuses_to_clobber_a_non_mapping_intermediate():
    # a user's scalar where a mapping is expected must NOT be silently destroyed.
    data = {"harness": "TODO"}
    with pytest.raises(config.ConfigError, match="not a mapping"):
        schema.set_path(data, "harness.auto_mode", False)
    assert data == {"harness": "TODO"}  # untouched
    # an explicit null intermediate is user-authored shape, not an absent path to clobber.
    null_data = {"permissions": None}
    with pytest.raises(config.ConfigError, match="not a mapping"):
        schema.set_path(null_data, "permissions.kind", None)
    assert null_data == {"permissions": None}
    # an absent intermediate IS created (the normal path)
    fresh: dict = {}
    schema.set_path(fresh, "harness.auto_mode", False)
    assert fresh == {"harness": {"auto_mode": False}}


def test_set_path_rejects_empty_segments():
    with pytest.raises(config.ConfigError, match="empty"):
        schema.set_path({}, "", True)
    with pytest.raises(config.ConfigError, match="empty segment"):
        schema.set_path({}, "harness..kind", "codex")
    with pytest.raises(config.ConfigError, match="empty segment"):
        schema.set_path({}, "harness. .kind", "codex")


def test_schema_get_path_strips_segments():
    data = {"harness": {"kind": "codex"}}

    assert schema.get_path(data, " harness . kind ") == "codex"


def test_get_path_rejects_malformed_segments():
    data = {"harness": {"kind": "codex"}}

    with pytest.raises(config.ConfigError, match="empty"):
        schema.get_path(data, "")
    with pytest.raises(config.ConfigError, match="empty segment"):
        schema.get_path(data, "harness..kind")
    with pytest.raises(config.ConfigError, match="empty segment"):
        schema.get_path(data, "harness. .kind")


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


def test_load_layer_config_empty_file_is_empty_layer(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("", encoding="utf-8")
    assert setup_wizard.load_layer_config(p) == {}
    assert setup_wizard.load_layer_config(tmp_path / "absent.yaml") == {}  # absent → {}
