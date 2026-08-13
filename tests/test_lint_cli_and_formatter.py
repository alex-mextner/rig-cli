import json

from riglib.cli import build_parser, main
from riglib.linter_environment import inspect_formatter_environment

def _pkg(repo, dev=None, scripts=None):
    payload = {"devDependencies": dev or {}}
    if scripts:
        payload["scripts"] = scripts
    (repo / "package.json").write_text(json.dumps(payload), encoding="utf-8")

def test_lint_rules_parser_is_nested_not_top_level_rules():
    args = build_parser().parse_args(["lint", "rules", "--json"])
    assert args.command == "lint" and args.lint_command == "rules" and args.json

def test_lint_rules_json_reports_provider_and_effective_policy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    repo = tmp_path / "repo"; repo.mkdir(); (repo / ".git").mkdir()
    (repo / "rig.yaml").write_text("version: 1\nlinters:\n  rules:\n    all: true\n", encoding="utf-8")
    rc = main(["lint", "rules", "anti-slop/no-optional-function-parameters", "-C", str(repo), "--config", str(repo / "rig.yaml"), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["provider"] == "anti-slop" and data[0]["effective"] == "error" and data[0]["reason"] == "all=true"

def test_formatter_readiness_detects_prettier_without_oxfmt(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir(); _pkg(repo, {"prettier": "x"})
    env = inspect_formatter_environment(repo)
    assert not env.ready and env.foreign_formatters == ("Prettier",)

def test_formatter_ready_with_repo_local_oxfmt_even_if_prettier_coexists(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir(); _pkg(repo, {"oxfmt": "x", "prettier": "x"})
    env = inspect_formatter_environment(repo)
    assert env.ready and env.oxfmt_present and env.foreign_formatters == ("Prettier",)


def test_script_detection_does_not_confuse_standard_version_with_standardjs(tmp_path):
    from riglib.linter_environment import inspect_linter_environment
    repo = tmp_path / "repo"; repo.mkdir(); _pkg(repo, {}, {"release": "standard-version"})
    env = inspect_linter_environment(repo)
    assert "StandardJS" not in env.foreign_linters

def test_script_detection_sees_exact_standard_executable(tmp_path):
    from riglib.linter_environment import inspect_linter_environment
    repo = tmp_path / "repo"; repo.mkdir(); _pkg(repo, {}, {"lint": "npx standard src"})
    env = inspect_linter_environment(repo)
    assert "StandardJS" in env.foreign_linters
