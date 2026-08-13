#!/usr/bin/env python3
"""One-shot follow-up integration for Rig-owned lint policy readiness and managed headers."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def regex_once(text: str, pattern: str, replacement: str, label: str) -> str:
    out, count = re.subn(pattern, lambda _m: replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, got {count}")
    return out


def patch_plan() -> None:
    path = "riglib/plan.py"
    text = read(path)
    replacement = '''def _build_linters(config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:
    """Plan generic config carriers plus Rig-owned Oxc rule policy.

    Explicit ``linters.items`` remain generic and can be provisioned regardless of the active lint
    engine. Rule policy is different: Rig only emits Oxc policy/plugin actions when the repository
    is ready to execute them. A foreign/no-linter repository gets one loud blocked action containing
    a copy-ready migration prompt while the rest of ``rig apply`` can continue.
    """
    li = config.data.get("linters")
    if li is None:
        li = {}
    if not isinstance(li, dict) or li.get("enabled") is False:
        return

    from .lint_policy import anti_slop_required, render_oxlint_config, resolve_rule_severities, rule_policy_summary
    from .linter_carriers import read_source_text
    from .linter_environment import inspect_linter_environment
    from .managed_config import with_managed_header

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
            source_backed = isinstance(source_rel, str) and bool(source_rel)
            if source_backed:
                try:
                    content = read_source_text(catalog.source, source_rel)
                except ValueError as exc:
                    raise PlanError(f"linters.items.{name}.source: {exc}") from exc
            if not isinstance(content, str) or not content:
                continue
            if source_backed:
                content = with_managed_header(
                    rel_path,
                    content,
                    source=f"agent-tools/{source_rel} + rig.yaml selection",
                )
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
    summary = rule_policy_summary(rules_cfg)
    plan.notes.append(
        "linters: effective rule policy — "
        f"{summary['error']} error, {summary['warn']} warn, {summary['off']} off"
    )

    env = inspect_linter_environment(config.repo_root)
    blocked_reason = ""
    if not env.ready:
        blocked_reason = env.reason
    elif anti_slop_required(severities) and "@oxlint/plugins" in env.missing_oxc_packages:
        blocked_reason = (
            "Rig anti-slop policy is blocked because @oxlint/plugins is not declared in this "
            "repository; the generated local plugin cannot execute reliably without it."
        )
    if blocked_reason:
        plan.actions.append(
            Action(
                kind="lint_policy_blocked",
                category="linters",
                item="rules",
                source=catalog.source,
                target=config.repo_root,
                options={"reason": blocked_reason, "agent_prompt": env.agent_prompt},
            )
        )
        return

    if env.foreign_linters:
        plan.notes.append(
            "linters: Oxlint is available alongside "
            + ", ".join(env.foreign_linters)
            + "; Rig applies Oxc policy but the repository should avoid duplicate/conflicting lint scripts"
        )

    generated = render_oxlint_config(rules_cfg)
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
                "rig_owned": True,
            },
        )
    )

    if anti_slop_required(severities):
        source_root = catalog.source / "vendor" / "anti-slop" / "src"
        if not source_root.is_dir():
            raise PlanError(
                "anti-slop rules are enabled but vendor/anti-slop/src is missing; initialize/update "
                "the pinned agent-tools submodule"
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
                        "rig_owned": True,
                    },
                )
            )


'''
    text = regex_once(
        text,
        r"def _build_linters\(config: LoadedConfig, catalog: Catalog, plan: InstallPlan\) -> None:.*?(?=def _build_global_excludes)",
        replacement,
        "plan._build_linters",
    )
    write(path, text)


def patch_runner() -> None:
    path = "riglib/actions/runner.py"
    text = read(path)
    marker = "\ndef _do_provision_linter_config(action: Action, on_conflict: str) -> ActionResult:\n"
    if marker not in text:
        raise RuntimeError("runner linter handler marker missing")
    blocked = '''\ndef _do_lint_policy_blocked(action: Action, on_conflict: str) -> ActionResult:
    """Surface a non-mutating lint-policy readiness failure while allowing other Rig areas to run."""
    reason = str(action.options.get("reason") or "Oxc lint policy prerequisites are not satisfied")
    prompt = str(action.options.get("agent_prompt") or "").strip()
    detail = f"lint-policy: BLOCKED — {reason}"
    if prompt:
        detail += f"\\n\\nReady-to-copy migration prompt:\\n{prompt}"
    return ActionResult(action, "error", detail)

'''
    text = text.replace(marker, blocked + marker, 1)
    old = '    "provision_linter_config": _do_provision_linter_config,\n'
    new = '    "lint_policy_blocked": _do_lint_policy_blocked,\n' + old
    if text.count(old) != 1:
        raise RuntimeError("handler-map linter entry missing/duplicate")
    text = text.replace(old, new, 1)
    write(path, text)


def main() -> None:
    patch_plan()
    patch_runner()
    print("lint policy follow-up integrated")


if __name__ == "__main__":
    main()
