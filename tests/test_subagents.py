"""rig provisions sub-agent definitions (the ``subagents`` category).

A sub-agent is a single ``.claude/agents/<name>.md`` file (CC frontmatter + system-prompt
body). rig scans ``agent-tools/subagents/<name>.md`` into catalog Items and copies each
enabled one straight into the harness agent dir — the install dir IS the discovery dir, so
(unlike skills) there is no harness-link second action. GLOBAL vs REPO-LOCAL is purely the
target shape (absolute/~ → global; relative → repo-anchored). rig never clobbers a real
hand-edited agent without honoring on_conflict.
"""

from __future__ import annotations

from pathlib import Path

from riglib.actions.runner import _do_copy_subagent, run_plan
from riglib.catalog import Catalog
from riglib.plan import Action, InstallPlan


def _fake_source(tmp_path: Path) -> Path:
    src = tmp_path / "agent-tools"
    (src / "skills").mkdir(parents=True)
    (src / "agent-hooks").mkdir()
    sub = src / "subagents"
    sub.mkdir()
    (sub / "ship-pr.md").write_text(
        "---\nname: ship-pr\ndescription: Ships a PR via gh ship\ntools: Bash, Read\nmodel: sonnet\n---\nYou ship PRs.\n",
        encoding="utf-8",
    )
    (sub / "roadmap-driver.md").write_text(
        "---\nname: roadmap-driver\ndescription: Drives a ROADMAP item to a shipped PR\n---\nbody\n",
        encoding="utf-8",
    )
    return src


def _action(source: Path, target: Path) -> Action:
    return Action(kind="copy_subagent", category="subagents", item=source.stem, source=source, target=target)


# ── scanner ─────────────────────────────────────────────────────────────────────────
def test_scan_finds_subagents_with_frontmatter_description(tmp_path):
    cat = Catalog.scan(str(_fake_source(tmp_path)))
    subs = sorted((i.name, i.description, i.category) for i in cat.items if i.category == "subagents")
    assert subs == [
        ("roadmap-driver", "Drives a ROADMAP item to a shipped PR", "subagents"),
        ("ship-pr", "Ships a PR via gh ship", "subagents"),
    ]


def test_scan_absent_subagents_dir_is_a_noop(tmp_path):
    src = tmp_path / "agent-tools"
    (src / "skills").mkdir(parents=True)
    (src / "agent-hooks").mkdir()  # no subagents/ dir
    cat = Catalog.scan(str(src))
    assert [i for i in cat.items if i.category == "subagents"] == []


# ── apply (the copy_subagent handler) ────────────────────────────────────────────────
def test_copy_subagent_creates_the_file(tmp_path):
    src = _fake_source(tmp_path) / "subagents" / "ship-pr.md"
    target = tmp_path / "home" / ".claude" / "agents" / "ship-pr.md"
    res = _do_copy_subagent(_action(src, target), "backup")
    assert res.status in ("created", "installed", "updated")
    assert target.is_file() and not target.is_symlink()
    assert target.read_text() == src.read_text()


def test_copy_subagent_is_idempotent(tmp_path):
    src = _fake_source(tmp_path) / "subagents" / "ship-pr.md"
    target = tmp_path / "home" / ".claude" / "agents" / "ship-pr.md"
    _do_copy_subagent(_action(src, target), "backup")
    res2 = _do_copy_subagent(_action(src, target), "backup")
    assert res2.status in ("skipped", "ok", "unchanged")
    assert target.read_text() == src.read_text()


def test_copy_subagent_backs_up_a_hand_edited_agent(tmp_path):
    src = _fake_source(tmp_path) / "subagents" / "ship-pr.md"
    target = tmp_path / "home" / ".claude" / "agents" / "ship-pr.md"
    target.parent.mkdir(parents=True)
    target.write_text("--- a user's own agent ---\n", encoding="utf-8")
    res = _do_copy_subagent(_action(src, target), "backup")
    # the rig version lands, but the user's content is preserved in a backup
    assert target.read_text() == src.read_text()
    backups = [p for p in target.parent.iterdir() if p.name != "ship-pr.md"]
    assert backups and any("a user's own agent" in p.read_text() for p in backups)


def test_copy_subagent_skip_policy_leaves_user_file(tmp_path):
    src = _fake_source(tmp_path) / "subagents" / "ship-pr.md"
    target = tmp_path / "home" / ".claude" / "agents" / "ship-pr.md"
    target.parent.mkdir(parents=True)
    target.write_text("mine\n", encoding="utf-8")
    _do_copy_subagent(_action(src, target), "skip")
    assert target.read_text() == "mine\n"  # untouched under skip


def test_run_plan_executes_copy_subagent(tmp_path):
    src = _fake_source(tmp_path) / "subagents" / "roadmap-driver.md"
    target = tmp_path / "global" / ".claude" / "agents" / "roadmap-driver.md"
    plan = InstallPlan()
    plan.actions.append(_action(src, target))
    run_plan(plan)
    assert target.is_file() and target.read_text() == src.read_text()
