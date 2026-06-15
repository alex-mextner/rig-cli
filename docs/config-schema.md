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

skills: { ... }
agent_hooks: { ... }
git_hooks: { ... }
ci: { ... }
mcp: { ... }
harness: { ... }              # agent harness auto/permission provisioning (auto-mode)
models: { ... }               # daily model-freshness checker schedule (launchd/crontab cron)
agents_md: { ... }            # AGENTS.md (canonical) + CLAUDE.md (symlink), default ON
github: { ... }               # GitHub repo branch ruleset via gh api, default ON (no-op without a github remote)
```

If `agent_tools_source` is omitted, rig resolves it from `$RIG_AGENT_TOOLS_SOURCE`, then
the default candidates (`~/xp/agent-tools`, `~/work/agent-tools`, `~/agent-tools`).

## Resolution rules (so the file stays terse)

- A category sets `enabled: false` to disable the whole category in one line.
- A category may set `all: true` and list exceptions in `disable: [...]`, **or** `all:
  false` and list only what is enabled in `enable: [...]`. Opt-out and opt-in are both
  expressible.
- Per-item overrides live under `items:` keyed by item name; absent items inherit the
  `all`/`enable`/`disable` decision.
- Targets resolve item → category `target` → `defaults.<x>_target` → built-in default.

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
| `harness_skill_dir` | path | per-harness default | where the harness discovers skills (claude-code: `~/.claude/skills`) |
| `universal.all` | bool | `true` | enable all universal skills (opt-out) |
| `universal.disable` / `universal.enable` | list[str] | `[]` | deltas on `all` |
| `by_type.enable` | list[str] | the detected project type | which `by-type/<kind>` bundles to install whole |
| `by_type.items.<by-type/kind/name>.enabled` | bool | inherited | per-skill override |

If `by_type.enable` is empty and the detected project type is known, that type's bundle is
auto-pulled.

### Harness skill discovery (why `harness_link`)

The agent harness lists/loads Skill-tool skills from its **own** dir, not from `target`.
For **claude-code** that is `~/.claude/skills` (its userSettings skill dir; symlinks there
resolve to the real skill). A skill copied into `~/.agents/skills` (the default `target`) is
therefore invisible to the harness until it is also present in the discovery dir. With
`harness_link: true` (the default), `rig apply` maintains an idempotent symlink
`<harness_skill_dir>/<skill> → <target>/<skill>` for every enabled skill:

- an existing **correct** symlink is a no-op;
- a symlink to the **wrong** destination is re-pointed;
- a **real** (non-symlink) dir/file already at the path is **left untouched** — some skills
  are hand-authored real dirs (e.g. `h-reason`, `debate-swarm`), and rig must not clobber
  them. `rig status` reports a missing/wrong link as drift; a real dir is not flagged.

The discovery dir is keyed by the harness `kind` (defaulting to claude-code, or following
`harness.kind` when a `harness:` block pins one). Set `harness_link: false` to opt out, or
`harness_skill_dir` to point at a non-default location.

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
  target_kind: claude-code          # claude-code | generic (logical point → harness event)
  all: true
  items:
    block-no-verify:     { enabled: true,  on_error: closed }
    enforce-timeout-on-bash: { enabled: true, on_error: open }
```

| Per-item key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | install this hook |
| `on_error` | `open`/`closed` | descriptor's value | fail policy (security = closed) |

