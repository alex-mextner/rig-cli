# Project Evolve Portal Specification

Status: draft
Owner ticket: #86
Requested proof target: `/Users/ultra/work/hyperide`
Primary rig surface: `rig evolve`

## Goal

Build a local, highly interactive React portal for understanding how a project evolves over
time. The portal combines a time histogram, a Windirstat-like code surface, symbol-level
navigation, git/LSP history, and ecosystem context from rig, task, tg, review, Haft, Sverklo,
Serena, and related tools.

The first durable artifact for this work is this file: `SPEC.md`. Do not create or reference
`SPEK.md`. The correction from `SPEK.md` to `SPEC.md` is a filename correction only; it does
not reduce the requested implementation scope.

## Agent Operating Rule

For this project, the main agent should act as an orchestrator. Independent work must be delegated
to parallel subagents whenever it can be separated by ownership or verification target. The main
thread keeps the plan, integrates results, verifies claims, and avoids doing long serial execution
inline unless a task is genuinely a one-command fix or a shared-file change that must serialize.

## Product Shape

`rig evolve` is the new portal entry point. It must use the same ecosystem lifecycle shape as
`rig config-web` and review dashboards:

- `rig evolve run`
- `rig evolve start`
- `rig evolve stop`
- `rig evolve status`
- `rig evolve enable`
- `rig evolve disable`
- `rig evolve _serve` as the hidden foreground server target

The service must delegate lifecycle management to `agenttools-service`; rig must not duplicate
launchd/systemd/service supervision code. The server binds `127.0.0.1` by default and can be
exposed deliberately through the machine's Tailscale address or a Tailscale Serve/Funnel layer
when the operator asks for a review URL.

`agenttools-service` and `agenttools-daemon` must be provisioned reproducibly. A developer should
not have to discover and manually install editable dependencies from a sibling checkout after a
failed `rig evolve status`; `rig doctor`, docs, and/or install tooling must surface the exact
installation path and fail closed when the shared service runtime is absent.

The portal opens to a project list containing projects discovered from:

- The current `rig` repo context.
- Repos with `rig.yaml`.
- `task-cli`'s project registry and active tickets.
- `sverklo list` registered repositories.
- Known active tool workspaces where discovery is cheap and fail-soft.

Every source must report health. A stale, locked, unauthenticated, or missing source is visible
in the UI instead of silently degrading the view.

## Core View

The top region is a large histogram over time. It supports day, week, and month buckets. Each
bucket carries at least:

- Commit count.
- Changed files.
- Added/deleted line totals.
- PR count when available.
- Review activity when available.
- Task/tg activity density when available.
- Highlight markers for selected modules/symbols that were touched in that bucket.

Below the histogram is a code surface: a proportional, rectangular treemap representing the
current or selected historical project snapshot.

The treemap hierarchy is:

- Repo or monorepo root.
- Subrepos/workspaces/packages as top frames when present.
- Heuristic file groups, configurable per project.
- Files.
- Classes/components/modules.
- Functions, nested functions, methods.
- Significant variables or exported constants when the parser can extract them reliably.

Types, imports/exports, and comments are metadata, not default rectangles. They may appear in
details panels or overlays, but they should not inflate the main surface by default.

The visual layout must be a Windirstat-style squarified mosaic, not a slice-and-dice partition
view. Long stripe-only layouts are a failed render even if their areas are mathematically
proportional. Visual proof for this feature must show mixed-aspect rectangles with nested framed
groups.

Rectangle area scales with code size. The default size metric is source bytes or non-comment
non-blank lines, with a switch for churn-weighted area. The full project canvas can grow beyond
the viewport; users can pan and zoom.

## Navigation And Selection

Clicking a rectangle selects the topmost meaningful block under the pointer, excluding subrepo
frames unless the user explicitly targets a frame. Repeated clicks drill to deeper children.

Keyboard navigation must feel close to Figma:

- `Enter` drills into the selected node.
- `Shift+Enter` moves back up.
- Arrow keys move among siblings.
- `Tab` skips up one level and moves to the next sibling group.
- Escape clears transient overlays.

When selection changes:

