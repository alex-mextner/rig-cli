"""rig status — the AREA-COVERAGE summary (ROADMAP "cover ALL reconciled areas, not mostly skills").

Drives the CLI through ``main(["status", ...])`` and asserts on captured stdout that every
reconciled area shows (grouped by the GLOBAL/REPO layer, with in-sync vs drift counts), plus
unit-tests the area registry (:mod:`riglib.areas`) that is the single source of truth for the
area list. See ``riglib/areas.py`` for the full rationale.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from riglib import errors
from riglib.areas import (
    AREAS,
    Area,
    area_matches_action,
    area_matches_drift,
    areas_for_layer,
)
from riglib.cli import main
from riglib.layers import GLOBAL, REPO


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Isolate HOME so the missing-target scan of ~/.claude/settings.json can't perturb tests."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    return path


# ── the area registry is exhaustive + correctly split by layer ────────────────────
def test_area_registry_covers_every_roadmap_area():
    """Every area the ROADMAP lists must be present in the registry, mapped to the right layer."""
    by_key = {a.key: a for a in AREAS}
    expected_global = {
        "skills", "agent_hooks", "git_hooks", "gitignore", "mcp", "harness", "tmux",
        "models", "tg_ctl",
    }
    expected_repo = {"ci", "ship", "agents_md", "github", "linters"}
    assert expected_global <= set(by_key)
    assert expected_repo <= set(by_key)
    for k in expected_global:
        assert by_key[k].layer == GLOBAL, k
    for k in expected_repo:
        assert by_key[k].layer == REPO, k
    # the ci/ship split: both ride the `ci` category but partition by slot.
    assert by_key["ci"].categories == ("ci",) and by_key["ci"].ship_slot is False
    assert by_key["ship"].categories == ("ci",) and by_key["ship"].ship_slot is True


def test_areas_for_layer_partitions_all_areas():
    g = areas_for_layer(GLOBAL)
    r = areas_for_layer(REPO)
    assert set(g) | set(r) == set(AREAS)
    assert not (set(g) & set(r))  # an area belongs to exactly one layer


def test_ci_ship_split_routes_actions_and_drift():
    ci = next(a for a in AREAS if a.key == "ci")
    ship = next(a for a in AREAS if a.key == "ship")
    # actions: keyed off options.slot
    assert area_matches_action(ship, "ci", {"slot": "ship"})
    assert not area_matches_action(ci, "ci", {"slot": "ship"})
    assert area_matches_action(ci, "ci", {"slot": "codeql"})
    assert not area_matches_action(ship, "ci", {"slot": "codeql"})
    # drift: keyed off the item name (drift items carry no options). The ship merge gate's
    # config→disk drift (missing/modified) carries item == "ship".
    assert area_matches_drift(ship, "ci", "ship", "missing")
    assert not area_matches_drift(ci, "ci", "ship", "missing")
    assert area_matches_drift(ci, "ci", "codeql", "missing")
    assert not area_matches_drift(ship, "ci", "codeql", "missing")
    # a disk→config EXTRA ci item is a workflow filename stem — even a literal "ship" (from a
    # rogue ship.yml) routes to CI gates, NOT the ship merge gate (which is a ~/bin script).
    assert area_matches_drift(ci, "ci", "ship", "extra")
    assert not area_matches_drift(ship, "ci", "ship", "extra")
    # the default direction is the config→disk one (missing), preserving the merge-gate routing.
    assert area_matches_drift(ship, "ci", "ship")
    # a non-ci category never matches the ci/ship areas
    assert not area_matches_action(ci, "skills", {})
    assert not area_matches_drift(ship, "skills", "naming")


def test_every_known_category_is_covered_by_some_area():
    """Guard against a HEADLINE/DETAIL divergence: every drift/plan category that the layer
    registry knows about must roll up into some Area. If a new category is added to the codebase
    (and to ``layers._CATEGORY_LAYER``, which AGENTS requires to stay exhaustive) without adding
    it to ``AREAS``, the summary would silently under-count it while the per-item drift dump would
    still show it — exactly the bug this feature exists to prevent. Driving the check off the
    layer registry (the single source of truth for category→layer) makes this fail loudly."""
    from riglib.layers import _CATEGORY_LAYER

    covered = {cat for area in AREAS for cat in area.categories}
    uncovered = set(_CATEGORY_LAYER) - covered
    assert not uncovered, f"categories with no Area (summary would under-count them): {uncovered}"


def test_every_area_category_is_layer_classified():
    """The inverse guard: every category an Area claims must be known to the layer registry, and
    the Area's layer must agree with the registry's classification (no split-brain ownership)."""
    from riglib.layers import _CATEGORY_LAYER, layer_for_category

    for area in AREAS:
        for cat in area.categories:
            assert cat in _CATEGORY_LAYER, f"{area.key} claims unknown category {cat!r}"
            assert layer_for_category(cat) == area.layer, (area.key, cat)


def test_area_dataclass_is_hashable_frozen():
    import dataclasses

    a = Area("x", "X", GLOBAL, ("x",))
    assert {a, a} == {a}  # frozen → hashable, usable in the set ops above
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.label = "mutated"  # type: ignore[misc]


# ── status prints the area summary covering EVERY area (not just skills) ───────────
def test_status_area_summary_lists_all_areas(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """With a representative config, the summary shows every reconciled area's heading — the
    point of the feature: not "mostly skills", every area is visible at a glance."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = _git_repo(tmp_path / "repo")
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {all: true}\nagent_hooks: {all: true}\nmcp: {enabled: false}\n"
        "ci: {enabled: true, all: false, items: {ship: {enabled: true}}}\n",
        encoding="utf-8",
    )
    main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    # a dedicated summary section header
    assert "areas rig manages" in out.lower()
    # every area heading shows — NOT just skills. Spot-check the once-buried ones.
    for label_substr in (
        "skills",
        "agent-hooks",
        "git-hooks dispatcher",
        "CI gates",
        "ship",
        "MCP servers",
        "AGENTS.md",
        "repo settings",
        "harness auto-mode",
        "tmux config",
        "model-freshness cron",
        "tg-ctl",
        "linter / formatter config files",
    ):
        assert label_substr in out, label_substr


