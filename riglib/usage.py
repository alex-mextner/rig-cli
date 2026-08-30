"""``rig usage`` — real Claude token/cost usage across the accounts on this machine.

Data source: per-message ``usage`` records written by Claude Code itself, one JSON object
per line, under a fixed two-level layout (the same layout
``riglib/stats/sources/claude_code.py`` assumes for the same on-disk format — verified
directly against real logs on this machine, not `**`-glob-recursive):

  * ``~/.claude/projects/<encoded-project-dir>/<session-uuid>.jsonl`` (the "default" account)
  * ``~/.claude-accounts/account-*/projects/<encoded-project-dir>/<session-uuid>.jsonl``
    (each claude-rotate account)

Each line that carries token usage has ``message.usage.{input_tokens,output_tokens,
cache_creation_input_tokens,cache_read_input_tokens}``, ``message.model``, a top-level
``timestamp`` (ISO-8601 UTC), and the pair ``(message.id, requestId)`` that uniquely
identifies one billed exchange. Parsing is streaming/line-by-line (one file open at a
time, ``errors="replace"`` so a single malformed byte can't abort the whole scan) — never
loading a whole file, let alone the whole tree, into memory at once.

Scale defenses beyond line-by-line parsing (added after review found the naive version
re-parses a machine's ENTIRE Claude Code history on every run, including every scheduled
push):

  * **mtime pruning** (:func:`_file_may_hold_events_after`): a session file is append-only,
    so its mtime is always >= the timestamp of its newest line. A file whose mtime predates
    the cutoff (minus a safety margin — see :data:`_MTIME_SAFETY_MARGIN`, which covers a
    clock that stepped backward between writes) can't contain anything in scope and is
    skipped without opening it — turning "parse years of logs to report last week" into
    "parse the files actually touched this week". A stat failure keeps a file in scope
    (fail open, never silently drop real usage).
  * **the pruning cutoff is fixed per clock, not per requested period**
    (:func:`_mtime_prune_cutoff`): file-level pruning changes WHICH files get scanned,
    which changes what dedup's first-occurrence-wins sees as "first" — so a cutoff that
    varied with ``--period`` made ``rig usage --period week`` and the ``week`` section of
    a bare ``rig usage`` disagree about the exact same duplicate message (a real bug,
    caught by review with a reproducible two-file case, fixed by always cutting off at
    ``min(week_start, month_start)`` regardless of which periods were actually requested).
    Dedup itself is unconditional given whichever files DO get scanned (see
    :func:`iter_usage_records`) — the fix lives entirely in making the file SET
    deterministic per clock, not in gating individual records.
  * **single streaming pass for multiple periods**: :func:`build_period_reports` folds one
    record stream into every requested :class:`PeriodReport` in one pass (the bare
    ``rig usage`` needs both week and month) — it never materializes the record stream into
    a list, so peak memory is bounded by the reports' own bucket counts, not by how many
    records were scanned.

Dedup: the same message can appear more than once across files (a ``--resume`` replay, or
a streamed response split across session files) with byte-identical ``usage`` figures —
verified empirically against real logs on this machine before writing this dedup. Records
are deduped by a digest of ``(message.id, requestId)`` — first occurrence wins, globally
across every file scanned. The two fields are length-prefixed before hashing so
``("a:b", "c")`` and ``("a", "b:c")`` can never collide to the same digest. First-wins is a
deliberate streaming-friendly tradeoff: IF a duplicate ever carries different (not just
byte-identical) figures — e.g. a mid-stream checkpoint line followed by a larger final
line — this undercounts using the first (possibly partial) figures rather than the last.

Pricing: matched by model ID string against :data:`PRICING_USD_PER_1M`, exact first, then
(a known Anthropic naming convention, not a guess — see :func:`_pricing_key`) with a
trailing ``-YYYYMMDD`` snapshot-date suffix stripped and retried. A model ID that still
doesn't match is never guessed at — its tokens are reported separately as "unpriced" (see
:attr:`PeriodReport.unpriced_models`). This is a Claude.ai SUBSCRIPTION setup, not
pay-per-token billing: every dollar figure this module produces is a HYPOTHETICAL "if this
usage had been billed at published API list prices" estimate, never an actual bill — see
:data:`COST_DISCLAIMER`, which is threaded into both the JSON and text output so the
caveat travels with the number, not just this docstring.

CLI/JSON contract for the separate, independently-built tg-cli scheduling side: run
``rig usage --json --period week`` at end-of-week and ``rig usage --json --period month``
at end-of-month. Both are pure reads (no state written); the period data itself (totals,
by-model, by-account, labels/bounds) is fully deterministic for a given clock — only the
top-level ``generated_at`` field is real wall-clock time (when the report ran), by design,
same as any "generated at" timestamp. Schema is versioned via :data:`JSON_SCHEMA_VERSION`
so a future change to the shape is detectable rather than silently breaking a consumer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .errors import ConfigError

# ── pricing ──────────────────────────────────────────────────────────────────────────
JSON_SCHEMA_VERSION = 1

COST_DISCLAIMER = (
    "Hypothetical cost at published Claude API list prices. This is a Claude.ai "
    "subscription, not pay-per-token billing — these figures are illustrative only, "
    "never an actual bill."
)

#: USD per 1,000,000 tokens, at published API list prices: {model id: (input, output)}.
PRICING_USD_PER_1M: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
CACHE_WRITE_MULTIPLIER = 1.25  # cache_creation_input_tokens cost this x the input rate
CACHE_READ_MULTIPLIER = 0.1  # cache_read_input_tokens cost this x the input rate

PERIODS: tuple[str, ...] = ("day", "week", "month")
_DEFAULT_PERIODS: tuple[str, ...] = ("week", "month")

_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def _pricing_key(model: str) -> str | None:
    """Resolve ``model`` to its key in :data:`PRICING_USD_PER_1M`, or ``None`` if genuinely
    unpriced. Anthropic sometimes logs a dated snapshot alias of a model already in the
    table — confirmed on this machine's real logs: ``claude-haiku-4-5-20251001`` alongside
    bare ``claude-haiku-4-5``. Stripping a trailing ``-YYYYMMDD`` and retrying resolves a
    KNOWN naming convention for the SAME model; it is not a price guess for an unfamiliar
    one — the "never guess" rule is about a model this table has no entry for at all,
    dated or not."""
    if model in PRICING_USD_PER_1M:
        return model
    stripped = _DATE_SUFFIX_RE.sub("", model)
    if stripped != model and stripped in PRICING_USD_PER_1M:
        return stripped
    return None


@dataclass
class TokenTotals:
    """Raw token counts for one bucket (a model, an account, a period). Mutable — callers
    accumulate into it with :meth:`add`."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(self, other: "TokenTotals") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "total_tokens": self.total_tokens,
        }