- The selected node is outlined.
- Parents are subtly retained as context.
- Related nodes are highlighted by relationship type.
- Histogram buckets touching that node are highlighted.
- A side panel shows parameters, ownership, git history, references, tasks, reviews, tg context,
  and source excerpts.
- Monaco opens the selected code with goto support when a source range is known.

If a selected node does not exist in a selected historical snapshot, the portal preserves the
selection as an external ghost item: grey, dashed, outside the current treemap, with the last
known location and disappearance explanation when available.

## Temporal Interaction

Clicking a histogram bucket changes the treemap to the project state at the end of that period.
The selected node remains selected if it can be tracked. If it moved, the viewport animates to
the new location. If it disappeared, it becomes a ghost selection.

The timeline must support:

- Current snapshot.
- End-of-day, end-of-week, end-of-month snapshots.
- Range brushing for churn overlays.
- Compare mode between two buckets.
- A clear "data is incomplete" state for missing git history, shallow clones, missing LSP data,
  or failed ecosystem-source reads.

## Pointer, Touch, And Hit Testing

Desktop:

- Pressing Shift and holding the pointer still for 300 ms opens a cursor-adjacent picker listing
  the containment stack under the cursor.
- Hovering a picker row highlights the corresponding rectangle.
- Clicking a picker row selects it and closes the picker.

Mobile/tablet:

- Any tap on the treemap opens the containment picker.
- Pinch zoom and touch pan must work on the treemap.
- The picker must be thumb-usable and not rely on hover.

## Relationships

For any selected symbol/module, show:

- Uses.
- Used by.
- Imports/exports as metadata.
- Calls/called-by where available.
- Type relationships where available, but not as primary rectangles.
- File/module membership.
- Task/tg/review references.
- Git commits and PRs touching it.
- JSDoc/docblock summary and tags.

Relationship quality must be labeled. For example:

- LSP exact.
- Tree-sitter exact range.
- Git rename-follow inferred.
- Text-search approximate.
- Tool unavailable.

The UI represents quality as explicit text chips beside each edge/list row, not only color. Chips
use stable labels and may add muted color/icon accents for scanning. Approximate or degraded
relationships must remain filterable and must never visually merge with exact relationships.

## Tracking Across History

Tracking must account for:

- Moves inside a file.
- File renames.
- Cross-file moves.
- Symbol renames.
- Splits.
- Merges.
- Extract method / inline method style refactors where detectable.

The tracking model should combine:

- Stable path and symbol identifiers for the current snapshot.
- `git log --follow` and rename/copy detection.
- Similarity hashing over symbol bodies.
- AST fingerprints for language-supported code.
- LSP references where available.
- Sverklo and Serena indexes when available.
- Manual override/annotation hooks for ambiguous split/merge events.

Ambiguity is not a failure. The portal must show confidence and alternatives instead of inventing
a single false lineage.

## Ecosystem Data Sources

The portal is an aggregator. Tools should be able to contribute data through a standard local
provider contract.

Initial providers:

- `git`: commits, authors, changed paths, rename detection, blame, diffs, PR refs.
- `lsp`: definitions, references, symbols, diagnostics when available.
- `tree-sitter`: structure and ranges for supported languages.
- `sverklo`: registered repositories, index status, dependency rank, digest, audit history,
  symbol/code search data when exposed.
- `Serena`: LSP-backed symbol overview, definition, references, diagnostics, and rename-aware
  code intelligence when its MCP tools are available.
- `haft`: project decisions, problems, evidence, WorkCommissions, spec carrier status, and
  governance health.
- `task`: original user requests, follow-up tasks, statuses, blockers, screenshots, and
  acceptance state.
- `tg`: inbound/outbound discussion context, voice-transcribed requests, decisions, reports, and
  currently active work.
- `review`: review runs, findings, reviewer models, unresolved comments, durations, and evidence.
- `rig`: project config, applied areas, drift/status, installed tools, harness state, and service
  lifecycle health.
- JSDoc/docblocks: extracted symbol descriptions, params, returns, examples, deprecations, and
  tags.

Provider failure is part of the model:

- Warning / partial data.
- Missing binary.
- Missing auth.
- Stale index.
- Database lock.
- Parse failure.
- Unsupported language.
- Shallow git history.
- Permission denied.

