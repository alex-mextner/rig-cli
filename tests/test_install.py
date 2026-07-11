"""install-skill — writes the rig SKILL.md AND links it into the harness discovery dir.

These tests are HOME-isolated (the autouse `_isolate_home` fixture points HOME at a tmp dir),
so they exercise the real symlink logic without touching the developer's ~/.claude/skills.
"""

from __future__ import annotations

import os
from pathlib import Path

from riglib.install import SKILL_NAME, install_skill


def _harness_link(home: Path) -> Path:
    return home / ".claude" / "skills" / SKILL_NAME


def _codex_link(home: Path) -> Path:
    return home / ".codex" / "skills" / SKILL_NAME


def test_install_skill_writes_md_and_harness_link(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    rc = install_skill()
    assert rc == 0
    md = home / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    assert md.is_file()
    link = _harness_link(home)
    assert link.is_symlink(), "rig skill not symlinked into the harness dir"
    assert link.resolve() == md.parent.resolve()
    assert (link / "SKILL.md").is_file()  # the link resolves to the real skill
    codex_link = _codex_link(home)
    assert codex_link.is_symlink(), "rig skill not symlinked into the Codex skill dir"
    assert codex_link.resolve() == md.parent.resolve()


def test_install_skill_links_codex_home_when_set(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert install_skill() == 0

    md = home / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    codex_link = codex_home / "skills" / SKILL_NAME
    assert codex_link.is_symlink(), "rig skill not symlinked into CODEX_HOME skills dir"
    assert codex_link.resolve() == md.parent.resolve()
    assert not _codex_link(home).exists()


def test_install_skill_md_lists_config_command(tmp_path, monkeypatch):
    # the embedded SKILL.md is the agent-facing command catalog — it must mention `rig config`
    # so agents discover the recommended single-key edit path (help-docs-sync across files).
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    assert install_skill() == 0
    md = (home / ".agents" / "skills" / SKILL_NAME / "SKILL.md").read_text(encoding="utf-8")
    assert "rig config" in md
    assert "rig config set" in md


def test_install_skill_idempotent(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    assert install_skill() == 0
    capsys.readouterr()
    assert install_skill() == 0  # second run is a clean no-op
    out = capsys.readouterr().out
    assert "already current" in out
    # the link still resolves correctly
    link = _harness_link(home)
    assert link.is_symlink()
    assert _codex_link(home).is_symlink()


def test_install_skill_leaves_real_harness_dir(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    # a hand-authored rig skill already occupies the harness path as a REAL dir
    real = home / ".claude" / "skills" / SKILL_NAME
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("hand-authored\n", encoding="utf-8")
    assert install_skill() == 0
    assert not real.is_symlink()  # left untouched
    assert (real / "SKILL.md").read_text() == "hand-authored\n"
    assert "left untouched" in capsys.readouterr().out


def test_install_skill_repoints_wrong_harness_link(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    link = _harness_link(home)
    link.parent.mkdir(parents=True)
    bogus = home / "elsewhere"
    bogus.mkdir()
    link.symlink_to(bogus)  # stale link
    assert install_skill() == 0
    expected = (home / ".agents" / "skills" / SKILL_NAME)
    assert link.resolve() == expected.resolve()  # re-pointed to the real skill
