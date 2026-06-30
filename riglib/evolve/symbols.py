"""Interim symbol extraction for the `rig evolve` portal.

Accessed by tests and, later, by the file/symbol treemap layer. This module deliberately stays
stdlib-only and returns JSON-ready dictionaries so LSP, Serena, or tree-sitter providers can
replace or enrich the same shape without changing callers.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SYMBOL_SCHEMA = "rig.evolve.symbol.v1"

_PYTHON_SUFFIXES = {".py"}
_JAVASCRIPT_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs"}
_TYPESCRIPT_SUFFIXES = {".ts", ".tsx", ".mts", ".cts"}

_JS_NAME = r"[A-Za-z_$][A-Za-z0-9_$]*"
_JS_CLASS_RE = re.compile(rf"\b(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+({_JS_NAME})\b")
_JS_FUNCTION_RE = re.compile(
    rf"\b(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+({_JS_NAME})\s*(?:<[^>\n]+>)?\s*\("
)
_JS_ARROW_RE = re.compile(
    rf"\b(?:export\s+)?(?:const|let|var)\s+({_JS_NAME})\s*(?::[^=\n]+)?=\s*"
    rf"(?:async\s*)?(?:\([^)]*\)|{_JS_NAME})\s*(?::[^=\n]+)?=>"
)
_JS_METHOD_RE = re.compile(
    rf"(?m)^[ \t]*(?:(?:public|private|protected|static|async|override|readonly)\s+)*"
    rf"(?:get\s+|set\s+)?({_JS_NAME})\s*(?:<[^>\n]+>)?\s*\([^;\n{{}}]*\)"
    rf"\s*(?::[^;\n{{}}]+)?\{{"
)
_JS_METHOD_KEYWORDS = {"catch", "for", "if", "switch", "while", "with"}


@dataclass
class _JsRecord:
    name: str
    kind: str
    start_char: int
    end_char: int
    doc: dict[str, Any] | None
    parser: str
    language: str
    parent: "_JsRecord | None" = None
    children: list["_JsRecord"] = field(default_factory=list)


def extract_symbols(
    path: Path,
    source: str | None = None,
    *,
    repo_root: Path | None = None,
    language: str | None = None,
) -> list[dict[str, Any]]:
    """Extract a conservative symbol tree for ``path``.

    The returned nodes are intentionally provider-shaped, not parser-shaped: stable IDs, source
    ranges, sizing fields, docs, and child nodes are available regardless of the backend.
    """

    file_path = Path(path)
    if source is None:
        source = file_path.read_text(encoding="utf-8")
    rel_path = _display_path(file_path, repo_root)
    resolved_language = language or _language_for_path(file_path)
    if resolved_language == "python":
        return _extract_python_symbols(rel_path, source)
    if resolved_language in {"javascript", "typescript"}:
        return _extract_js_symbols(rel_path, source, resolved_language)
    return []


def flatten_symbols(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return ``nodes`` and every descendant in pre-order."""

    out: list[dict[str, Any]] = []

    def visit(node: dict[str, Any]) -> None:
        out.append(node)
        for child in node.get("children", []):
            visit(child)

    for node in nodes:
        visit(node)
    return out


