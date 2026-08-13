# Rig-managed files

Rig distinguishes ownership at file/block granularity instead of treating every generated-looking file the same.

| Class | What Rig owns | Where to make durable changes | Direct edits |
| --- | --- | --- | --- |
| `generated` | the entire target file | global Rig config / `rig.yaml` / generator policy | diagnostic only; the next reconcile may replace them |
| `source-backed` | the entire materialized target copied from a canonical carrier | the named agent-tools source or Rig selection | do not edit the target; change the source and reconcile |
| `marker-managed` | only the region between Rig BEGIN/END markers | Rig config/source for the managed block | content outside the markers is user-owned and safe to edit |

Comment-capable generated targets carry this contract in a syntax-valid header, for example:

```ts
// Managed by Rig. Class: generated. Source of truth: global Rig config + rig.yaml.
```

or:

```yaml
# Managed by Rig. Class: source-backed. Canonical source: agent-tools/linters/ruff.toml.
```

`rig status`/drift logic must use the same ownership boundary as apply: a marker-managed file is not compared as if Rig owned unrelated user text outside its block.

## Strict JSON

Rig does **not** inject fake metadata properties into arbitrary strict JSON. Unknown properties are legal in JSON syntax but many consumers validate a closed application schema, so a made-up `_rig`, `$comment`, or similar key can turn an otherwise valid tool configuration into an invalid one.

The preferred order is:

1. use JSONC/JSON5/YAML/TOML when the consumer officially supports a comment-capable format;
2. use a documented extension field such as `$comment` only when that consumer/schema explicitly permits it;
3. otherwise keep ownership metadata out-of-band in Rig state/status and identify the target there.

This is deliberately capability-driven rather than extension-driven: Rig may add in-band metadata only when it can prove the target tool accepts that metadata without changing semantics. A future per-tool carrier capability can opt strict JSON formats into a known-safe metadata field; the generic JSON writer remains conservative.