def estimate_cost_usd(model: str, totals: TokenTotals) -> float | None:
    """Hypothetical USD cost at list price for ``totals`` under ``model``'s pricing.
    ``None`` when ``model`` (exact or date-suffix-stripped) is not in
    :data:`PRICING_USD_PER_1M` — an unpriced model is reported separately, never guessed
    at."""
    key = _pricing_key(model)
    if key is None:
        return None
    price_in, price_out = PRICING_USD_PER_1M[key]
    return (
        totals.input_tokens * price_in
        + totals.output_tokens * price_out
        + totals.cache_creation_input_tokens * price_in * CACHE_WRITE_MULTIPLIER
        + totals.cache_read_input_tokens * price_in * CACHE_READ_MULTIPLIER
    ) / 1_000_000


# ── ISO-8601 parsing ─────────────────────────────────────────────────────────────────
# A deliberately self-contained copy of `riglib.stats.sources.base.parse_iso` (also
# mirrored, differently, by `riglib.daily.timeutil.parse_utc`): importing the real one
# would pull `riglib.stats.sources.base`'s `Claude Code` module import chain — which
# executes `riglib/stats/__init__.py` (command/aggregate/model) and
# `riglib/stats/sources/__init__.py`'s `_discover()`, registering every harness parser —
# into `rig usage`, a command whose whole design point is staying light and unrelated to
# the tool-adoption-analytics subsystem. ~15 stable stdlib lines duplicated is cheaper than
# that coupling; hoisting all three into one shared leaf module is a reasonable future
# cleanup but is out of scope for this change (touches otherwise-untouched shared code).
def _parse_iso(ts: str | None) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── parsing ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One deduped usage event."""

    timestamp: datetime | None
    account: str  # "default" | "account-0" | "account-1" | ...
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


def _resolve_home(home: Path | None) -> Path:
    return home if home is not None else Path(os.path.expanduser("~"))


def _account_sort_key(account_dir: Path) -> tuple[int, str]:
    """Natural-sort ``account-N`` by the numeric suffix, not lexically — a plain
    ``sorted()`` on directory names would order ``account-10`` before ``account-2`` once a
    machine accumulates a 10th rotated account, which is real nondeterminism in
    ``accounts_scanned``, a field of the documented "stable" JSON contract. A non-numeric
    or missing suffix sorts after every numeric one, so this can't raise on an unexpected
    directory name."""
    suffix = account_dir.name.removeprefix("account-")
    return (1, account_dir.name) if not suffix.isdigit() else (0, f"{int(suffix):020d}")


def resolve_account_roots(home: Path | None = None) -> list[tuple[str, Path]]:
    """``[("default", ~/.claude/projects), ("account-0", ~/.claude-accounts/account-0/projects), ...]``
    — only roots that actually exist on disk. Globs ``account-*`` rather than hardcoding a
    count, so a 4th rotated account is picked up with zero code changes; a missing
    ``~/.claude-accounts`` directory is a normal skip, not an error."""
    home = _resolve_home(home)
    roots: list[tuple[str, Path]] = []
    default_root = home / ".claude" / "projects"
    if default_root.is_dir():
        roots.append(("default", default_root))
    accounts_dir = home / ".claude-accounts"
    if accounts_dir.is_dir():
        for account_dir in sorted(accounts_dir.glob("account-*"), key=_account_sort_key):
            projects = account_dir / "projects"
            if projects.is_dir():
                roots.append((account_dir.name, projects))
    return roots


def _dedup_key(message_id: object, request_id: object) -> bytes | None:
    """A digest for the (message id, request id) pair, or ``None`` when either is missing
    — a record we can't key is never dropped as a false duplicate. Each field is
    length-prefixed before hashing so two different (id, id) pairs can never hash to the
    same digest through a boundary shift (e.g. ``("a:b", "c")`` vs ``("a", "b:c")`` — a
    plain ``f"{a}:{b}"`` join would collide those; this can't)."""
    if not isinstance(message_id, str) or not message_id:
        return None
    if not isinstance(request_id, str) or not request_id:
        return None
    mid = message_id.encode()
    rid = request_id.encode()
    payload = len(mid).to_bytes(4, "big") + mid + len(rid).to_bytes(4, "big") + rid
    return hashlib.blake2b(payload, digest_size=16).digest()


# No real message's usage field is anywhere near this large; a value beyond it is
# corrupted/adversarial input, not real usage. Rejecting it here (same as a negative or
# non-numeric value) is what stops it from later blowing up float arithmetic in
# `estimate_cost_usd` (`int * float` raises `OverflowError` for an int with hundreds of
# digits — confirmed empirically) or, for a merely astronomical integral float like
# `1e308`, silently producing `inf` — which `json.dumps` then emits as a bare `Infinity`
# token, invalid JSON for any strict consumer of the "stable, versioned" contract.
_MAX_PLAUSIBLE_TOKEN_COUNT = 10**15


def _as_token_count(val: object) -> int:
    """Coerce a raw usage field to a plausible non-negative int (see
    :data:`_MAX_PLAUSIBLE_TOKEN_COUNT`). ``bool`` is an ``int`` subclass in Python but is
    never a real token count, so it's rejected even though ``isinstance`` would accept it;
    an integral ``float`` (e.g. a ``123.0`` some JSON producer emitted for a whole-number
    count) is accepted and coerced rather than silently zeroed — this module counts data
    it can make sense of instead of silently dropping it. A negative count, an
    out-of-plausible-range count, a non-integral float (NaN included — comparisons against
    it are always False, so it falls through to the final ``return 0``), or any other type
    is a malformed record and degrades that field to 0 rather than corrupting downstream
    totals/cost. Module-level (not a per-line closure) since this runs once per
    usage-bearing JSONL line across the whole log tree."""
    if isinstance(val, bool):
        return 0
    if isinstance(val, int):
        return val if 0 <= val <= _MAX_PLAUSIBLE_TOKEN_COUNT else 0
    if isinstance(val, float) and val.is_integer() and 0 <= val <= _MAX_PLAUSIBLE_TOKEN_COUNT:
        return int(val)
    return 0


# A clock that steps backward between two writes to the same file (NTP correction, a
# backup/sync tool touching mtimes) would otherwise make `_file_may_hold_events_after`
# prune a file that genuinely still holds in-window events. This margin trades a small
# amount of the pruning win for never silently dropping real usage over it.
_MTIME_SAFETY_MARGIN = timedelta(hours=48)


def _file_may_hold_events_after(path: Path, cutoff: datetime) -> bool:
    """False only when the file's own mtime PROVES every line inside predates ``cutoff``
    (with :data:`_MTIME_SAFETY_MARGIN` of slack for a backward clock step). A stat failure
    (permission, TOCTOU removal) keeps the file in scope — this is a prune, never a filter
    that can make real usage vanish."""
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return True
    return mtime >= cutoff - _MTIME_SAFETY_MARGIN


def _iter_session_records(session_file: Path, account: str) -> Iterator[tuple[bytes | None, UsageRecord]]:
    try:
        # errors="replace": one malformed byte degrades to U+FFFD rather than raising
        # UnicodeDecodeError and aborting the whole scan (same defense as stats' CC parser).
        with session_file.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                # Cheap pre-gate before the expensive full parse: most lines in a real
                # session (user messages, tool-result/file-read payloads — often multi-KB)
                # aren't usage-bearing at all. Any line that IS must contain the literal
                # ASCII key `"usage"` somewhere in it; `errors="replace"` only touches
                # genuinely undecodable bytes, never this structural JSON key, so the
                # substring check is a safe, lossless skip — not an approximation.
                if '"usage"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    # json.JSONDecodeError (malformed JSON) is a ValueError subclass, but
                    # a syntactically valid line can ALSO raise a plain ValueError: Python
                    # 3.11+ caps integer-string conversion length (CVE-2020-10735), so a
                    # ~4300+ digit numeric literal anywhere in the line — corrupted or
                    # adversarial — raises "Exceeds the limit... for integer string
                    # conversion" instead of a JSONDecodeError. Catching the common
                    # ValueError base skips that line too, instead of crashing the whole
                    # scan (confirmed empirically; review finding).
                    continue
                if not isinstance(event, dict):
                    continue
                msg = event.get("message")
                if not isinstance(msg, dict):
                    continue
                raw_usage = msg.get("usage")
                if not isinstance(raw_usage, dict):
                    continue
                model = msg.get("model")
                if not isinstance(model, str) or not model:
                    continue
                key = _dedup_key(msg.get("id"), event.get("requestId"))
                record = UsageRecord(
                    timestamp=_parse_iso(event.get("timestamp")),
                    account=account,
                    model=model,
                    input_tokens=_as_token_count(raw_usage.get("input_tokens")),
                    output_tokens=_as_token_count(raw_usage.get("output_tokens")),
                    cache_creation_input_tokens=_as_token_count(raw_usage.get("cache_creation_input_tokens")),
                    cache_read_input_tokens=_as_token_count(raw_usage.get("cache_read_input_tokens")),
                )
                yield key, record
    except OSError:
        return


def _iter_session_files(root: Path) -> Iterator[Path]:
    """Every ``*.jsonl`` under ``root``'s project subdirectories, guarded end to end
    against ``OSError`` (permission changes, or the directory vanishing mid-scan) so one
    unreadable account never aborts the whole command — the remaining accounts still
    report."""
    try:
        proj_dirs = sorted(root.iterdir())
    except OSError:
        return
    for proj_dir in proj_dirs:
        if not proj_dir.is_dir():
            continue
        try:
            session_files = sorted(proj_dir.glob("*.jsonl"))
        except OSError:
            continue
        yield from session_files


def iter_usage_records(
    home: Path | None = None,
    *,
    roots: list[tuple[str, Path]] | None = None,
    mtime_after: datetime | None = None,
) -> Iterator[UsageRecord]:
    """Stream every usage record across the resolved account roots, deduped by
    ``(message.id, requestId)`` — first occurrence in scan order wins, globally, so a
    message replayed across a ``--resume`` or split across session files is counted once.

    ``roots``: reuse an already-resolved account list (see ``run()``) instead of resolving
    fresh from ``home`` — avoids a second, possibly-inconsistent directory scan.
    ``mtime_after``: skip whole FILES that can't contain anything in the window (see the
    module docstring's "mtime pruning"). It does NOT gate which individual records get
    remembered for dedup — that decision is deliberately independent of any window
    boundary (see below), so ``rig usage --period week`` and the ``week`` section of a
    bare ``rig usage`` (which uses an earlier combined cutoff, month start) report the
    SAME numbers for the same underlying data. An earlier version gated dedup persistence
    on ``record.timestamp >= mtime_after``, which made the result depend on which OTHER
    periods happened to be requested in the same invocation — caught by review as a
    concrete undercount (a duplicate whose first occurrence fell inside the combined
    window but outside `week` could silently zero out `week`'s total depending on whether
    `--period week` was passed alone or as part of the bare run).

    A record's key is remembered as soon as it's seen, UNLESS the record is undated
    (``timestamp is None``): an undated occurrence can never itself land in any window
    (see ``build_period_reports``), so letting it claim the dedup slot would silently
    suppress a later, real, timestamped duplicate of the same message with nothing to
    show for it — pinned by
    ``test_dedup_does_not_drop_an_in_window_duplicate_behind_an_undated_first_occurrence``.
    Two timestamped duplicates (any windows) still follow strict first-wins — the same
    documented tradeoff as byte-differing duplicate figures — pinned by
    ``test_dedup_of_in_window_duplicates_survives_the_mtime_cutoff_boundary`` and
    ``test_dedup_decision_is_independent_of_which_periods_are_requested``.
    """
    seen: set[bytes] = set()
    for account, root in (roots if roots is not None else resolve_account_roots(home)):
        for session_file in _iter_session_files(root):
            if mtime_after is not None and not _file_may_hold_events_after(session_file, mtime_after):
                continue
            for key, record in _iter_session_records(session_file, account):
                if key is not None:
                    if key in seen:
                        continue
                    if record.timestamp is not None:
                        seen.add(key)
                yield record


# ── period bucketing ─────────────────────────────────────────────────────────────────
def period_bounds(period: str, now: datetime | None = None) -> tuple[datetime, datetime, str]:
    """``(start, end, label)`` for ``period`` ("day"|"week"|"month") containing ``now``
    (default: the real current time), all in UTC. ``end`` is exclusive. Weeks are ISO
    weeks (Monday start); the label mirrors ``stats.aggregate``'s bucket format
    (``YYYY-MM-DD`` / ``YYYY-Www`` / ``YYYY-MM``) for consistency across `rig` commands."""
    if period not in PERIODS:
        raise ValueError(f"unknown period: {period!r} (expected one of {PERIODS})")
    now = (now if now is not None else datetime.now(timezone.utc)).astimezone(timezone.utc)
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        label = start.strftime("%Y-%m-%d")
    elif period == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        iso_year, iso_week, _ = start.isocalendar()
        label = f"{iso_year}-W{iso_week:02d}"
    else:  # "month"
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start.replace(year=start.year + 1, month=1) if start.month == 12
               else start.replace(month=start.month + 1))
        label = start.strftime("%Y-%m")
    return start, end, label


