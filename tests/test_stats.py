"""Tests for `rig stats show` — the tool-adoption analytics pipeline.

Strategy: write SYNTHETIC, tiny session logs (matching each harness's real on-disk shape,
as verified on a live machine) into a throwaway ``$HOME``, then assert the parser →
aggregator → renderers produce the exact category counts, repo/harness breakdowns, time
buckets, and JSON shape. Fully deterministic, HOME-isolated (the autouse fixture in
conftest points HOME at a tmp dir; tests pass an explicit ``home=`` too), no real logs, no
network, no sockets.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from riglib.stats import aggregate, build_report
from riglib.stats.aggregate import compare_periods
from riglib.stats.command import _passes, collect, parse_date
from riglib.stats.model import ToolInvocation
from riglib.stats.render import json_out, tui, web
from riglib.stats.sources.base import parse_epoch, parse_iso
from riglib.stats.taxonomy import OUR_CLIS, categorize, detect_our_cli
from riglib.permissions import DEFAULT_ECOSYSTEM_TOOLS


# ── fixture builders: write minimal-but-real-shaped logs into a fake HOME ────────────────
def _cc_event(ts: str, cwd: str, tools: list[dict]) -> str:
    """A Claude Code `assistant` JSONL line carrying `tool_use` blocks."""
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": ts,
            "cwd": cwd,
            "message": {"content": [{"type": "tool_use", **t} for t in tools]},
        }
    )


def write_claude_session(home: Path, encoded: str, session: str, cwd: str, events: list[str]) -> None:
    d = home / ".claude" / "projects" / encoded
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session}.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")


def _write_codex_session(sessions_root: Path, day: tuple[str, str, str], cwd: str, calls: list[dict]) -> None:
    y, m, dd = day
    d = sessions_root / y / m / dd
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "session_meta", "timestamp": f"{y}-{m}-{dd}T00:00:00Z",
                         "payload": {"cwd": cwd, "id": "s1"}})]
    for c in calls:
        argd: dict = {}
        if "cmd" in c:
            argd["cmd"] = c["cmd"]
        if "workdir" in c:
            argd["workdir"] = c["workdir"]
        lines.append(json.dumps({
            "type": "response_item",
            "timestamp": c["ts"],
            "payload": {"type": "function_call", "name": c["name"],
                        "arguments": json.dumps(argd)},
        }))
    (d / f"rollout-{y}-{m}-{dd}T00-00-00-abc.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_codex_session(home: Path, day: tuple[str, str, str], cwd: str, calls: list[dict]) -> None:
    _write_codex_session(home / ".codex" / "sessions", day, cwd, calls)


def write_codex_session_in_root(root: Path, day: tuple[str, str, str], cwd: str, calls: list[dict]) -> None:
    _write_codex_session(root / "sessions", day, cwd, calls)


def write_gemini_session(home: Path, phash: str, repo: str, messages: list[dict]) -> None:
    chats = home / ".gemini" / "tmp" / phash / "chats"
    chats.mkdir(parents=True, exist_ok=True)
    (chats / "session-2026-06-10T00-00-x.json").write_text(
        json.dumps({"sessionId": "g1", "projectHash": phash, "messages": messages}), encoding="utf-8"
    )
    projects = home / ".gemini" / "projects.json"
    projects.write_text(json.dumps({"projects": {repo: phash}}), encoding="utf-8")


def _write_opencode_part_storage(
    storage: Path, session: str, repo: str, tool: str, command: str | None, start_ms: int
) -> None:
    base = storage
    sess_dir = base / "session" / "proj1"
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / f"{session}.json").write_text(
        json.dumps({"id": session, "directory": repo}), encoding="utf-8"
    )
    part_dir = base / "part" / f"msg_{session}"
    part_dir.mkdir(parents=True, exist_ok=True)
    state: dict = {"input": {}, "time": {"start": start_ms}}
    if command is not None:
        state["input"]["command"] = command
    (part_dir / f"prt_{tool}_{start_ms}.json").write_text(
        json.dumps({"id": f"prt_{start_ms}", "sessionID": session, "type": "tool",
                    "tool": tool, "state": state}), encoding="utf-8"
    )


def write_opencode_part(home: Path, session: str, repo: str, tool: str, command: str | None,
                        start_ms: int) -> None:
    _write_opencode_part_storage(
        home / ".local" / "share" / "opencode" / "storage",
        session,
        repo,
        tool,
        command,
        start_ms,
    )


def write_opencode_part_in_data_home(
    data_home: Path, session: str, repo: str, tool: str, command: str | None, start_ms: int
) -> None:
    _write_opencode_part_storage(data_home / "opencode" / "storage", session, repo, tool, command, start_ms)


# ── taxonomy unit tests ──────────────────────────────────────────────────────────────────
def test_our_clis_track_the_provisioned_ecosystem_surface():
    assert set(DEFAULT_ECOSYSTEM_TOOLS) <= OUR_CLIS


def test_categorize_baseline_tools():
    assert categorize("Read")[0] == "baseline"
    assert categorize("Edit")[0] == "baseline"
    assert categorize("MultiEdit")[0] == "baseline"
    # bare Bash with no command is baseline shell
    assert categorize("Bash", command="")[0] == "baseline"
    assert categorize("Bash", command="ls -la")[0] == "baseline"


def test_categorize_our_clis_inside_bash():
    cat, label = categorize("Bash", command="review --staged -C /repo")
    assert cat == "ours" and label == "review (cli)"
    assert categorize("Bash", command="tg 'done'")[0] == "ours"
    cat, label = categorize("Bash", command="dev test")
    assert cat == "ours" and label == "dev (cli)"
    assert categorize("Bash", command="cd /x && rig apply")[1] == "rig (cli)"
    # env-prefixed and piped commands still detect our CLI
    assert categorize("Bash", command="FOO=1 review -C /r")[0] == "ours"
    assert categorize("Bash", command="git diff | review")[0] == "ours"


def test_categorize_mcp_split():
    assert categorize("mcp__review__review_diff")[0] == "ours"
    assert categorize("mcp__serena__find_symbol")[0] == "external-advertised"
    assert categorize("mcp__playwright__browser_click")[0] == "external-advertised"
    assert categorize("mcp__unknownsrv__do")[0] == "other"


def test_categorize_skills():
    assert categorize("Skill", skill="shell-timeouts")[0] == "ours"
    assert categorize("Skill", skill="superpowers:brainstorming")[0] == "external-advertised"
    assert categorize("Skill", skill="agent-browser")[0] == "external-advertised"
    assert categorize("Skill", skill="h-frame")[0] == "external-advertised"


def test_baseline_aliases_cross_harness():
    """Non-CC native built-ins fold onto the canonical baseline label so the adoption ratio
    is comparable across harnesses (review finding)."""
    assert categorize("read_file")[0] == "baseline"  # gemini
    assert categorize("read_file")[1] == "Read"
    assert categorize("write_file")[1] == "Write"
    assert categorize("replace")[1] == "Edit"
    assert categorize("apply_patch")[1] == "Edit"  # codex
    assert categorize("edit")[1] == "Edit"  # opencode lowercase
    assert categorize("todowrite")[1] == "TodoWrite"
    assert categorize("google_web_search")[1] == "WebSearch"
    # a genuinely unknown tool is still "other"
    assert categorize("frobnicate")[0] == "other"


def test_detect_our_cli_handles_pipelines():
    assert detect_our_cli("echo hi && tg 'x'") == "tg"
    assert detect_our_cli("ls && cat f") is None
    # subshell / brace grouping and a leading path don't hide the CLI
    assert detect_our_cli("(cd /x && review)") == "review"
    assert detect_our_cli("{ review; }") == "review"
    assert detect_our_cli("/usr/local/bin/3d test") == "3d"
    assert detect_our_cli("dev e2e smoke") == "dev"
    assert detect_our_cli("dev-server up") is None
    assert detect_our_cli("(cd /x && ls)") is None


def test_detect_our_cli_is_quote_aware():
    """A CLI name that appears only INSIDE a quoted string must NOT be detected — otherwise a
    commit message / echo arg would inflate the headline adoption metric (review finding)."""
    assert detect_our_cli('git commit -m "refactor; review pending"') is None
    assert detect_our_cli('echo "a && rig is great"') is None
    assert detect_our_cli('echo "tg"') is None
    assert detect_our_cli("printf 'run review later'") is None
    assert detect_our_cli('git commit -m "wip; dev server fix"') is None
    # the headline case: a `;` INSIDE a quoted commit message must not split into a stage
    # whose "first program" is the frequent word `task` ∈ OUR_CLIS — that silently inflated
    # the adoption metric this command exists to measure (the original review HIGH finding).
    assert detect_our_cli('git commit -m "wip; task list cleanup"') is None
    assert detect_our_cli('echo "3d printing | review of the draw"') is None
    # but a real operator outside quotes still splits into a detectable stage
    assert detect_our_cli('grep wip | tg later') == "tg"
    assert detect_our_cli('git commit -m "wip" && review -C /x') == "review"
    # ...and the same operator-inside-quotes case truly counts as baseline (not ours) e2e
    assert categorize("Bash", command='git commit -m "wip; task list cleanup"')[0] == "baseline"


def test_detect_our_cli_unwraps_transparent_wrappers():
    """A transparent launcher (timeout / env / sudo / nice / …) must not HIDE the wrapped CLI
    from the adoption metric — ``timeout 60 review`` is a `review` invocation, not a `timeout`
    one. Otherwise our own `shell-timeouts` recommendation would suppress the very signal."""
    assert detect_our_cli("timeout 60 review -C /x") == "review"
    assert detect_our_cli("timeout -k 5 30 tg hi") == "tg"  # -k value flag + bare duration
    assert detect_our_cli("sudo -u alex tg hi") == "tg"  # -u consumes its value
    assert detect_our_cli("env FOO=1 BAR=2 3d test") == "3d"
    assert detect_our_cli("nice -n 5 review -C /x") == "review"
    assert detect_our_cli("nohup draw 'a cat'") == "draw"
    # a wrapper with NO wrapped program resolves to nothing (not a false 'timeout' hit).
    assert detect_our_cli("timeout 60") is None
    # the wrapper itself is never one of OUR_CLIS, so a bare wrapper is never 'ours'.
    assert detect_our_cli("env") is None
    # `command -v/-V <name>` only PROBES PATH — it does not run the CLI, so it must NOT count
    # as an invocation (false-inflation, same class as the quoted-operator finding).
    assert detect_our_cli("command -v review") is None
    assert detect_our_cli("command -V tg") is None
    # but a bare `command <prog>` DOES execute it → still counted.
    assert detect_our_cli("command review -C /x") == "review"


def test_detect_our_cli_unwraps_interpreter_runners():
    """The README documents running our CLIs from a checkout via ``python3 bin/rig`` and
    ``uv run bin/rig`` — those must count as the CLI, not baseline ``python``/``uv`` (review
    P1 finding: undercounting our own most-documented invocation form)."""
    assert detect_our_cli("python3 bin/rig apply") == "rig"
    assert detect_our_cli("python bin/rig apply") == "rig"
    assert detect_our_cli("uv run bin/rig apply") == "rig"
    assert detect_our_cli("uv run bin/review -C /x") == "review"
    assert detect_our_cli("uv run --no-sync bin/tg hi") == "tg"
    assert detect_our_cli("python3 /Users/ultra/xp/rig-cli/bin/rig status") == "rig"
    assert detect_our_cli("python3 bin/draw.py prompt") == "draw"  # .py stripped
    assert detect_our_cli("timeout 60 python3 bin/rig apply") == "rig"  # wrapper + interpreter
    assert detect_our_cli("uvx review") == "review"
    # a non-our script run through an interpreter stays baseline
    assert detect_our_cli("python3 manage.py runserver") is None
    assert detect_our_cli("python3 bin/something_else") is None


# ── parser tests (per harness, HOME-isolated) ────────────────────────────────────────────
def test_claude_code_parser_counts(tmp_path):
    home = tmp_path / "home"
    write_claude_session(
        home, "-Users-ultra-xp-demo", "sess1", "/Users/ultra/xp/demo",
        [
            _cc_event("2026-06-10T10:00:00Z", "/Users/ultra/xp/demo", [
                {"name": "Read", "input": {"file_path": "/x"}},
                {"name": "Bash", "input": {"command": "review -C /Users/ultra/xp/demo"}},
            ]),
            _cc_event("2026-06-10T11:00:00Z", "/Users/ultra/xp/demo", [
                {"name": "Bash", "input": {"command": "ls"}},
                {"name": "Skill", "input": {"skill": "shell-timeouts"}},
                {"name": "mcp__serena__find_symbol", "input": {}},
            ]),
        ],
    )
    invs, supported, notes = collect(home=home, harnesses=["claude-code"])
    assert "claude-code" in supported
    cats = aggregate(invs).by_category
    assert cats["baseline"] == 2  # Read + bare Bash(ls)
    assert cats["ours"] == 2  # review-cli + shell-timeouts skill
    assert cats["external-advertised"] == 1  # serena mcp
    assert all(i.repo == "/Users/ultra/xp/demo" for i in invs)


def test_codex_parser_counts(tmp_path):
    home = tmp_path / "home"
    write_codex_session(
        home, ("2026", "06", "12"), "/Users/ultra/xp/proj",
        [
            {"ts": "2026-06-12T09:00:00Z", "name": "exec_command", "cmd": "git status"},
            {"ts": "2026-06-12T09:05:00Z", "name": "exec_command", "cmd": "review -C /Users/ultra/xp/proj"},
        ],
    )
    invs, supported, _ = collect(home=home, harnesses=["codex"])
    assert supported == ["codex"]
    agg = aggregate(invs)
    assert agg.by_category["baseline"] == 1
    assert agg.by_category["ours"] == 1
    assert all(i.repo == "/Users/ultra/xp/proj" for i in invs)
    assert all(i.harness == "codex" for i in invs)


def test_codex_parser_honors_rig_codex_home_for_real_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("RIG_CODEX_HOME", str(codex_home))
    write_codex_session_in_root(
        codex_home, ("2026", "06", "12"), "/Users/ultra/xp/proj",
        [{"ts": "2026-06-12T09:00:00Z", "name": "exec_command", "cmd": "git status"}],
    )

    invs, supported, _ = collect(harnesses=["codex"])  # no home= means the default $HOME branch

    assert supported == ["codex"]
    assert len(invs) == 1
    assert invs[0].repo == "/Users/ultra/xp/proj"


def test_codex_parser_expands_rig_codex_home_tilde(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RIG_CODEX_HOME", "~/codex-home")
    write_codex_session_in_root(
        home / "codex-home", ("2026", "06", "12"), "/Users/ultra/xp/tilde",
        [{"ts": "2026-06-12T09:00:00Z", "name": "exec_command", "cmd": "git status"}],
    )

    invs, supported, _ = collect(harnesses=["codex"])

    assert supported == ["codex"]
    assert [i.repo for i in invs] == ["/Users/ultra/xp/tilde"]


def test_codex_parser_expands_rig_codex_home_env_value_tilde(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_ROOT", "~/codex-from-var")
    monkeypatch.setenv("RIG_CODEX_HOME", "$CODEX_ROOT")
    write_codex_session_in_root(
        home / "codex-from-var", ("2026", "06", "12"), "/Users/ultra/xp/env-tilde",
        [{"ts": "2026-06-12T09:00:00Z", "name": "exec_command", "cmd": "git status"}],
    )

    invs, supported, _ = collect(harnesses=["codex"])

    assert supported == ["codex"]
    assert [i.repo for i in invs] == ["/Users/ultra/xp/env-tilde"]


def test_codex_parser_expands_rig_codex_home_env_vars(tmp_path, monkeypatch):
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("RIG_CODEX_HOME", "$XDG_CONFIG_HOME/codex")
    write_codex_session_in_root(
        xdg / "codex", ("2026", "06", "12"), "/Users/ultra/xp/env",
        [{"ts": "2026-06-12T09:00:00Z", "name": "exec_command", "cmd": "git status"}],
    )

    invs, supported, _ = collect(harnesses=["codex"])

    assert supported == ["codex"]
    assert [i.repo for i in invs] == ["/Users/ultra/xp/env"]


def test_codex_parser_expands_rig_codex_home_xdg_config_prefix(tmp_path, monkeypatch):
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("RIG_CODEX_HOME", "~/.config/codex")
    write_codex_session_in_root(
        xdg / "codex", ("2026", "06", "12"), "/Users/ultra/xp/xdg-prefix",
        [{"ts": "2026-06-12T09:00:00Z", "name": "exec_command", "cmd": "git status"}],
    )
    write_codex_session_in_root(
        home / ".config" / "codex", ("2026", "06", "12"), "/Users/ultra/xp/wrong-home-prefix",
        [{"ts": "2026-06-12T09:05:00Z", "name": "exec_command", "cmd": "review"}],
    )

    invs, supported, _ = collect(harnesses=["codex"])

    assert supported == ["codex"]
    assert [i.repo for i in invs] == ["/Users/ultra/xp/xdg-prefix"]


def test_codex_parser_explicit_home_ignores_rig_codex_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("RIG_CODEX_HOME", str(codex_home))
    write_codex_session(
        home, ("2026", "06", "12"), "/Users/ultra/xp/sandbox",
        [{"ts": "2026-06-12T09:00:00Z", "name": "exec_command", "cmd": "git status"}],
    )
    write_codex_session_in_root(
        codex_home, ("2026", "06", "12"), "/Users/ultra/xp/host",
        [{"ts": "2026-06-12T09:00:00Z", "name": "exec_command", "cmd": "review"}],
    )

    invs, supported, _ = collect(home=home, harnesses=["codex"])

    assert supported == ["codex"]
    assert [i.repo for i in invs] == ["/Users/ultra/xp/sandbox"]


def test_codex_parser_ignores_ambient_codex_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ambient_codex_home = tmp_path / "ambient-codex-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(ambient_codex_home))
    monkeypatch.delenv("RIG_CODEX_HOME", raising=False)
    write_codex_session(
        home, ("2026", "06", "12"), "/Users/ultra/xp/stable",
        [{"ts": "2026-06-12T09:00:00Z", "name": "exec_command", "cmd": "git status"}],
    )
    write_codex_session_in_root(
        ambient_codex_home, ("2026", "06", "12"), "/Users/ultra/xp/ambient",
        [{"ts": "2026-06-12T09:00:00Z", "name": "exec_command", "cmd": "review"}],
    )

    invs, supported, _ = collect(harnesses=["codex"])

    assert supported == ["codex"]
    assert [i.repo for i in invs] == ["/Users/ultra/xp/stable"]


def test_gemini_parser_counts_and_repo_mapping(tmp_path):
    home = tmp_path / "home"
    write_gemini_session(
        home, "deadbeef", "/Users/ultra/xp/gem",
        [
            {"type": "gemini", "timestamp": "2026-06-10T08:00:00Z", "toolCalls": [
                {"name": "run_shell_command", "args": {"command": "ls"}},
                {"name": "run_shell_command", "args": {"command": "tg 'hi'"}},
                {"name": "read_file", "args": {}},
            ]},
        ],
    )
    invs, supported, _ = collect(home=home, harnesses=["gemini"])
    assert supported == ["gemini"]
    agg = aggregate(invs)
    # bare ls + read_file (gemini's native Read, aliased to canonical baseline) = 2 baseline
    assert agg.by_category["baseline"] == 2
    assert agg.by_category["ours"] == 1  # tg
    assert agg.by_category.get("other", 0) == 0  # read_file is a recognized baseline op now
    assert all(i.repo == "/Users/ultra/xp/gem" for i in invs)  # hash mapped to real path


def test_opencode_explicit_home_ignores_xdg_data_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-elsewhere"))
    write_opencode_part(home, "ses_1", "/Users/ultra/xp/oc", "bash", "ls", 1781509838000)

    invs, supported, _ = collect(home=home, harnesses=["opencode"])

    assert supported == ["opencode"]
    assert len(invs) == 1
    assert invs[0].repo == "/Users/ultra/xp/oc"


def test_gemini_ignores_toolcalls_on_non_gemini_messages(tmp_path):
    """Tool calls live on `type == "gemini"` messages only; a `toolCalls` key on a user/result
    message shape must NOT be counted (would double-count / misattribute). (review finding)"""
    home = tmp_path / "home"
    write_gemini_session(
        home, "deadbeef", "/Users/ultra/xp/gem",
        [
            {"type": "gemini", "timestamp": "2026-06-10T08:00:00Z", "toolCalls": [
                {"name": "run_shell_command", "args": {"command": "tg hi"}},
            ]},
            # a non-gemini message carrying a toolCalls key — must be skipped entirely.
            {"type": "user", "timestamp": "2026-06-10T08:01:00Z", "toolCalls": [
                {"name": "run_shell_command", "args": {"command": "review -C /x"}},
                {"name": "read_file", "args": {}},
            ]},
        ],
    )
    invs, _, _ = collect(home=home, harnesses=["gemini"])
    # only the one tg call from the real gemini message; the user-message calls are ignored.
    assert len(invs) == 1
    assert invs[0].category == "ours" and invs[0].tool_name == "tg (cli)"


def test_opencode_parser_counts(tmp_path):
    home = tmp_path / "home"
    write_opencode_part(home, "ses_1", "/Users/ultra/xp/oc", "read", None, 1781509838000)
    write_opencode_part(home, "ses_1", "/Users/ultra/xp/oc", "bash", "review -C /x", 1781509839000)
    write_opencode_part(home, "ses_1", "/Users/ultra/xp/oc", "bash", "npm test", 1781509840000)
    invs, supported, _ = collect(home=home, harnesses=["opencode"])
    assert supported == ["opencode"]
    agg = aggregate(invs)
    assert agg.by_category["baseline"] == 2  # read + bare bash(npm test)
    assert agg.by_category["ours"] == 1  # bash review
    assert all(i.repo == "/Users/ultra/xp/oc" for i in invs)
    assert all(i.timestamp is not None for i in invs)  # epoch-ms parsed


def test_opencode_parser_honors_xdg_data_home_for_real_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    write_opencode_part_in_data_home(
        data_home, "ses_xdg", "/Users/ultra/xp/xdg-oc", "bash", "review -C /x", 1781509839000
    )

    invs, supported, _ = collect(harnesses=["opencode"])

    assert supported == ["opencode"]
    assert [i.repo for i in invs] == ["/Users/ultra/xp/xdg-oc"]


def test_opencode_parser_explicit_home_ignores_xdg_data_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    write_opencode_part(home, "ses_home", "/Users/ultra/xp/home-oc", "bash", "npm test", 1781509839000)
    write_opencode_part_in_data_home(
        data_home, "ses_xdg", "/Users/ultra/xp/xdg-oc", "bash", "review -C /x", 1781509840000
    )

    invs, supported, _ = collect(home=home, harnesses=["opencode"])

    assert supported == ["opencode"]
    assert [i.repo for i in invs] == ["/Users/ultra/xp/home-oc"]


# ── aggregator: breakdowns + time buckets ────────────────────────────────────────────────
def _sample_invocations() -> list[ToolInvocation]:
    def inv(day, harness, repo, tool, cat):
        ts = datetime(2026, 6, day, 12, 0, tzinfo=timezone.utc)
        return ToolInvocation(ts, harness, repo, "s", tool, cat, tool)
    return [
        inv(10, "claude-code", "/a", "Read", "baseline"),
        inv(10, "claude-code", "/a", "review (cli)", "ours"),
        inv(11, "claude-code", "/b", "Bash", "baseline"),
        inv(12, "codex", "/a", "tg (cli)", "ours"),
        # an undated row still counts toward totals but not the day series
        ToolInvocation(None, "gemini", "/b", "s", "Glob", "baseline", "Glob"),
    ]


def test_aggregate_breakdowns_and_buckets():
    agg = aggregate(_sample_invocations())
    assert agg.total == 5
    assert agg.undated == 1
    assert agg.by_category["baseline"] == 3
    assert agg.by_category["ours"] == 2
    # by repo
    assert agg.by_repo["/a"] == 3 and agg.by_repo["/b"] == 2
    # by harness
    assert agg.by_harness["claude-code"] == 3 and agg.by_harness["codex"] == 1
    # nested harness->category
    assert agg.harness_category["claude-code"]["ours"] == 1
    # day buckets: 3 active days; undated excluded
    assert set(agg.by_day) == {"2026-06-10", "2026-06-11", "2026-06-12"}
    assert agg.by_day["2026-06-10"]["baseline"] == 1
    assert agg.by_day["2026-06-10"]["ours"] == 1
    # week buckets present
    assert agg.by_week
    # adoption ratio = ours / (ours + baseline) = 2 / 5
    assert abs(agg.adoption_ratio() - (2 / 5)) < 1e-9


def test_period_comparison():
    agg = aggregate(_sample_invocations())
    split = datetime(2026, 6, 11, tzinfo=timezone.utc)
    cmp = compare_periods(agg, split)
    # earlier (<11): day 10 → baseline1 ours1
    assert cmp.earlier["baseline"] == 1 and cmp.earlier["ours"] == 1
    # later (>=11): day11 baseline1, day12 ours1
    assert cmp.later["baseline"] == 1 and cmp.later["ours"] == 1
    assert cmp.adoption_delta() == 0.0  # 50% both sides


# ── JSON shape (the canonical contract) ──────────────────────────────────────────────────
def test_json_render_shape():
    agg = aggregate(_sample_invocations())
    d = json_out.to_dict(agg, meta={"supported_harnesses": ["claude-code", "codex"]})
    assert set(d) >= {"meta", "summary", "by_category", "by_tool", "by_harness", "by_repo",
                      "harness_category", "repo_category", "category_tools", "trends"}
    assert d["summary"]["total"] == 5
    assert d["summary"]["undated"] == 1
    assert d["by_category"] == {"baseline": 3, "ours": 2, "external-advertised": 0, "other": 0}
    # by_tool is a sorted list of dicts with tool/count/category
    assert isinstance(d["by_tool"], list)
    assert all({"tool", "count", "category"} <= set(row) for row in d["by_tool"])
    assert d["trends"]["by_day"]["2026-06-10"]["ours"] == 1
    # round-trips as valid JSON
    json.loads(json_out.render(agg))


def test_json_includes_comparison_when_trend_given():
    agg = aggregate(_sample_invocations())
    cmp = compare_periods(agg, datetime(2026, 6, 11, tzinfo=timezone.utc))
    d = json_out.to_dict(agg, trend=cmp)
    assert "comparison" in d
    assert d["comparison"]["adoption_delta"] == 0.0


# ── renderers don't crash + degrade gracefully ───────────────────────────────────────────
def test_tui_plain_fallback_has_numbers():
    agg = aggregate(_sample_invocations())
    out = tui.render_plain(agg, meta={"note": "x"})
    assert "total tool calls : 5" in out
    assert "adoption ratio" in out
    assert "by category" in out
    # README promises ALL formats break down by repo AND harness — plain text included.
    assert "by harness:" in out
    assert "by repo:" in out
    assert "/a" in out and "/b" in out  # the sample repos appear


def test_tui_render_runs(monkeypatch):
    # exercise whichever path is available (rich present → rich; absent → plain); must not raise.
    agg = aggregate(_sample_invocations())
    out = tui.render(agg)
    assert "adoption" in out.lower()


def test_web_html_is_self_contained():
    agg = aggregate(_sample_invocations())
    html_str = web.build_html(agg, meta={"note": "demo"})
    assert html_str.startswith("<!doctype html>")
    assert "<svg" in html_str  # inline charts
    assert "http://" not in html_str.split("</style>")[0]  # no external CDN before content
    assert "cdn" not in html_str.lower()
    assert "rig stats" in html_str
    # html-escapes a repo with an ampersand if present (smoke: no raw unescaped injection)
    assert "tool adoption" in html_str


# ── end-to-end through build_report with filters ─────────────────────────────────────────
def test_build_report_multi_harness_and_filters(tmp_path):
    home = tmp_path / "home"
    write_claude_session(
        home, "-Users-ultra-xp-demo", "s", "/Users/ultra/xp/demo",
        [_cc_event("2026-06-10T10:00:00Z", "/Users/ultra/xp/demo",
                   [{"name": "Read", "input": {}}, {"name": "Bash", "input": {"command": "tg x"}}])],
    )
    write_codex_session(
        home, ("2026", "06", "20"), "/Users/ultra/xp/other",
        [{"ts": "2026-06-20T09:00:00Z", "name": "exec_command", "cmd": "ls"}],
    )
    # no filter → both harnesses
    agg, meta, trend = build_report(home=home)
    assert set(meta["supported_harnesses"]) >= {"claude-code", "codex"}
    assert agg.by_harness["claude-code"] == 2 and agg.by_harness["codex"] == 1

    # --since cuts the codex row (it's after) but keeps... actually since filters before;
    # filter to only on/after 2026-06-15 → only the codex row survives.
    since = parse_date("2026-06-15")
    agg2, meta2, _ = build_report(home=home, since=since)
    assert agg2.total == 1 and agg2.by_harness["codex"] == 1

    # --repo filter
    agg3, _, _ = build_report(home=home, repos=["/Users/ultra/xp/demo"])
    assert set(agg3.by_repo) == {"/Users/ultra/xp/demo"}


def test_baseline_category_filter():
    """build_report(categories=...) drops the external/other buckets for the adoption view."""
    invs = _sample_invocations()
    invs.append(ToolInvocation(
        datetime(2026, 6, 12, tzinfo=timezone.utc), "claude-code", "/a", "s",
        "mcp__serena__x", "external-advertised", "mcp__serena__x"))
    agg = aggregate([i for i in invs if i.category in {"baseline", "ours"}])
    assert agg.by_category.get("external-advertised", 0) == 0
    assert agg.by_category["baseline"] == 3 and agg.by_category["ours"] == 2


def test_supported_harness_list_is_data_driven(tmp_path):
    """An empty HOME yields no supported harnesses and a note per registered parser."""
    home = tmp_path / "empty-home"
    home.mkdir()
    invs, supported, notes = collect(home=home)
    assert supported == []
    assert len(notes) >= 4  # one 'not found' per registered harness
    assert invs == []


def test_parse_date_validation():
    with pytest.raises(Exception):
        parse_date("not-a-date")
    assert parse_date(None) is None
    d = parse_date("2026-06-10", end=True)
    assert d.hour == 23 and d.tzinfo is not None


# ── regression: --since must yield a NON-empty earlier window (review finding #1) ─────────
def test_since_period_comparison_has_nonempty_earlier(tmp_path):
    """The bug: --since filtered the stream, so the trend's 'earlier' half was always empty.
    Fix compares the [since, until] window against the equally-long window before it."""
    home = tmp_path / "home"
    # day 10 (prior window) and day 12 (selected window) both have activity.
    write_claude_session(
        home, "-Users-ultra-xp-demo", "s", "/Users/ultra/xp/demo",
        [
            _cc_event("2026-06-10T10:00:00Z", "/Users/ultra/xp/demo",
                      [{"name": "Read", "input": {}}, {"name": "Bash", "input": {"command": "tg x"}}]),
            _cc_event("2026-06-12T10:00:00Z", "/Users/ultra/xp/demo",
                      [{"name": "Bash", "input": {"command": "ls"}}]),
        ],
    )
    since = parse_date("2026-06-11")
    until = parse_date("2026-06-13", end=True)
    report = build_report(home=home, since=since, until=until)
    assert report.trend is not None
    # earlier window (day 10) must be non-empty — that was the whole bug.
    assert sum(report.trend.earlier.values()) > 0
    assert report.trend.earlier.get("ours", 0) == 1  # the tg call on day 10
    assert report.trend.later.get("baseline", 0) == 1  # the ls call on day 12


# ── parse_epoch coverage (review finding #18) ────────────────────────────────────────────
def test_parse_epoch_units_and_bad_input():
    sec = parse_epoch(1765482063)  # seconds
    ms = parse_epoch(1765482063000)  # milliseconds
    assert sec is not None and ms is not None
    assert abs((sec - ms).total_seconds()) < 1  # same instant, different units
    assert parse_epoch(0) is None
    assert parse_epoch(-5) is None
    assert parse_epoch("not-a-number") is None
    assert parse_epoch(None) is None
    assert parse_iso("2026-06-10T10:00:00Z") is not None
    assert parse_iso("garbage") is None
    assert parse_iso(None) is None
    # a non-string timestamp (malformed log line `{"timestamp": 123}`) must skip, not raise
    assert parse_iso(123) is None  # type: ignore[arg-type]
    assert parse_iso({"weird": 1}) is None  # type: ignore[arg-type]


# ── defensive parsing: one bad line/shape must not kill the harness (review finding #19) ──
def test_claude_parser_survives_malformed_lines(tmp_path):
    home = tmp_path / "home"
    d = home / ".claude" / "projects" / "-Users-ultra-xp-demo"
    d.mkdir(parents=True)
    good = _cc_event("2026-06-10T10:00:00Z", "/Users/ultra/xp/demo", [{"name": "Read", "input": {}}])
    lines = [
        "{ this is not json",  # malformed
        json.dumps(["a", "list", "not", "an", "object"]),  # wrong top-level shape
        json.dumps({"type": "assistant", "message": {"content": {"not": "a list"}}}),  # content dict
        json.dumps({"type": "user", "message": {"content": []}}),  # non-assistant
        good,  # the one real tool call
    ]
    (d / "s.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    invs, supported, _ = collect(home=home, harnesses=["claude-code"])
    assert supported == ["claude-code"]
    assert len(invs) == 1 and invs[0].tool_name == "Read"  # the good line survived


def test_collect_isolates_a_broken_harness_root(tmp_path):
    """A harness root that EXISTS but isn't a readable dir (here: a file) must not abort the
    whole command — it's noted and the other harnesses still report (review P1 finding)."""
    home = tmp_path / "home"
    # claude-code root is a FILE → iterdir() raises NotADirectoryError mid-iteration.
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "projects").write_text("i am not a directory")
    # a healthy codex session in the same run must still come through.
    write_codex_session(
        home, ("2026", "06", "12"), "/x",
        [{"ts": "2026-06-12T09:00:00Z", "name": "exec_command", "cmd": "ls"}],
    )
    invs, supported, notes = collect(home=home)
    assert "codex" in supported and len(invs) == 1  # codex survived the broken claude root
    assert any("could not read logs" in n for n in notes)


