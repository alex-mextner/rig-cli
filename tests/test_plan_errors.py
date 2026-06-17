"""Plan-builder error integration — unknown items now raise structured errors v2.

`plan.build` (via `_validate_item_names`) is where a config that names a non-existent
catalog item is caught. These tests assert it now raises the structured
:class:`errors.UnknownItemError` (exit 4) carrying the 3-part what/why/fix — with the
removed-slot, did-you-mean, and empty-category heuristics — instead of the old thin
``PlanError("unknown … (known: none)")``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib import config, errors
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.plan import build


def _loaded(tmp_path, data: dict) -> LoadedConfig:
    cfg = tmp_path / "rig.yaml"
    cfg.write_text("version: 1\n", encoding="utf-8")  # a real file for the path to name
    return LoadedConfig(data=data, repo_root=tmp_path, repo_path=cfg, layers=[f"repo:{cfg}"])


def _write_yaml(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


def test_unknown_mcp_item_raises_structured_with_suggestion(tmp_path, fake_agent_tools):
    catalog = Catalog.scan(str(fake_agent_tools))
    # fake catalog has mcp slot "review"; "reviewr" is a typo
    loaded = _loaded(tmp_path, {"mcp": {"items": {"reviewr": {"enabled": True}}}})
    with pytest.raises(errors.UnknownItemError) as exc:
        build(loaded, catalog)
    e = exc.value
    assert e.exit_code == errors.EXIT_UNKNOWN_ITEM
    assert "reviewr" in e.what
    assert "review" in e.fix  # did-you-mean
    assert str(tmp_path / "rig.yaml") in e.fix  # the offending file path


def test_removed_mcp_review_slot_names_pr_and_fix(tmp_path, fake_agent_tools):
    # a catalog WITHOUT a review mcp slot (so 'review' is genuinely gone) → removed-slot path.
    # Build a catalog then drop the review mcp item to simulate the post-#32 world.
    catalog = Catalog.scan(str(fake_agent_tools))
    catalog.items = [i for i in catalog.items if not (i.category == "mcp" and i.name == "review")]
    loaded = _loaded(tmp_path, {"mcp": {"items": {"review": {"enabled": True}}}})
    with pytest.raises(errors.UnknownItemError) as exc:
        build(loaded, catalog)
    blob = exc.value.what + exc.value.why + exc.value.fix
    assert "#32" in blob  # names the removal PR
    assert "mcp.items.review" in blob  # the exact key
    assert "remove" in blob.lower()
    assert str(tmp_path / "rig.yaml") in blob


def test_unknown_universal_skill_suggests_nearest(tmp_path, fake_agent_tools):
    catalog = Catalog.scan(str(fake_agent_tools))
    # fake catalog has "naming"; "namin" is a typo
    loaded = _loaded(tmp_path, {"skills": {"universal": {"enable": ["namin"]}}})
    with pytest.raises(errors.UnknownItemError) as exc:
        build(loaded, catalog)
    assert "naming" in exc.value.fix


def test_unknown_ci_item_raises_structured(tmp_path, fake_agent_tools):
    catalog = Catalog.scan(str(fake_agent_tools))
    loaded = _loaded(tmp_path, {"ci": {"items": {"secret-scn": {"enabled": True}}}})
    with pytest.raises(errors.UnknownItemError) as exc:
        build(loaded, catalog)
    assert exc.value.exit_code == errors.EXIT_UNKNOWN_ITEM
    assert "secret-scan" in exc.value.fix  # nearest valid


def test_unknown_git_hooks_key_raises_structured(tmp_path, fake_agent_tools):
    catalog = Catalog.scan(str(fake_agent_tools))
    loaded = _loaded(tmp_path, {"git_hooks": {"dispatcherr": {"enabled": True}}})
    with pytest.raises(errors.UnknownItemError) as exc:
        build(loaded, catalog)
    assert "dispatcher" in exc.value.fix


def test_unknown_item_from_global_layer_names_global_file(tmp_path, fake_agent_tools, monkeypatch):
    """An unknown item that came SOLELY from the global config is reported against the GLOBAL
    file, not the repo's rig.yaml — the loader tracks per-key layer provenance.

    Motivating case: a stale/removed MCP slot lingers in ``~/.config/rig/config.yaml`` while the
    repo's rig.yaml has its own (valid) blocks. The fix must tell the user to edit the GLOBAL
    file that actually carries the bad key, not the repo file that doesn't mention it.
    """
    xdg = tmp_path / "xdg"
    gcfg = xdg / "rig" / "config.yaml"
    _write_yaml(gcfg, f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
             "mcp: {items: {reviewr: {enabled: true}}}\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    repo = tmp_path / "repo"
    rcfg = repo / "rig.yaml"
    # repo declares OTHER (valid) blocks — it must NOT be blamed for the global's bad mcp key.
    _write_yaml(rcfg, "version: 1\nskills: {enabled: false}\n")

    loaded = config.load(repo)
    catalog = Catalog.scan(str(fake_agent_tools))
    with pytest.raises(errors.UnknownItemError) as exc:
        build(loaded, catalog)
    e = exc.value
    assert "reviewr" in e.what
    assert str(gcfg) in e.why  # provenance: names the GLOBAL file that carries the bad key
    assert str(gcfg) in e.fix
    assert str(rcfg) not in (e.why + e.fix)  # the repo file is NOT blamed


def test_unknown_item_overridden_in_repo_names_repo_file(tmp_path, fake_agent_tools, monkeypatch):
    """When the repo layer ALSO sets the offending top-level key, the repo file wins provenance
    (the repo overrides the global layer) — so the error names the repo file the user edits."""
    xdg = tmp_path / "xdg"
    gcfg = xdg / "rig" / "config.yaml"
    _write_yaml(gcfg, f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
             "mcp: {items: {review: {enabled: true}}}\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    repo = tmp_path / "repo"
    rcfg = repo / "rig.yaml"
    _write_yaml(rcfg, "version: 1\nmcp: {items: {reviewr: {enabled: true}}}\n")  # repo's own bad mcp key

    loaded = config.load(repo)
    catalog = Catalog.scan(str(fake_agent_tools))
    with pytest.raises(errors.UnknownItemError) as exc:
        build(loaded, catalog)
    e = exc.value
    assert "reviewr" in e.what
    assert str(rcfg) in e.fix  # the repo set mcp → repo file is the source to edit


def test_unknown_item_from_explicit_config_layer_names_that_file(tmp_path, fake_agent_tools, monkeypatch):
    """A `--config P` (explicit) layer carries provenance too: an unknown key in P is reported
    against P, not the repo's rig.yaml. Locks in the explicit branch of the cascade so a new
    layer can't silently skip key-source tracking."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _write_yaml(repo / "rig.yaml", "version: 1\nskills: {enabled: false}\n")  # ignored: --config replaces it
    explicit = tmp_path / "explicit.yaml"
    _write_yaml(explicit, f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
                "mcp: {items: {reviewr: {enabled: true}}}\n")

    loaded = config.load(repo, explicit_config=explicit)
    catalog = Catalog.scan(str(fake_agent_tools))
    with pytest.raises(errors.UnknownItemError) as exc:
        build(loaded, catalog)
    e = exc.value
    assert "reviewr" in e.what
    assert str(explicit) in e.fix  # the explicit --config file is the source to edit
