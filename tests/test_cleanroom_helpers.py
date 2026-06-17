"""Hermetic unit tests for the clean-room e2e's own helpers (no Docker required).

The Docker e2e (``test_cleanroom_e2e.py``) is opt-in and only runs when a container runtime is
present, so its non-trivial in-test logic — the settings.json checker and the fake agent-tools
fixture builder — would otherwise be exercised ONLY in that gated path. These tests run in the
default hermetic suite and pin that logic directly, so a regression in the harness's own
assertions (e.g. the ``hook_bridge`` check regressing to a bare substring match that false-passes)
is caught without standing up a container.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

# pytest's default (prepend) import mode puts this ``tests/`` dir on sys.path, so the sibling
# e2e module is importable by name. We pull its constants/helpers to test them hermetically —
# importing the module does NOT trigger its module-level skip (that only gates its own test items).
import test_cleanroom_e2e as cr


def _run_checker(tmp_path: Path, which: str, settings: dict) -> int:
    """Run the staged ``check_settings.py`` against a settings dict; return its exit code."""
    script = tmp_path / "check_settings.py"
    script.write_text(cr._CHECK_SETTINGS, encoding="utf-8")
    sfile = tmp_path / "settings.json"
    sfile.write_text(json.dumps(settings), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script), which, str(sfile)],
        capture_output=True,
        text=True,
        timeout=30,
    ).returncode


def test_check_settings_auto_mode(tmp_path: Path) -> None:
    assert _run_checker(tmp_path, "auto_mode", {"permissions": {"defaultMode": "auto"}}) == 0
    assert _run_checker(tmp_path, "auto_mode", {"permissions": {"defaultMode": "default"}}) == 1
    assert _run_checker(tmp_path, "auto_mode", {}) == 1


def test_check_settings_hook_bridge_accepts_a_wired_command(tmp_path: Path) -> None:
    wired = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": "PYTHONPATH=/x python3 -m cc_hook_bridge PreToolUse"}
                    ],
                }
            ]
        }
    }
    assert _run_checker(tmp_path, "hook_bridge", wired) == 0


def test_check_settings_hook_bridge_rejects_bare_substring_elsewhere(tmp_path: Path) -> None:
    """The tightened check must NOT pass on a stray 'cc_hook_bridge' string outside a command.

    A matcher/comment/path that merely contains the marker (but no actual command wiring it) is a
    false positive the old substring match accepted — this pins the regression shut.
    """
    decoy = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "cc_hook_bridge-is-not-wired-here",
                    "hooks": [{"type": "command", "command": "echo hi"}],
                }
            ]
        }
    }
    assert _run_checker(tmp_path, "hook_bridge", decoy) == 1
    # also reject an empty PreToolUse and a missing hooks block
    assert _run_checker(tmp_path, "hook_bridge", {"hooks": {"PreToolUse": []}}) == 1
    assert _run_checker(tmp_path, "hook_bridge", {}) == 1


def test_build_fake_agent_tools_produces_the_expected_tree(tmp_path: Path) -> None:
    """The fixture must carry exactly what the clean-room config provisions — a structurally-valid,
    self-contained agent-tools checkout, so the e2e never depends on a real one."""
    root = tmp_path / "agent-tools"
    cr._build_fake_agent_tools(root)

    # exactly 5 skills (3 universal + 2 cli) — the count the e2e asserts on
    universal_dir = root / "skills" / "universal"
    cli_dir = root / "skills" / "by-type" / "cli"
    universal = sorted(p.name for p in universal_dir.iterdir())
    cli = sorted(p.name for p in cli_dir.iterdir())
    assert universal == ["naming", "push-regularly", "shell-timeouts"]
    assert cli == ["lazy-imports", "self-registering-commands"]
    for name in universal:
        assert (universal_dir / name / "SKILL.md").is_file()
    for name in cli:
        assert (cli_dir / name / "SKILL.md").is_file()

    # the one agent-hook descriptor points at a REAL in-container path (not a placeholder)
    desc = root / "agent-hooks" / "block-no-verify" / "block-no-verify.pre-bash.json"
    assert desc.is_file()
    data = json.loads(desc.read_text(encoding="utf-8"))
    assert data["id"] == "block-no-verify"
    assert data["cmd"] == "/opt/agent-tools/agent-hooks/block-no-verify/block_no_verify.py"
    assert "PLACEHOLDER" not in data["cmd"] and "ABSOLUTE/PATH" not in data["cmd"]

    # the global dispatcher runner + the cc_hook_bridge dispatcher (so the bridge wires, not skips)
    assert (root / "git-hooks" / "global-dispatcher" / "run-global-hooks").is_file()
    assert (root / "lib" / "cc_hook_bridge" / "dispatch.py").is_file()
    # the CI slots the clean-room config enables
    assert (root / "ci" / "secret-scan" / "secret-scan.yml").is_file()
    assert (root / "ci" / "leftover-grep" / "workflow.yml").is_file()


def test_assert_script_is_valid_bash() -> None:
    """The embedded clean-room assertion script must be syntactically valid bash.

    A syntax error in the heredoc-built script would otherwise only surface when the opt-in Docker
    e2e runs; this catches it in the hermetic suite via `bash -n` (skips if bash is unavailable)."""
    import shutil

    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - bash is present everywhere the e2e would run
        import pytest

        pytest.skip("bash not available")
    r = subprocess.run([bash, "-n"], input=cr._ASSERT_SCRIPT, text=True, capture_output=True, timeout=30)
    assert r.returncode == 0, f"assert.sh has a bash syntax error:\n{r.stderr}"


def test_check_settings_is_valid_python() -> None:
    """The embedded settings checker must compile (no indentation/heredoc damage)."""
    compile(textwrap.dedent(cr._CHECK_SETTINGS), "check_settings.py", "exec")


def test_check_settings_hook_bridge_rejects_malformed_shapes(tmp_path: Path) -> None:
    """A non-list PreToolUse / a group missing `hooks` / a non-string command must not false-pass."""
    # PreToolUse is a dict, not a list
    assert _run_checker(tmp_path, "hook_bridge", {"hooks": {"PreToolUse": {"x": "cc_hook_bridge"}}}) == 1
    # a group with no `hooks` key
    assert _run_checker(tmp_path, "hook_bridge", {"hooks": {"PreToolUse": [{"matcher": "Bash"}]}}) == 1
    # a hook whose command is non-string (would crash a naive check) — guarded, so it just misses
    assert _run_checker(
        tmp_path, "hook_bridge", {"hooks": {"PreToolUse": [{"hooks": [{"command": 123}]}]}}
    ) == 1


def test_check_settings_handles_malformed_json_cleanly(tmp_path: Path) -> None:
    """Malformed JSON / a non-dict root / non-dict sections must return a clean code, not a raw
    traceback (the assert.sh `|| fail` catches the exit, but the script should stay quiet-failing)."""
    script = tmp_path / "check_settings.py"
    script.write_text(cr._CHECK_SETTINGS, encoding="utf-8")

    def rc(which: str, raw: str) -> int:
        f = tmp_path / "s.json"
        f.write_text(raw, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(script), which, str(f)], capture_output=True, text=True, timeout=30
        ).returncode

    assert rc("auto_mode", "{ not json") == 3  # malformed
    assert rc("auto_mode", "[]") == 3  # root not an object
    # a non-dict `permissions`/`hooks` is tolerated as "absent" → a clean miss (1), not a crash
    assert rc("auto_mode", '{"permissions": []}') == 1
    assert rc("hook_bridge", '{"hooks": "nope"}') == 1


def test_cleanroom_config_is_valid_yaml_with_one_src_token() -> None:
    """The clean-room rig.yaml string must parse as YAML and carry exactly one __SRC__ to substitute.

    A stray tab / bad indent (or a second/zero __SRC__) would only surface inside the container;
    this catches it in the hermetic suite. pyyaml is rig's own runtime dep; importorskip keeps
    the test honest (skip, not error) on a bare host without it."""
    import pytest

    yaml = pytest.importorskip("yaml")

    assert cr._CLEANROOM_CONFIG.count("__SRC__") == 1
    data = yaml.safe_load(cr._CLEANROOM_CONFIG.replace("__SRC__", "/opt/agent-tools"))
    assert data["version"] == 1
    assert data["agent_tools_source"] == "/opt/agent-tools"
    # the four acceptance subjects must be ON; the daemon/network categories OFF
    assert data["skills"]["enabled"] is True
    assert data["harness"]["auto_mode"] is True
    assert data["models"]["enabled"] is False
    assert data["tmux"]["enabled"] is False
    assert data["gitignore"]["enabled"] is False


def test_dockerfile_references_every_staged_dir() -> None:
    """The Dockerfile must COPY every directory `_stage_build_context` lays out — a rename in one
    place without the other would only fail inside the opt-in Docker build."""
    df = cr._DOCKERFILE
    for path in ("rig-cli", "agent-tools", "cleanroom"):
        assert f"COPY {path} " in df, f"Dockerfile does not COPY {path}"
    assert "/opt/cleanroom/assert.sh" in df  # the entrypoint script
    assert "/opt/cleanroom-out" not in df  # the dead unused dir must stay removed


def test_docker_available_honors_env_and_probe(monkeypatch) -> None:
    """`_docker_available` maps the CLI presence + `docker info` exit code, and honors $DOCKER."""
    import subprocess as sp

    # CLI absent → False, no probe
    monkeypatch.setenv("DOCKER", "definitely-not-a-real-cli-xyz")
    monkeypatch.setattr(cr.shutil, "which", lambda _name: None)
    assert cr._docker_available() is False

    # CLI present + `info` exits 0 → True; exits non-zero → False (honoring $DOCKER name)
    monkeypatch.setenv("DOCKER", "podman")
    monkeypatch.setattr(cr.shutil, "which", lambda name: "/usr/bin/podman" if name == "podman" else None)

    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return sp.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cr.subprocess, "run", fake_run)
    assert cr._docker_available() is True
    assert calls and calls[0][0] == "podman" and calls[0][1] == "info"

    monkeypatch.setattr(cr.subprocess, "run", lambda cmd, **_kw: sp.CompletedProcess(cmd, 1, "", ""))
    assert cr._docker_available() is False


def test_docker_available_treats_timeout_as_unavailable(monkeypatch) -> None:
    """A hung daemon (`docker info` timing out) must skip, not crash — return False."""
    import subprocess as sp

    monkeypatch.setenv("DOCKER", "docker")
    monkeypatch.setattr(cr.shutil, "which", lambda _name: "/usr/bin/docker")

    def boom(cmd, **_kw):
        raise sp.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(cr.subprocess, "run", boom)
    assert cr._docker_available() is False


def test_stage_build_context_lays_out_every_copied_dir(tmp_path: Path) -> None:
    """`_stage_build_context` must produce exactly the tree the Dockerfile COPYs — a rename here
    would otherwise only surface inside the opt-in Docker build."""
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    cr._stage_build_context(ctx)

    # the three COPY sources + the Dockerfile the build expects
    assert (ctx / "rig-cli" / "riglib" / "__init__.py").is_file()  # the installable package
    assert (ctx / "rig-cli" / "pyproject.toml").is_file()
    assert (ctx / "agent-tools" / "skills" / "universal").is_dir()
    assert (ctx / "cleanroom" / "rig.yaml").is_file()
    assert (ctx / "cleanroom" / "assert.sh").is_file()
    assert (ctx / "cleanroom" / "check_settings.py").is_file()
    assert (ctx / "Dockerfile").is_file()
    # the allowlist must NOT copy the repo's own .git / .venv / tests / .github (only package inputs)
    assert not (ctx / "rig-cli" / ".git").exists()
    assert not (ctx / "rig-cli" / ".venv").exists()
    assert not (ctx / "rig-cli" / "tests").exists()
    assert not (ctx / "rig-cli" / ".github").exists()


def test_run_converts_timeout_to_labeled_assertion(monkeypatch) -> None:
    """`_run` must turn a docker hang (TimeoutExpired) into a LABELED AssertionError carrying the
    command + timeout + captured output — not a bare traceback."""
    import subprocess as sp

    import pytest

    def boom(cmd, **_kw):
        raise sp.TimeoutExpired(cmd, 600, output=b"partial-out", stderr=b"partial-err")

    monkeypatch.setattr(cr.subprocess, "run", boom)
    with pytest.raises(AssertionError) as ei:
        cr._run(["docker", "build", "-t", "x", "."], timeout=600)
    msg = str(ei.value)
    assert "docker build" in msg and "600s" in msg
    assert "partial-out" in msg and "partial-err" in msg


def test_stage_build_context_raises_on_missing_required_input(tmp_path: Path, monkeypatch) -> None:
    """A missing REQUIRED rig-cli input (pyproject.toml / riglib) must raise a clear error here,
    not silently `continue` and fail obscurely inside `docker build`."""
    import pytest

    # point REPO_ROOT at an empty dir → pyproject.toml + riglib are absent
    empty = tmp_path / "empty-repo"
    empty.mkdir()
    monkeypatch.setattr(cr, "REPO_ROOT", empty)
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    with pytest.raises(FileNotFoundError, match="REQUIRED rig-cli input"):
        cr._stage_build_context(ctx)


def test_fixture_dispatcher_and_hook_scripts_are_executable(tmp_path: Path) -> None:
    """The fixture's shell scripts + the agent-hook script carry the exec bit (mirroring real
    agent-tools) — so the in-container `[ -x ... ]` assertions aren't leaning on rig chmod'ing."""
    root = tmp_path / "agent-tools"
    cr._build_fake_agent_tools(root)
    runner = root / "git-hooks" / "global-dispatcher" / "run-global-hooks"
    hook = root / "agent-hooks" / "block-no-verify" / "block_no_verify.py"
    for p in (runner, hook):
        assert p.stat().st_mode & 0o111, f"{p} is not executable"


def test_idempotency_regex_distinguishes_clean_from_dirty() -> None:
    """The assert script's idempotency gate regex must pass an all-skipped summary and fail any
    summary with a non-zero created/updated/backed_up/error/failed counter. Extracted from the
    embedded script so the test and the script can't drift."""
    import re

    # pull the exact ERE the script greps with, so this pins the real signal
    m = re.search(r"grep -Eq '\(([^']+)\)=\[1-9\]'", cr._ASSERT_SCRIPT)
    assert m, "could not find the idempotency regex in _ASSERT_SCRIPT"
    ere = re.compile(rf"({m.group(1)})=[1-9]")

    assert not ere.search("Summary: skipped=18")
    assert not ere.search("Summary: created=0 updated=0 backed_up=0 skipped=18")
    assert ere.search("Summary: created=2 skipped=16")
    assert ere.search("Summary: updated=1")
    assert ere.search("Summary: backed_up=3")
    assert ere.search("Summary: error=1")
    assert ere.search("Summary: failed=2")


def test_scaffold_leg_env_vars_are_consumed_by_riglib() -> None:
    """The no-config scaffold leg sets RIG_AGENT_TOOLS_SOURCE / RIG_SCHEDULE_DRY_RUN /
    RIG_TMUX_DRY_RUN. This is a tripwire: if any is renamed/removed in riglib, that leg would only
    fail inside the opt-in container — so assert each is still referenced in the installed package."""
    riglib_dir = Path(cr.REPO_ROOT) / "riglib"
    blob = "\n".join(
        p.read_text(encoding="utf-8") for p in riglib_dir.rglob("*.py")
    )
    for var in ("RIG_AGENT_TOOLS_SOURCE", "RIG_SCHEDULE_DRY_RUN", "RIG_TMUX_DRY_RUN"):
        assert var in blob, f"{var} is no longer consumed by riglib — the scaffold leg would break"
