from pathlib import Path

from riglib import config
from riglib.cli import main

def _w(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

def _base(repo: Path, agent_tools):
    _w(repo / "rig.yaml", f"version: 1\nagent_tools_source: {agent_tools}\nskills: {{enabled: false}}\nagent_hooks: {{enabled: false}}\nci: {{enabled: false}}\nmcp: {{enabled: false}}\ngit_hooks: {{dispatcher: {{enabled: false}}}}\nharness: {{auto_mode: true}}\n")

def test_config_set_is_preview_by_default(tmp_path, fake_agent_tools, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo=tmp_path/"repo"; repo.mkdir(); _base(repo, fake_agent_tools)
    before=(repo/"rig.yaml").read_bytes()
    rc=main(["config","set","harness.auto_mode","false","-C",str(repo)])
    out=capsys.readouterr().out
    assert rc == 0
    assert (repo/"rig.yaml").read_bytes() == before
    assert "Would set harness.auto_mode = false" in out
    assert "PREVIEW" in out and "--commit" in out

def test_config_set_commit_writes_change(tmp_path, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo=tmp_path/"repo"; repo.mkdir(); _base(repo, fake_agent_tools)
    rc=main(["config","set","harness.auto_mode","false","-C",str(repo),"--commit"])
    assert rc == 0
    assert config.load(repo).data["harness"]["auto_mode"] is False

def test_global_config_set_preview_does_not_create_global_file(tmp_path, fake_agent_tools, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    repo=tmp_path/"repo"; repo.mkdir(); _base(repo, fake_agent_tools)
    gpath=config.global_config_path()
    assert not gpath.exists()
    rc=main(["config","set","defaults.on_conflict","overwrite","-C",str(repo),"--global"])
    assert rc == 0 and not gpath.exists()
    assert "Would set defaults.on_conflict = overwrite" in capsys.readouterr().out

def test_no_apply_remains_explicit_write_only(tmp_path, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo=tmp_path/"repo"; repo.mkdir(); _base(repo, fake_agent_tools)
    rc=main(["config","set","harness.auto_mode","false","-C",str(repo),"--no-apply"])
    assert rc == 0
    assert config.load(repo).data["harness"]["auto_mode"] is False
