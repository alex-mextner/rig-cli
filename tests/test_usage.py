"""Tests for `rig usage` — cross-account Claude token/cost usage aggregation.

Strategy (mirrors tests/test_stats.py): write SYNTHETIC, real-shaped per-message usage
JSONL lines into a throwaway HOME (``~/.claude/projects/...`` for the default account,
``~/.claude-accounts/account-N/projects/...`` for each claude-rotate account), then assert
the parser -> dedup -> aggregator -> renderers produce exact token/cost figures. No real
logs, no network.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from riglib import usage
from riglib.errors import ConfigError
from riglib.usage import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    PRICING_USD_PER_1M,
    TokenTotals,
    build_period_report,
    build_period_reports,
    estimate_cost_usd,
    iter_usage_records,
    period_bounds,
    render_json,
)


# ── fixture builders ──────────────────────────────────────────────────────────────────
def _usage_event(
    ts: str,
    model: str,
    mid: str,
    rid: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> str:
    """A Claude Code `assistant` JSONL line carrying `message.usage` (real on-disk shape)."""
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": ts,
            "requestId": rid,
            "message": {
                "id": mid,
                "model": model,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_creation_input_tokens": cache_creation_input_tokens,
                    "cache_read_input_tokens": cache_read_input_tokens,
                },
            },
        }
    )


def write_default_session(home: Path, encoded: str, session: str, lines: list[str]) -> None:
    d = home / ".claude" / "projects" / encoded
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_account_session(
    home: Path, account: str, encoded: str, session: str, lines: list[str]
) -> None:
    d = home / ".claude-accounts" / account / "projects" / encoded
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── pricing math ───────────────────────────────────────────────────────────────────────
def test_pricing_table_has_documented_rates():
    assert PRICING_USD_PER_1M["claude-opus-4-8"] == (5.00, 25.00)
    assert PRICING_USD_PER_1M["claude-sonnet-5"] == (2.00, 10.00)
    assert PRICING_USD_PER_1M["claude-haiku-4-5"] == (1.00, 5.00)


def test_estimate_cost_usd_plain_input_output():
    totals = TokenTotals(input_tokens=1_000_000, output_tokens=1_000_000)
    # claude-sonnet-5: $2/1M in, $10/1M out
    assert estimate_cost_usd("claude-sonnet-5", totals) == pytest.approx(12.00)


def test_estimate_cost_usd_cache_write_multiplier():
    totals = TokenTotals(cache_creation_input_tokens=1_000_000)
    # claude-opus-4-8: $5/1M in * 1.25 cache-write multiplier = $6.25
    cost = estimate_cost_usd("claude-opus-4-8", totals)
    assert cost == pytest.approx(5.00 * CACHE_WRITE_MULTIPLIER)
    assert cost == pytest.approx(6.25)


def test_estimate_cost_usd_cache_read_multiplier():
    totals = TokenTotals(cache_read_input_tokens=1_000_000)
    # claude-opus-4-8: $5/1M in * 0.1 cache-read multiplier = $0.50
    cost = estimate_cost_usd("claude-opus-4-8", totals)
    assert cost == pytest.approx(5.00 * CACHE_READ_MULTIPLIER)
    assert cost == pytest.approx(0.50)


def test_estimate_cost_usd_combines_all_four_token_types():
    totals = TokenTotals(
        input_tokens=500_000,
        output_tokens=200_000,
        cache_creation_input_tokens=100_000,
        cache_read_input_tokens=1_000_000,
    )
    price_in, price_out = PRICING_USD_PER_1M["claude-sonnet-4-6"]
    expected = (
        500_000 * price_in
        + 200_000 * price_out
        + 100_000 * price_in * CACHE_WRITE_MULTIPLIER
        + 1_000_000 * price_in * CACHE_READ_MULTIPLIER
    ) / 1_000_000
    assert estimate_cost_usd("claude-sonnet-4-6", totals) == pytest.approx(expected)


def test_estimate_cost_usd_unpriced_model_returns_none_never_guesses():
    totals = TokenTotals(input_tokens=1_000)
    assert estimate_cost_usd("claude-some-future-model-99", totals) is None


# ── parsing + dedup ──────────────────────────────────────────────────────────────────
def test_iter_usage_records_reads_default_and_account_roots(tmp_path):
    write_default_session(
        tmp_path,
        "-Users-ultra-xp-rig-cli",
        "s1",
        [_usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50)],
    )
    write_account_session(
        tmp_path,
        "account-0",
        "-Users-ultra-xp-rig-cli",
        "s2",
        [_usage_event("2026-08-25T11:00:00Z", "claude-opus-4-8", "m2", "r2", input_tokens=10, output_tokens=5)],
    )
    records = list(iter_usage_records(home=tmp_path))
    accounts = {r.account for r in records}
    assert accounts == {"default", "account-0"}
    models = {r.account: r.model for r in records}
    assert models == {"default": "claude-sonnet-5", "account-0": "claude-opus-4-8"}


def test_iter_usage_records_dedups_same_message_within_one_file(tmp_path):
    # a streamed response can write the same (message.id, requestId) more than once
    # (e.g. --resume replays); the usage figures must be counted exactly once.
    lines = [
        _usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50),
        _usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50),
    ]
    write_default_session(tmp_path, "-Users-ultra-xp-rig-cli", "s1", lines)
    records = list(iter_usage_records(home=tmp_path))
    assert len(records) == 1
    assert records[0].input_tokens == 100


def test_iter_usage_records_dedups_same_message_across_files(tmp_path):
    # a resumed session can spread the same message across two session files.
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50)],
    )
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s2",
        [_usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50)],
    )
    records = list(iter_usage_records(home=tmp_path))
    assert len(records) == 1


def test_iter_usage_records_ignores_events_without_usage(tmp_path):
    d = tmp_path / ".claude" / "projects" / "-Users-ultra-xp-rig-cli"
    d.mkdir(parents=True)
    lines = [
        json.dumps({"type": "user", "timestamp": "2026-08-25T10:00:00Z", "message": {"role": "user"}}),
        json.dumps({"not": "even valid usefully"}),
        "",
        "{not valid json at all",
    ]
    (d / "s1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert list(iter_usage_records(home=tmp_path)) == []


def test_iter_usage_records_skips_missing_account_dirs_gracefully(tmp_path):
    # no ~/.claude-accounts at all on this HOME — must not error.
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=1, output_tokens=1)],
    )
    records = list(iter_usage_records(home=tmp_path))
    assert len(records) == 1


# ── period bucketing ─────────────────────────────────────────────────────────────────
def test_period_bounds_day():
    now = datetime(2026, 8, 27, 15, 30, tzinfo=timezone.utc)  # Thursday
    start, end, label = period_bounds("day", now=now)
    assert start == datetime(2026, 8, 27, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 28, tzinfo=timezone.utc)
    assert label == "2026-08-27"


def test_period_bounds_week_monday_start():
    now = datetime(2026, 8, 27, 15, 30, tzinfo=timezone.utc)  # Thursday, ISO week 35
    start, end, label = period_bounds("week", now=now)
    assert start == datetime(2026, 8, 24, tzinfo=timezone.utc)  # Monday
    assert end == datetime(2026, 8, 31, tzinfo=timezone.utc)
    assert label == "2026-W35"


def test_period_bounds_month():
    now = datetime(2026, 8, 27, 15, 30, tzinfo=timezone.utc)
    start, end, label = period_bounds("month", now=now)
    assert start == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert label == "2026-08"


def test_period_bounds_month_december_rolls_into_next_year():
    now = datetime(2026, 12, 15, tzinfo=timezone.utc)
    start, end, label = period_bounds("month", now=now)
    assert start == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert end == datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert label == "2026-12"


def test_build_period_report_buckets_by_window_and_model_and_account(tmp_path):
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [
            # inside the week
            _usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50),
            # outside the week (prior week)
            _usage_event("2026-08-10T10:00:00Z", "claude-sonnet-5", "m2", "r2", input_tokens=999, output_tokens=999),
        ],
    )
    write_account_session(
        tmp_path, "account-0", "-Users-ultra-xp-rig-cli", "s2",
        [_usage_event("2026-08-26T10:00:00Z", "claude-opus-4-8", "m3", "r3", input_tokens=10, output_tokens=5)],
    )
    records = list(iter_usage_records(home=tmp_path))
    now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    report = build_period_report(records, "week", now=now)

    assert report.label == "2026-W35"
    assert report.total.input_tokens == 110
    assert report.total.output_tokens == 55
    assert set(report.by_model) == {"claude-sonnet-5", "claude-opus-4-8"}
    assert report.by_model["claude-sonnet-5"].totals.input_tokens == 100
    assert report.by_model["claude-opus-4-8"].totals.input_tokens == 10
    assert set(report.by_account) == {"default", "account-0"}
    assert report.by_account["default"].input_tokens == 100
    assert report.by_account["account-0"].input_tokens == 10


def test_build_period_report_unpriced_model_reported_separately_not_guessed(tmp_path):
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event(
            "2026-08-25T10:00:00Z", "claude-future-model-x", "m1", "r1",
            input_tokens=1000, output_tokens=1000,
        )],
    )
    records = list(iter_usage_records(home=tmp_path))
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    report = build_period_report(records, "week", now=now)

    assert report.priced_cost_usd == 0.0
    assert "claude-future-model-x" in report.unpriced_models
    assert report.unpriced_models["claude-future-model-x"].input_tokens == 1000
    assert report.by_model["claude-future-model-x"].cost_usd is None


def test_build_period_report_undated_events_are_counted_not_silently_dropped(tmp_path):
    from riglib.usage import UsageRecord

    records = [
        UsageRecord(
            timestamp=None, account="default", model="claude-sonnet-5",
            input_tokens=5, output_tokens=5,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )
    ]
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    report = build_period_report(records, "week", now=now)
    assert report.undated == 1
    assert report.total.input_tokens == 0  # never bucketed into a window it can't place


# ── JSON rendering ───────────────────────────────────────────────────────────────────
def test_render_json_shape_and_schema_version(tmp_path):
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50)],
    )
    records = list(iter_usage_records(home=tmp_path))
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    report = build_period_report(records, "week", now=now)
    out = render_json({"week": report}, accounts_scanned=["default"])

    assert out["schema"] == usage.JSON_SCHEMA_VERSION
    assert "hypothetical" in out["disclaimer"].lower()
    assert "not" in out["disclaimer"].lower()
    assert out["accounts_scanned"] == ["default"]
    week = out["periods"]["week"]
    assert week["label"] == "2026-W35"
    assert week["totals"]["input_tokens"] == 100
    assert week["totals"]["total_tokens"] == 150
    assert week["by_model"]["claude-sonnet-5"]["totals"]["input_tokens"] == 100
    assert week["by_model"]["claude-sonnet-5"]["estimated_cost_usd"] is not None
    assert week["by_account"]["default"]["input_tokens"] == 100
    assert week["unpriced_tokens"] == {}
    # round-trips through json.dumps without error (no bare Counter/datetime leaking)
    json.dumps(out)


# ── CLI wiring (`rig usage`) ─────────────────────────────────────────────────────────
# `--now` is a hidden test seam (mirrors `--home`) so these assert REAL token figures flow
# end-to-end through the CLI at a fixed, known window, instead of only checking headers —
# a fixture timestamped in the past would silently fall outside the real "current week"
# after enough real time passes, and a header-only assertion would never notice.
_FIXED_NOW = "2026-08-27T12:00:00Z"


def test_cli_bare_usage_prints_week_and_month_text_with_real_figures(tmp_path, capsys):
    from riglib.cli import main

    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50)],
    )
    rc = main(["usage", "--home", str(tmp_path), "--now", _FIXED_NOW])
    out = capsys.readouterr().out
    assert rc == 0
    assert "hypothetical" in out.lower()
    # bare invocation reports BOTH the current week and current month, with the real figure
    assert "Week 2026-W35" in out
    assert "Month 2026-08" in out
    # the exact formatted totals prefix, not a bare "100" substring — a "$100.00" cost
    # string would also satisfy a bare "100" in out" check and mask a real regression.
    assert "total tokens: 150  (in 100" in out


def test_cli_usage_period_json_selects_one_period_with_real_figures(tmp_path, capsys):
    from riglib.cli import main

    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50)],
    )
    rc = main(["usage", "--home", str(tmp_path), "--now", _FIXED_NOW, "--period", "week", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert set(out["periods"]) == {"week"}
    assert out["schema"] == usage.JSON_SCHEMA_VERSION
    assert out["periods"]["week"]["totals"]["input_tokens"] == 100


def test_cli_usage_period_day_end_to_end(tmp_path, capsys):
    from riglib.cli import main

    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event("2026-08-27T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=7, output_tokens=3)],
    )
    rc = main(["usage", "--home", str(tmp_path), "--now", _FIXED_NOW, "--period", "day", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["periods"]["day"]["label"] == "2026-08-27"
    assert out["periods"]["day"]["totals"]["input_tokens"] == 7


def test_cli_usage_no_logs_at_all_does_not_crash(tmp_path, capsys):
    from riglib.cli import main

    rc = main(["usage", "--home", str(tmp_path), "--now", _FIXED_NOW, "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["accounts_scanned"] == []
    assert out["periods"]["week"]["totals"]["total_tokens"] == 0


# ── dedup key correctness ────────────────────────────────────────────────────────────
def test_dedup_key_does_not_collide_across_a_field_boundary_shift():
    from riglib.usage import _dedup_key

    # a naive f"{a}:{b}" join would make these collide ("a:b" + "c" == "a" + "b:c")
    key1 = _dedup_key("a:b", "c")
    key2 = _dedup_key("a", "b:c")
    assert key1 != key2


def test_dedup_key_none_when_either_field_missing_or_wrong_type():
    from riglib.usage import _dedup_key

    assert _dedup_key(None, "r1") is None
    assert _dedup_key("m1", None) is None
    assert _dedup_key("", "r1") is None
    assert _dedup_key(123, "r1") is None


# ── malformed token fields ───────────────────────────────────────────────────────────
def test_negative_and_boolean_token_fields_are_coerced_to_zero_not_corrupting_totals(tmp_path):
    d = tmp_path / ".claude" / "projects" / "-Users-ultra-xp-rig-cli"
    d.mkdir(parents=True)
    line = json.dumps({
        "type": "assistant",
        "timestamp": "2026-08-25T10:00:00Z",
        "requestId": "r1",
        "message": {
            "id": "m1",
            "model": "claude-sonnet-5",
            "usage": {
                "input_tokens": -100,  # malformed: negative
                "output_tokens": True,  # malformed: bool, not a real count
                "cache_creation_input_tokens": 50,
                "cache_read_input_tokens": 0,
            },
        },
    })
    (d / "s1.jsonl").write_text(line + "\n", encoding="utf-8")
    records = list(iter_usage_records(home=tmp_path))
    assert len(records) == 1
    assert records[0].input_tokens == 0  # negative -> 0, never a negative total
    assert records[0].output_tokens == 0  # bool -> 0, never miscounted as 1
    assert records[0].cache_creation_input_tokens == 50  # valid field untouched

    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    report = build_period_report(records, "week", now=now)
    assert report.priced_cost_usd >= 0  # never a negative "cost"


# ── unreadable directory does not abort the whole scan ──────────────────────────────
def test_unreadable_project_dir_is_skipped_not_fatal(tmp_path):
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50)],
    )
    unreadable = tmp_path / ".claude" / "projects" / "-Users-ultra-broken"
    unreadable.mkdir(parents=True)
    (unreadable / "s2.jsonl").write_text(
        _usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m2", "r2", input_tokens=1, output_tokens=1) + "\n",
        encoding="utf-8",
    )
    try:
        unreadable.chmod(0o000)
        if os.access(unreadable, os.R_OK):
            pytest.skip("running as root (or on a filesystem) where chmod 0o000 doesn't "
                        "actually block reads — this leg can't exercise the OSError guard")
        # is_dir() still True (stat succeeds without read perms); iterdir()/glob() must
        # not raise out of the generator — the readable sibling dir must still report.
        records = list(iter_usage_records(home=tmp_path))
        assert len(records) == 1  # only the readable sibling's record — not 2, not a crash
        assert records[0].input_tokens == 100
    finally:
        unreadable.chmod(0o755)  # restore so tmp_path cleanup can remove it


# ── mtime-based file pruning ──────────────────────────────────────────────────────────
def test_mtime_pruning_skips_a_file_that_cannot_hold_events_after_cutoff(tmp_path):
    import os as _os

    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "old-session",
        [_usage_event("2020-01-01T00:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=999, output_tokens=999)],
    )
    old_file = tmp_path / ".claude" / "projects" / "-Users-ultra-xp-rig-cli" / "old-session.jsonl"
    old_epoch = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    _os.utime(old_file, (old_epoch, old_epoch))  # backdate mtime to match its content

    cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
    records = list(iter_usage_records(home=tmp_path, mtime_after=cutoff))
    assert records == []  # pruned by mtime, never opened

    # without the cutoff, the same file IS read
    records_unpruned = list(iter_usage_records(home=tmp_path))
    assert len(records_unpruned) == 1


def test_mtime_pruning_never_drops_a_file_still_in_window(tmp_path):
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50)],
    )
    cutoff = datetime(2020, 1, 1, tzinfo=timezone.utc)  # well before the file's real mtime
    records = list(iter_usage_records(home=tmp_path, mtime_after=cutoff))
    assert len(records) == 1


def test_mtime_pruning_safety_margin_protects_against_backward_clock_step(tmp_path):
    import os as _os

    # a file mtime just inside the 48h safety margin before cutoff must NOT be pruned —
    # this is exactly the backward-clock-step scenario the margin exists to protect.
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50)],
    )
    session_file = tmp_path / ".claude" / "projects" / "-Users-ultra-xp-rig-cli" / "s1.jsonl"
    cutoff = datetime(2026, 8, 27, tzinfo=timezone.utc)
    mtime_inside_margin = (cutoff - timedelta(hours=47)).timestamp()
    _os.utime(session_file, (mtime_inside_margin, mtime_inside_margin))

    records = list(iter_usage_records(home=tmp_path, mtime_after=cutoff))
    assert len(records) == 1


def test_mtime_pruning_stat_failure_fails_open(tmp_path, monkeypatch):
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50)],
    )
    real_stat = Path.stat

    def _flaky_stat(self, *a, **kw):
        if self.suffix == ".jsonl":
            raise PermissionError("simulated stat failure")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", _flaky_stat)
    cutoff = datetime(2026, 8, 27, tzinfo=timezone.utc)
    # a stat failure must keep the file IN scope, never silently drop real usage
    records = list(iter_usage_records(home=tmp_path, mtime_after=cutoff))
    assert len(records) == 1


# ── date-suffixed model IDs (real, observed on a live machine) ──────────────────────
def test_estimate_cost_usd_resolves_a_date_suffixed_model_alias():
    # confirmed via a live-machine log scan: claude-haiku-4-5-20251001 is the SAME model
    # as claude-haiku-4-5, just a dated snapshot alias — not an unfamiliar model.
    totals = TokenTotals(input_tokens=1_000_000, output_tokens=1_000_000)
    dated_cost = estimate_cost_usd("claude-haiku-4-5-20251001", totals)
    bare_cost = estimate_cost_usd("claude-haiku-4-5", totals)
    assert dated_cost == bare_cost
    assert dated_cost == pytest.approx(1.00 + 5.00)


def test_estimate_cost_usd_a_genuinely_unknown_dated_id_still_unpriced():
    totals = TokenTotals(input_tokens=1_000)
    assert estimate_cost_usd("claude-nonexistent-model-9-20990101", totals) is None


def test_build_period_report_by_model_key_stays_the_exact_observed_id(tmp_path):
    # pricing resolves via the date-alias, but the report still shows the EXACT id seen in
    # the log — no silent merging of two distinct log entries under one label.
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event(
            "2026-08-25T10:00:00Z", "claude-haiku-4-5-20251001", "m1", "r1",
            input_tokens=100, output_tokens=50,
        )],
    )
    records = list(iter_usage_records(home=tmp_path))
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    report = build_period_report(records, "week", now=now)
    assert "claude-haiku-4-5-20251001" in report.by_model
    assert report.by_model["claude-haiku-4-5-20251001"].cost_usd is not None
    assert report.priced_cost_usd > 0


# ── --now hidden test seam: fail loud on garbage, never silently use the real clock ──
def test_resolve_now_raises_config_error_on_garbage_not_silent_fallback():
    """A garbage `--now` must surface as rig's own structured `ConfigError` (exit 2,
    what/why/fix) — not a silent fallback to the real clock. Same class as
    `riglib/daily/command.py::_parse_since_arg`'s `--since` handling."""
    from riglib.usage import _resolve_now

    class _Args:
        now = "not-a-timestamp-at-all"

    with pytest.raises(ConfigError) as exc_info:
        _resolve_now(_Args())
    assert exc_info.value.exit_code == 2
    assert "not-a-timestamp-at-all" in exc_info.value.what