@dataclass
class ModelRow:
    """One model's totals + priced-or-not cost — the single source of truth per model, so
    a JSON/text renderer never has to re-join separately-mutated dicts that could drift."""

    totals: TokenTotals = field(default_factory=TokenTotals)
    cost_usd: float | None = None  # set once, after all records for the period are folded in

    def to_dict(self) -> dict:
        return {
            "totals": self.totals.to_dict(),
            "estimated_cost_usd": round(self.cost_usd, 4) if self.cost_usd is not None else None,
        }


@dataclass
class PeriodReport:
    period: str
    label: str
    start: datetime
    end: datetime
    total: TokenTotals = field(default_factory=TokenTotals)
    by_model: dict[str, ModelRow] = field(default_factory=dict)
    by_account: dict[str, TokenTotals] = field(default_factory=dict)
    priced_cost_usd: float = 0.0
    # Records with no parseable timestamp — counted, never silently dropped. This is a
    # property of the SCAN, not of this one window: when multiple periods are built from
    # one pass (build_period_reports), every report ends up with the IDENTICAL count (an
    # undated record can't be placed into any specific window, so it's attributed to all
    # of them equally) — used for the per-period text line, but deliberately NOT exposed
    # per-period in the JSON contract (see render_json's top-level
    # "undated_records_scanned"): a naive consumer summing a per-period JSON field across
    # "week" + "month" would double the real count, a footgun caught by review before any
    # consumer existed to depend on the wrong shape.
    undated: int = 0

    @property
    def unpriced_models(self) -> dict[str, TokenTotals]:
        return {model: row.totals for model, row in self.by_model.items() if row.cost_usd is None}

    @property
    def all_models_unpriced(self) -> bool:
        return bool(self.by_model) and all(row.cost_usd is None for row in self.by_model.values())

    def to_dict(self) -> dict:
        ordered_models = sorted(self.by_model.items(), key=lambda kv: -kv[1].totals.total_tokens)
        ordered_unpriced = sorted(self.unpriced_models.items(), key=lambda kv: -kv[1].total_tokens)
        return {
            "period": self.period,
            "label": self.label,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "totals": self.total.to_dict(),
            "estimated_cost_usd": round(self.priced_cost_usd, 4),
            # An unattended JSON consumer reading `estimated_cost_usd: 0.0` can't tell
            # "genuinely zero usage" from "every model in this window is unpriced" without
            # this flag — the text renderer already distinguishes them; this is the same
            # distinction, additive-stable, for the JSON contract (review finding).
            "all_models_unpriced": self.all_models_unpriced,
            "by_model": {model: row.to_dict() for model, row in ordered_models},
            "by_account": {
                account: totals.to_dict()
                for account, totals in sorted(self.by_account.items(), key=lambda kv: -kv[1].total_tokens)
            },
            "unpriced_tokens": {model: totals.to_dict() for model, totals in ordered_unpriced},
        }


