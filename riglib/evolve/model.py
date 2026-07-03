"""Normalized provider payloads for `rig evolve`.

Accessed via: evolve provider adapters, durable provider cache, and future API endpoints that
surface raw ecosystem data beside the treemap.

Assumptions: providers are local, optional, and allowed to fail; a failure is represented as a
payload with health/errors, never as an exception escaping the provider boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROVIDER_SCHEMA = "rig.evolve.provider.v1"


@dataclass(frozen=True)
class ProviderPayload:
    """Versioned, JSON-serializable payload emitted by one evolve provider.

    Status is intentionally string-based in v1. Current producers emit ok, warning, error, or
    not-wired; not-wired represents a planned integration row, not a failed provider run.
    """

    source: str
    project_path: str | Path
    collected_at: str = field(default_factory=lambda: _utc_now())
    status: str = "ok"
    data: dict[str, Any] = field(default_factory=dict)
    links: list[dict[str, Any]] = field(default_factory=list)
    raw_ref: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    schema: str = PROVIDER_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_path", _normalize_project_path(self.project_path))
        object.__setattr__(self, "data", dict(self.data))
        object.__setattr__(self, "links", [dict(link) for link in self.links])
        object.__setattr__(self, "errors", [dict(error) for error in self.errors])

    @classmethod
    def ok(
        cls,
        *,
        source: str,
        project_path: str | Path,
        collected_at: str | None = None,
        data: dict[str, Any] | None = None,
        links: list[dict[str, Any]] | None = None,
        raw_ref: str | None = None,
        message: str = "",
    ) -> "ProviderPayload":
        return cls(
            source=source,
            project_path=project_path,
            collected_at=collected_at or _utc_now(),
            status="ok",
            data=data or {},
            links=links or [],
            raw_ref=raw_ref,
            message=message,
        )

    @classmethod
    def warning(
        cls,
        *,
        source: str,
        project_path: str | Path,
        message: str,
        collected_at: str | None = None,
        data: dict[str, Any] | None = None,
        links: list[dict[str, Any]] | None = None,
        raw_ref: str | None = None,
    ) -> "ProviderPayload":
        return cls(
            source=source,
            project_path=project_path,
            collected_at=collected_at or _utc_now(),
            status="warning",
            data=data or {},
            links=links or [],
            raw_ref=raw_ref,
            message=message,
        )

    @classmethod
    def error(
        cls,
        *,
        source: str,
        project_path: str | Path,
        message: str,
        collected_at: str | None = None,
        data: dict[str, Any] | None = None,
        links: list[dict[str, Any]] | None = None,
        raw_ref: str | None = None,
    ) -> "ProviderPayload":
        return cls(
            source=source,
            project_path=project_path,
            collected_at=collected_at or _utc_now(),
            status="error",
            data=data or {},
            links=links or [],
            raw_ref=raw_ref,
            errors=[{"message": message}],
            message=message,
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProviderPayload":
        """Load a payload from cache or a provider JSON contract."""

        health = raw.get("health") if isinstance(raw.get("health"), dict) else {}
        source = raw.get("source") or raw.get("provider")
        project_path = raw.get("project_path") or raw.get("project")
        if not isinstance(source, str) or not source:
            raise ValueError("provider payload is missing source/provider")
        if not isinstance(project_path, str) or not project_path:
            raise ValueError("provider payload is missing project_path/project")
        status = raw.get("status") or health.get("status") or "unknown"
        message = raw.get("message") or health.get("message") or ""
        collected_at = raw.get("collected_at") or raw.get("generated_at") or _utc_now()
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        links = raw.get("links") if isinstance(raw.get("links"), list) else []
        errors = raw.get("errors") if isinstance(raw.get("errors"), list) else []
        raw_ref = raw.get("raw_ref") if isinstance(raw.get("raw_ref"), str) else None
        schema = raw.get("schema") if isinstance(raw.get("schema"), str) else PROVIDER_SCHEMA
        return cls(
            source=source,
            project_path=project_path,
            collected_at=str(collected_at),
            status=str(status),
            data=data,
            links=links,
            raw_ref=raw_ref,
            errors=errors,
            message=str(message),
            schema=schema,
        )

    def to_dict(self) -> dict[str, Any]:
        health: dict[str, Any] = {"status": self.status}
        if self.message:
            health["message"] = self.message
        return {
            "schema": self.schema,
            "source": self.source,
            "provider": self.source,
            "project_path": self.project_path,
            "project": self.project_path,
            "collected_at": self.collected_at,
            "generated_at": self.collected_at,
            "status": self.status,
            "health": health,
            "data": self.data,
            "links": self.links,
            "raw_ref": self.raw_ref,
            "errors": self.errors,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_project_path(path: str | Path) -> str:
    candidate = Path(path).expanduser()
    try:
        return str(candidate.resolve())
    except OSError:
        return str(candidate.absolute())