def test_cli_usage_garbage_now_exits_2_via_errors_guard(tmp_path, capsys):
    from riglib.cli import main

    rc = main(["usage", "--home", str(tmp_path), "--now", "not-a-timestamp-at-all", "--json"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "not-a-timestamp-at-all" in out


def test_cli_usage_valid_now_is_honored():
    from riglib.usage import _resolve_now

    class _Args:
        now = "2026-08-27T12:00:00Z"

    resolved = _resolve_now(_Args())
    assert resolved == datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


# ── bare invocation: BOTH periods must carry real, independently-verified figures ────
def test_cli_bare_json_both_periods_carry_independent_real_figures(tmp_path, capsys):
    from riglib.cli import main

    # one event inside the current week AND month, one inside the month but a prior week —
    # if a future regression let "month" drain an already-exhausted generator, this event
    # (only reachable via the month window) would silently vanish.
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [
            _usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50),
            _usage_event("2026-08-05T10:00:00Z", "claude-sonnet-5", "m2", "r2", input_tokens=7, output_tokens=3),
        ],
    )
    rc = main(["usage", "--home", str(tmp_path), "--now", _FIXED_NOW, "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["periods"]["week"]["totals"]["input_tokens"] == 100
    # the month total includes BOTH events; the week total does not — this is the
    # assertion that would fail if a generator got double-consumed/exhausted.
    assert out["periods"]["month"]["totals"]["input_tokens"] == 107


# ── UX: an all-unpriced window must not read as "free" ──────────────────────────────
def test_render_text_says_unpriced_not_zero_dollars_when_every_model_is_unpriced(tmp_path):
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event(
            "2026-08-25T10:00:00Z", "claude-totally-unknown-model", "m1", "r1",
            input_tokens=1000, output_tokens=1000,
        )],
    )
    records = list(iter_usage_records(home=tmp_path))
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    report = build_period_report(records, "week", now=now)
    assert report.all_models_unpriced is True
    text = usage.render_text({"week": report}, accounts_scanned=["default"])
    assert "unpriced" in text
    assert "$0.00" not in text


