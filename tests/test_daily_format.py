"""Mechanical title -> Slack-line transform (no LLM call — regex only)."""

from __future__ import annotations

from riglib.daily.format_report import extract_ticket_refs, format_line, render_report
from riglib.daily.model import MergedPR


def _pr(title: str, body: str = "", number: int = 705, repo: str = "hyperide/hyper-saas") -> MergedPR:
    return MergedPR(
        repo=repo, number=number, title=title, body=body,
        merged_at="2026-08-19T13:53:08Z", url=f"https://github.com/{repo}/pull/{number}",
    )


def test_strips_conventional_prefix_and_trailing_refs_then_recombines():
    pr = _pr(
        "feat(component-create): guided New-component dialog with shared ext+SaaS logic "
        "(HYP-1184) (#705)",
        number=705,
    )
    assert format_line(pr) == (
        "Guided New-component dialog with shared ext+SaaS logic (HYP-1184, #705)"
    )


def test_no_ticket_ref_falls_back_to_bare_pr_number():
    pr = _pr("fix(cli): resolve symlink before deriving repo root for draw --version", number=8)
    assert format_line(pr) == (
        "Resolve symlink before deriving repo root for draw --version (#8)"
    )


def test_mixed_trailing_paren_with_extra_content_is_stripped_whole():
    # Real title seen in production: the trailing paren carries MORE than a bare ref
    # ("AC #3" alongside "HYP-1180") — the whole block is still the ref parenthetical.
    pr = _pr(
        "feat(matrix): per-run freshness stamp for the nightly e2e server (HYP-1180 AC #3)",
        number=156,
    )
    assert format_line(pr) == (
        "Per-run freshness stamp for the nightly e2e server (HYP-1180, #156)"
    )


def test_multiple_ticket_refs_are_deduplicated_and_ordered():
    pr = _pr(
        "docs(agents): fix stale ext-e2e repo URL, document known nightly-server gaps",
        body="Refs HYP-1180 and also HYP-1299. See also HYP-1180 again.",
        number=737,
    )
    line = format_line(pr)
    assert line == (
        "Fix stale ext-e2e repo URL, document known nightly-server gaps (HYP-1180, HYP-1299, #737)"
    )


def test_title_ticket_ref_wins_over_body_ref():
    pr = _pr("fix: unrelated body ticket test (HYP-1)", body="mentions HYP-2 in the body", number=1)
    assert extract_ticket_refs(pr.title, pr.body) == ["HYP-1"]


def test_no_lowercase_forced_capital_when_already_capitalized():
    pr = _pr("feat: Already Capitalized Title (#42)", number=42)
    assert format_line(pr) == "Already Capitalized Title (#42)"


def test_render_report_groups_by_category_with_header_only_when_nonempty():
    prs = [
        _pr("feat: add new widget (#1)", number=1, repo="a"),
        _pr("fix(security): patch a semgrep finding (#2)", number=2, repo="a"),
    ]
    report = render_report(prs)
    assert "Security" in report
    assert "Product / UX" in report
    assert "Performance" not in report  # no perf item -> no header at all
    assert "Infra / CI" not in report


def test_render_report_empty_input():
    assert render_report([]) == "No merged PRs to report."


def test_render_report_sorts_within_bucket_by_merge_time():
    older = MergedPR(repo="a", number=1, title="feat: first (#1)", body="", merged_at="2026-08-18T00:00:00Z", url="")
    newer = MergedPR(repo="a", number=2, title="feat: second (#2)", body="", merged_at="2026-08-19T00:00:00Z", url="")
    report = render_report([newer, older])
    assert report.index("First") < report.index("Second")


def test_single_repo_report_uses_bare_pr_number():
    pr = _pr("feat: widget (#1)", number=1, repo="owner/a")
    assert "(#1)" in render_report([pr])
    assert "owner/a#1" not in render_report([pr])


def test_multi_repo_report_qualifies_pr_number_to_avoid_collision():
    """PR numbers are per-repo on GitHub — #1 in one repo and #1 in another are
    DIFFERENT pull requests. A report spanning two repos must disambiguate them.
    Regression for the codex review P2 finding."""
    prs = [
        _pr("feat: widget A (#1)", number=1, repo="owner/a"),
        _pr("feat: widget B (#1)", number=1, repo="owner/b"),
    ]
    report = render_report(prs)
    assert "owner/a#1" in report
    assert "owner/b#1" in report
    # Neither line should be left with a bare, ambiguous "(#1)".
    assert "(#1)" not in report


def test_format_line_qualify_flag_direct():
    pr = _pr("feat: widget (#1)", number=1, repo="owner/a")
    assert format_line(pr, qualify_pr_ref=True) == "Widget (owner/a#1)"
    assert format_line(pr, qualify_pr_ref=False) == "Widget (#1)"
