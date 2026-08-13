"""Machine-local repository registry used by fleet operations.

The registry is deliberately read-only with respect to repositories: discovery may read
``.git`` metadata and ``rig.yaml`` but never writes into a checkout. User-defined tags and
other machine-local metadata live in the registry file under the Rig config directory.

This module is intentionally independent of the fleet executor. It provides the shared
substrate that fleet reconcile, bulk config mutation, onboarding/status views, and future
team policy sources can consume without each rediscovering repositories differently.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

REGISTRY_VERSION = 1
DEFAULT_REGISTRY_NAME = "repositories.json"
_REMOTE_SCP_RE = re.compile(r"^(?P<user>[^@]+@)?(?P<host>[^:]+):(?P<path>.+)$")


class RegistryError(ValueError):
    """Raised when the on-disk repository registry is malformed."""


def registry_path() -> Path:
    """Return the machine-local registry path, honoring XDG_CONFIG_HOME."""
    base = Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"))
    return base / "rig" / DEFAULT_REGISTRY_NAME


def _norm_path(path: Path | str) -> Path:
    return Path(os.path.expanduser(str(path))).resolve(strict=False)


def _canonical_remote(remote: str) -> str:
    """Normalize common Git remote URL spellings into one stable repository identity."""
    value = remote.strip()
    if not value:
        return ""
    if value.endswith(".git"):
        value = value[:-4]

    # git@github.com:owner/repo and ssh://git@github.com/owner/repo should identify the same repo.
    m = _REMOTE_SCP_RE.match(value)
    if m and "://" not in value:
        host = m.group("host").lower()
        path = m.group("path").lstrip("/")
        return f"{host}/{path}".rstrip("/").lower()

    for prefix in ("https://", "http://", "ssh://", "git://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix) :]
            break
    if "@" in value.split("/", 1)[0]:
        value = value.split("@", 1)[1]
    return value.rstrip("/").lower()


def _repo_identity(path: Path, remote: str) -> str:
    """Return a deterministic ID stable across moves when an origin remote exists."""
    basis = f"remote:{_canonical_remote(remote)}" if remote else f"path:{path.as_posix()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def _read_origin_from_git_config(repo: Path) -> str:
    """Read origin URL without mutating the repository.

    ``git config`` handles worktrees and include rules correctly. If git is unavailable or the
    checkout is intentionally minimal, fall back to a tiny parser for ``.git/config``.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass

    git = repo / ".git"
    config = git / "config" if git.is_dir() else None
    if config is None or not config.is_file():
        return ""
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    in_origin = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_origin = line.lower() == '[remote "origin"]'
            continue
        if in_origin and line.lower().startswith("url") and "=" in line:
            return line.split("=", 1)[1].strip()
    return ""


def _read_rig_metadata(repo: Path) -> tuple[str | None, list[str]]:
    """Read committed stack/tags metadata if present, without requiring PyYAML.

    ``stack`` is already a first-class Rig key. ``metadata.tags`` is accepted here as a future
    compatible read-only convention only when present; discovery never writes it and the registry
    remains authoritative for user-defined machine tags. If PyYAML is available we parse normally;
    otherwise a conservative line parser still recovers the top-level stack.
    """
    cfg = repo / "rig.yaml"
    if not cfg.is_file():
        return None, []
    try:
        text = cfg.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, []

    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
        if isinstance(data, dict):
            stack = data.get("stack")
            stack_value = stack.strip() if isinstance(stack, str) and stack.strip() else None
            tags: list[str] = []
            metadata = data.get("metadata")
            if isinstance(metadata, dict) and isinstance(metadata.get("tags"), list):
                tags = sorted(
                    {tag.strip() for tag in metadata["tags"] if isinstance(tag, str) and tag.strip()}
                )
            return stack_value, tags
    except Exception:
        # Registry discovery must be resilient to an unavailable YAML dependency or a malformed
        # repo config. Rig's normal config validation owns the actual config error.
        pass

    for raw in text.splitlines():
        if raw.startswith((" ", "\t", "#")):
            continue
        if raw.startswith("stack:"):
            value = raw.split(":", 1)[1].strip().strip("'\"")
            return value or None, []
    return None, []


def _is_repo(path: Path) -> bool:
    git = path / ".git"
    return git.is_dir() or git.is_file()


