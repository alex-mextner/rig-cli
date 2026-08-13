# Rig linter bundles

Status: implementation contract for `linters.bundles`.

## Problem

The existing `linters.items` reconciler is file-oriented: one item declares a tool, role, repo-relative path, and exact text content. That works for linter/formatter configs but not for a local plugin intentionally vendored as a directory tree.

`anti-slop` is the motivating case. `agent-tools` pins the reviewed fork as `vendor/anti-slop`; target projects should receive the plugin's supported vendoring payload, not import the remote repository at lint time and not embed every TypeScript file into `rig.yaml`.

## Contract

```yaml
linters:
  enabled: true
  bundles:
    anti-slop:
      source: vendor/anti-slop/skills/install-anti-slop/assets/anti-slop
      target: tools/oxlint/anti-slop
  items:
    oxlint:
      tool: oxlint
      role: linter
      path: oxlint.config.ts
      content: |
        # exact-content Oxc config
```

Each enabled `bundles.<label>` has:

- `source` — a relative directory under resolved `agent_tools_source`; no absolute path, `..`, backslash, `.git`, or symlink traversal;
- `target` — a repo-relative destination directory with the same containment restrictions;
- `enabled` — optional bool, default true.

The source is anchored to `agent_tools_source`. A committed project config cannot copy arbitrary machine directories. If the anti-slop Git subrepo has not been initialized, the source is missing and apply fails closed with an actionable error; an empty/missing source must never produce an empty target.

## Desired state

A bundle is a deterministic tree of regular files. Directories are implicit. Symlinks, device files, sockets, and other non-regular source entries are rejected.

- missing target → copy bundle;
- exact target → no-op;
- differing target → apply `defaults.on_conflict`;
- target symlink or symlink in any parent → error;
- source/target escape or `.git` component → validation error plus apply-time defense in depth.

Exact means the complete regular-file tree matches. Extra target files are drift so a removed rule cannot remain executable forever.

## Conflict semantics

- `skip` — leave a differing target untouched and keep drift visible;
- `overwrite` — replace the full differing target so stale files disappear;
- `backup` — move the full prior target to a unique `.rig-bak-<UTC>` sibling, then copy the desired tree.

A correct re-apply is a true no-op and creates no backup. Rig never follows source or target symlinks.

## Plan / apply / status parity

Add one `provision_linter_bundle` action per enabled bundle. Apply and drift share one classifier, mirroring `provision_linter_config`.

Status reports missing, modified, unsafe/unreadable, or in-sync. Disabling a bundle does not surprise-delete an existing target.

## Schema

`linters` remains closed except for its two named maps:

```yaml
linters:
  enabled: boolean
  items:   # existing exact-content config files
  bundles: # vendored directory payloads
```

Each bundle item is closed, requires non-empty `source` and `target`, and accepts optional boolean `enabled`.

The current schema `Block` supports one `open_map`; model `items` and `bundles` as explicit nested map blocks or extend the representation to multiple named open maps. Prefer the smallest representation that keeps the canonical schema emitter, runtime validator, and published schema in lockstep.

## anti-slop + Oxc consumer

The source of truth is the pinned subrepo:

```text
agent-tools/
  vendor/anti-slop/                     # git subrepo
    skills/install-anti-slop/assets/anti-slop/
      index.ts
      rules/
      shared/
```

Rig copies that supported payload to:

```text
target-repo/
  tools/oxlint/anti-slop/
  oxlint.config.ts
  .oxfmtrc.jsonc   # optional team formatting config
```

The standard JS/TS toolchain is Oxc: Oxlint + Oxfmt, not Biome. The Oxlint config enables the complete anti-slop plugin and type-aware mode, including:

- `typescript/no-unsafe-type-assertion`
- `typescript/no-unnecessary-type-assertion`
- `typescript/no-non-null-assertion`
- `typescript/ban-ts-comment` with `@ts-ignore`/`@ts-nocheck` banned and `@ts-expect-error` description required.

`tsc --noEmit` remains a separate compiler gate.

## Non-goals

- no global binaries;
- no package-manager mutation inside the directory-copy action;
- no network download during apply;
- no anti-slop special case in Rig core;
- no arbitrary AST merge of an existing Oxlint config.

## Tests required before merge

- validation: valid bundle; bad scalar/map types; unknown item key; missing/empty source/target; non-bool enabled;
- containment: POSIX/Windows absolute paths, `..`, backslashes, `.git`, whitespace-padded paths;
- source safety: missing source, source file instead of directory, source symlink, nested symlink, non-regular entry, uninitialized subrepo;
- apply: create nested tree, binary/text bytes preserved, exact re-apply no-op;
- conflict: skip/overwrite/backup on changed file, extra target file, missing target file;
- target safety: leaf/parent symlinks and non-directory parents;
- drift: missing/modified/io-error parity with apply;
- plan: area disabled, bundle disabled, one action per enabled bundle;
- full round-trip under temporary HOME;
- published JSON schema sync.
