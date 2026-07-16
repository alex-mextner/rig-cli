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


# --- init stack resolution (headless + interactive wizard share ONE cascade) ---


def test_resolve_init_stack_explicit_wins(tmp_path) -> None:
    from riglib.config import resolve_init_stack

    # explicit --stack beats both the global default and any repo-file detection
    (tmp_path / "package.json").write_text('{"dependencies":{"react":"18"}}', encoding="utf-8")
    got = resolve_init_stack(
        tmp_path, explicit="backend/python", global_stack="mobile/swift"
    )
    assert got == "backend/python"


def test_resolve_init_stack_global_over_detection(tmp_path) -> None:
    from riglib.config import resolve_init_stack

    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    assert resolve_init_stack(tmp_path, global_stack="frontend/ts/react") == "frontend/ts/react"


def test_resolve_init_stack_falls_back_to_detection(tmp_path) -> None:
    from riglib.config import resolve_init_stack

    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    assert resolve_init_stack(tmp_path) == "backend/go"


def test_resolve_init_stack_none_when_unknown(tmp_path) -> None:
    from riglib.config import resolve_init_stack

    assert resolve_init_stack(tmp_path) is None


def test_wizard_initial_state_seeds_detected_stack(tmp_path, monkeypatch) -> None:
    # The canonical interactive path (`rig init` in a TTY) must open the wizard with the
    # detected stack already selected, so Export/Apply writes a rig.yaml carrying it.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    # a React/TS repo → frontend/ts/react
    (repo / "package.json").write_text('{"dependencies":{"react":"18"}}', encoding="utf-8")
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")

    from riglib.detect import detect_environment
    from riglib.tui.app import _initial_wizard_state

    env = detect_environment(repo)
    state = _initial_wizard_state(env)
    assert state.data.get("stack") == "frontend/ts/react"
    # portability invariant preserved: the auto-detected source is never pinned
    assert "agent_tools_source" not in state.data or state.data["agent_tools_source"] is None


def test_wizard_initial_state_explicit_stack_beats_detection(tmp_path, monkeypatch) -> None:
    # an explicit --stack threaded into the wizard must win over repo-file detection
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module x\n", encoding="utf-8")  # would detect backend/go

    from riglib.detect import detect_environment
    from riglib.tui.app import _initial_wizard_state

    env = detect_environment(repo)
    state = _initial_wizard_state(env, explicit_stack="backend/python")
    assert state.data.get("stack") == "backend/python"


def test_wizard_global_stack_ignores_repo_layer(tmp_path, monkeypatch) -> None:
    # _global_stack must read only the GLOBAL layer: an existing repo rig.yaml must NOT
    # shadow the global default, and a MALFORMED repo rig.yaml must NOT raise (it would
    # otherwise stop the wizard opening — the very thing needed to fix that file).
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    (home / ".config" / "rig").mkdir(parents=True)
    (home / ".config" / "rig" / "config.yaml").write_text(
        "stack: frontend/ts/react\n", encoding="utf-8"
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    # a garbage repo rig.yaml that would fail-closed if the repo layer were loaded
    (repo / "rig.yaml").write_text("stack: [not, a, string]\n:::bad", encoding="utf-8")

    from riglib.tui.app import _global_stack

    assert _global_stack(repo) == "frontend/ts/react"
