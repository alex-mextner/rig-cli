import json

from riglib.linter_environment import inspect_linter_environment


def _package(repo, *, dev=None, scripts=None):
    payload = {}
    if dev is not None:
        payload["devDependencies"] = dev
    if scripts is not None:
        payload["scripts"] = scripts
    (repo / "package.json").write_text(json.dumps(payload), encoding="utf-8")


def test_no_linter_is_blocked_with_ready_to_copy_prompt(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _package(repo)
    env = inspect_linter_environment(repo)
    assert not env.ready
    assert not env.oxlint_present
    assert "no repository-local Oxlint dependency" in env.reason
    assert "Install compatible oxlint, oxfmt, and @oxlint/plugins" in env.agent_prompt
    assert "do not rely on a globally installed binary" in env.agent_prompt


def test_foreign_dependency_is_blocked_and_named(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _package(repo, dev={"eslint": "x"})
    env = inspect_linter_environment(repo)
    assert not env.ready
    assert env.foreign_linters == ("ESLint",)
    assert "uses ESLint" in env.reason


def test_foreign_config_is_detected_without_dependency(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _package(repo)
    (repo / "biome.json").write_text("{}\n", encoding="utf-8")
    env = inspect_linter_environment(repo)
    assert not env.ready
    assert "Biome" in env.foreign_linters


def test_foreign_script_is_detected_without_dependency(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _package(repo, scripts={"lint": "eslint src"})
    env = inspect_linter_environment(repo)
    assert not env.ready
    assert "ESLint" in env.foreign_linters


def test_repo_local_oxlint_is_ready_even_when_foreign_linter_coexists(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _package(repo, dev={"oxlint": "x", "eslint": "x"})
    env = inspect_linter_environment(repo)
    assert env.ready
    assert env.oxlint_present
    assert env.foreign_linters == ("ESLint",)
    assert "@oxlint/plugins" in env.missing_oxc_packages


def test_full_oxc_toolchain_has_no_missing_packages(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _package(
        repo,
        dev={"oxlint": "x", "oxfmt": "x", "@oxlint/plugins": "x"},
    )
    env = inspect_linter_environment(repo)
    assert env.ready
    assert env.missing_oxc_packages == ()