def test_status_linters_area_renders_under_repo_section(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """A declared `linters` item shows as drift in the REPO section (docs claim "the repo section")."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = _git_repo(tmp_path / "repo")
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "linters:\n  items:\n    ruff:\n      tool: ruff\n      role: linter\n"
        "      path: ruff.toml\n      content: \"line-length = 100\\n\"\n",
        encoding="utf-8",
    )
    main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    # the REPO area-summary heading carries the linters drift count (1 declared-but-missing).
    repo_summary = out.split("REPO — this repository")[1]
    assert "linter / formatter config files: drift" in repo_summary
    # and the per-item drift dump (also under REPO, never GLOBAL) carries the detail line — the
    # label renders the role ("linter") so the per-item knob is reflected in output.
    assert "linters/linter ruff:ruff: ruff.toml not provisioned" in out


def test_status_area_summary_shows_in_sync_vs_drift_counts(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """Each area heading reports in-sync vs drift: a drifting area says "drift (N)", a configured
    area with no drift says "in sync", an off area says it is not configured."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = _git_repo(tmp_path / "repo")
    # skills declared (will drift: nothing on disk), mcp/github/tmux OFF (not configured).
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {all: true}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n"
        "agents_md: {enabled: false}\ngitignore: {enabled: false}\n",
        encoding="utf-8",
    )
    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == errors.EXIT_DRIFT
    low = out.lower()
    # skills are declared but absent → the summary marks the skills area as drifted with the
    # ACTUAL count. The fake checkout has 3 universal skills, each emitting a copy + a harness-link
    # action (6 declared) and a drift row each (6 missing) → "6 declared-but-missing/modified". Asserting
    # the NUMBER (not just the word "drift") catches a future declared↔drift accounting divergence.
    summary_region = out.split("areas rig manages")[1].split("GLOBAL — machine-wide")[1]
    skills_line = next(ln for ln in summary_region.splitlines() if ln.strip().lower().startswith("skills:"))
    assert "drift (6 declared-but-missing/modified)" in skills_line
    # the count never silently clamps to a nonsense state
    assert "count mismatch" not in low
    # an off area is reported as not-configured (mcp is enabled:false here)
    assert "not configured" in low
    # the two layer groupings frame the summary
    assert "global" in low and "repo" in low


