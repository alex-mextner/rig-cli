"""Tests for the ``env`` block — rig-managed shell environment variables.

Mirrors ``tests/test_tmux.py``'s structure at a scale matching the feature's own size (one
generated file + one spliced import line, no dual apply-mode, no inline-content neutralization):
config validation, pure rendering (``riglib.shell_env``), plan building, and the install/drift
round trip through ``riglib.actions.runner``/``riglib.drift``.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from riglib import shell_env
from riglib.config import ConfigError, validate


# ── config validation ───────────────────────────────────────────────────────────────────
def test_env_block_accepted():
    validate({"version": 1, "env": {"enabled": True}})


def test_env_block_empty_ok():
    validate({"version": 1, "env": {}})


def test_env_full_block_accepted():
    validate(
        {
            "version": 1,
            "env": {
                "enabled": True,
                "rc_path": "~/.zshenv",
                "generated_dir": "~/.config/rig/env",
                "vars": {"COLORTERM": "truecolor", "MY_FLAG": "1"},
            },
        }
    )


def test_env_unknown_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "env": {"nope": 1}})


def test_env_enabled_must_be_bool():
    with pytest.raises(ConfigError):
        validate({"version": 1, "env": {"enabled": "yes"}})


@pytest.mark.parametrize("pathkey", ["rc_path", "generated_dir"])
def test_env_path_keys_must_be_string(pathkey):
    with pytest.raises(ConfigError):
        validate({"version": 1, "env": {pathkey: 123}})


@pytest.mark.parametrize("pathkey", ["rc_path", "generated_dir"])
def test_env_path_keys_must_not_be_empty(pathkey):
    """An empty string passes the `isinstance(..., str)` check but resolves to `Path(".")` —
    for `generated_dir` that silently writes rig.env.sh into the resolved repo-root/CWD
    instead of the intended machine-wide location; for `rc_path` it errors at apply. A
    machine-wide GLOBAL artifact should reject this footgun at validate time (review finding)."""
    with pytest.raises(ConfigError):
        validate({"version": 1, "env": {pathkey: ""}})


@pytest.mark.parametrize("pathkey", ["rc_path", "generated_dir"])
@pytest.mark.parametrize("bad", ["~/foo\nbar", "~/foo\rbar"])
def test_env_path_keys_must_not_contain_a_newline(pathkey, bad):
    """A newline breaks the LINE-oriented splice/drift layer's idempotency (not an injection —
    `shlex.quote` keeps it literal — but every `rig apply` would append a fresh, never-matched
    copy forever, since `desired_rc_text`'s `current_present` check compares against a single
    physical line). Same adversarial-config threat model as the single-quote hardening in
    `ShellEnvPlan.import_line` (review finding)."""
    with pytest.raises(ConfigError):
        validate({"version": 1, "env": {pathkey: bad}})


def test_env_vars_must_be_a_mapping():
    with pytest.raises(ConfigError):
        validate({"version": 1, "env": {"vars": ["COLORTERM=truecolor"]}})


def test_env_vars_keys_must_be_strings():
    with pytest.raises(ConfigError):
        validate({"version": 1, "env": {"vars": {1: "truecolor"}}})


def test_env_vars_values_must_be_strings():
    with pytest.raises(ConfigError):
        validate({"version": 1, "env": {"vars": {"COLORTERM": 1}}})


@pytest.mark.parametrize(
    "bad_key",
    [
        "MY VAR",       # space
        "MY;VAR",       # command separator
        "MY=VAR",       # would terminate the export's own assignment early
        "$(rm -rf ~)",  # command substitution
        "1VAR",         # leading digit — not a valid shell identifier
        "MY-VAR",       # hyphen is not a valid identifier character
        "",             # empty (also covered by the non-empty-string check, belt and braces)
    ],
)
def test_env_vars_key_must_be_a_valid_shell_identifier(bad_key):
    """A `vars` key is interpolated UNQUOTED into `export {key}=...` in rig.env.sh, which is
    sourced by every zsh invocation on the machine — an invalid key must be rejected at
    validate time rather than reaching the generated, machine-wide-executed file (review
    finding: shell injection through an unvalidated var key)."""
    with pytest.raises(ConfigError):
        validate({"version": 1, "env": {"vars": {bad_key: "x"}}})


@pytest.mark.parametrize("good_key", ["COLORTERM", "MY_FLAG", "_LEADING_UNDERSCORE", "A1B2"])
def test_env_vars_key_valid_shell_identifiers_accepted(good_key):
    validate({"version": 1, "env": {"vars": {good_key: "x"}}})


# ── pure rendering (riglib.shell_env) ───────────────────────────────────────────────────
def _plan(**over):
    """A ShellEnvPlan with sensible defaults, overridable per-test."""
    return shell_env.build_shell_env(repo_home=Path("/home/u"), **over)


def test_defaults_are_zshenv_and_rig_config_dir():
    plan = _plan()
    assert plan.rc_path == Path("/home/u/.zshenv")
    assert plan.generated_dir == Path("/home/u/.config/rig/env")
    assert plan.generated_file_path == Path("/home/u/.config/rig/env/rig.env.sh")


def test_render_env_file_empty_vars_is_header_only():
    body = _plan(vars={}).render_env_file()
    assert "export" not in body
    assert "rig-managed shell environment" in body


def test_render_env_file_one_var():
    body = _plan(vars={"COLORTERM": "truecolor"}).render_env_file()
    assert "export COLORTERM=truecolor" in body


def test_render_env_file_sorted_by_key_deterministic():
    body = _plan(vars={"ZVAR": "1", "AVAR": "2"}).render_env_file()
    assert body.index("export AVAR=2") < body.index("export ZVAR=1")


def test_render_env_file_shell_quotes_values_with_special_chars():
    body = _plan(vars={"MSG": "hello world"}).render_env_file()
    assert "export MSG='hello world'" in body


def test_render_is_deterministic():
    a = _plan(vars={"COLORTERM": "truecolor"}).render_env_file()
    b = _plan(vars={"COLORTERM": "truecolor"}).render_env_file()
    assert a == b


def test_import_line_shlex_quoted():
    """`shlex.quote` leaves an ordinary path (no shell metacharacters) BARE — no quotes are
    needed for it to be safe, unlike hand-placed single quotes which would always wrap it."""
    line = _plan().import_line()
    assert line == "source /home/u/.config/rig/env/rig.env.sh"


def test_import_line_quotes_a_path_containing_a_space():
    plan = shell_env.build_shell_env(repo_home=Path("/home/u"), generated_dir="/tmp/my dir")
    line = plan.import_line()
    assert line == "source '/tmp/my dir/rig.env.sh'"


def test_import_line_safely_quotes_a_path_containing_a_single_quote():
    """A `generated_dir` containing a single quote — a genuinely adversarial value, and per
    the review's own repo-level-config test this is reachable from a repo's committed
    rig.yaml, not just the operator's global config — must not let a hand-placed quote be
    broken out of. `shlex.quote` (not string-formatted single quotes) makes this safe: the
    quote character is escaped, never terminates the shell string early (review finding)."""
    plan = shell_env.build_shell_env(repo_home=Path("/home/u"), generated_dir="/tmp/x'; rm -rf ~; '")
    line = plan.import_line()
    # the ENTIRE malicious path is a single shlex-safe token — never a bare `;` outside quotes.
    assert shlex.split(line) == ["source", "/tmp/x'; rm -rf ~; '/rig.env.sh"]
    assert "; rm -rf ~;" not in line.split("'", 1)[0]  # nothing escapes before the first quote


def test_home_relative_paths_expand_against_repo_home():
    plan = shell_env.build_shell_env(
        repo_home=Path("/home/u"), rc_path="~/.zshenv", generated_dir="~/.config/rig/env"
    )
    assert plan.rc_path == Path("/home/u/.zshenv")


def test_absolute_override_paths_are_used_verbatim():
    plan = shell_env.build_shell_env(
        repo_home=Path("/home/u"), rc_path="/etc/zshenv", generated_dir="/opt/rig-env"
    )
    assert plan.rc_path == Path("/etc/zshenv")
    assert plan.generated_dir == Path("/opt/rig-env")


# ── desired_rc_text (the idempotent import-line splice) ────────────────────────────────
def test_desired_rc_text_appends_when_absent():
    plan = _plan()
    out = shell_env.desired_rc_text("eval brew shellenv\n", plan)
    assert out == "eval brew shellenv\n" + plan.import_line() + "\n"


def test_desired_rc_text_empty_file():
    plan = _plan()
    assert shell_env.desired_rc_text("", plan) == plan.import_line() + "\n"


def test_desired_rc_text_idempotent():
    plan = _plan()
    once = shell_env.desired_rc_text("eval brew shellenv\n", plan)
    twice = shell_env.desired_rc_text(once, plan)
    assert once == twice


def test_desired_rc_text_preserves_unrelated_lines_verbatim():
    plan = _plan()
    existing = "# a comment\nexport PATH=$HOME/bin:$PATH\n"
    out = shell_env.desired_rc_text(existing, plan)
    assert "# a comment" in out
    assert "export PATH=$HOME/bin:$PATH" in out
    assert out.endswith(plan.import_line() + "\n")


def test_desired_rc_text_drops_stale_import_before_reappending():
    """A previous `generated_dir` leaves a STALE `source '<old path>/rig.env.sh'` line — the
    desired text drops it and re-appends the CURRENT import exactly once (mirrors tmux's
    `_desired_tmux_conf_text` stale-import handling)."""
    plan = _plan(generated_dir="~/.config/rig/env")
    stale = "source '/home/u/.config/rig/OLD/rig.env.sh'\n"
    out = shell_env.desired_rc_text(stale, plan)
    assert out == plan.import_line() + "\n"
    assert out.count("rig.env.sh") == 1


def test_desired_rc_text_comment_mentioning_path_is_not_matched_as_import():
    plan = _plan()
    existing = f"# see {plan.generated_file_path} for details\n"
    out = shell_env.desired_rc_text(existing, plan)
    assert existing.strip() in out
    assert out.count(plan.import_line()) == 1


def test_desired_rc_text_preserves_trailing_blank_lines_on_first_append():
    """A lossy `splitlines()` + `\\n`.join() + `.rstrip("\\n")` round-trip would silently drop
    the user's trailing blank line(s) even though nothing about them needed reconciling
    (review finding)."""
    plan = _plan()
    existing = "export PATH=$HOME/bin:$PATH\n\n\n"  # two trailing blank lines
    out = shell_env.desired_rc_text(existing, plan)
    assert out == existing + plan.import_line() + "\n"


def test_desired_rc_text_preserves_crlf_line_endings_on_first_append():
    """Every OTHER line's original terminator must survive byte-for-byte — a file using CRLF
    must not be silently converted to LF (review finding)."""
    plan = _plan()
    existing = "export PATH=$HOME/bin:$PATH\r\n"
    out = shell_env.desired_rc_text(existing, plan)
    assert out == existing + plan.import_line() + "\n"
    assert "\r\n" in out  # the user's own line kept its CRLF terminator


def test_desired_rc_text_current_line_already_present_is_byte_identical_noop():
    """When the current import line is already present ANYWHERE, `existing` is returned
    completely unchanged — including any trailing blank lines / CRLF elsewhere in the file
    that a rebuild-from-scratch would otherwise risk normalizing away."""
    plan = _plan()
    existing = plan.import_line() + "\r\nexport OTHER=1\r\n\r\n"
    assert shell_env.desired_rc_text(existing, plan) == existing


def test_desired_rc_text_a_duplicated_current_line_is_left_as_is():
    """Two copies of the CURRENT (already-correct) import line are tolerated, not deduped —
    sourcing the same file twice is a functional no-op, and `desired_rc_text` returns
    `existing` verbatim whenever the current line is present at all (documents the deliberate
    design choice: position-tolerance takes priority over exactly-once enforcement once the
    line is already correct anywhere)."""
    plan = _plan()
    line = plan.import_line()
    existing = f"{line}\nexport X=1\n{line}\n"
    assert shell_env.desired_rc_text(existing, plan) == existing


def test_desired_rc_text_stale_only_duplicated_collapses_to_one_current_line():
    plan = _plan(generated_dir="~/.config/rig/env")
    stale = "source '/home/u/.config/rig/OLD/rig.env.sh'\n" * 2
    out = shell_env.desired_rc_text(stale, plan)
    assert out == plan.import_line() + "\n"
    assert out.count("rig.env.sh") == 1


# ── render_env_file: value/key safety ───────────────────────────────────────────────────
def test_render_env_file_value_with_shell_metacharacters_is_inert():
    """The VALUE side is the actual injection surface once keys are identifier-restricted —
    `shlex.quote` must neutralize command substitution, backticks, and embedded single quotes."""
    plan = _plan(vars={"MSG": "$(rm -rf ~) `whoami` it's-fine"})
    body = plan.render_env_file()
    line = next(ln for ln in body.splitlines() if ln.startswith("export MSG="))
    # shlex.quote wraps the whole thing in single quotes and escapes the embedded one; the
    # rendered line is never a bare, shell-interpretable `$(...)`/backtick sequence.
    assert not line.startswith("export MSG=$(")
    assert "'" in line  # shlex.quote's own quoting is present


def test_render_env_file_rejects_an_invalid_key_even_if_it_bypassed_config_validate():
    """Defense in depth: `render_env_file` re-asserts the shell-identifier invariant itself
    rather than trusting every possible caller to have gone through `config.validate` first."""
    plan = shell_env.build_shell_env(repo_home=Path("/home/u"), vars={"1 BAD; KEY": "x"})
    with pytest.raises(ValueError):
        plan.render_env_file()


# ── plan building ────────────────────────────────────────────────────────────────────────
def _cfg(data, repo_root):
    from riglib.config import LoadedConfig

    return LoadedConfig(data=data, repo_root=repo_root)


def _build(data, repo_root, fake_agent_tools):
    from riglib.catalog import Catalog
    from riglib.plan import build

    data = {"agent_tools_source": str(fake_agent_tools), **data}
    cat = Catalog.scan(str(fake_agent_tools))
    return build(_cfg(data, repo_root), cat, project_type="unknown")


def test_plan_no_env_when_absent(fake_agent_tools, tmp_path):
    plan = _build({}, tmp_path, fake_agent_tools)
    assert not [a for a in plan.actions if a.kind == "provision_env"]


def test_plan_no_env_when_disabled(fake_agent_tools, tmp_path):
    plan = _build({"env": {"enabled": False}}, tmp_path, fake_agent_tools)
    assert not [a for a in plan.actions if a.kind == "provision_env"]


def test_plan_emits_env_action_on_empty_block(fake_agent_tools, tmp_path):
    """A present, empty `env: {}` block opts in — mirrors tmux's own "empty block still
    provisions (with safe/empty defaults)" contract."""
    plan = _build({"env": {}}, tmp_path, fake_agent_tools)
    acts = [a for a in plan.actions if a.kind == "provision_env"]
    assert len(acts) == 1
    assert acts[0].category == "env" and acts[0].item == "vars"


