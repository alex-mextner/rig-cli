"""Bulk config mutation for registered repositories.

Repository-local changes are independent and may run in parallel. A global policy change is
workspace-wide by definition: it requires ``--global --all-repos``, previews the prospective
inherited change against every active registered repository, writes the global layer once only if
all previews validate, then reconciles the same repository set. This prevents a stack/tag selector
from accidentally becoming a machine-global policy mutation.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .fleet import DEFAULT_JOBS, FleetReport, FleetResult, execute
from .repository_registry import RegistryError, RepositoryEntry, RepositoryRegistry


@dataclass
class GlobalMutationReport:
    path: str
    value: str
    preview: FleetReport
    write: FleetResult | None = None
    reconcile: FleetReport | None = None

    @property
    def ok(self) -> bool:
        return (
            self.preview.ok
            and (self.write is None or self.write.ok)
            and (self.reconcile is None or self.reconcile.ok)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": "global",
            "path": self.path,
            "value": self.value,
            "ok": self.ok,
            "preview": self.preview.to_dict(),
            "write": self.write.to_dict() if self.write else None,
            "reconcile": self.reconcile.to_dict() if self.reconcile else None,
        }


def _config_command(
    repo: RepositoryEntry,
    path: str,
    value: str,
    *,
    commit: bool,
    is_global: bool = False,
    no_apply: bool = False,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "riglib",
        "config",
        "set",
        path,
        value,
        "-C",
        repo.path,
    ]
    if is_global:
        command.append("--global")
    if commit:
        command.append("--commit")
    if no_apply:
        command.append("--no-apply")
    return command


def _run_config(
    repo: RepositoryEntry,
    *,
    path: str,
    value: str,
    commit: bool,
    is_global: bool = False,
    no_apply: bool = False,
    timeout_s: float | None = None,
) -> FleetResult:
    command = _config_command(
        repo,
        path,
        value,
        commit=commit,
        is_global=is_global,
        no_apply=no_apply,
    )
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        code = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        note = f"fleet config timeout after {timeout_s:g}s"
        stderr = f"{stderr.rstrip()}\n{note}\n" if stderr else f"{note}\n"
    except OSError as exc:
        code, stdout, stderr = 127, "", f"fleet config could not execute rig: {exc}\n"
    scope = "global" if is_global else "repo"
    mode = "commit" if commit or no_apply else "info"
    return FleetResult(
        repository_id=repo.id,
        name=repo.name,
        path=repo.path,
        operation=f"config set {scope} {mode}",
        exit_code=code,
        duration_s=round(time.monotonic() - started, 3),
        stdout=stdout,
        stderr=stderr,
    )


def execute_repo_config(
    repositories: Sequence[RepositoryEntry],
    *,
    path: str,
    value: str,
    commit: bool,
    jobs: int = DEFAULT_JOBS,
    timeout_s: float | None = None,
) -> FleetReport:
    if jobs < 1:
        raise ValueError("jobs must be >= 1")
    if timeout_s is not None and timeout_s <= 0:
        raise ValueError("timeout must be > 0")
    ordered = sorted(repositories, key=lambda repo: (repo.path, repo.id))
    if not ordered:
        return FleetReport("config-set", "commit" if commit else "info", [], [])
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(jobs, len(ordered))) as pool:
        futures = {
            pool.submit(
                _run_config,
                repo,
                path=path,
                value=value,
                commit=commit,
                timeout_s=timeout_s,
            ): (index, repo)
            for index, repo in enumerate(ordered)
        }
        by_index: dict[int, FleetResult] = {}
        for future in concurrent.futures.as_completed(futures):
            index, repo = futures[future]
            try:
                by_index[index] = future.result()
            except Exception as exc:  # defensive collect-and-continue boundary
                by_index[index] = FleetResult(
                    repo.id,
                    repo.name,
                    repo.path,
                    "config set repo",
                    1,
                    0.0,
                    "",
                    f"fleet config worker failed: {exc}\n",
                )
    return FleetReport(
        "config-set",
        "commit" if commit else "info",
        ordered,
        [by_index[index] for index in range(len(ordered))],
    )


def execute_global_config(
    repositories: Sequence[RepositoryEntry],
    *,
    path: str,
    value: str,
    commit: bool,
    jobs: int = DEFAULT_JOBS,
    timeout_s: float | None = None,
) -> GlobalMutationReport:
    """Preflight a global mutation everywhere, then write once and reconcile everywhere."""
    ordered = sorted(repositories, key=lambda repo: (repo.path, repo.id))
    preview_results = []
    if ordered:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(jobs, len(ordered))) as pool:
            futures = {
                pool.submit(
                    _run_config,
                    repo,
                    path=path,
                    value=value,
                    commit=False,
                    is_global=True,
                    timeout_s=timeout_s,
                ): (index, repo)
                for index, repo in enumerate(ordered)
            }
            by_index: dict[int, FleetResult] = {}
            for future in concurrent.futures.as_completed(futures):
                index, repo = futures[future]
                try:
                    by_index[index] = future.result()
                except Exception as exc:
                    by_index[index] = FleetResult(
                        repo.id,
                        repo.name,
                        repo.path,
                        "config set global info",
                        1,
                        0.0,
                        "",
                        f"fleet global preview worker failed: {exc}\n",
                    )
            preview_results = [by_index[index] for index in range(len(ordered))]
    preview = FleetReport("config-set-global", "info", ordered, preview_results)
    report = GlobalMutationReport(path=path, value=value, preview=preview)
    if not commit or not preview.ok or not ordered:
        return report

    # Write the machine-global layer exactly once. --no-apply is intentional here: all selected
    # repositories were already preflighted against this exact prospective value; reconciliation
    # happens as the next explicit fleet stage rather than letting the anchor repo run twice.
    anchor = ordered[0]
    report.write = _run_config(
        anchor,
        path=path,
        value=value,
        commit=False,
        is_global=True,
        no_apply=True,
        timeout_s=timeout_s,
    )
    if not report.write.ok:
        return report
    report.reconcile = execute(
        ordered,
        operation="apply",
        mode="commit",
        jobs=jobs,
        timeout_s=timeout_s,
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rig fleet config",
        description="preview or commit one config change across selected registered repositories",
    )
    parser.add_argument("--registry", type=Path, default=None)
    sub = parser.add_subparsers(dest="config_command", required=True)
    set_cmd = sub.add_parser("set")
    set_cmd.add_argument("path")
    set_cmd.add_argument("value")
    set_cmd.add_argument("--repo", action="append", default=[])
    set_cmd.add_argument("--stack", action="append", default=[])
    set_cmd.add_argument("--tag", action="append", default=[])
    set_cmd.add_argument("--root", action="append", default=[])
    set_cmd.add_argument("--all-repos", action="store_true")
    set_cmd.add_argument("--global", dest="is_global", action="store_true")
    set_cmd.add_argument("--commit", action="store_true")
    set_cmd.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    set_cmd.add_argument("--timeout", type=float, default=None)
    set_cmd.add_argument("--json", action="store_true")
    return parser


def _has_selector(args: argparse.Namespace) -> bool:
    return bool(args.repo or args.stack or args.tag or args.root)


def _print_fleet_report(report: FleetReport) -> None:
    for result in report.results:
        marker = "OK" if result.ok else f"FAIL({result.exit_code})"
        print(f"[{marker}] {result.name} — {result.path}")
        for line in result.stdout.rstrip().splitlines():
            print(f"  {line}")
        for line in result.stderr.rstrip().splitlines():
            print(f"  stderr: {line}")
    summary = report.summary()
    print(
        f"Fleet config summary: selected={summary['selected']} "
        f"succeeded={summary['succeeded']} failed={summary['failed']}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.config_command != "set":
        return 2
    if not args.all_repos and not _has_selector(args):
        print(
            "error: fleet config mutation requires a selector or explicit --all-repos",
            file=sys.stderr,
        )
        return 2
    if args.all_repos and _has_selector(args):
        print("error: --all-repos cannot be combined with repo/stack/tag/root selectors", file=sys.stderr)
        return 2
    if args.is_global and not args.all_repos:
        print(
            "error: --global is workspace-wide; use it only with explicit --all-repos",
            file=sys.stderr,
        )
        return 2
    try:
        registry = RepositoryRegistry.load(args.registry)
        selected = registry.select(
            repos=args.repo,
            stacks=args.stack,
            tags=args.tag,
            roots=args.root,
            include_stale=False,
        )
        if args.all_repos:
            selected = registry.select(include_stale=False)
        if not selected:
            print("error: fleet config selection matched no active repositories", file=sys.stderr)
            return 2
        if args.is_global:
            report = execute_global_config(
                selected,
                path=args.path,
                value=args.value,
                commit=args.commit,
                jobs=args.jobs,
                timeout_s=args.timeout,
            )
            if args.json:
                print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
            else:
                print("Global policy preflight:")
                _print_fleet_report(report.preview)
                if report.write is not None:
                    print(f"Global write: {'OK' if report.write.ok else 'FAIL'}")
                if report.reconcile is not None:
                    print("Reconcile after global write:")
                    _print_fleet_report(report.reconcile)
                if not args.commit:
                    print("preview only — add --commit to write global policy once and reconcile all repos")
            return 0 if report.ok else 1

        report = execute_repo_config(
            selected,
            path=args.path,
            value=args.value,
            commit=args.commit,
            jobs=args.jobs,
            timeout_s=args.timeout,
        )
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        else:
            _print_fleet_report(report)
            if not args.commit:
                print("preview only — add --commit with the same selectors to write + reconcile")
        return 0 if report.ok else 1
    except (RegistryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