# ── single-pass multi-period builder ─────────────────────────────────────────────────
def test_build_period_reports_single_pass_matches_per_period_build(tmp_path):
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [
            _usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50),
            _usage_event("2026-08-05T10:00:00Z", "claude-sonnet-5", "m2", "r2", input_tokens=7, output_tokens=3),
        ],
    )
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    records = list(iter_usage_records(home=tmp_path))

    combined = build_period_reports(iter(records), ["week", "month"], now=now)
    week_only = build_period_report(list(records), "week", now=now)
    month_only = build_period_report(list(records), "month", now=now)

    assert combined["week"].total.input_tokens == week_only.total.input_tokens
    assert combined["month"].total.input_tokens == month_only.total.input_tokens
    assert combined["month"].total.input_tokens == 107


def test_build_period_reports_accepts_a_true_one_pass_generator(tmp_path):
    # the whole point of build_period_reports: it must work on a generator that can only
    # be consumed ONCE, proving no period silently starves another.
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50)],
    )
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    reports = build_period_reports(iter_usage_records(home=tmp_path), ["week", "month"], now=now)
    assert reports["week"].total.input_tokens == 100
    assert reports["month"].total.input_tokens == 100


# ── control-character sanitization in text output ────────────────────────────────────
def test_render_text_strips_control_characters_from_model_name(tmp_path):
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event(
            "2026-08-25T10:00:00Z", "claude-sonnet-5\x1b]0;pwn\x07", "m1", "r1",
            input_tokens=10, output_tokens=5,
        )],
    )
    records = list(iter_usage_records(home=tmp_path))
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    report = build_period_report(records, "week", now=now)
    text = usage.render_text({"week": report}, accounts_scanned=["default"])
    assert "\x1b" not in text
    assert "\x07" not in text


