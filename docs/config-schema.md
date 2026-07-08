# `rig.yaml` — declarative config schema

`rig.yaml` is the committed, reproducible source of truth for a repo's setup. `rig apply`
reads it, computes the diff vs disk, and converges. `rig init` writes it.

**Cascade (by location, no scope flag):**

1. **Global** — `~/.config/rig/config.yaml` (`$XDG_CONFIG_HOME/rig/config.yaml`).
2. **Per-repo** — `./rig.yaml` (overrides global; committed by default).

Dicts merge recursively (per-repo wins); lists and scalars replace wholesale. The result
is validated **fail-closed** before any write: unknown top-level keys, unknown categories,
bad enum values, and unknown item names abort.

**Round-trip invariant:** `init` → `rig.yaml` → `apply --config` produces the same plan.
`riglib/state.py` (`SetupState`) is the single serializer; `riglib/config.py` the loader.

## The JSON Schema (enforced + editor-facing)

This document is the human-readable reference; the machine-readable schema is
**`schema/rig.schema.json`** — a Draft-07 JSON Schema **generated from one in-code registry**
(`riglib/config_schema.py`) and committed. Both layers ride the same source:

- **Editors** read the committed file via the `# yaml-language-server: $schema=schema/rig.schema.json`
  modeline at the top of every `rig.yaml` (and the global config) → live key completion and
  unknown-key / bad-value squiggles, the *same* rules `rig apply` enforces.
- **`rig apply` / `rig config set` / `rig status` / `rig init`** validate against the registry
  (`riglib/config.py`, which mirrors the schema). A rejection is a **3-part error** — *what* is
  wrong, *why* (with the **schema path**, e.g. `harness.auto_mode`, and a pointer into
  `schema/rig.schema.json`), and *how to fix it* — and exits `2`.
- **`rig schema`** prints the schema; **`rig schema --check`** fails if the committed file drifted
  from the registry (a CI-usable gate); **`rig schema --write`** regenerates it. A test
  (`tests/test_config_schema.py`) keeps the file, the registry, and `config.validate`'s key set in
  lockstep, so the three never disagree.

**Strict by default — an unknown key is rejected, not ignored.** Fixed rig-owned blocks are closed
(`additionalProperties: false`): a typo'd key (`aut_mode`, `enabld`) fails loudly with the schema
path, rather than silently having no effect. The deliberate pass-through maps are top-level
`scripts:` / `dev:` (owned by dev helpers), catalog-keyed `items:` (under `skills.by_type`,
`agent_hooks`, `ci`, `mcp`), and `fragments:` (under `git_hooks.dispatcher`). A bad catalog item
*name* is caught later as a catalog/unknown-item error (exit `4`), not a schema typo.

## Top-level shape

```yaml
version: 1                      # schema version (int, required; only 1 supported)

defaults:                       # cross-category fallback targets/policy
  skills_target: ~/.agents/skills
  hooks_target: ~/.claude/hooks
  ci_target: .github/workflows
  mcp_target: ~/.claude/mcp
  on_conflict: skip | overwrite | backup   # what apply does when a target exists (default: backup)

agent_tools_source: ~/xp/agent-tools   # the agent-tools checkout to apply FROM (default: auto-detect)

scripts: { ... }             # project-local named commands consumed by dev helpers
dev: { ... }                 # dev/e2e lifecycle metadata consumed by dev helpers
skills: { ... }
agent_hooks: { ... }
git_hooks: { ... }
ci: { ... }
mcp: { ... }
harness: { ... }              # agent harness auto/permission provisioning (auto-mode)
permissions: { ... }          # per-harness permissions layer (allowlist + deny/ask baselines), default ON
models: { ... }               # daily model-freshness checker schedule (launchd/crontab cron)
agents_md: { ... }            # AGENTS.md (canonical) + CLAUDE.md (symlink), default ON
github: { ... }               # repo settings: ruleset/merge/ghas/actions via gh api + browser via agent-browser, default ON
tmux: { ... }                 # rig-managed tmux config (generate + migrate ~/.tmux.conf), opt-in
gitignore: { ... }            # rig-managed block in the GLOBAL git excludesfile (ignores **/.claude/worktrees/ in EVERY repo), default ON
tg_ctl: { ... }               # tg-ctl inbound daemon as a macOS boot LaunchAgent, default ON (macOS-only)
```

If `agent_tools_source` is omitted, rig resolves it from `$RIG_AGENT_TOOLS_SOURCE`, then
the default candidates (`~/xp/agent-tools`, `~/work/agent-tools`, `~/agent-tools`).

## `scripts`

Project-local named commands consumed by the `dev` CLI and portable hooks. `rig` validates that
this top-level key is a mapping, then preserves it; command semantics are owned by the dev helper
that executes the script.

```yaml
scripts:
  test: uv run --with pytest pytest tests/
  server: pnpm run dev
  e2e: pnpm exec playwright test
```

## `dev`

Project-local dev/e2e lifecycle metadata consumed by the `dev` CLI. `rig` validates that this
top-level key is a mapping, then preserves it; fields such as `server`, `e2e`, `jobs`,
`logs_root`, and `artifacts_root` are interpreted by the dev helper, not by `rig apply`.

```yaml
dev:
  server:
    script: server
    ports: [5173]
  e2e:
    script: e2e
```

## Resolution rules (so the file stays terse)

- A category sets `enabled: false` to disable the whole category in one line.
- A category may set `all: true` and list exceptions in `disable: [...]`, **or** `all:
  false` and list only what is enabled in `enable: [...]`. Opt-out and opt-in are both
  expressible.
- Per-item overrides live under `items:` keyed by item name; absent items inherit the
  `all`/`enable`/`disable` decision.
- Targets resolve item → category `target` → `defaults.<x>_target` → built-in default.

---

## `defaults`

Cross-category fallback targets + the on-conflict policy. A category that does not set its own
`target` falls back to the matching `defaults.<x>_target`; `on_conflict` governs what `apply` does
when a target file/dir already exists.

```yaml
defaults:
  skills_target: ~/.agents/skills
  hooks_target: ~/.claude/hooks
  ci_target: .github/workflows
  mcp_target: ~/.claude/mcp
  on_conflict: backup            # skip | overwrite | backup
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `skills_target` | path | `~/.agents/skills` | default skills install dir |
| `hooks_target` | path | `~/.claude/hooks` | default agent-hooks dir; with `harness.kind: codex`, the effective default is `~/.codex/hooks` unless a custom hook descriptor target is configured |
| `ci_target` | path | `.github/workflows` | default CI workflows dir |
| `mcp_target` | path | `~/.claude/mcp` | default MCP config dir |
| `on_conflict` | `skip`/`overwrite`/`backup` | `backup` | what `apply` does when a target already exists |

`on_conflict` protects **user-authored** content. Files that carry a **rig provenance header** —
the ship delegator (`.claude/scripts/pr-ship.sh`) and the machine env file
(`agent-tools/env`) — are rig's own prior output: apply rewrites them **in place, no backup,
regardless of `on_conflict`** (like the managed marker blocks). Anything at those paths
*without* the header is treated as user content and gets the full policy. Corollary: lines you
hand-append **below** a rig header are lost on the next apply — the header marks the whole file
as rig's; put machine customization in `AGENT_TOOLS_ROOT` (env var, always wins) instead.

---

## `skills`

```yaml
skills:
  enabled: true
  target: ~/.agents/skills          # or ~/.claude/skills | ./.agents/skills | custom
  harness_link: true                # symlink each skill into the harness discovery dir
  harness_skill_dir: ~/.claude/skills   # override the per-harness default discovery dir
  universal:
    all: true                       # all universal skills (opt-out model)
    disable: [push-regularly]       # ...except these
  by_type:
    enable: [backend, cli]          # install these by-type bundles wholesale
    items:
      by-type/bot/russian-pluralization: { enabled: false }   # fine-grained override
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | install skills at all |
| `target` | path | `~/.agents/skills` | where SKILL.md dirs are copied |
| `harness_link` | bool | `true` | also symlink each installed skill into the harness's skill-discovery dir |
| `harness_skill_dir` | path | per-harness default | where the harness discovers skills (claude-code: `~/.claude/skills`; codex: `~/.codex/skills`). An explicit value forces a directory link even for a native-discovery or instruction-file harness |
| `universal.all` | bool | `true` | enable all universal skills (opt-out) |
| `universal.disable` / `universal.enable` | list[str] | `[]` | deltas on `all` |
| `by_type.enable` | list[str] | the detected project type | which `by-type/<kind>` bundles to install whole |
| `by_type.items.<by-type/kind/name>.enabled` | bool | inherited | per-skill override |

If `by_type.enable` is empty and the detected project type is known, that type's bundle is
auto-pulled.

### Harness skill discovery (why `harness_link`)

The agent harness lists/loads Skill-tool skills from its **own** location, not from `target`,
and harnesses split into three families by *how* they discover skills:

- **skills-directory harnesses** enumerate a directory of skill folders. rig makes an
  installed skill discoverable by symlinking `<skill_dir>/<skill> → <target>/<skill>`:
  - **claude-code** → `~/.claude/skills`
  - **codex** → `~/.codex/skills` (Codex CLI's native skills dir; codex does **not** read
    `~/.agents/skills`, so rig must link here or codex sees no skills). codex is *also* an
    instruction-file harness (`~/.codex/AGENTS.md`) — the two are complementary.
- **native-discovery harnesses** auto-load the default `target` (`~/.agents/skills`) directly,
  so a copied skill is already visible and rig links **nothing**. `rig status` reports
  *discovers natively* instead of a pointless symlink:
  - **opencode** → auto-loads `~/.agents/skills` (and `~/.claude/skills`) natively since ≥1.16