def _extract_python_symbols(rel_path: str, source: str) -> list[dict[str, Any]]:
    try:
        module = ast.parse(source)
    except SyntaxError:
        return []

    lines = source.splitlines()
    offsets = _line_byte_offsets(source)

    def build(
        node: ast.AST,
        kind: str,
        scope_parts: list[str],
        parent_id: str | None,
    ) -> dict[str, Any]:
        name = str(getattr(node, "name"))
        line_start = int(getattr(node, "lineno", 1))
        line_end = int(getattr(node, "end_lineno", line_start))
        start_col = int(getattr(node, "col_offset", 0) or 0)
        default_end = len(lines[line_end - 1].encode("utf-8")) if 0 < line_end <= len(lines) else 0
        end_col = int(getattr(node, "end_col_offset", default_end) or default_end)
        byte_start = _byte_from_line_col(offsets, line_start, start_col)
        byte_end = max(byte_start + 1, _byte_from_line_col(offsets, line_end, end_col))
        doc = _python_doc(node, lines)
        symbol = _make_node(
            rel_path=rel_path,
            name=name,
            kind=kind,
            scope_parts=scope_parts,
            line_start=line_start,
            line_end=line_end,
            byte_start=byte_start,
            byte_end=byte_end,
            doc=doc,
            parser="python-ast",
            language="python",
            parent_id=parent_id,
        )
        symbol["children"] = _python_children(node, scope_parts + [name], symbol["id"], kind)
        return symbol

    def walk(container: ast.AST, scope_parts: list[str], parent_id: str | None, parent_kind: str | None) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for child in ast.iter_child_nodes(container):
            if isinstance(child, ast.ClassDef):
                found.append(build(child, "class", scope_parts, parent_id))
            elif isinstance(child, (ast.AsyncFunctionDef, ast.FunctionDef)):
                kind = "method" if parent_kind == "class" else "function"
                found.append(build(child, kind, scope_parts, parent_id))
            else:
                found.extend(walk(child, scope_parts, parent_id, parent_kind))
        found.sort(key=lambda item: (item["line_start"], item["byte_start"], item["name"]))
        return found

    def _python_children(
        container: ast.AST,
        scope_parts: list[str],
        parent_id: str,
        parent_kind: str,
    ) -> list[dict[str, Any]]:
        return walk(container, scope_parts, parent_id, parent_kind)

    return walk(module, [], None, None)


def _extract_js_symbols(rel_path: str, source: str, language: str) -> list[dict[str, Any]]:
    records = _js_records(source, language)
    _attach_js_parents(records)

    roots = [record for record in records if record.parent is None]
    roots.sort(key=lambda record: (record.start_char, record.name))
    return [_js_node(rel_path, source, record, [], None) for record in roots]


def _js_records(source: str, language: str) -> list[_JsRecord]:
    records: list[_JsRecord] = []

    class_records: list[_JsRecord] = []
    for match in _JS_CLASS_RE.finditer(source):
        record = _js_block_record(source, match.start(), match.end(), match.group(1), "class", language)
        if record is not None:
            records.append(record)
            class_records.append(record)

    for match in _JS_FUNCTION_RE.finditer(source):
        record = _js_block_record(source, match.start(), match.end(), match.group(1), "function", language)
        if record is not None:
            records.append(record)

    for match in _JS_ARROW_RE.finditer(source):
        record = _js_block_record(source, match.start(), match.end(), match.group(1), "function", language)
        if record is not None:
            records.append(record)

    for class_record in class_records:
        body_start = _find_open_brace(source, class_record.start_char, class_record.start_char)
        if body_start is None:
            continue
        body = source[body_start + 1 : class_record.end_char - 1]
        for match in _JS_METHOD_RE.finditer(body):
            name = match.group(1)
            if name in _JS_METHOD_KEYWORDS:
                continue
            absolute_start = body_start + 1 + match.start()
            absolute_end_hint = body_start + 1 + match.end()
            if _brace_depth_between(source, body_start + 1, absolute_start, initial=1) != 1:
                continue
            record = _js_block_record(source, absolute_start, absolute_end_hint, name, "method", language)
            if record is not None:
                records.append(record)

    records.sort(key=lambda record: (record.start_char, record.end_char, record.kind, record.name))
    return records


def _js_block_record(
    source: str,
    start_char: int,
    end_hint: int,
    name: str,
    kind: str,
    language: str,
) -> _JsRecord | None:
    open_brace = _find_open_brace(source, start_char, end_hint)
    if open_brace is None:
        line_end = _line_end_char(source, start_char)
        end_char = max(start_char + 1, line_end)
    else:
        close_brace = _find_matching_brace(source, open_brace)
        if close_brace is None:
            return None
        end_char = close_brace + 1
    return _JsRecord(
        name=name,
        kind=kind,
        start_char=start_char,
        end_char=end_char,
        doc=_leading_jsdoc(source, start_char),
        parser="js-regex-v1",
        language=language,
    )