def test_render_text_strips_control_characters_from_accounts_scanned_header(tmp_path):
    # regression: the header line used to bypass the sanitizer that per-row output uses.
    text = usage.render_text({}, accounts_scanned=["default\x1b]0;pwn\x07"])
    assert "\x1b" not in text
    assert "\x07" not in text
    assert "default" in text


# ── dedup × mtime_after interaction (the load-bearing production path) ──────────────
# `run()` ALWAYS passes `mtime_after=earliest_start` — these two tests exercise the
# dedup set's persistence decision under that real cutoff, not just the un-cutoffed
# `iter_usage_records(home=...)` calls the rest of the suite uses. A mutation that flips
# the `>=` in `iter_usage_records`'s "counts_toward_some_window" check (or drops the
# timestamp-not-None guard) would double-count or drop tokens here while staying green
# everywhere else in the suite (review finding).
def test_dedup_of_in_window_duplicates_survives_the_mtime_cutoff_boundary(tmp_path):
    lines = [
        _usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50),
        _usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50),
    ]
    write_default_session(tmp_path, "-Users-ultra-xp-rig-cli", "s1", lines)
    cutoff = datetime(2026, 8, 24, tzinfo=timezone.utc)  # the window start, both events after it
    records = list(iter_usage_records(home=tmp_path, mtime_after=cutoff))
    assert len(records) == 1
    assert records[0].input_tokens == 100  # not 200 — the duplicate was not double-counted