The UI must show source health and let the user retry or inspect raw provider output.

## Standard Provider Contract

Each tool can add information to the portal by emitting JSON in a versioned shape:

```json
{
  "schema": "rig.evolve.provider.v1",
  "provider": "task",
  "project": "/absolute/project/path",
  "generated_at": "2026-06-29T00:00:00Z",
  "health": {
    "status": "ok",
    "message": "optional human-readable detail",
    "warnings": []
  },
  "entities": [],
  "events": [],
  "relationships": [],
  "attachments": []
}
```

`health.status` is one of `ok`, `warning`, or `error`. Use `warning` for partial functionality or
non-critical gaps such as optional Haft API keys missing while core local carriers are readable.
`entities` describe durable things such as projects, modules, symbols, tasks, reviews, decisions,
and tg messages. `events` describe temporal activity. `relationships` connect entities. The portal
stores raw provider payloads for inspection and normalizes them into the shared index.

Every provider string is untrusted. The UI must render provider data through text-safe DOM APIs or
explicit sanitizers before it reaches details panels, relationship labels, raw inspection views, or
Monaco decorations. Raw payload inspection shows escaped/preformatted JSON, never executable HTML.

## Caching

The browser should preload children of the visible project/module and cache them with rotation.

Server-side cache:

- Lives under the user's cache/state directory, not the repo.
- Is keyed by project path, git HEAD, selected historical commit, provider version, and schema
  version.
- Has explicit invalidation for git HEAD changes, provider config changes, and stale index
  signals.
- Treats provider payloads as potentially sensitive local data. Default permissions should be
  user-only where the platform supports it, cache entries need retention/rotation, and a later
  encryption-at-rest decision must be driven by the provider data sensitivity.

Browser cache:

- Uses IndexedDB for larger data.
- Keeps recent project snapshots and symbol details.
- Evicts by LRU and by schema/provider version.
- Never treats cached provider failures as permanent without retry affordance.
- Must expose clear-cache behavior and avoid storing sensitive raw payloads in IndexedDB unless
  the provider explicitly marks them safe for browser-side retention.

## UX Principles

This is an operational tool, not a marketing page. The first screen is the usable viewer, not a
hero section.

Relevant visualization lessons:

- Tufte: maximize useful data density, avoid decorative chart junk, use small multiples and
  compact comparison where they increase understanding.
- Shneiderman/Johnson treemaps: use space-filling hierarchy for overview and relative size.
- History Flow: make temporal persistence, deletion, churn, and authorship visible rather than
  reducing history to isolated commits.
- CodeCity/software visualization: preserve locality and navigable structure, but avoid a purely
  aesthetic metaphor that hides metrics.

Design constraints:

- Dense, quiet, work-focused UI.
- Clear labels only where they fit; otherwise use hover/focus/selection panels.
- Accessible contrast and keyboard navigation.
- Avoid color-only encoding; use line style, opacity, and shape for confidence/status.
- Use animation only to preserve object constancy: selection moves, snapshot transitions, relation
  reveal, zoom/pan. Avoid decorative motion.
- The main canvas should not be inside a decorative card.

## Avoid

- Treating incomplete source data as truth.
- Hand-rolled service lifecycle code.
- A static screenshot-like dashboard with no click-through.
- A tree that renders every directory blindly.
- Beautiful but uninspectable rectangles.
- Single confidence-free lineage for ambiguous split/merge history.
- UI text explaining obvious controls instead of making controls discoverable.
- Blocking the whole portal when one provider fails.
- Running long indexers on every page load.
- Mutating live services or project files merely to inspect them.

## Architecture

### Server

`riglib/evolve/` should hold the portal implementation:

- `service.py`: lifecycle seam, mirroring `config_web_service.py`.
- `web.py`: HTTP app, static asset serving, JSON APIs, CSRF/Host guards.
- `projects.py`: project discovery.
- `git_index.py`: commit and snapshot extraction.
- `structure.py`: file and symbol hierarchy extraction.
- `providers/`: provider adapters for task, tg, review, Haft, Sverklo, Serena, rig, and LSP.
- `model.py`: normalized project, node, event, relationship, and health dataclasses.
- `cache.py`: server-side cache and invalidation.

