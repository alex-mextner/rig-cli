"""Detect whether a repository is ready for Rig-managed Oxc policy.

Stdlib-only. The gate is deliberately repository-local and reproducible: a globally installed
binary is not enough for a committed generated config whose imports/scripts must also work in CI.
This module does not mutate package manifests; it produces an actionable gate and a copy-ready
migration prompt for a human or coding agent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FOREIGN_LINTER_PACKAGES = {
    "eslint": "ESLint",
    "@biomejs/biome": "Biome",
    "standard": "StandardJS",
    "xo": "XO",
    "tslint": "TSLint",
}
_FOREIGN_CONFIG_GLOBS = {
    "ESLint": ("eslint.config.*", ".eslintrc", ".eslintrc.*"),
    "Biome": ("biome.json", "biome.jsonc"),
    "TSLint": ("tslint.json",),
}
_OXC_PACKAGES = ("oxlint", "oxfmt", "@oxlint/plugins")


@dataclass(frozen=True)
class LinterEnvironment:
    ready: bool
    oxlint_present: bool
    foreign_linters: tuple[str, ...]
    missing_oxc_packages: tuple[str, ...]
    reason: str
    agent_prompt: str


def _package_manifest(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "package.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _declared_packages(manifest: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        block = manifest.get(key)
        if isinstance(block, dict):
            out.update(str(name) for name in block)
    return out


def _script_text(manifest: dict[str, Any]) -> str:
    scripts = manifest.get("scripts")
    if not isinstance(scripts, dict):
        return ""
    return "\n".join(value for value in scripts.values() if isinstance(value, str)).lower()


def _foreign_from_files(repo_root: Path) -> set[str]:
    found: set[str] = set()
    for label, patterns in _FOREIGN_CONFIG_GLOBS.items():
        if any(any(repo_root.glob(pattern)) for pattern in patterns):
            found.add(label)
    return found


def _foreign_from_scripts(manifest: dict[str, Any]) -> set[str]:
    text = _script_text(manifest)
    found: set[str] = set()
    token_map = {
        "eslint": "ESLint",
        "biome": "Biome",
        "standard": "StandardJS",
        "xo": "XO",
        "tslint": "TSLint",
    }
    for token, label in token_map.items():
        if token in text:
            found.add(label)
    return found


def _prompt(*, foreign: tuple[str, ...], missing: tuple[str, ...]) -> str:
    foreign_text = ", ".join(foreign) if foreign else "no existing linter"
    missing_text = ", ".join(missing) if missing else "none"
    return (
        "Prepare this repository for Rig-managed Oxc lint policy. "
        f"Current lint environment: {foreign_text}. Missing Oxc packages: {missing_text}. "
        "Install compatible oxlint, oxfmt, and @oxlint/plugins as repository development "
        "dependencies using the existing package manager; do not rely on a globally installed "
        "binary because CI and other developers must reproduce the toolchain. Migrate lint/format "
        "scripts and CI from any foreign linter to Oxc without deleting project-specific semantics; "
        "remove obsolete linter configuration only after its behavior is represented; run the "
        "repository tests and lint; then run `rig apply` again. Do not hand-author "
        "oxlint.config.ts: Rig owns that file and its rule policy comes from global Rig config plus "
        "rig.yaml."
    )


def inspect_linter_environment(repo_root: Path) -> LinterEnvironment:
    """Return the reproducible Oxc readiness gate for one repository."""
    manifest = _package_manifest(repo_root)
    packages = _declared_packages(manifest)
    # A PATH-global `oxlint` is intentionally NOT sufficient: the committed generated config and
    # package scripts have to work on a clean CI/developer checkout too.
    oxlint_present = "oxlint" in packages
    foreign_set = {label for pkg, label in _FOREIGN_LINTER_PACKAGES.items() if pkg in packages}
    foreign_set.update(_foreign_from_files(repo_root))
    foreign_set.update(_foreign_from_scripts(manifest))
    foreign = tuple(sorted(foreign_set))
    missing = tuple(pkg for pkg in _OXC_PACKAGES if pkg not in packages)

    if oxlint_present:
        return LinterEnvironment(
            ready=True,
            oxlint_present=True,
            foreign_linters=foreign,
            missing_oxc_packages=missing,
            reason="Repository-local Oxlint is declared; Rig can apply the rule policy.",
            agent_prompt=_prompt(foreign=foreign, missing=missing),
        )

    if foreign:
        reason = (
            "Rig rule policy is blocked because this repository uses "
            + ", ".join(foreign)
            + " but repository-local Oxlint is not declared."
        )
    else:
        reason = (
            "Rig rule policy is blocked because no repository-local Oxlint dependency was detected."
        )
    return LinterEnvironment(
        ready=False,
        oxlint_present=False,
        foreign_linters=foreign,
        missing_oxc_packages=missing,
        reason=reason,
        agent_prompt=_prompt(foreign=foreign, missing=missing),
    )
