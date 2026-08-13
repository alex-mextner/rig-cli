"""Syntax-aware ownership headers for Rig-managed text files."""
from __future__ import annotations

from pathlib import PurePosixPath

GENERATED = "generated"
SOURCE_BACKED = "source-backed"
MARKER_MANAGED = "marker-managed"
MANAGEMENT_CLASSES = {GENERATED, SOURCE_BACKED, MARKER_MANAGED}

_SLASH_COMMENT_EXTENSIONS = {".ts", ".js", ".mjs", ".cjs", ".mts", ".cts", ".jsonc"}
_HASH_COMMENT_EXTENSIONS = {".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".properties", ".env"}

def _prefix(rel_path: str) -> str:
    suffix = PurePosixPath(rel_path).suffix.lower()
    if suffix in _SLASH_COMMENT_EXTENSIONS:
        return "//"
    if suffix in _HASH_COMMENT_EXTENSIONS:
        return "#"
    return ""

def managed_header(rel_path: str, *, source: str = "global Rig config + rig.yaml", management_class: str = GENERATED) -> str:
    """Return the ownership contract header, or empty for comment-forbidden formats."""
    if management_class not in MANAGEMENT_CLASSES:
        raise ValueError(f"unknown Rig management class: {management_class}")
    prefix = _prefix(rel_path)
    if not prefix:
        return ""
    if management_class == GENERATED:
        lines = [
            f"Managed by Rig. Class: generated. Source of truth: {source}.",
            "Do not edit directly: `rig apply` reconciles the whole file. Temporary diagnostic edits are allowed locally,",
            "but move the final change into Rig policy before commit.",
        ]
    elif management_class == SOURCE_BACKED:
        lines = [
            f"Managed by Rig. Class: source-backed. Canonical source: {source}.",
            "Do not edit this target directly: change the canonical source (or Rig selection) and run `rig apply`.",
        ]
    else:
        lines = [
            f"Managed by Rig. Class: marker-managed (partial). Policy source: {source}.",
            "You may edit this file outside Rig BEGIN/END markers; edit inside a managed block via Rig config/source only.",
        ]
    return "\n".join(f"{prefix} {line}" for line in lines) + "\n\n"

def with_managed_header(rel_path: str, content: str, *, source: str = "global Rig config + rig.yaml", management_class: str = GENERATED) -> str:
    header = managed_header(rel_path, source=source, management_class=management_class)
    if not header or content.startswith(header):
        return content
    return header + content