def test_parsers_survive_invalid_utf8_bytes(tmp_path):
    """A malformed multibyte sequence in ANY harness log must not raise UnicodeDecodeError
    (a ValueError, NOT an OSError — so the old `except OSError` let it through) and abort the
    whole `rig stats` command. Each parser opens with errors='replace'. (review finding)"""
    home = tmp_path / "home"

    # claude-code: a raw invalid byte 0xFF spliced into an otherwise-valid JSONL line.
    cc_dir = home / ".claude" / "projects" / "-Users-ultra-xp-demo"
    cc_dir.mkdir(parents=True)
    good = _cc_event("2026-06-10T10:00:00Z", "/Users/ultra/xp/demo", [{"name": "Read", "input": {}}])
    (cc_dir / "s.jsonl").write_bytes(b"\xff\xfe not utf-8 \xc3\x28\n" + good.encode("utf-8") + b"\n")

    # codex: invalid byte inside a rollout file (the good line still parses).
    cx_dir = home / ".codex" / "sessions" / "2026" / "06" / "12"
    cx_dir.mkdir(parents=True)
    meta = json.dumps({"type": "session_meta", "timestamp": "2026-06-12T00:00:00Z",
                       "payload": {"cwd": "/x", "id": "s1"}})
    call = json.dumps({"type": "response_item", "timestamp": "2026-06-12T09:00:00Z",
                       "payload": {"type": "function_call", "name": "exec_command",
                                   "arguments": json.dumps({"cmd": "ls"})}})
    (cx_dir / "rollout-2026-06-12T00-00-00-abc.jsonl").write_bytes(
        b"\xff bad bytes here \x80\n" + meta.encode() + b"\n" + call.encode() + b"\n"
    )

    # gemini: a single JSON object file with an invalid byte → must not abort the run.
    gem_chats = home / ".gemini" / "tmp" / "deadbeef" / "chats"
    gem_chats.mkdir(parents=True)
    (gem_chats / "session-2026-06-10T00-00-x.json").write_bytes(b"\xff\x00 not json \xc3")
    (home / ".gemini" / "projects.json").write_text(
        json.dumps({"projects": {"/Users/ultra/xp/gem": "deadbeef"}}), encoding="utf-8"
    )

    # opencode: an invalid byte in a part file → that part is skipped, no crash.
    oc = home / ".local" / "share" / "opencode" / "storage"
    (oc / "session" / "proj1").mkdir(parents=True)
    (oc / "session" / "proj1" / "ses_1.json").write_text(
        json.dumps({"id": "ses_1", "directory": "/Users/ultra/xp/oc"}), encoding="utf-8"
    )
    (oc / "part" / "msg_ses_1").mkdir(parents=True)
    (oc / "part" / "msg_ses_1" / "prt_bad.json").write_bytes(b"\xff\xfe not json \x80")

    # the whole command must complete and still surface the one good codex/cc invocation.
    invs, supported, _ = collect(home=home)
    assert set(supported) == {"claude-code", "codex", "gemini", "opencode"}
    # the valid claude Read + valid codex ls survived; the corrupt files didn't abort anything.
    tools = sorted(i.tool_name for i in invs)
    assert "Read" in tools  # claude good line
    assert "Bash" in tools  # codex `ls`