def test_status_area_summary_in_sync_area_marked(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """A configured area with no drift is shown as in-sync in the summary (visible, not omitted)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = _git_repo(tmp_path / "repo")
    # CI ship slot configured AND installed so it is in sync; everything else off.
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n"
        "ci: {enabled: true, all: false, items: {ship: {enabled: true}}}\n"
        "agents_md: {enabled: false}\ngitignore: {enabled: false}\n",
        encoding="utf-8",
    )
    # drive an apply first so the ship gate lands on disk, making it in-sync for status
    apply_rc = main(["apply", "-C", str(repo)])
    assert apply_rc == 0, "apply must succeed for the ship gate to actually land on disk"
    capsys.readouterr()  # drain apply output
    main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    # the ship area shows in-sync (it was applied), proving in-sync areas are surfaced. Anchor on
    # the unambiguous label fragment "merge gate" so an unrelated "ship" substring can't match.
    ship_line = next(ln for ln in out.splitlines() if "merge gate" in ln.lower())
    assert "in sync" in ship_line.lower()


def test_status_area_summary_ci_and_ship_drift_route_to_distinct_areas(
    tmp_path, capsys, fake_agent_tools, monkeypatch
):
    """A drifting ordinary CI gate and the drifting ship gate land in DIFFERENT summary areas —
    exercising the ci/ship split on the DRIFT path end-to-end (the unit test covers the matcher;
    this proves it through real plan + detect)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = _git_repo(tmp_path / "repo")
    # enable an ordinary CI gate (codeql) AND the ship gate, install NEITHER → both drift.
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n"
        "ci: {enabled: true, all: false, items: {codeql: {enabled: true}, ship: {enabled: true}}}\n"
        "agents_md: {enabled: false}\ngitignore: {enabled: false}\n",
        encoding="utf-8",
    )
    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == errors.EXIT_DRIFT
    summary = out.split("areas rig manages")[1].split("REPO — this repository")[1]
    ci_line = next(ln for ln in summary.splitlines() if ln.strip().startswith("CI gates:"))
    ship_line = next(ln for ln in summary.splitlines() if "merge gate" in ln.lower())
    # the ordinary CI gate drifts under "CI gates", the ship gate under its own area — not merged
    assert "drift (1 declared-but-missing/modified)" in ci_line
    assert "drift (1 declared-but-missing/modified)" in ship_line


def test_status_area_summary_ci_extra_routes_to_ci_not_ship(
    tmp_path, capsys, fake_agent_tools, monkeypatch
):
    """A disk→config EXTRA ci workflow (an undeclared .yml on disk) routes to the "CI gates" area,
    never "ship": `_extras_ci` names the drift item by the workflow's filename stem (never the
    literal "ship", which is a ~/bin script, not a workflows/*.yml), so the ci/ship split holds on
    the EXTRA direction too (the review asked to confirm the split for disk→config drift)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = _git_repo(tmp_path / "repo")
    # an undeclared workflow on disk while ci is enabled-but-empty → a ci EXTRA (disk→config).
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "rogue.yml").write_text("name: rogue\n", encoding="utf-8")
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: true, all: false}\n"
        "agents_md: {enabled: false}\ngitignore: {enabled: false}\n",
        encoding="utf-8",
    )
    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == errors.EXIT_DRIFT
    summary = out.split("areas rig manages")[1].split("REPO — this repository")[1]
    ci_line = next(ln for ln in summary.splitlines() if ln.strip().startswith("CI gates:"))
    ship_line = next(ln for ln in summary.splitlines() if "merge gate" in ln.lower())
    # the extra lands under CI gates, NOT ship
    assert "on-disk-not-declared" in ci_line
    assert "on-disk-not-declared" not in ship_line


def test_status_area_summary_rogue_ship_yml_extra_routes_to_ci_not_ship(
    tmp_path, capsys, fake_agent_tools, monkeypatch
):
    """The COLLISION case: an undeclared `.github/workflows/ship.yml` on disk produces a ci extra
    with item == "ship" (its filename stem), but it is a WORKFLOW, not the ~/bin ship merge-gate
    script. The direction-aware split must route it to "CI gates", never the ship area (the
    review's specific worry that the `rogue.yml` test didn't exercise)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = _git_repo(tmp_path / "repo")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ship.yml").write_text("name: ship\n", encoding="utf-8")
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: true, all: false}\n"
        "agents_md: {enabled: false}\ngitignore: {enabled: false}\n",
        encoding="utf-8",
    )
    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == errors.EXIT_DRIFT
    summary = out.split("areas rig manages")[1].split("REPO — this repository")[1]
    ci_line = next(ln for ln in summary.splitlines() if ln.strip().startswith("CI gates:"))
    ship_line = next(ln for ln in summary.splitlines() if "merge gate" in ln.lower())
    # the rogue ship.yml is a CI-gates extra, NOT the ship merge gate
    assert "on-disk-not-declared" in ci_line
    assert "on-disk-not-declared" not in ship_line