def test_dedup_does_not_drop_an_in_window_duplicate_behind_an_undated_first_occurrence(tmp_path):
    """Regression: an undated FIRST occurrence used to be persisted into the dedup set
    unconditionally, which then silently discarded a LATER, real, in-window duplicate of
    the same (message.id, requestId) — an in-window token undercount (review finding)."""
    d = tmp_path / ".claude" / "projects" / "-Users-ultra-xp-rig-cli"
    d.mkdir(parents=True)
    undated_first = json.dumps({
        "type": "assistant",
        # no "timestamp" key at all -> parses to timestamp=None
        "requestId": "r1",
        "message": {
            "id": "m1", "model": "claude-sonnet-5",
            "usage": {"input_tokens": 100, "output_tokens": 50,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        },
    })
    dated_second = _usage_event(
        "2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50
    )
    (d / "s1.jsonl").write_text(undated_first + "\n" + dated_second + "\n", encoding="utf-8")

    cutoff = datetime(2026, 8, 24, tzinfo=timezone.utc)
    records = list(iter_usage_records(home=tmp_path, mtime_after=cutoff))
    dated_records = [r for r in records if r.timestamp is not None]
    assert len(dated_records) == 1  # the real, in-window duplicate must survive
    assert dated_records[0].input_tokens == 100


# ── integral-float token fields ──────────────────────────────────────────────────────
def test_integral_float_token_field_is_coerced_not_silently_zeroed(tmp_path):
    d = tmp_path / ".claude" / "projects" / "-Users-ultra-xp-rig-cli"
    d.mkdir(parents=True)
    line = json.dumps({
        "type": "assistant",
        "timestamp": "2026-08-25T10:00:00Z",
        "requestId": "r1",
        "message": {
            "id": "m1", "model": "claude-sonnet-5",
            "usage": {
                "input_tokens": 100.0,  # some JSON producer's whole-number float
                "output_tokens": 50.5,  # non-integral -> malformed, coerced to 0
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            },
        },
    })
    (d / "s1.jsonl").write_text(line + "\n", encoding="utf-8")
    records = list(iter_usage_records(home=tmp_path))
    assert len(records) == 1
    assert records[0].input_tokens == 100  # coerced, not silently zeroed
    assert records[0].output_tokens == 0  # non-integral float is still malformed


# ── dedup persistence is independent of which periods are requested ─────────────────
def test_dedup_decision_is_independent_of_which_periods_are_requested(tmp_path):
    """Regression (Codex review, round 3): dedup persistence used to be gated on the
    COMBINED earliest-requested-window cutoff, so the same underlying data could dedup
    differently depending on whether `--period week` was requested alone or as part of
    the bare week+month run — a duplicate whose first occurrence fell inside the wider
    (month) window but outside the narrower (week) window could silently zero out the
    week total only when month was ALSO requested. Both invocations must now agree."""
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [
            # first occurrence: inside the month, outside the (later) week
            _usage_event("2026-08-05T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50),
            # second occurrence, same key: inside the week
            _usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50),
        ],
    )
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)

    week_alone = build_period_reports(iter_usage_records(home=tmp_path, mtime_after=period_bounds("week", now=now)[0]), ["week"], now=now)
    bare_both = build_period_reports(iter_usage_records(home=tmp_path, mtime_after=period_bounds("month", now=now)[0]), ["week", "month"], now=now)

    # both must report the SAME week total for the SAME underlying data
    assert week_alone["week"].total.input_tokens == bare_both["week"].total.input_tokens