def test_plan_carries_vars(fake_agent_tools, tmp_path):
    plan = _build(
        {"env": {"enabled": True, "vars": {"COLORTERM": "truecolor"}}}, tmp_path, fake_agent_tools
    )
    a = [a for a in plan.actions if a.kind == "provision_env"][0]
    assert a.options["vars"] == {"COLORTERM": "truecolor"}


def test_plan_defaults_vars_to_empty_dict_when_absent(fake_agent_tools, tmp_path):
    plan = _build({"env": {"enabled": True}}, tmp_path, fake_agent_tools)
    a = [a for a in plan.actions if a.kind == "provision_env"][0]
    assert a.options["vars"] == {}


def test_env_from_a_repo_level_config_is_honored_same_trust_model_as_tmux(fake_agent_tools, tmp_path):
    """DOCUMENTS the current trust model (a review question, not a gap this feature introduces):
    ``LoadedConfig`` carries one already-MERGED ``data`` dict with no per-key record of which
    layer (global vs a repo's own ``./rig.yaml``) contributed it — the global/repo split is a
    CASCADE at load time and a LABEL for `rig status` display (`riglib.layers`), never an
    enforcement boundary in `config.validate`/`_build_env`. So a repo's own committed
    ``rig.yaml`` CAN declare an ``env:`` block and have it honored by `rig apply` run from that
    repo, exactly like `tmux:`/`gitignore:`/`spotlight:` already can (verified: none of those
    reject a repo-level occurrence either). This is a pre-existing, system-wide characteristic
    of every GLOBAL-labeled block, not something `env` introduces or could unilaterally close
    without doing the same for its siblings — the operator running `rig apply` against a repo
    is trusting that repo's config, the same trust already extended to a Makefile/npm script."""
    plan = _build(
        {"env": {"enabled": True, "vars": {"COLORTERM": "truecolor"}}}, tmp_path, fake_agent_tools
    )
    env_actions = [a for a in plan.actions if a.kind == "provision_env"]
    assert len(env_actions) == 1

    # the SAME is true of tmux, its closest sibling in shape — pinning the parity explicitly.
    tmux_plan = _build({"tmux": {"enabled": True}}, tmp_path, fake_agent_tools)
    assert [a for a in tmux_plan.actions if a.kind == "provision_tmux"]


