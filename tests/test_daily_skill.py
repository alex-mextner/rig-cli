"""`rig daily install-skill` — writes daily's own SKILL.md and links it into the harness
discovery dirs, via the SAME generic worker `rig install-skill` uses (`install_named_skill`).
HOME-isolated by the autouse fixture in conftest.py."""

from __future__ import annotations

from pathlib import Path

from riglib.daily.skill import SKILL_NAME, install_skill


def _harness_link(home: Path) -> Path:
    return home / ".claude" / "skills" / SKILL_NAME


def test_install_skill_writes_md_and_harness_link(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    assert install_skill() == 0
    md = home / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    assert md.is_file()
    assert "rig daily" in md.read_text(encoding="utf-8")
    link = _harness_link(home)
    assert link.is_symlink()
    assert link.resolve() == md.parent.resolve()


def test_install_skill_idempotent(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    assert install_skill() == 0
    capsys.readouterr()
    assert install_skill() == 0
    assert "already current" in capsys.readouterr().out


def test_install_skill_does_not_collide_with_rig_own_skill(tmp_path, monkeypatch):
    """`rig install-skill` (name="rig") and `rig daily install-skill` (name="daily") must
    land as SIBLING skill dirs, not overwrite one another."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    from riglib.install import install_skill as rig_install_skill

    assert rig_install_skill() == 0
    assert install_skill() == 0
    assert (home / ".agents" / "skills" / "rig" / "SKILL.md").is_file()
    assert (home / ".agents" / "skills" / "daily" / "SKILL.md").is_file()