def test_status_area_summary_partial_drift_reports_only_drifting_items(
    tmp_path, capsys, fake_agent_tools, monkeypatch
):
    """When an area has SOME items installed and SOME missing, the line reports the partial drift
    count (only the missing ones), not the whole area as drifted and not a fragile in-sync count."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = _git_repo(tmp_path / "repo")
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {all: true}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n"
        "agents_md: {enabled: false}\ngitignore: {enabled: false}\n",
        encoding="utf-8",
    )
    # apply (all 3 skills land in sync), then DELETE one skill's installed copy so ONLY it drifts.
    assert main(["apply", "-C", str(repo)]) == 0
    capsys.readouterr()
    import os as _os
    import shutil

    home = Path(_os.environ["HOME"])
    installed = home / ".agents" / "skills"
    victims = sorted(p for p in installed.iterdir() if p.is_dir())
    assert len(victims) == 3, "apply should have installed the 3 fake universal skills"
    shutil.rmtree(victims[0])  # one skill copy gone → exactly that copy drifts (1 missing)
    main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    summary = out.split("areas rig manages")[1].split("GLOBAL — machine-wide")[1]
    skills_line = next(ln for ln in summary.splitlines() if ln.strip().lower().startswith("skills:"))
    # a PARTIAL drift: exactly the one removed skill's copy is missing — not all 6 rows.
    assert "drift (1 declared-but-missing/modified)" in skills_line
    # the count never silently clamps or produces a nonsense state
    assert "count mismatch" not in out.lower()


# ── tg-ctl is platform-gated: off macOS the summary says "unsupported", not a false "in sync" ──
def test_status_area_summary_tg_ctl_unsupported_off_darwin(
    tmp_path, capsys, fake_agent_tools, monkeypatch
):
    """Off macOS the tg-ctl provisioner is a no-op, so detect() reports no drift even though
    nothing is installed. The summary must NOT then claim "in sync" — it says "unsupported",
    matching the dedicated tg-ctl detail line (codex review finding)."""
    import riglib.drift as drift

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setattr(drift, "_on_darwin", lambda: False)  # force the off-Darwin path
    repo = _git_repo(tmp_path / "repo")
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n"
        "agents_md: {enabled: false}\ngitignore: {enabled: false}\n",
        encoding="utf-8",
    )
    main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    summary = out.split("areas rig manages")[1].split("GLOBAL — machine-wide")[1]
    tg_line = next(ln for ln in summary.splitlines() if "tg-ctl" in ln.lower())
    assert "unsupported" in tg_line.lower()
    assert "in sync" not in tg_line.lower()  # the false-positive the finding flagged


def test_status_area_summary_tg_ctl_disabled_off_darwin_is_not_configured(
    tmp_path, capsys, fake_agent_tools, monkeypatch
):
    """A DISABLED tg_ctl off macOS must read "not configured" like any other off area — the
    "unsupported" platform note only applies once tg_ctl is actually turned on (codex P3: the
    early platform return must not fire for an unconfigured area)."""
    import riglib.drift as drift

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setattr(drift, "_on_darwin", lambda: False)
    repo = _git_repo(tmp_path / "repo")
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n"
        "agents_md: {enabled: false}\ngitignore: {enabled: false}\n"
        "tg_ctl: {enabled: false}\n",  # explicitly OFF
        encoding="utf-8",
    )
    main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    summary = out.split("areas rig manages")[1].split("GLOBAL — machine-wide")[1]
    tg_line = next(ln for ln in summary.splitlines() if "tg-ctl" in ln.lower())
    assert "not configured" in tg_line.lower()
    assert "unsupported" not in tg_line.lower()  # platform note must not fire for an off area


# ── harness area: action category and drift category agree (no false "in sync") ────
def test_status_area_summary_harness_drift_not_false_in_sync(
    tmp_path, capsys, fake_agent_tools, monkeypatch
):
    """The harness area folds two distinct actions (apply_harness + register_hook_bridge), both
    category="harness". A configured harness whose settings file is absent MUST show drift, never
    a false "in sync" — guarding the action↔drift category coherence the summary relies on (Opus
    review: the area most likely to diverge because it merges heterogeneous actions)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # empty HOME → no harness settings.json
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = _git_repo(tmp_path / "repo")
    # harness on + agent_hooks on → both apply_harness AND register_hook_bridge actions exist;
    # nothing on disk → harness drift. Everything else off to isolate the harness line.
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {all: true}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n"
        "agents_md: {enabled: false}\ngitignore: {enabled: false}\n"
        "harness: {enabled: true, kind: claude-code, auto_mode: true}\n",
        encoding="utf-8",
    )
    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == errors.EXIT_DRIFT
    summary = out.split("areas rig manages")[1].split("REPO — this repository")[0]
    harness_line = next(ln for ln in summary.splitlines() if "harness" in ln.lower())
    # configured + on-disk-absent → drift, NOT a false in-sync
    assert "drift" in harness_line.lower()
    assert "in sync" not in harness_line.lower()
    assert "not configured" not in harness_line.lower()