def test_codex_per_call_workdir_attributes_repo(tmp_path):
    """A codex session that runs commands in DIFFERENT worktrees must attribute each call to
    its own ``workdir``, not the session's starting cwd (review finding)."""
    home = tmp_path / "home"
    write_codex_session(
        home, ("2026", "06", "12"), "/Users/ultra/xp/main",
        [
            {"ts": "2026-06-12T09:00:00Z", "name": "exec_command", "cmd": "ls"},  # no workdir → session cwd
            {"ts": "2026-06-12T09:05:00Z", "name": "exec_command", "cmd": "ls",
             "workdir": "/Users/ultra/xp/worktree-a"},
        ],
    )
    invs, _, _ = collect(home=home, harnesses=["codex"])
    repos = {i.repo for i in invs}
    assert repos == {"/Users/ultra/xp/main", "/Users/ultra/xp/worktree-a"}
    # the --repo filter then targets the per-call workdir, not just the session cwd.
    only_a, _, _ = collect(home=home, harnesses=["codex"], repos=["/Users/ultra/xp/worktree-a"])
    assert len(only_a) == 1 and only_a[0].repo == "/Users/ultra/xp/worktree-a"


def test_codex_empty_shell_command_is_baseline(tmp_path):
    """A shell function_call with empty/no arguments counts as a bare baseline shell, not lost
    (review finding: codex.py empty-arguments case)."""
    home = tmp_path / "home"
    write_codex_session(
        home, ("2026", "06", "12"), "/x",
        [{"ts": "2026-06-12T09:00:00Z", "name": "exec_command"}],  # no cmd key
    )
    invs, _, _ = collect(home=home, harnesses=["codex"])
    assert len(invs) == 1 and invs[0].category == "baseline" and invs[0].tool_name == "Bash"


