"""internal-dev daemon auto-reload — config, pure hook rendering, plan, install, drift.

The ``internal_dev`` block wires a repo's commit to a graceful daemon reload: an opt-in,
per-repo (committed ``rig.yaml``) concern that installs a ``post-commit`` git hook. When a commit
touches the configured daemon-source paths, the hook runs the reload command (``tg-ctl restart``).

These tests are HOME-isolated and NEVER fire a real reload nor touch the real global hooks dir:
the hook + runner both honor ``RIG_DEV_RELOAD_DRY_RUN``, and the end-to-end hook test injects a
FAKE ``tg-ctl`` onto PATH and asserts against a sentinel file it writes — no real daemon involved.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from riglib import config as configmod
from riglib import dev_reload
from riglib import drift as driftmod
from riglib.actions import runner
from riglib.config import ConfigError, LoadedConfig, validate
from riglib.plan import Action, InstallPlan, _build_internal_dev


# ── config validation ────────────────────────────────────────────────────────────────────
def test_internal_dev_block_accepted():
    validate(
        {
            "version": 1,
            "internal_dev": {"auto_reload_on_commit": True, "daemon_source_paths": ["src/*"]},
        }
    )


def test_internal_dev_block_empty_ok():
    validate({"version": 1, "internal_dev": {}})


def test_internal_dev_full_block_accepted():
    validate(
        {
            "version": 1,
            "internal_dev": {
                "auto_reload_on_commit": True,
                "daemon_source_paths": ["src/daemon/*", "bin/tg-ctl"],
                "reload_command": "tg-ctl restart",
            },
        }
    )


def test_internal_dev_unknown_key_rejected():
    with pytest.raises(ConfigError) as exc:
        validate({"version": 1, "internal_dev": {"auto_relaod": True}})
    assert "internal_dev" in str(exc.value.schema_path)


def test_internal_dev_bad_bool_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "internal_dev": {"auto_reload_on_commit": "yes"}})


def test_internal_dev_bad_paths_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "internal_dev": {"daemon_source_paths": "src/daemon"}})


def test_internal_dev_bad_command_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "internal_dev": {"reload_command": ["tg-ctl", "restart"]}})


def test_internal_dev_enabled_without_paths_rejected():
    # a review-caught gap: enabling with no daemon_source_paths installs a hook that can never
    # match anything — fail closed instead of shipping a silently-dead config.
    with pytest.raises(ConfigError) as exc:
        validate({"version": 1, "internal_dev": {"auto_reload_on_commit": True}})
    assert "daemon_source_paths" in str(exc.value.schema_path)
    with pytest.raises(ConfigError):
        validate(
            {
                "version": 1,
                "internal_dev": {"auto_reload_on_commit": True, "daemon_source_paths": []},
            }
        )


def test_internal_dev_enabled_with_only_blank_paths_rejected():
    # review-caught: dev_reload.build_dev_reload() strips whitespace-only entries to nothing,
    # so a list of blank strings passed the plain non-empty-list check yet still installed the
    # same dead hook the empty-list guard exists to prevent.
    with pytest.raises(ConfigError) as exc:
        validate(
            {
                "version": 1,
                "internal_dev": {"auto_reload_on_commit": True, "daemon_source_paths": ["   ", ""]},
            }
        )
    assert "daemon_source_paths" in str(exc.value.schema_path)


def test_internal_dev_forbidden_in_global_layer(tmp_path, monkeypatch):
    # review-caught: internal_dev is documented + schema-registered as REPO-only, but nothing
    # enforced that at the raw config-load level — a global block would silently cascade into
    # every repo's apply. Mirrors the existing `mode`-in-repo-layer guard, opposite direction.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    global_path = configmod.global_config_path()
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(
        "version: 1\ninternal_dev: {auto_reload_on_commit: true, daemon_source_paths: [src/*]}\n",
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    with pytest.raises(ConfigError) as exc:
        configmod.load(repo)
    assert exc.value.schema_path == "internal_dev"
    assert "global-only" in exc.value.what or "repo-only" in exc.value.what


def test_internal_dev_empty_block_allowed_in_global_layer(tmp_path, monkeypatch):
    # review-caught: the global-layer guard must key off CONTENT, not mere key presence — an
    # empty/inert `internal_dev: {}` is explicitly a valid no-op (test_internal_dev_block_empty_ok)
    # and must not be rejected just because the key exists in the global file.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    global_path = configmod.global_config_path()
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text("version: 1\ninternal_dev: {}\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    configmod.load(repo)  # must not raise


# ── pure hook rendering ──────────────────────────────────────────────────────────────────
def test_render_hook_embeds_paths_and_command(tmp_path):
    plan = dev_reload.build_dev_reload(
        repo_root=tmp_path,
        daemon_source_paths=["src/daemon/*", "bin/tg-ctl"],
        reload_command="tg-ctl restart",
    )
    hook = plan.render_hook()
    assert dev_reload.HOOK_MARKER in hook
    assert "src/daemon/*" in hook
    assert "bin/tg-ctl" in hook
    assert "tg-ctl restart" in hook
    assert dev_reload.DRY_RUN_ENV in hook


def test_render_hook_is_valid_posix_sh(tmp_path):
    plan = dev_reload.build_dev_reload(
        repo_root=tmp_path, daemon_source_paths=["src/daemon/*"], reload_command="tg-ctl restart"
    )
    script = tmp_path / "post-commit"
    script.write_text(plan.render_hook(), encoding="utf-8")
    # `sh -n` parses without executing — a syntax error fails here.
    subprocess.run(["sh", "-n", str(script)], check=True)


def test_render_hook_default_reload_command(tmp_path):
    plan = dev_reload.build_dev_reload(repo_root=tmp_path, daemon_source_paths=["x"])
    assert plan.reload_command == dev_reload.DEFAULT_RELOAD_COMMAND
    assert dev_reload.DEFAULT_RELOAD_COMMAND in plan.render_hook()


def test_render_composer_is_valid_posix_sh(tmp_path):
    script = tmp_path / "post-commit-composer"
    script.write_text(dev_reload.render_post_commit_composer(), encoding="utf-8")
    subprocess.run(["sh", "-n", str(script)], check=True)
    assert dev_reload.COMPOSER_MARKER in script.read_text(encoding="utf-8")


# ── plan builder ─────────────────────────────────────────────────────────────────────────
def _cfg(data, repo_root):
    return LoadedConfig(data=data, repo_root=repo_root)


def test_plan_emits_action_only_when_enabled(tmp_path):
    plan = InstallPlan()
    _build_internal_dev(_cfg({}, tmp_path), plan)  # absent → no action
    assert not plan.actions

    plan = InstallPlan()
    _build_internal_dev(_cfg({"internal_dev": {}}, tmp_path), plan)  # present-but-empty → OFF
    assert not plan.actions

    plan = InstallPlan()
    _build_internal_dev(
        _cfg({"internal_dev": {"auto_reload_on_commit": False}}, tmp_path), plan
    )  # disabled → no action
    assert not plan.actions

    plan = InstallPlan()
    _build_internal_dev(
        _cfg(
            {"internal_dev": {"auto_reload_on_commit": True, "daemon_source_paths": ["src/*"]}},
            tmp_path,
        ),
        plan,
    )
    assert len(plan.actions) == 1
    act = plan.actions[0]
    assert act.kind == "install_dev_reload_hook"
    assert act.category == "internal_dev"
    assert act.options["daemon_source_paths"] == ["src/*"]
    assert act.options["reload_command"] == dev_reload.DEFAULT_RELOAD_COMMAND


# ── runner: install / idempotency / conflict / dry-run ───────────────────────────────────
def _git_init(repo: Path, monkeypatch=None) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    if monkeypatch is not None:
        # Review-caught: `effective_hooks_path()` runs plain `git config --get core.hooksPath`,
        # which layers in GLOBAL/system config too. On a machine that has the rig global-hook
        # dispatcher installed (core.hooksPath machine-wide) — exactly the environment this
        # feature targets — a test with the dry-run guard disabled could otherwise write into
        # the developer's REAL global hooks dir. The suite-wide HOME/XDG isolation fixture
        # happens to neutralize this today, but that protection is incidental to an unrelated
        # fixture; make each test process's git blind to any external config so it can't depend
        # on fixture ordering. (NOT `git config core.hooksPath ""` locally — git treats an empty
        # hooksPath as a REAL, resolvable-to-nothing path rather than "unset", which broke the
        # hook it was meant to protect.) These env vars propagate to every git subprocess this
        # test's process spawns, including the hook script git itself invokes on commit.
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


def _action(repo: Path, *, paths=("src/daemon/*",), cmd="tg-ctl restart") -> Action:
    return Action(
        kind="install_dev_reload_hook",
        category="internal_dev",
        item="post-commit",
        source=repo,
        target=repo / ".git" / "hooks" / "post-commit",
        options={"daemon_source_paths": list(paths), "reload_command": cmd},
    )


def test_runner_writes_executable_hook(tmp_path, monkeypatch):
    monkeypatch.setenv(dev_reload.DRY_RUN_ENV, "1")
    repo = tmp_path / "repo"
    _git_init(repo, monkeypatch)
    res = runner._do_install_dev_reload_hook(_action(repo), "backup")
    assert res.status == "created"
    hook = repo / ".git" / "hooks" / "post-commit"
    assert hook.is_file()
    assert os.access(hook, os.X_OK)
    assert dev_reload.HOOK_MARKER in hook.read_text(encoding="utf-8")


def test_runner_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv(dev_reload.DRY_RUN_ENV, "1")
    repo = tmp_path / "repo"
    _git_init(repo, monkeypatch)
    runner._do_install_dev_reload_hook(_action(repo), "backup")
    res = runner._do_install_dev_reload_hook(_action(repo), "backup")
    assert res.status == "skipped"


def test_runner_backs_up_conflicting_hook(tmp_path, monkeypatch):
    monkeypatch.setenv(dev_reload.DRY_RUN_ENV, "1")
    repo = tmp_path / "repo"
    _git_init(repo, monkeypatch)
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text("#!/bin/sh\necho hand-written\n", encoding="utf-8")
    res = runner._do_install_dev_reload_hook(_action(repo), "backup")
    assert res.status == "backed_up"
    assert res.backup is not None and res.backup.is_file()
    assert "hand-written" in res.backup.read_text(encoding="utf-8")
    assert dev_reload.HOOK_MARKER in hook.read_text(encoding="utf-8")


def test_runner_dry_run_skips_global_composer(tmp_path, monkeypatch):
    """Under the dry-run gate the repo-local hook IS written, the machine-global composer is NOT."""
    monkeypatch.setenv(dev_reload.DRY_RUN_ENV, "1")
    repo = tmp_path / "repo"
    _git_init(repo, monkeypatch)
    # wire a composer core.hooksPath with the dispatcher layout (sibling run-global-hooks).
    composer_dir = tmp_path / "gitconfig" / "hooks"
    composer_dir.mkdir(parents=True)
    (composer_dir.parent / "run-global-hooks").write_text("#!/bin/sh\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(composer_dir)], check=True)

    runner._do_install_dev_reload_hook(_action(repo), "backup")
    assert (repo / ".git" / "hooks" / "post-commit").is_file()  # managed artifact written
    assert not (composer_dir / "post-commit").exists()  # live mutation skipped under dry-run


def test_runner_writes_composer_when_active(tmp_path, monkeypatch):
    monkeypatch.delenv(dev_reload.DRY_RUN_ENV, raising=False)
    repo = tmp_path / "repo"
    _git_init(repo, monkeypatch)
    composer_dir = tmp_path / "gitconfig" / "hooks"
    composer_dir.mkdir(parents=True)
    (composer_dir.parent / "run-global-hooks").write_text("#!/bin/sh\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(composer_dir)], check=True)

    runner._do_install_dev_reload_hook(_action(repo), "backup")
    composer = composer_dir / "post-commit"
    assert composer.is_file()
    assert os.access(composer, os.X_OK)
    assert dev_reload.COMPOSER_MARKER in composer.read_text(encoding="utf-8")


def test_runner_surfaces_composer_write_in_result(tmp_path, monkeypatch):
    """Regression: the composer write's status/backup used to be discarded — a composer-only
    change (repo-local hook already correct, composer newly written) reported `skipped`, hiding
    the one live/global mutation this action makes."""
    monkeypatch.delenv(dev_reload.DRY_RUN_ENV, raising=False)
    repo = tmp_path / "repo"
    _git_init(repo, monkeypatch)
    composer_dir = tmp_path / "gitconfig" / "hooks"
    composer_dir.mkdir(parents=True)
    (composer_dir.parent / "run-global-hooks").write_text("#!/bin/sh\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(composer_dir)], check=True)

    first = runner._do_install_dev_reload_hook(_action(repo), "backup")
    assert first.status == "created"
    assert "composer" in first.detail

    # a hand-written composer the runner must back up on the next apply.
    (composer_dir / "post-commit").write_text("#!/bin/sh\necho hand-written\n", encoding="utf-8")
    second = runner._do_install_dev_reload_hook(_action(repo), "backup")
    # the repo-local hook is unchanged (skipped) but the composer was just backed up — the
    # overall result must surface that, not silently report "skipped".
    assert second.status == "backed_up"
    assert second.backup is not None
    assert "hand-written" in second.backup.read_text(encoding="utf-8")


def test_runner_no_composer_for_raw_hooks_repo(tmp_path, monkeypatch):
    """A repo without a core.hooksPath composer needs no trampoline — none is written."""
    monkeypatch.delenv(dev_reload.DRY_RUN_ENV, raising=False)
    repo = tmp_path / "repo"
    _git_init(repo, monkeypatch)
    res = runner._do_install_dev_reload_hook(_action(repo), "backup")
    assert res.status == "created"
    # nothing was created outside the repo's own .git/hooks
    assert (repo / ".git" / "hooks" / "post-commit").is_file()


# ── drift ────────────────────────────────────────────────────────────────────────────────
def test_drift_missing_when_hook_absent(tmp_path):
    repo = tmp_path / "repo"
    _git_init(repo)
    report = driftmod.DriftReport()
    driftmod._check_internal_dev(_action(repo), report)
    assert any(i.direction == "missing" and i.category == "internal_dev" for i in report.items)


def test_drift_modified_when_hook_differs(tmp_path, monkeypatch):
    monkeypatch.setenv(dev_reload.DRY_RUN_ENV, "1")
    repo = tmp_path / "repo"
    _git_init(repo, monkeypatch)
    (repo / ".git" / "hooks" / "post-commit").write_text("#!/bin/sh\necho other\n", encoding="utf-8")
    report = driftmod.DriftReport()
    driftmod._check_internal_dev(_action(repo), report)
    assert any(i.direction == "modified" and i.category == "internal_dev" for i in report.items)


def test_drift_clean_when_hook_current(tmp_path, monkeypatch):
    monkeypatch.setenv(dev_reload.DRY_RUN_ENV, "1")
    repo = tmp_path / "repo"
    _git_init(repo, monkeypatch)
    runner._do_install_dev_reload_hook(_action(repo), "backup")
    report = driftmod.DriftReport()
    driftmod._check_internal_dev(_action(repo), report)
    assert not [i for i in report.items if i.category == "internal_dev"]


# ── schema registry (wizard + config_web hints) ──────────────────────────────────────────
def test_schema_exposes_internal_dev_options_with_hints():
    from riglib import schema

    area = schema.area_for_category("internal_dev")
    assert area is not None
    keys = {o.key for o in area.options}
    assert "internal_dev.auto_reload_on_commit" in keys
    for o in area.options:
        assert o.hint.strip(), f"{o.key} has no wizard hint"
    # REPO-owned (committed rig.yaml), like agent_hooks — NOT a global-only category.
    opt = schema.option_for_key("internal_dev.auto_reload_on_commit")
    assert opt is not None and opt.layer == schema.REPO


# ── end-to-end: the hook actually reloads on a daemon-source commit (adversarial) ─────────
def _fake_tg_ctl_on_path(tmp_path, monkeypatch) -> Path:
    """Put a fake `tg-ctl` on PATH that records its invocation into a sentinel file."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    sentinel = tmp_path / "reloaded.txt"
    fake = bindir / "tg-ctl"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {sentinel}\n', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return sentinel