# ── oversized integer literal (Python 3.11+ int-string conversion limit) ────────────
def test_oversized_integer_literal_skips_line_not_crashes_command(tmp_path):
    d = tmp_path / ".claude" / "projects" / "-Users-ultra-xp-rig-cli"
    d.mkdir(parents=True)
    huge_digits = "9" * 5000  # well past Python's default 4300-digit conversion limit
    bad_line = (
        '{"type": "assistant", "timestamp": "2026-08-25T10:00:00Z", "requestId": "r1", '
        '"message": {"id": "m1", "model": "claude-sonnet-5", '
        '"usage": {"input_tokens": ' + huge_digits + ', "output_tokens": 1, '
        '"cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}'
    )
    good_line = _usage_event("2026-08-25T11:00:00Z", "claude-sonnet-5", "m2", "r2", input_tokens=10, output_tokens=5)
    (d / "s1.jsonl").write_text(bad_line + "\n" + good_line + "\n", encoding="utf-8")
    records = list(iter_usage_records(home=tmp_path))  # must not raise
    assert len(records) == 1
    assert records[0].input_tokens == 10


# ── C1 control character range ────────────────────────────────────────────────────────
def test_render_text_strips_c1_control_characters_too(tmp_path):
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event(
            "2026-08-25T10:00:00Z", "claude-sonnet-531mRED", "m1", "r1",
            input_tokens=10, output_tokens=5,
        )],
    )
    records = list(iter_usage_records(home=tmp_path))
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    report = build_period_report(records, "week", now=now)
    text = usage.render_text({"week": report}, accounts_scanned=["default"])
    assert "" not in text


