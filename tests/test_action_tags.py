"""The action-tag taxonomy (riglib/action_tags.py) must stay in lockstep with the REAL action
kinds the shared engine can execute (riglib.actions.runner._HANDLERS) -- rig-cli#310's core
safety property for the plan-preview tag system: a newly added action kind can never ship to the
web UI untagged.
"""

from __future__ import annotations

from riglib import action_tags
from riglib.actions import runner


def test_every_handler_kind_has_a_tag():
    handler_kinds = set(runner._HANDLERS.keys())
    tagged_kinds = set(action_tags.ACTION_TAGS.keys())
    missing = handler_kinds - tagged_kinds
    assert not missing, f"action kinds with no tag in action_tags.ACTION_TAGS: {sorted(missing)}"


def test_no_stale_tags_for_kinds_that_no_longer_exist():
    handler_kinds = set(runner._HANDLERS.keys())
    tagged_kinds = set(action_tags.ACTION_TAGS.keys())
    stale = tagged_kinds - handler_kinds
    assert not stale, f"action_tags.ACTION_TAGS has tags for removed kinds: {sorted(stale)}"


def test_every_tag_has_a_real_category_and_nonempty_detail():
    for kind, tag in action_tags.ACTION_TAGS.items():
        assert tag.kind == kind
        assert tag.category in action_tags.CATEGORIES, f"{kind}: unknown category {tag.category!r}"
        assert tag.audience in (
            action_tags.AUDIENCE_AGENT,
            action_tags.AUDIENCE_HUMAN,
            action_tags.AUDIENCE_BOTH,
        )
        assert tag.detail.strip(), f"{kind}: empty detail"
        assert tag.label == action_tags.CATEGORIES[tag.category]


def test_tag_for_kind_returns_registered_tag():
    tag = action_tags.tag_for_kind("copy_skill")
    assert tag.category == "creates_file"
    assert tag.audience == action_tags.AUDIENCE_AGENT


def test_tag_for_kind_falls_back_for_unknown_kind_without_crashing():
    tag = action_tags.tag_for_kind("some_future_kind_not_yet_registered")
    assert tag.kind == "some_future_kind_not_yet_registered"
    assert tag.category in action_tags.CATEGORIES