def _fold_record(report: PeriodReport, rec: UsageRecord) -> None:
    """Accumulate one in-window record into the report's total/by-model/by-account
    buckets. Dict entries are created only on a genuine miss (``dict.get`` + create, not
    ``setdefault``, which always builds its default eagerly even on a hit)."""
    totals = TokenTotals(
        input_tokens=rec.input_tokens,
        output_tokens=rec.output_tokens,
        cache_creation_input_tokens=rec.cache_creation_input_tokens,
        cache_read_input_tokens=rec.cache_read_input_tokens,
    )
    report.total.add(totals)

    row = report.by_model.get(rec.model)
    if row is None:
        row = ModelRow()
        report.by_model[rec.model] = row
    row.totals.add(totals)

    account_totals = report.by_account.get(rec.account)
    if account_totals is None:
        account_totals = TokenTotals()
        report.by_account[rec.account] = account_totals
    account_totals.add(totals)


def _price_reports(reports: Iterable[PeriodReport]) -> None:
    for report in reports:
        for model, row in report.by_model.items():
            row.cost_usd = estimate_cost_usd(model, row.totals)
            if row.cost_usd is not None:
                report.priced_cost_usd += row.cost_usd


def build_period_reports(
    records: Iterable[UsageRecord], periods: Iterable[str], now: datetime | None = None
) -> dict[str, PeriodReport]:
    """Fold ONE record stream into every requested period's report in a single pass — the
    streaming counterpart to calling :func:`build_period_report` once per period, which
    would force materializing ``records`` into a list to iterate it more than once. Pure:
    no I/O. This is what ``run()`` uses for the bare (week + month) invocation."""
    windows = {p: period_bounds(p, now=now) for p in periods}
    reports = {
        p: PeriodReport(period=p, label=label, start=start, end=end)
        for p, (start, end, label) in windows.items()
    }
    for rec in records:
        if rec.timestamp is None:
            for report in reports.values():
                report.undated += 1
            continue
        for p, (start, end, _label) in windows.items():
            if start <= rec.timestamp < end:
                _fold_record(reports[p], rec)
    _price_reports(reports.values())
    return reports