# ── install (runner) + drift — real filesystem, isolated $HOME ─────────────────────────
def test_apply_writes_generated_file_and_splices_import(tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshenv").write_text("eval brew shellenv\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    result = runner._do_provision_env(action, "backup")
    assert result.status == "updated"

    generated = home / ".config" / "rig" / "env" / "rig.env.sh"
    assert generated.is_file()
    assert "export COLORTERM=truecolor" in generated.read_text(encoding="utf-8")

    rc_text = (home / ".zshenv").read_text(encoding="utf-8")
    assert "eval brew shellenv" in rc_text  # the user's own line is untouched
    plan = runner.env_plan_from_action(action)
    assert plan.import_line() in rc_text


def test_apply_is_idempotent(tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    first = runner._do_provision_env(action, "backup")
    second = runner._do_provision_env(action, "backup")
    assert first.status == "updated"
    assert second.status == "skipped"
    # pin the DETAIL text too, not just status: the idempotent-skip vs conflict-skip split is
    # decided by sniffing `WriteOutcome.detail`'s "identical" prefix (review finding) — if that
    # wording ever changed, an ORDINARY re-apply could start emitting the misleading
    # conflict-skip "NOT regenerated" text while still reporting `skipped`, silently.
    assert second.detail == "env: already current"


def test_apply_updates_generated_file_when_vars_change(tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    runner._do_provision_env(action, "backup")
    changed = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor", "OTHER": "1"}},
    )
    result = runner._do_provision_env(changed, "backup")
    assert result.status == "updated"
    generated = home / ".config" / "rig" / "env" / "rig.env.sh"
    assert "export OTHER=1" in generated.read_text(encoding="utf-8")


def test_apply_on_conflict_skip_leaves_a_hand_edited_generated_file_untouched(tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    runner._do_provision_env(action, "backup")
    generated = home / ".config" / "rig" / "env" / "rig.env.sh"
    generated.write_text("export HAND_EDITED=1\n", encoding="utf-8")

    result = runner._do_provision_env(action, "skip")
    assert generated.read_text(encoding="utf-8") == "export HAND_EDITED=1\n"
    # the import line is already correct (from the prior apply) and stays untouched either
    # way (position-tolerant), so the generated-file conflict is the ONLY thing `skip` left
    # unresolved — nothing was WRITTEN this run, so the precise status is `skipped`, and the
    # detail text must say so explicitly rather than the misleading "already current" a bare
    # `changed`-only check would have produced (review finding).
    assert result.status == "skipped"
    assert "on_conflict=skip" in result.detail
    assert result.detail != "env: already current"


def test_apply_on_conflict_skip_surfaces_unresolved_splice_not_a_silent_updated(tmp_path, monkeypatch):
    """A FRESH `rc_path` (real pre-existing content, never touched by rig before) under
    `on_conflict=skip`: the generated file gets CREATED (no prior conflict there) while the
    splice into `rc_path` is skipped — that combination must not read as a plain, fully
    successful `updated`; the still-unresolved splice must be visible in the detail text
    (review finding: the far weaker `status in ("skipped", "updated")` assertion on the
    hand-edited-generated-file test above left this exact ambiguity untested)."""
    from riglib.actions import runner
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshenv").write_text("export MY_OWN_VAR=1\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )

    result = runner._do_provision_env(action, "skip")
    generated = home / ".config" / "rig" / "env" / "rig.env.sh"
    assert generated.is_file()  # the generated file WAS created — no conflict there
    rc_text = (home / ".zshenv").read_text(encoding="utf-8")
    assert rc_text == "export MY_OWN_VAR=1\n"  # the splice did NOT happen
    assert "NOT added" in result.detail  # the unresolved splice is visible, not silently lost
    assert result.status == "updated"  # the generated file DID change this run


def test_apply_on_conflict_overwrite_replaces_a_hand_edited_generated_file(tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    runner._do_provision_env(action, "backup")
    generated = home / ".config" / "rig" / "env" / "rig.env.sh"
    generated.write_text("export HAND_EDITED=1\n", encoding="utf-8")

    result = runner._do_provision_env(action, "overwrite")
    assert result.status == "updated"
    body = generated.read_text(encoding="utf-8")
    assert "export COLORTERM=truecolor" in body
    assert "HAND_EDITED" not in body


def test_drift_missing_when_nothing_applied(tmp_path, monkeypatch):
    from riglib.drift import _check_env, DriftReport
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    report = DriftReport()
    _check_env(action, report)
    directions = {(d.direction, d.category) for d in report.items}
    assert ("missing", "env") in directions
    assert len([d for d in report.items if d.category == "env"]) == 2  # file + import line


def test_drift_clean_after_apply(tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.drift import _check_env, DriftReport
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    runner._do_provision_env(action, "backup")
    report = DriftReport()
    _check_env(action, report)
    assert not [d for d in report.items if d.category == "env"]


def test_drift_modified_when_generated_file_hand_edited(tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.drift import _check_env, DriftReport
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    runner._do_provision_env(action, "backup")
    generated = home / ".config" / "rig" / "env" / "rig.env.sh"
    generated.write_text("export HAND_EDITED=1\n", encoding="utf-8")
    report = DriftReport()
    _check_env(action, report)
    modified = [d for d in report.items if d.category == "env" and d.direction == "modified"]
    assert len(modified) == 1


def test_drift_missing_import_line_when_rc_path_lost_it(tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.drift import _check_env, DriftReport
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    runner._do_provision_env(action, "backup")
    (home / ".zshenv").write_text("# the import line got removed\n", encoding="utf-8")
    report = DriftReport()
    _check_env(action, report)
    missing = [d for d in report.items if d.category == "env" and d.direction == "missing"]
    assert len(missing) == 1


def test_drift_stays_clean_when_user_relocated_the_import_line(tmp_path, monkeypatch):
    """`env` is deliberately POSITION-TOLERANT (unlike tmux): a user who moves rig's still
    byte-identical import line above their own exports (so their values win) is genuinely IN
    SYNC. Neither drift nor a re-apply should move it back — both must agree on that (review
    finding: an earlier end-anchoring version disagreed with a looser "present anywhere" drift
    check; the fix taken here is to make apply itself position-tolerant instead, so the two
    share one predicate by construction rather than by careful separate maintenance)."""
    from riglib.actions import runner
    from riglib.drift import _check_env, DriftReport
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    runner._do_provision_env(action, "backup")
    plan = runner.env_plan_from_action(action)
    relocated = plan.import_line() + "\nexport MY_OWN_VAR=1\n"
    (home / ".zshenv").write_text(relocated, encoding="utf-8")

    report = DriftReport()
    _check_env(action, report)
    assert not [d for d in report.items if d.category == "env"]

    # and apply agrees: re-applying is a true no-op, the relocated line stays exactly where the
    # user put it.
    result = runner._do_provision_env(action, "skip")
    assert result.status == "skipped"
    assert (home / ".zshenv").read_text(encoding="utf-8") == relocated


def test_apply_drops_a_stale_import_even_when_the_current_line_is_also_present(tmp_path, monkeypatch):
    """Position-tolerance for the CURRENT line must not become an excuse to leave an orphaned
    STALE `rig.env.sh` (an old `generated_dir`) silently sourced forever — the two must not
    coexist (review finding)."""
    from riglib.actions import runner
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    plan = runner.env_plan_from_action(action)
    stale = "source '/home/u/.config/rig/OLD/rig.env.sh'\n"
    both = stale + plan.import_line() + "\n"
    (home / ".zshenv").write_text(both, encoding="utf-8")

    result = runner._do_provision_env(action, "backup")
    assert result.status == "updated"
    rc_text = (home / ".zshenv").read_text(encoding="utf-8")
    assert "OLD/rig.env.sh" not in rc_text
    assert rc_text.count(plan.import_line()) == 1


def test_drift_flags_a_stale_import_coexisting_with_the_current_one(tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.drift import _check_env, DriftReport
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    plan = runner.env_plan_from_action(action)
    stale = "source '/home/u/.config/rig/OLD/rig.env.sh'\n"
    (home / ".zshenv").write_text(stale + plan.import_line() + "\n", encoding="utf-8")
    # the generated rig.env.sh was never applied in this test -- that's a SEPARATE, expected
    # "missing" item; only the rc_path item is what this test is pinning.
    plan.generated_dir.mkdir(parents=True, exist_ok=True)
    plan.generated_file_path.write_text(plan.render_env_file(), encoding="utf-8")

    report = DriftReport()
    _check_env(action, report)
    rc_items = [d for d in report.items if d.category == "env" and d.target == plan.rc_path]
    assert len(rc_items) == 1
    # the current line IS present -- reported as `modified` (needs reconciling), never the
    # misleading `missing` a naive predicate would have said (review finding).
    assert rc_items[0].direction == "modified"


def test_env_plan_from_action_requires_rc_path_and_generated_dir(tmp_path, monkeypatch):
    """UNLIKE tmux's `tmux_plan_from_action` (which defaults a genuinely pre-dated option),
    `env_plan_from_action` REQUIRES `rc_path`/`generated_dir` in `action.options` — `env` is a
    brand-new action kind with no real "persisted by an older rig" scenario to serve, and
    `_build_env` (`riglib/plan.py`) always writes both keys, so a caller reaching this WITHOUT
    them is a bug, not a legitimate replay. Failing loudly (`KeyError`) beats a bare-default
    fallback silently resolving differently than the plan builder would have (review finding,
    discovered via exactly that divergence — see `riglib/shell_env.py`'s `build_shell_env`
    docstring for the fuller story)."""
    from riglib.actions import runner
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"vars": {"COLORTERM": "truecolor"}},  # no "rc_path"/"generated_dir" at all
    )
    with pytest.raises(KeyError):
        runner.env_plan_from_action(action)


def test_env_plan_from_action_resolves_exactly_what_options_carry(tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={
            "rc_path": str(home / ".zshenv"),
            "generated_dir": str(home / ".config" / "rig" / "env"),
            "vars": {"COLORTERM": "truecolor"},
        },
    )
    plan = runner.env_plan_from_action(action)
    assert plan.rc_path == home / ".zshenv"
    assert plan.generated_dir == home / ".config" / "rig" / "env"

    result = runner._do_provision_env(action, "backup")
    assert result.status == "updated"
    assert (home / ".zshenv").is_file()
    assert (home / ".config" / "rig" / "env" / "rig.env.sh").is_file()


def test_apply_reports_error_not_exception_on_non_utf8_rc_path(tmp_path, monkeypatch):
    """The docstring's stated `(OSError, UnicodeDecodeError)` catch on the `rc_path` read must
    actually fire for the failure mode it names — a genuinely non-UTF-8 file — not just the
    `NotADirectoryError` case already covered."""
    from riglib.actions import runner
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    (home / ".zshenv").write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    result = runner._do_provision_env(action, "backup")
    assert result.status == "error"


def test_drift_reports_modified_not_exception_on_non_utf8_rc_path(tmp_path, monkeypatch):
    from riglib.drift import _check_env, DriftReport
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    (home / ".zshenv").write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    report = DriftReport()
    _check_env(action, report)  # must not raise
    assert [d for d in report.items if d.category == "env"]


def test_drift_still_checks_rc_path_when_generated_file_cannot_be_rendered(tmp_path, monkeypatch):
    """A render failure (an invalid var key that somehow bypassed `config.validate` and
    reached a persisted Action directly) must not ALSO silently skip the rc_path check — that
    check doesn't depend on the render at all (review finding: an earlier version `return`ed
    immediately on the render `ValueError`, so a genuinely missing/stale import line went
    unreported alongside the render-failure item)."""
    from riglib.drift import _check_env, DriftReport
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={
            "rc_path": str(home / ".zshenv"),
            "generated_dir": str(home / ".config" / "rig" / "env"),
            "vars": {"1 BAD; KEY": "x"},  # invalid identifier — bypasses config.validate here
        },
    )
    report = DriftReport()
    _check_env(action, report)  # must not raise
    targets = {d.target for d in report.items if d.category == "env"}
    assert home / ".config" / "rig" / "env" / "rig.env.sh" in targets  # the render-failure item
    assert home / ".zshenv" in targets  # the rc_path check STILL ran (import line is missing)


def test_drift_dispatches_through_detect(tmp_path, monkeypatch):
    """The `elif action.kind == "provision_env"` wiring in `drift.detect()` itself is exercised
    (not just a direct `_check_env` call) — a typo'd/missing dispatch entry would silently drop
    every env drift item from `rig status`."""
    from riglib import drift as drift_mod
    from riglib.actions import runner
    from riglib.plan import Action, InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    report = drift_mod.detect(InstallPlan(actions=[action]))
    assert any(d.category == "env" for d in report.items)

    runner._do_provision_env(action, "backup")
    clean_report = drift_mod.detect(InstallPlan(actions=[action]))
    assert not [d for d in clean_report.items if d.category == "env"]


def test_check_disabled_env_flags_a_leftover_generated_file(tmp_path, monkeypatch):
    """`apply` never deletes — so turning `env.enabled` to `false` after a prior apply leaves
    `rig.env.sh` (and its live source line) fully active. With no `provision_env` action in
    the plan, `_check_env` never runs; `check_disabled_env` is the separate scan that catches
    this specific leftover (mirrors `check_disabled_global_excludes`)."""
    from riglib.actions import runner
    from riglib.drift import check_disabled_env, DriftReport
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    runner._do_provision_env(action, "backup")  # simulate a prior apply while enabled

    report = DriftReport()
    check_disabled_env(action, report)
    # BOTH halves are still live (the generated file AND rc_path's source line) -- checked
    # independently, so both are reported (review finding: an earlier version checked only the
    # generated file).
    items = [d for d in report.items if d.category == "env"]
    assert len(items) == 2
    assert {d.direction for d in items} == {"extra"}
    assert {d.target for d in items} == {
        (home / ".config" / "rig" / "env" / "rig.env.sh"),
        (home / ".zshenv"),
    }


def test_check_disabled_env_flags_the_inverse_orphan_deleted_generated_file(tmp_path, monkeypatch):
    """The INVERSE orphan (review finding): the generated file was deleted by hand, but a
    stale `source` line survives in `rc_path` — every zsh invocation on the machine then
    errors at startup ('no such file or directory'). This must be caught even though the
    generated file itself is gone (the OTHER half of the pair is fine)."""
    from riglib.actions import runner
    from riglib.drift import check_disabled_env, DriftReport
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    runner._do_provision_env(action, "backup")
    (home / ".config" / "rig" / "env" / "rig.env.sh").unlink()  # hand-deleted; source line stays

    report = DriftReport()
    check_disabled_env(action, report)
    items = [d for d in report.items if d.category == "env"]
    assert len(items) == 1
    assert items[0].target == home / ".zshenv"
    assert "GONE" in items[0].detail


def test_check_disabled_env_reports_no_exception_on_non_utf8_rc_path(tmp_path, monkeypatch):
    """`check_disabled_env`'s rc_path read must not crash `rig status` any more than
    `_do_provision_env`'s/`_check_env`'s reads do (review finding: this fourth read had been
    missed by the earlier hardening pass)."""
    from riglib.actions import runner
    from riglib.drift import check_disabled_env, DriftReport
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    runner._do_provision_env(action, "backup")
    (home / ".zshenv").write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")

    report = DriftReport()
    check_disabled_env(action, report)  # must not raise
    # the generated-file half still reports independently (its own read is unaffected).
    assert any(d.target == home / ".config" / "rig" / "env" / "rig.env.sh" for d in report.items)


def test_check_disabled_env_silent_when_nothing_was_ever_installed(tmp_path, monkeypatch):
    from riglib.drift import check_disabled_env, DriftReport
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=home / ".zshenv",
        options={"rc_path": str(home / ".zshenv"), "generated_dir": str(home / ".config" / "rig" / "env"), "vars": {"COLORTERM": "truecolor"}},
    )
    report = DriftReport()
    check_disabled_env(action, report)
    assert not [d for d in report.items if d.category == "env"]


def test_apply_reports_error_not_exception_when_rc_path_write_fails(tmp_path, monkeypatch):
    """`_do_provision_env` must convert an `OSError` writing `rc_path` into an `ActionResult`
    error, not let it propagate raw — `rc_path` is the user's OWN, possibly irreplaceable file
    (review finding: it previously had no error-handling parity with the generated file)."""
    from riglib.actions import runner
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # rc_path's PARENT is a FILE, not a dir — `.parent.mkdir()` / `.write_text()` both raise
    # NotADirectoryError (an OSError subclass) rather than silently succeeding.
    blocker = home / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    action = Action(
        kind="provision_env", category="env", item="vars",
        source=tmp_path, target=blocker / "zshenv",
        options={
            "rc_path": str(blocker / "zshenv"),
            "generated_dir": str(home / ".config" / "rig" / "env"),
            "vars": {"COLORTERM": "truecolor"},
        },
    )
    result = runner._do_provision_env(action, "backup")
    assert result.status == "error"
    assert "blocker" in result.detail


def test_plan_to_apply_to_drift_round_trip(fake_agent_tools, tmp_path, monkeypatch):
    """rig.yaml -> plan action -> runner install -> drift check, end to end.

    ``_build_env`` (plan.py) resolves the default ``~/.zshenv`` via ``os.path.expanduser`` (the
    ``HOME`` env var), but ``~/.config/rig/env`` goes through ``expand_user_path``'s ``~/.config``
    -> ``$XDG_CONFIG_HOME`` special case — while the runner/drift side (``env_plan_from_action``)
    resolves via ``Path.home()``. All three must point at the SAME dir for this real-file-I/O
    test, so set them explicitly rather than relying on the autouse ``_isolate_home`` fixture's
    own private tmp dir (which sets ``HOME``/``XDG_CONFIG_HOME`` to a DIFFERENT throwaway dir than
    the one this test wants to assert against).
    """
    from riglib.actions import runner
    from riglib.drift import _check_env, DriftReport

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    plan = _build({"env": {"enabled": True, "vars": {"COLORTERM": "truecolor"}}}, tmp_path, fake_agent_tools)
    a = next(act for act in plan.actions if act.kind == "provision_env")
    result = runner._do_provision_env(a, "backup")
    assert result.status == "updated"

    report = DriftReport()
    _check_env(a, report)
    assert not [d for d in report.items if d.category == "env"]

    generated = home / ".config" / "rig" / "env" / "rig.env.sh"
    assert "export COLORTERM=truecolor" in generated.read_text(encoding="utf-8")
