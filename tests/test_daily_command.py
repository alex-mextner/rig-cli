"""End-to-end `rig daily` orchestration: per-repo window resolution, per-repo watermark
advance, dedup, --since read-only, --dry-run, all-repos-failed. Fetch is stubbed — no
real `gh` call."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import pytest

from riglib.daily import command as daily_command
from riglib.daily.github import FetchResult
from riglib.daily.model import MergedPR
from riglib.daily.state import load_watermarks

# Fixed "now" so the default 24h-lookback window is deterministic regardless of the real
# wall clock at test-run time — every fixture PR below is merged well within 24h of this.
_FIXED_NOW = datetime(2026, 8, 19, 23, 0, 0, tzinfo=timezone.utc)


def _args(**overrides) -> argparse.Namespace:
    base = dict(since=None, repo=["owner/repo"], config=None, dry_run=False, state=None, action=None)
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    monkeypatch.setattr(daily_command, "now_utc", lambda: _FIXED_NOW)


def _pr(number: int, merged_at: str, repo: str = "owner/repo") -> MergedPR:
    return MergedPR(repo=repo, number=number, title=f"feat: item {number} (#{number})",
                     body="", merged_at=merged_at, url="")


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "daily-state.json"


def _stub_fetch(monkeypatch, prs_by_call, *, complete: bool = True):
    """``prs_by_call`` — a list of PR lists; each `fetch_merged_prs` call pops the next
    entry and wraps it as a :class:`FetchResult` (pass ``complete=False`` to simulate
    every call returning a possibly-truncated page)."""
    calls = list(prs_by_call)

    def _fake(repo, since):
        prs = calls.pop(0) if calls else []
        return FetchResult(prs=prs, complete=complete)

    monkeypatch.setattr(daily_command, "fetch_merged_prs", _fake)


def test_first_run_advances_watermark_to_max_merged_at(monkeypatch, state_path, capsys):
    _stub_fetch(monkeypatch, [[_pr(1, "2026-08-19T10:00:00Z"), _pr(2, "2026-08-19T12:00:00Z")]])
    rc = daily_command.run(_args(state=str(state_path)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Item 1" in out and "Item 2" in out
    assert load_watermarks(state_path) == {"owner/repo": "2026-08-19T12:00:00Z"}


def test_second_run_with_no_new_prs_reports_nothing_and_never_double_reports(monkeypatch, state_path, capsys):
    _stub_fetch(monkeypatch, [[_pr(1, "2026-08-19T10:00:00Z")]])
    daily_command.run(_args(state=str(state_path)))
    capsys.readouterr()

    _stub_fetch(monkeypatch, [[]])  # nothing new since the watermark
    daily_command.run(_args(state=str(state_path)))
    assert capsys.readouterr().out.strip() == "No merged PRs to report."


def test_quiet_complete_run_still_advances_watermark_to_now(monkeypatch, state_path):
    """A repo with a complete (non-truncated) fetch that found zero PRs is proven clean
    up to "now" — it must still advance the watermark, not leave the repo with none at
    all. Otherwise a delayed next run (>24h later) falls back to the rolling default
    lookback and can miss whatever merged in the gap. Regression for the codex review
    P2 finding, round 3."""
    _stub_fetch(monkeypatch, [[]])
    rc = daily_command.run(_args(state=str(state_path)))
    assert rc == 0
    assert load_watermarks(state_path) == {"owner/repo": daily_command.to_utc_iso(_FIXED_NOW)}


def test_explicit_since_never_writes_state(monkeypatch, state_path):
    _stub_fetch(monkeypatch, [[_pr(1, "2026-08-19T10:00:00Z")]])
    daily_command.run(_args(since="2026-08-01T00:00:00Z", state=str(state_path)))
    assert load_watermarks(state_path) == {}


def test_dry_run_never_writes_state(monkeypatch, state_path):
    _stub_fetch(monkeypatch, [[_pr(1, "2026-08-19T10:00:00Z")]])
    daily_command.run(_args(dry_run=True, state=str(state_path)))
    assert load_watermarks(state_path) == {}


def test_relative_since_window(monkeypatch):
    since = daily_command._parse_since_arg("24h")
    now = daily_command.now_utc()
    assert (now - since).total_seconds() == pytest.approx(24 * 3600, abs=5)


def test_invalid_since_raises_structured_config_error_not_a_raw_crash():
    """A garbage `--since` value must surface as rig's own structured `ConfigError`
    (exit 2, what/why/fix) — not an unhandled `ValueError` traceback. Regression for the
    codex review P2 finding, round 4."""
    from riglib.errors import ConfigError

    with pytest.raises(ConfigError) as exc_info:
        daily_command._parse_since_arg("not-a-timestamp-or-window")
    assert exc_info.value.exit_code == 2
    assert "not-a-timestamp-or-window" in exc_info.value.what


def test_gh_error_on_one_repo_does_not_abort_others(monkeypatch, capsys):
    def _fake(repo, since):
        if repo == "owner/broken":
            raise daily_command.GhError("boom")
        return FetchResult(prs=[_pr(1, "2026-08-19T10:00:00Z")], complete=True)

    monkeypatch.setattr(daily_command, "fetch_merged_prs", _fake)
    rc = daily_command.run(_args(repo=["owner/broken", "owner/repo"], state=None))
    assert rc == 0
    captured = capsys.readouterr()
    assert "Item 1" in captured.out
    assert "boom" in captured.err


def test_gh_error_on_one_repo_withholds_only_that_repos_watermark(monkeypatch, state_path):
    """A repo that failed to fetch must not get its watermark advanced — but a repo that
    DID succeed should still advance normally; per-repo cursors mean one repo's outage
    never blocks another repo's progress. Regression for the codex review P1 finding
    (round 1: a single global gate over-withheld; round 2: a single global scalar was
    itself wrong the moment repos differ)."""

    def _fake(repo, since):
        if repo == "owner/broken":
            raise daily_command.GhError("boom")
        return FetchResult(prs=[_pr(1, "2026-08-19T10:00:00Z", repo="owner/good")], complete=True)

    monkeypatch.setattr(daily_command, "fetch_merged_prs", _fake)
    rc = daily_command.run(_args(repo=["owner/broken", "owner/good"], state=str(state_path)))
    assert rc == 0
    assert load_watermarks(state_path) == {"owner/good": "2026-08-19T10:00:00Z"}


def test_incomplete_page_withholds_watermark(monkeypatch, state_path, capsys):
    """A repo whose page may be truncated (github.py's own truncation warning) must also
    withhold the watermark advance — reporting from a possibly-incomplete page is fine,
    but persisting a watermark derived from it would permanently hide whatever the page
    missed. Regression for the codex review P1 finding on github.py + command.py."""
    _stub_fetch(monkeypatch, [[_pr(1, "2026-08-19T10:00:00Z")]], complete=False)
    rc = daily_command.run(_args(state=str(state_path)))
    assert rc == 0
    assert load_watermarks(state_path) == {}


def test_adding_a_new_repo_does_not_lose_its_pre_existing_watermark_repo(monkeypatch, state_path):
    """Regression for the codex review P1 finding (round 2): a single SHARED watermark
    used to apply to every repo, so adding a repo after the first run would silently
    skip all of ITS PRs merged before the older repos' watermark. Per-repo cursors mean
    a newly-added repo starts from "no watermark" (default lookback) on its own, never
    borrowing an unrelated repo's history."""
    _stub_fetch(monkeypatch, [[_pr(1, "2026-08-19T10:00:00Z", repo="owner/old")]])
    daily_command.run(_args(repo=["owner/old"], state=str(state_path)))
    assert load_watermarks(state_path) == {"owner/old": "2026-08-19T10:00:00Z"}

    # owner/new is added to the config now; it has never been fetched before, so it must
    # NOT inherit owner/old's watermark — it should use the default lookback instead.
    seen_since: dict[str, object] = {}

    def _fake(repo, since):
        seen_since[repo] = since
        if repo == "owner/new":
            return FetchResult(prs=[_pr(2, "2026-08-19T09:00:00Z", repo="owner/new")], complete=True)
        return FetchResult(prs=[], complete=True)

    monkeypatch.setattr(daily_command, "fetch_merged_prs", _fake)
    rc = daily_command.run(_args(repo=["owner/old", "owner/new"], state=str(state_path)))
    assert rc == 0
    assert seen_since["owner/new"] != seen_since["owner/old"]
    # owner/old found nothing new this run (a complete, quiet fetch) — its watermark
    # advances to "now", same as a quiet run for a single repo (see
    # test_quiet_complete_run_still_advances_watermark_to_now). owner/new gets its
    # first-ever watermark, from the PR it actually found.
    assert load_watermarks(state_path) == {
        "owner/old": daily_command.to_utc_iso(_FIXED_NOW),
        "owner/new": "2026-08-19T09:00:00Z",
    }


def test_malformed_saved_watermark_falls_back_to_default_lookback(monkeypatch, state_path, capsys):
    """A hand-edited or corrupted per-repo state entry has the SAME contract as a missing
    one (state.py's own docstring): never crash the report. Regression for the codex
    review P2 finding — `parse_utc` used to raise ValueError straight out of the window
    resolver."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text('{"repos": {"owner/repo": "not-a-timestamp"}}', encoding="utf-8")
    _stub_fetch(monkeypatch, [[_pr(1, "2026-08-19T10:00:00Z")]])
    rc = daily_command.run(_args(state=str(state_path)))
    assert rc == 0
    assert "not a valid timestamp" in capsys.readouterr().err
    # Recovered: the run still completed and re-derived a fresh watermark.
    assert load_watermarks(state_path) == {"owner/repo": "2026-08-19T10:00:00Z"}


def test_all_repos_failed_is_a_nonzero_error_not_a_silent_empty_report(monkeypatch, state_path, capsys):
    """When every configured repo fails to fetch, `rig daily` must NOT print the same
    "No merged PRs to report" a genuinely quiet day would produce — that's a false
    negative that could get pasted into Slack as if it were real data. Regression for
    the codex review P2 finding (round 2)."""

    def _fake(repo, since):
        raise daily_command.GhError("boom")

    monkeypatch.setattr(daily_command, "fetch_merged_prs", _fake)
    rc = daily_command.run(_args(repo=["owner/a", "owner/b"], state=str(state_path)))
    assert rc == 1
    captured = capsys.readouterr()
    assert "No merged PRs to report" not in captured.out
    assert "no report" in captured.err.lower()
    assert load_watermarks(state_path) == {}
