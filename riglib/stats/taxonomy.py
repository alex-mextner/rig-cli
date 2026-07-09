"""The adoption taxonomy: which bucket does a tool invocation belong to?

This is the one place that encodes "what is OURS vs the built-in baseline vs the
third-party stuff we merely advertise". It is intentionally DATA-DRIVEN — the sets below
are the knobs the CTO edits as the ecosystem grows; no parser or renderer hard-codes a
tool name. Reached from :func:`riglib.stats.sources.base.LogSource` parsers, which feed
each raw tool name + (for shells) the command string through :func:`categorize`.

Why a shell command needs inspecting: every harness exposes ONE shell tool (Bash /
exec_command / run_shell_command / bash). A bare count of that tool tells us nothing about
adoption — the signal is *what the agent ran inside it*. So a Bash call whose command
invokes ``review`` / ``tg`` / ``rig`` / ``draw`` / ``3d`` / ``task`` / ``dev`` is re-labelled as an
"ours" CLI invocation and pulled OUT of the baseline shell count. That re-labelling is the
core measurement this whole command exists to produce.
"""

from __future__ import annotations

import os
import shlex

# ── the built-in harness tools (the baseline we measure adoption against) ──────────────
# Names are normalized case-insensitively; we list the canonical-cased label. Every harness
# maps its native tool onto one of these via the parser (e.g. codex exec_command → "Bash").
BASELINE_TOOLS: frozenset[str] = frozenset(
    {
        "Bash",
        "Read",
        "Write",
        "Edit",
        "MultiEdit",
        "Grep",
        "Glob",
        "NotebookEdit",
        "Task",
        "Agent",  # CC's subagent launcher (the SDK renamed Task→Agent; count both)
        "WebFetch",
        "WebSearch",
        "TodoWrite",
        "LS",
    }
)

# ── cross-harness baseline aliases ─────────────────────────────────────────────────────
# Non-CC harnesses name the SAME built-in operations differently (gemini ``read_file``,
# codex ``apply_patch``). Map those native names onto the canonical baseline label so the
# adoption ratio (ours / ours+baseline) is comparable ACROSS harnesses — otherwise a
# gemini ``read_file`` would fall to "other" and silently inflate gemini's ratio. The shell
# tools are handled separately (normalized to "bash" by the parsers).
BASELINE_ALIASES: dict[str, str] = {
    # gemini native file/search tools
    "read_file": "Read",
    "read_many_files": "Read",
    "write_file": "Write",
    "replace": "Edit",
    "glob": "Glob",
    "search_file_content": "Grep",
    "google_web_search": "WebSearch",
    "web_fetch": "WebFetch",
    "save_memory": "TodoWrite",
    # codex native tools
    "apply_patch": "Edit",
    "update_plan": "TodoWrite",
    "view_image": "Read",
    # opencode native tools (lowercase built-ins, confirmed on-disk)
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "grep": "Grep",
    "list": "LS",
    "todowrite": "TodoWrite",
    "todoread": "TodoWrite",
    "webfetch": "WebFetch",
}

# ── OUR ecosystem CLIs (detected INSIDE a shell command string) ────────────────────────
# A shell command counts as "ours" when one of these is the program of any pipeline stage.
# KNOWN AMBIGUITY: ``task`` is also the go-task / Taskfile runner. On a machine that uses
# go-task (and not our task-cli) a ``task build`` would be miscounted as "ours". We accept
# this as a deliberate tradeoff — distinguishing the two reliably needs a path/marker probe
# that the log doesn't carry — and it's a knob the CTO can drop here if it ever skews a real
# number. ``dev`` is also generic (devbox/devcontainer-style commands can exist), but it is the
# permissioned agent-tools development surface; count it here so adoption stats follow the same
# default command surface rig provisions. The remaining five names are unambiguous to this
# ecosystem.
OUR_CLIS: frozenset[str] = frozenset({"rig", "review", "tg", "draw", "3d", "task", "dev"})

# ── OUR MCP servers (the `mcp__<server>__<tool>` prefix) ───────────────────────────────
OUR_MCP_SERVERS: frozenset[str] = frozenset({"review"})

