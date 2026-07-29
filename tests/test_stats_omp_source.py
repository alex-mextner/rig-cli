"""Tests for the omp (Oh My Pi harness) stats log-source parser.

Mirrors the per-harness parser tests in tests/test_stats.py: HOME-isolated synthetic
session trees (no real logs), defensive-parsing guarantees, the ``repos`` pre-filter,
and the absent-root case. The on-disk shape under test (verified live, 2026-07):

  ``~/.omp/agent/sessions/<encoded-cwd>/<timestamp>_<session-uuid>.jsonl``

with a ``{"type":"session","cwd":...}`` event carrying the real cwd and assistant
``{"type":"message","message":{"role":"assistant","content":[{"type":"toolCall",...}]}}``
events carrying the tool calls (lowercase tool names, dict-shaped ``arguments``).
"""

from __future__ import annotations

import json
from pathlib import Path

from riglib.stats.command import collect
from riglib.stats.sources.omp import OmpSource


def _omp_session_event(cwd: str, session_id: str = "s1") -> str:
    """The omp `session` JSONL line that carries the real cwd and session id."""
    return json.dumps(
        {"type": "session", "version": 3, "id": session_id,
         "timestamp": "2026-07-29T10:00:00.000Z", "cwd": cwd}
    )


def _omp_tool_event(ts: str, tools: list[dict]) -> str:
    """An omp assistant `message` JSONL line carrying `toolCall` content blocks."""
    return json.dumps(
        {
            "type": "message",
            "id": "m1",
            "timestamp": ts,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "id": f"tool_{i}", "name": t["name"],
                     "arguments": t.get("arguments", {})}
                    for i, t in enumerate(tools)
                ],
            },
        }
    )


def write_omp_session(home: Path, encoded: str, session: str, events: list[str]) -> Path:
    d = home / ".omp" / "agent" / "sessions" / encoded
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{session}.jsonl"
    f.write_text("\n".join(events) + "\n", encoding="utf-8")
    return f


def test_omp_parser_counts(tmp_path):
    home = tmp_path / "home"
    # the encoded dir decodes to a DIFFERENT path than the session event's cwd — the
    # event's cwd must win (the encode is lossy, exactly like claude's).
    write_omp_session(
        home, "-tmp-lossy-decode", "2026-07-29T10-00-00-000Z_s1",
        [
            _omp_session_event("/Users/ultra/xp/demo"),
            _omp_tool_event("2026-07-29T10:01:00Z", [
                {"name": "read", "arguments": {"path": "/x"}},
                {"name": "bash", "arguments": {"command": "ls"}},
                {"name": "bash", "arguments": {"command": "tg done"}},
                {"name": "todo", "arguments": {"op": "add", "task": "x"}},
                {"name": "web_search", "arguments": {"query": "q"}},
            ]),
        ],
    )
    invs, supported, _ = collect(home=home, harnesses=["omp"])
    assert supported == ["omp"]
    assert len(invs) == 5
    assert all(i.harness == "omp" for i in invs)
    assert all(i.repo == "/Users/ultra/xp/demo" for i in invs)
    by_key = {(i.raw_tool, i.detail): i for i in invs}
    read_inv, ls_inv, tg_inv = by_key[("read", "")], by_key[("bash", "ls")], by_key[("bash", "tg done")]
    # lowercase native names fold onto the canonical baseline labels
    assert read_inv.category == "baseline" and read_inv.tool_name == "Read"
    assert ls_inv.category == "baseline" and ls_inv.tool_name == "Bash"
    # omp's native todo tool folds onto the canonical baseline TodoWrite (alias, not case-fold)
    assert by_key[("todo", "")].category == "baseline"
    assert by_key[("todo", "")].tool_name == "TodoWrite"
    assert by_key[("web_search", "")].category == "baseline"
    assert by_key[("web_search", "")].tool_name == "WebSearch"
    # a bash command running one of OUR CLIs is re-labelled "ours"
    assert tg_inv.category == "ours" and tg_inv.tool_name == "tg (cli)"
    assert ls_inv.timestamp is not None and ls_inv.timestamp.year == 2026


def test_omp_parser_falls_back_to_decoded_dir_without_session_event(tmp_path):
    """A session file with no `session` event maps to the best-effort dir-name decode.
    omp flattens the HOME-RELATIVE cwd (verified live: `-work-hyperide` held a session
    whose cwd was `$HOME/work/hyperide`), so the decode resolves under the agent home."""
    home = tmp_path / "home"
    write_omp_session(
        home, "-work-hyperide", "s2",
        [_omp_tool_event("2026-07-29T10:01:00Z", [{"name": "read", "arguments": {}}])],
    )
    invs, _, _ = collect(home=home, harnesses=["omp"])
    assert len(invs) == 1
    assert invs[0].repo == str(home / "work" / "hyperide")


