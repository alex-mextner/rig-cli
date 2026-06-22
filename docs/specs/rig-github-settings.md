# Spec: rig `github:` settings provisioning

Status: design-as-built (shipped on `main`, ROADMAP §5 / rig-cli#5). This document is the
authoritative spec for the `github:` block; it records the intended behavior, the secure-default
contract, and the remaining open gaps. Code that disagrees with this spec is the bug.

## 1. Problem

A fresh repo created from a committed `rig.yaml` has no branch protection, no GHAS scanners, an
arbitrary merge-button policy, and a write-scoped `GITHUB_TOKEN`. The CI gates rig provisions
(`dependency-review`, secret-scanning, etc.) skip or fail until the matching repo settings are
turned on. Wave-1 rollout enabled these by hand (`gh api PUT .../vulnerability-alerts`, …). rig
must reconcile GitHub repo settings declaratively and idempotently, the same way it reconciles
skills, hooks, CI, and linters — so `rig init`/`rig apply` lands a secure, mergeable repo in one
pass and `rig status` reports drift.

## 2. Scope & shape

`github` is a **separate apply step targeting the REMOTE**, NOT a catalog category. The catalog
scans an agent-tools checkout into filesystem-carrier Items; GitHub settings have no on-disk
carrier — they live on github.com. So `github` behaves like `harness`/`tmux`/`permissions`:
config-driven Actions with no Item backing them, classified against LIVE API state.

Five sub-blocks, each its own pure stdlib core module + a live-API classifier/handler in
`actions/runner.py`. Two backends:

| Sub-block        | Backend       | Core module           | Endpoints (github.com) |
|------------------|---------------|-----------------------|------------------------|
| `github.ruleset` | `gh api`      | `github_ruleset.py`   | `GET/POST /repos/{o}/{r}/rulesets`, `PUT .../rulesets/{id}` (modern rulesets — NOT legacy `/branches/{b}/protection`) |
| `github.merge`   | `gh api`      | `github_merge.py`     | `PATCH /repos/{o}/{r}` (merge fields only) |
| `github.actions` | `gh api`      | `github_actions.py`   | `PUT .../actions/permissions`, `PUT .../actions/permissions/workflow` |
| `github.ghas`    | `gh api`      | `github_ghas.py`      | `PATCH /repos` (`security_and_analysis`), `PUT/DELETE .../vulnerability-alerts`, `PUT/DELETE .../automated-security-fixes`, `PATCH .../code-scanning/default-setup` |
| `github.browser` | agent-browser | `github_browser.py`   | drives the settings UI (no REST endpoint) |

Only `github.com` is managed. A non-github or GitHub-Enterprise-host remote is a clean
**no-op** (`skipped`), never an error.

## 3. Architecture invariants

- **Pure core / live seam split.** Each module is side-effect-free, stdlib-only:
  `build_*_body` + `normalize_*` + a `*_state(action)` classifier. `plan.py` and `state.py`
  import these at module top (no cycle: `runner` imports `Action` from `plan`, so `plan` cannot
  import `runner`). All side effects funnel through one seam — `_gh_api(args, input_text)` in
  `runner.py` and `_agent_browser(args)` for the browser backend — which tests monkeypatch, so
  no test ever spawns `gh` or hits the network.
- **One classifier, two consumers.** `rig apply` and `rig status` call the SAME
  `github_*_state` function, so they can never disagree on "in sync". States:
  `create` / `update` / `ok` / `no_remote` / `gh_error`.
- **Idempotency = read → normalize both sides → diff → mutate only on difference.** `normalize_*`
  sorts rules/checks, reduces bypass actors to identity tuples, reads absent fields as their
  off value, so a semantic match reads as in-sync, not churn.
- **Plan build order is load-bearing** (`plan.py`): `_build_github_actions` runs BEFORE
  `_build_github_ghas` because CodeQL default-setup requires Actions enabled (GitHub rejects it
  with "Actions must be enabled for default setup" otherwise). `runner.run_plan` iterates
  `plan.actions` in build order with no sort, so plan order == execution order. A fresh repo
  converges in ONE apply, not two.

## 4. Per-sub-block contract

### 4.1 `github.ruleset` (branch protection)
Modern rulesets API. Body: `{name, target:"branch", enforcement:"active",
conditions.ref_name.include:["~DEFAULT_BRANCH"], bypass_actors:[…], rules:[…]}`. Targets the
default branch via `~DEFAULT_BRANCH` so it follows a branch rename. Rules emitted per knob:
`pull_request` (+ `required_approving_review_count`), `non_fast_forward`, `deletion`,
`required_linear_history`, `required_signatures`, `required_status_checks` (emitted ONLY when
the context list is non-empty).

- **Footgun guard (structural).** The rule assembly is INCAPABLE of emitting the `update`
  ("Restrict updates") rule or an empty-environment `required_deployments` rule — both lock out
  every merge (`Cannot update this protected ref`). There is no knob and no code path. With
  `admin_bypass` on (default), `actor_id:5 / RepositoryRole / always` keeps admins able to merge.
- **Required-checks auto-default.** When `required_status_checks` is omitted, rig sets it to the
  merge-gating CI gates this repo ACTUALLY provisions (`PR Checklist`, `review-threads`), but
  only for gates that are enabled and written. A required check whose workflow is absent would
  wedge every PR (lockout guard). An explicit list (incl. `[]`) wins verbatim; an empty result
  emits no rule.
- **Inheritance guard.** List uses `includes_parents=false` + `--paginate`, and the managed
  ruleset is found by `name` + `target=="branch"` + `source_type=="Repository"` — an inherited
  ORG ruleset is never mistaken for the repo's.

### 4.2 `github.merge` (merge-button policy)
`PATCH /repos` carrying ONLY the six merge fields — never a `name`/`visibility` key, so a PATCH
can never rename or expose the repo. Squash-only is ENFORCED (all three model flags managed:
squash ON, merge_commit + rebase OFF), not just "squash also allowed". No `create` state — a
repo always has merge settings. Purely repo-level: an org cannot override it.

### 4.3 `github.actions` (Actions permissions)
Two endpoints. `allowed_actions` is omitted from the permissions body when `actions_enabled:false`
(the API rejects them together). When Actions is disabled, the classifier AND apply SKIP the
workflow-permissions endpoint (GitHub rejects it with Actions off) — else it reads as perpetual
drift and never converges. Secure default: token READ-only, workflows may not approve PRs.

### 4.4 `github.ghas` (Advanced Security)
Four endpoint families (see table). **No `dependency_graph` knob** — github.com cloud has no
separately-togglable API for it (always-on for public; governed by vuln-alerts/Dependabot). A
knob no path could honor would be a config lie. CodeQL is reconciled BOTH directions
(`configured` ↔ `not-configured`).
- **Capability degrade splits plan-limit from real failure.** `_looks_like_ghas_unlicensed`
  matches SPECIFIC plan wording ("advanced security", "not included in", "upgrade your plan",
  "not available for") — a bare 403/422 is NOT enough (that could be a no-admin token or a real
  bug). Plan-limit → loud DEGRADE; genuine auth/permission failure → hard ERROR (so
  `status != error` automation never reads a false green). One unreadable scanner is recorded in
  `info["unverifiable"]`, forces `update`, but the FREE features (dep-graph, vuln-alerts,
  Dependabot) still apply — never masked by one unlicensed scanner.

### 4.5 `github.browser` (API-unreachable toggles)
A first-class second backend invoked INSIDE apply, not a manual "go click this" step. Pure plan
in `build_command_plan` → list of `agent-browser` argv lists (`["open", <url>]`, then per toggle
`["find","role","switch",<check|uncheck>,"--name",<label>]`). Accessibility-role selectors
(`--name` is an OPTION, not a positional — a positional name parses as the action and the toggle
never flips), so a cosmetic redesign doesn't silently mis-target. Planned default-ON so
`rig status` lists it, but the ACTION is GATED OFF unless `RIG_GH_BROWSER=1` (a real browser is
heavier/slower than gh api). A missing control degrades LOUDLY ("could not find the setting"),
never a blind click. Reuses the user's existing logged-in agent-browser session — rig never
touches GitHub credentials; `_browser_on_login_page` (a `get url` probe) is the per-page auth
verification.

## 5. Auth gate (CTO #4136.1 — ASK and WAIT, never silently fail)
Every `github.*` action runs the shared gate (`github_auth.ensure_gh_auth` /
`ensure_browser_auth`) BEFORE the first live read — not just before mutation, because the read
itself fails without a token, which would make the notify-wait path dead code in exactly the
case it exists for. If unauthenticated, rig NOTIFIES via `tg` (tag `problem`, the exact
`gh auth login -h github.com -s repo` command) and POLLS:
- `RIG_GH_AUTH_WAIT` — max seconds to block. Unset/`0` (default) → do not block (unattended CI
  never hangs); a positive value (e.g. `1800`) opts into interactive ask-and-wait.
- `RIG_GH_AUTH_POLL` — re-probe interval (default `5`s).
- Per-process dedup (`_TIMED_OUT_KINDS`) — ~5 actions never spam 5 phone pushes / 5× the budget.
- Under `RIG_GH_DRY_RUN=1` the gate is SKIPPED (no mutation → no auth → no hang). Note: dry-run
  still does the read-only GET to classify, so a dry-run with NO token on a github repo reports a
  read error, not a clean preview; a fully no-op offline preview only happens on a no-remote repo.

**Scope adequacy is enforced by the 403, not the gate.** A token present but lacking admin scope
passes the gate (`gh auth status` is 0) then gets HTTP 403 on the call → `gh_error` → loud
`error` (apply) / "could not verify" drift (status).

## 6. Drift / `rig status`
`drift.py` dispatches each `provision_github_*` to `_check_github_*` over the shared classifier:
`create` → `missing`; `update` → `modified`; `ok`/`no_remote` → no item; `gh_error` → a VISIBLE
`modified` item worded "could not verify … status unknown, not confirmed in sync" (the honesty
guarantee — never green while rig couldn't check). GHAS folds per-scanner unverifiability into the
same `modified` item. Browser drift is a deliberate `pass` (no cheap UI read-back; see Gap 4).
github rows render under the **REPO** heading (declared by the committed `rig.yaml`).

## 7. Open gaps (tracked, not blockers)
1. **Org-policy override is not first-class.** An org/enterprise policy can CAP `allowed_actions`,
   ENFORCE GHAS scanners org-wide, or ADD stricter rules on top of the repo ruleset. Today this
   surfaces only as a generic `gh_error`/degrade. There is no `org_enforced`/`overridden`
   classification state, so `rig status` can't say "capped/enforced by org policy" vs a real
   permission error. (Merge policy correctly needs none — repo-level only.)
2. **`allowed_actions:"selected"` has no companion allow-list.** The enum value is accepted but
   there is no `PUT .../actions/permissions/selected-actions` payload
   (`github_owned_allowed`/`verified_allowed`/`patterns_allowed`). Selecting `"selected"` sets
   the mode with an empty/unmanaged list. Either drop `selected` or add the sub-payload.
3. **Browser table mis-classified.** `discussions` and `projects` are BOTH API-exposed
   (`PATCH /repos` `has_discussions`/`has_projects`) → they belong in a gh-api backend, not the
   browser, and being browser-gated-off-by-default they silently never get set on a normal apply.
   The only genuinely API-less candidate with guardrail value is the Sponsor-button toggle, and
   even that is better solved by committing `.github/FUNDING.yml`. Honest v1 table is near-empty;
   keep the browser MECHANISM ready for the first true API-less toggle GitHub ships. Stale
   docstrings (delete-head-branch, `allow_forking` claimed UI-only) must be corrected.
4. **Browser idempotency + drift.** `build_command_plan` emits an unconditional check/uncheck
   each run (DOM-idempotent, but drift-unobservable and never reports "in sync"). Add a pure
   `build_read_plan` (`find role switch --name … --json` → compare `aria-checked`), skip the
   write on match, and replace the `drift.py` `pass` with a real read-only probe behind
   `RIG_GH_BROWSER`. On the first `find role` failure, capture `snapshot` + `screenshot` into the
   rig state dir and surface the artifact path in the ActionResult (visual-proof discipline,
   best-effort/non-fatal).
5. **Minor honesty/UX.** `no_remote` is wholly silent in status (consider an informational line);
   an unauthenticated `rig status` makes 5+ failing 30s `gh api` calls with no short-circuit; no
   top-level `github.enabled` kill switch (opt-out is per sub-block); multi-branch ruleset
   protection is out of scope (YAGNI).

## 8. Testing
All seams (`_gh_api`, `_agent_browser`) are monkeypatched — no test mutates a real repo or hits
the network. Coverage lives in `tests/test_github_settings.py` and `tests/test_github_ruleset.py`:
per-category deterministic body/plan, normalize idempotency, classifier states, default-ON when
the block is absent, capability degrade vs hard error, auth-gate notify/wait/dedup, browser
gated-off-by-default + per-step loud degrade + login-probe path-segment correctness. The published
`schema/rig.schema.json` is generated from `config_schema.py` and a sync test asserts they match.

## Secure-default `github:` block (what a fresh `rig init` lands)

```yaml
# What a fresh `rig init` lands when `github:` is ABSENT from rig.yaml.
# Every sub-block is default-ON even with the whole block omitted: plan builders treat a
# missing/None block as {} and emit each Action with full secure defaults. Opt out per
# sub-block with `enabled: false` (there is no top-level github.enabled kill switch).
github:
  ruleset:                          # branch protection (modern rulesets, default branch)
    enabled: true
    name: rig-managed               # rig only ever touches a ruleset with this name
    require_pull_request: true      # require a PR to merge to the default branch
    required_reviews: 0             # require-PR but 0 approvals (solo merges keep working)
    block_force_push: true          # non_fast_forward rule
    restrict_deletion: true         # deletion rule
    require_linear_history: false
    require_signatures: false
    # required_status_checks: omitted -> auto = the merge-gating CI gates this repo actually
    #   provisions (PR Checklist + review-threads), only for gates enabled & written.
    admin_bypass: true              # repo Admin role (actor_id 5) in bypass_actors -> never
                                    # locks admins out; the `update` rule is NEVER emitted.
  merge:                            # merge-button policy: squash-ONLY, linear, auto-clean
    enabled: true
    squash_merge: true
    merge_commit: false
    rebase_merge: false
    delete_branch_on_merge: true
    allow_auto_merge: true          # PR lands the instant its gate goes green
    allow_update_branch: true
  actions:                          # GitHub Actions permissions (least privilege)
    enabled: true
    actions_enabled: true           # don't silently break CI
    allowed_actions: all
    default_workflow_permissions: read     # GITHUB_TOKEN read-only by default
    can_approve_pull_request_reviews: false
  ghas:                             # Advanced Security: all scanners ON (secure default)
    enabled: true
    vulnerability_alerts: true
    automated_security_fixes: true
    secret_scanning: true
    secret_scanning_push_protection: true
    code_scanning_default_setup: true
    # NOTE: no dependency_graph knob — github.com has no togglable API for it.
  browser:                          # API-unreachable toggles via agent-browser
    enabled: true                   # PLANNED so status lists it; apply needs RIG_GH_BROWSER=1
    discussions: false
    projects: true
```

## Phased implementation plan

# Phased plan — smallest-shippable first, agent-browser LAST

The capability is already shipped end-to-end on `main`; this plan is the order in which the work
WAS / SHOULD BE layered, gh-api guardrails before the browser fallback. Each phase is independently
landable, leaves the build green, and extends the existing `github_*` helpers (never reinvents).

## Phase 0 — Skeleton (shared seams)  [DONE]
- `_gh_api(args, input_text)` single seam in `runner.py`; `github_owner_repo` resolves
  owner/repo from `git remote get-url origin` (no github remote -> `skipped`).
- `RIG_GH_DRY_RUN` seam (GET still runs, POST/PUT/PATCH skipped); CTO #4136.1 auth gate in
  `github_auth.py` (`ensure_gh_auth`, notify+wait, `RIG_GH_AUTH_WAIT`/`_POLL`, per-process dedup).
- `github` wired as a REPO-layer separate apply step (NOT a catalog category); `config_schema.py`
  `_GITHUB_BLOCK` registered; `plan.py` builders + `state.py` scaffold + `drift.py` dispatch.

## Phase 1 — Branch protection ruleset (the highest-value guardrail)  [DONE — PR #19]
- `github_ruleset.py`: `build_ruleset_body`/`build_ruleset_rules`/`normalize_ruleset`/
  `github_ruleset_state`; modern `/rulesets` endpoint; `~DEFAULT_BRANCH`; structural `update`-rule
  guard + admin-bypass; required-checks auto-default from provisioned CI gates; inheritance guard.
- Smallest shippable on its own: a repo that can't be force-pushed / deleted, PR-gated.

## Phase 2 — GHAS (the security settings the CI gates depend on — the literal #5 ask)  [DONE — PR per 3aa2fbb]
- `github_ghas.py`: four endpoint families; capability degrade (`_looks_like_ghas_unlicensed`
  plan-wording match, not bare status); free-vs-licensed split; no `dependency_graph` lie.
- ORDER: build/run AFTER `github.actions` (CodeQL needs Actions enabled) — so Phase 3 lands first
  in the plan order even though Phase 2 is the headline ask.

## Phase 3 — Actions permissions (least-privilege token)  [DONE]
- `github_actions.py`: two PUT endpoints; skip workflow-permissions when Actions off (else
  perpetual drift); secure default read-only token + no PR-approval.

## Phase 4 — Merge-button policy  [DONE]
- `github_merge.py`: PATCH merge-fields-only; squash-only enforced (all three flags); no `create`
  state. Lowest risk (a mis-set value only disables a button), so it can land any time after P0.

## Phase 5 — agent-browser fallback (LAST — heaviest, gated off, narrowest value)  [SHIPPED as mechanism]
- `github_browser.py` pure plan + `_agent_browser`/`_do_provision_github_browser`/
  `_browser_on_login_page` in runner; `ensure_browser_auth` gate; `RIG_GH_BROWSER=1` apply gate;
  accessibility-role selectors; loud per-step degrade. Tested without launching a browser.
- Deliberately last: a real browser is slow/flaky, so every API-reachable setting MUST be a gh-api
  job first; the browser is the fallback only for settings GitHub never shipped an endpoint for.

## Phase 6 — Hardening follow-ups (own tickets, post-ship)
- 6a. First-class `org_enforced`/`overridden` classification (distinct from `gh_error`/degrade) so
  status can name org/enterprise caps: Actions `allowed_actions` caps, org-enforced GHAS, inherited
  org rulesets.
- 6b. `allowed_actions:"selected"` allow-list sub-payload (`selected-actions` endpoint) OR drop the
  enum value.
- 6c. Browser correctness: re-classify the toggle table to genuinely-API-less settings (move
  discussions/projects to gh-api, fix stale docstrings); add `build_read_plan` read-before-write
  idempotency; replace the `drift.py` browser `pass` with a real read-only probe behind
  `RIG_GH_BROWSER`; snapshot+screenshot artifact on first selector failure.
- 6d. Status UX: informational `no_remote` line; short-circuit the 5 failing gh reads when unauthed.

## Open issues — RESOLVE BEFORE BUILDING (adversarial critique: needs-work)

This is a design, not a green light. It MUTATES real GitHub repo settings with secure
defaults ON — a wrong default can lock the CTO out of merging or clobber an org ruleset.
The must-fix items below are blocking for an implementer; the full critique is in the
workflow record.

### Must fix before building
- allowed_actions 'selected': reject it at config-validation time until the selected-actions allow-list sub-payload exists (github_actions.py ALLOWED_ACTIONS_VALUES + the validator). Shipping it accepted means a one-line config typo bricks all third-party actions in CI. (Spec Gap 2 — promote from 'tracked' to blocker.)
- Browser-backed discussions/projects: either move them to the gh-api backend (PATCH /repos has_discussions/has_projects — they're API-exposed) so the default-ON secure values actually apply on a normal `rig apply`, OR drop them from DEFAULTS so the config stops advertising a setting that silently no-ops. Do not ship `projects: true` as a default that never takes effect. (Spec Gap 3.)
- Auth notify command scope: change the suggested `gh auth login -s repo` to the actual scope set the provisioners need (e.g. add workflow / security_events as applicable), OR explicitly tell the user in the notify message that more scope may be required and the next action will reveal it. A correct-looking command that still 403s is worse than no command.
- Required-status-check contexts: make the cross-repo coupling defensive. At minimum, the context strings must be co-located/asserted against agent-tools' actual workflow job names by a sync test that fails if they drift (mirror the schema↔config_schema sync test pattern already used). A silent rename = PR lockout for contributors is too sharp an edge to leave on a hardcoded constant pair.
- Transient-read vs plan-gated must be distinguished in GHAS: a None from _gh_subresource_enabled that is NOT a confirmed 404/plan-limit (i.e. an HTTP 5xx / network / rate-limit) should NOT cause the secure-default free feature to be silently skipped on apply — either retry, or attempt the write anyway (PUT is idempotent), or classify it as a hard error, instead of folding it into the 'unverifiable → skip' path that leaves the secure default OFF.
- Stale docstrings (Spec Gap 3, now confirmed in code): github_browser.py still documents `allow_forking_label` and 'Automatically delete head branches' toggles that aren't in UI_ONLY_TOGGLES; github_ghas.py lines 71-72 claim 'the dependency_graph config knob still exists' while lines 19-21 and DEFAULTS say there is NO such knob. Correct both before build — they actively mislead the next reader about what's managed.

### Risks
- LOCKOUT (mitigated, not eliminated): the ruleset's required_status_checks contexts are HARDCODED constants in rig-cli (github_ruleset.py CI_GATE_CHECK_CONTEXTS = 'PR Checklist', 'review-threads') that must byte-match the job name: in agent-tools' workflow.yml in a DIFFERENT repo. They match today (verified), but this is a cross-repo coupling: rename a job in agent-tools and every rig-managed repo silently wedges every PR (GitHub waits forever for a check-run that never reports). admin_bypass (actor_id 5, default on) is the only safety net, so non-admin contributors are locked out, admins aren't. The guard only covers 'workflow absent', NOT 'workflow present but job name drifted'.
- WRONG AUTH INSTRUCTION: the gate notifies the user to run `gh auth login -h github.com -s repo`, but `repo` scope alone does not cover everything provisioned (Actions permissions / workflow-token / some security_and_analysis paths can need workflow/security_events/admin). github_auth.py admits 'scope adequacy is enforced by the 403'. So a user who dutifully runs the EXACT command rig gave them can still hit 403 on the next action and get a failed apply right after being told they're fine. The suggested command is a floor, not the requirement, and the spec presents it as the fix.
- FREE-FEATURE SKIP ON TRANSIENT READ: _gh_subresource_enabled() collapses ANY non-404 error (network blip, rate-limit, transient 5xx) to None, and the GHAS apply loop SKIPS the mutation for any endpoint marked unverifiable. So a transient read failure on vulnerability-alerts/automated-security-fixes means that secure-default feature is silently NOT enabled this run (only a 'could not read' degrade) — contradicting the spec's claim that free features 'still apply, never masked'. Self-heals on next clean apply, but an unattended one-shot `rig init` lands the repo with the secure default OFF.
- SETTINGS THAT LOOK APPLIED BUT NO-OP: github.browser ships defaults `projects: true` / `discussions: false` (UI_ONLY_TOGGLES), but both ARE gh-api-exposed (PATCH /repos has_projects/has_discussions). The browser backend is gated OFF unless RIG_GH_BROWSER=1, so on a normal `rig apply` these advertised secure defaults are NEVER set. Spec Gap 3 acknowledges it but ships the misleading defaults anyway.
- SELF-INFLICTED CI BREAK: allowed_actions accepts 'selected' (in ALLOWED_ACTIONS_VALUES, body builder emits it) with NO companion selected-actions allow-list payload. Setting 'selected' PUTs the mode with an empty list → GitHub allows ZERO third-party actions → every workflow using a marketplace action fails. Spec Gap 2 acknowledges it; the value is still accepted.
- CLOBBER (bounded, intended): ruleset apply PUTs the FULL desired body to /rulesets/{id} (complete replace, not merge). Any rule a human manually added to the rig-managed-named ruleset is stripped on next apply. normalize_ruleset's `else` branch DOES include unknown rule types in the diff so it shows as drift first (not fully silent), but the contract is 'rig owns this named ruleset entirely' — fine only as long as nobody hand-edits the rig-managed ruleset.
- AUTH-WAIT DEDUP HOLE: _TIMED_OUT_KINDS makes every action AFTER the first timeout short-circuit to timed_out WITHOUT re-probing. A user who logs in during action 2-5's turn (after action 1 timed out) still gets those actions failed — 'resume automatically once logged in' is only true for the FIRST action. Re-run fixes it.
- NO ATOMICITY: run_plan collects errors and continues. A hard-error mid-apply (e.g. GHAS 403) leaves ruleset+merge already mutated, Actions/GHAS not. Safe due to idempotent re-run + admin_bypass, but a partial-apply mixed state is possible and undocumented in the spec.

---
*Designed by the `rig-repo-settings-design` agent workflow (gh-api / agent-browser / rig-mechanics facets → synthesis → adversarial critique), 2026-06-21. Grounded in the existing riglib/github_*.py helpers.*