def build_period_report(
    records: Iterable[UsageRecord], period: str, now: datetime | None = None
) -> PeriodReport:
    """Single-period convenience wrapper over :func:`build_period_reports`."""
    return build_period_reports(records, [period], now=now)[period]


# ── rendering ────────────────────────────────────────────────────────────────────────
def render_json(reports: dict[str, PeriodReport], *, accounts_scanned: list[str]) -> dict:
    """The stable, versioned JSON contract ``rig usage --json`` emits and the tg-cli
    scheduled push consumes:

        {
          "schema": 1,
          "generated_at": "<ISO-8601 UTC, real wall-clock time of report generation>",
          "disclaimer": "<COST_DISCLAIMER>",
          "accounts_scanned": ["default", "account-0", ...],
          "undated_records_scanned": <int>,
          "periods": {
            "<period name>": { ...PeriodReport.to_dict()... },
            ...
          }
        }

    ``undated_records_scanned`` is scan-level, not per-period, deliberately: an undated
    record can't be placed into any specific window, so every requested period would
    otherwise carry the SAME count — a naive consumer summing it across "week" and "month"
    would double the real number. One top-level field avoids that footgun outright (all
    requested periods share one scan, so any report's ``.undated`` is that same value).

    Keys are additive-stable: a future change adds fields, never renames/removes one a
    consumer may already read.
    """
    return {
        "schema": JSON_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer": COST_DISCLAIMER,
        "accounts_scanned": accounts_scanned,
        "undated_records_scanned": next(iter(reports.values())).undated if reports else 0,
        "periods": {name: report.to_dict() for name, report in reports.items()},
    }