- **instruction-file harnesses** have **no** per-skill directory; their guidance comes from a
  single global instruction file that the [`agents_md`](#agents_md) area maintains, not from a
  symlink. rig links **nothing** for these (it never invents a directory) and `rig status`
  reports the kind as *N/A — uses `<file>`* so the empty link area is explained, not silent:
  - **gemini** → `~/.gemini/GEMINI.md`
  - **pi** → `~/.config/pi/AGENTS.md`
  - **commandcode** → `~/.commandcode/AGENTS.md`

A skill copied into `~/.agents/skills` (the default `target`) is invisible to a skills-dir
harness until it is also present in the discovery dir. With `harness_link: true` (the
default), `rig apply` maintains an idempotent symlink for every enabled skill on a skills-dir
harness:

- an existing **correct** symlink is a no-op;
- a symlink to the **wrong** destination is re-pointed;
- a **real** (non-symlink) dir/file already at the path is **left untouched** — some skills
  are hand-authored real dirs (e.g. `h-reason`, `debate-swarm`), and rig must not clobber
  them. `rig status` reports a missing/wrong link as drift; a real dir is not flagged.

The discovery dir is keyed by the harness `kind` (defaulting to claude-code, or following
`harness.kind` when a `harness:` block pins one). Set `harness_link: false` to opt out, or
`harness_skill_dir` to point at a non-default location — an explicit `harness_skill_dir`
forces a directory link **even for an instruction-file harness** (you pointed at a real dir
on purpose).

The harness symlink is the **one action that does not consult `on_conflict`**: a wrong
symlink is always re-pointed (a symlink carries no user data to back up), and a real dir is
always left alone (no policy ever clobbers hand-authored content). `on_conflict` governs file
and directory *content*, which a discovery symlink has neither of.

---

## `agent_hooks`

```yaml
agent_hooks:
  enabled: true
  target: ~/.claude/hooks
  target_kind: claude-code          # claude-code | generic
  all: true
  worktree_only: true               # enforce the worktree-only workflow in THIS repo (default off)
  orchestrator_only: true           # keep the orchestrator thin in THIS repo (default on)
  items:
    block-no-verify:     { enabled: true,  on_error: closed }
    block-reset-hard:    { enabled: true,  on_error: closed }
    enforce-timeout-on-bash: { enabled: true, on_error: open }
```

| Per-item key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | install this hook |
| `on_error` | `open`/`closed` | descriptor's value | fail policy (security = closed) |

The install action always writes an **absolute** `cmd` (rewriting the
`/ABSOLUTE/PATH/TO/...` placeholder to the script's real path in the agent-tools checkout),
per the `agents-hooks/v1` contract.

When `harness.kind: codex` is active, rig defaults the descriptor target to `~/.codex/hooks`
unless `agent_hooks.target` / `defaults.hooks_target` is set to a non-default custom path. Claude
behavior is unchanged: `claude-code` still defaults to `~/.claude/hooks`.

### Per-repo workflow knobs — `worktree_only` / `orchestrator_only`

Two booleans that configure the **runtime behaviour** of two installed hooks per repo. They
are read by the hook scripts from **this committed `rig.yaml`** at fire time — `rig apply`
does **not** consume them (nothing to install; the hooks self-read the knob). They live in the
`agent_hooks` block so the strict validator accepts them.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `worktree_only` | bool | `false` | **opt-IN.** When `true`, the `worktree-only-writes` pre-write hook **denies an Edit/Write while the checkout is on the repo's default branch** (main/master, detected via `origin/HEAD` / `init.defaultBranch`, never hardcoded). Authoring must happen in a separate worktree on a feature branch; main is for merge/pull/read-only. Default **off** so a repo that legitimately works on main (e.g. `3d-cli`) is never blocked. Escape hatch for a deliberate one-off: `RIG_ALLOW_MAIN_EDIT=1`. (Alex tg#5742.) |
| `orchestrator_only` | bool | `true` | **opt-OUT.** When `true` (default), the `orchestrator-stays-thin` hook blocks inline implementation Bash / code Edits by the main thread (delegate to a subagent). It allows read-only inspection **and** orchestration (`gh pr list/view/checks`, `gh ship`, `tg`, `review`, `git worktree list`). Set `false` to exempt a repo that works inline (e.g. `3d-cli`). Default **on** = no behaviour change for a repo that omits the key. Escape hatch: `ALLOW_ORCHESTRATOR_WORK=1` + reason. (Alex tg#5743.) |

Both take effect only where the corresponding hook is installed (`agent_hooks.all: true`, or
the item enabled). They are **runtime** signals, not provisioning — a fresh clone reads the
committed value directly; no re-`apply` is needed to change enforcement.

---

## `git_hooks`

v0.1 ships the **global dispatcher** (the headline feature — your hooks run in every repo,
even ones that hijack `core.hooksPath`). Per-repo hook templates are deferred to v0.2.

```yaml
git_hooks:
  dispatcher:
    enabled: true
    dir: ~/.config/git/global-hooks.d         # drop-in fragments dir
    runner: ~/.config/git/run-global-hooks     # the dispatcher script
    set_global_hooks_path: true                # set global core.hooksPath (records prior value)
    install_local_retrofit_script: true        # put install-local-hooks.sh on ~/.local/bin
    fragments:
      secret-scan:         { enabled: true }
      conventional-commit: { enabled: false }
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `dispatcher.enabled` | bool | `false` | install the global-hook dispatcher |
| `dispatcher.dir` | path | `~/.config/git/global-hooks.d` | fragments dir |
| `dispatcher.runner` | path | `~/.config/git/run-global-hooks` | the runner |
| `dispatcher.set_global_hooks_path` | bool | `true` | wire it as global `core.hooksPath` (backs up the prior value into the action note) |
| `dispatcher.install_local_retrofit_script` | bool | `true` | put `install-local-hooks.sh` on PATH |

---

## `ci`

```yaml
ci:
  enabled: true
  target: .github/workflows        # or `export-only` (record choices, write no files)
  all: false
  items:
    secret-scan:       { enabled: true,  tier: block }
    codeql:            { enabled: true,  tier: block, variant: selfgate }
    dependency-review: { enabled: true,  tier: block }
    leftover-grep:     { enabled: true,  tier: block }
    review-threads:    { enabled: true,  tier: block }
    ship:
      enabled: true
      install_to: ~/bin              # ship is a client command, not a workflow
      gh_alias: true                 # gh alias set ship
```

| Per-item key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | per-item | install this gate |
| `tier` | `block`/`warn` | `block` | enforcement strength (recorded; the workflow itself encodes it) |
| `variant` | str | — | e.g. codeql `selfgate` selects `workflow-selfgate.yml` |
| `install_to` / `gh_alias` | path/bool | — | ship-specific (it is a client command) |

`target: export-only` records the choices without writing files (an agent applies later or
a non-GitHub CI uses each slot's `*.sh`).

---

## `mcp`

```yaml
mcp:
  enabled: true
  target: ~/.claude/mcp            # or ./.mcp.json | export-only
  items:
    code-search:
      enabled: true
      server: serena               # a code-search server (serena/sverklo) — see mcp/README.md
      command: ""                  # no command → nothing to register (reported, not an error)
      args: []                     # optional: when present, command is not shell-split
      env: {}                      # optional environment variables for this server
```

> `review` is **not** an MCP item — it is a CLI + skill, and its MCP slot was removed in
> agent-tools #32. A config that still declares `mcp.items.review` is rejected as a removed
> slot (`rig status` exits 4); remove it.

The install action merges an MCP entry **idempotently by server name** into
`<target>/mcp.json` (or the target file if it ends in `.json`) and never overwrites an
existing differing entry unless `defaults.on_conflict: overwrite`.

| Per-item key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | per-item | register this server |
| `server` | str | item name | key written under `mcpServers` |
| `command` | str | `""` | executable/launch command; empty skips registration even when `args`/`env` are set |
| `args` | list[str] | shell-split from `command` | when present, use `command` exactly and this argv exactly |
| `env` | map[str,str] | omitted | non-empty environment map written into the MCP server entry |

Compatibility: if `args` is omitted, rig keeps the legacy behavior and parses `command` as a
shell-like string, so `node server.js --foo` becomes `{"command":"node","args":["server.js","--foo"]}`.
If `args` is present, `command` is the executable value exactly, allowing paths such as
`/opt/my tools/run` without quoting.
Do not put arguments or shell syntax into `command` when `args` is present:
use `command: "node"` with `args: ["server.js", "--foo"]`, not
`command: "node server.js"` with `args: ["--foo"]`.

---

## `harness`

Provisions the **agent harness's auto/permission mode** as part of the reconciler. With a
`harness:` block, `rig apply` writes the harness's permission setting so **auto-mode**
(the agent runs autonomously, auto-accepting tool calls, with minimum babysitting) is part
of the reproducible config — not a manual per-machine toggle. **Recommended on by default**:
auto-mode is safe because the agent-hook guards (`block-secrets-write`, `block-no-verify`,
`enforce-timeout-on-bash`, `block-raw-process-env`, `block-raw-pr-merge`, `block-reset-hard`)
are installed in the same apply and catch the dangerous tool calls before the side effect.

```yaml
harness:
  enabled: true
  kind: claude-code            # skills-dir: claude-code | codex · native: opencode · instruction-file: gemini | pi | commandcode
  auto_mode: true              # true → auto-accept tool calls; false → interactive prompts (claude-code write only)
  # mode: bypassPermissions    # optional: pin the exact mode value (overrides the auto_mode map)
  # settings_path: .claude/settings.json   # where to write (repo-local default; committed)
  hook_bridge:                 # wire the agents-hooks/v1 → harness dispatcher (default ON)
    enabled: true              # set false to skip wiring the dispatcher into harness config
    # python: python3          # optional: the interpreter the dispatcher runs under
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the harness setting (set `false` to leave the harness config untouched) |
| `kind` | enum | `claude-code` | which harness to provision. Skills-dir (`claude-code`, `codex`) get per-skill symlinks; native-discovery (`opencode`) auto-loads `~/.agents/skills`; instruction-file (`gemini`, `pi`, `commandcode`) get their skill discovery via `AGENTS.md`/`GEMINI.md`. The auto/permission-MODE write below is `claude-code`-only today; other kinds still get skill discovery |
| `auto_mode` | bool | `false` (scaffold writes `true`) | `true` = auto-accept; maps to the harness's non-interactive permission value |
| `mode` | str | — | pin the exact permission value (e.g. `acceptEdits`), overriding the `auto_mode` mapping |
| `settings_path` | path | `.claude/settings.json` for Claude auto/mode writes; `~/.codex/config.toml` for the Codex hook bridge | the settings/config file to merge into. If overridden, point it at the harness's native format (JSON for Claude, TOML for Codex). A suffixed path is treated as the file; a suffixless path is treated as a directory containing the native settings filename |
| `hook_bridge.enabled` | bool | `true` | wire the harness bridge dispatcher so installed agent-hooks actually fire (`cc_hook_bridge` for Claude, `codex_hook_bridge` for Codex) |
| `hook_bridge.python` | str | `python3` | the Python interpreter the dispatcher command runs under |

**What gets written.** For `kind: claude-code`, rig merges `permissions.defaultMode` into
the settings JSON — `auto_mode: true` → `bypassPermissions` (auto-accepts every tool call),
`auto_mode: false` → `default` (interactive prompts). Only that one key is touched; every
other setting in the file is preserved. The write is **idempotent** (a re-apply with the
same value is a no-op) and **backup-noted** (a differing prior value is backed up per
`defaults.on_conflict` before converging). `rig status` reports drift if the on-disk value
no longer matches the config.

**Auto-mode write is claude-code-only (for now).** `kind: opencode` (and `codex`/`gemini`/
`pi`/`commandcode`) are now **accepted** — rig provisions their **skill discovery** (see
[Harness skill discovery](#harness-skill-discovery-why-harness_link)). But the auto/permission-
**mode** write is still implemented only for `claude-code`. If you set `auto_mode`/`mode` on a
kind without a mode-writer yet, rig **skips that write and says so** in a plan note (its skills
are still provisioned) rather than silently doing nothing — set the mode in the harness's own
config for now. opencode expresses the same intent through a `permission` block in its
`opencode.json` (`"permission": { "edit": "allow", "bash": "allow" }` for auto-accept, vs
`"ask"` for interactive); wiring that write is tracked separately.

**The hook bridge (`hook_bridge`).** Harnesses only run hooks declared in their own config; they
do not execute the `agents-hooks/v1` descriptors directly. Without a bridge, installed
agent-hooks are inert. So when a supported harness block is present (and `agent_hooks` is
enabled), `rig apply` registers the matching dispatcher from `agent-tools/lib`:

- `kind: claude-code` writes `cc_hook_bridge` into `settings.json`: `PreToolUse` matchers
  `Bash`, `Edit|Write|MultiEdit|NotebookEdit`, and `Agent|Task`; `PostToolUse` matcher
  `Edit|Write|MultiEdit|NotebookEdit`; and `Stop`.
- `kind: codex` writes `codex_hook_bridge` into `~/.codex/config.toml` (or `settings_path`) as a
  managed `[hooks]` TOML block: `PreToolUse` matchers `Bash` and `apply_patch`, `PostToolUse`
  matcher `apply_patch`, and `Stop`. A custom Codex `settings_path` must end in `.toml`.

Each command runs `PYTHONPATH=<agent-tools>/lib python3 -m <bridge_module> <Event>`. The merge is
idempotent and preserves unrelated config; `rig status` reports the bridge as missing/stale drift
if a managed hook is absent or points at an old checkout. Other harness kinds still skip the bridge
with a note when explicitly enabled.

Codex `pre-agent` is not wired yet: rig may install `pre-agent` descriptors into `~/.codex/hooks`
alongside the rest of the selected catalog, but the Codex bridge currently dispatches only
`pre-bash`, `pre-write`, `post-write`, and `stop` via the confirmed Codex hook events above.
The managed TOML block has this shape, with full commands including the resolved
`PYTHONPATH=<agent-tools>/lib`:

```toml
[hooks]
# >>> rig managed: codex hook bridge
PreToolUse = [{matcher = "Bash", hooks = [{type = "command", command = "... codex_hook_bridge PreToolUse"}]}, {matcher = "apply_patch", hooks = [{type = "command", command = "... codex_hook_bridge PreToolUse"}]}]
PostToolUse = [{matcher = "apply_patch", hooks = [{type = "command", command = "... codex_hook_bridge PostToolUse"}]}]
Stop = [{hooks = [{type = "command", command = "... codex_hook_bridge Stop"}]}]
# <<< rig managed: codex hook bridge
```

Codex TOML merge is intentionally conservative: rig refuses to add the managed bridge when the
top-level `hooks` table already uses an inline table (`hooks = { ... }`), array-of-tables
(`[[hooks]]`), dotted hook keys (`hooks.PreToolUse = ...`), nested hook tables
(`[hooks.foo]`), or unmanaged keys for events rig owns (`PreToolUse`, `PostToolUse`, `Stop`)
inside `[hooks]`. It also refuses files containing TOML triple-quote tokens (`"""` or `'''`),
because multiline strings can contain table-shaped text that this preservation merge must not
reinterpret.
Unrelated `[hooks]` keys such as `Notification` are preserved, as are `hooks` keys inside other
tables such as `[profiles.default]`.

---

## `permissions`

Reconciles the **per-harness permissions layer** (rig-cli#100): the command **ALLOWLIST** — our
ecosystem CLIs and the safe-to-allow external dev tools **pre-allowed**, so the agent never
stops to ask permission for a known-safe command — plus, for claude-code, the conservative
**deny/ask rule baselines** (the outer enforcement belt: the harness evaluates
deny → ask → allow *before* PreToolUse hooks and independently of the model; the argv-parsing
agent-hook guards stay the deep layer underneath). **Default ON**: an absent/empty
`permissions:` block still provisions the default tool set AND the deny/ask baselines, so
`rig init`/`rig apply` on a clean machine gets both with no config at all. Everything is
**config-driven** — `tools` REPLACES the default set, `extra` adds, `disable` removes; `allow`
adds RAW rule entries on top of the tool-derived allowlist; `deny`/`ask` REPLACE the baked rule
baselines (an explicit `[]` disables one). The merge is **additive** — every existing entry in
every list (auto-mode, your accumulated allowlist, your own deny/ask rules) is preserved, the
desired entries are merged in, deduped; a re-apply is a no-op.

**Scope of the default set, honestly.** rig pre-allows the *tools* in the list at the
command-prefix level — for claude-code that is `Bash(<tool>:*)`, which DOES cover every subcommand
and flag of that tool (so `git` includes `git push --force`, `gh` includes `gh repo delete`). The
default set is therefore "tools we trust the agent to drive", not "only read-only subcommands". The
restraint is in WHICH tools are on the list: it is dev/VCS tooling we already lean on, and it does
NOT add inherently-destructive standalone commands (`rm`, `sudo`, `dd`, `mkfs`, …) — those stay
behind a prompt. If you want a narrower grant (e.g. only `git status`/`git log`), set `tools` to a
custom list and add the specific `Bash(...)` entries by hand in the harness settings.

This is **GLOBAL-only** (the allowlist file is per-machine), like `tg_ctl`/`gitignore` — it is
never scaffolded into a committed repo `rig.yaml`; declare it in `~/.config/rig/config.yaml` to
override the tool list.

```yaml
permissions:
  enabled: true                # provision the permissions layer (false → leave the harness config alone)
  # kind: opencode             # target opencode's allowlist (default: follow harness.kind / claude-code)
  # tools: [tg, review, draw, 3d, rig, task, gh, git, rg, uv, bun, jq, gitleaks]  # REPLACES the default set
  # extra: [kubectl]           # ADD to the (default or explicit) set
  # disable: [gitleaks]        # drop from rig's desired set (won't ADD it; never removes a live entry)
  # allow:                     # RAW rule entries asserted present in the allow list (on TOP of tools)
  #   - WebFetch
  #   - "Read(//private/tmp/reports/**)"
  # deny: []                   # REPLACES the baked deny baseline ([] disables it; absent → baseline)
  # ask: []                    # REPLACES the baked ask baseline ([] disables it; absent → baseline)
  # settings_path: ~/.claude/settings.json   # override the per-harness settings file (rare; JSON)
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the permissions layer (set `false` to leave the harness config untouched) |
| `kind` | `claude-code` \| `opencode` | follows `harness.kind`, else `claude-code` | which harness's permissions to provision. `opencode` is supported here independently of `harness.kind` (whose auto-mode write reserves it); `codex`/`gemini`/`pi` are rejected (N/A) |
| `tools` | str[] | the default set | the command names to pre-allow; **replaces** the default set wholesale |
| `extra` | str[] | `[]` | command names to ADD on top of the (default or explicit) set |
| `disable` | str[] | `[]` | command names to drop from rig's **desired** set, so rig won't ADD them. NB: this is additive-only — it does NOT delete an entry already in your allowlist (rig never removes the user's entries; that stays your call) |
| `allow` | str[] | `[]` | RAW permission-rule entries (`Tool` or `Tool(specifier)`, e.g. `WebFetch`, `Bash(kubectl get:*)`, `Read(//tmp/**)`) asserted present in the allow list, on TOP of the tool-derived entries — this is how a hand-grown allowlist is adopted as declared config. claude-code only — see below |
| `deny` | str[] | the baked deny baseline | rule entries asserted present in the deny list; **replaces** the baseline wholesale (`[]` disables it). claude-code only — see below |
| `ask` | str[] | the baked ask baseline | rule entries asserted present in the ask list; **replaces** the baseline wholesale (`[]` disables it). claude-code only — see below |
| `settings_path` | path (JSON) | per-harness default | override the settings file to merge into (default: `~/.claude/settings.json` for claude-code, `~/.config/opencode/opencode.json` for opencode). A suffixed path is treated as the file; a suffixless path is treated as a directory |

**Default tool set.** Our ecosystem CLIs — `tg`, `review`, `draw`, `3d`, `rig`, `task` — plus
the external tools we lean on: `gh`, `git`, `rg`, `uv`, `bun`, `jq`, `gitleaks`.

**Baked deny/ask baselines (claude-code, rig-cli#100).** Deliberately conservative and
word-boundary precise — a deny rule that false-positives on legitimate commands teaches agents
to route around the belt. Deny: `Bash(gh pr merge:*)` (merges go through `gh ship`),
`Bash(git push --force:*)` + `Bash(git push * --force *)` + `Bash(git push * --force)` +
`Bash(git push -f:*)` + `Bash(git push * -f *)` + `Bash(git push * -f)` (force pushes in
flag-first, mid AND end-anchored positions; `--force-with-lease` is NOT matched — the word
boundary excludes it), `Bash(git commit --no-verify:*)` (flag-first only: the flag-anywhere form
cannot be pattern-matched without false-positiving on commit messages that merely mention the
flag — the `block-no-verify` agent-hook remains the authoritative argv-level guard),
`Bash(sudo rm:*)`, and `Bash(screencapture:*)` (screenshots go through Playwright/CDP). Ask
(prompt, don't block): `Bash(pkill:*)`, `Bash(killall:*)`, `Bash(git reset --hard:*)` +
`Bash(git reset * --hard *)` + `Bash(git reset * --hard)`. The full annotated list lives in
`riglib/permissions.py`. For **opencode** the raw `allow`/`deny`/`ask` lists are **N/A**: its
`permission.bash` object accepts deny/ask values, but its glob-key dialect is a DIFFERENT syntax
from claude-code rule strings and is unverified for multi-word rules — a configured raw list
under `kind: opencode` is dropped with a visible plan note, never guessed at (a claude-shaped
rule written as an opencode glob key would be a bogus entry that never matches). The
tool-derived allowlist (`tools`/`extra`/`disable`) still works for opencode.

**What gets written, per harness (keyed off `harness.kind`).**

- **claude-code** — `permissions.allow` in `~/.claude/settings.json` (a JSON array) gains an
  entry per tool in the form `Bash(<tool>:*)` (matching the prefix-glob form CC honors). The
  array is merged additively + deduped; every other entry survives.
- **opencode** — `permission.bash` in `~/.config/opencode/opencode.json` (a JSON object) gains a
  `"<tool> *": "allow"` entry per tool, only when the key is absent — an existing user
  `"deny"`/`"ask"` is never downgraded.
- **codex** — **N/A**. `~/.codex/config.toml` has no per-command allowlist rig can additively
  merge; command execution is gated by `approval_policy`/`sandbox_mode` (coarse) and Starlark
  `execpolicy` `.rules` files — a separate mechanism. Recorded N/A, never written.
- **gemini / pi** — **N/A**. Gemini's `tools.core`/`coreTools` is a *toolset restriction* list,
  not a per-command auto-approve: writing it to pre-allow a command would disable every unlisted
  built-in tool. No safe per-command allowlist exists, so it is recorded N/A, never written.

The write is **idempotent** (a re-apply with the same config is a no-op) and **backup-noted** (the
prior file is backed up per `defaults.on_conflict` before converging; a file that fails to parse as
JSON is backed up before being rewritten for any policy other than `skip`, which leaves it
untouched and surfaces the drift). `rig status` reports a desired entry that is absent as `missing`
drift `rig apply` would add; a wrong-shape file (non-object root, non-array/non-object container) as
`modified` (matching the error `apply` would raise — status and apply agree on what to fix); and a
user entry BEYOND the rig-managed baseline as `extra` drift — reported, **never deleted** (allow
extras are summarized into one counted item, since a live allowlist accumulates hundreds of
hand-approved entries; deny/ask extras are named per entry). To silence an allow `extra`, adopt the
entry into `permissions.allow` in the config or prune it from the settings file by hand.

---

## `models`

Provisions a **daily cron that runs the agent-tools model-freshness checker**
(`lib/checker/model_freshness.py`), which polls provider model-list endpoints and proposes
version bumps to the model board (`agent-tools/lib/contracts/models.yaml`). Per the
provisioning rule, on **`rig init` AND `rig apply`** rig **checks whether the schedule is
installed and installs it if missing** (idempotent — a re-apply that finds it present and
current is a no-op).

```yaml
models:
  enabled: true                # provision the daily checker schedule (false → leave system cron alone)
  schedule:
    time: "12:00"              # daily run time, HH:MM 24h (default: noon)
    # label: ai.hyperide.model-freshness   # launchd Label / crontab sentinel (advanced)
  # checker_path: ~/xp/agent-tools/lib/checker/model_freshness.py   # default: resolved from agent_tools_source
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` (scaffold) | provision the daily schedule (set `false` to leave the system cron untouched) |
| `schedule.time` | `HH:MM` | `12:00` (noon) | daily run time, 24-hour; fail-closed on a malformed/out-of-range value |
| `schedule.label` | str | `ai.hyperide.model-freshness` | the launchd Label / crontab sentinel identity (one string across platforms) |
| `checker_path` | path | resolved from `agent_tools_source` | the `model_freshness.py` the schedule runs |

**Cross-platform.** The CTO asked for a "cron"; rig provisions the platform-native
equivalent:

- **macOS → launchd.** A `~/Library/LaunchAgents/<label>.plist` with a daily
  `StartCalendarInterval` (Hour/Minute), loaded via `launchctl load`. (cron is
  deprecated/unmanaged on macOS; launchd is the supported scheduler.)
- **Linux → crontab.** A single managed crontab line `MIN HOUR * * * python3 <checker>`,
  fenced by a `# rig-managed: <label>` sentinel comment so it is idempotent (re-apply finds
  it by sentinel) and removable, and so it never disturbs the user's other crontab lines.

**Idempotency (the "check whether the cron exists and install it if missing" rule).** Both `init` and `apply`
run this. A present-and-current schedule re-applies as a no-op (`skipped`); a missing or
drifted one is (re)installed. `rig status` reports the schedule explicitly (installed /
drifted / not configured) and surfaces a wrong run time or checker path as drift; `rig
doctor` flags a missing scheduler binary (`launchctl`/`crontab`).

Set `RIG_SCHEDULE_DRY_RUN=1` to write the artifact file but **skip the live daemon mutation**
(no `launchctl load`, no `crontab` write) — for CI, containers, or smoke tests where touching
the real per-user scheduler is unwanted.

---

## `agents_md`

Provisions one **canonical agent-guide file** in the repo, exposed under both conventional
names so every harness reads the same instructions: **`AGENTS.md`** is the real file
(Codex/most agents read it) and **`CLAUDE.md`** is a relative symlink to it (Claude Code
reads it). Default **ON** — on `rig init` AND `rig apply`, rig converges the repo to this
invariant; idempotent (a re-apply that finds it already correct is a no-op).

```yaml
agents_md:
  enabled: true     # provision AGENTS.md (canonical) + CLAUDE.md (symlink). Default ON.
  # symlink: false  # equivalent opt-out (leave the repo's files untouched)
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the canonical + symlink pair (set `false` to opt out) |
| `symlink` | bool | `true` | alias opt-out — `false` is equivalent to `enabled: false` |

**Canonical direction.** `AGENTS.md` is canonical by convention. A **real file always wins**:
a repo that already has a real `CLAUDE.md` and no real `AGENTS.md` keeps `CLAUDE.md` as the
source of truth and makes `AGENTS.md` the symlink — rig never demotes a real file to a link.
rig **writes a relative symlink** (just the filename) so the link it creates stays valid when
the repo moves.

**Safety-first.** rig only ever (a) creates into an **empty** slot, (b) collapses two
**identical real** files to a symlink, or (c) no-ops an already-correct pair. Every other
shape is a **conflict** that rig leaves completely untouched and surfaces via `rig status` —
it never clobbers a real file, a user-placed symlink, or a directory.

**On-disk cases (idempotent, never destructive):**

- **both absent** → create `AGENTS.md` (a minimal placeholder) + `CLAUDE.md` symlink.
- **one real, the other absent** → symlink the other → the real one.
- **one real, the other already the correct symlink** → no-op (in sync).
- **both real & identical content** → converge the link side (`CLAUDE.md`) to a symlink,
  honoring `on_conflict` (`backup` keeps a `CLAUDE.md.rig-bak-*` copy; `skip` leaves both real
  files). `rig status` reports the un-converged pair as drift.
- **conflict — left untouched, reported as drift:** both real with **different** content; a
  real file on one side with a **foreign symlink or a directory** on the other; **neither**
  slot a real file (a stray symlink/dir occupies one, e.g. an `AGENTS.md → CLAUDE.md` peer
  link). rig won't pick a winner or risk a symlink loop — reconcile to one real file (or a
  correct symlink) and re-apply.

`rig status` flags every state in which `rig apply` *would* act (missing pair, missing link,
un-converged identical files) plus the conflicts above; a correct canonical+symlink pair is in
sync.

---

## `ship_delegator`

Provisions the per-repo **`gh ship` delegator** — `.claude/scripts/pr-ship.sh` — so the global
`gh ship` alias (which runs `<repo>/.claude/scripts/pr-ship.sh`) works in **this** repo on a clean
machine. Historically that delegator existed **only in agent-tools**, so `gh ship` failed in every
other managed repo (papered over by a runtime alias fallback); this is the durable fix — rig
provisions it everywhere. Default **ON** — on `rig init` AND `rig apply` rig writes/reconciles the
delegator; idempotent (a re-apply that finds it correct is a no-op).

```yaml
ship_delegator:
  enabled: true     # provision .claude/scripts/pr-ship.sh so `gh ship` works here. Default ON.
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the delegator (set `false` to leave the repo untouched) |

**What the delegator does.** It's a thin shim that `exec`s the **canonical** generalized ship
implementation (agent-tools' `ci/ship/ship.sh`). Resolution order: a **repo-local**
`ci/ship/ship.sh` first (so agent-tools self-hosts and runs its own checked-out version), then
`$AGENT_TOOLS_ROOT` from the environment, then `$AGENT_TOOLS_ROOT` sourced from the
**machine-level env file** `${XDG_CONFIG_HOME:-~/.config}/agent-tools/env`, which `rig apply`
writes idempotently from the agent-tools checkout it applied from. The rendered delegator itself
is a **portable constant** — no machine-specific path is ever baked into it — so a repo may even
commit the file verbatim (agent-tools does) and a re-apply stays a byte-for-byte no-op. A clean
machine therefore gets a working `gh ship` in every repo with no alias hacks.

**Repo-local shadowing is intentional (and a debugging footgun to know about).** Because the
delegator runs a repo-local `ci/ship/ship.sh` first, any repo that happens to carry one — a stale,
hand-edited, or old copy — will run THAT, not the canonical resolved via `AGENT_TOOLS_ROOT`, and
rig emits **no drift**
for it (rig reconciles the delegator's bytes, never the repo-local ship.sh it may exec). This is by
design for agent-tools' self-hosting; if `gh ship` "runs the wrong thing" in another repo, check for
a stray `ci/ship/ship.sh` in that repo.

**Worktree hygiene — the provisioned file does NOT dirty the tree.** `ship` refuses to merge from
a dirty worktree, so an un-ignored provisioned file would break the very command it enables. rig
adds `.claude/scripts/pr-ship.sh` to the repo's **`.git/info/exclude`** (the per-repo,
**never-committed** git exclude), worktree-aware: in a git worktree `info/exclude` is **per-worktree**
(it lives in that worktree's private gitdir, and `.git` is a file not a dir), so rig resolves the real
path via `git rev-parse --git-path info/exclude` rather than assuming `<repo>/.git/info/exclude`. The
entry is fenced with rig markers and
reconciled idempotently (duplicates collapse, every other line is preserved verbatim).

**Fail-closed.** If the agent-tools checkout has no `ci/ship/ship.sh`, rig provisions **nothing**
(a note explains why) rather than writing a delegator that would exec a non-existent script.

**Drift.** `rig status` flags the delegator as drift when the file is missing, its bytes differ
from the rig-generated delegator, or the file is present but **not** git-ignored (an un-ignored
delegator would dirty the worktree). `rig apply` reconciles. Shown in the **repo** section.
The **machine env file** is checked too, under its own `ship_env` category — shown in the
**global** section, since the file is a machine-wide artifact (`rig apply --only ship_env`
scopes to the owning `ship_delegator` action). A repo that carries its own `ci/ship/ship.sh`
never reads the env file, so both `status` and `apply` leave it entirely alone for that repo —
no check, no write, no backup (status/apply parity in both directions). The env-file check also
survives a **non-git** cwd: `status` there drops repo-scoped areas, but `apply` still reconciles
the machine env file, so the `ship_env` check keeps running (parity again — a missing/stale env
file surfaces even from `rig status` in `~`).

---

## `linters`

Provisions this repo's **linter + formatter config files** — the same reconciled-area treatment
skills/hooks/CI/ship get, applied to tool config. You declare, per repo, **which** config file each
tool needs and the **exact bytes** it should hold; `rig init` / `rig apply` writes/repairs each
file, and `rig status` reports drift. rig hardcodes **no** specific linter — the tool, the path, and
the content are all per-repo config, so it provisions an `oxfmt` formatter, a `ruff` linter, an
`eslint`/`prettier` pair, or anything else equally. Default **ON**; opt out of the whole area with
`enabled: false`, or a single file with `items.<label>.enabled: false`.

```yaml
linters:
  enabled: true                 # provision the declared config files. Default ON.
  items:
    oxfmt-format:               # a free-form LABEL (any unique key)
      tool: oxfmt               # the tool name (informational; used in status/logs)
      role: formatter           # linter | formatter (default linter; status label only)
      path: .oxfmtrc.jsonc      # repo-relative file path (must stay inside the repo)
      content: |                # the EXACT file content rig writes/reconciles
        {
          "indentWidth": 2,
          "lineWidth": 100
        }
    ruff-lint:
      tool: ruff
      role: linter
      path: ruff.toml
      content: |
        line-length = 100
        [lint]
        select = ["E", "F", "I"]
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the declared config files (set `false` to skip the whole area) |
| `items` | map | `{}` | the config files, keyed by a free-form label |
| `items.<label>.tool` | string | — | the tool name (required; informational — drives the status/log label) |
| `items.<label>.role` | enum | `linter` | `linter` or `formatter` — rendered in the apply/status label (`<role> <tool>:<item>`); both reconcile identically |
| `items.<label>.path` | string | — | repo-relative path of the config file (required; no leading `/`, `..`, or `\`) |
| `items.<label>.content` | string | — | the exact bytes rig writes/reconciles (required) |
| `items.<label>.enabled` | bool | `true` | provision this one file (set `false` to skip it) |

**Never-clobber.** A config file already on disk that **differs** from `content` is reconciled
honoring `defaults.on_conflict`: `backup` (the default) moves the existing file to a
`<name>.rig-bak-*` copy before writing rig's version, `skip` leaves the hand-edited file untouched,
`overwrite` replaces it with no backup. rig never silently clobbers a hand-written config. A file
whose bytes already match is a true no-op (no needless backup).

**Containment.** A `path` is rejected at validation **and** re-checked at apply/drift time if it is
absolute, escapes the repo with `..`, or contains a backslash (a Windows separator, ambiguous across
platforms) — a committed `rig.yaml` can only provision files **inside** its own repo.

**Line endings are normalized to LF.** rig writes the configured `content` with its line endings
collapsed to `\n`, and the drift compare uses a universal-newline read, so CRLF vs LF is **never**
spurious drift in either direction — a `\r\n`-on-disk file vs an `\n`-in-config file is equivalent
(left untouched), and a `\r\n`-in-config literal converges (rig writes an LF file rather than
rewriting it every run). rig does not churn a file solely over line endings. (A genuine content
difference *is* drift.)

**Symlinks are refused.** A symlink **anywhere** on the path — the final file or any parent
directory, pointing inside the repo or out — is reported as drift that `rig apply` errors on. rig
never writes **through** a link (that could clobber the link target or escape the repo). Resolve the
link to a real file/dir first. A non-directory **parent** (a regular file where a directory must be,
e.g. a file `config` with `path: config/x.toml`) is likewise an error, not a misleading "missing".

**Drift.** `rig status` flags an item as drift when its file is missing or its bytes differ from
the configured `content` (or a directory, a symlink-on-path, a non-directory parent, or an
unreadable / non-UTF-8 file sits at the path). `rig apply` reconciles. Shown in the **repo** section.

**Containment.** A `path` may not be absolute, contain `..`, a backslash, or a `.git` component (a
write into the git dir could rewrite repo metadata / install a hook) — rejected at validation and
re-checked at apply.

**One-way drift (a known limit).** Drift is **config→disk** only: rig flags a declared file that is
missing or wrong, but because a linter config lives at an arbitrary path (not a rig-owned directory
rig can enumerate), **removing** an item from `rig.yaml` does **not** flag the previously-provisioned
file as a disk→config `extra` — it is left in place (consistent with rig's "apply never deletes
on-disk extras" safety rule). Delete the stale config file by hand when you drop its item. Two-way
detection would need a managed manifest (a tracked follow-up).

---

## `project_tools`

Provisions repo-local carriers for the project-intelligence tools used by the agent ecosystem:
Haft, Serena, and Sverklo. This is a **repo** block, distinct from the global `tools:` block:
`tools:` installs personal CLIs into a machine PATH, while `project_tools:` writes committed
project files and safe live registrations so this repository is usable by those CLIs.

```yaml
project_tools:
  enabled: true
  haft:
    enabled: true
    codex_mcp: true
    # project_name defaults to the repo directory name.
    # project_id defaults to a stable qnt_<hash> derived from project_name.
    workflow:
      mode: standard
      require_decision: true
      require_verify: true
      allow_autonomy: false
  serena:
    enabled: true
    # languages auto-detects from repo files when omitted.
    read_only: false
    ignored_paths: []
  sverklo:
    enabled: true
    register: true
    reindex: false
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision project-tool integrations (set `false` to skip the whole area) |
| `haft.enabled` | bool | `true` | write the `.haft/` project/spec/workflow carriers |
| `haft.project_name` | string | repo dir name | Haft project name |
| `haft.project_id` | string | `qnt_<hash>` | stable Haft project id |
| `haft.codex_mcp` | bool | `true` | merge the Haft MCP server into `.codex/config.toml` |
| `haft.workflow.mode` | enum | `standard` | `standard` or `tactical` |
| `haft.workflow.require_decision` | bool | `true` | require explicit decisions for high-impact work |
| `haft.workflow.require_verify` | bool | `true` | require verification evidence before completion |
| `haft.workflow.allow_autonomy` | bool | `false` | allow autonomous Haft execution by default |
| `serena.enabled` | bool | `true` | write `.serena/project.yml` and `.serena/.gitignore` |
| `serena.project_name` | string | repo dir name | Serena project name |
| `serena.languages` | list[str] | auto | Serena language ids; auto-detected from repo files when omitted |
| `serena.read_only` | bool | `false` | disable Serena editing tools for this project |
| `serena.ignored_paths` | list[str] | `[]` | extra Serena ignore patterns |
| `sverklo.enabled` | bool | `true` | provision Sverklo integration |
| `sverklo.register` | bool | `true` | register the repo in the global Sverklo registry during `rig apply` |
| `sverklo.reindex` | bool | `false` | run `sverklo reindex` during `rig apply` |

**Haft.** rig writes `.haft/project.yaml`, `.haft/workflow.md`, parseable placeholder spec
carriers under `.haft/specs/`, and empty tool-owned state directories via `.gitkeep` files. It
does not scan or delete tool-owned data such as `.haft/evidence` contents. When `codex_mcp` is
enabled, rig merges a managed Haft MCP section into `.codex/config.toml`; unrelated Codex config is
preserved, and an existing unmarked `[mcp_servers.haft]` table is replaced by the managed block.

**Serena.** rig writes `.serena/project.yml` plus `.serena/.gitignore` for Serena-local cache and
machine-local overrides. `languages` is optional; when omitted, rig infers common language ids from
repo files such as `pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`, and `Package.swift`.
Serena memories remain tool-owned and are not enumerated as drift.

**Sverklo.** `register` is idempotent: apply checks `sverklo list` first and skips if the repo is
already registered. `reindex` is off by default because indexing can be slow and the registry uses
a shared database. Live Sverklo commands are gated in tests/CI by `RIG_PROJECT_TOOLS_DRY_RUN=1` or
`RIG_SVERKLO_DRY_RUN=1`.

**Drift.** `rig status` flags a declared Haft/Serena/Codex carrier as missing or modified when the
file is absent or differs from the rendered config. Sverklo registration drift is reported when
`sverklo list` does not include this repo (or the CLI is unavailable). `sverklo reindex` is
apply-only maintenance and has no cheap drift read-back.

---

## `github`

Provisions a **GitHub repository branch ruleset** — the modern replacement for branch
protection — on the repo's **default branch**, declaratively and reconciled like every other
category. rig owns the ruleset named `ruleset.name` (default `rig-managed`) and converges it
via `gh api`. Default **ON**: on `rig init` AND `rig apply` rig creates the ruleset if absent,
updates it if it drifted from config, and no-ops if it already matches. A repo with **no
github remote is a no-op** (the action reports `skipped`, never an error) — so "default ON when
the repo has a github remote" needs no extra flag.

```yaml
github:
  ruleset:
    enabled: true                 # provision the ruleset (default ON; false opts out)
    name: rig-managed             # rig owns rulesets with this name
    require_pull_request: true    # pull_request rule
    required_reviews: 0           # required_approving_review_count
    required_conversation_resolution: true  # block merge while a review thread is open
    dismiss_stale_reviews: true   # drop a stale approval on a new push
    block_force_push: true        # non_fast_forward rule
    restrict_deletion: true       # deletion rule
    require_linear_history: false # required_linear_history rule
    require_signatures: false     # required_signatures rule
    # required_status_checks: omit to auto-default to the merge-gating CI gates this repo
    # provisions (PR Checklist + review-threads); set a list to pin, or [] to require none.
    admin_bypass: true            # add the repo Admin role to bypass_actors
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the ruleset (set `false` to leave the repo's rulesets untouched) |
| `name` | str | `rig-managed` | the ruleset rig owns/reconciles (rig only ever touches a ruleset with this name) |
| `require_pull_request` | bool | `true` | emit the `pull_request` rule (require a PR to merge to the default branch) |
| `required_reviews` | int ≥ 0 | `0` | `required_approving_review_count` on the `pull_request` rule |
| `required_conversation_resolution` | bool | `true` | `required_review_thread_resolution` on the `pull_request` rule — the SERVER blocks any merge while any review thread is unresolved (the open-thread durable fix). Does not require a reviewer or an approval, but does block the merge until all threads are resolved. **Inert if `require_pull_request: false`** (the whole `pull_request` rule is then not emitted) |
| `dismiss_stale_reviews` | bool | `true` | `dismiss_stale_reviews_on_push` on the `pull_request` rule — dismisses any approval (including voluntary ones) when a new commit is pushed. **Inert if `require_pull_request: false`** |
| `block_force_push` | bool | `true` | emit the `non_fast_forward` rule (block force-push) |
| `restrict_deletion` | bool | `true` | emit the `deletion` rule (block deleting the branch) |
| `require_linear_history` | bool | `false` | emit the `required_linear_history` rule |
| `require_signatures` | bool | `false` | emit the `required_signatures` rule |
| `required_status_checks` | list[str] | *auto* | contexts for the `required_status_checks` rule. **When omitted**, rig defaults it (ROADMAP §5) to the merge-gating CI gates this repo actually provisions — `PR Checklist` and `review-threads` — so a PR can't merge until those are green, but ONLY for gates that are enabled and written (requiring an absent check would wedge every PR). An explicit list (including `[]` for "require none") wins verbatim. **An empty result emits no rule** (never a no-op rule) |
| `admin_bypass` | bool | `true` | add the repo Admin role (`actor_id: 5`, `RepositoryRole`, `always`) to `bypass_actors` |

**The footgun guard — rig never emits the `update` rule.** A hand-made ruleset with the
`update` ("Restrict updates") rule and **zero bypass actors** locks out *every* merge: each
merge is an "update" to the protected default branch, so GitHub answers `Cannot update this
protected ref` and only a repo admin using `--admin` can push past it. rig's rule assembly
**cannot emit the `update` rule** — there is no config knob and no code path that produces it.
It likewise never emits a `required_deployments` rule with an empty environment list (a no-op
smell that can also block). And when `admin_bypass` is on (the default) the repo Admin role is
a bypass actor, so an active ruleset never locks admins out of merging.

**The default ruleset rig emits:** `pull_request` (0 required reviews) + `non_fast_forward` +
`deletion`, with the Admin role in `bypass_actors`. Linear history, signatures, and required
status checks are off/empty. Targets the default branch via the `~DEFAULT_BRANCH` ref token, so
the ruleset follows a rename of the default branch.

**Reconcile + idempotency.** `github_ruleset_state` is the single classification `rig apply`
and `rig status` share (so they can never disagree): `create` (no managed ruleset → POST),
`update` (managed ruleset differs → PUT), `ok` (matches → no-op), `no_remote` (no github origin
→ no-op, no drift). The desired-vs-actual comparison normalizes both sides (sorted rules,
sorted check contexts, identity-only bypass actors, order-independent conditions, and EVERY
managed rule parameter) so a semantic match reads as in sync, not churn. The list call uses
`includes_parents=false` + `--paginate` so an inherited org ruleset is never mistaken for the
repo's, and a managed ruleset on a later page is found rather than duplicated.

**When rig can't reach GitHub (`gh` missing / not authed / API error).** `rig apply` returns an
`error` on the action (it tried to act and couldn't). `rig status` does NOT report the repo as
"in sync" — it surfaces a visible *could-not-verify* drift item, because a green "in sync" while
rig was unable to check would mask a genuinely missing/drifted ruleset. (A repo with **no github
remote** is different: that is a clean no-op, no item.) GitHub Enterprise hosts other than
`github.com` are treated as no-remote (skipped) — only `github.com` rulesets are managed today.

Set `RIG_GH_DRY_RUN=1` to compute what *would* change (the create/update is reported) but make
**no `gh` POST/PUT** — for CI, smoke, or a dry inspection where mutating a real repo is unwanted.

### `github.merge` — the repo merge-button policy (squash-only)

Reconciles the repo's **merge-button policy** via `PATCH /repos/{owner}/{repo}`: squash is the
only merge model (`merge_commit`/`rebase_merge` off → linear, one commit per PR), the head branch
is auto-deleted on merge, and auto-merge is allowed so a PR lands the instant its gate goes green.
These are repo **settings**, not a ruleset, so a mis-set value at worst disables a button — it can
never lock anyone out of merging. Default **ON**; opt out with `merge.enabled: false`.

```yaml
github:
  merge:
    enabled: true                 # provision the merge policy (default ON)
    squash_merge: true            # allow_squash_merge — the only model by default
    merge_commit: false           # allow_merge_commit
    rebase_merge: false           # allow_rebase_merge
    delete_branch_on_merge: true  # auto-delete the head branch on merge
    allow_auto_merge: true        # a PR auto-merges when its required checks pass
    allow_update_branch: true     # offer the "Update branch" button
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the merge policy |
| `squash_merge` | bool | `true` | `allow_squash_merge` |
| `merge_commit` | bool | `false` | `allow_merge_commit` |
| `rebase_merge` | bool | `false` | `allow_rebase_merge` |
| `delete_branch_on_merge` | bool | `true` | auto-delete the head branch on merge |
| `allow_auto_merge` | bool | `true` | allow a PR to auto-merge when green |
| `allow_update_branch` | bool | `true` | offer the "Update branch" button |

### `github.ghas` — GitHub Advanced Security

Reconciles the repo's supply-chain and secret-leak guards: **dependency graph** + **secret
scanning** (+ push protection) via the repo's `security_and_analysis` block (`PATCH /repos`),
**vulnerability alerts** + **Dependabot security updates** via their own `PUT/DELETE`
sub-resources, and **CodeQL default-setup** via its own endpoint. Default **ON** (secure defaults).
On a **private repo whose plan does not include GHAS** the licensed scanners return 403/422 — the
action degrades **loudly** (a visible "could not enable — plan does not include GHAS" in the
result), it does NOT crash and does NOT silently report green; the free features (dep-graph,
vuln-alerts, Dependabot) are applied independently so one unlicensed scanner never masks them.

```yaml
github:
  ghas:
    enabled: true
    vulnerability_alerts: true
    automated_security_fixes: true
    secret_scanning: true
    secret_scanning_push_protection: true
    code_scanning_default_setup: true
```

(There is no `dependency_graph` knob: on github.com cloud the dependency graph is not a
separately-togglable repo setting via the API — it is always-on for public repos and is governed by
the vuln-alerts / Dependabot sub-resources rig manages, so a standalone knob would have no effect.)

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision GHAS settings |
| `vulnerability_alerts` | bool | `true` | the `vulnerability-alerts` sub-resource (Dependabot alerts) |
| `automated_security_fixes` | bool | `true` | Dependabot security updates (`automated-security-fixes`) |
| `secret_scanning` | bool | `true` | `security_and_analysis.secret_scanning` |
| `secret_scanning_push_protection` | bool | `true` | secret-scanning push protection |
| `code_scanning_default_setup` | bool | `true` | CodeQL default-setup (`configured`) |

### `github.actions` — GitHub Actions permissions

Reconciles the repo's Actions permissions on two endpoints: `PUT .../actions/permissions`
(whether Actions runs + which actions are allowed) and `PUT .../actions/permissions/workflow`
(the default `GITHUB_TOKEN` scope + whether workflows may approve PRs). Secure defaults: Actions
**enabled** (don't silently break CI), but the token is **READ-only** (least privilege — a
workflow that needs write declares it explicitly) and workflows may **not** approve PRs.

```yaml
github:
  actions:
    enabled: true
    actions_enabled: true
    allowed_actions: all          # all | local_only | selected
    default_workflow_permissions: read   # read | write
    can_approve_pull_request_reviews: false
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision Actions permissions |
| `actions_enabled` | bool | `true` | whether Actions runs at all |
| `allowed_actions` | enum | `all` | `all` / `local_only` / `selected` |
| `default_workflow_permissions` | enum | `read` | default `GITHUB_TOKEN` scope: `read` / `write` |
| `can_approve_pull_request_reviews` | bool | `false` | may a workflow approve/create PRs |

### `github.browser` — settings the REST API does NOT expose (agent-browser backend)

A first-class **second backend** for the handful of repo settings GitHub has never shipped an API
for. rig drives the GitHub **settings UI** headlessly with `agent-browser` (accessibility-role
selectors, not brittle CSS), invoked **inside** `rig apply` — not a manual "go click this" step.
It is **planned** by default (so `rig status` lists it) but **gated off at apply** unless
`RIG_GH_BROWSER=1` — driving a real browser is heavier and slower than `gh api`, so it runs only
when explicitly enabled. If the toggle is absent from the page (org policy hid it / GitHub moved
it) the action degrades loudly, never a blind click.

```yaml
github:
  browser:
    enabled: true                 # plan the backend (status lists it); apply gated by RIG_GH_BROWSER=1
    discussions: false            # the Discussions UI-only toggle
    projects: true                # the Projects UI-only toggle
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | plan the agent-browser backend (apply still needs `RIG_GH_BROWSER=1`) |
| `discussions` | bool | `false` | the Discussions UI-only toggle |
| `projects` | bool | `true` | the Projects UI-only toggle |

### The auth gate (CTO #4136.1 — ASK and WAIT, never silently fail)

Every `github.*` mutation passes a shared **auth gate** before it touches a live setting. If `gh`
is **not authenticated** for the admin scope it needs (or, for the browser backend, `agent-browser`
is unavailable), rig does **not** silently fail: it **notifies you via `tg`** with the exact
`gh auth login` command and **blocks/waits** for auth to appear, then **resumes** the apply.

- `RIG_GH_AUTH_WAIT` — max **seconds** to block waiting for login. **Unset/`0` (the default)** →
  do not block: probe once and, if still unauthenticated, degrade loudly (so an **unattended**
  `rig apply` in CI never hangs forever on a human). A positive value (e.g. `1800` = 30 min) opts
  into the interactive "ask and wait": rig notifies once, then polls until you log in or the budget
  elapses.
- `RIG_GH_AUTH_POLL` — seconds between re-probes while blocking (default `5`).
- Under `RIG_GH_DRY_RUN=1` the auth **gate is skipped** (dry-run performs no `gh` POST/PUT, so it
  never blocks on a login prompt — this is why CI and the test suite don't hang). Note the dry-run
  still performs the read-only `gh api` GET to classify live-vs-desired state, so on a repo with a
  github remote a dry-run with **no token at all** reports a read error rather than a preview; an
  offline/no-auth preview is only fully no-op on a repo with no github remote (skipped). In CI the
  test suite monkeypatches the `gh` seam, so neither the gate nor the read ever runs for real.

---

## `tmux`

rig **MANAGES tmux configuration declaratively** from this block — generating the config from
`rig.yaml` and **MIGRATING** an existing hand-written `~/.tmux.conf` instead of clobbering it.
Because rig *generates* the managed region it can **GUARANTEE plugin-init ordering** — the
root-cause fix for a stale-session-on-reboot bug (below). **Opt-in:** a `tmux:` block with
`enabled` not `false`; an absent block leaves tmux alone.

```yaml
tmux:
  enabled: true                 # provision the rig-managed tmux config (opt-in)
  apply: import                 # import (preferred) | block (sentinel-fenced fallback)
  conf_path: ~/.tmux.conf       # the file rig migrates/wires
  generated_dir: ~/.config/rig/tmux   # where rig writes rig.tmux.conf + the managed scripts
  resurrect:
    # do NOT list `claude` here while cc_restore is on — cc-restore owns the exact resume
    # (see "Why claude is NOT in the default @resurrect-processes" below).
    processes: [ssh, psql, mysql, sqlite3]
    capture_pane_contents: true
  continuum:
    restore: true               # @continuum-restore on
    save_interval: 15           # @continuum-save-interval (minutes)
    boot: true                  # NO-OP: rig's launchd agent owns boot (see boot.enabled); rig
                                # never emits @continuum-boot 'on' (it would install continuum's
                                # own untracked iTerm Tmux.Start.plist — the path rig replaces)
  moshi:
    enabled: false              # opt-in Moshi (iOS client) status-line tweaks — see below
  cc_restore:
    enabled: true               # per-window Claude Code resume by exact session id
  anti_sprawl:
    enabled: true               # install an attach-or-create entry (one canonical session)
    session: main               # the canonical session name
  boot:
    enabled: true               # a launchd agent that brings tmux up after a macOS reboot
  login_shell:
    enabled: true               # restored panes are LOGIN shells (so ~/.zprofile/PATH is sourced)
    shell: ""                   # "" → resolve the user's $SHELL at apply; else an absolute path
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | — (opt-in) | provision the rig-managed tmux config (`false`/absent → leave tmux alone) |
| `apply` | enum | `import` | apply mechanism: `import` (preferred) or `block` (managed-block fallback) |
| `conf_path` | path | `~/.tmux.conf` | the user's tmux config rig migrates and wires |
| `generated_dir` | path | `~/.config/rig/tmux` | where rig writes `rig.tmux.conf` + cc-save/cc-restore/tmux-attach scripts |
| `resurrect.processes` | list[str] | `[ssh, psql, mysql, sqlite3]` | `@resurrect-processes`. `claude` is **omitted by default while cc_restore is on** (cc-restore owns the exact resume); rig adds it only when cc_restore is off (fallback). List it explicitly to force it. |
| `resurrect.capture_pane_contents` | bool | `true` | `@resurrect-capture-pane-contents on` |
| `continuum.restore` | bool | `true` | `@continuum-restore on` |
| `continuum.save_interval` | int ≥ 1 | `15` | `@continuum-save-interval` (minutes) |
| `continuum.boot` | bool | `true` | **no-op** — rig always emits `@continuum-boot 'off'` (its own launchd agent owns boot; see `boot.enabled`). Kept for forward-compat. |
| `moshi.enabled` | bool | `false` | opt-in Moshi status-line tweaks, emitted **before** continuum init |
| `cc_restore.enabled` | bool | `true` | wire cc-save/cc-restore via resurrect post-save/post-restore hooks |
| `anti_sprawl.enabled` | bool | `true` | install the attach-or-create entry script |
| `anti_sprawl.session` | str | `main` | the one canonical session name |
| `boot.enabled` | bool | `true` | write a launchd agent (macOS) that runs the boot script after a reboot, and `launchctl load -w` it on apply |
| `boot.label` | str | `ai.hyperide.tmux-boot` | the launchd agent label (and plist filename stem) |
| `login_shell.enabled` | bool | `true` | set a **login-shell** `default-command` so restored panes source `~/.zprofile`/PATH (resurrect otherwise restores a non-login shell with a broken env) |
| `login_shell.shell` | str | `""` | login shell path. `""` resolves the user's `$SHELL` at apply (falling back to `/bin/zsh` then `/bin/sh`); a non-empty override **must be an absolute path** to the shell binary (a relative name or a command-with-args is rejected, so it can't silently produce a broken `default-command`) and is used verbatim. The path is **baked at generation** — NOT a tmux `${SHELL}` reference, because tmux rejects `${VAR:-default}` and would abort the whole config |

**Apply mechanism — import-preferred, managed-block fallback.**

- **`import` (preferred).** rig owns `~/.config/rig/tmux/rig.tmux.conf` (built wholesale from
  this block on every apply, idempotent) and `~/.tmux.conf` carries a **single `source-file`
  import line** appended after the user's own lines. rig rewrites only its own file and the one
  import line — every hand-written line in `~/.tmux.conf` is left untouched.
- **`block` (fallback).** rig splices the generated body between sentinel markers
  `# === rig-managed (tmux) BEGIN ===` / `# === rig-managed (tmux) END ===`, replacing **only**
  between the markers (conda-init style). Lines before and after the block are preserved.

**First apply / migration.** When the existing `~/.tmux.conf` carries rig-owned settings inline
(resurrect/continuum/tpm/Moshi), rig backs the **original** up to a UNIQUE timestamped
`~/.tmux.conf.rig-bak-<UTC>` before wiring the managed region — so every migrating apply keeps its
OWN restore point and a later apply (after a hand-edit) never loses the in-between state by
skipping a backup. The migration **neutralizes** (comments out, with a
`# rig-migrated (now in rig.tmux.conf):` marker) exactly the **rig-OWNED** init that rig now
re-emits itself in `rig.tmux.conf` (in BOTH import and block apply mode):

- the three rig-owned `@plugin` declarations — `tmux-plugins/tpm`, `…/tmux-resurrect`,
  `…/tmux-continuum`;
- every `@continuum-*` and `@resurrect-*` option (restore, boot, boot-options, save-interval,
  processes, capture-pane-contents, strategy-vim, hooks, …);
- the plugin **init** lines — `run-shell …/tmux-resurrect/resurrect.tmux`,
  `run-shell …/tmux-continuum/continuum.tmux`, and tpm's `run '…/tpm/tpm'`;
- the Moshi `status-left`/`status-right` wipe.

This is the root-cause completion for the **double-init** bug: a hand-written
`run-shell …/continuum.tmux` runs continuum-restore *before* rig's appended `source-file` sets the
login-shell `default-command`, so restored panes spawn **non-login** (`~/.zprofile` skipped); and a
live `@continuum-boot 'on'` / `@resurrect-processes '…'` fights rig's clean values. rig re-runs
these inits in the pinned order at the END of its sourced file, so the inline copies must go.

rig **owns the whole resurrect/continuum surface**, so it neutralizes *every* live `@continuum-*` /
`@resurrect-*` set directive — including options it does not itself re-emit (e.g.
`@resurrect-strategy-vim`, `@continuum-boot-options`). Those are **recoverable** from the
timestamped `.rig-bak-<UTC>` backup. To keep a value rig models, set the matching `tmux:` knob in
`rig.yaml`. An *unmodeled* option you truly need is best carried in a **separate** tmux file you
`source-file` yourself **after** rig's import (rig only neutralizes lines IN `~/.tmux.conf`/the
managed conf, never another file you source) — re-adding it as a bare line inside `~/.tmux.conf`
will be re-neutralized on the next `rig apply`, by design (rig owns that surface). The match is
anchored to a `set`/`set-option`/`set-window-option`/`setw` directive, so an
`@continuum-…`/`@resurrect-…` token appearing only inside a `status-right` **value** or a
keybinding is **not** neutralized.

**What migration NEVER touches** (no over-reach): a **third-party** `@plugin`
(`tmux-plugins/tmux-sensible`, `tmux-yank`, any non-rig plugin) — rig's tpm, run at the end of
`rig.tmux.conf`, loads it — and every **personal pref** (`set -g mouse on`, history-limit,
base-index, set-titles, `update-environment MOSHI_CLIENT`, key bindings, a real `status-right`
value, status styling that isn't continuum's, …). The migration is **idempotent**: re-applying an
already-migrated conf never double-comments and (when nothing else changed) writes nothing.
`rig status` reports drift on the **managed region only** (the generated file, the scripts, the
boot plist, the import line / managed block) — never on the user's hand-written lines.

**The root-cause ordering guarantee.** tmux-continuum's autosave timer lives in `status-right`.
A hand-written conf that ran `set -g status-right ''` (a Moshi tweak) **after**
`run-shell …/continuum.tmux` silently wiped continuum's hook → autosave died → a reboot restored
a weeks-stale session. rig's generator pins the order: plugin options → cc-restore hooks → the
**Moshi tweak (opt-in, BEFORE continuum init)** → resurrect init → **continuum init LAST** → tpm
init last-of-all. So the Moshi tweak can never wipe continuum's hook again.

**Boot + live activation (clean machine → fully working, zero manual steps).** A `rig apply`
with `boot.enabled` writes a launchd agent whose entrypoint is the generated **boot script**
(`tmux-boot.sh`), then **`launchctl load -w`**s it so it fires at login across reboots. The boot
script runs `tmux new-session -d` (NOT `tmux start-server`): a bare `start-server` starts an
**empty** server that loads neither the config nor any plugin (tmux sources the conf only on the
first session), so `@continuum-restore` never fires — `tmux ls` says "no server running" after
login. Creating a session loads `~/.tmux.conf` → the sourced `rig.tmux.conf` → continuum →
restore. The boot script is idempotent (`has-session` → exit 0), so a warm login never spawns a
duplicate. On the same apply rig also: creates `~/.tmux/resurrect` (absent → resurrect writes no
snapshot → nothing to restore); **clones** `tpm` + `tmux-resurrect` + `tmux-continuum` into
`~/.tmux/plugins` if missing (so the `@plugin` decls resolve on a clean machine); takes a first
`resurrect save` (so a reboot has something to restore); and on macOS **cleans continuum's own
stale boot** (its `osx_iterm/terminal_start_tmux.sh` Login Items + an old `Tmux.Start` launchd
agent) that would otherwise compete with rig's boot agent. Every step is idempotent and non-fatal
(an offline machine just skips the clone and retries on the next apply). Set `RIG_TMUX_DRY_RUN=1`
to write the on-disk artifacts but skip all live activation (CI / containers).

**cc-restore — per-window Claude Code resume by exact session id.** rig installs two managed
scripts and wires them via `@resurrect-hook-post-save-all` / `@resurrect-hook-post-restore-all`:

- **`cc-save.sh`** — for every pane whose **process tree** contains a `claude` process, take its
  cwd, find the **newest** session id under `~/.claude/projects/<encoded-cwd>/`, and write a
  `window/pane → cwd → session_id` map. **Detection is by the process TREE, not the command
  string:** Claude Code shows up in `pane_current_command` as its VERSION (e.g. `2.1.178`), and
  the real `claude` process is a CHILD of the pane's shell — so cc-save walks the pane's
  descendants (`ps -eo pid,ppid,args`) for a process whose **executable** (argv[0]) is `claude`.
  (Filtering on `pane_current_command == claude` matched nothing → an empty map → cc never
  resumed.) **It matches the versioned install too:** cc installs as a symlink
  `~/.local/bin/claude → …/claude/versions/<version>`, so launched by the resolved path the
  process name is the *version* (`2.1.179`), not `claude`; cc-save also matches an argv[0] under
  `…/claude/versions/`. It reads the full `args` (not `comm`) so the path is visible on **both
  macOS and Linux** (Linux `comm` is the truncated basename, with no path), and keys on argv[0]
  only so a `claude` that is merely an *argument* (`vim claude.md`, `grep …/claude/versions/`)
  never false-matches. *Accepted limitations:* an install path containing a **space**, or a
  **wrapper** launch that rewrites argv[0] (`npx claude`, `node …/cli.js`), is not detected — both
  are absent from the canonical direct-exec install this targets.
  **Encoding (verified against real on-disk dirs):** the projects-dir name is the cwd with
  **every `/` and `.` replaced by `-`** (e.g. `/Users/u/.files` → `-Users-u--files`).
- **`cc-restore.sh`** — after a reboot, for each mapped window run `claude --resume <id>` —
  **only into a fresh shell pane** (never on top of a running `claude`). A stale/missing id
  falls back to `claude --continue` (most-recent session in that cwd) so a reboot is never left
  with a dead pane.

**Why `claude` is NOT in the default `@resurrect-processes`.** When `cc_restore` is on, cc-restore
owns the resume, so rig **deliberately leaves `claude` OUT** of `@resurrect-processes`: if it were
in the list, tmux-resurrect would restart the pane as a *bare* `claude` (a new/default session)
**before** the cc-restore hook runs, and cc-restore — which only ever resumes a *fresh shell* —
would then skip that pane, leaving the wrong session. So resurrect brings the **shell** back and
cc-restore does the exact `claude --resume <id>`. (With `cc_restore: { enabled: false }`, `claude`
*is* added to `@resurrect-processes` as the best-effort fallback. You can also list `claude`
explicitly in `resurrect.processes` to force it in.)

**Known limitation — per-cwd, not strictly per-pane.** Claude Code does not expose its session id
per tmux pane, so cc-save records the **newest** session id for each pane's **cwd**. Two `claude`
panes in the **same** directory therefore map to the same session id and cc-restore resumes both
into it. Per-window exact resume holds when each claude pane is in a **distinct** cwd (the common
case).

**anti-sprawl — one canonical session.** A Moshi/iTerm reconnect that ran a bare `tmux` spawned
a **duplicate** session. rig installs `tmux-attach.sh` (attach `<session>` if it exists, else
create it). Wire it from the login shell (documented, **not** auto-wired — rig never edits the
user's shell rc): `[ -z "$TMUX" ] && exec ~/.config/rig/tmux/tmux-attach.sh`. On this machine
there is **one** canonical tmux path — the rumored second wrapper (`ln`/`.ln.conf`) does not
exist here (`/bin/ln` is coreutils; no `~/.ln.conf`), so there is nothing to reconcile against.

**boot.** rig's launchd agent is the **single** boot path; the mechanics are in the
"Boot + live activation" section above — in short: the agent's `RunAtLoad` plist runs the
generated **boot script** (`tmux-boot.sh` → `tmux new-session -d`, NOT a bare `tmux start-server`,
which would start an empty server with no conf/plugins loaded), `@continuum-restore 'on'` restores
the saved session into it, and `rig apply` **`launchctl load -w`s** the agent so it fires at login.
**rig deliberately keeps `@continuum-boot 'off'`** in the generated config: `@continuum-boot 'on'`
would make tmux-continuum install its OWN, untracked boot artifact (the iTerm-coupled
`Tmux.Start.plist` on macOS / a systemd user unit on Linux) — a second, competing boot path; rig
also **cleans** that stale artifact on macOS. So continuum handles *restore*, rig's launchd agent
handles *boot*. rig never runs `tmux source-file` against the user's **live** server (the user
reloads their config when ready); the boot agent's script is idempotent (`has-session` → exit 0,
no duplicate, no pane touched), so loading it does not disrupt an active session. The
boot-from-cold path can only be fully proven by an actual reboot.

---

## `gitignore`

Maintains a **rig-managed block** in git's **GLOBAL excludes file** (`core.excludesfile`) so
harness artifacts are ignored in **EVERY repo on the machine** — with **zero per-repo commits**
and no per-repo `rig apply`. The motivating case: Claude Code creates throwaway worktrees under
each repo's `.claude/worktrees/`; those must be gitignored everywhere, and rig owns that ignore
**globally** (one managed block, machine-wide) rather than per-repo. This is **GLOBAL config** —
it belongs in the global rig layer (`~/.config/rig/config.yaml`), wired like the git-hooks
`dispatcher` (a `git config --global` setting plus a managed file). Default **ON** — on `rig init`
AND `rig apply` rig converges the block; idempotent (a re-apply that finds it correct is a no-op).

```yaml
gitignore:
  enabled: true                 # provision the managed block (default ON; false opts out)
  entries:                      # the ignored paths inside the managed block
    - "**/.claude/worktrees/"   # default: Claude Code's throwaway worktrees (every repo)
  # excludesfile: ~/.gitignore  # rare: force a specific file instead of honoring core.excludesfile
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the managed block (set `false` to leave the global excludes file untouched) |
| `entries` | list[str] | `["**/.claude/worktrees/"]` | the paths ignored inside the managed block; an empty/absent list uses the default |
| `excludesfile` | str | *(unset)* | force a specific file; by default rig honors `core.excludesfile` (or sets it — see below) |

**Target resolution (the headline behavior).** rig decides WHICH file holds the block at apply
time, honoring the user's existing choice:

- **`core.excludesfile` is already set** (e.g. `~/.gitignore`): rig manages the block **in that
  file** and leaves the git config alone — your choice is respected, the block is not moved.
- **`core.excludesfile` is unset**: rig **sets** it to the XDG default `~/.config/git/ignore`
  **and** writes the block there. So on a **clean machine** `rig init` does everything itself
  (set the git config if absent + write/reconcile the block) — no manual `git config` step.
- **`excludesfile:` override set**: rig reconciles the block in that file and points
  `core.excludesfile` at it when git's value doesn't already match.

**`.serena/` is intentionally NOT ignored.** Serena state is **committed** shared project memory
(project memories travel with the repo), so it is never in the default entries — only throwaway
harness artifacts are.

**The managed block.** rig fences its lines with explicit markers and a fixed explanatory comment,
and touches **only** what is between them — every other line in the excludes file (the user's or
another tool's) is preserved verbatim (CRLF, trailing blanks, no-final-newline all survive):

```
# >>> rig-managed (do not edit) >>>
# Claude Code creates throwaway worktrees under each repo's .claude/worktrees/; rig ignores them globally.
**/.claude/worktrees/
# <<< rig-managed (do not edit) <<<
```

**Strict idempotency.** A re-apply is a **byte-identical no-op** when the block is already correct.
If a prior non-idempotent tool appended the block **more than once**, rig **collapses the entire
rig-managed region to one correct block** (it never duplicates, and never edits lines outside the
markers). An **unbalanced** marker pair (a begin with no end, an end before a begin) is a
`conflict` rig leaves untouched and surfaces for manual reconcile.

**Drift.** `rig status` flags the GLOBAL block as drift when it is missing, divergent, or
duplicated — **and** when `core.excludesfile` is unset and rig would set it. `rig apply`
reconciles. Shown in the **global** section of status (not the repo section).

---

## `tools`

rig's **primary purpose**: install + advertise the **personal CLI ecosystem** — `tg`, `review`,
`task`, `draw` (and more, declaratively) — at `rig apply`. For each declared tool, rig runs the
tool's **own `install.sh`** (which locates the repo, installs deps, symlinks the entry into the
managed PATH dir, and runs `<tool> install-skill` to advertise it into the agent harness). rig does
**not** reimplement any of that — the tool stays the single source of truth for how it installs, so
a tool changing its install is inherited for free.

**Default OFF (opt-in).** Unlike `tg_ctl`/`models`, an absent / empty / `enabled: false` block
provisions **nothing** — a machine opts in by listing tools under `items`. This is a **per-MACHINE**
concern (the tool ecosystem on this dev box), so the block belongs in the **GLOBAL** layer
(`~/.config/rig/config.yaml`), never a committed repo `rig.yaml`. Cross-platform (no launchd).

```yaml
tools:
  enabled: true              # opt-in; absent/false provisions nothing
  target: ~/.local/bin       # managed PATH dir each tool symlinks its bin into (default)
  items:
    tg:     { repo: ~/.files/repos/tg-cli }
    review: { repo: ~/xp/review-cli }
    task:   { repo: ~/xp/task-cli }
    draw:   { repo: ~/xp/draw-cli }
    # a bare entry defaults repo to ~/xp/<name>-cli:
    # foo: {}
```

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | `false` | opt-in master switch; `false`/absent = rig provisions no tools at all |
| `target` | str | `~/.local/bin` | managed PATH dir each tool's `install.sh` symlinks its entry into |
| `items.<name>` | map | — | one tool, keyed by its command name (the skill-blurb stem) |
| `items.<name>.enabled` | bool | `true` | `false` = skip this one tool |
| `items.<name>.repo` | str | `~/xp/<name>-cli` | the tool's checkout dir (holds its `install.sh`) |
| `items.<name>.bin_dir` | str | `target` | override the managed PATH dir for this one tool |

**Idempotent + safe.** A tool already installed — its bin resolves (the managed symlink **or**
anywhere on PATH, so a Homebrew `review` counts) **and** its skill blurb is advertised — is a
**no-op** (`skipped`); rig does **not** re-run `install.sh`. rig never deletes a user's existing
symlink. A tool that resolves but isn't advertised is (re-)installed only to wire up its skill.

**Drift.** `rig status` flags (in the **GLOBAL** section) a declared tool whose bin doesn't resolve
(**missing** — apply runs its `install.sh`) or that resolves but isn't advertised (**modified** —
apply re-runs `install-skill`). `rig apply` reconciles.

**Dry-run seam.** `RIG_TOOLS_DRY_RUN=1` reports what WOULD install without running any `install.sh`
(used by the e2e suite so tests never shell out to a real tool installer).

---

## `tg_ctl`

rig provisions the **tg-ctl inbound control daemon** (tg-cli's long-poll / inject-into-tmux /
voice→text daemon, run as `tg-ctl run`) as a **macOS boot LaunchAgent** so it auto-starts at
login/boot — exactly like the tmux boot service. **Default ON** (an absent or empty `tg_ctl:`
block still provisions it, so `rig init` on a clean machine sets it up with no config). This is a
**per-MACHINE** concern (one inbound daemon per machine), so the block belongs in the **GLOBAL**
layer (`~/.config/rig/config.yaml`) — never a committed repo `rig.yaml`. **macOS-only** (launchd);
off darwin it is a no-op.

```yaml
tg_ctl:
  enabled: true                       # provision the tg-ctl LaunchAgent (default true; false = off)
  boot: true                          # write + load the boot agent (default true)
  # everything below is auto-discovered per-machine — override only if non-standard:
  label: ai.hyperide.tg-ctl           # launchd Label / plist filename stem (advanced)
  bun_path: ~/.bun/bin/bun            # the bun binary (default: `which bun` → ~/.bun fallback)
  tg_ctl_path: ~/.files/bin/tg-ctl    # the tg-ctl Bun script
  config_dir: ~/.config/tg-cli        # tg-cli config + launchd logs (default honors $TG_CTL_CONFIG_DIR)
```

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | `true` | `false` = **don't touch tg-ctl at all** — rig emits no action, so it neither provisions NOR cleans up NOR reports drift (a hands-off opt-out). Use `boot: false` instead to keep rig tracking a leftover plist. |
| `boot` | bool | `true` | `false` = provisioned-but-no-boot: rig writes/loads nothing, but a leftover plist (or the stale predecessor) IS still surfaced as drift so you see the orphan |
| `label` | str | `ai.hyperide.tg-ctl` | launchd Label / plist filename stem (one identity for install/drift/remove) |
| `bun_path` | str | discovered | the bun binary; default `which bun`, else `~/.bun/bin/bun` |
| `tg_ctl_path` | str | `~/.files/bin/tg-ctl` | the tg-ctl Bun script launchd runs (`bun <path> run`) |
| `config_dir` | str | `~/.config/tg-cli` | tg-cli config dir; the launchd out/err logs land here (honors `$TG_CTL_CONFIG_DIR`) |

> **`enabled: false` vs `boot: false`.** `enabled: false` is a complete opt-out — rig stops
> emitting the action, so a previously-installed plist is NOT cleaned up or flagged (rig is
> hands-off). If you want rig to keep watching for / flagging a leftover plist while not running
> the daemon, use `boot: false` (the action still runs; drift surfaces the orphan).

**What rig writes.** `~/Library/LaunchAgents/ai.hyperide.tg-ctl.plist` — a `RunAtLoad` +
`KeepAlive` agent that runs `bun ~/.files/bin/tg-ctl run` with a login PATH and the tg-cli config
dir's logs. The plist is **byte-exact** to a hand-created working file, so `rig apply` against an
already-correct live plist is a true **no-op** (`skipped`), never a spurious rewrite.

**(Re)load mechanism.** Unlike tmux-boot (which only writes the plist), rig **(re)loads** the agent
via `launchctl bootout`/`bootstrap` in the per-user `gui/<uid>` domain, so a clean `rig init` starts
the daemon without a reboot, and a changed plist is picked up on the next `apply`.

**Stale-predecessor teardown.** If the dead predecessor service
`~/Library/LaunchAgents/com.ultra.codex-tg-bot.plist` exists, `rig apply` **boots it out**, backs it
up (timestamped), and removes it.

**Drift.** `rig status` flags (in the **GLOBAL** section) when the agent is missing, divergent, or
written-but-not-loaded; a leftover plist when `boot: false`, or the stale predecessor, surfaces as a
disk→config **extra**. `rig apply` reconciles.

**Dry-run seam.** `RIG_TG_CTL_DRY_RUN=1` writes the managed plist into the configured (HOME-isolated)
path but skips every live/destructive mutation — the gui-domain `launchctl bootstrap`/`bootout` AND
the stale-predecessor teardown (its bootout and the on-disk backup+remove of its plist) — so
tests/smoke never touch the real launchd domain or delete the predecessor file.

---

## Editing this config — `rig setup` (wizard) and `rig config get|set`

`rig setup` is the **interactive configuration wizard**. In a terminal it (1) SHOWS what is
enabled across every reconciled area above — read from the cascaded config (global + repo),
absent keys shown at their documented default — (2) lets you CHANGE any option, with an inline
HINT (the why/how) next to each, and (3) APPLIES (`rig apply`) on the spot. Run from a non-TTY
(piped / redirected) it prints USAGE for `init`/`apply`/`config get|set` rather than a
half-wizard you can't answer.

The wizard's option list + hints come from the in-code registry `riglib/schema.py` (the curated
toggle subset). The **complete** schema — every block and key — lives in `riglib/config_schema.py`
and is what `schema/rig.schema.json` is generated from; `schema.json_schema()` delegates to it, so
there is one emitter and the schema is never maintained in parallel (see "The JSON Schema" above).

**Which file a change is written to.** Each option is routed to its **owning layer**:

- **REPO** options (`skills`, `agent_hooks`, `git_hooks`, `ci`, `mcp`, `harness`, `models`,
  `github`, `agents_md`, `linters`, `project_tools`) are written to the repo's `./rig.yaml` — the
  values the default scaffold commits.
- **GLOBAL-only** options (`gitignore`, `tg_ctl`, `tmux`) are machine-wide blocks the scaffold
  never writes into a committed repo file; the wizard writes them to `~/.config/rig/config.yaml`
  only — it never lands a global-only block in a committed repo `rig.yaml`.

`rig config get|set <dot.path>` is the **headless counterpart** (and what non-interactive
`rig setup` points at). It is a dot-path editor: it reads/edits ONE nested key by dot-notation
into the tree above (`harness.auto_mode`, `ci.items.secret-scan.tier`, `defaults.on_conflict`),
then **reconciles** (runs the same plan + apply engine as `rig apply`). `--global` targets
`~/.config/rig/config.yaml` (XDG-aware) instead of `./rig.yaml`.

```bash
rig config get harness.auto_mode                 # read one key (from ./rig.yaml, NOT the cascade)
rig config get harness.auto_mode --json          # machine-readable (JSON value)
rig config get harness                            # a subtree prints as YAML
rig config get defaults.on_conflict --global     # read the global config instead

rig config set harness.auto_mode false           # write, then RECONCILE (rig apply engine)
rig config set ci.items.secret-scan.tier warn    # creates intermediate keys as needed
rig config set defaults.on_conflict overwrite --global   # edit the global config
rig config set harness.auto_mode false --no-apply        # write only, print the plan, skip apply
```

- **`get`** reads the single target file (NOT the cascade): `./rig.yaml`, or the global file
  with `--global`. A missing file or absent path exits non-zero (fail-closed). `--json` emits
  the raw JSON value; a mapping/list subtree prints as YAML; bools print as `true`/`false`.
  Errors go to stderr, so `rig config get k --json | jq` keeps a clean stdout even when the key
  is missing.
- **`set`** coerces the value conservatively — `true`/`false` → bool, a plain integer → int,
  `1.5` → float, `null`/`none`/`~` → null, everything else (including `09`, `1e3`, `nan`, `inf`,
  `1_000`, and Unicode digits) stays a string. Quote-wrap to force a literal string
  (`rig config set k '"true"'` stores the string `"true"`). It creates intermediate mappings as
  needed, then guards the write with two **pre-apply** gates: the schema (`config.validate` —
  enums/types) and the catalog-backed plan build (`rig apply`'s engine — an unknown item a
  category references, a bad `agent_tools_source`, or any otherwise-unbuildable config). If
  **either** gate rejects the edit, the target file is rolled back to its prior contents and the
  command exits non-zero.
- A value that **starts with `-`** (and is not a negative number) needs the `--` separator so
  argparse doesn't read it as a flag: `rig config set k -- -weird`. **Dot paths cannot address a
  key that itself contains a dot** — `<dot.path>` always splits on `.`; every real catalog id is
  dash-cased (`secret-scan`), so this is a non-issue in practice.
- **A repo-local `set` requires an existing `./rig.yaml`.** It edits a committed config; it does
  not bootstrap one — run `rig init` (or `rig export -o rig.yaml`) first, so built-in defaults
  never reconcile onto disk without a committed source of truth (the same guard `rig apply`
  has). `--global` may create the machine-wide `~/.config/rig/config.yaml` if it is absent.
- **`set` rewrites the whole file** through rig's serializer, so it normalizes formatting and
  **drops comments** — the value is the source of truth, not the surrounding YAML prose. It is
  also **repo-scoped**: even a `--global` edit resolves and reconciles the current repo, so run
  it from inside one (use `--no-apply` to skip the reconcile, not the repo resolution). Setting
  the removed `scope` key is refused (the cascade is by location, not a flag).

## Validation

`apply`/`status`/`init` validate before touching disk and **fail closed**. **Every block is
strict**: an unknown FIXED key in *any* block — `defaults`, `skills`, `agent_hooks`, `git_hooks`
(+ `dispatcher`), `ci`, `mcp`, `harness` (+ `hook_bridge`), `permissions`, `models` (+ `schedule`),
`agents_md`, `github` (+ `ruleset` / `merge` / `ghas` / `actions` / `browser`), `tmux` (+ every
sub-block), `gitignore`, `tg_ctl`, `project_tools` (+ `haft` / `workflow` / `serena` / `sverklo`) — is
rejected with the schema path of the offender, not silently ignored. The deliberate pass-through
maps are top-level `scripts:` / `dev:` and the catalog-keyed `items:` / `fragments:` maps described
above. Bad-value rejections
include: unsupported `version`, invalid `on_conflict` / ci `tier` / agent-hook `on_error`, an
unknown or reserved `harness.kind`, a non-bool `harness.auto_mode`, a non-mapping
`harness.hook_bridge` / non-bool `hook_bridge.enabled` / non-string `hook_bridge.python`, a
non-bool `git_hooks.dispatcher` bool knob, a malformed/out-of-range `models.schedule.time`, a
non-bool `agents_md.enabled`/`symlink`, a non-bool `github.ruleset` boolean knob, a
`github.ruleset.required_reviews` that is not an int ≥ 0, a `github.ruleset.required_status_checks`
that is not a list of strings, a bad `tmux.apply` enum, a `tmux.resurrect.processes` that is not a
list of strings, a `tmux.continuum.save_interval` that is not an int ≥ 1, a non-bool `tmux` bool
knob, a non-string `gitignore.excludesfile` / a `gitignore.entries` that is not a list of strings
or that contains a rig-managed marker line, a non-bool `tg_ctl.enabled`/`boot`, a non-string
`tg_ctl.label`/`bun_path`/`tg_ctl_path`/`config_dir`, a bad
`project_tools.haft.workflow.mode`, a non-list/string item in `project_tools.serena.languages` or
`project_tools.serena.ignored_paths`, and an `agent_tools_source` that is not an agent-tools
checkout. Every rejection is the **3-part error** (what / why + schema path / fix) and
exits `2`. `--dry-run` prints the resolved plan and exits 0 without writing.

`rig config set` is **fail-closed** with full rollback: a malformed/non-mapping existing target
file, a non-mapping intermediate on the dot path, or a schema-rejected resulting doc
(`config.validate`) is reported **before any write**; a write IO error or a catalog-backed plan
failure (the second gate — an unknown item, a bad `agent_tools_source`) rolls the file back to
its prior contents (a freshly-created file, and any dir created for it, are removed). Because every
block now enforces its key set, a `set` against a config that already carries a typo'd key
surfaces it (the whole edited tree is validated) — the same strictness a hand-edited `rig.yaml`
gets on `apply`. After it passes, `set` reconciles (the same plan + apply engine as `rig apply`);
`--no-apply` writes the key and prints the plan without converging.