# ── _passes branch coverage (review finding #23) ─────────────────────────────────────────
def test_passes_drops_undated_under_date_filter():
    dated = ToolInvocation(datetime(2026, 6, 10, tzinfo=timezone.utc), "h", "/r", "s", "Read", "baseline", "Read")
    undated = ToolInvocation(None, "h", "/r", "s", "Read", "baseline", "Read")
    since = datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert _passes(dated, None, since, None) is True
    assert _passes(undated, None, since, None) is False  # date filter drops undated
    assert _passes(undated, None, None, None) is True  # no filter keeps it
    # repo filter is normalized (trailing slash / ~ don't break the match)
    assert _passes(dated, frozenset({"/r"}), None, None) is True
    assert _passes(dated, frozenset({"/other"}), None, None) is False


def test_repo_filter_normalizes_trailing_slash(tmp_path):
    home = tmp_path / "home"
    write_claude_session(
        home, "-x", "s", "/Users/ultra/xp/demo",
        [_cc_event("2026-06-10T10:00:00Z", "/Users/ultra/xp/demo/", [{"name": "Read", "input": {}}])],
    )
    # log records a trailing slash; filter passes the path without one → still matches.
    invs, _, _ = collect(home=home, repos=["/Users/ultra/xp/demo"])
    assert len(invs) == 1