The default backend stays stdlib-friendly at import time. Heavy dependencies are lazy:

- YAML only inside config paths.
- Tree-sitter only inside parsing paths.
- Any React build tooling only in the web asset build step, not in `rig --help`.

### Frontend

The frontend should be a React app shipped as static assets and served by rig:

- Histogram component.
- Treemap canvas component.
- Selection state machine.
- Relationship overlay.
- Monaco detail panel.
- Provider health panel.
- Project picker.
- Mobile containment picker.

The app must degrade to a minimal no-build HTML/JS fallback if static assets are missing. The
fallback is intentionally scoped: it shows service health, project selection, provider health, and
clear build/install guidance; it is not a second full treemap implementation. A broken frontend
build must not break `rig --help`.

## Implementation Slices

### Slice 1: Working Local Portal

Acceptance:

- `rig evolve run --port <p> -C <repo>` serves a React/static page.
- `start/stop/status/enable/disable` use `agenttools-service`.
- Project list includes the current repo and sverklo-registered repos.
- API returns git histogram data and a file-level treemap for the current HEAD.
- Selection highlights treemap nodes and histogram buckets touching selected files.
- Browser validation collects rendered treemap rectangle bounds and asserts squarified geometry by
  aspect metrics; screenshots alone are not sufficient proof.
- Tested against `/Users/ultra/work/hyperide`.
- Screenshot captured and sent to review.

### Slice 2: Symbol Structure

Acceptance:

- Supported languages expose classes/functions/methods/nested functions from AST/LSP.
- JSDoc/docblocks populate the detail panel.
- Monaco opens selected symbol ranges.
- Relation overlay supports at least same-file call/reference approximations and exact provider
  results when available.

### Slice 3: Historical Snapshots

Acceptance:

- Clicking a histogram bucket shows the snapshot at the end of that period.
- Existing selection persists across snapshots when trackable.
- Missing selections become grey dashed ghosts.
- File rename and intra-file symbol move tracking are covered by tests.

### Slice 4: Ecosystem Context

Acceptance:

- Task, tg, review, Haft, Sverklo, Serena, and rig providers populate health and normalized
  events.
- Provider payloads are inspectable.
- Current in-progress work and stale/blocked work appear in the details/timeline.

### Slice 5: Advanced Lineage

Acceptance:

- Cross-file moves, symbol renames, splits, and merges are represented with confidence.
- Ambiguous lineage shows candidate alternatives.
- Users can add local annotations for lineage overrides.

## Test Strategy

Unit tests:

- Project discovery from rig/task/sverklo fixtures, including warning/error health for stale,
  locked, unauthenticated, missing, and permission-denied sources.
- Git histogram bucketing by day/week/month.
- Treemap hierarchy and size metrics.
- Provider health normalization.
- Provider failure-type display/retry for missing binary, missing auth, parse failure, unsupported
  language, shallow git history, permission denied, stale index, and database lock.
- Sverklo transient `database is locked` handling with bounded retry/backoff and visible degraded
  health when retries are exhausted.
- Historical snapshot selection persistence.
- Ghost selection rendering model.
- Symbol lineage confidence scoring.
- Provider strings are escaped/sanitized before UI rendering, including health messages, docblocks,
  task/tg text, commit messages, and raw JSON drilldowns.
- Server/browser cache retention, user-only file permissions where supported, and clear-cache
  behavior for sensitive provider payloads.

Integration tests:

- `rig evolve` parser and lifecycle seam without importing `agenttools-service` at parser-build time.
- HTTP API with temp git repos.
- Hyperide smoke path gated as opt-in if runtime is heavy.
- `rig evolve run|start|stop|status` delegate to a live or fake `agenttools-service` manager and
  report clear state transitions and missing-runtime errors.
- Missing `agenttools-service` / `agenttools-daemon` runtime exits non-zero with fail-closed
  remediation guidance.
- Static-assets-missing fallback renders service health, project/provider health when possible, and
  build/install guidance instead of a blank page.

Visual tests:

- Browser screenshot of `/Users/ultra/work/hyperide`.
- Desktop and mobile viewports.
- Nonblank treemap canvas.
- Browser-side rectangle probes record treemap bounds and aspect ratios, proving a mixed squarified
  mosaic and rejecting stripe-only layouts.
