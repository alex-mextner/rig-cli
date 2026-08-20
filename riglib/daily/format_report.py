"""Mechanical title -> Slack-line transform, and the full report renderer.

The house rule (CLAUDE.md, "Rule for internal identifiers"): plain language first, the
ticket/PR reference last in parentheses, never a bare internal codename with no
explanation. This module applies that rule MECHANICALLY to a conventional-commit PR
title — no LLM call:

    "feat(component-create): guided New-component dialog with shared ext+SaaS logic
    (HYP-1184) (#705)"
    -> "Guided new-component dialog with shared ext+SaaS logic (HYP-1184, #705)"

Steps: strip the leading `type(scope):` prefix, strip trailing `(HYP-NNNN)` /
`(#NNN)` / `(Closes #NNN)` parens (title-side; the PR number itself always comes
from gh's own `number` field, not a title parse), collect any HYP-NNNN ticket refs
(falling back to the PR body when the title has none), and re-append them as one
combined parenthetical.
"""

from __future__ import annotations

import re

from .categorize import CATEGORY_ORDER, categorize
from .model import MergedPR

_CONVENTIONAL_PREFIX_RE = re.compile(r"^\w+(?:\([^)]*\))?!?:\s*")
# A trailing paren block that MENTIONS a ticket/PR ref, not just one that IS exactly a
# bare ref — real titles carry extras inside it too ("(HYP-1180 AC #3)"), and the whole
# block is still just the ref parenthetical the house style wants stripped and re-combined.
# NOTE: no `\b` before `#\d+` — `#` is itself a non-word char, so a `\b` immediately before
# it (between two non-word chars, e.g. "(#705)") never matches; `\b` is only needed before
# the word-starting `HYP-\d+` alternative.
_TRAILING_REF_RE = re.compile(r"\s*\([^()]*(?:\bHYP-\d+\b|#\d+\b)[^()]*\)\s*$", re.I)
_HYP_TICKET_RE = re.compile(r"\bHYP-\d+\b")


def extract_ticket_refs(title: str, body: str) -> list[str]:
    """Ticket IDs mentioned in the title, else the body — title wins so an explicit
    per-line ticket isn't shadowed by an unrelated one mentioned deeper in the body.
    Order-preserving, de-duplicated."""
    refs = _HYP_TICKET_RE.findall(title) or _HYP_TICKET_RE.findall(body)
    seen: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.append(ref)
    return seen


def format_line(pr: MergedPR, *, qualify_pr_ref: bool = False) -> str:
    """One plain-language fact line: "<what changed> (<ticket>, #<pr>)". PR numbers are
    per-repo on GitHub, so ``owner/repo#123`` from one repo and ``#123`` from another are
    NOT the same PR — pass ``qualify_pr_ref=True`` (whenever a report spans more than one
    repo; see :func:`render_report`) to disambiguate as ``owner/repo#123``."""
    plain = _CONVENTIONAL_PREFIX_RE.sub("", pr.title.strip())
    # Trailing ref parens can stack ("... (HYP-1184) (#705)") — strip repeatedly.
    while True:
        stripped = _TRAILING_REF_RE.sub("", plain)
        if stripped == plain:
            break
        plain = stripped
    plain = plain.strip()
    if plain and plain[0].islower():
        plain = plain[0].upper() + plain[1:]

    pr_ref = f"{pr.repo}#{pr.number}" if qualify_pr_ref else f"#{pr.number}"
    refs = extract_ticket_refs(pr.title, pr.body)
    ref_str = ", ".join([*refs, pr_ref])
    return f"{plain} ({ref_str})"


def render_report(prs: list[MergedPR]) -> str:
    """The full paste-into-Slack report: one category header per non-empty bucket,
    one fact per line, sorted within a bucket by merge time (oldest first — the
    order they actually landed in). PR references are repo-qualified (``owner/repo#123``)
    whenever ``prs`` spans more than one distinct repo — otherwise a bare ``#123`` from
    one repo is visually indistinguishable from an unrelated ``#123`` in another."""
    if not prs:
        return "No merged PRs to report."

    qualify = len({pr.repo for pr in prs}) > 1
    buckets: dict[str, list[MergedPR]] = {name: [] for name in CATEGORY_ORDER}
    for pr in prs:
        buckets[categorize(pr)].append(pr)

    blocks: list[str] = []
    for name in CATEGORY_ORDER:
        items = buckets[name]
        if not items:
            continue
        items = sorted(items, key=lambda p: p.merged_at)
        lines = [format_line(pr, qualify_pr_ref=qualify) for pr in items]
        blocks.append(name + "\n" + "\n".join(lines))
    return "\n\n".join(blocks)