def _fmt_usd(amount: float) -> str:
    return f"${amount:,.2f}"


def _fmt_tokens(n: int) -> str:
    return f"{n:,}"


# Model/account strings come from on-disk JSONL (or, for accounts, a directory name); JSON
# output escapes them automatically, but plain text does not. Stripping control characters
# before they reach the terminal closes off a crafted/corrupted log injecting escape
# sequences via `model`. Covers both C0 controls + DEL (\x00-\x1f, \x7f) AND the C1 range
# (\x80-\x9f, e.g. U+009B — a single-character CSI some terminals still honor) — a C0-only
# filter leaves that range open (review finding, confirmed empirically).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _sanitize_display(s: str) -> str:
    return _CONTROL_CHARS_RE.sub("", s)


def _render_period_text(name: str, report: PeriodReport) -> list[str]:
    cost_line = (
        "  estimated cost: unpriced (no priced models in this window)"
        if report.all_models_unpriced
        else f"  estimated cost: {_fmt_usd(report.priced_cost_usd)}"
    )
    lines = [
        f"{name.capitalize()} {report.label}  "
        f"({report.start.date()} .. {(report.end - timedelta(days=1)).date()}, UTC)",
        f"  total tokens: {_fmt_tokens(report.total.total_tokens)}"
        f"  (in {_fmt_tokens(report.total.input_tokens)}"
        f" / out {_fmt_tokens(report.total.output_tokens)}"
        f" / cache-write {_fmt_tokens(report.total.cache_creation_input_tokens)}"
        f" / cache-read {_fmt_tokens(report.total.cache_read_input_tokens)})",
        cost_line,
    ]
    if report.by_model:
        lines.append("  by model:")
        for model, row in sorted(report.by_model.items(), key=lambda kv: -kv[1].totals.total_tokens):
            cost_str = _fmt_usd(row.cost_usd) if row.cost_usd is not None else "unpriced"
            lines.append(
                f"    {_sanitize_display(model):<28} {_fmt_tokens(row.totals.total_tokens):>14} tok   {cost_str}"
            )
    if report.by_account:
        lines.append("  by account:")
        for account, totals in sorted(report.by_account.items(), key=lambda kv: -kv[1].total_tokens):
            lines.append(f"    {_sanitize_display(account):<28} {_fmt_tokens(totals.total_tokens):>14} tok")
    if report.undated:
        lines.append(f"  ({report.undated} record(s) had no timestamp and were excluded from this window)")
    lines.append("")
    return lines


