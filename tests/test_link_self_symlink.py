"""Guard: a link action whose SOURCE resolves onto its own TARGET must never self-symlink.

Regression for the 2026-07-15 damage — the opencode hook-bridge link action found
``plugin_path == dest`` (the symlink would point a file at itself), yet it backed up the
real git-tracked ``plugin.js`` and replaced it with a self-referential symlink, corrupting
the source module (``stat -L`` → "Too many levels of symbolic links"). Any link action must
detect ``source == target`` and REFUSE, leaving the real file intact.
"""

from __future__ import annotations

from pathlib import Path

from riglib.actions.runner import (
    _do_link_skill_harness,
    _do_register_opencode_hook_bridge,
)
from riglib.plan import Action


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── opencode hook bridge — the actual damaged action ──────────────────────────────
def _opencode_action(lib_dir: Path, module: str) -> Action:
    dest = lib_dir / module / "plugin.js"
    # target IS the source plugin (has a suffix → hook_bridge_settings_file returns it
    # verbatim), so plugin_path resolves onto dest — the self-symlink trap.
    return Action(
        kind="register_hook_bridge",
        category="hook_bridge",
        item="opencode",
        source=lib_dir,
        target=dest,
        options={"lib_dir": str(lib_dir), "module": module, "format": "opencode-plugin"},
    )


def test_opencode_bridge_source_equals_target_is_noop(tmp_path):
    module = "opencode_hook_bridge"
    dest = tmp_path / module / "plugin.js"
    dest.parent.mkdir(parents=True)
    original = "// real ES module\nexport const AgentToolsHookBridge = {};\n"
    dest.write_text(original, encoding="utf-8")

    res = _do_register_opencode_hook_bridge(_opencode_action(tmp_path, module), "backup")

    assert res.status == "skipped", res.detail
    assert "source == target" in res.detail
    # the real file is UNTOUCHED — not a symlink, content preserved.
    assert dest.is_file() and not dest.is_symlink()
    assert _read(dest) == original
    # no backup was made next to it.
    assert not list(dest.parent.glob("plugin.js.rig-bak-*"))


def test_opencode_bridge_distinct_paths_still_links(tmp_path):
    # guard must NOT over-match the damaged action: a normal bridge (plugin_path != dest)
    # is still symlinked into the opencode plugin dir.
    module = "opencode_hook_bridge"
    dest = tmp_path / "lib" / module / "plugin.js"
    dest.parent.mkdir(parents=True)
    dest.write_text("export const AgentToolsHookBridge = {};\n", encoding="utf-8")
    plugin_link = tmp_path / "opencode" / "plugin" / "zz-agent-tools-hook-bridge.js"

    action = Action(
        kind="register_hook_bridge",
        category="hook_bridge",
        item="opencode",
        source=tmp_path / "lib",
        target=plugin_link,  # distinct from dest → guard does not trigger
        options={"lib_dir": str(tmp_path / "lib"), "module": module, "format": "opencode-plugin"},
    )
    res = _do_register_opencode_hook_bridge(action, "backup")

    assert res.status in {"created", "updated"}, res.detail
    assert plugin_link.is_symlink()
    assert plugin_link.resolve() == dest.resolve()


# ── skill-harness link — same class, cover it too ─────────────────────────────────
def test_skill_link_source_equals_target_is_noop(tmp_path):
    skill = tmp_path / "skills" / "my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# my skill\n", encoding="utf-8")

    # link_path == dest: the harness link would point the installed skill at itself.
    action = Action(
        kind="link_skill_harness",
        category="skills",
        item="my-skill",
        source=skill,
        target=skill,
        options={},
    )
    res = _do_link_skill_harness(action, "backup")

    assert res.status == "skipped", res.detail
    assert "source == target" in res.detail
    assert skill.is_dir() and not skill.is_symlink()
    assert (skill / "SKILL.md").is_file()
    assert not list(skill.parent.glob("my-skill.rig-bak-*"))


# ── positive path — a legitimate link (source != target) must STILL proceed ───────
def _skill_action(source: Path, target: Path) -> Action:
    return Action(
        kind="link_skill_harness",
        category="skills",
        item="my-skill",
        source=source,
        target=target,
        options={},
    )


def test_skill_link_distinct_paths_is_created(tmp_path):
    # the guard must NOT over-match: a normal link (link_path != dest) is created as usual.
    installed = tmp_path / "installed" / "my-skill"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("# my skill\n", encoding="utf-8")
    harness_link = tmp_path / "harness" / "my-skill"

    res = _do_link_skill_harness(_skill_action(installed, harness_link), "backup")

    assert res.status == "created", res.detail
    assert harness_link.is_symlink()
    assert harness_link.resolve() == installed.resolve()


def test_skill_link_idempotent_rerun_is_skipped_not_selfguarded(tmp_path):
    # an already-correct symlink at link_path (which IS a symlink) must be recognized as an
    # idempotent no-op via _same_link_dest — NOT mistaken for a self-symlink by the new guard
    # (the guard keeps the leaf verbatim precisely so a symlink-at-link_path != its own target).
    installed = tmp_path / "installed" / "my-skill"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("# my skill\n", encoding="utf-8")
    harness_link = tmp_path / "harness" / "my-skill"
    harness_link.parent.mkdir(parents=True)
    harness_link.symlink_to(installed.resolve())

    res = _do_link_skill_harness(_skill_action(installed, harness_link), "backup")

    assert res.status == "skipped", res.detail
    assert "already linked" in res.detail
    assert harness_link.is_symlink()
    assert harness_link.resolve() == installed.resolve()