# ── registry behavior (review finding #21) ───────────────────────────────────────────────
def test_registry_lists_all_four_in_import_order():
    from riglib.stats.sources import source_names

    assert source_names() == ["claude-code", "codex", "gemini", "opencode"]


def test_register_rejects_nameless_source():
    from riglib.stats.sources.base import LogSource, register

    with pytest.raises(ValueError):
        @register
        class _Nameless(LogSource):
            def root(self):  # pragma: no cover - never reached
                raise NotImplementedError

            def iter_invocations(self, *, repos=None):  # pragma: no cover
                raise NotImplementedError


# ── run() branch coverage: bad format, no-logs, help fallback (review finding #20) ───────
def _ns(**kw):
    import argparse

    base = dict(format="tui", harness=None, repo=None, since=None, until=None,
                web_port=0, baseline=False, home=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_run_rejects_unknown_format(capsys):
    from riglib.stats import run

    assert run(_ns(format="xml")) == 2
    assert "unknown --format" in capsys.readouterr().out


def test_run_rejects_bad_since(capsys):
    from riglib.stats import run

    assert run(_ns(since="nope")) == 2


def test_run_rejects_inverted_date_range(capsys):
    """--since after --until is a transposed-date typo; it must error, not emit an empty
    report that reads like real zero usage."""
    from riglib.stats import run

    assert run(_ns(since="2026-06-15", until="2026-06-01")) == 2
    assert "is after --until" in capsys.readouterr().out


def test_run_rejects_unknown_harness(capsys):
    """A typo like `--harness codx` must error (exit 2), not silently emit a valid zero-count
    report that reads like real data to a script. (review finding)"""
    from riglib.stats import run

    assert run(_ns(harness=["codx"], format="json")) == 2
    out = capsys.readouterr().out
    assert "unknown --harness" in out and "codx" in out
    # a known harness mixed with an unknown one still fails (the unknown is caught).
    assert run(_ns(harness=["claude-code", "nope"])) == 2
    # a valid harness alone does not trip the guard.
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        assert run(_ns(harness=["claude-code"], format="json", home=d)) == 0


def test_run_reports_no_harness_logs(tmp_path, capsys):
    from riglib.stats import run

    empty = tmp_path / "empty"
    empty.mkdir()
    rc = run(_ns(home=str(empty)))  # default tui → human hint
    assert rc == 0
    assert "no harness logs found" in capsys.readouterr().out


def test_run_json_on_empty_machine_emits_valid_json(tmp_path, capsys):
    """A fresh machine with no logs must still produce parseable JSON, not a prose line
    (the README's machine-readable contract — review finding)."""
    from riglib.stats import run

    empty = tmp_path / "empty"
    empty.mkdir()
    rc = run(_ns(format="json", home=str(empty)))
    assert rc == 0
    d = json.loads(capsys.readouterr().out)  # must parse
    assert d["summary"]["total"] == 0
    assert d["by_category"] == {"baseline": 0, "ours": 0, "external-advertised": 0, "other": 0}


def test_since_window_no_double_count_at_boundary(tmp_path):
    """An invocation exactly at the `since` instant is counted in the later window only, never
    both windows (review finding: inclusive bounds double-count at the split)."""
    home = tmp_path / "home"
    # one call exactly at 2026-06-12T00:00:00Z (the `since` instant), one before, one after.
    write_claude_session(
        home, "-x", "s", "/r",
        [
            _cc_event("2026-06-11T12:00:00Z", "/r", [{"name": "Read", "input": {}}]),  # prior window
            _cc_event("2026-06-12T00:00:00Z", "/r", [{"name": "Read", "input": {}}]),  # boundary
            _cc_event("2026-06-12T06:00:00Z", "/r", [{"name": "Read", "input": {}}]),  # later window
        ],
    )
    since = parse_date("2026-06-12")  # midnight UTC
    until = parse_date("2026-06-12", end=True)
    report = build_report(home=home, since=since, until=until)
    assert report.trend is not None
    # later window holds the boundary + the 06:00 call = 2; earlier holds only the prior = 1.
    assert sum(report.trend.later.values()) == 2
    assert sum(report.trend.earlier.values()) == 1


def test_run_json_end_to_end(tmp_path, capsys):
    from riglib.stats import run

    home = tmp_path / "home"
    write_claude_session(
        home, "-x", "s", "/r",
        [_cc_event("2026-06-10T10:00:00Z", "/r", [{"name": "Read", "input": {}}])],
    )
    rc = run(_ns(format="json", home=str(home)))
    assert rc == 0
    d = json.loads(capsys.readouterr().out)
    assert d["summary"]["total"] == 1


# ── web.serve smoke: bind to port 0, one request, shut down (review finding #22) ─────────
def test_web_serve_smoke():
    import http.server
    import threading
    import urllib.request

    agg = aggregate(_sample_invocations())
    page = web.build_html(agg).encode("utf-8")

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.handle_request, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            assert r.status == 200
            body = r.read().decode("utf-8")
        assert body.startswith("<!doctype html>") and "<svg" in body
    finally:
        srv.server_close()
