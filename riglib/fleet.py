"""Fleet operations over Rig's machine-local repository registry.

Fleet deliberately reuses the single-repository CLI contract rather than duplicating plan/apply
semantics. Each selected repository runs in its own child Python process, which gives isolation
for cwd, environment, stdout, config loading, and failure handling. Preview remains preview: fleet
`apply` calls `rig apply info`; mutation requires the explicit `commit` positional.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .repository_registry import RegistryError, RepositoryEntry, RepositoryRegistry, registry_path

DEFAULT_JOBS = min(4, max(1, os.cpu_count() or 1))


@dataclass(frozen=True)
class FleetResult:
    repository_id: str
    name: str
    path: str
    operation: str
    exit_code: int
    duration_s: float
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["ok"] = self.ok
        return data


@dataclass
class FleetReport:
    operation: str
    mode: str | None
    selected: list[RepositoryEntry]
    results: list[FleetResult]

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    @property
    def failed(self) -> list[FleetResult]:
        return [result for result in self.results if not result.ok]

    def summary(self) -> dict[str, int]:
        return {
            "selected": len(self.selected),
            "succeeded": sum(result.ok for result in self.results),
            "failed": len(self.failed),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "mode": self.mode,
            "ok": self.ok,
            "summary": self.summary(),
            "repositories": [entry.to_dict() for entry in self.selected],
            "results": [result.to_dict() for result in self.results],
        }


def _rig_module_command(operation: str, mode: str | None, repo: RepositoryEntry) -> list[str]:
    cmd = [sys.executable, "-m", "riglib", operation]
    if operation == "apply":
        cmd.append(mode or "info")
    cmd.extend(["-C", repo.path])
    return cmd


def _run_repository(
    repo: RepositoryEntry,
    *,
    operation: str,
    mode: str | None,
    timeout_s: float | None,
) -> FleetResult:
    command = _rig_module_command(operation, mode, repo)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        code = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        code = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        suffix = f"fleet timeout after {timeout_s:g}s" if timeout_s is not None else "fleet timeout"
        stderr = f"{stderr.rstrip()}\n{suffix}\n" if stderr else f"{suffix}\n"
    except OSError as exc:
        code = 127
        stdout = ""
        stderr = f"fleet could not execute rig for {repo.path}: {exc}\n"
    return FleetResult(
        repository_id=repo.id,
        name=repo.name,
        path=repo.path,
        operation=operation if mode is None else f"{operation} {mode}",
        exit_code=code,
        duration_s=round(time.monotonic() - started, 3),
        stdout=stdout,
        stderr=stderr,
    )


def execute(
    repositories: Sequence[RepositoryEntry],
    *,
    operation: str,
    mode: str | None = None,
    jobs: int = DEFAULT_JOBS,
    timeout_s: float | None = None,
) -> FleetReport:
    """Run one Rig operation across selected repositories with bounded parallelism.

    Results are always returned in deterministic repository-path order even though execution may
    happen concurrently. One repository failing or timing out never prevents independent selected
    repositories from running.
    """
    if operation not in {"apply", "status"}:
        raise ValueError(f"unsupported fleet operation: {operation}")
    if operation == "apply" and mode not in {"info", "commit"}:
        raise ValueError("fleet apply mode must be 'info' or 'commit'")
    if operation == "status" and mode is not None:
        raise ValueError("fleet status does not accept a mode")
    if jobs < 1:
        raise ValueError("jobs must be >= 1")
    if timeout_s is not None and timeout_s <= 0:
        raise ValueError("timeout must be > 0")

    ordered = sorted(repositories, key=lambda repo: (repo.path, repo.id))
    if not ordered:
        return FleetReport(operation=operation, mode=mode, selected=[], results=[])

    worker_count = min(jobs, len(ordered))
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(
                _run_repository,
                repo,
                operation=operation,
                mode=mode,
                timeout_s=timeout_s,
            ): (index, repo)
            for index, repo in enumerate(ordered)
        }
        by_index: dict[int, FleetResult] = {}
        for future in concurrent.futures.as_completed(futures):
            index, repo = futures[future]
            try:
                by_index[index] = future.result()
            except Exception as exc:  # defensive: preserve collect-and-continue semantics
                by_index[index] = FleetResult(
                    repository_id=repo.id,
                    name=repo.name,
                    path=repo.path,
                    operation=operation if mode is None else f"{operation} {mode}",
                    exit_code=1,
                    duration_s=0.0,
                    stdout="",
                    stderr=f"fleet worker failed: {exc}\n",
                )
        results = [by_index[index] for index in range(len(ordered))]
    return FleetReport(operation=operation, mode=mode, selected=ordered, results=results)


def _select_from_args(registry: RepositoryRegistry, args: argparse.Namespace) -> list[RepositoryEntry]:
    return registry.select(
        repos=getattr(args, "repo", []) or [],
        stacks=getattr(args, "stack", []) or [],
        tags=getattr(args, "tag", []) or [],
        roots=getattr(args, "root", []) or [],
        include_stale=False,
    )


def _add_selectors(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", action="append", default=[], help="repo id, name, path, or remote")
    parser.add_argument("--stack", action="append", default=[], help="select stack/profile")
    parser.add_argument("--tag", action="append", default=[], help="select repository tag")
    parser.add_argument("--root", action="append", default=[], help="select repositories below root")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rig fleet",
        description="preview/apply Rig policy across registered repositories",
    )
    parser.add_argument("--registry", type=Path, default=None)
    sub = parser.add_subparsers(dest="fleet_command", required=True)

    discover = sub.add_parser("discover", help="preview or persist repository discovery")
    discover.add_argument("--root", action="append", required=True)
    discover.add_argument("--max-depth", type=int, default=5)
    discover.add_argument("--commit", action="store_true")
    discover.add_argument("--json", action="store_true")

    listing = sub.add_parser("list", help="list repositories matching fleet selectors")
    _add_selectors(listing)
    listing.add_argument("--include-stale", action="store_true")
    listing.add_argument("--json", action="store_true")

    tags = sub.add_parser("tags", help="set machine-local repository tags")
    tags_sub = tags.add_subparsers(dest="tags_command", required=True)
    tags_set = tags_sub.add_parser("set", help="replace tags for one registered repository")
    tags_set.add_argument("repository_id")
    tags_set.add_argument("tags", nargs="*")
    tags_set.add_argument("--commit", action="store_true")
    tags_set.add_argument("--json", action="store_true")

    apply_cmd = sub.add_parser("apply", help="preview or reconcile all selected repositories")
    apply_cmd.add_argument("mode", nargs="?", choices=("info", "commit"), default="info")
    _add_selectors(apply_cmd)
    apply_cmd.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    apply_cmd.add_argument("--timeout", type=float, default=None)
    apply_cmd.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="run rig status across selected repositories")
    _add_selectors(status)
    status.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    status.add_argument("--timeout", type=float, default=None)
    status.add_argument("--json", action="store_true")
    return parser


def _print_registry_entries(entries: Iterable[RepositoryEntry]) -> None:
    for entry in entries:
        stack = entry.stack or "-"
        tags = ",".join(entry.all_tags) or "-"
        stale = " stale" if entry.stale else ""
        print(f"{entry.id}  {entry.name}  stack={stack} tags={tags}{stale}  {entry.path}")


def _print_report(report: FleetReport) -> None:
    for result in report.results:
        marker = "OK" if result.ok else f"FAIL({result.exit_code})"
        print(f"[{marker}] {result.name} — {result.path} ({result.duration_s:.3f}s)")
        if result.stdout.strip():
            for line in result.stdout.rstrip().splitlines():
                print(f"  {line}")
        if result.stderr.strip():
            for line in result.stderr.rstrip().splitlines():
                print(f"  stderr: {line}")
    summary = report.summary()
    print(
        f"Fleet summary: selected={summary['selected']} "
        f"succeeded={summary['succeeded']} failed={summary['failed']}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        registry = RepositoryRegistry.load(args.registry)
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.fleet_command == "discover":
        try:
            registry.refresh(args.root, max_depth=args.max_depth)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.commit:
            registry.save(args.registry)
        if args.json:
            print(json.dumps(registry.export(), indent=2, sort_keys=True))
        else:
            _print_registry_entries(registry.repositories)
            if not args.commit:
                print("preview only — run with --commit to persist registry discovery")
        return 0

    if args.fleet_command == "list":
        selected = registry.select(
            repos=args.repo,
            stacks=args.stack,
            tags=args.tag,
            roots=args.root,
            include_stale=args.include_stale,
        )
        if args.json:
            print(json.dumps([repo.to_dict() for repo in selected], indent=2, sort_keys=True))
        else:
            _print_registry_entries(selected)
        return 0

    if args.fleet_command == "tags":
        try:
            registry.set_tags(args.repository_id, args.tags)
        except RegistryError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.commit:
            registry.save(args.registry)
        if args.json:
            print(json.dumps(registry.export(), indent=2, sort_keys=True))
        else:
            entry = next(repo for repo in registry.repositories if repo.id == args.repository_id)
            print(f"{entry.id} tags -> {','.join(entry.tags) or '-'}")
            if not args.commit:
                print("preview only — run with --commit to persist tag change")
        return 0

    if args.fleet_command in {"apply", "status"}:
        selected = _select_from_args(registry, args)
        operation = args.fleet_command
        mode = args.mode if operation == "apply" else None
        try:
            report = execute(
                selected,
                operation=operation,
                mode=mode,
                jobs=args.jobs,
                timeout_s=args.timeout,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        else:
            _print_report(report)
            if operation == "apply" and mode == "info":
                print("preview only — run `rig fleet apply commit` with the same selectors to execute")
        return 0 if report.ok else 1

    return 2