def discover_repository_paths(roots: Iterable[Path | str], *, max_depth: int = 5) -> list[Path]:
    """Discover Git repositories below roots without modifying them.

    Once a repository root is found we do not descend into it; nested repositories are normally
    dependencies/vendor content and should be registered explicitly with another root if desired.
    Symlinked directories are not followed, preventing cycles and surprising traversal outside a
    configured development root.
    """
    if max_depth < 0:
        raise ValueError("max_depth must be >= 0")

    found: set[Path] = set()
    for raw_root in roots:
        root = _norm_path(raw_root)
        if not root.is_dir():
            continue
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            if _is_repo(current):
                found.add(current)
                continue
            if depth >= max_depth:
                continue
            try:
                children = sorted(current.iterdir(), key=lambda p: p.name, reverse=True)
            except OSError:
                continue
            for child in children:
                if child.name in {".git", ".cache", "node_modules", ".venv", "venv"}:
                    continue
                try:
                    if child.is_symlink() or not child.is_dir():
                        continue
                except OSError:
                    continue
                stack.append((child, depth + 1))
    return sorted(found, key=lambda p: p.as_posix())


@dataclass
class RepositoryEntry:
    id: str
    path: str
    name: str
    root: str
    remote: str = ""
    stack: str | None = None
    tags: list[str] = field(default_factory=list)
    committed_tags: list[str] = field(default_factory=list)
    policy_source: str | None = None
    last_status: str | None = None
    stale: bool = False

    @property
    def all_tags(self) -> list[str]:
        return sorted(set(self.tags) | set(self.committed_tags))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["all_tags"] = self.all_tags
        return data


