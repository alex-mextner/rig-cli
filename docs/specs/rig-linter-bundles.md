# Rig reusable linter carriers

Status: implementation contract for directory `linters.bundles` plus source-file-backed `linters.items`.

## Problem

Today's `linters.items` embeds exact text in `rig.yaml`. That is fine for a one-off config but poor for organization-wide presets. It also cannot represent a vendored local plugin directory such as anti-slop.

`agent-tools` now has two canonical source forms:

- `vendor/anti-slop/skills/install-anti-slop/assets/anti-slop/` — pinned subrepo directory payload;
- `linters/oxc/` — canonical Oxlint/Oxfmt config files.

Rig should reconcile both without network downloads and without duplicating their bytes into every project YAML.

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
      source: linters/oxc/oxlint.config.ts

    oxfmt:
      tool: oxfmt
      role: formatter
      path: .oxfmtrc.jsonc
      source: linters/oxc/.oxfmtrc.jsonc
```

### `linters.bundles.<label>`

- `source`: required repo-relative directory under resolved `agent_tools_source`;
- `target`: required repo-relative destination directory under the target repo;
- `enabled`: optional bool, default true.

### `linters.items.<label>`

Keep today's `tool`, `role`, `path`, `enabled`. Desired bytes may come from **exactly one** of:

- `content`: existing inline exact text;
- `source`: a repo-relative regular file under resolved `agent_tools_source`.

Require one and only one of `content`/`source`. This preserves backward compatibility while allowing reusable presets.

All source paths are anchored to `agent_tools_source`. A committed project config cannot read arbitrary machine files.

## Safety and desired state

For both source files and bundles reject absolute paths, `..`, backslashes, `.git`, leading/trailing whitespace, and symlink traversal. Missing source is an error. For anti-slop specifically, an uninitialized Git subrepo therefore fails closed instead of silently creating an empty plugin.

A bundle is a deterministic regular-file tree. Symlinks, device files, sockets, and other non-regular entries are invalid. Exact means the complete tree matches, including absence of extra target files.

A source-file item reads exact bytes from its declared source and otherwise uses the existing `provision_linter_config` conflict/drift semantics.

## Conflict semantics

Both carrier forms use Rig's existing `skip | overwrite | backup` policy.

For bundles, the directory is one conflict unit: `backup` moves the complete previous target to a unique sibling and then copies the source; `overwrite` replaces the whole tree so stale files disappear; `skip` leaves drift visible.

For source-file items, existing file behavior remains unchanged. A correct re-apply is a no-op and creates no backup.

## Plan / apply / status parity

- source-file items still emit `provision_linter_config`; plan resolves their source bytes once and carries the desired content, so apply/status compare the same bytes;
- bundles emit `provision_linter_bundle` with a shared apply/drift classifier;
- disabling a carrier never surprise-deletes the old target.

## Schema

`linters` stays closed with named `items` and `bundles` maps. Bundle items are closed. Linter items add optional `source` and change the requirement from mandatory `content` to an exclusive `content XOR source` semantic validation.

The published JSON schema should express this with `oneOf` where practical, while runtime validation remains authoritative and fail-closed.

## anti-slop + Oxc consumer

```text
agent-tools/
  vendor/anti-slop/
    skills/install-anti-slop/assets/anti-slop/
  linters/oxc/
    oxlint.config.ts
    .oxfmtrc.jsonc
```

becomes:

```text
target-repo/
  tools/oxlint/anti-slop/
  oxlint.config.ts
  .oxfmtrc.jsonc
```

The standard JS/TS toolchain is Oxc: Oxlint + Oxfmt. The canonical Oxlint preset enables every anti-slop rule, type-aware mode, `typescript/no-unsafe-type-assertion`, `typescript/no-unnecessary-type-assertion`, `typescript/no-non-null-assertion`, and strict `typescript/ban-ts-comment`. `tsc --noEmit` remains a separate compiler gate.

## Non-goals

- no global binaries;
- no package-manager mutation in linter carriers;
- no network download during apply;
- no anti-slop special case in Rig core;
- no AST merge of arbitrary existing Oxlint config.

## Tests required before merge

- validation for both carriers, including `content XOR source`;
- POSIX/Windows containment and `.git` rejection;
- missing/uninitialized subrepo and nested source symlink rejection;
- source-file create/update/skip/backup and byte parity;
- bundle create, exact no-op, binary/text preservation, extra/missing/changed file drift;
- bundle skip/overwrite/backup conflict behavior;
- target leaf/parent symlink rejection;
- apply/status classifier parity;
- area/item/bundle disable gating;
- clean-room round trip under temporary HOME;
- published JSON schema sync.
