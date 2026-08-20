"""Category inference — purely mechanical, no LLM call.

Precedence (checked in order, first match wins): a recognized GitHub label, then a
security signal (keyword or `security`-flavored scope), then performance, then
infra/CI, then the conventional-commit type `feat`/`fix` as the Product/UX default.
Anything left over — `docs:`, `refactor:`, `test:`, or a title with no recognizable
signal at all — falls into "Other" rather than being silently dropped: completeness
("did this PR make it into the report at all") matters more than a clean bucket for
every item.

Checked live against this repo's actual PR history (`hyperide/hyper-saas`,
`hyperide/hyper-ext-e2e`, 2026-08): neither repo uses PR labels at all, so title/body
text is the only signal that exists in practice today; label matching is kept as the
higher-precedence path for if/when that changes.
"""

from __future__ import annotations

import re

from .model import MergedPR

CATEGORY_ORDER: tuple[str, ...] = (
    "Security",
    "Infra / CI",
    "Performance",
    "Product / UX",
    "Other",
)

_CONVENTIONAL_RE = re.compile(r"^(?P<type>\w+)(?:\((?P<scope>[^)]*)\))?!?:\s*")

_LABEL_MAP: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"security|vuln", re.I), "Security"),
    (re.compile(r"perf(ormance)?", re.I), "Performance"),
    (re.compile(r"^(ci|infra)$", re.I), "Infra / CI"),
    (re.compile(r"product|ux|ui", re.I), "Product / UX"),
)

_SECURITY_RE = re.compile(
    r"\b(security|semgrep|trivy|codeql|vulnerabilit\w*|CVE-\d|GHSA-[\w-]+|"
    r"secret[- ]scan\w*|exploit|privilege escalation)\b",
    re.I,
)
_PERF_RE = re.compile(
    r"\b(perf(ormance)?|faster|latency|throughput|slowness|memory leak|optimi[sz]e\w*)\b",
    re.I,
)
_INFRA_RE = re.compile(
    r"\b(ci pipeline|github actions|workflow|runner|billing.?block|deploy(ment)?|"
    r"release\b|dependency|dependencies|lockfile)\b",
    re.I,
)
_INFRA_TYPES = frozenset({"ci", "build"})
_INFRA_CHORE_SCOPE_RE = re.compile(r"ci|release|deploy|deps?|dependency", re.I)
_PRODUCT_TYPES = frozenset({"feat", "fix"})


def categorize(pr: MergedPR) -> str:
    """One of :data:`CATEGORY_ORDER` for this merged PR."""
    label_cat = _from_labels(pr.labels)
    if label_cat:
        return label_cat

    text = f"{pr.title}\n{pr.body}"
    if _SECURITY_RE.search(text):
        return "Security"
    if _PERF_RE.search(text):
        return "Performance"

    ctype, scope = _conventional_type_scope(pr.title)
    if ctype == "perf":
        return "Performance"
    if ctype in _INFRA_TYPES:
        return "Infra / CI"
    if ctype == "chore" and _INFRA_CHORE_SCOPE_RE.search(scope):
        return "Infra / CI"
    if _INFRA_RE.search(text):
        return "Infra / CI"
    if ctype in _PRODUCT_TYPES:
        return "Product / UX"
    return "Other"


def _from_labels(labels: list[str]) -> str | None:
    for label in labels:
        for pattern, category in _LABEL_MAP:
            if pattern.search(label):
                return category
    return None


def _conventional_type_scope(title: str) -> tuple[str, str]:
    m = _CONVENTIONAL_RE.match(title.strip())
    if not m:
        return "", ""
    return m.group("type").lower(), (m.group("scope") or "").lower()
