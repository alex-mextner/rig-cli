# Spec: stack presets — stack-organized skill/tool curation

Status: foundation landing (this PR). Follow-up milestones flagged in §9.
Origin: Alex tg#8708.

## 1. Problem

rig provisions the **same** skill set to every repo: the universal baseline
(`skills/universal/*`, opt-out) plus the detected project *type* bundle
(`skills/by-type/<kind>/*`, e.g. `backend`, `frontend`). There is no notion of a repo's
**technology stack** (`mobile/swift/swiftui`, `frontend/ts/react`, `backend/python`), so:

1. **No curation by stack.** A Swift mobile repo and a React web repo receive the identical
   skill set. As agent-tools grows language/framework-specific skills (SwiftUI MVVM, TCA,
   swift-concurrency, Vercel-React patterns), provisioning *all* of them to *every* harness
   is the "skill firehose" — noise that drowns the relevant guidance. Stacks are the
   curation axis: a repo gets the universal set **+ its stack's set**, nothing else.
2. **Stack is not declared anywhere.** Neither the global rig config nor a per-repo
   `rig.yaml` records what a repo *is*. rig can't provision stack-appropriate content
   because it never learns the stack.

Note on the word "stack": `riglib/detect.py` already has an `Environment.stack` field
meaning the **build toolchain** (`bun-node | python-uv | go | unknown`). That is a
different, pre-existing concept. This spec's "stack" is the **stack preset** — the
`l1/lang[/framework]` taxonomy below. To avoid churning working code, the toolchain field
keeps its name; the new concept is addressed as **stack preset** in code
(`detect_stack_preset`, `stack_preset` helpers) and as the top-level **`stack`** config key
(the name Alex specified) on disk.

## 2. Taxonomy

A stack preset is a slash-separated path `l1/lang[/framework]`:

- **Level 1 (`l1`)** — a CLOSED enum of six domains:
  `mobile | frontend | backend | desktop | embedded | system`.
- **Level 2 (`lang`)** — the language. OPEN vocabulary (`swift`, `ts`, `js`, `python`,
  `go`, `rust`, `kotlin`, `cpp`, …). Required (a preset is at least `l1/lang`).
- **Level 3 (`framework`)** — OPTIONAL. OPEN vocabulary (`swiftui`, `react`, `vue`,
  `django`, …). A preset may stop at `l1/lang` (`backend/python` is valid with no
  framework).

Examples: `mobile/swift/swiftui`, `frontend/ts/react`, `backend/python`, `backend/go`,
`backend/rust`, `system/rust`.

**Open by design.** A stack whose lang/framework has NO skills or tools mapped yet is
still valid — it just provisions nothing extra. The taxonomy is a growing tree; declaring
a leaf that has no content is expected and fine. Only `l1` is validated (against the
six-enum) because it is the fixed spine; a bad `l1` is almost always a typo and fails
closed with a helpful message.

### 2.1 Validation rules (`config.validate`)

Given a `stack` value:
- Must be a non-empty string of 2 or 3 `/`-separated non-empty segments.
- `segments[0]` (l1) must be one of the six enum values → else `ConfigError` naming the
  allowed set.
- `segments[1]` (lang) must be non-empty. OPEN — any token accepted.
- `segments[2]` (framework), if present, must be non-empty. OPEN.
- More than 3 segments, or a trailing/leading/`//` empty segment → `ConfigError`.

A **missing** `stack` is NOT a hard validation error (see §5, migration). Only a *malformed*
present value fails validation.

## 3. agent-tools catalog layout

Stack skills live under a new sibling of `universal/` and `by-type/`:

```
skills/by-stack/<l1>/<lang>[/<framework>]/<name>/SKILL.md
```

Examples:
```
skills/by-stack/mobile/swift/swiftui/swiftui-mvvm/SKILL.md
skills/by-stack/mobile/swift/swiftui/tca-swiftui/SKILL.md
skills/by-stack/mobile/swift/swift-concurrency/SKILL.md      # lang-level (no framework)
skills/by-stack/frontend/ts/react/vercel-react-patterns/SKILL.md
skills/by-stack/frontend/ts/ts-strictness/SKILL.md           # lang-level
```

- The directory path **is** the stack path. A skill placed at
  `by-stack/<l1>/<lang>/<name>` is a **lang-level** skill (applies to every framework
  under that lang); `by-stack/<l1>/<lang>/<fw>/<name>` is a **framework-level** skill.
- This mirrors the existing `by-type/<kind>/<name>` convention exactly, so the catalog
  scanner, plan resolver, and drift code stay symmetrical (directory convention, not
  metadata tags — consistent with `by-type`, no SKILL.md frontmatter change needed).