# ── OUR skills (Skill-tool `skill:` arg). Names match ~/.agents/skills/ entries we ship. ─
# Kept as a set the CTO extends; everything else invoked through the Skill tool is treated
# as external-advertised (superpowers:*, agent-browser, h-*, debate-swarm, …).
OUR_SKILLS: frozenset[str] = frozenset(
    {
        "rig",
        "review",
        "tg",
        "draw",
        "ai-review-before-commit",
        "atomic-commits",
        "ci-gate-suite",
        "comment-hygiene",
        "dead-code-investigation",
        "deferred-findings-tracking",
        "dependency-version-ranges",
        "file-header-comments",
        "gan-critic-loop",
        "git-merge-syntax-aware",
        "git-workflow",
        "global-git-hooks",
        "help-docs-sync",
        "idempotent-bootstrap",
        "lazy-heavy-imports",
        "naming",
        "no-npx-direct-binary",
        "no-type-escape-hatches",
        "parallelize-independent",
        "pre-commit-gate",
        "promise-durable-action",
        "push-regularly",
        "secret-scanning",
        "self-registering-commands",
        "semantic-code-search",
        "shared-util-single-source",
        "shell-exit-codes",
        "shell-timeouts",
        "single-file-live-symlink-cli",
        "smallest-change",
        "specs-are-authoritative",
        "squash-merge-cleanup",
        "structured-exit-codes",
        "subagent-handoff-contract",
        "systematic-debugging",
        "task-completion-selfcheck",
        "tdd-red-first",
        "test-discipline",
        "unused-params",
        "worktree-base-trap",
        "worktree-isolation",
        "yagni-kiss-dry",
    }
)

# ── third-party MCP servers we advertise / install (NOT ours, but on-ramp tooling) ──────
EXTERNAL_MCP_SERVERS: frozenset[str] = frozenset(
    {
        "serena",
        "sverklo",
        "context7",
        "claude-in-chrome",
        "computer-use",
        "computer",
        "playwright",
        "fetch",
        "haft",
        "linear",
        "pencil",
    }
)

# ── third-party skills we advertise. Prefix match handles "superpowers:brainstorming". ──
EXTERNAL_SKILL_PREFIXES: tuple[str, ...] = ("superpowers", "h-", "debate-swarm", "agent-browser")
EXTERNAL_SKILLS: frozenset[str] = frozenset(
    {"agent-browser", "superpowers", "h-reason", "debate-swarm", "deep-research", "frontend-design"}
)

# the shell tool names each harness uses — so categorize() knows when to inspect a command.
# THE single source of truth: ``sources/_shellutil.py`` imports this so the parsers and the
# categorizer can never disagree on what counts as a shell tool (the prior drift between two
# separate sets was a latent footgun for any future parser passing a raw name through).
SHELL_TOOLS: frozenset[str] = frozenset(
    {"bash", "shell", "exec_command", "local_shell", "run_shell_command", "run_terminal_cmd"}
)

_BASELINE_LOWER = {t.lower(): t for t in BASELINE_TOOLS}


# shell operators that, AS A STANDALONE token, begin a fresh pipeline stage. We detect these
# only AFTER quote-aware tokenization (see _command_programs), so an operator that lives
# inside a quoted string — ``git commit -m "fix; review later"`` — is part of one token and
# never starts a stage, eliminating the false "ours" hit that a raw regex split produced.
_STAGE_OPERATORS = frozenset({"&&", "||", "|", ";", "&", "(", "{", "|&"})

# "transparent" wrappers: programs that RUN another program, so the wrapped command is the
# one that matters for adoption. ``timeout 60 review -C /x`` must count as ``review``, not
# ``timeout`` — otherwise our own ``shell-timeouts`` recommendation would hide every wrapped
# CLI from the very metric that measures adoption. We skip the wrapper, its option flags, and
# (for the duration/-style wrappers) one bare argument, then read the next word as the real
# program. Conservative set — only well-known pass-through launchers.
_TRANSPARENT_WRAPPERS = frozenset(
    {"env", "sudo", "nohup", "time", "timeout", "xargs", "command", "stdbuf", "nice",
     "ionice", "doas", "setsid", "exec"}
)
# wrappers whose FIRST non-flag bare argument is a value, not the wrapped program
# (``timeout 60 cmd``, ``nice -n 5 cmd`` already handled via flags, ``timeout 60`` is bare).
_WRAPPERS_WITH_LEADING_VALUE = frozenset({"timeout"})

# interpreter runners: ``python3 bin/rig apply`` / ``uv run bin/rig`` / ``python -m riglib.cli``.
# The README itself documents running our CLIs this way (``python3 bin/rig`` / ``uv run
# bin/rig``), so NOT unwrapping them undercounts our own most-documented invocation form.
# We derive the CLI name from the script path's basename or the ``-m`` module's tail.
_INTERPRETERS = frozenset({"python", "python2", "python3", "uv", "uvx", "pipx", "poetry", "pdm", "hatch"})