def render_text(reports: dict[str, PeriodReport], *, accounts_scanned: list[str]) -> str:
    """Plain-text human report — stdlib-only, no rich dependency (this command must stay
    fast and dependency-light like every other `rig` command)."""
    # accounts_scanned is directory names (from disk, not JSONL content), but sanitize
    # them the same way as model/account rows — the header is text output too, and a
    # crafted claude-rotate account directory name shouldn't get a free pass just because
    # it landed here instead of in a per-account row (review finding).
    sanitized_accounts = [_sanitize_display(a) for a in accounts_scanned]
    lines: list[str] = [
        f"rig usage — accounts scanned: {', '.join(sanitized_accounts) or '(none found)'}",
        COST_DISCLAIMER,
        "",
    ]
    for name, report in reports.items():
        lines.extend(_render_period_text(name, report))
    return "\n".join(lines).rstrip() + "\n"


# ── CLI wiring ───────────────────────────────────────────────────────────────────────
def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--period", choices=PERIODS, default=None,
        help="report only this window (default: both the current week and current month)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    # hidden seams: tests/scripts can point the whole pipeline at a sandbox HOME/clock.
    parser.add_argument("--home", help=argparse.SUPPRESS)
    parser.add_argument("--now", help=argparse.SUPPRESS)  # ISO-8601 UTC


