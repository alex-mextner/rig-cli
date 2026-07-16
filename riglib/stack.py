"""Stack-preset taxonomy — parse, validate, and hierarchical prefix matching.

What this is
------------
The single source of truth for the ``stack`` config value's SHAPE (``l1/lang[/framework]``)
and for how a declared repo stack SELECTS the ``by-stack`` skills it inherits. Kept
dependency-free (stdlib only, no config/catalog import) so every layer — the validator, the
plan resolver, the catalog scanner, detection, and the wizard — shares ONE notion of a stack
and can import it without a cycle.

Taxonomy (see docs/specs/2026-07-15-stack-presets.md)
-----------------------------------------------------
- **Level 1** — a CLOSED enum of six domains (:data:`STACK_L1`). It is the fixed spine; a bad
  l1 is a typo and fails closed.
- **Level 2 (lang)** — required, OPEN vocabulary (swift/ts/python/go/rust/…).
- **Level 3 (framework)** — optional, OPEN vocabulary (swiftui/react/django/…).

"Stack" here is the *stack preset*, distinct from ``detect.Environment.stack`` (the build
TOOLCHAIN: bun-node/python-uv/go). The two coexist deliberately; this module only ever means
the preset.

Hierarchical selection
----------------------
A declared stack INHERITS every by-stack skill whose stack path is a PREFIX of (or equal to)
it. Skill stack paths are at least ``l1/lang`` (2 segments) — there is no ``l1``-only skill —
so ``mobile/swift/swiftui`` pulls ``mobile/swift``-level and ``mobile/swift/swiftui``-level
skills. That is what keeps a mobile repo from receiving react skills — ``frontend/ts/react``
is not a prefix of any ``mobile/...`` stack.
"""

from __future__ import annotations

from functools import lru_cache

# The six Level-1 domains. CLOSED enum — validated; a value outside this set fails closed.
STACK_L1: tuple[str, ...] = ("mobile", "frontend", "backend", "desktop", "embedded", "system")


class StackError(ValueError):
    """A malformed stack value. A plain ValueError subclass so callers can wrap it into their
    own error type (config.ConfigError) with a schema path while `except ValueError` still catches."""


@lru_cache(maxsize=256)
def parse_stack(value: str) -> tuple[str, ...]:
    """Split + validate a stack string into its ``(l1, lang[, framework])`` segments.

    Raises :class:`StackError` on any malformed value: wrong segment count, an empty segment
    (leading/trailing/`//`), a non-string, or an l1 outside :data:`STACK_L1`. lang and framework
    are OPEN (any non-empty token). Returns the tuple of 2 or 3 segments on success.

    Pure + cached: ``stack_matches`` re-parses the declared stack once per by-stack catalog item,
    so memoizing the parse keeps that loop O(1) per item on the parse. Inputs are tiny stack
    strings; a malformed input raises and is simply not cached (lru_cache does not cache raises).
    """
    if not isinstance(value, str) or not value.strip():
        raise StackError("stack must be a non-empty string like 'l1/lang[/framework]'")
    raw = value.strip()
    segments = raw.split("/")
    if any(seg.strip() == "" for seg in segments):
        raise StackError(
            f"stack {value!r} has an empty segment; use 'l1/lang' or 'l1/lang/framework' "
            "with no leading/trailing/double slash"
        )
    segments = [seg.strip() for seg in segments]
    if not (2 <= len(segments) <= 3):
        raise StackError(
            f"stack {value!r} must have 2 or 3 segments (l1/lang[/framework]), got {len(segments)}"
        )
    if segments[0] not in STACK_L1:
        raise StackError(
            f"stack level-1 {segments[0]!r} is not one of {list(STACK_L1)} "
            f"(the value was {value!r})"
        )
    return tuple(segments)


def is_valid_stack(value: str) -> bool:
    """True iff ``value`` parses as a well-formed stack. Never raises."""
    try:
        parse_stack(value)
        return True
    except StackError:
        return False


def normalize_stack(value: str) -> str:
    """The canonical string form of a stack (trimmed, single-slash-joined). Raises on malformed."""
    return "/".join(parse_stack(value))


def stack_matches(declared: str, item_stack: str) -> bool:
    """True iff ``item_stack`` is a PREFIX of (or equal to) ``declared`` — i.e. a skill tagged
    ``item_stack`` is inherited by a repo whose stack is ``declared``.

    Both are parsed first; a malformed value on either side is a non-match (never raises), so a
    typo'd stack simply selects nothing rather than exploding the plan.
    """
    try:
        dsegs = parse_stack(declared)
        isegs = parse_stack(item_stack)
    except StackError:
        return False
    if len(isegs) > len(dsegs):
        return False
    return dsegs[: len(isegs)] == isegs