The catalog scans each into an `Item`:
- `category = "skills"`, `group = "by-stack/<l1>/<lang>[/<framework>]"`,
  `name = "by-stack/<l1>/<lang>[/<framework>]/<skillname>"`,
- `default_enabled = False` (like by-type — pulled in only by a matching declared stack),
- `meta = {"stack": "<l1>/<lang>[/<framework>]", "skill": "<skillname>"}`.

## 4. Provisioning: which skills a stack maps to (hierarchical prefix match)

A declared stack **inherits** every by-stack skill whose stack path is a **prefix** of (or
equal to) the declared stack. For `stack: mobile/swift/swiftui` the selected groups are:

- `by-stack/mobile/*` (every domain-level skill), plus
- `by-stack/mobile/swift/*` (lang-level), plus
- `by-stack/mobile/swift/swiftui/*` (framework-level).

For `stack: backend/python` (no framework) only `by-stack/backend/*` and
`by-stack/backend/python/*` match. This is what makes "mobile repos don't get react
skills and vice versa" true: a React skill lives at `by-stack/frontend/ts/react/*`, which
is not a prefix of any `mobile/...` stack.

Selection is automatic from the `stack` value — no per-repo enable list needed. Opt-out is
available via `skills.by_stack.disable: ["<item-name>"]` and per-item override via
`skills.by_stack.items.<item-name>.enabled: false`, mirroring `by_type`.

The **two example stacks** shipped in agent-tools (companion PR, see §9):
- `mobile/swift/swiftui` → `swiftui-mvvm`, `tca-swiftui` (framework-level) +
  `swift-concurrency` (lang-level, at `by-stack/mobile/swift/`).
- `frontend/ts/react` → `vercel-react-patterns` (framework-level) + a `ts` lang-level
  skill.

## 5. Config schema (`stack` — global default + per-repo required)

`stack` is a **top-level scalar string** key (not a block), present in both
`~/.config/rig/config.yaml` (global) and per-repo `rig.yaml`, cascaded like every other
value (global is the fallback, repo overrides).

