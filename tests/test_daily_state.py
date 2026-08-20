"""The per-repo watermarks: default {}, round-trip, tolerate a corrupt file/entry,
write atomically."""

from __future__ import annotations

import os

from riglib.daily.state import load_watermarks, save_watermarks


def test_no_state_file_returns_empty(tmp_path):
    assert load_watermarks(tmp_path / "nope.json") == {}


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "daily-state.json"
    save_watermarks({"owner/a": "2026-08-19T13:53:08Z", "owner/b": "2026-08-18T00:00:00Z"}, path=path)
    assert load_watermarks(path) == {
        "owner/a": "2026-08-19T13:53:08Z",
        "owner/b": "2026-08-18T00:00:00Z",
    }


def test_corrupt_state_file_degrades_to_empty(tmp_path):
    path = tmp_path / "daily-state.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_watermarks(path) == {}


def test_non_dict_state_file_degrades_to_empty(tmp_path):
    path = tmp_path / "daily-state.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_watermarks(path) == {}


def test_non_dict_repos_key_degrades_to_empty(tmp_path):
    path = tmp_path / "daily-state.json"
    path.write_text('{"repos": "not-a-dict"}', encoding="utf-8")
    assert load_watermarks(path) == {}


def test_one_malformed_repo_entry_does_not_blind_the_others(tmp_path):
    path = tmp_path / "daily-state.json"
    path.write_text(
        '{"repos": {"owner/good": "2026-08-19T00:00:00Z", "owner/bad": 12345, "owner/empty": ""}}',
        encoding="utf-8",
    )
    assert load_watermarks(path) == {"owner/good": "2026-08-19T00:00:00Z"}


def test_save_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "dir" / "daily-state.json"
    save_watermarks({"owner/a": "2026-08-19T00:00:00Z"}, path=path)
    assert path.is_file()


def test_save_leaves_no_stray_temp_file_behind(tmp_path):
    path = tmp_path / "daily-state.json"
    save_watermarks({"owner/a": "2026-08-19T00:00:00Z"}, path=path)
    leftover_tmp = [p.name for p in tmp_path.iterdir() if p.name.startswith(f".{path.name}.")]
    assert leftover_tmp == []


def test_save_is_atomic_old_content_survives_a_failed_write(tmp_path, monkeypatch):
    """Regression for the codex review P1 finding: a plain in-place `write_text` would
    truncate the file before writing the new content, so a crash mid-write leaves
    invalid JSON — `load_watermarks` degrades that to "no state at all" and every repo
    silently re-reports from its default lookback. Writing to a temp file first and
    `os.replace`-ing it means a failure never touches the original file."""
    path = tmp_path / "daily-state.json"
    save_watermarks({"owner/a": "2026-08-19T00:00:00Z"}, path=path)

    def _boom(*args, **kwargs):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(os, "replace", _boom)
    try:
        save_watermarks({"owner/a": "2026-08-20T00:00:00Z"}, path=path)
    except OSError:
        pass
    # The original, still-valid content survives the failed write.
    assert load_watermarks(path) == {"owner/a": "2026-08-19T00:00:00Z"}
    # No leftover temp file from the failed attempt.
    leftover_tmp = [p.name for p in tmp_path.iterdir() if p.name.startswith(f".{path.name}.")]
    assert leftover_tmp == []
