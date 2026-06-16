"""Plan-builder error integration — unknown items now raise structured errors v2.

`plan.build` (via `_validate_item_names`) is where a config that names a non-existent
catalog item is caught. These tests assert it now raises the structured
:class:`errors.UnknownItemError` (exit 4) carrying the 3-part what/why/fix — with the
removed-slot, did-you-mean, and empty-category heuristics — instead of the old thin
``PlanError("unknown … (known: none)")``.
"""

from __future__ import annotations

import pytest

from riglib import errors
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.plan import build


def _loaded(tmp_path, data: dict) -> LoadedConfig:
    cfg = tmp_path / "rig.yaml"
    cfg.write_text("version: 1\n", encoding="utf-8")  # a real file for the path to name
    return LoadedConfig(data=data, repo_root=tmp_path, repo_path=cfg, layers=[f"repo:{cfg}"])


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
