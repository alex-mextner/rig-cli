"""Unit tests for the stack-preset taxonomy (riglib/stack.py)."""

from __future__ import annotations

import pytest

from riglib import stack


@pytest.mark.parametrize(
    "value,expected",
    [
        ("mobile/swift/swiftui", ("mobile", "swift", "swiftui")),
        ("frontend/ts/react", ("frontend", "ts", "react")),
        ("backend/python", ("backend", "python")),
        ("  backend/go  ", ("backend", "go")),  # trimmed
        ("system/rust", ("system", "rust")),
    ],
)
def test_parse_stack_valid(value: str, expected: tuple[str, ...]) -> None:
    assert stack.parse_stack(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "mobile",  # only l1
        "mobile/swift/swiftui/extra",  # 4 segments
        "mobile//swiftui",  # empty middle
        "/swift/ui",  # empty l1
        "backend/python/",  # trailing empty
        "web/ts/react",  # bad l1 (not in enum)
    ],
)
def test_parse_stack_invalid(value: str) -> None:
    with pytest.raises(stack.StackError):
        stack.parse_stack(value)


def test_is_valid_stack() -> None:
    assert stack.is_valid_stack("backend/python") is True
    assert stack.is_valid_stack("nope/x") is False


def test_normalize_stack() -> None:
    assert stack.normalize_stack("  mobile/swift/swiftui ") == "mobile/swift/swiftui"


def test_open_vocabulary_lang_and_framework() -> None:
    # unknown lang/framework are accepted (open vocabulary) as long as l1 is valid
    assert stack.parse_stack("backend/zig") == ("backend", "zig")
    assert stack.parse_stack("mobile/kotlin/compose") == ("mobile", "kotlin", "compose")


@pytest.mark.parametrize(
    "declared,item,expected",
    [
        # exact + prefix inheritance (items are l1/lang minimum, like declared stacks)
        ("mobile/swift/swiftui", "mobile/swift", True),
        ("mobile/swift/swiftui", "mobile/swift/swiftui", True),
        # a deeper item than the declared stack does NOT match
        ("mobile/swift", "mobile/swift/swiftui", False),
        # a sibling framework/lang does NOT match (react not for a swift repo)
        ("mobile/swift/swiftui", "frontend/ts/react", False),
        ("mobile/swift/swiftui", "mobile/kotlin", False),
        # backend without framework
        ("backend/python", "backend/python", True),
        ("backend/python", "backend/go", False),
        # malformed on either side → non-match, never raises
        ("garbage", "mobile", False),
        ("mobile/swift", "garbage", False),
    ],
)
def test_stack_matches(declared: str, item: str, expected: bool) -> None:
    assert stack.stack_matches(declared, item) is expected
