"""`gh pr list` parsing + the exclusive-since / truncation-warning logic — no real `gh` call."""

from __future__ import annotations

import json
import subprocess

import pytest

from riglib.daily import github as daily_github
from riglib.daily.timeutil import parse_utc


class _FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _stub_gh(monkeypatch, items: list[dict], returncode: int = 0, stderr: str = ""):
    def _fake_run(*args, **kwargs):
        return _FakeCompleted(json.dumps(items), returncode=returncode, stderr=stderr)

    monkeypatch.setattr(daily_github.subprocess, "run", _fake_run)


def test_filters_by_since_exclusive(monkeypatch):
    _stub_gh(monkeypatch, [
        {"number": 1, "title": "a", "body": "", "mergedAt": "2026-08-19T10:00:00Z", "url": "", "labels": []},
        {"number": 2, "title": "b", "body": "", "mergedAt": "2026-08-19T09:00:00Z", "url": "", "labels": []},
    ])
    since = parse_utc("2026-08-19T09:00:00Z")
    result = daily_github.fetch_merged_prs("owner/repo", since)
    # PR #2 merged EXACTLY at `since` must be excluded (exclusive) — this is the
    # invariant that keeps a re-run from double-reporting the PR that set the watermark.
    assert [p.number for p in result.prs] == [1]
    assert result.complete is True


def test_gh_not_found_raises_gh_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(daily_github.subprocess, "run", _raise)
    with pytest.raises(daily_github.GhError, match="not found"):
        daily_github.fetch_merged_prs("owner/repo", parse_utc("2026-08-19T00:00:00Z"))


def test_gh_nonzero_exit_raises_gh_error(monkeypatch):
    _stub_gh(monkeypatch, [], returncode=1, stderr="repo not found")
    with pytest.raises(daily_github.GhError, match="repo not found"):
        daily_github.fetch_merged_prs("owner/repo", parse_utc("2026-08-19T00:00:00Z"))


def test_non_list_json_root_raises_gh_error_not_a_crash(monkeypatch):
    """`gh pr list --json ...` always emits a JSON ARRAY — a non-list root (e.g. an
    error object from a `gh` version/flag mismatch) must be treated the same as any
    other unreadable response, not crash `_page_may_be_truncated`'s `raw[-1]` past
    `command.py`'s per-repo `except GhError`. Regression for the codex review P1
    finding, round 5."""

    def _fake_run(*args, **kwargs):
        return _FakeCompleted(json.dumps({"error": "unexpected"}))

    monkeypatch.setattr(daily_github.subprocess, "run", _fake_run)
    with pytest.raises(daily_github.GhError, match="non-list JSON root"):
        daily_github.fetch_merged_prs("owner/repo", parse_utc("2026-08-19T00:00:00Z"))


def test_timeout_raises_gh_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=30)

    monkeypatch.setattr(daily_github.subprocess, "run", _raise)
    with pytest.raises(daily_github.GhError, match="timed out"):
        daily_github.fetch_merged_prs("owner/repo", parse_utc("2026-08-19T00:00:00Z"))


def test_full_page_still_in_window_warns_and_is_incomplete(monkeypatch, capsys):
    items = [
        {"number": i, "title": "x", "body": "", "mergedAt": "2026-08-19T10:00:00Z", "url": "", "labels": []}
        for i in range(daily_github._PAGE_LIMIT)
    ]
    _stub_gh(monkeypatch, items)
    result = daily_github.fetch_merged_prs("owner/repo", parse_utc("2026-08-01T00:00:00Z"))
    assert "warning" in capsys.readouterr().err
    # A possibly-truncated page must be flagged incomplete so the caller withholds the
    # watermark advance — see test_daily_command.py for the end-to-end contract.
    assert result.complete is False


def test_full_page_already_aged_past_window_does_not_warn(monkeypatch, capsys):
    # Oldest (last) entry on the page merged well BEFORE `since` -> the page has already
    # paged past the requested window; hitting the limit here is not truncation.
    items = [
        {"number": i, "title": "x", "body": "", "mergedAt": "2020-01-01T00:00:00Z", "url": "", "labels": []}
        for i in range(daily_github._PAGE_LIMIT)
    ]
    _stub_gh(monkeypatch, items)
    result = daily_github.fetch_merged_prs("owner/repo", parse_utc("2026-08-01T00:00:00Z"))
    assert capsys.readouterr().err == ""
    assert result.complete is True


def test_malformed_record_is_skipped_not_a_crash_and_marks_incomplete(monkeypatch, capsys):
    """A single bad remote record (missing/garbage `number`) must not take down the
    whole fetch — the other, well-formed records in the same page still come through.
    But the skipped record COULD have belonged in the window, so the result must be
    marked incomplete (never advance the watermark past an unknown omission).
    Regression for the codex review P2 finding (round 3) + P1 finding (round 4)."""
    _stub_gh(monkeypatch, [
        {"number": "not-a-number", "title": "bad", "body": "", "mergedAt": "2026-08-19T10:00:00Z", "url": "", "labels": []},
        {"number": 2, "title": "good", "body": "", "mergedAt": "2026-08-19T11:00:00Z", "url": "", "labels": []},
    ])
    result = daily_github.fetch_merged_prs("owner/repo", parse_utc("2026-08-19T00:00:00Z"))
    assert [p.number for p in result.prs] == [2]
    assert result.complete is False
    assert "warning" in capsys.readouterr().err


def test_non_dict_record_is_skipped_not_a_crash_and_marks_incomplete(monkeypatch, capsys):
    _stub_gh(monkeypatch, [
        "not-even-a-dict",
        {"number": 2, "title": "good", "body": "", "mergedAt": "2026-08-19T11:00:00Z", "url": "", "labels": []},
    ])
    result = daily_github.fetch_merged_prs("owner/repo", parse_utc("2026-08-19T00:00:00Z"))
    assert [p.number for p in result.prs] == [2]
    assert result.complete is False
    assert "warning" in capsys.readouterr().err


def test_unparseable_merged_at_is_skipped_not_a_crash_and_marks_incomplete(monkeypatch, capsys):
    _stub_gh(monkeypatch, [
        {"number": 1, "title": "bad", "body": "", "mergedAt": "not-a-timestamp", "url": "", "labels": []},
        {"number": 2, "title": "good", "body": "", "mergedAt": "2026-08-19T11:00:00Z", "url": "", "labels": []},
    ])
    result = daily_github.fetch_merged_prs("owner/repo", parse_utc("2026-08-19T00:00:00Z"))
    assert [p.number for p in result.prs] == [2]
    assert result.complete is False
    assert "warning" in capsys.readouterr().err


def test_malformed_last_item_in_full_page_does_not_crash_truncation_check(monkeypatch, capsys):
    """The truncation heuristic reads the LAST item's `mergedAt` directly — if that item
    is itself malformed, it must degrade to "assume truncated", not raise past
    `fetch_merged_prs` (codex review P2 finding, round 4)."""
    items = [
        {"number": i, "title": "x", "body": "", "mergedAt": "2026-08-19T10:00:00Z", "url": "", "labels": []}
        for i in range(daily_github._PAGE_LIMIT - 1)
    ]
    items.append("not-even-a-dict")  # the malformed LAST item
    _stub_gh(monkeypatch, items)
    result = daily_github.fetch_merged_prs("owner/repo", parse_utc("2026-08-01T00:00:00Z"))
    assert result.complete is False
    assert "warning" in capsys.readouterr().err