The install action always writes an **absolute** `cmd` (rewriting the
`/ABSOLUTE/PATH/TO/...` placeholder to the script's real path in the agent-tools checkout),
per the `agents-hooks/v1` contract.

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
    review:
      enabled: true
      command: "review --mcp"      # the launch command merged into the harness MCP config
    code-search:
      enabled: true
      server: serena
      command: ""                  # no command → nothing to register (reported, not an error)
```

The install action merges an MCP entry **idempotently by server name** into
`<target>/mcp.json` (or the target file if it ends in `.json`) and never overwrites an
existing differing entry unless `defaults.on_conflict: overwrite`.

---

## `harness`

Provisions the **agent harness's auto/permission mode** as part of the reconciler. With a
`harness:` block, `rig apply` writes the harness's permission setting so **auto-mode**
(the agent runs autonomously, auto-accepting tool calls, with minimum babysitting) is part
of the reproducible config — not a manual per-machine toggle. **Recommended on by default**:
auto-mode is safe because the agent-hook guards (`block-secrets-write`, `block-no-verify`,
`enforce-timeout-on-bash`, `block-raw-process-env`, `block-raw-pr-merge`) are installed in
the same apply and catch the dangerous tool calls before the side effect.

```yaml
harness:
  enabled: true
  kind: claude-code            # claude-code (implemented) | opencode (documented, reserved)
  auto_mode: true              # true → auto-accept tool calls; false → interactive prompts
  # mode: bypassPermissions    # optional: pin the exact mode value (overrides the auto_mode map)
  # settings_path: .claude/settings.json   # where to write (repo-local default; committed)
  hook_bridge:                 # wire the agents-hooks/v1 → CC dispatcher (default ON)
    enabled: true              # set false to skip wiring the dispatcher into settings.json
    # python: python3          # optional: the interpreter the dispatcher runs under
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the harness setting (set `false` to leave the harness config untouched) |
| `kind` | `claude-code` | `claude-code` | which harness to write. `opencode` is documented-but-reserved → fails closed until implemented |
| `auto_mode` | bool | `false` (scaffold writes `true`) | `true` = auto-accept; maps to the harness's non-interactive permission value |
| `mode` | str | — | pin the exact permission value (e.g. `acceptEdits`), overriding the `auto_mode` mapping |
| `settings_path` | path | `.claude/settings.json` | the settings file to merge into (repo-relative default keeps it committed/reproducible) |
| `hook_bridge.enabled` | bool | `true` | wire the `cc_hook_bridge` dispatcher into `settings.json` so installed agent-hooks actually fire (claude-code only) |
| `hook_bridge.python` | str | `python3` | the Python interpreter the dispatcher command runs under |

**What gets written.** For `kind: claude-code`, rig merges `permissions.defaultMode` into
the settings JSON — `auto_mode: true` → `bypassPermissions` (auto-accepts every tool call),
`auto_mode: false` → `default` (interactive prompts). Only that one key is touched; every
other setting in the file is preserved. The write is **idempotent** (a re-apply with the
same value is a no-op) and **backup-noted** (a differing prior value is backed up per
`defaults.on_conflict` before converging). `rig status` reports drift if the on-disk value
no longer matches the config.

**opencode equivalent (documented, not yet written by rig).** opencode expresses the same
intent through a `permission` block in its `opencode.json` — e.g.
`"permission": { "edit": "allow", "bash": "allow" }` for auto-accept, vs `"ask"` for
interactive. A config with `kind: opencode` is rejected with a clear "not implemented yet"
message rather than silently doing nothing, so you are never misled into thinking rig wrote
a setting it didn't.

**The hook bridge (`hook_bridge`).** Claude Code only runs hooks declared in
`settings.json` (`PreToolUse`/`Stop`) — it never reads the `~/.claude/hooks/*.json`
`agents-hooks/v1` descriptors `agent_hooks` installs. Without a bridge, **every installed
agent-hook is inert in CC** (agent-tools#18) and the "auto-mode is safe because the guards
intercept" claim above is false. So when a `claude-code` harness block is present (and
`agent_hooks` is enabled), `rig apply` also registers the `cc_hook_bridge` dispatcher
(shipped in `agent-tools/lib/cc_hook_bridge`) into the same `settings.json`:
`PreToolUse` (matchers `Bash` and `Edit|Write|MultiEdit|NotebookEdit`) and `Stop`, each
running `PYTHONPATH=<agent-tools>/lib python3 -m cc_hook_bridge <Event>`. The dispatcher
runs the matching descriptors and translates their exit-10 BLOCK into CC's
`permissionDecision: "deny"` / `decision: "block"`. The merge is **additive and
idempotent** — your other hooks (rtk-rewrite, tg-ctl, …) are preserved; a re-apply is a
no-op; a drifted managed command (e.g. the checkout path moved) is rewritten in place.
`rig status` reports the bridge as missing drift if a managed hook is absent.

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
    block_force_push: true        # non_fast_forward rule
    restrict_deletion: true       # deletion rule
    require_linear_history: false # required_linear_history rule
    require_signatures: false     # required_signatures rule
    required_status_checks: []    # check/context names to require; empty = no rule
    admin_bypass: true            # add the repo Admin role to bypass_actors
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | provision the ruleset (set `false` to leave the repo's rulesets untouched) |
| `name` | str | `rig-managed` | the ruleset rig owns/reconciles (rig only ever touches a ruleset with this name) |
| `require_pull_request` | bool | `true` | emit the `pull_request` rule (require a PR to merge to the default branch) |
| `required_reviews` | int ≥ 0 | `0` | `required_approving_review_count` on the `pull_request` rule |
| `block_force_push` | bool | `true` | emit the `non_fast_forward` rule (block force-push) |
| `restrict_deletion` | bool | `true` | emit the `deletion` rule (block deleting the branch) |
| `require_linear_history` | bool | `false` | emit the `required_linear_history` rule |
| `require_signatures` | bool | `false` | emit the `required_signatures` rule |
| `required_status_checks` | list[str] | `[]` | contexts for the `required_status_checks` rule; **empty emits no rule** (never a no-op rule) |
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

---

## Validation

`apply`/`status`/`init` validate before touching disk and **fail closed** on:
unknown top-level keys, unsupported `version`, invalid `on_conflict` / ci `tier` /
agent-hook `on_error`, an unknown or reserved `harness.kind`, a non-bool `harness.auto_mode`,
a non-mapping `harness.hook_bridge` / non-bool `hook_bridge.enabled` / non-string `hook_bridge.python`,
a malformed/out-of-range `models.schedule.time` or unknown `models` key, a non-bool
`agents_md.enabled`/`agents_md.symlink` or unknown `agents_md` key, an unknown
`github`/`github.ruleset` key, a non-bool `github.ruleset` boolean knob, a
`github.ruleset.required_reviews` that is not an int ≥ 0, a `github.ruleset.required_status_checks`
that is not a list of strings, and an `agent_tools_source` that is not an agent-tools checkout.
`--dry-run` prints the resolved plan and exits 0 without writing.