- Treemap geometry probes run in the local pre-commit/CI visual gate once browser automation is
  available, so stripe-only regressions fail before final manual review.
- No overlapping controls.
- Selection and histogram highlight visible.
- Containment picker visible on Shift-hold/tap.
- Full Figma-like keyboard navigation: `Enter`, `Shift+Enter`, arrows, `Tab`, and `Escape`.
- Mobile/tablet containment picker tap behavior, thumb-usable layout, and touch selection.
- Relationship-quality labels remain text-visible; color/icon accents are redundant and never the
  only quality signal.
- Relationship quality filters keep exact, inferred, approximate, unavailable, and degraded edges
  separately selectable.
- Tailscale exposure policy requires explicit operator action, defaults to tailnet-only, and blocks
  Funnel/public exposure without stronger confirmation.

Review gate:

- Run `review diff -C <repo>` before commit.
- Send screenshots to Opus for visual review through the established review workflow.

## Implementation Status, 2026-06-29

Completed first runnable slice:

- `rig evolve run|start|stop|status|enable|disable` lifecycle is wired through the shared
  `agenttools-service` seam.
- The local web API exposes project discovery, git activity histogram buckets, and a proportional
  file treemap snapshot.
- The browser UI supports a project picker, histogram selection, treemap selection, and a detail
  panel for the selected node.
- Tested against `/Users/ultra/work/hyperide` and exposed through Tailscale Serve at
  `/evolve`.

Completed project-tool provisioning slice:

- `project_tools` is a repo-owned `rig.yaml` block for Haft, Serena, and Sverklo, distinct from
  the global personal-CLI `tools` block.
- `rig init` scaffolds the block; `rig apply` plans `provision_project_tool` actions; `rig status`
  reports drift under the repo section.
- Haft writes `.haft/` carriers and merges the Haft MCP section into `.codex/config.toml`.
- Serena writes `.serena/project.yml` and `.serena/.gitignore`.
- Sverklo registration is idempotent and dry-run gated; reindex stays opt-in.
- `rig-cli` now dogfoods this block in its committed `rig.yaml`.
- Focused tests plus the full suite passed: `1378 passed, 16 skipped`.

Performance correction after live feedback:

- The treemap must use a squarified Windirstat-style mosaic; stripe-only slice-and-dice layouts are
  a failed render.
- Snapshot loading must be measured during development, not judged by feel.
- The snapshot API should cache repeat loads, parallelize independent providers, and filter
  generated/binary artifacts from the code surface unless explicitly enabled.
- Performance work should use browser-trace tooling when available: Chrome DevTools MCP for live
  traces and layout/network analysis, Cloudflare's `web-perf` skill as the audit workflow, and
  Lighthouse/PageSpeed MCP where Core Web Vitals or Lighthouse scoring is the right evidence.
- These performance tools are **candidate catalog items**, not rig-local special cases. `rig.yaml`
  can enable them only after `agent-tools` catalogs the corresponding MCP/skill carriers; until
  then they are research notes or ad hoc developer setup.

## Audit Snapshot, 2026-06-30

Current state is **not** the full original request. It is a runnable Slice 1 skeleton plus
repo-local project-tool provisioning and the visual-review plumbing needed to keep UI proof
flowing.
Items below labeled still partial or missing are known implementation gaps, not accepted
completion criteria; they remain tracked by the delivery milestones.

Verified on 2026-06-30:

- `uv run --with pytest python -m pytest -q tests/test_evolve.py tests/test_evolve_projects.py
  tests/test_project_tools.py` passed: `24 passed`.
- The local `.venv` had been missing the shared `agenttools-service` runtime; installing editable
  `agenttools_daemon` and `agenttools_service` from `/Users/ultra/xp/agent-tools/lib/` restored the
  lifecycle seam.
- `uv run bin/rig evolve status -C /Users/ultra/work/hyperide --port 8799` now reaches the shared
  service manager and reports the Hyperide evolve service as `stopped`.

Implemented now:

- `rig evolve run|start|stop|status|enable|disable|_serve` is registered and delegated through the
  shared service manager.