def _commit(repo: Path, path: str, body: str) -> None:
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", f"touch {path}"],
        check=True,
        env={**os.environ, "SKIP_RIG_SMOKE": "1"},
    )


def test_hook_reloads_only_on_daemon_source_change(tmp_path, monkeypatch):
    monkeypatch.delenv(dev_reload.DRY_RUN_ENV, raising=False)
    repo = tmp_path / "repo"
    _git_init(repo, monkeypatch)
    sentinel = _fake_tg_ctl_on_path(tmp_path, monkeypatch)
    runner._do_install_dev_reload_hook(
        _action(repo, paths=("src/daemon/*",), cmd="tg-ctl restart"), "backup"
    )

    # a NON-daemon file → no reload
    _commit(repo, "README.md", "docs\n")
    assert not sentinel.exists()

    # a daemon-source file → reload fires
    _commit(repo, "src/daemon/loop.ts", "loop\n")
    assert sentinel.exists()
    assert "restart" in sentinel.read_text(encoding="utf-8")


def test_hook_dry_run_suppresses_reload(tmp_path, monkeypatch):
    monkeypatch.setenv(dev_reload.DRY_RUN_ENV, "1")
    repo = tmp_path / "repo"
    _git_init(repo, monkeypatch)
    sentinel = _fake_tg_ctl_on_path(tmp_path, monkeypatch)
    runner._do_install_dev_reload_hook(_action(repo, paths=("src/daemon/*",)), "backup")
    _commit(repo, "src/daemon/loop.ts", "loop\n")
    assert not sentinel.exists()  # dry-run gate in the hook suppressed the real reload