- **Global**: OPTIONAL, acts as the machine default (a sensible fallback for a new repo
  whose stack rig couldn't detect).
- **Per-repo**: MANDATORY *by policy*, enforced with a **soft-require** first (see below).

Schema wiring:
- `config_schema.py`: a `stack` entry in `_TOP_LEAVES` (`type: string`), so the emitted
  JSON schema + editor completion know it. Marked open (any string) — the six-enum spine
  is enforced by the Python validator, not the JSON-schema `enum`, because lang/framework
  are open.
- `schema.py` (wizard registry): a new `stack` `Area` with one `Option("stack", KIND_STR,
  …)` carrying the taxonomy hint. Category `stack` → writable layer REPO (the preset
  belongs in the committed repo file); the global default is set via `rig config set
  --global stack …`.

### 5.1 Migration — soft-require, not a hard break

Making `stack` a hard validation requirement would break every existing committed
`rig.yaml` on the next `rig apply`. Instead:

1. **Phase 1 (this PR).** A missing per-repo `stack` is a **warning**, not an error. `rig
   apply`, `rig status`, and `rig init` surface `stack: not set — declare it in rig.yaml
   (l1/lang[/framework]); rig detected <guess> (run rig init to confirm)`. A *malformed*
   stack still fails closed.
2. **Phase 2 (follow-up milestone).** After existing repos have been migrated, flip the
   soft-require to a hard `ConfigError` on a missing per-repo `stack`. Tracked as a
   milestone; NOT in this PR.

`rig init`/`rig setup` on a repo with no `stack` PROMPTS for it (auto-detect → confirm,
else pick), so new repos are born with a `stack` and never hit the warning.

## 6. Detection heuristics (`detect_stack_preset`)

A pure, observational `riglib` function `detect_stack_preset(repo_root) -> str | None`
returns a best-guess stack path or `None` (couldn't tell). Cheap top-level probes only
(no deep recursion):

| Signal | Guess |
|---|---|
| `Package.swift`, `*.xcodeproj`/`*.xcworkspace`, or top-level `*.swift` | `mobile/swift` (framework left off — SwiftUI vs UIKit needs source scan, deferred) |
| `package.json` with a `react`/`next` dep | `frontend/ts/react` if `tsconfig.json` present, else `frontend/js/react` |
| `package.json` with `vue`/`svelte`/`@angular/core` | `frontend/ts/<fw>` (ts if tsconfig) |
| `package.json` with a backend dep (`express`/`fastify`/`hono`/`koa`/`@nestjs/core`) and no frontend dep | `backend/ts` (ts if tsconfig, else `backend/js`) |
| `pyproject.toml` or `uv.lock` or `setup.py` | `backend/python` |
| `go.mod` | `backend/go` |
| `Cargo.toml` | `backend/rust` |
| none of the above | `None` |

Framework-level detection (SwiftUI, specific Python web frameworks) is deliberately
shallow in the foundation; deeper source scans are a follow-up milestone. The heuristic is
a *starting suggestion the user confirms*, never an authority.

## 7. init / apply UX

- **`rig init` / `rig setup` (config being scaffolded).** `SetupState.default(...)` accepts
  a `stack` argument; the CLI calls `detect_stack_preset(repo_root)` and:
  - **auto-detected** → the generated `rig.yaml` gets `stack: <guess>`; in an interactive
    run the user is asked to confirm/override, in `--yes` the guess is written as-is (or
    left unset with a warning if `None`).
  - **not detected** → interactive: prompt the user to pick `l1` (from the six-enum) then
    type lang and optional framework; non-interactive: leave `stack` unset and emit the
    soft-require warning.
- **`rig apply` (existing config).** Uses the repo's declared `stack` to select by-stack
  skills. If `stack` is absent, apply proceeds (universal + by-type only) and prints the
  soft-require warning + the detected guess.
- **`rig status`.** Reports the declared `stack` (or `not set` + guess), and lists the
  by-stack skills the stack pulls in, alongside the existing toolchain `stack:`/`type:`
  line (renamed in the status line to `stack-preset:` to disambiguate from the toolchain).

The wizard's stack prompt shares the same `plan.build` engine — no forked executor. The
stack only changes *which skill Items* the plan selects; the apply path is unchanged.

## 8. One-engine / hard-rules alignment

- **One engine.** by-stack selection is added to `plan._skills_enabled` (the shared
  headless engine both `rig init` and `rig apply` call). The wizard never re-implements
  selection.
- **Stdlib-only at import.** Detection + catalog scanning are stdlib-only.
- **Idempotent + backup-noted.** by-stack skills flow through the SAME `link_skill_*` /
  copy actions as universal/by-type — no new action type, same idempotency/backup.
- **Catalog seam.** Only `catalog.py` learns the `by-stack/` on-disk layout.
- **Open vocabulary.** An unknown lang/framework selects zero items and is not an error.

## 9. Scope: foundation (this PR) vs follow-up

**Foundation (this PR, rig-cli):**
- `stack` top-level schema key + validation (shape + l1-enum) + soft-require warning.
- `skills.by_stack` block (disable / items) in schema + validator.
- `stack` wizard Area in `schema.py`.
- `by-stack/` catalog scanner.
- Hierarchical prefix selection in `plan._skills_enabled`.
- `detect_stack_preset` with the §6 heuristics.
- `default_state` carries `stack`; `rig init`/`setup` write the detected/confirmed stack;
  soft-require warning at apply/status.
- Test fixtures (`fake_agent_tools` gains `by-stack/` skills) + unit tests throughout.

**Follow-up milestones (flagged, NOT in this PR):**
- **agent-tools companion PR** — author the two example stacks' real skill content under
  `skills/by-stack/` (separate repo, separate PR). Scaffolded on branch there; content is
  the substantive skill authoring.
- **Full catalog reorg** — migrate any future language/framework skills into `by-stack/`;
  decide whether some current `by-type` skills are really stack skills.
- **Hard-require phase 2** — flip the soft-require to a `ConfigError` after repos migrate.
- **Deeper framework detection** — SwiftUI-vs-UIKit source scan, Python web-framework
  detection (django/fastapi/flask), monorepo multi-stack.
- **Full interactive TUI stack picker** — the rich Textual picker; the foundation ships the
  headless + confirm path.
- **Stack-scoped tools** (not just skills) — MCP servers / CI gates selected by stack.

## 10. Open questions for Alex

1. **l1 strictness.** Spec validates `l1` against the six-enum (fail-closed on a typo) but
   leaves lang/framework fully open. OK, or should `l1` also be open?
2. **Multi-stack repos.** A monorepo may be both `frontend/ts/react` and `backend/python`.
   Foundation models a single `stack` string. Do we need `stacks: [..]` (a list) later?
3. **Global default semantics.** If global `stack` is set and a repo omits it, the repo
   *inherits* the global via the cascade — which partially defeats "per-repo mandatory".
   Should a repo be required to set its own even when a global default exists (warn on
   inherited), or is inheriting acceptable?
