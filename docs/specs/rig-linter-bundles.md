# Rig linter bundles

Status: implementation contract for `linters.bundles`.

## Problem

The existing `linters.items` reconciler is intentionally file-oriented: one item declares a tool, role, repo-relative path, and exact text content. That is sufficient for `.oxfmtrc`, `ruff.toml`, and similar configuration files, but not for local linter plugins that are intentionally vendored as a directory tree.

`dmmulroy/anti-slop` is the motivating case. Its supported installation model copies the plugin source into the target repository (for example `tools/oxlint/anti-slop/`) instead of consuming the upstream repository as a fixed npm dependency. Encoding every TypeScript source file as a YAML `content:` scalar would make `rig.yaml` unreadable and duplicate source ownership.

## Contract

Extend the existing repo-layer `linters` block with a second open map:

```yaml
linters:
  enabled: true
  bundles:
    anti-slop:
      source: linters/anti-slop
      target: tools/oxlint/anti-slop
  items:
    oxlint:
      tool: oxlint
      role: linter
      path: oxlint.config.ts
      content: |
        # normal exact-content linter item remains supported
```

Each enabled `bundles.<label>` has:

- `source` — a relative directory under the resolved `agent_tools_source` checkout. No absolute path, `..`, backslash, `.git`, or symlink traversal is allowed.
- `target` — a repo-relative destination directory. Apply must reject absolute paths, `..`, backslashes, `.git`, or symlink traversal, using the same containment threat model as `linters.items.path`.
- `enabled` — optional bool, default true.

The source is deliberately anchored to `agent_tools_source`. A committed `rig.yaml` cannot make `rig apply` copy an arbitrary machine directory into the repository.

## Desired state

A bundle is a deterministic set of regular files beneath `source`. The desired state records relative paths plus exact bytes. Directories are implicit. Symlinks, device files, sockets, and other non-regular source entries are rejected.

The target directory is treated as one managed unit for conflict policy, but reconciliation is file-aware:

- missing target → copy the bundle;
- exact target → no-op;
- differing target → conflict according to `defaults.on_conflict`;
- target symlink or a symlink in any parent component → error;
- source/target path escape or `.git` component → validation error and defense-in-depth apply error.

A target is exact only when the complete regular-file tree matches the source. Extra files count as drift: otherwise deleting a source rule from the managed bundle would leave stale executable plugin code in the target forever.

## Conflict semantics

Match Rig's existing never-clobber model:

- `skip` — leave a differing target untouched and report skipped/drift;
- `overwrite` — replace the differing target atomically enough that stale files are removed;
- `backup` — rename the full prior target to a unique `.rig-bak-<UTC>` sibling, then write the desired tree.

A correct re-apply is a true no-op and creates no backup.

Rig never follows target symlinks. A source tree containing symlinks is invalid rather than dereferenced.

## Plan / apply / status parity

Add one action kind, `provision_linter_bundle`, per enabled bundle. The action carries the resolved source and target directories; apply and drift must share one resolver/classifier just as `provision_linter_config` does today.

`rig status` reports:

- missing bundle target;
- modified bundle target;
- unsafe/unreadable bundle state;
- in-sync bundle as no drift.

Disabling a bundle does not auto-delete an existing target, consistent with Rig's general no-surprise-delete policy; the plan should note the leftover when useful.

## Schema

`linters` remains a closed block except for its two named maps:

```yaml
linters:
  enabled: boolean
  items:   # existing
  bundles: # new
```

Each bundle item is closed and requires `source` and `target` non-empty strings; `enabled` is optional bool.

Because the current schema `Block` representation supports only one `open_map`, model `items` and `bundles` as explicit nested map blocks or extend `Block` to support multiple named open maps. Prefer the former if it keeps the schema emitter smaller and avoids a registry-wide representation change.

## anti-slop consumer

`agent-tools` should carry a reviewed vendored copy at:

```text
linters/anti-slop/
  index.ts
  rules/
  shared/
```

A TypeScript repository then declares:

```yaml
linters:
  bundles:
    anti-slop:
      source: linters/anti-slop
      target: tools/oxlint/anti-slop
```

The repository's Oxlint config registers `./tools/oxlint/anti-slop/index.ts` as a JS plugin and enables the anti-slop rules.

The recommended policy also enables Oxlint's built-in `typescript/no-non-null-assertion` and `typescript/ban-ts-comment`, replacing the old grep checks for non-null assertions / TypeScript suppression comments with AST-aware enforcement.

## Non-goals

- Do not install global binaries.
- Do not mutate package-manager dependencies from the directory-copy action.
- Do not download source from the network during apply.
- Do not special-case `anti-slop` in Rig's core; it is a consumer of generic linter bundles.
- Do not merge arbitrary existing `oxlint.config.ts` AST in the bundle action. Config files remain ordinary `linters.items` desired state or project-owned files.

## Tests required before merge

- validation: valid bundle; bad scalar/map types; unknown item key; missing/empty source/target; non-bool enabled;
- containment: POSIX/Windows absolute paths, `..`, backslashes, `.git`, whitespace-padded paths;
- source safety: missing source, source file instead of directory, source symlink, nested symlink, non-regular entry;
- apply: create nested tree, binary/text bytes preserved, exact re-apply no-op;
- conflict: skip/overwrite/backup on changed file, extra target file, missing target file;
- target safety: leaf/parent symlinks and non-directory parents;
- drift: missing/modified/io-error parity with apply;
- plan: area disabled, bundle disabled, one action per enabled bundle;
- full round-trip under temporary HOME;
- published JSON schema sync.