- The HTTP API exposes projects, snapshots, and touched-bucket lookup.
- Project discovery includes the current repo plus `sverklo list`, with alias/stale-path handling
  and provider-health errors.
- Git histogram aggregation covers commits, changed files, additions/deletions, and
  day/week/month bucket calculation.
- The current project surface is a file-level proportional treemap with generated/binary/lockfile
  filtering and a squarified SVG UI.
- Basic UI supports project selection, histogram click, treemap click, selected-node detail, and
  selected-file histogram highlighting.
- First-pass snapshot performance uses parallel histogram/tree/git-health work and an in-memory
  cache keyed by project, bucket, and HEAD.

Slice 1 proof/performance update, 2026-06-30:

- The UI now exposes a provider-health panel backed by existing project-discovery health,
  snapshot git health, and snapshot cache state.
- The histogram has explicit day/week/month controls, and file-touch highlighting requests the
  same selected bucket granularity from `/api/touches`.
- The treemap SVG exposes browser geometry hooks: `data-testid="treemap-canvas"`,
  `data-probe="treemap-tile"`, per-rectangle role/depth/path/aspect/orientation attributes, and
  an on-demand `window.rigEvolveTreemapProbe()` helper. The helper reads DOM geometry only when a
  browser/proof run calls it; page load still uses the existing parallel snapshot and cache path.
- Hyperide proof on `http://127.0.0.1:8894/` produced a headless Chrome DOM dump with 1445 treemap
  rectangles, both `row` and `column` orientations, 3 health rows, and all three bucket controls.
  Screenshot artifact: `/tmp/rig-evolve-slice1-proof.png`.
- Focused test proof: `uv run --with pytest python -m pytest -q tests/test_evolve.py` passed with
  `14 passed`.

UI-owner accessibility/performance audit update, 2026-06-30:

- Applied the current web-design-guidelines priority set plus the frontend skill pack:
  behavioral tests paired with screenshot review, semantic dark-theme token direction for touched
  CSS, and a wiring-only architecture note for the future React/static app split.
- Added low-risk UI hardening: skip link, labeled project select, labeled reload/bucket buttons,
  semantic `main`, polite provider-health updates, visible `:focus-visible` states, keyboard
  activation for histogram bars and treemap rectangles, stable/truncated header content, reduced
  motion handling, explicit touch-action choices, and RAF-batched resize rendering.
- Hyperide proof on `http://127.0.0.1:8895/` produced a DOM dump with 1445 treemap rectangles,
  both `row` and `column` orientations, 3 health rows, all three bucket controls, skip/main
  targets, focus-visible CSS, reduced-motion CSS, and touch-action CSS. Screenshot artifact:
  `/tmp/rig-evolve-ui-owner-proof.png`.
- Focused test proof: `uv run --with pytest python -m pytest -q tests/test_evolve.py` passed with
  `14 passed`. Screenshot review proof: `review visual /tmp/rig-evolve-ui-owner-proof.png -C
  /Users/ultra/xp/rig-cli` returned `KEEP`.

Symbol tree integration update, 2026-06-30:

- File-tree snapshots can now opt into symbol children with `build_file_tree(...,
  include_symbols=True)` while the default remains file-level for current UI performance.
- Supported Python and JS/TS files attach classes, functions, methods, nested functions, source
  ranges, byte/line sizes, stable symbol IDs, scope, parser/language metadata, and doc/docblock
  summaries under their file nodes.
- File and group size rollups still use source-file bytes, so existing treemap area semantics and
  generated/binary filtering stay intact.
- Focused test proof: `uv run --with pytest python -m pytest -q tests/test_evolve.py
  tests/test_evolve_symbols.py` passed with `18 passed`.

Historical snapshot backend update, 2026-06-30:

- Added backend resolution for the end commit/time of a day, week, or month histogram bucket.
- Added file-level historical snapshots built from git tree objects at the resolved commit without
  checking out or mutating the user's working tree.
- Snapshot payloads include the requested bucket, resolved commit metadata, and selected-path
  metadata for future ghosting: exists in snapshot, missing in snapshot, and exists at current
  `HEAD`.