def test_omp_parser_ignores_non_assistant_and_non_toolcall(tmp_path):
    """Only assistant `message` events' `toolCall` blocks count — user/toolResult
    messages and thinking/text blocks must not be counted."""
    home = tmp_path / "home"
    write_omp_session(
        home, "-x", "s3",
        [
            _omp_session_event("/r"),
            json.dumps({"type": "message", "timestamp": "2026-07-29T10:01:00Z", "message": {
                "role": "user", "content": [
                    {"type": "toolCall", "name": "bash", "arguments": {"command": "ls"}}]}}),
            json.dumps({"type": "message", "timestamp": "2026-07-29T10:01:01Z", "message": {
                "role": "assistant", "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "working on it"}]}}),
            json.dumps({"type": "model_change", "timestamp": "2026-07-29T10:01:02Z",
                        "model": "kimi-code/k3"}),
            _omp_tool_event("2026-07-29T10:01:03Z", [{"name": "read", "arguments": {}}]),
        ],
    )
    invs, _, _ = collect(home=home, harnesses=["omp"])
    assert len(invs) == 1 and invs[0].raw_tool == "read"


def test_omp_parser_survives_malformed_lines_and_unreadable_files(tmp_path):
    """One bad line or one unreadable session file must never abort the harness."""
    home = tmp_path / "home"
    good = _omp_tool_event("2026-07-29T10:01:00Z", [{"name": "read", "arguments": {}}])
    write_omp_session(
        home, "-x", "s4",
        [_omp_session_event("/r"), "", "not json {", "[1, 2]", "42", good],
    )
    # a DIRECTORY named *.jsonl: open() raises IsADirectoryError (an OSError) mid-iteration.
    bad = home / ".omp" / "agent" / "sessions" / "-x" / "baddir.jsonl"
    bad.mkdir()
    invs, supported, _ = collect(home=home, harnesses=["omp"])
    assert supported == ["omp"]
    assert len(invs) == 1 and invs[0].tool_name == "Read"  # the good line survived


def test_omp_parser_honors_repos_prefilter(tmp_path):
    home = tmp_path / "home"
    for encoded, cwd in (("-a", "/repo/a"), ("-b", "/repo/b")):
        write_omp_session(
            home, encoded, "s",
            [_omp_session_event(cwd),
             _omp_tool_event("2026-07-29T10:01:00Z", [{"name": "read", "arguments": {}}])],
        )
    # the authoritative filter (collect) …
    invs, _, _ = collect(home=home, harnesses=["omp"], repos=["/repo/a"])
    assert len(invs) == 1 and invs[0].repo == "/repo/a"
    # … and the source's own cheap pre-filter (the LogSource contract) agree.
    direct = list(OmpSource(home=home).iter_invocations(repos=frozenset({"/repo/b"})))
    assert len(direct) == 1 and direct[0].repo == "/repo/b"


def test_omp_per_call_cwd_attributes_repo(tmp_path):
    """An omp session that runs commands in DIFFERENT repos must attribute each call to
    its own ``arguments.cwd``, not the session's starting cwd (same rule as codex's
    per-call workdir)."""
    home = tmp_path / "home"
    write_omp_session(
        home, "-Users-ultra-xp-main", "s5",
        [
            _omp_session_event("/Users/ultra/xp/main"),
            _omp_tool_event("2026-07-29T10:01:00Z", [
                {"name": "bash", "arguments": {"command": "ls"}},  # no cwd → session cwd
                {"name": "bash", "arguments": {"command": "ls", "cwd": "/Users/ultra/xp/worktree-a"}},
                # a RELATIVE cwd is not a repo path — ignored, session cwd wins
                {"name": "bash", "arguments": {"command": "ls", "cwd": "worktree-b"}},
            ]),
        ],
    )
    invs, _, _ = collect(home=home, harnesses=["omp"])
    assert [i.repo for i in invs].count("/Users/ultra/xp/main") == 2
    assert {i.repo for i in invs} == {"/Users/ultra/xp/main", "/Users/ultra/xp/worktree-a"}
    # the source pre-filter then targets the per-call cwd, not just the session cwd.
    only_a = list(OmpSource(home=home).iter_invocations(repos=frozenset({"/Users/ultra/xp/worktree-a"})))
    assert len(only_a) == 1 and only_a[0].repo == "/Users/ultra/xp/worktree-a"


def test_omp_parser_walks_nested_subagent_transcripts(tmp_path):
    """Per-subagent transcripts live at <session-stem>/<agent>.jsonl — DISJOINT sessions
    that must be discovered recursively, with session ids that can't collide."""
    home = tmp_path / "home"
    main = write_omp_session(
        home, "-x", "2026-07-29T10-00-00-000Z_main",
        [_omp_session_event("/r"),
         _omp_tool_event("2026-07-29T10:01:00Z", [{"name": "read", "arguments": {}}])],
    )
    # two subagent transcripts under the session dir; one shares a bare stem with a
    # subagent in ANOTHER session (the collision the session-id fallback must survive).
    for session_dir, agent, sid in (
        (main.stem, "SubA", "sub-1"),
        ("2026-07-29T11-00-00-000Z_other", "SubA", "sub-2"),
    ):
        d = home / ".omp" / "agent" / "sessions" / "-x" / session_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{agent}.jsonl").write_text(
            "\n".join([
                _omp_session_event("/r", session_id=sid),
                _omp_tool_event("2026-07-29T10:02:00Z", [{"name": "bash", "arguments": {"command": "ls"}}]),
            ]) + "\n",
            encoding="utf-8",
        )
    invs, _, _ = collect(home=home, harnesses=["omp"])
    assert len(invs) == 3  # main + both nested subagents
    assert {i.session for i in invs} == {"s1", "sub-1", "sub-2"}  # the session event id wins


def test_omp_parser_skips_nameless_tool_calls(tmp_path):
    """A toolCall block with a missing/empty ``name`` is malformed — skipped, not counted
    as a phantom empty-label invocation."""
    home = tmp_path / "home"
    write_omp_session(
        home, "-x", "s6",
        [
            _omp_session_event("/r"),
            _omp_tool_event("2026-07-29T10:01:00Z", [
                {"name": "", "arguments": {"command": "ls"}},
                {"name": "read", "arguments": {}},
            ]),
        ],
    )
    invs, _, _ = collect(home=home, harnesses=["omp"])
    assert len(invs) == 1 and invs[0].raw_tool == "read"


def test_omp_skill_reads_count_as_skill_invocations(tmp_path):
    """omp loads skills via ``read`` on ``skill://<name>`` URLs — those must hit the SAME
    skill taxonomy as CC's Skill tool, not the baseline Read bucket."""
    home = tmp_path / "home"
    write_omp_session(
        home, "-x", "s9",
        [
            _omp_session_event("/r"),
            _omp_tool_event("2026-07-29T10:01:00Z", [
                {"name": "read", "arguments": {"path": "skill://shell-timeouts"}},
                {"name": "read", "arguments": {"path": "skill://superpowers:brainstorming"}},
                {"name": "read", "arguments": {"path": "skill://some-unknown-skill/SKILL.md"}},
                {"name": "read", "arguments": {"path": "/regular/file.py"}},
            ]),
        ],
    )
    invs, _, _ = collect(home=home, harnesses=["omp"])
    by_label = {i.tool_name: i for i in invs}
    assert by_label["skill:shell-timeouts"].category == "ours"
    assert by_label["skill:superpowers:brainstorming"].category == "external-advertised"
    assert by_label["skill:some-unknown-skill"].category == "other"
    # a plain file read stays a baseline Read
    assert by_label["Read"].category == "baseline"
    assert by_label["skill:shell-timeouts"].raw_tool == "read"


def test_omp_nested_fallback_session_ids_never_collide(tmp_path):
    """Two same-named subagent transcripts in DIFFERENT session dirs, both WITHOUT a
    `session` event, must still get distinct fallback session ids (root-relative path)."""
    home = tmp_path / "home"
    for session_dir in ("2026-07-29T10-00-00-000Z_a", "2026-07-29T11-00-00-000Z_b"):
        d = home / ".omp" / "agent" / "sessions" / "-x" / session_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / "SubA.jsonl").write_text(
            _omp_tool_event("2026-07-29T10:02:00Z", [{"name": "read", "arguments": {}}]) + "\n",
            encoding="utf-8",
        )
    invs, _, _ = collect(home=home, harnesses=["omp"])
    assert len(invs) == 2
    assert len({i.session for i in invs}) == 2, [i.session for i in invs]


def test_omp_parser_skips_unreadable_project_dirs(tmp_path):
    """A project dir the walk can't read must be skipped, not abort the whole source."""
    home = tmp_path / "home"
    write_omp_session(
        home, "-good", "s",
        [_omp_session_event("/r"),
         _omp_tool_event("2026-07-29T10:01:00Z", [{"name": "read", "arguments": {}}])],
    )
    bad = home / ".omp" / "agent" / "sessions" / "-bad"
    bad.mkdir(parents=True)
    (bad / "x.jsonl").write_text("{}", encoding="utf-8")
    bad.chmod(0o000)
    try:
        invs, supported, _ = collect(home=home, harnesses=["omp"])
    finally:
        bad.chmod(0o755)  # let tmp_path cleanup work
    assert supported == ["omp"]
    assert len(invs) == 1 and invs[0].repo == "/r"


def test_omp_absent_root_is_unavailable_and_yields_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    source = OmpSource(home=home)
    assert not source.available()
    assert list(source.iter_invocations()) == []


def test_omp_prefilter_matches_trailing_slash_normalized_repos(tmp_path):
    """The source pre-filter must not drop a call the authoritative (normalizing) filter
    would keep — a logged cwd with a trailing slash still matches the filter set."""
    home = tmp_path / "home"
    write_omp_session(
        home, "-x", "s7",
        [_omp_session_event("/r/"),
         _omp_tool_event("2026-07-29T10:01:00Z", [{"name": "read", "arguments": {}}])],
    )
    direct = list(OmpSource(home=home).iter_invocations(repos=frozenset({"/r"})))
    assert len(direct) == 1


def test_omp_parser_honors_pi_coding_agent_dir_for_real_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PI_CONFIG_DIR", raising=False)  # hermetic: agent-dir var must win alone
    agent_dir = tmp_path / "omp-agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    d = agent_dir / "sessions" / "-x"
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(
        "\n".join([_omp_session_event("/r"),
                   _omp_tool_event("2026-07-29T10:01:00Z", [{"name": "read", "arguments": {}}])]) + "\n",
        encoding="utf-8",
    )

    invs, supported, _ = collect(harnesses=["omp"])  # no home= → the default $HOME branch

    assert supported == ["omp"]
    assert len(invs) == 1


def test_omp_parser_honors_pi_config_dir_for_real_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)  # hermetic: config-dir var alone
    monkeypatch.setenv("PI_CONFIG_DIR", ".omp-custom")
    d = tmp_path / "home" / ".omp-custom" / "agent" / "sessions" / "-x"
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(
        "\n".join([_omp_session_event("/r"),
                   _omp_tool_event("2026-07-29T10:01:00Z", [{"name": "read", "arguments": {}}])]) + "\n",
        encoding="utf-8",
    )

    invs, supported, _ = collect(harnesses=["omp"])

    assert supported == ["omp"]
    assert len(invs) == 1


