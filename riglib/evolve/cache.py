"""Durable provider cache for `rig evolve`.

Accessed via: evolve provider collection and future HTTP endpoints that need to reuse local tool
payloads without rerunning every provider on each page load.

Assumptions: cache entries are disposable JSON files outside the repo, keyed by project path,
git/head version, provider name, and schema version; changing any of those inputs is invalidation.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .model import PROVIDER_SCHEMA, ProviderPayload


@dataclass(frozen=True)
class ProviderCacheKey:
    project_path: str | Path
    version: str
    provider: str
    schema: str = PROVIDER_SCHEMA

    def normalized(self) -> dict[str, str]:
        return {
            "project_path": _normalize_project_path(self.project_path),
            "version": self.version,
            "provider": self.provider,
            "schema": self.schema,
        }

    def digest(self) -> str:
        raw = json.dumps(self.normalized(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


class ProviderCache:
    """Small JSON-file cache for normalized provider payloads."""

    def __init__(self, *, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser() if root is not None else default_provider_cache_root()

    def get(self, key: ProviderCacheKey) -> ProviderPayload | None:
        path = self.path_for(key)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            payload = ProviderPayload.from_dict(raw)
        except ValueError:
            return None
        expected = key.normalized()
        if payload.schema != expected["schema"]:
            return None
        if payload.source != expected["provider"]:
            return None
        if payload.project_path != expected["project_path"]:
            return None
        return payload

    def set(self, key: ProviderCacheKey, payload: ProviderPayload) -> Path:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload.to_dict(), sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        return path

    def invalidate(self, key: ProviderCacheKey) -> bool:
        try:
            self.path_for(key).unlink()
            return True
        except FileNotFoundError:
            return False

    def path_for(self, key: ProviderCacheKey) -> Path:
        provider = _safe_segment(key.provider)
        return self.root / provider / f"{key.digest()}.json"


def default_provider_cache_root() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        base = Path(xdg)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path.home() / ".cache"
    return base / "rig" / "evolve" / "providers"


def _safe_segment(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value) or "provider"


def _normalize_project_path(path: str | Path) -> str:
    candidate = Path(path).expanduser()
    try:
        return str(candidate.resolve())
    except OSError:
        return str(candidate.absolute())
