"""Repo-local project-tool provisioning for Haft, Serena, and Sverklo.

The ``project_tools`` config block owns committed, repo-local integration artifacts for
code-intelligence/governance tools. It is deliberately separate from ``tools``: ``tools`` installs
personal CLIs into a machine PATH, while this module renders files and live registrations that make
one repository usable by those CLIs.

Stdlib-only at import time.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


HAFT_WORKFLOW_MODES = ("standard", "tactical")

HAFT_KEYS = {"enabled", "project_name", "project_id", "codex_mcp", "workflow"}
HAFT_WORKFLOW_KEYS = {"mode", "require_decision", "require_verify", "allow_autonomy"}
SERENA_KEYS = {"enabled", "project_name", "languages", "read_only", "ignored_paths"}
SVERKLO_KEYS = {"enabled", "register", "reindex"}
PROJECT_TOOLS_KEYS = {"enabled", "haft", "serena", "sverklo"}

_CODEX_HAFT_BEGIN = "# >>> rig managed: haft mcp"
_CODEX_HAFT_END = "# <<< rig managed: haft mcp"


@dataclass(frozen=True)
class ProjectToolEntry:
    """One desired file or live operation emitted from ``project_tools`` config."""

    item: str
    tool: str
    operation: str
    rel_path: str = ""
    content: str = ""

    def to_options(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "operation": self.operation,
            "rel_path": self.rel_path,
            "content": self.content,
        }


@dataclass(frozen=True)
class ProjectToolResolution:
    target_path: Path
    content: str
    state: str
    detail: str = ""


def project_tools_enabled(config: dict[str, Any] | None) -> bool:
    """True when the top-level block should emit enabled subtools."""

    if not isinstance(config, dict):
        return False
    return config.get("enabled") is not False


def desired_entries(repo_root: Path, config: dict[str, Any] | None) -> list[ProjectToolEntry]:
    """Render all desired files/operations for an enabled ``project_tools`` block."""

    if not project_tools_enabled(config):
        return []
    cfg = config if isinstance(config, dict) else {}
    entries: list[ProjectToolEntry] = []
    haft = cfg.get("haft")
    if isinstance(haft, dict) and haft.get("enabled") is not False:
        entries.extend(_haft_entries(repo_root, haft))
    serena = cfg.get("serena")
    if isinstance(serena, dict) and serena.get("enabled") is not False:
        entries.extend(_serena_entries(repo_root, serena))
    sverklo = cfg.get("sverklo")
    if isinstance(sverklo, dict) and sverklo.get("enabled") is not False:
        entries.extend(_sverklo_entries(sverklo))
    return entries


def _project_name(repo_root: Path, spec: dict[str, Any]) -> str:
    raw = spec.get("project_name")
    if isinstance(raw, str) and raw:
        return raw
    return repo_root.name or "project"


def _haft_project_id(project_name: str, spec: dict[str, Any]) -> str:
    raw = spec.get("project_id")
    if isinstance(raw, str) and raw:
        return raw
    digest = hashlib.sha256(project_name.encode("utf-8")).hexdigest()[:8]
    return f"qnt_{digest}"


def _bool(spec: dict[str, Any], key: str, default: bool) -> bool:
    val = spec.get(key)
    return val if isinstance(val, bool) else default


def _workflow(haft: dict[str, Any]) -> dict[str, Any]:
    raw = haft.get("workflow")
    wf = raw if isinstance(raw, dict) else {}
    mode = wf.get("mode")
    return {
        "mode": mode if isinstance(mode, str) and mode in HAFT_WORKFLOW_MODES else "standard",
        "require_decision": _bool(wf, "require_decision", True),
        "require_verify": _bool(wf, "require_verify", True),
        "allow_autonomy": _bool(wf, "allow_autonomy", False),
    }


def _haft_entries(repo_root: Path, haft: dict[str, Any]) -> list[ProjectToolEntry]:
    name = _project_name(repo_root, haft)
    project_id = _haft_project_id(name, haft)
    wf = _workflow(haft)
    entries = [
        ProjectToolEntry("haft-project", "haft", "file", ".haft/project.yaml", f"id: {project_id}\nname: {name}\n"),
        ProjectToolEntry("haft-workflow", "haft", "file", ".haft/workflow.md", _haft_workflow_md(wf)),
        ProjectToolEntry("haft-enabling-system", "haft", "file", ".haft/specs/enabling-system.md", _haft_spec_md("Enabling System Spec", "ES.placeholder.001", "Enabling system placeholder", "enabling-system governance")),
        ProjectToolEntry("haft-target-system", "haft", "file", ".haft/specs/target-system.md", _haft_spec_md("Target System Spec", "TS.placeholder.001", "Target system placeholder", "target-system claim")),
        ProjectToolEntry("haft-term-map", "haft", "file", ".haft/specs/term-map.md", _haft_term_map_md()),
    ]
    for rel in (
        ".haft/decisions/.gitkeep",
        ".haft/evidence/.gitkeep",
        ".haft/notes/.gitkeep",
        ".haft/problems/.gitkeep",
        ".haft/refresh/.gitkeep",
        ".haft/solutions/.gitkeep",
    ):
        entries.append(ProjectToolEntry(rel.replace("/", "-").replace(".", "").strip("-"), "haft", "file", rel, ""))
    if haft.get("codex_mcp") is not False:
        entries.append(ProjectToolEntry("haft-codex-mcp", "haft", "codex_mcp", ".codex/config.toml", _haft_codex_mcp_section()))
    return entries


def _haft_workflow_md(wf: dict[str, Any]) -> str:
    return (
        "# Workflow\n\n"
        "## Intent\n\n"
        "Haft should bias toward small reversible changes, require explicit decisions for "
        "core/domain edits, and always verify behavior with tests or concrete runtime evidence "
        "before calling work complete.\n\n"
        "## Defaults\n\n"
        "```yaml\n"
        f"mode: {wf['mode']}\n"
        f"require_decision: {str(wf['require_decision']).lower()}\n"
        f"require_verify: {str(wf['require_verify']).lower()}\n"
        f"allow_autonomy: {str(wf['allow_autonomy']).lower()}\n"
        "```\n"
    )


def _haft_spec_md(title: str, section_id: str, section_title: str, claim: str) -> str:
    return (
        f"# {title}\n\n"
        f"## {section_id} {section_title}\n\n"
        "```yaml spec-section\n"
        f"id: {section_id}\n"
        "kind: environment-change\n"
        f"title: {section_title}\n"
        "statement_type: explanation\n"
        "claim_layer: carrier\n"
        "owner: human\n"
        "status: draft\n"
        "valid_until: null\n"
        "depends_on: []\n"
        "supersedes: []\n"
        "terms: []\n"
        "target_refs: []\n"
        "evidence_required: []\n"
        "```\n\n"
        f"This placeholder only reserves a parseable carrier for onboarding. It is not an active {claim}.\n"
    )


def _haft_term_map_md() -> str:
    return (
        "# Term Map\n\n"
        "```yaml term-map\n"
        "entries: []\n"
        "status: draft\n"
        "```\n\n"
        "This placeholder has no term definitions. Add human-approved vocabulary during onboarding.\n"
    )


def _haft_codex_mcp_section() -> str:
    return (
        f"{_CODEX_HAFT_BEGIN}\n"
        "[mcp_servers.haft]\n"
        'command = "haft"\n'
        'args = ["serve"]\n'
        "startup_timeout_sec = 10\n"
        "tool_timeout_sec = 60\n\n"
        "[mcp_servers.haft.env]\n"
        'HAFT_PROJECT_ROOT = "."\n'
        f"{_CODEX_HAFT_END}\n"
    )


def _serena_entries(repo_root: Path, serena: dict[str, Any]) -> list[ProjectToolEntry]:
    return [
        ProjectToolEntry("serena-project", "serena", "file", ".serena/project.yml", _serena_project_yml(repo_root, serena)),
        ProjectToolEntry("serena-gitignore", "serena", "file", ".serena/.gitignore", "/cache\n/project.local.yml\n"),
    ]


def _serena_project_yml(repo_root: Path, serena: dict[str, Any]) -> str:
    name = _project_name(repo_root, serena)
    languages = _serena_languages(repo_root, serena)
    ignored = _string_list(serena.get("ignored_paths"))
    lines = [
        f'project_name: "{_yaml_quote(name)}"',
        "languages:" if languages else "languages: []",
        *[f"- {lang}" for lang in languages],
        'encoding: "utf-8"',
        "ignore_all_files_in_gitignore: true",
        "ignored_paths:" if ignored else "ignored_paths: []",
        *[f"- {path}" for path in ignored],
        f"read_only: {str(_bool(serena, 'read_only', False)).lower()}",
        "excluded_tools: []",
        "included_optional_tools: []",
        "fixed_tools: []",
        'initial_prompt: ""',
    ]
    return "\n".join(lines) + "\n"


def _yaml_quote(value: str) -> str:
    out: list[str] = []
    for char in value:
        code = ord(char)
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif code < 0x20:
            out.append(f"\\x{code:02x}")
        else:
            out.append(char)
    return "".join(out)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def _serena_languages(repo_root: Path, serena: dict[str, Any]) -> list[str]:
    explicit = _string_list(serena.get("languages"))
    if explicit:
        return explicit
    langs: list[str] = []
    if (repo_root / "pyproject.toml").is_file() or (repo_root / "setup.py").is_file() or (repo_root / "requirements.txt").is_file():
        langs.append("python")
    if (repo_root / "package.json").is_file() or (repo_root / "tsconfig.json").is_file():
        langs.append("typescript")
    if (repo_root / "go.mod").is_file():
        langs.append("go")
    if (repo_root / "Cargo.toml").is_file():
        langs.append("rust")
    if (repo_root / "Package.swift").is_file():
        langs.append("swift")
    return langs


def _sverklo_entries(sverklo: dict[str, Any]) -> list[ProjectToolEntry]:
    entries: list[ProjectToolEntry] = []
    if sverklo.get("register") is not False:
        entries.append(ProjectToolEntry("sverklo-register", "sverklo", "register"))
    if sverklo.get("reindex") is True:
        entries.append(ProjectToolEntry("sverklo-reindex", "sverklo", "reindex"))
    return entries


def resolve_entry(repo_root: Path, rel_path: str, content: str, operation: str) -> ProjectToolResolution:
    """Classify desired project-tool file state without writing."""

    target = repo_root / rel_path
    if path_escapes_repo(rel_path):
        return ProjectToolResolution(target, content, "io_error", f"{rel_path!r} escapes the repo")
    if operation == "codex_mcp":
        return _resolve_codex_mcp(target, content)
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    if not target.exists():
        return ProjectToolResolution(target, content, "create")
    if not target.is_file():
        return ProjectToolResolution(target, content, "io_error", f"{target} is not a regular file")
    try:
        on_disk = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return ProjectToolResolution(target, content, "io_error", f"cannot read {target}: {exc}")
    if on_disk == content:
        return ProjectToolResolution(target, content, "ok")
    return ProjectToolResolution(target, content, "update")


def _resolve_codex_mcp(target: Path, section: str) -> ProjectToolResolution:
    if not target.exists():
        return ProjectToolResolution(target, section, "create")
    if not target.is_file():
        return ProjectToolResolution(target, section, "io_error", f"{target} is not a regular file")
    try:
        on_disk = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return ProjectToolResolution(target, section, "io_error", f"cannot read {target}: {exc}")
    desired = merge_codex_mcp_section(on_disk, section)
    if desired == on_disk:
        return ProjectToolResolution(target, desired, "ok")
    return ProjectToolResolution(target, desired, "update")


def merge_codex_mcp_section(existing: str, section: str) -> str:
    """Insert or replace the rig-managed Haft MCP section in Codex TOML text."""

    if _CODEX_HAFT_BEGIN in existing and _CODEX_HAFT_END in existing:
        before, rest = existing.split(_CODEX_HAFT_BEGIN, 1)
        _, after = rest.split(_CODEX_HAFT_END, 1)
        prefix = before.rstrip()
        suffix = after.lstrip("\n")
        body = section.rstrip() + "\n"
        if prefix:
            body = prefix + "\n\n" + body
        if suffix:
            body += suffix
        return body

    cleaned = _remove_toml_tables(existing, {"mcp_servers.haft"})
    prefix = cleaned.rstrip()
    return (prefix + "\n\n" if prefix else "") + section


_TOML_TABLE_RE = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*(?:#.*)?$")


def _remove_toml_tables(text: str, table_roots: set[str]) -> str:
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    skipping = False
    for line in lines:
        match = _TOML_TABLE_RE.match(line)
        if match:
            name = match.group(1)
            skipping = any(name == root or name.startswith(root + ".") for root in table_roots)
        if not skipping:
            kept.append(line)
    return "".join(kept)


def path_escapes_repo(rel_path: str) -> bool:
    if not rel_path:
        return True
    if rel_path != rel_path.strip():
        return True
    posix = PurePosixPath(rel_path)
    windows = PureWindowsPath(rel_path)
    if posix.is_absolute() or windows.is_absolute():
        return True
    return ".." in posix.parts or ".." in windows.parts


def dry_run_enabled() -> bool:
    return bool(os.environ.get("RIG_PROJECT_TOOLS_DRY_RUN") or os.environ.get("RIG_SVERKLO_DRY_RUN"))


def sverklo_registered(repo_root: Path) -> tuple[bool, str]:
    """Return whether ``repo_root`` is in ``sverklo list``, plus a human detail."""

    exe = shutil.which("sverklo")
    if not exe:
        return False, "sverklo CLI not found on PATH"
    try:
        proc = subprocess.run([exe, "list"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"sverklo list failed: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"sverklo list exited {proc.returncode}"
        return False, detail
    try:
        target = repo_root.resolve()
    except OSError:
        target = repo_root
    for line in proc.stdout.splitlines():
        candidate = _parse_sverklo_path(line)
        if candidate is None:
            continue
        try:
            if candidate.resolve() == target:
                return True, f"registered as {candidate}"
        except OSError:
            if candidate == target:
                return True, f"registered as {candidate}"
    return False, "not registered in sverklo registry"


def _parse_sverklo_path(line: str) -> Path | None:
    stripped = line.strip()
    if not stripped or stripped.lower().startswith("registry:"):
        return None
    if not stripped.startswith(("/", "~")):
        for separator in (" — ", " - "):
            if separator not in stripped:
                continue
            candidate = stripped.rsplit(separator, 1)[-1].strip()
            if candidate.startswith(("/", "~")):
                return Path(os.path.expanduser(candidate))
        return None
    return Path(os.path.expanduser(stripped))


def run_sverklo(repo_root: Path, operation: str) -> tuple[str, str]:
    """Run one live Sverklo operation, with idempotent register and dry-run support."""

    exe = shutil.which("sverklo")
    if not exe:
        # sverklo is an optional external tool — not installed is not an error.
        return "skipped", "sverklo CLI not found on PATH — skipping (optional tool)"
    if operation == "register":
        registered, detail = sverklo_registered(repo_root)
        if registered:
            return "skipped", f"sverklo/register: {detail}"
        if dry_run_enabled():
            return "skipped", f"sverklo/register: dry-run would register {repo_root}"
        try:
            proc = subprocess.run([exe, "register", str(repo_root)], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            return "error", f"sverklo register failed: {exc}"
    elif operation == "reindex":
        if dry_run_enabled():
            return "skipped", f"sverklo/reindex: dry-run would reindex {repo_root}"
        try:
            proc = subprocess.run([exe, "reindex", str(repo_root)], capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            return "error", f"sverklo reindex failed: {exc}"
    else:
        return "error", f"unknown sverklo operation {operation!r}"
    out = (proc.stdout or proc.stderr).strip()
    if proc.returncode != 0:
        return "error", out or f"sverklo {operation} exited {proc.returncode}"
    return "created", out or f"sverklo/{operation}: ok"