def test_hook_matches_nested_daemon_source_path(tmp_path, monkeypatch):
    """Regression for a review-caught bug: unquoted `for p in $patterns` used to get shell-glob-
    expanded against the working tree BEFORE `case` ever ran, so `src/daemon/*` degraded to
    matching only the flat on-disk entries under `src/daemon/` — a nested changed file like
    `src/daemon/sub/x.ts` silently never matched, contradicting the documented `case`-globs-span-
    `/` invariant. `set -f` in the hook template is the fix; this seeds MULTIPLE on-disk siblings
    (including one that pre-expansion would have matched) so a pass here can't be a fluke."""
    monkeypatch.delenv(dev_reload.DRY_RUN_ENV, raising=False)
    repo = tmp_path / "repo"
    _git_init(repo, monkeypatch)
    _commit(repo, "README.md", "seed\n")  # isolates this test to path-matching, not root-commit
    sentinel = _fake_tg_ctl_on_path(tmp_path, monkeypatch)
    runner._do_install_dev_reload_hook(
        _action(repo, paths=("src/daemon/*",), cmd="tg-ctl restart"), "backup"
    )
    # a sibling flat file first, so `src/daemon/*` has something to (mis)expand to on disk.
    _commit(repo, "src/daemon/loop.ts", "loop\n")
    assert sentinel.exists()
    sentinel.unlink()

    _commit(repo, "src/daemon/sub/nested.ts", "nested\n")
    assert sentinel.exists(), "nested daemon-source file must match src/daemon/* like `case` promises"