def _attach_js_parents(records: list[_JsRecord]) -> None:
    for record in records:
        candidates = [
            candidate
            for candidate in records
            if candidate is not record
            and candidate.start_char < record.start_char
            and record.end_char <= candidate.end_char
        ]
        if candidates:
            record.parent = min(candidates, key=lambda candidate: candidate.end_char - candidate.start_char)

    for record in records:
        if record.parent is not None:
            record.parent.children.append(record)

    for record in records:
        record.children.sort(key=lambda child: (child.start_char, child.name))


def _js_node(
    rel_path: str,
    source: str,
    record: _JsRecord,
    scope_parts: list[str],
    parent_id: str | None,
) -> dict[str, Any]:
    line_start = _line_for_char(source, record.start_char)
    line_end = _line_for_char(source, max(record.start_char, record.end_char - 1))
    byte_start = _char_to_byte(source, record.start_char)
    byte_end = max(byte_start + 1, _char_to_byte(source, record.end_char))
    node = _make_node(
        rel_path=rel_path,
        name=record.name,
        kind=record.kind,
        scope_parts=scope_parts,
        line_start=line_start,
        line_end=line_end,
        byte_start=byte_start,
        byte_end=byte_end,
        doc=record.doc,
        parser=record.parser,
        language=record.language,
        parent_id=parent_id,
    )
    node["children"] = [
        _js_node(rel_path, source, child, scope_parts + [record.name], node["id"]) for child in record.children
    ]
    return node


def _make_node(
    *,
    rel_path: str,
    name: str,
    kind: str,
    scope_parts: list[str],
    line_start: int,
    line_end: int,
    byte_start: int,
    byte_end: int,
    doc: dict[str, Any] | None,
    parser: str,
    language: str,
    parent_id: str | None,
) -> dict[str, Any]:
    scope = ".".join(scope_parts)
    id_scope = scope or "<module>"
    size_bytes = max(1, byte_end - byte_start)
    size_lines = max(1, line_end - line_start + 1)
    return {
        "schema": SYMBOL_SCHEMA,
        "id": f"symbol:{rel_path}:{id_scope}:{kind}:{name}",
        "name": name,
        "kind": kind,
        "path": rel_path,
        "scope": scope,
        "parent_id": parent_id,
        "language": language,
        "parser": parser,
        "line_start": line_start,
        "line_end": line_end,
        "byte_start": byte_start,
        "byte_end": byte_end,
        "size": size_bytes,
        "size_bytes": size_bytes,
        "size_lines": size_lines,
        "range": {
            "start_line": line_start,
            "end_line": line_end,
            "start_byte": byte_start,
            "end_byte": byte_end,
        },
        "doc": doc,
        "children": [],
    }


def _python_doc(node: ast.AST, lines: list[str]) -> dict[str, Any] | None:
    docstring = ast.get_docstring(node, clean=True)
    if docstring:
        return _doc("python-docstring", docstring)
    line_start = int(getattr(node, "lineno", 1))
    comments: list[str] = []
    idx = line_start - 2
    while idx >= 0 and lines[idx].lstrip().startswith("#"):
        comments.append(lines[idx].lstrip()[1:].strip())
        idx -= 1
    if comments:
        comments.reverse()
        return _doc("python-comment", "\n".join(comments))
    return None


def _leading_jsdoc(source: str, start_char: int) -> dict[str, Any] | None:
    end = start_char
    while end > 0 and source[end - 1].isspace():
        end -= 1
    if not source[:end].endswith("*/"):
        return None
    start = source.rfind("/**", 0, end)
    if start == -1:
        return None
    if source.find("*/", start, end) != end - 2:
        return None
    return _parse_jsdoc(source[start:end])