- Focused test proof: `uv run --with pytest python -m pytest -q tests/test_evolve_history.py
  tests/test_evolve.py` passed with `19 passed`. The history fixture covers a change, a rename,
  and a delete across buckets.

Provider API/inspection update, 2026-06-30:

- Added the normalized provider API seam: `/api/providers?project=<path>` returns
  `rig.evolve.provider.v1` payloads plus per-provider cache metadata.
- Added durable server-side provider cache under the user cache directory, keyed by project path,
  git HEAD/version, provider name, and schema version.
- Wired initial fail-soft providers for `git`, `rig`, and `sverklo` into the API and compact
  provider inspection panel. The panel shows provider name, status, cache state/age, error count,
  and `raw_ref`/message without expanding raw payloads into the main view.
- Focused test proof: `uv run --with pytest python -m pytest -q tests/test_evolve.py
  tests/test_evolve_providers.py` passed with `23 passed`.

Still partial:

- The UI is inline HTML/SVG, not a React portal/static asset bundle yet.
- Browser geometry proof is not yet automated in CI, and mobile proof is still missing.
- The project list does not yet merge `rig.yaml` scans, task-cli projects, active sessions, or
  review/tg workspaces.
- Histogram UI still lacks PR/review/task/tg density.
- Treemap UI is still file-level by default. Symbol/docblock children exist as an opt-in backend
  model, but `/api/snapshot` and the browser UI do not yet expose symbol rectangles.
- Selection is shallow; no Figma-like keyboard traversal, containment picker, Monaco, relationship
  overlay, or disappeared-node ghosting.
- Snapshot cache is still process-memory only; provider payloads have durable server cache, but
  browser IndexedDB rotation is not implemented.

Missing from the full SPEC:

- Real task, tg, review, Haft, Serena, LSP, and tree-sitter provider adapters beyond the initial
  `git`/`rig`/`sverklo` set.
- API/UI wiring for opt-in symbol rectangles and JSDoc/docblock display; the backend extractor
  exists for Python and JS/TS, but it is not yet part of the live viewer.
- Call/reference relationships and relationship quality labels.
- Historical snapshot switching on histogram bucket click, range brushing, compare mode, and
  selection persistence with grey dashed ghosts.
- Rename/move/split/merge lineage with confidence scores and local annotations.
- Visual/browser regression tests with desktop/mobile screenshots, rectangle aspect probes, and
  overlap checks.

Delivery milestones from this audit:

- 2026-06-30: finish Slice 1 as a product proof: run Hyperide locally again, restore Tailscale
  exposure, add explicit provider-health UI, day/week/month proof, and screenshot/review evidence.
- 2026-07-01: land the normalized backend foundation: `model.py`, `cache.py`, provider contract,
  git/sverklo/rig providers, raw-provider inspection, and durable server cache.
- 2026-07-02: land symbol structure: tree-sitter/LSP-backed symbol rectangles, JSDoc/docblocks,
  Monaco ranges, keyboard traversal, containment picker, and initial relationship overlay.
- 2026-07-03: land historical snapshots and ecosystem context: bucket-end snapshots, persisted
  selection, missing-node ghosts, task/tg/review/Haft/Serena provider health, and browser cache.
- 2026-07-04 to 2026-07-05: land advanced lineage: cross-file moves, symbol renames, split/merge
  candidates, confidence display, annotations, mobile pinch/pan polish, and final visual review.

Current performance tooling research, 2026-06-29:

- Chrome DevTools MCP (`chrome-devtools-mcp`) is the strongest candidate for local live-browser
  evidence: Chrome's public preview exposes DevTools-backed page automation, console/network
  inspection, layout debugging, screenshots, and performance traces. Catalog shape: likely
  `agent-tools/mcp/chrome-devtools/`, with docs noting its browser-data exposure and opt-out flags
  for usage statistics / CrUX lookup where appropriate.
- Cloudflare `web-perf` is currently an Agent Skill, not an MCP server. It is useful as the audit
  workflow around Core Web Vitals, render-blocking resources, layout shift causes, and network
  chains. Catalog shape: skill carrier first; do not declare it under `mcp.items` unless
  agent-tools also ships an MCP carrier.