def test_omp_agent_dir_override_wins_over_config_dir(tmp_path, monkeypatch):
    """PI_CODING_AGENT_DIR (full override) outranks PI_CONFIG_DIR (root rename)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    agent_dir = tmp_path / "omp-agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    monkeypatch.setenv("PI_CONFIG_DIR", ".omp-ignored")
    d = agent_dir / "sessions" / "-x"
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(
        "\n".join([_omp_session_event("/r"),
                   _omp_tool_event("2026-07-29T10:01:00Z", [{"name": "read", "arguments": {}}])]) + "\n",
        encoding="utf-8",
    )

    invs, supported, _ = collect(harnesses=["omp"])

    assert supported == ["omp"]
    assert len(invs) == 1


def test_omp_parser_expands_pi_coding_agent_dir_tilde(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CONFIG_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "~/omp-agent")
    d = home / "omp-agent" / "sessions" / "-x"
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(
        "\n".join([_omp_session_event("/r"),
                   _omp_tool_event("2026-07-29T10:01:00Z", [{"name": "read", "arguments": {}}])]) + "\n",
        encoding="utf-8",
    )

    invs, supported, _ = collect(harnesses=["omp"])

    assert supported == ["omp"]
    assert len(invs) == 1


def test_omp_parser_explicit_home_ignores_pi_coding_agent_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    agent_dir = tmp_path / "omp-agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    write_omp_session(
        home, "-x", "s8",
        [_omp_session_event("/sandbox"),
         _omp_tool_event("2026-07-29T10:01:00Z", [{"name": "read", "arguments": {}}])],
    )
    # the env-override tree also has a session — an explicit home= must be a sandbox boundary.
    d = agent_dir / "sessions" / "-x"
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(
        "\n".join([_omp_session_event("/host"),
                   _omp_tool_event("2026-07-29T10:01:00Z", [{"name": "bash", "arguments": {"command": "ls"}}])]) + "\n",
        encoding="utf-8",
    )

    invs, supported, _ = collect(home=home, harnesses=["omp"])

    assert supported == ["omp"]
    assert [i.repo for i in invs] == ["/sandbox"]