def test_hook_reloads_on_root_commit(tmp_path, monkeypatch):
    """Regression: `git diff-tree HEAD` (no `--root`) prints nothing for a parentless commit, so
    a repo whose very first commit introduces the daemon source used to never reload. `--root`
    in the hook template is the fix."""
    monkeypatch.delenv(dev_reload.DRY_RUN_ENV, raising=False)
    repo = tmp_path / "repo"
    _git_init(repo, monkeypatch)
    sentinel = _fake_tg_ctl_on_path(tmp_path, monkeypatch)
    runner._do_install_dev_reload_hook(
        _action(repo, paths=("src/daemon/*",), cmd="tg-ctl restart"), "backup"
    )
    # the daemon-source file IS the repo's first-ever (parentless) commit.
    _commit(repo, "src/daemon/loop.ts", "loop\n")
    assert sentinel.exists(), "a repo's root commit touching daemon source must still reload"


def test_hook_installs_into_common_dir_from_a_linked_worktree(tmp_path, monkeypatch):
    """Regression for a review-caught bug: resolving via `--absolute-git-dir` returns a linked
    worktree's PRIVATE administrative dir (`<main>/.git/worktrees/<name>`), which git does NOT
    read hooks from — hooks live in the COMMON dir. A hook written to the wrong path never fires,
    while `rig status`/apply both agreed with each other and were both wrong. This drives a REAL
    `git worktree add` checkout (not a fresh repo) and commits IN the worktree."""
    monkeypatch.delenv(dev_reload.DRY_RUN_ENV, raising=False)
    main_repo = tmp_path / "main"
    _git_init(main_repo, monkeypatch)
    _commit(main_repo, "README.md", "seed\n")
    subprocess.run(["git", "-C", str(main_repo), "branch", "feature"], check=True)
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(main_repo), "worktree", "add", str(worktree), "feature"], check=True
    )

    sentinel = _fake_tg_ctl_on_path(tmp_path, monkeypatch)
    res = runner._do_install_dev_reload_hook(
        _action(worktree, paths=("src/daemon/*",), cmd="tg-ctl restart"), "backup"
    )
    assert res.status == "created"
    # the installed hook must land in the COMMON dir main git actually consults...
    common_hook = main_repo / ".git" / "hooks" / "post-commit"
    assert common_hook.is_file()
    # ...NOT under the worktree's private per-worktree administrative dir.
    private_hook = main_repo / ".git" / "worktrees" / "worktree" / "hooks" / "post-commit"
    assert not private_hook.is_file()

    # and a real commit made IN the worktree must actually fire the reload.
    _commit(worktree, "src/daemon/loop.ts", "loop\n")
    assert sentinel.exists(), "post-commit hook installed via a linked worktree must actually fire"
