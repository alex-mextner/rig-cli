"""Headers for text configuration files whose source of truth is Rig.

The helper is syntax-aware: strict JSON has no comments, so Rig must not make it invalid merely to
add ownership metadata. Other common config formats get a short deterministic header.
"""
from __future__ import annotations

from pathlib import PurePosixPath

_SLASH_COMMENT_EXTENSIONS = {".ts", ".js", ".mjs", ".cjs", ".mts", ".cts", ".jsonc"}
_HASH_COMMENT_EXTENSIONS = {".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".properties", ".env"}


def managed_header(rel_path: str, *, source: str = "global Rig config + rig.yaml") -> str:
    """Return a valid comment header for ``rel_path`` or ``""`` when its syntax forbids comments."""
    suffix = PurePosixPath(rel_path).suffix.lower()
    if suffix in _SLASH_COMMENT_EXTENSIONS:
        prefix = "//"
    elif suffix in _HASH_COMMENT_EXTENSIONS:
        prefix = "#"
    else:
        return ""
    return (
        f"{prefix} Managed by Rig. Source of truth: {source}.\n"
        f"{prefix} Do not edit directly: `rig apply` reconciles this file. Temporary diagnostic edits "
        "are allowed locally, but move the final change into Rig policy/source before commit.\n\n"
    )


def with_managed_header(rel_path: str, content: str, *, source: str = "global Rig config + rig.yaml") -> str:
    """Prefix Rig ownership metadata once, preserving formats where comments are illegal."""
    header = managed_header(rel_path, source=source)
    if not header or content.startswith(header):
        return content
    return header + content