@dataclass
class RepositoryRegistry:
    version: int = REGISTRY_VERSION
    roots: list[str] = field(default_factory=list)
    repositories: list[RepositoryEntry] = field(default_factory=list)

    @classmethod
    def empty(cls) -> "RepositoryRegistry":
        return cls()

    @classmethod
    def load(cls, path: Path | None = None) -> "RepositoryRegistry":
        target = path or registry_path()
        if not target.exists():
            return cls.empty()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RegistryError(f"cannot read repository registry {target}: {exc}") from exc
        if not isinstance(raw, dict):
            raise RegistryError("repository registry root must be an object")
        version = raw.get("version")
        if version != REGISTRY_VERSION:
            raise RegistryError(
                f"unsupported repository registry version {version!r}; expected {REGISTRY_VERSION}"
            )
        roots = raw.get("roots", [])
        repos = raw.get("repositories", [])
        if not isinstance(roots, list) or not all(isinstance(v, str) for v in roots):
            raise RegistryError("repository registry roots must be a string array")
        if not isinstance(repos, list):
            raise RegistryError("repository registry repositories must be an array")
        entries: list[RepositoryEntry] = []
        allowed = set(RepositoryEntry.__dataclass_fields__)
        for idx, item in enumerate(repos):
            if not isinstance(item, dict):
                raise RegistryError(f"repositories[{idx}] must be an object")
            cooked = {k: v for k, v in item.items() if k in allowed}
            try:
                entry = RepositoryEntry(**cooked)
            except TypeError as exc:
                raise RegistryError(f"invalid repositories[{idx}]: {exc}") from exc
            if not isinstance(entry.tags, list) or not all(isinstance(v, str) for v in entry.tags):
                raise RegistryError(f"repositories[{idx}].tags must be a string array")
            if not isinstance(entry.committed_tags, list) or not all(
                isinstance(v, str) for v in entry.committed_tags
            ):
                raise RegistryError(f"repositories[{idx}].committed_tags must be a string array")
            entries.append(entry)
        return cls(version=version, roots=roots, repositories=entries)

    def save(self, path: Path | None = None) -> Path:
        target = path or registry_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.version,
            "roots": self.roots,
            "repositories": [asdict(repo) for repo in sorted(self.repositories, key=lambda r: r.id)],
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        return target

    def refresh(
        self,
        roots: Sequence[Path | str] | None = None,
        *,
        max_depth: int = 5,
    ) -> "RepositoryRegistry":
        """Refresh discovery while preserving stable IDs and machine-local tags/status.

        A moved checkout with the same canonical origin keeps its existing ID and metadata. Missing
        checkouts remain as ``stale`` entries so fleet/status can surface them instead of silently
        forgetting policy coverage.
        """
        selected_roots = [_norm_path(root) for root in (roots or self.roots)]
        self.roots = sorted({path.as_posix() for path in selected_roots})

        by_id = {entry.id: entry for entry in self.repositories}
        by_remote = {
            _canonical_remote(entry.remote): entry
            for entry in self.repositories
            if _canonical_remote(entry.remote)
        }
        discovered_ids: set[str] = set()
        updated: list[RepositoryEntry] = []

        for path in discover_repository_paths(selected_roots, max_depth=max_depth):
            remote = _read_origin_from_git_config(path)
            identity = _repo_identity(path, remote)
            previous = by_id.get(identity)
            if previous is None and remote:
                previous = by_remote.get(_canonical_remote(remote))
                if previous is not None:
                    identity = previous.id
            stack, committed_tags = _read_rig_metadata(path)
            root = next(
                (
                    candidate
                    for candidate in selected_roots
                    if path == candidate or candidate in path.parents
                ),
                path.parent,
            )
            entry = RepositoryEntry(
                id=identity,
                path=path.as_posix(),
                name=path.name,
                root=root.as_posix(),
                remote=remote,
                stack=stack,
                tags=sorted(set(previous.tags)) if previous else [],
                committed_tags=committed_tags,
                policy_source=(previous.policy_source if previous else None),
                last_status=(previous.last_status if previous else None),
                stale=False,
            )
            discovered_ids.add(entry.id)
            updated.append(entry)

        for previous in self.repositories:
            if previous.id in discovered_ids:
                continue
            stale = RepositoryEntry(**asdict(previous))
            stale.stale = True
            updated.append(stale)

        self.repositories = sorted(updated, key=lambda entry: (entry.stale, entry.path, entry.id))
        return self

    def set_tags(self, repository_id: str, tags: Iterable[str]) -> None:
        normalized = sorted({tag.strip() for tag in tags if tag.strip()})
        for repo in self.repositories:
            if repo.id == repository_id:
                repo.tags = normalized
                return
        raise RegistryError(f"unknown repository id {repository_id!r}")

    def select(
        self,
        *,
        repos: Iterable[str] = (),
        stacks: Iterable[str] = (),
        tags: Iterable[str] = (),
        roots: Iterable[Path | str] = (),
        include_stale: bool = False,
    ) -> list[RepositoryEntry]:
        """Select entries using deterministic AND-across-dimensions fleet semantics.

        Multiple values within one dimension are ORed. Different dimensions are ANDed. Explicit
        repo selectors match deterministic ID, name, absolute path, or canonical remote identity.
        """
        repo_terms = {term.strip() for term in repos if term.strip()}
        stack_terms = {term.strip() for term in stacks if term.strip()}
        tag_terms = {term.strip() for term in tags if term.strip()}
        root_terms = {_norm_path(root).as_posix() for root in roots}

        def repo_matches(entry: RepositoryEntry) -> bool:
            if not repo_terms:
                return True
            candidates = {entry.id, entry.name, entry.path, _canonical_remote(entry.remote)}
            return bool(repo_terms & candidates)

        def root_matches(entry: RepositoryEntry) -> bool:
            if not root_terms:
                return True
            path = Path(entry.path)
            return any(path == Path(root) or Path(root) in path.parents for root in root_terms)

        selected = []
        for entry in self.repositories:
            if entry.stale and not include_stale:
                continue
            if not repo_matches(entry):
                continue
            if stack_terms and entry.stack not in stack_terms:
                continue
            if tag_terms and not tag_terms.intersection(entry.all_tags):
                continue
            if not root_matches(entry):
                continue
            selected.append(entry)
        return sorted(selected, key=lambda entry: (entry.path, entry.id))

    def export(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "roots": list(self.roots),
            "repositories": [repo.to_dict() for repo in self.repositories],
        }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect Rig's machine-local repository registry")
    parser.add_argument("--registry", type=Path, default=None, help="override registry JSON path")
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="discover/refresh repositories under roots")
    discover.add_argument("--root", action="append", required=True)
    discover.add_argument("--max-depth", type=int, default=5)
    discover.add_argument("--write", action="store_true", help="persist refreshed registry")

    list_cmd = sub.add_parser("list", help="list selected repositories")
    list_cmd.add_argument("--repo", action="append", default=[])
    list_cmd.add_argument("--stack", action="append", default=[])
    list_cmd.add_argument("--tag", action="append", default=[])
    list_cmd.add_argument("--root", action="append", default=[])
    list_cmd.add_argument("--include-stale", action="store_true")
    list_cmd.add_argument("--json", action="store_true")

    export_cmd = sub.add_parser("export", help="export complete registry as JSON")
    export_cmd.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    registry = RepositoryRegistry.load(args.registry)

    if args.command == "discover":
        registry.refresh(args.root, max_depth=args.max_depth)
        if args.write:
            registry.save(args.registry)
        print(json.dumps(registry.export(), indent=2, sort_keys=True))
        return 0

    if args.command == "list":
        selected = registry.select(
            repos=args.repo,
            stacks=args.stack,
            tags=args.tag,
            roots=args.root,
            include_stale=args.include_stale,
        )
        if args.json:
            print(json.dumps([entry.to_dict() for entry in selected], indent=2, sort_keys=True))
        else:
            for entry in selected:
                tags = ",".join(entry.all_tags) or "-"
                stack = entry.stack or "-"
                stale = " stale" if entry.stale else ""
                print(f"{entry.id}  {entry.name}  stack={stack} tags={tags}{stale}  {entry.path}")
        return 0

    if args.command == "export":
        if args.pretty:
            print(json.dumps(registry.export(), indent=2, sort_keys=True))
        else:
            print(json.dumps(registry.export(), sort_keys=True))
        return 0

    raise AssertionError(args.command)


if __name__ == "__main__":  # pragma: no cover - exercised through main() in tests
    raise SystemExit(main())