# ── a MODIFIED-on-disk item counts as config→disk drift (not just "missing") ───────
def test_status_area_summary_counts_modified_items(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """A declared item PRESENT on disk but CHANGED is config→disk drift too (direction
    "modified"). The summary folds it into the drift count under the missing/modified label —
    the "modified" branch was previously untested (Opus review)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = _git_repo(tmp_path / "repo")
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {all: true, harness_link: false}\nagent_hooks: {enabled: false}\n"
        "mcp: {enabled: false}\ngit_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n"
        "agents_md: {enabled: false}\ngitignore: {enabled: false}\n",
        encoding="utf-8",
    )
    # apply all 3 skills in sync (harness_link off → exactly one copy action per skill, no link
    # rows), then OVERWRITE one installed skill's content so it drifts as "modified", not missing.
    assert main(["apply", "-C", str(repo)]) == 0
    capsys.readouterr()
    import os as _os

    installed = Path(_os.environ["HOME"]) / ".agents" / "skills"
    victim = sorted(p for p in installed.iterdir() if p.is_dir())[0]
    (victim / "SKILL.md").write_text("---\nname: tampered\n---\nlocally edited\n", encoding="utf-8")
    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == errors.EXIT_DRIFT
    summary = out.split("areas rig manages")[1].split("GLOBAL — machine-wide")[1]
    skills_line = next(ln for ln in summary.splitlines() if ln.strip().lower().startswith("skills:"))
    # exactly the one tampered skill drifts (modified), counted under the missing/modified label
    assert "drift (1 declared-but-missing/modified)" in skills_line
    # the detailed dump names it as a modified item (proving the direction is "modified")
    assert "modified" in out.lower()


def test_status_area_summary_gitignore_in_sync_after_apply(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """The gitignore area is ONE plan action but TWO drift checks (the excludesfile setting + the
    managed block). After a successful apply both checks pass, so the summary reads "in sync" —
    proving the configured/in-sync path for an area whose action↔check counts differ (Opus
    flagged this as the area most prone to a configured-vs-drift mismatch)."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    # a private global excludes file so apply has somewhere to write, isolated from the real one.
    excludes = home / ".config" / "git" / "ignore"
    repo = _git_repo(tmp_path / "repo")
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n"
        "agents_md: {enabled: false}\n"
        f"gitignore: {{enabled: true, excludesfile: {excludes}}}\n",
        encoding="utf-8",
    )
    assert main(["apply", "-C", str(repo)]) == 0
    capsys.readouterr()
    main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    summary = out.split("areas rig manages")[1].split("GLOBAL — machine-wide")[1]
    gi_line = next(ln for ln in summary.splitlines() if "gitignore" in ln.lower())
    # both the excludesfile setting AND the managed block landed → the single area reads in sync
    assert "in sync" in gi_line.lower()
    assert "count mismatch" not in out.lower()


# ── the summary is gated to git repos; a non-git dir shows GLOBAL areas only ───────
def test_status_area_summary_non_git_shows_global_only(tmp_path, capsys, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    rc = main(["status", "-C", str(plain)])
    out = capsys.readouterr().out
    assert rc in (0, errors.EXIT_DRIFT)
    low = out.lower()
    assert "not a git repository" in low
    # the GLOBAL areas DO appear (the summary still gives the machine-wide picture)
    assert "areas rig manages" in low
    assert "skills" in low
    assert "agent-hooks" in low
    # the REPO-only areas must NOT appear (no repo layer in a non-git dir)
    assert "repo settings" not in low
    assert "ci gates" not in low
    assert "merge gate" not in low
    # the REPO layer heading itself is suppressed in the summary for a non-git dir
    assert "repo — this repository" not in low