def _interp_program(tokens: list[str], i: int, n: int) -> str | None:
    """Given an interpreter at ``tokens[i]``, find the CLI name it runs. Handles
    ``uv run [flags] <script>``, ``python[3] -m <module>``, ``python[3] <script.py>``,
    and ``pipx run <cli>``. Returns the CLI basename (without ``.py``) or None."""
    j = i + 1
    # uv/uvx/pipx/poetry/pdm/hatch use a ``run`` subcommand before the target.
    if tokens[i].rsplit("/", 1)[-1] in {"uv", "pipx", "poetry", "pdm", "hatch"}:
        while j < n and tokens[j].startswith("-"):
            j += 1
        if j < n and tokens[j] == "run":
            j += 1
    # skip interpreter / runner flags (``python -X foo``, ``uv run --no-sync``); ``-m`` is special.
    while j < n and tokens[j].startswith("-") and tokens[j] != "-m":
        j += 1
    if j < n and tokens[j] == "-m" and j + 1 < n:  # ``python -m riglib.cli`` → ``cli``? no — module
        module = tokens[j + 1]
        return module.rsplit(".", 1)[-1] or None
    if j < n and tokens[j] not in _STAGE_OPERATORS and not tokens[j].startswith("-"):
        name = tokens[j].rsplit("/", 1)[-1]
        if name.endswith(".py"):
            name = name[:-3]
        return name or None
    return None

