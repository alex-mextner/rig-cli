"""rig provisions the `gh-graphql` alias skill (the `ghgql` wrapper over `gh api graphql`).

The convenient `gh api graphql` alias ships in agent-tools as a UNIVERSAL SKILL
(`skills/universal/gh-graphql/`) that bundles a `SKILL.md` plus a runnable `ghgql` wrapper.
rig provisions it through the ordinary skills path — the catalog auto-discovers any
`skills/universal/<name>/SKILL.md`, and a skills-enabled plan emits a `copy_skill` action
whose carrier is the whole skill directory, so the bundled `ghgql` executable rides along.

These tests are the rig HALF of the coupled change: they prove `rig apply` installs the
alias without any bespoke catalog wiring (a new item follows the generic skills path) and
that the executable is carried, not just the markdown. The hook HALF (the read-only alias
invocation is NOT blocked by `block-raw-pr-merge`) is proven in agent-tools' own suite.
"""

from __future__ import annotations

from pathlib import Path

import stat

from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.plan import InstallPlan, build

_SKILL_DESCRIPTION = (
    "Use when querying the GitHub GraphQL API from the shell — prefer the ghgql wrapper."
)


def _install_gh_graphql_skill(root: Path) -> None:
    """Add a faithful `gh-graphql` skill (SKILL.md + executable `ghgql`) to a checkout."""
    skill = root / "skills" / "universal" / "gh-graphql"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: gh-graphql\ndescription: {_SKILL_DESCRIPTION}\n---\n# gh-graphql\n",
        encoding="utf-8",
    )
    ghgql = skill / "ghgql"
    ghgql.write_text("#!/usr/bin/env bash\nexec gh api graphql -f \"query=$1\"\n", encoding="utf-8")
    ghgql.chmod(0o755)


def _cfg(data: dict, repo_root: Path) -> LoadedConfig:
    return LoadedConfig(data=data, repo_root=repo_root)


def test_catalog_discovers_gh_graphql_skill(fake_agent_tools):
    _install_gh_graphql_skill(fake_agent_tools)
    cat = Catalog.scan(str(fake_agent_tools))
    item = cat.get("skills", "gh-graphql")
    assert item is not None, "gh-graphql skill must be auto-discovered by the catalog"
    assert item.group == "universal"
    assert item.default_enabled is True  # a normal universal skill, on by default
    assert "GraphQL" in item.description


def test_plan_provisions_gh_graphql_skill(fake_agent_tools, tmp_path):
    _install_gh_graphql_skill(fake_agent_tools)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"universal": {"all": True}, "by_type": {"enable": []}}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    copy = next(
        (a for a in plan.actions if a.category == "skills" and a.item == "gh-graphql"),
        None,
    )
    assert copy is not None, "a skills-enabled apply must plan a copy_skill for gh-graphql"
    assert copy.kind == "copy_skill"


def test_gh_graphql_bundled_wrapper_is_provisioned_executable(fake_agent_tools, tmp_path):
    """Actually APPLY the plan and assert the DESTINATION: `copy_skill` must install the whole skill
    directory, so the bundled `ghgql` executable lands next to `SKILL.md` (not left behind) and
    keeps its executable bit. Guards against a future 'copy only SKILL.md' or 'drop the mode'
    regression — inspecting the plan source alone would not."""
    _install_gh_graphql_skill(fake_agent_tools)
    cat = Catalog.scan(str(fake_agent_tools))
    target = tmp_path / "skills"
    cfg = _cfg(
        {
            "skills": {
                "target": str(target),
                "harness_link": False,  # keep hermetic — no ~/.claude symlink
                "universal": {"all": True},
                "by_type": {"enable": []},
            }
        },
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    # Run ONLY the skills actions so the apply is hermetic — never touch a real HOME target that a
    # non-skills default-enabled action might carry.
    skills_only = InstallPlan(actions=[a for a in plan.actions if a.category == "skills"])
    run_plan(skills_only)
    installed = target / "gh-graphql" / "ghgql"
    assert installed.is_file(), "apply must install the ghgql wrapper, not only SKILL.md"
    assert (target / "gh-graphql" / "SKILL.md").is_file()
    # Faithful mode carry: the 0o755 source must arrive 0o755, not merely 'some exec bit survived'.
    assert stat.S_IMODE(installed.stat().st_mode) == 0o755, "installed ghgql must keep mode 0o755"
    # KNOWN GAP, not fixed here (review of #175): `fsutil.dirs_identical` — the skip-if-identical/
    # drift check both re-apply and `rig status` use — compares file set + content only, never
    # mode bits, so a `chmod 644` on an already-installed executable skill file like `ghgql`
    # reads as "identical to source" and silently stays non-executable. A stricter, mode-aware
    # `dirs_identical` was attempted and reverted: it broke the global-hooks dispatcher's composer
    # copy (see the comment at its call site in `riglib/actions/runner.py`), which relies on
    # mode-blindness to converge a non-executable source to an executable target. Closing this
    # properly needs a per-caller opt-in, not a change to `dirs_identical`'s default behavior — a
    # separate, larger change deserving its own PR rather than scope-creep here.
