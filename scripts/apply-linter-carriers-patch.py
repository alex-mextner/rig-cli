from pathlib import Path
import re


def replace(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:80]!r}")
    p.write_text(text.replace(old, new, 1))


def regex(path: str, pattern: str, replacement: str) -> None:
    p = Path(path)
    text = p.read_text()
    new, n = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if n != 1:
        raise SystemExit(f"pattern matched {n} times in {path}: {pattern[:80]!r}")
    p.write_text(new)


# config schema: source-file items + bundles map.
replace(
    "riglib/config_schema.py",
    '        "content": Leaf("string", "the exact bytes rig writes/reconciles"),\n        "enabled": Leaf("boolean", "provision this one file", default=True),',
    '        "content": Leaf("string", "inline exact bytes rig writes/reconciles (exclusive with source)"),\n        "source": Leaf("string", "repo-relative preset file under agent_tools_source (exclusive with content)"),\n        "enabled": Leaf("boolean", "provision this one file", default=True),',
)
replace(
    "riglib/config_schema.py",
    '    open_map_item_required=("tool", "path", "content"),\n)\n\n_PROJECT_TOOLS_BLOCK = Block(',
    '    open_map_item_required=("tool", "path"),\n    nested={\n        "bundles": Block(\n            doc="vendored linter/plugin directory bundles keyed by label.",\n            additional_properties={\n                "type": "object",\n                "properties": {\n                    "source": {"type": "string"},\n                    "target": {"type": "string"},\n                    "enabled": {"type": "boolean", "default": True},\n                },\n                "required": ["source", "target"],\n                "additionalProperties": False,\n            },\n        ),\n    },\n)\n\n_PROJECT_TOOLS_BLOCK = Block(',
)

# runtime validation.
replace(
    "riglib/config.py",
    'LINTER_ITEM_KEYS = {"tool", "role", "path", "content", "enabled"}',
    'LINTER_ITEM_KEYS = {"tool", "role", "path", "content", "source", "enabled"}\nLINTER_BUNDLE_KEYS = {"source", "target", "enabled"}',
)
regex(
    "riglib/config.py",
    r'def _validate_linters\(li: dict\[str, Any\]\) -> None:.*?\n\ndef _validate_string_list',
    '''def _validate_linters(li: dict[str, Any]) -> None:\n    """Validate reusable per-repo linter config files and directory bundles."""\n    if not isinstance(li, dict):\n        raise ConfigError("linters must be a mapping", schema_path="linters")\n    if not li:\n        return\n    for key in li:\n        if key not in {"enabled", "items", "bundles"}:\n            raise ConfigError(f"unknown linters key {key!r} (expected one of: bundles, enabled, items)", schema_path="linters")\n    _check_bool(li, "enabled", "linters.enabled")\n\n    items = li.get("items", {})\n    if not isinstance(items, dict):\n        raise ConfigError("linters.items must be a mapping", schema_path="linters.items")\n    seen_paths: dict[str, str] = {}\n    for name, spec in items.items():\n        path = f"linters.items.{name}"\n        if not isinstance(spec, dict):\n            raise ConfigError(f"{path} must be a mapping", schema_path=path)\n        for key in spec:\n            if key not in LINTER_ITEM_KEYS:\n                raise ConfigError(f"unknown {path} key {key!r} (expected one of: {', '.join(sorted(LINTER_ITEM_KEYS))})", schema_path=path)\n        for req in ("tool", "path"):\n            val = spec.get(req)\n            if not isinstance(val, str) or not val:\n                raise ConfigError(f"{path}.{req} must be a non-empty string, got {val!r}", schema_path=f"{path}.{req}")\n        has_content = isinstance(spec.get("content"), str) and bool(spec.get("content"))\n        has_source = isinstance(spec.get("source"), str) and bool(spec.get("source"))\n        if has_content == has_source:\n            raise ConfigError(f"{path} must set exactly one of content or source", schema_path=path)\n        for key in ("path", "source"):\n            rel = spec.get(key)\n            if rel is None:\n                continue\n            if not isinstance(rel, str) or not rel or rel != rel.strip() or linter_path_escapes_repo(rel):\n                raise ConfigError(f"{path}.{key} must be a safe non-empty relative path", schema_path=f"{path}.{key}")\n        role = spec.get("role")\n        if role is not None and (not isinstance(role, str) or role not in _VALID_LINTER_ROLES):\n            raise ConfigError(f"{path}.role must be one of {sorted(_VALID_LINTER_ROLES)}, got {role!r}", schema_path=f"{path}.role")\n        _check_bool(spec, "enabled", f"{path}.enabled")\n        if spec.get("enabled") is not False:\n            norm = PurePosixPath(spec["path"]).as_posix()\n            if norm in seen_paths:\n                raise ConfigError(f"{path}.path {spec['path']!r} is already provisioned by linters.items.{seen_paths[norm]}", schema_path=f"{path}.path")\n            seen_paths[norm] = str(name)\n\n    bundles = li.get("bundles", {})\n    if not isinstance(bundles, dict):\n        raise ConfigError("linters.bundles must be a mapping", schema_path="linters.bundles")\n    seen_targets: dict[str, str] = {}\n    for name, spec in bundles.items():\n        path = f"linters.bundles.{name}"\n        if not isinstance(spec, dict):\n            raise ConfigError(f"{path} must be a mapping", schema_path=path)\n        for key in spec:\n            if key not in LINTER_BUNDLE_KEYS:\n                raise ConfigError(f"unknown {path} key {key!r} (expected one of: {', '.join(sorted(LINTER_BUNDLE_KEYS))})", schema_path=path)\n        for key in ("source", "target"):\n            rel = spec.get(key)\n            if not isinstance(rel, str) or not rel or rel != rel.strip() or linter_path_escapes_repo(rel):\n                raise ConfigError(f"{path}.{key} must be a safe non-empty relative path", schema_path=f"{path}.{key}")\n        _check_bool(spec, "enabled", f"{path}.enabled")\n        if spec.get("enabled") is not False:\n            norm = PurePosixPath(spec["target"]).as_posix()\n            if norm in seen_targets:\n                raise ConfigError(f"{path}.target {spec['target']!r} is already provisioned by linters.bundles.{seen_targets[norm]}", schema_path=f"{path}.target")\n            seen_targets[norm] = str(name)\n\n\ndef _validate_string_list''',
)

