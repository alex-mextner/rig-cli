#!/usr/bin/env python3
"""One-shot branch patcher for the large Rig integration files.

The GitHub connector can safely create small modules but only replaces whole files. This script is
committed temporarily and executed in CI against the branch checkout so the large existing modules
can be changed by narrow, asserted transformations. It deletes itself in the workflow commit.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def regex_once(text: str, pattern: str, replacement: str, label: str) -> str:
    # Use a callable replacement. Passing a string directly makes re.sub interpret backslash
    # escapes in generated source (for example the literal ``\\r\\n`` becomes a physical CRLF),
    # which can corrupt Python code even though the replacement template itself is correct.
    out, count = re.subn(pattern, lambda _match: replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one regex match, found {count}")
    return out


def patch_schema() -> None:
    path = "riglib/config_schema.py"
    text = read(path)
    replacement = '''_LINTERS_ITEM_BLOCK = Block(
    doc="one explicit linter/formatter config file rig writes/reconciles.",
    leaves={
        "tool": Leaf("string", "the tool name (drives status/log labels)"),
        "role": Leaf("string", "linter | formatter", enum=("linter", "formatter"), default="linter"),
        "path": Leaf("string", "repo-relative target config path"),
        "content": Leaf("string", "literal desired content; mutually exclusive with source"),
        "source": Leaf("string", "agent-tools-relative canonical source file; mutually exclusive with content"),
        "enabled": Leaf("boolean", "provision this one file", default=True),
    },
)

_LINTERS_RULES_BLOCK = Block(
    doc="Rig-owned rule selection; inherited through global config and refined by rig.yaml.",
    leaves={
        "all": Leaf("boolean", "enable every known applicable rule", default=False),
        "enable": Leaf("array", "individual rules to enable", items_type="string"),
        "disable": Leaf("array", "individual rules to disable", items_type="string"),
        "severity": Leaf("object", "final per-rule off|warn|error overrides", additional_properties_type="string"),
    },
    nested={
        "groups": Block(
            doc="rule-group toggles keyed by group name",
            additional_properties={"type": "boolean"},
        ),
    },
)

_LINTERS_BLOCK = Block(
    doc="Rig-owned lint/format policy plus explicit reusable config carriers.",
    leaves={
        "enabled": Leaf("boolean", "provision lint/format policy", default=True),
        "preview": Leaf("boolean", "show before/after lint finding counts when policy changes", default=True),
    },
    nested={"rules": _LINTERS_RULES_BLOCK},
    open_map="items",
    open_map_doc=(
        "extra config files keyed by label; each uses `{ tool, role, path, content|source, enabled }`."
    ),
    open_map_item=_LINTERS_ITEM_BLOCK,
    open_map_item_required=("tool", "path"),
)

_PROJECT_TOOLS_BLOCK'''
    text = regex_once(
        text,
        r"_LINTERS_ITEM_BLOCK = Block\(.*?\n\)\n\n_LINTERS_BLOCK = Block\(.*?\n\)\n\n_PROJECT_TOOLS_BLOCK",
        replacement,
        "config_schema linters blocks",
    )
    write(path, text)


def patch_config() -> None:
    path = "riglib/config.py"
    text = read(path)
    text = replace_once(
        text,
        'LINTER_ITEM_KEYS = {"tool", "role", "path", "content", "enabled"}',
        'LINTER_ITEM_KEYS = {"tool", "role", "path", "content", "source", "enabled"}',
        "linter item keys",
    )
    text = replace_once(
        text,
        '        if key not in {"enabled", "items"}:\n            raise ConfigError(\n                f"unknown linters key {key!r} (expected one of: enabled, items)",\n                schema_path="linters",\n            )',
        '        if key not in {"enabled", "preview", "rules", "items"}:\n            raise ConfigError(\n                f"unknown linters key {key!r} (expected one of: enabled, preview, rules, items)",\n                schema_path="linters",\n            )',
        "linters top-level keys",
    )
    needle = '''    if "enabled" in li and not isinstance(li["enabled"], bool):
        raise ConfigError(
            f"linters.enabled must be a bool, got {li['enabled']!r}", schema_path="linters.enabled"
        )
    items = li.get("items", {})
'''
    replacement = '''    if "enabled" in li and not isinstance(li["enabled"], bool):
        raise ConfigError(
            f"linters.enabled must be a bool, got {li['enabled']!r}", schema_path="linters.enabled"
        )
    if "preview" in li and not isinstance(li["preview"], bool):
        raise ConfigError(
            f"linters.preview must be a bool, got {li['preview']!r}", schema_path="linters.preview"
        )
    rules = li.get("rules", {})
    if not isinstance(rules, dict):
        raise ConfigError("linters.rules must be a mapping", schema_path="linters.rules")
    try:
        from .lint_policy import resolve_rule_severities

        resolve_rule_severities(rules)
    except ValueError as exc:
        raise ConfigError(str(exc), schema_path="linters.rules") from exc
    items = li.get("items", {})
'''
    text = replace_once(text, needle, replacement, "linters rules validation insertion")

    old = '''        # tool / path / content are REQUIRED non-empty strings — a config file with no path or no
        # bytes is meaningless; failing here beats writing a 0-byte file or crashing in the runner.
        for req in ("tool", "path", "content"):
            val = spec.get(req)
            if not isinstance(val, str) or not val:
                raise ConfigError(
                    f"{path}.{req} must be a non-empty string, got {val!r}",
                    schema_path=f"{path}.{req}",
                )
        rel = spec["path"]
'''
    new = '''        # tool/path are required. Desired bytes come from exactly one of literal `content` or a
        # canonical agent-tools-relative `source` file.
        for req in ("tool", "path"):
            val = spec.get(req)
            if not isinstance(val, str) or not val:
                raise ConfigError(
                    f"{path}.{req} must be a non-empty string, got {val!r}",
                    schema_path=f"{path}.{req}",
                )
        content = spec.get("content")
        source = spec.get("source")
        if (content is None) == (source is None):
            raise ConfigError(
                f"{path} must set exactly one of content or source",
                fix="use content for repo-specific bytes or source for a reusable agent-tools file",
                schema_path=path,
            )
        desired = content if content is not None else source
        desired_key = "content" if content is not None else "source"
        if not isinstance(desired, str) or not desired:
            raise ConfigError(
                f"{path}.{desired_key} must be a non-empty string, got {desired!r}",
                schema_path=f"{path}.{desired_key}",
            )
        if source is not None and (source != source.strip() or linter_path_escapes_repo(source)):
            raise ConfigError(
                f"{path}.source must be a safe agent-tools-relative path, got {source!r}",
                schema_path=f"{path}.source",
            )
        rel = spec["path"]
'''
    text = replace_once(text, old, new, "linter item content/source validation")
    write(path, text)


def patch_plan() -> None:
    path = "riglib/plan.py"
    text = read(path)
    text = replace_once(
        text,
        "    _build_linters(config, plan)",
        "    _build_linters(config, catalog, plan)",
        "build linters call",
    )
    replacement = '''def _build_linters(config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:
    """Plan Rig-owned lint policy and reusable linter/formatter config carriers.

    Rig, not an Oxc file, is the policy source. TypeScript stacks receive the built-in rule
    baseline automatically; the merged ``linters.rules`` block applies global/repository
    overrides. The generated Oxlint config and the selected anti-slop implementation files are
    ordinary reconciled linter actions, so apply/status retain the same idempotency guarantees as
    explicit ``linters.items``.
    """
    li = config.data.get("linters")
    if li is None:
        li = {}
    if not isinstance(li, dict) or li.get("enabled") is False:
        return

    from .lint_policy import anti_slop_required, render_oxlint_config, resolve_rule_severities, rule_policy_summary
    from .linter_carriers import read_source_text

    items = li.get("items", {})
    item_paths: set[str] = set()
    if isinstance(items, dict):
        for name, spec in items.items():
            if not isinstance(spec, dict) or spec.get("enabled") is False:
                continue
            rel_path = spec.get("path")
            if not isinstance(rel_path, str) or not rel_path:
                continue
            content = spec.get("content")
            source_rel = spec.get("source")
            if isinstance(source_rel, str) and source_rel:
                try:
                    content = read_source_text(catalog.source, source_rel)
                except ValueError as exc:
                    raise PlanError(f"linters.items.{name}.source: {exc}") from exc
            if not isinstance(content, str) or not content:
                continue
            item_paths.add(PurePosixPath(rel_path).as_posix())
            plan.actions.append(
                Action(
                    kind="provision_linter_config",
                    category="linters",
                    item=str(name),
                    source=catalog.source,
                    target=config.repo_root,
                    options={
                        "tool": str(spec.get("tool") or ""),
                        "role": str(spec.get("role") or "linter"),
                        "rel_path": rel_path,
                        "content": content,
                    },
                )
            )

    stack_parts = set((config.stack or "").split("/"))
    typescript_stack = bool({"ts", "typescript"} & stack_parts)
    rules_explicit = "rules" in li
    if not typescript_stack and not rules_explicit:
        return

    if "oxlint.config.ts" in item_paths:
        raise PlanError(
            "linters.items targets oxlint.config.ts while Rig rule policy also owns that file; "
            "remove the item and configure linters.rules instead"
        )

    rules_cfg = li.get("rules", {})
    if not isinstance(rules_cfg, dict):
        rules_cfg = {}
    severities = resolve_rule_severities(rules_cfg)
    generated = render_oxlint_config(rules_cfg)
    summary = rule_policy_summary(rules_cfg)
    plan.notes.append(
        "linters: effective rule policy — "
        f"{summary['error']} error, {summary['warn']} warn, {summary['off']} off"
    )
    plan.actions.append(
        Action(
            kind="provision_linter_config",
            category="linters",
            item="rig-oxlint-policy",
            source=catalog.source,
            target=config.repo_root,
            options={
                "tool": "oxlint",
                "role": "linter",
                "rel_path": "oxlint.config.ts",
                "content": generated,
                "preview_findings": li.get("preview") is not False,
            },
        )
    )

    if anti_slop_required(severities):
        source_root = catalog.source / "vendor" / "anti-slop" / "src"
        if not source_root.is_dir():
            raise PlanError(
                "anti-slop rules are enabled but the pinned vendor/anti-slop/src source is missing; "
                "initialize/update the agent-tools submodule"
            )
        for source_file in sorted(source_root.rglob("*.ts")):
            if source_file.name.endswith(".test.ts"):
                continue
            rel = source_file.relative_to(source_root).as_posix()
            target_rel = f"tools/oxlint/anti-slop/{rel}"
            try:
                content = source_file.read_text(encoding="utf-8").replace("\\r\\n", "\\n").replace("\\r", "\\n")
            except (OSError, UnicodeDecodeError) as exc:
                raise PlanError(f"cannot read anti-slop source {source_file}: {exc}") from exc
            plan.actions.append(
                Action(
                    kind="provision_linter_config",
                    category="linters",
                    item=f"anti-slop/{rel}",
                    source=source_file,
                    target=config.repo_root,
                    options={
                        "tool": "anti-slop",
                        "role": "linter",
                        "rel_path": target_rel,
                        "content": content,
                    },
                )
            )


'''
    text = regex_once(
        text,
        r"def _build_linters\(config: LoadedConfig, plan: InstallPlan\) -> None:.*?(?=def _build_global_excludes)",
        replacement,
        "plan _build_linters",
    )
    write(path, text)


def patch_runner() -> None:
    path = "riglib/actions/runner.py"
    text = read(path)
    replacement = '''def _do_provision_linter_config(action: Action, on_conflict: str) -> ActionResult:
    """Provision/reconcile one Rig-managed linter/formatter file.

    For the generated Oxlint policy action, a best-effort finding preview runs before the write and
    is appended to the result: current errors/warnings → desired errors/warnings. Preview failures
    never block policy convergence.
    """
    rel_path = action.options.get("rel_path")
    content = action.options.get("content")
    tool = str(action.options.get("tool") or "")
    role = str(action.options.get("role") or "linter")
    label = _linter_label(role, tool, action.item)
    if not isinstance(rel_path, str) or not rel_path:
        return ActionResult(action, "error", f"{label}: missing rel_path")
    if not isinstance(content, str):
        return ActionResult(action, "error", f"{label}: missing content")

    r = resolve_linter_config(action.target, rel_path, content)
    if r.state == "io_error":
        return ActionResult(action, "error", f"{label}: {r.detail}")
    if r.state == "ok":
        return ActionResult(action, "skipped", f"{label}: already correct")

    preview = ""
    if action.options.get("preview_findings") and tool == "oxlint":
        try:
            from ..lint_preview import preview_oxlint_policy

            impact = preview_oxlint_policy(action.target, content, rel_path)
            preview = f"; {impact.render()}"
        except Exception as exc:  # preview is informative, never a provisioning gate
            preview = f"; lint finding preview skipped: {type(exc).__name__}: {exc}"

    out = fsutil.write_file(r.target, content, on_conflict)
    return ActionResult(action, out.status, f"{label}: {out.detail}{preview}", out.backup)


'''
    text = regex_once(
        text,
        r"def _do_provision_linter_config\(action: Action, on_conflict: str\) -> ActionResult:.*?(?=def [A-Za-z_])",
        replacement,
        "runner linter handler",
    )
    write(path, text)


def refresh_schema() -> None:
    # Import only after source patches are on disk.
    import sys

    sys.path.insert(0, str(ROOT))
    from riglib import config_schema

    schema = config_schema.json_schema()
    (ROOT / config_schema.SCHEMA_REL_PATH).write_text(
        json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> None:
    patch_schema()
    patch_config()
    patch_plan()
    patch_runner()
    refresh_schema()
    print("lint policy integration patch applied")


if __name__ == "__main__":
    main()