- Lighthouse/PageSpeed MCP candidates are community servers around Google Lighthouse and Google
  PageSpeed Insights. They are useful when the evidence target is Lighthouse scoring, mobile vs.
  desktop comparison, or Core Web Vitals / PageSpeed API output. Catalog shape: pick one concrete
  maintained implementation, document API-key / network requirements, then add an agent-tools MCP
  item before any `rig.yaml` enablement.

## Current Tool Findings

Haft:

- `haft init --local --codex` initialized this repo as project `qnt_723ae7ec`.
- `haft doctor` now reports 11 passed, 0 failed, with expected warnings for missing optional API
  keys.
- `haft spec check --json` reports scaffolded spec carriers as placeholders with no active
  sections; this portal should surface that kind of governance health rather than hiding it.

Sverklo:

- `sverklo register /Users/ultra/xp/rig-cli` and `sverklo register /Users/ultra/work/hyperide`
  succeeded.
- `sverklo reindex /Users/ultra/xp/rig-cli --timing` produced 216 files and 4354 chunks.
- A parallel `sverklo reindex /Users/ultra/work/hyperide` conflicted with another sverklo read and
  emitted many `database is locked` errors, then reported 0 files/chunks for that run.
- A sequential retry succeeded for hyperide with 1981 files and 13691 chunks.
- This proves provider lock/retry/backoff and source-health reporting are required product
  behavior, not polish. The Sverklo adapter should retry transient lock failures with bounded
  exponential backoff, then return `warning` or `error` health with the raw failure reference when
  retries are exhausted.

Serena:

- Serena is required as a first-class integration source for LSP-backed symbol overview,
  definitions, references, diagnostics, and rename-aware operations.
- Serena MCP tools are not exposed in this Codex turn through `tool_search`; implementation must
  treat Serena availability as runtime-discovered and fail-soft.

## Open Decisions

- Whether the React assets live under `riglib/evolve/static/` as committed build output or are
  built from a separate source directory during release.
- The first runnable slice uses SVG for the treemap because it gives inspectable DOM rectangles
  and browser geometry probes. The later React/static app may keep SVG, move to Canvas 2D for very
  large projects, or use a hybrid SVG-overlay/Canvas-body renderer after performance evidence.
- Which tree-sitter grammars are bundled, optional, or delegated to Sverklo/Serena.
- How much provider data is stored server-side versus browser IndexedDB for privacy and speed.
  Security/privacy impact is part of this decision: raw sensitive payloads should default to
  server-side user-only cache or no browser retention until a provider marks browser caching safe.
- Exact Tailscale exposure mechanism: print local Tailscale IP URL, use `tailscale serve`, or use
  an existing internal share command.
- Tailscale exposure policy: exposure must require explicit operator action, default to
  tailnet-only access, document who can reach the URL, and provide a clear revoke/stop command.
  Funnel/public exposure requires a stronger confirmation path than a local-only review URL.

## Research Notes

- Edward Tufte emphasizes dense, useful data presentation with restrained non-data ink, small
  multiples, and sparklines; the portal should keep controls quiet and let the data carry the
  interface.
- Shneiderman and Johnson's treemap work supports a space-filling hierarchy for showing relative
  size at overview scale.
- Viégas, Wattenberg, and Dave's History Flow work is relevant for showing persistence, deletion,
  and collaboration dynamics over time.
- CodeCity is relevant as a warning and an inspiration: locality helps comprehension, but a
  metaphor must not replace inspectable metrics.

Sources:

- https://www.edwardtufte.com/
- https://www.cs.umd.edu/~ben/papers/Shneiderman1992Tree.pdf
- https://hint.fm/projects/historyflow/
- https://research.ibm.com/publications/studying-cooperation-and-conflict-between-authors-with-history-flow-visualizations
- https://wettel.github.io/codecity.html
- https://developer.chrome.com/blog/chrome-devtools-mcp
- https://github.com/ChromeDevTools/chrome-devtools-mcp
- https://github.com/cloudflare/skills
- https://developer.chrome.com/docs/lighthouse/overview
- https://developers.google.com/speed/docs/insights/v5/get-started
- https://github.com/ncosentino/google-psi-mcp
- https://github.com/danielsogl/lighthouse-mcp-server
