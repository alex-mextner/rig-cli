# rig provisions sub-agents (`subagents` category)

Status: spec (not yet implemented). Tracks ledger task #43.
Owner: rig-cli (`riglib/`). Source catalog: agent-tools.
Full spec on disk: `/Users/ultra/xp/agent-tools/docs/specs/rig-agents-provisioning.md`

## Goal

Let rig provision reusable **sub-agent definitions** (Claude Code `.claude/agents/*.md`)
the same way it already provisions skills and agent-hooks: a curated catalog in
agent-tools, selected by a committed/global `rig.yaml` block, applied idempotently into
the harness's agent-discovery directory, and drift-checked two ways.

Two scopes, one mechanism (rig already has both, by config *location*, not a `scope` key):

- **GLOBAL** — a machine-wide sub-agent library installed into `~/.claude/agents`,
  declared in `~/.config/rig/config.yaml`. Carried across every repo.
- **REPO-LOCAL** — project-specific task specialists committed with the repo, installed
  into `<repo>/.claude/agents`, declared in the repo's `./rig.yaml`.

v1 ships **Claude Code only**. Cross-harness mapping (opencode/gemini/copilot/codex) is
designed below but deferred — see "Cross-harness handling".

### Why `subagents`, not `agents`

The name `agents` is already taken in rig. `config_schema.py:509` registers an `agents_md`
block and `runner.py` has `_do_provision_agents_symlink` / `resolve_agents_md` — these own
the **AGENTS.md (canonical) + CLAUDE.md (symlink)** instruction-file invariant, an entirely
different concept from `.claude/agents/*.md` sub-agents. To avoid a confusing overload, the
new category, config block, scanner, handler, and registry all use the word **`subagents`**.

## What a Claude Code sub-agent is (authoritative — current CC docs, verified 2026-06-21)

A single Markdown file with YAML frontmatter; the Markdown body is the sub-agent's system
prompt.

```markdown
---
name: code-reviewer            # required. lowercase+hyphens. invocation identity (NOT the filename)
description: Reviews diffs...   # required. the orchestrator reads this to decide WHEN to delegate
tools: Read, Glob, Grep         # optional. inherits all tools if omitted
model: sonnet|opus|haiku|inherit|<full-id>   # optional. default: inherit
---
<markdown system prompt = the sub-agent's operational contract>
```

Optional frontmatter CC also accepts (rig copies the file verbatim, so these pass through
untouched): `disallowedTools`, `permissionMode`, `maxTurns`, `skills`, `mcpServers`,
`hooks`, `memory`, `background`, `effort`, `isolation`, `color`, `initialPrompt`.

**Locations + runtime precedence (highest→lowest), enforced by CC, NOT by rig:**
managed-settings `.claude/agents/` → `--agents` CLI JSON → **project `.claude/agents/`** →
**user `~/.claude/agents/`** → plugin `agents/`. Same-`name` conflict: higher location
wins. Both project and user dirs are scanned **recursively** (subfolders allowed but do not
affect identity; identity = the `name` field only). Duplicate `name` in one scope → one
silently discarded.

**Invocation:** automatic (CC matches `description`), explicit (`@agent-<name>`), or
whole-session (`claude --agent <name>`). The Agent/Task tool's `subagent_type` is the
`name`; the SubagentStart hook receives it as `agent_type`. CC **does** ship built-in
subagents (`Explore`, `Plan`, `general-purpose`, plus `claude-code-guide` and
`statusline-setup`); custom `.claude/agents/*.md` definitions **add** to that built-in set,
they are not the only agents.

**Implication for rig:** unlike skills, the install dir IS the discovery dir. There is no
"install into `~/.agents/...` then symlink into the harness dir" split for CC. One
`copy_subagent` action suffices; no `link_*_harness` second action.

## Source catalog layout (agent-tools)

Flat single-file per sub-agent, mirroring the on-disk shape CC itself ships:

```
agent-tools/subagents/<name>.md   # frontmatter + body. <name> == frontmatter `name` (validated at scan)
```

Rationale for flat (not dir-per-agent like agent-hooks): a CC sub-agent *is* one `.md` with
no companion assets. A directory buys nothing for v1 — switch to `subagents/<name>/<name>.md`
only when companion assets or per-harness source variants become real. No `by-type/`
grouping in v1 — flat universal set (`group=""`).

The `_looks_like_agent_tools` guard (`catalog.py:96`, keys on `skills/` AND `agent-hooks/`)
**stays unchanged** — adding `subagents/` does not gate source recognition (backward compat).

## Scanner (`catalog.py`)

Add `_scan_subagents()` mirroring `_scan_skills` but flat, carrier = the file. Reuse
`_read_skill_description` (`catalog.py:100`) — CC frontmatter uses the same `description:`
line, no yaml import (AGENTS.md hard rule). Register the call in `Catalog.scan()` after
`_scan_agent_hooks()` (`catalog.py:149`). Extend the category-enum comment at `catalog.py:48`
to include `subagents`. Item: `name=md.stem, category="subagents", group="",
description=_read_skill_description(md), path=md, default_enabled=True`.