def _parse_jsdoc(raw: str) -> dict[str, Any]:
    lines: list[str] = []
    for line in raw.splitlines():
        text = line.strip()
        if text.startswith("/**"):
            text = text[3:]
        if text.endswith("*/"):
            text = text[:-2]
        text = text.strip()
        if text.startswith("*"):
            text = text[1:].strip()
        if text:
            lines.append(text)

    body: list[str] = []
    tags: list[dict[str, str]] = []
    for line in lines:
        if line.startswith("@"):
            tag, _, rest = line[1:].partition(" ")
            tags.append({"tag": tag, "text": rest.strip()})
        else:
            body.append(line)

    text = "\n".join(body)
    doc = _doc("jsdoc", text)
    doc["tags"] = tags
    return doc


def _doc(fmt: str, text: str) -> dict[str, Any]:
    clean = text.strip()
    summary = next((line.strip() for line in clean.splitlines() if line.strip()), "")
    return {"format": fmt, "text": clean, "summary": summary, "tags": []}


def _find_open_brace(source: str, start_char: int, end_hint: int) -> int | None:
    idx = end_hint
    while idx < len(source):
        char = source[idx]
        if char == "{":
            return idx
        if char in ";\n":
            return None
        idx += 1
    return None


def _find_matching_brace(source: str, open_brace: int) -> int | None:
    depth = 0
    idx = open_brace
    while idx < len(source):
        char = source[idx]
        next_char = source[idx + 1] if idx + 1 < len(source) else ""
        if char == "/" and next_char == "/":
            idx = _line_end_char(source, idx)
            continue
        if char == "/" and next_char == "*":
            end = source.find("*/", idx + 2)
            if end == -1:
                return None
            idx = end + 2
            continue
        if char in {'"', "'", "`"}:
            idx = _skip_js_string(source, idx)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return idx
        idx += 1
    return None


def _brace_depth_between(source: str, start_char: int, end_char: int, *, initial: int) -> int:
    depth = initial
    idx = start_char
    while idx < end_char:
        char = source[idx]
        next_char = source[idx + 1] if idx + 1 < len(source) else ""
        if char == "/" and next_char == "/":
            idx = _line_end_char(source, idx)
            continue
        if char == "/" and next_char == "*":
            end = source.find("*/", idx + 2)
            idx = end + 2 if end != -1 else end_char
            continue
        if char in {'"', "'", "`"}:
            idx = min(_skip_js_string(source, idx), end_char)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        idx += 1
    return depth


def _skip_js_string(source: str, start_char: int) -> int:
    quote = source[start_char]
    idx = start_char + 1
    while idx < len(source):
        char = source[idx]
        if char == "\\":
            idx += 2
            continue
        if char == quote:
            return idx + 1
        idx += 1
    return idx


def _line_end_char(source: str, start_char: int) -> int:
    end = source.find("\n", start_char)
    return len(source) if end == -1 else end


def _line_for_char(source: str, char_index: int) -> int:
    return source.count("\n", 0, char_index) + 1


def _char_to_byte(source: str, char_index: int) -> int:
    return len(source[:char_index].encode("utf-8"))


def _line_byte_offsets(source: str) -> list[int]:
    offsets = [0]
    total = 0
    for line in source.splitlines(keepends=True):
        total += len(line.encode("utf-8"))
        offsets.append(total)
    return offsets


def _byte_from_line_col(offsets: list[int], line: int, col: int) -> int:
    if line <= 0:
        return 0
    if line - 1 >= len(offsets):
        return offsets[-1]
    return offsets[line - 1] + col


def _display_path(path: Path, repo_root: Path | None) -> str:
    if repo_root is not None:
        try:
            return path.resolve(strict=False).relative_to(repo_root.resolve(strict=False)).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _language_for_path(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in _PYTHON_SUFFIXES:
        return "python"
    if suffix in _TYPESCRIPT_SUFFIXES:
        return "typescript"
    if suffix in _JAVASCRIPT_SUFFIXES:
        return "javascript"
    return None
