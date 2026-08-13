"""Effective lint-rule introspection for `rig lint rules`."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import load
from .detect import detect_environment
from .lint_policy import RULES, resolve_rule_severities

def _reason(name: str, spec, cfg: dict[str, Any]) -> str:
    severity = cfg.get("severity") if isinstance(cfg.get("severity"), dict) else {}
    if name in severity:
        return "explicit severity"
    disabled = cfg.get("disable") if isinstance(cfg.get("disable"), list) else []
    if name in disabled:
        return "explicit disable"
    enabled = cfg.get("enable") if isinstance(cfg.get("enable"), list) else []
    if name in enabled:
        return "explicit enable"
    groups = cfg.get("groups") if isinstance(cfg.get("groups"), dict) else {}
    if spec.group in groups:
        return f"group {spec.group}={str(groups[spec.group]).lower()}"
    if cfg.get("all") is True:
        return "all=true"
    return "built-in default"

def rows(cwd: str, config_path: str | None = None) -> list[dict[str, str]]:
    env = detect_environment(Path(cwd).resolve())
    explicit = None
    if config_path:
        cp = Path(config_path)
        explicit = (cp if cp.is_absolute() else env.repo_root / cp).resolve()
    loaded = load(env.repo_root, explicit_config=explicit, include_repo=env.is_git_repo or explicit is not None)
    linters = loaded.data.get("linters") if isinstance(loaded.data.get("linters"), dict) else {}
    cfg = linters.get("rules") if isinstance(linters.get("rules"), dict) else {}
    effective = resolve_rule_severities(cfg)
    return [
        {
            "rule": spec.name,
            "effective": effective[spec.name],
            "default": spec.default,
            "all": spec.all_severity,
            "group": spec.group,
            "provider": spec.provider,
            "reason": _reason(spec.name, spec, cfg),
        }
        for spec in RULES
    ]

def run(args) -> int:
    if getattr(args, "lint_command", None) != "rules":
        args._lint_parser.print_help()
        return 0
    data = rows(args.cwd, args.config)
    if args.rule:
        data = [row for row in data if row["rule"] == args.rule]
        if not data:
            print(f"unknown lint rule: {args.rule}")
            return 4
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    if not data:
        return 0
    print("Effective Rig lint policy (built-in defaults < groups/all < enable/disable < severity).")
    print("Change it in global Rig config or rig.yaml; generated Oxlint files are outputs, not policy inputs.\n")
    widths = {key: max(len(key), *(len(row[key]) for row in data)) for key in ("effective", "default", "all", "group", "provider")}
    for row in data:
        print(
            f"{row['effective']:<{widths['effective']}}  {row['rule']}  "
            f"[default={row['default']:<{widths['default']}} all={row['all']:<{widths['all']}} "
            f"group={row['group']:<{widths['group']}} provider={row['provider']:<{widths['provider']}}]  {row['reason']}"
        )
    return 0