# ── JSON contract: all_models_unpriced disambiguates "$0.00" from "unpriced" ─────────
def test_json_all_models_unpriced_flag(tmp_path):
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event(
            "2026-08-25T10:00:00Z", "claude-totally-unknown-model", "m1", "r1",
            input_tokens=1000, output_tokens=1000,
        )],
    )
    records = list(iter_usage_records(home=tmp_path))
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    report = build_period_report(records, "week", now=now)
    out = render_json({"week": report}, accounts_scanned=["default"])
    assert out["periods"]["week"]["all_models_unpriced"] is True
    assert out["periods"]["week"]["estimated_cost_usd"] == 0.0


def test_json_all_models_unpriced_false_with_real_usage(tmp_path):
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50)],
    )
    records = list(iter_usage_records(home=tmp_path))
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    report = build_period_report(records, "week", now=now)
    out = render_json({"week": report}, accounts_scanned=["default"])
    assert out["periods"]["week"]["all_models_unpriced"] is False


# ── astronomically large / adversarial numeric token fields ─────────────────────────
def test_gigantic_integer_token_count_is_rejected_not_crashing_on_cost_math(tmp_path):
    """Regression: a plausible-but-huge integer (hundreds of digits, well under Python's
    4300-digit json parse limit so it parses fine) used to survive `_as_token_count`
    unbounded, then raise `OverflowError` in `estimate_cost_usd`'s `int * float` — losing
    the whole report to one bad line."""
    d = tmp_path / ".claude" / "projects" / "-Users-ultra-xp-rig-cli"
    d.mkdir(parents=True)
    giant = int("9" * 309)  # parses fine; way too large for float conversion
    line = json.dumps({
        "type": "assistant", "timestamp": "2026-08-25T10:00:00Z", "requestId": "r1",
        "message": {
            "id": "m1", "model": "claude-sonnet-5",
            "usage": {"input_tokens": giant, "output_tokens": 5,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        },
    })
    (d / "s1.jsonl").write_text(line + "\n", encoding="utf-8")
    records = list(iter_usage_records(home=tmp_path))
    assert len(records) == 1
    assert records[0].input_tokens == 0  # rejected as implausible, not corrupting totals

    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    report = build_period_report(records, "week", now=now)  # must not raise OverflowError
    assert report.priced_cost_usd >= 0


def test_astronomical_integral_float_token_count_never_produces_infinity_in_json(tmp_path):
    """Regression: 1e308 is an integral float that used to pass `_as_token_count`, then
    overflow to `inf` in cost math, which `json.dumps` emits as a bare `Infinity` token —
    invalid JSON for the "stable" contract's own consumers."""
    d = tmp_path / ".claude" / "projects" / "-Users-ultra-xp-rig-cli"
    d.mkdir(parents=True)
    line = json.dumps({
        "type": "assistant", "timestamp": "2026-08-25T10:00:00Z", "requestId": "r1",
        "message": {
            "id": "m1", "model": "claude-sonnet-5",
            "usage": {"input_tokens": 1e308, "output_tokens": 5,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        },
    })
    (d / "s1.jsonl").write_text(line + "\n", encoding="utf-8")
    records = list(iter_usage_records(home=tmp_path))
    assert records[0].input_tokens == 0

    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    report = build_period_report(records, "week", now=now)
    out = render_json({"week": report}, accounts_scanned=["default"])
    serialized = json.dumps(out)  # must round-trip cleanly, no bare Infinity/NaN
    assert "Infinity" not in serialized
    assert "NaN" not in serialized
    reparsed = json.loads(serialized)  # strict re-parse: fails loudly if it snuck through
    assert reparsed["periods"]["week"]["estimated_cost_usd"] != float("inf")


# ── undated_records_scanned: scan-level, not per-period (JSON contract) ─────────────
def test_json_undated_records_scanned_is_top_level_not_per_period(tmp_path):
    from riglib.usage import UsageRecord

    records = [
        UsageRecord(
            timestamp=None, account="default", model="claude-sonnet-5",
            input_tokens=5, output_tokens=5,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )
    ]
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    reports = build_period_reports(iter(records), ["week", "month"], now=now)
    out = render_json(reports, accounts_scanned=["default"])
    assert out["undated_records_scanned"] == 1
    # NOT present per-period — the whole point of hoisting it out
    assert "undated_records_skipped" not in out["periods"]["week"]
    assert "undated_records_skipped" not in out["periods"]["month"]


def test_json_undated_records_scanned_zero_when_no_reports():
    out = render_json({}, accounts_scanned=[])
    assert out["undated_records_scanned"] == 0


# ── account directory natural-sort ordering (accounts_scanned determinism) ──────────
def test_accounts_scanned_natural_sort_beyond_nine_accounts(tmp_path):
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    for n in [2, 10, 1]:
        (tmp_path / ".claude-accounts" / f"account-{n}" / "projects").mkdir(parents=True)
    roots = usage.resolve_account_roots(home=tmp_path)
    names = [name for name, _ in roots]
    assert names == ["default", "account-1", "account-2", "account-10"]


# ── mtime-pruning cutoff must be independent of which --period was requested ────────
def test_mtime_cutoff_is_independent_of_requested_period_two_file_repro(tmp_path):
    """Regression (Opus review, round 4): file-level mtime pruning changes WHICH files
    get scanned, which changes what dedup's first-occurrence-wins sees as "first". A
    cutoff that varied with the requested periods let `rig usage --period week` and the
    `week` section of a bare `rig usage` (which used a wider, month-start cutoff) report
    DIFFERENT totals for the exact same underlying duplicate message: an old file (mtime
    Aug 5, timestamp Aug 5 — inside the month, outside the week) got pruned only under
    the narrower week-only cutoff, so which occurrence "won" first-wins depended on
    `--period`. With a fixed cutoff (independent of the requested periods), File A is
    now ALWAYS in scope, so its Aug-5 occurrence ALWAYS wins first-wins (it's scanned
    before File B) and the week total is consistently 0 — that specific number is the
    documented first-wins tradeoff, not what this test is pinning; what matters, and
    what actually regressed before this fix, is that both invocations AGREE.
    """
    d = tmp_path / ".claude" / "projects" / "-Users-ultra-xp-rig-cli"
    d.mkdir(parents=True)
    # File A sorts before B (glob is lexical), mtime + timestamp both Aug 5 (in month,
    # outside week).
    (d / "aaa-session.jsonl").write_text(
        _usage_event("2026-08-05T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50)
        + "\n",
        encoding="utf-8",
    )
    aug5 = datetime(2026, 8, 5, tzinfo=timezone.utc).timestamp()
    os.utime(d / "aaa-session.jsonl", (aug5, aug5))
    # File B: same key, mtime + timestamp both Aug 25 (inside the week).
    (d / "bbb-session.jsonl").write_text(
        _usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50)
        + "\n",
        encoding="utf-8",
    )
    aug25 = datetime(2026, 8, 25, tzinfo=timezone.utc).timestamp()
    os.utime(d / "bbb-session.jsonl", (aug25, aug25))

    now = datetime(2026, 8, 27, tzinfo=timezone.utc)

    # the SAME cutoff run() actually uses — independent of which periods are requested
    cutoff = usage._mtime_prune_cutoff(now)
    week_alone = build_period_reports(iter_usage_records(home=tmp_path, mtime_after=cutoff), ["week"], now=now)
    bare_both = build_period_reports(iter_usage_records(home=tmp_path, mtime_after=cutoff), ["week", "month"], now=now)

    # the actual regression: these two used to disagree (0 vs 100) depending on --period.
    # Both must now agree, for the SAME underlying data and the SAME fixed cutoff.
    assert week_alone["week"].total.input_tokens == bare_both["week"].total.input_tokens
    # and the month total (unaffected by this scenario either way) still sees the record
    # that "won" first-wins, confirming the scan isn't silently dropping data entirely.
    assert bare_both["month"].total.input_tokens == 100


def test_mtime_prune_cutoff_is_min_of_week_and_month_start():
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    week_start, _, _ = period_bounds("week", now=now)
    month_start, _, _ = period_bounds("month", now=now)
    assert usage._mtime_prune_cutoff(now) == min(week_start, month_start)


def test_mtime_prune_cutoff_handles_week_straddling_a_month_boundary():
    # Sep 2, 2026 is a Wednesday; its ISO week starts Monday Aug 31 — BEFORE Sep 1
    # (month start). month_start is NOT always the wider bound.
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    week_start, _, _ = period_bounds("week", now=now)
    month_start, _, _ = period_bounds("month", now=now)
    assert week_start < month_start
    assert usage._mtime_prune_cutoff(now) == week_start


# ── "usage" substring pre-gate never skips a real usage-bearing line ────────────────
def test_usage_substring_pregate_does_not_drop_real_usage_lines(tmp_path):
    write_default_session(
        tmp_path, "-Users-ultra-xp-rig-cli", "s1",
        [_usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50)],
    )
    records = list(iter_usage_records(home=tmp_path))
    assert len(records) == 1
    assert records[0].input_tokens == 100


def test_usage_substring_pregate_skips_non_usage_lines_without_crashing(tmp_path):
    d = tmp_path / ".claude" / "projects" / "-Users-ultra-xp-rig-cli"
    d.mkdir(parents=True)
    lines = [
        json.dumps({"type": "user", "timestamp": "2026-08-25T09:00:00Z", "message": {"role": "user", "content": "hi"}}),
        _usage_event("2026-08-25T10:00:00Z", "claude-sonnet-5", "m1", "r1", input_tokens=100, output_tokens=50),
    ]
    (d / "s1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    records = list(iter_usage_records(home=tmp_path))
    assert len(records) == 1
    assert records[0].input_tokens == 100
