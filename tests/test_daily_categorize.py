"""Category precedence — labels > security > performance > infra/CI > product/UX > Other."""

from __future__ import annotations

from riglib.daily.categorize import categorize
from riglib.daily.model import MergedPR


def _pr(title: str, body: str = "", labels: list[str] | None = None) -> MergedPR:
    return MergedPR(
        repo="a", number=1, title=title, body=body,
        merged_at="2026-08-19T00:00:00Z", url="", labels=labels or [],
    )


def test_security_keyword_beats_feat_type():
    pr = _pr("feat: patch a semgrep finding in the auth path (#1)")
    assert categorize(pr) == "Security"


def test_ci_type_is_infra():
    pr = _pr("ci: close billing-block fallback gap (#1)")
    assert categorize(pr) == "Infra / CI"


def test_chore_release_scope_is_infra():
    pr = _pr("chore(release): bump extension to 0.1.74 (#1)")
    assert categorize(pr) == "Infra / CI"


def test_bare_chore_with_no_infra_signal_is_other():
    pr = _pr("chore: tidy up variable names (#1)")
    assert categorize(pr) == "Other"


def test_perf_type_is_performance():
    pr = _pr("perf(canvas): cache the fiber source index lookup (#1)")
    assert categorize(pr) == "Performance"


def test_feat_defaults_to_product_ux():
    pr = _pr("feat(component-create): guided dialog for shared logic (#1)")
    assert categorize(pr) == "Product / UX"


def test_fix_defaults_to_product_ux():
    pr = _pr("fix(drag): correct resize handle offset (#1)")
    assert categorize(pr) == "Product / UX"


def test_docs_with_no_signal_falls_to_other_not_dropped():
    pr = _pr("docs(spec): note a clarification in the write pipeline (#1)")
    assert categorize(pr) == "Other"


def test_label_wins_over_title_signal():
    pr = _pr("feat: add a widget (#1)", labels=["security"])
    assert categorize(pr) == "Security"


def test_ghas_codeql_keyword_in_body_is_security():
    pr = _pr("fix: repair a broken gate (#1)", body="This fixes the CodeQL check.")
    assert categorize(pr) == "Security"