def _resolve_now(args: argparse.Namespace) -> datetime:
    """The real clock, unless the hidden ``--now`` test/scheduler seam pins it. A ``--now``
    that's present but unparseable fails LOUD (:class:`ConfigError`, exit 2) rather than
    silently falling back to the real clock — this flag exists precisely so an unattended
    caller can pin the window; a typo silently reporting the wrong week/month with no
    signal is the exact failure this repo already fixed once for `--since`
    (``riglib/daily/command.py::_parse_since_arg``)."""
    now_arg = getattr(args, "now", None)
    if now_arg is None:
        return datetime.now(timezone.utc)
    parsed = _parse_iso(now_arg)
    if parsed is None:
        raise ConfigError(
            what=f"--now value {now_arg!r} is not a valid ISO-8601 timestamp",
            why="this is the clock-pin seam an unattended scheduled caller relies on; "
            "silently falling back to the real clock would report the wrong week/month "
            "with no signal that anything went wrong",
            fix="pass an ISO-8601 UTC timestamp, e.g. --now 2026-08-27T12:00:00Z",
        )
    return parsed


def _mtime_prune_cutoff(now: datetime) -> datetime:
    """The mtime-pruning cutoff for a given clock — deliberately ``min(week_start,
    month_start)``, NOT keyed off which ``--period`` was actually requested.

    File-level pruning removes whole files from the scan, which changes which files
    dedup's "first occurrence in scan order" gets to see — so a cutoff that varies with
    the requested periods made ``rig usage --period week`` and the ``week`` section of a
    bare ``rig usage`` (which used a wider, month-start cutoff) disagree about the SAME
    underlying duplicate message: a message replayed across two files, one old enough to
    be pruned only under the narrower cutoff, could be counted under one invocation and
    silently dropped under the other (caught by review with a concrete repro; reproduced
    and pinned by ``test_mtime_cutoff_is_independent_of_requested_period_two_file_repro``).
    Using the SAME cutoff for every invocation, regardless of ``--period``, makes which
    files get scanned deterministic for a given ``now`` and log tree — the property the
    "stable, versioned" JSON contract actually needs. ``day`` never needs its own case:
    a day's start is always >= that day's week's start, so day is already covered by
    ``week_start``; only week vs. month needs the explicit min (a week straddling a month
    boundary can start in the PRIOR month, so month_start is not always the wider one)."""
    week_start = period_bounds("week", now=now)[0]
    month_start = period_bounds("month", now=now)[0]
    return min(week_start, month_start)


def run(args: argparse.Namespace) -> int:
    # `is not None`, not truthiness: an explicit `--home ""` is a deliberate (if unusual)
    # override and must not be silently reinterpreted as "no override, use the real HOME"
    # (same reasoning as `riglib/daily/command.py`'s documented `--config` handling).
    home_arg = getattr(args, "home", None)
    home = Path(os.path.expanduser(home_arg)) if home_arg is not None else None
    now = _resolve_now(args)
    periods = [args.period] if getattr(args, "period", None) else list(_DEFAULT_PERIODS)
    earliest_start = _mtime_prune_cutoff(now)

    roots = resolve_account_roots(home)
    accounts_scanned = [name for name, _ in roots]
    records = iter_usage_records(roots=roots, mtime_after=earliest_start)
    reports = build_period_reports(records, periods, now=now)

    if getattr(args, "json", False):
        print(json.dumps(render_json(reports, accounts_scanned=accounts_scanned), indent=2))
    else:
        print(render_text(reports, accounts_scanned=accounts_scanned), end="")
    return 0