# plan: catalog-backed source files and bundle actions.
replace("riglib/plan.py", "_build_linters(config, plan)", "_build_linters(config, catalog, plan)")
regex(
    "riglib/plan.py",
    r'def _build_linters\(config: LoadedConfig, plan: InstallPlan\) -> None:.*?\n\ndef _build_global_excludes',
    '''def _build_linters(config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:\n    """Plan reusable linter config files plus vendored directory bundles."""\n    from .linter_carriers import read_source_text\n\n    li = config.data.get("linters") or {}\n    if not isinstance(li, dict) or li.get("enabled") is False:\n        return\n    items = li.get("items", {})\n    if isinstance(items, dict):\n        for name, spec in items.items():\n            if not isinstance(spec, dict) or spec.get("enabled") is False:\n                continue\n            rel_path = spec.get("path")\n            if not isinstance(rel_path, str) or not rel_path:\n                continue\n            if spec.get("source"):\n                try:\n                    content = read_source_text(catalog.source, str(spec["source"]))\n                except ValueError as exc:\n                    raise PlanError(f"linters.items.{name}: {exc}") from exc\n            else:\n                content = spec.get("content")\n            if not isinstance(content, str) or not content:\n                continue\n            plan.actions.append(Action(\n                kind="provision_linter_config", category="linters", item=str(name),\n                source=catalog.source, target=config.repo_root,\n                options={"tool": str(spec.get("tool") or ""), "role": str(spec.get("role") or "linter"), "rel_path": rel_path, "content": content},\n            ))\n    bundles = li.get("bundles", {})\n    if isinstance(bundles, dict):\n        for name, spec in bundles.items():\n            if not isinstance(spec, dict) or spec.get("enabled") is False:\n                continue\n            source_rel, target_rel = spec.get("source"), spec.get("target")\n            if not isinstance(source_rel, str) or not isinstance(target_rel, str):\n                continue\n            plan.actions.append(Action(\n                kind="provision_linter_bundle", category="linters", item=str(name),\n                source=catalog.source, target=config.repo_root,\n                options={"source_rel": source_rel, "target_rel": target_rel},\n            ))\n\n\ndef _build_global_excludes''',
)

# runner handler.
replace(
    "riglib/actions/runner.py",
    '\n\ndef _do_provision_project_tool(action: Action, on_conflict: str) -> ActionResult:',
    '''\n\ndef _do_provision_linter_bundle(action: Action, on_conflict: str) -> ActionResult:\n    from ..linter_carriers import apply_bundle\n    source_rel = str(action.options.get("source_rel") or "")\n    target_rel = str(action.options.get("target_rel") or "")\n    if not source_rel or not target_rel:\n        return ActionResult(action, "error", "linter-bundle: malformed action")\n    out = apply_bundle(action.source, action.target, source_rel, target_rel, on_conflict)\n    return ActionResult(action, out.status, f"linter-bundle ({action.item}): {out.detail}", out.backup)\n\n\ndef _do_provision_project_tool(action: Action, on_conflict: str) -> ActionResult:''',
)
replace(
    "riglib/actions/runner.py",
    '    "provision_linter_config": _do_provision_linter_config,',
    '    "provision_linter_config": _do_provision_linter_config,\n    "provision_linter_bundle": _do_provision_linter_bundle,',
)

# drift.
replace("riglib/drift.py", "from . import project_tools", "from . import project_tools, linter_carriers")
replace(
    "riglib/drift.py",
    '        elif action.kind == "provision_linter_config":\n            _check_linter_config(action, report)',
    '        elif action.kind == "provision_linter_config":\n            _check_linter_config(action, report)\n        elif action.kind == "provision_linter_bundle":\n            _check_linter_bundle(action, report)',
)
replace(
    "riglib/drift.py",
    '\n\ndef _check_project_tool(action: Action, report: DriftReport) -> None:',
    '''\n\ndef _check_linter_bundle(action: Action, report: DriftReport) -> None:\n    source_rel = str(action.options.get("source_rel") or "")\n    target_rel = str(action.options.get("target_rel") or "")\n    r = linter_carriers.resolve_bundle(action.source, action.target, source_rel, target_rel)\n    target = action.target / target_rel\n    if r.state == "ok":\n        return\n    direction = "missing" if r.state == "create" else "modified"\n    report.items.append(DriftItem(direction, "linters", f"bundle {action.item}", target, r.detail or "bundle differs from source"))\n\n\ndef _check_project_tool(action: Action, report: DriftReport) -> None:''',
)

print("linter carrier patch applied")
