"""Resolve Rig-owned lint policy into generated Oxlint configuration.

The policy source is Rig configuration, not ``oxlint.config.ts``. The config loader already
cascades built-in defaults < global ``~/.config/rig/config.yaml`` < repository ``rig.yaml``;
this module resolves the merged ``linters.rules`` block into deterministic rule severities and
renders the generated Oxc config.

Stdlib-only at import time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

Severity = str
VALID_SEVERITIES = {"off", "warn", "error"}


@dataclass(frozen=True)
class RuleSpec:
    name: str
    group: str
    default: Severity
    all_severity: Severity
    provider: str = "oxlint"


# Defaults are policy, not a mirror of the plugin's inventory. `all_severity` answers the stronger
# explicit request `all: true`: even intentionally off-by-default rules become enabled.
RULES: tuple[RuleSpec, ...] = (
    RuleSpec("typescript/no-unsafe-type-assertion", "typescript-core", "error", "error"),
    RuleSpec("typescript/no-unnecessary-type-assertion", "typescript-core", "error", "error"),
    RuleSpec("typescript/no-non-null-assertion", "typescript-core", "error", "error"),
    RuleSpec("typescript/ban-ts-comment", "typescript-core", "error", "error"),
    RuleSpec("anti-slop/no-chained-type-assertions", "anti-slop", "error", "error", "anti-slop"),
    RuleSpec("anti-slop/no-known-value-widening", "anti-slop", "error", "error", "anti-slop"),
    RuleSpec("anti-slop/no-widen-then-assert", "anti-slop", "error", "error", "anti-slop"),
    RuleSpec("anti-slop/no-unsafe-dictionary-type", "anti-slop", "error", "error", "anti-slop"),
    RuleSpec("anti-slop/require-safety-comment-for-type-assertion", "anti-slop", "error", "error", "anti-slop"),
    RuleSpec("anti-slop/no-object-parameters", "anti-slop", "error", "error", "anti-slop"),
    RuleSpec("anti-slop/no-unknown-type-aliases", "anti-slop", "error", "error", "anti-slop"),
    RuleSpec("anti-slop/no-unknown-returns", "anti-slop", "error", "error", "anti-slop"),
    RuleSpec("anti-slop/no-reflect-get", "anti-slop", "warn", "error", "anti-slop"),
    RuleSpec("anti-slop/no-reflect-apply", "anti-slop", "warn", "error", "anti-slop"),
    RuleSpec("anti-slop/no-module-mocking", "anti-slop", "warn", "error", "anti-slop"),
    RuleSpec("anti-slop/no-unknown-parameters", "anti-slop", "warn", "error", "anti-slop"),
    RuleSpec("anti-slop/no-runtime-typeof", "anti-slop", "off", "error", "anti-slop"),
    RuleSpec("anti-slop/no-conditional-empty-object-spread", "anti-slop", "off", "error", "anti-slop"),
    RuleSpec("anti-slop/no-shape-in-symbol-names", "anti-slop", "off", "error", "anti-slop"),
    RuleSpec("anti-slop/no-multiple-function-params", "anti-slop", "off", "error", "anti-slop"),
    RuleSpec("anti-slop/no-optional-function-parameters", "anti-slop", "off", "error", "anti-slop"),
)

RULE_BY_NAME = {rule.name: rule for rule in RULES}
GROUPS = tuple(dict.fromkeys(rule.group for rule in RULES))


def _string_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError("lint rule enable/disable values must be arrays of non-empty rule names")
    return tuple(value)


def _validate_known(names: tuple[str, ...], field: str) -> None:
    unknown = sorted(set(names) - set(RULE_BY_NAME))
    if unknown:
        raise ValueError(f"unknown lint rule(s) in {field}: {', '.join(unknown)}")


def resolve_rule_severities(rules_config: Mapping[str, Any] | None = None) -> dict[str, Severity]:
    """Resolve one already-cascaded ``linters.rules`` mapping.

    Resolution order is intentionally explicit:

    1. built-in per-rule defaults;
    2. ``groups`` toggles;
    3. ``all: true`` (enable every known rule at its strict ``all_severity``);
    4. ``enable`` / ``disable``;
    5. explicit ``severity`` mapping (final authority).

    ``enable`` restores a rule to its normal default when non-off, otherwise to ``warn``. This
    makes enabling an opinionated off-by-default rule useful without unexpectedly turning it into
    a blocking error. ``all: true`` is deliberately stronger and enables everything.
    """
    cfg: Mapping[str, Any] = rules_config or {}
    severities = {rule.name: rule.default for rule in RULES}

    groups = cfg.get("groups", {})
    if groups is None:
        groups = {}
    if not isinstance(groups, Mapping):
        raise ValueError("linters.rules.groups must be a mapping of group name to bool")
    unknown_groups = sorted(set(groups) - set(GROUPS))
    if unknown_groups:
        raise ValueError(f"unknown lint rule group(s): {', '.join(unknown_groups)}")
    for group, enabled in groups.items():
        if not isinstance(enabled, bool):
            raise ValueError(f"linters.rules.groups.{group} must be a bool")
        if not enabled:
            for rule in RULES:
                if rule.group == group:
                    severities[rule.name] = "off"
        else:
            for rule in RULES:
                if rule.group == group:
                    severities[rule.name] = rule.default

    all_rules = cfg.get("all", False)
    if not isinstance(all_rules, bool):
        raise ValueError("linters.rules.all must be a bool")
    if all_rules:
        severities = {rule.name: rule.all_severity for rule in RULES}

    enable = _string_list(cfg.get("enable"))
    disable = _string_list(cfg.get("disable"))
    _validate_known(enable, "linters.rules.enable")
    _validate_known(disable, "linters.rules.disable")
    for name in enable:
        spec = RULE_BY_NAME[name]
        severities[name] = spec.default if spec.default != "off" else "warn"
    for name in disable:
        severities[name] = "off"

    explicit = cfg.get("severity", {})
    if explicit is None:
        explicit = {}
    if not isinstance(explicit, Mapping):
        raise ValueError("linters.rules.severity must be a mapping of rule name to off|warn|error")
    unknown = sorted(set(explicit) - set(RULE_BY_NAME))
    if unknown:
        raise ValueError(f"unknown lint rule(s) in linters.rules.severity: {', '.join(unknown)}")
    for name, severity in explicit.items():
        if severity not in VALID_SEVERITIES:
            raise ValueError(f"linters.rules.severity.{name} must be off, warn, or error")
        severities[name] = str(severity)

    return severities


def anti_slop_required(severities: Mapping[str, Severity]) -> bool:
    return any(
        severity != "off" and RULE_BY_NAME.get(name, RuleSpec(name, "", "off", "off")).provider == "anti-slop"
        for name, severity in severities.items()
    )


def render_oxlint_config(rules_config: Mapping[str, Any] | None = None) -> str:
    """Render deterministic ``oxlint.config.ts`` from Rig policy."""
    severities = resolve_rule_severities(rules_config)
    lines = [
        "// Managed by Rig. Source of truth: global Rig config + repository rig.yaml.",
        "// Do not edit directly: `rig apply` reconciles this file. Temporary diagnostic edits are allowed locally,",
        "// but move the final change into Rig policy before commit.",
        "",
        'import { defineConfig } from "oxlint";',
        "",
        "export default defineConfig({",
        "  options: { typeAware: true },",
    ]
    if anti_slop_required(severities):
        lines.extend(
            [
                "  jsPlugins: [",
                '    { name: "anti-slop", specifier: "./tools/oxlint/anti-slop/index.ts" },',
                "  ],",
            ]
        )
    lines.extend(
        [
            '  categories: { correctness: "error", suspicious: "error", perf: "error" },',
            "  rules: {",
        ]
    )
    for name in sorted(severities):
        severity = severities[name]
        if name == "typescript/ban-ts-comment" and severity != "off":
            lines.extend(
                [
                    f'    "{name}": [',
                    f'      "{severity}",',
                    "      {",
                    '        "ts-ignore": true,',
                    '        "ts-nocheck": true,',
                    '        "ts-expect-error": "allow-with-description",',
                    "        minimumDescriptionLength: 8,",
                    "      },",
                    "    ],",
                ]
            )
        else:
            lines.append(f'    {json.dumps(name)}: {json.dumps(severity)},')
    lines.extend(["  },", "});", ""])
    return "\n".join(lines)


def rule_policy_summary(rules_config: Mapping[str, Any] | None = None) -> dict[str, int]:
    severities = resolve_rule_severities(rules_config)
    return {
        "error": sum(value == "error" for value in severities.values()),
        "warn": sum(value == "warn" for value in severities.values()),
        "off": sum(value == "off" for value in severities.values()),
    }