**Frontmatter validation (fail-closed at scan):** require `name` and `description`. If
`name` present and `!= md.stem`, error with the file path (identity ambiguity). If
`description` missing, error (orchestrator can't delegate). Loud catalog error, not a
silent skip.

## Apply — Claude Code (the v1 deliverable)

### Target resolution (the GLOBAL-vs-LOCAL switch — no new code path)
- `_BUILTIN_TARGETS` (`plan.py:48`): add `"subagents": "~/.claude/agents"`.
- `_DEFAULTS_KEY` (`plan.py:54`): add `"subagents": "subagents_target"`.
- `_resolve_target` already does `cat.target → defaults.subagents_target →
  _BUILTIN_TARGETS["subagents"]`, then `_expand`.
- `_expand` (`plan.py:99`) is the entire global-vs-local mechanism: `~`/`$VAR`/absolute →
  absolute machine path (**GLOBAL**, default `~/.claude/agents`); a *relative* target (e.g.
  `.claude/agents`) → anchored at `repo_root` (**REPO-LOCAL**, travels with the repo). No
  separate code for the local path.

### One action per enabled item
In `plan.build()`, after the agent_hooks block (~`plan.py:417`), iterate
`catalog.by_category("subagents")`, gate by `_item_enabled(sa, item, type_enabled=False)`,
and append `Action(kind="copy_subagent", category="subagents", item=item.name,
source=item.path, target=subagents_target / item.path.name)`. `_item_enabled` (`plan.py:208`)
gives precedence for free (`items.<name>.enabled → enable/disable → all → default_enabled`).
Add `copy_subagent` to the `Action.kind` docstring (`plan.py:78`).

### Handler (`actions/runner.py`)
A single-file write (not a tree copy), reusing the `fsutil.write_file` primitive
`_do_install_agent_hook` already uses (`runner.py:303`): mkdir the target parent, then
`fsutil.write_file(action.target, action.source.read_text(), on_conflict)`. Register in
`_HANDLERS` (`runner.py:3985`): `"copy_subagent": _do_copy_subagent`. Conflict policy reuses
the shared `on_conflict` (skip/backup/overwrite).

### Idempotence
`rig apply` twice produces identical results: `write_file` re-writes only on byte difference;
no hashing state file.

## Precedence (resolved end-to-end)

**rig config (which sub-agents get written, and where):** per-item
`subagents.items.<name>.enabled` (repo `./rig.yaml` beats global config, more specific
layer) → `enable`/`disable` lists → `all:` (opt-out default true) → catalog
`default_enabled`.

**CC runtime (which `name` wins on a disk collision):** rig does NOT enforce — it writes
files; CC's own precedence applies (project `.claude/agents` > user `~/.claude/agents` >
plugin). Installing the same `name` both globally and repo-locally lets the repo-local copy
win at runtime — the intended shadowing composition.

## Drift (`drift.py`)

- `action.kind` switch (`drift.py:107`): add the `copy_subagent` arm + record declared
  `(dir, filename)`.
- `_check_copy_subagent`: **missing** if target absent; **modified** on byte diff (file
  compare, not `dirs_identical`).
- `_extras_subagents` mirroring `_extras_skills` (`drift.py:184`): flag undeclared `*.md` as
  "extra". **Caution — the target may hold hand-authored / `/agents`-created sub-agents**
  (esp. GLOBAL `~/.claude/agents`). Gate the extras scan behind category-enabled at
  `cli.py:727` (same gate skills use). When enabled, undeclared `.md` is a soft "extra" note;
  rig never deletes it (no `clean` auto-delete in v1).

## Config schema (`config_schema.py`) + CLI surface

Add `_SUBAGENTS_BLOCK` mirroring `_AGENT_HOOKS_BLOCK` (`config_schema.py:196`) — leaves
`enabled`(bool,true) / `target`(str,`~/.claude/agents`) / `all`(bool,true) / `enable`(array) /
`disable`(array), `open_map="items"` keyed by agent name. Wiring (each changes together, sync
test enforces):
- `BLOCKS` (`config_schema.py:499`): add `"subagents": _SUBAGENTS_BLOCK`.
- `_DEFAULTS_BLOCK` (`config_schema.py:146`): add `subagents_target` leaf.
- `config.py:31` `_VALID_TOP_KEYS`: add `"subagents"`.
- `config.py:51` `_VALID_CATEGORIES`: add `"subagents"`.
- `config.py` `validate()`: add `_validate_subagents(...)` mirroring `_validate_agent_hooks`
  (reject-unknown-keys; bool `enabled`/`all`; str `target`; `items` map w/ per-item bool
  `enabled`; str-array `enable`/`disable`).
- `plan.py:332` `_validate_item_names` flat-items loop: extend `for cat_name in
  ("agent_hooks", "mcp")` → `(..., "subagents")` so a typo fails closed with did-you-mean.
- `layers.py:29` `_CATEGORY_LAYER`: add `"subagents": GLOBAL`. Known wart (shared with
  skills): a relative repo-local target still classifies GLOBAL in `rig status`. Acceptable v1.
- Regenerate `schema/rig.schema.json` (`rig schema --write`).

### What a user writes
GLOBAL (`~/.config/rig/config.yaml`), opt-out all: `subagents: {enabled: true, all: true}`
→ writes every `agent-tools/subagents/*.md` into `~/.claude/agents`.
REPO-LOCAL (`./rig.yaml`): `subagents: {target: .claude/agents, all: false, items:
{code-reviewer: {enabled: true}}}` — relative target → repo-local, travels with the repo.

## Edge cases
- Malformed catalog file (missing `name`/`description`, or `name != stem`): scan-time error
  naming the file. Loud, not a silent skip.
- Shared global dir pollution: extras scan gated on category-enabled; rig never deletes.
- Repo-local conflict with a real existing `.claude/agents/<name>.md`: governed by
  `on_conflict` (skip/backup/overwrite); default does not clobber without backup.
- `name` vs filename divergence: forbidden in catalog (validated) — no upside, only a
  config/runtime mismatch.
- Duplicate `name` across global + repo-local: allowed; CC runtime precedence resolves it
  (the shadowing feature).
- No versioning / no auto-update: `rig apply` is the update trigger.

## Out of scope (v1)
Cross-harness provisioning (designed, deferred), `by-type/` agents, agent-teams, `clean`
auto-delete, secrets injection.