# short option flags (per wrapper) that consume the FOLLOWING token as their value, so the
# value isn't mistaken for the wrapped program (``sudo -u alex review`` → skip ``-u alex``).
_WRAPPER_VALUE_FLAGS = {
    "sudo": frozenset({"-u", "-g", "-U", "-C", "-h", "-p", "-r", "-t"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p"}),
    "doas": frozenset({"-u", "-C"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
}


def _is_env_assignment(tok: str) -> bool:
    """``VAR=val`` leading assignment (not a path/flag like ``./x`` or ``-x=y``)."""
    return "=" in tok and not tok.startswith(("/", ".", "-")) and tok.split("=", 1)[0].isidentifier()


def _tokenize(command: str) -> list[str]:
    """Quote-aware split of the whole command. shlex respects quotes so operators inside a
    string literal stay inside their token. ``punctuation_chars`` makes shlex emit ``&&`` /
    ``|`` / ``;`` as their OWN tokens (so we can recognize stage boundaries) instead of
    gluing them onto a word."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        # unbalanced quote → best-effort fallback that still keeps quoted operators glued.
        return command.split()


def _command_programs(command: str) -> list[str]:
    """Every pipeline-stage program in the command (basename), for OUR-CLI detection.

    A program is the first command word of each stage: the very first token, or the first
    real word after a stage-operator token, skipping leading ``VAR=val`` assignments,
    subshell-group punctuation, AND transparent wrappers (``timeout`` / ``env`` / ``sudo`` /
    …) so the WRAPPED program is the one counted. Quote-aware, so operators inside quotes
    don't start stages.
    """
    progs: list[str] = []
    tokens = _tokenize(command)
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in _STAGE_OPERATORS:
            i += 1
            continue
        if _is_env_assignment(tok):  # leading VAR=val — program is still ahead
            i += 1
            continue
        # found a stage program (possibly a wrapper): resolve through wrappers to the real one.
        prog, i = _resolve_stage_program(tokens, i)
        if prog:
            progs.append(os.path.basename(prog))
        # advance to the next stage operator (rest of this stage is arguments).
        while i < n and tokens[i] not in _STAGE_OPERATORS:
            i += 1
    return progs


def _resolve_stage_program(tokens: list[str], i: int) -> tuple[str | None, int]:
    """From a stage-start index, return ``(program, next_index)`` resolving through any
    transparent wrappers. ``next_index`` points at the token after the resolved program."""
    n = len(tokens)
    guard = 0  # bound the wrapper chain so a pathological input can't loop
    while i < n and guard < 8:
        guard += 1
        tok = tokens[i]
        if tok in _STAGE_OPERATORS:
            return None, i
        base = tok.rsplit("/", 1)[-1]
        if base in _INTERPRETERS:
            # ``python3 bin/rig`` / ``uv run bin/rig`` → the CLI the interpreter runs.
            interp = _interp_program(tokens, i, n)
            if interp is not None:
                # return the script name; advance i past this whole stage's program token.
                return interp, i + 1
            return tok, i + 1
        if base not in _TRANSPARENT_WRAPPERS:
            return tok, i + 1  # the real program
        # ``command -v/-V <name>`` only PROBES PATH for <name>, it does not run it — counting
        # the probed name as a real invocation would falsely inflate adoption (same class as
        # the quoted-operator HIGH finding). A bare ``command <prog>`` DOES exec, so only the
        # probe flags short-circuit. (review finding)
        if base == "command" and i + 1 < n and tokens[i + 1] in ("-v", "-V"):
            return None, i
        # skip the wrapper, then its flags / flag-values / env-assignments, and (for value-
        # wrappers) one bare leading value, until the wrapped program.
        value_flags = _WRAPPER_VALUE_FLAGS.get(base, frozenset())
        i += 1
        while i < n and tokens[i] not in _STAGE_OPERATORS:
            t = tokens[i]
            if t.startswith("-"):
                i += 1
                # a value-taking flag consumes the next token (``-u alex``) unless it's
                # the ``--flag=value`` form (already self-contained).
                if t in value_flags and "=" not in t and i < n and tokens[i] not in _STAGE_OPERATORS:
                    i += 1
                continue
            if _is_env_assignment(t):  # ``env FOO=1 cmd``
                i += 1
                continue
            break
        # a value-wrapper (``timeout 60 cmd``) has one bare duration before the program.
        if base in _WRAPPERS_WITH_LEADING_VALUE and i < n and tokens[i] not in _STAGE_OPERATORS:
            if not tokens[i].startswith("-") and not _is_env_assignment(tokens[i]):
                i += 1
    return None, i


def detect_our_cli(command: str) -> str | None:
    """Return the OUR-CLI name if any pipeline stage runs one, else None."""
    for prog in _command_programs(command):
        if prog in OUR_CLIS:
            return prog
    return None


def _mcp_server(tool_name: str) -> str | None:
    """``mcp__serena__find_symbol`` → ``serena``; non-MCP names → None."""
    if not tool_name.startswith("mcp__"):
        return None
    rest = tool_name[len("mcp__") :]
    return rest.split("__", 1)[0] if rest else None


def _classify_skill(skill: str) -> str:
    if skill in OUR_SKILLS:
        return "ours"
    base = skill.split(":", 1)[0]
    if skill in EXTERNAL_SKILLS or base in EXTERNAL_SKILLS:
        return "external-advertised"
    if any(skill.startswith(p) or base.startswith(p) for p in EXTERNAL_SKILL_PREFIXES):
        return "external-advertised"
    # an unknown skill is harness-built infrastructure adoption we can't attribute → other
    return "other"


def categorize(raw_tool: str, *, command: str | None = None, skill: str | None = None) -> tuple[str, str]:
    """Map a raw tool invocation to ``(category, display_label)``.

    ``command`` is the shell command string when ``raw_tool`` is a shell tool; ``skill`` is
    the Skill-tool argument when ``raw_tool`` is the Skill tool. Both optional.
    """
    low = raw_tool.lower()

    # 1) MCP — prefix decides ours/external regardless of the specific tool.
    server = _mcp_server(raw_tool)
    if server is not None:
        if server in OUR_MCP_SERVERS:
            return "ours", raw_tool
        if server in EXTERNAL_MCP_SERVERS:
            return "external-advertised", raw_tool
        return "other", raw_tool

    # 2) Skill tool — the skill arg decides.
    if low == "skill" and skill:
        return _classify_skill(skill), f"skill:{skill}"

    # 3) Shell tool — inspect the command for an OUR-CLI, else it's baseline shell.
    if low in SHELL_TOOLS:
        if command:
            cli = detect_our_cli(command)
            if cli:
                return "ours", f"{cli} (cli)"
        return "baseline", "Bash"

    # 4) A non-CC harness's native built-in (gemini read_file, opencode edit, codex
    #    apply_patch, …) → fold onto the canonical baseline label so the ratio compares
    #    across harnesses. All alias keys are lowercase, so the case-folded lookup suffices.
    if low in BASELINE_ALIASES:
        return "baseline", BASELINE_ALIASES[low]

    # 5) A plain baseline tool (case-insensitive match to the canonical label).
    if low in _BASELINE_LOWER:
        return "baseline", _BASELINE_LOWER[low]

    # 6) Anything else — unattributed.
    return "other", raw_tool
