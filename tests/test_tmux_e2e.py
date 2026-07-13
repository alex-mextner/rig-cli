"""REAL tmux e2e — the acceptance test for the rig-tmux-v2 reboot-cycle fix.

What this is
------------
The #24/#26 tmux provisioning passed unit + tmux parse-check but the LIVE reboot cycle
(apply -> save -> REBOOT -> restore) broke on a real machine — multiple defects that a
pure-render/unit suite cannot catch. This file is the acceptance gate the CTO asked for: it
drives REAL ``tmux`` (a real server, real sessions, the actual generated scripts) in a throwaway
``$HOME`` and asserts the whole cycle works with ZERO manual steps. It is the proof that a
clean-machine ``rig apply`` leaves tmux persistence FULLY working.

How it is reached
-----------------
The plugin-cloning tests are OPT-IN (``@_requires_tmux_e2e`` → ``RIG_TMUX_E2E=1`` + tmux + git +
network); a plain ``pytest`` SKIPS them so default CI stays hermetic. The socket-leak REGRESSION
(``test_teardown_unlinks_the_private_socket_file``) is gated on ``@_requires_tmux`` instead
(tmux-present only, NO network) so it runs in default hermetic CI — it guards the leak that killed
the dev's server, so it must NOT be hidden behind the network gate (INCIDENT 2026-06-17, follow-up
a). Every tmux call goes through a PRIVATE ``-L <socket>`` (a per-test ``tmux`` shim on PATH injects
``-L``), so it NEVER touches the developer's real tmux server. A session-scoped teardown kills every
spawned server/socket. The unit suite already covers the render/plan/drift logic hermetically.

What it proves (maps 1:1 to the six defects)
--------------------------------------------
1. boot: the generated boot script (NOT a bare ``start-server``) brings a server UP with the
   config LOADED and a session present; ``rig apply`` ``launchctl load -w``s the agent (asserted
   on the artifact + the load call, since a test can't reboot).
2. cc-save: a FAKE ``claude`` child under a pane's shell makes cc-save write a NON-EMPTY
   cwd->id map (the old command-string filter wrote nothing); cc-restore would relaunch
   ``claude --resume <id>`` into a fresh shell pane.
3. login shell: the generated config sets a login-shell ``default-command``.
4. resurrect dir: ``~/.tmux/resurrect`` exists and a real ``.txt`` snapshot is written.
5. old-boot cleanup: the stale continuum Login Items / ``Tmux.Start`` agent are removed.
6. plugins: tpm + resurrect + continuum are installed so the ``@plugin`` decls resolve, and a
   first ``resurrect save`` lands a snapshot.

Invariants
----------
- PRIVATE socket only (``-L``). Never the default server. Teardown kills the server AND unlinks
  its socket file — ``kill-server`` ends the process but leaks the socket inode on macOS, which
  once accumulated ~185 ``rigtest-*`` files in ``/tmp/tmux-501/`` and starved the dev's server.
- The generated scripts are run UNMODIFIED (via the PATH ``tmux`` shim) — testing the real
  artifact rig writes, not a paraphrase of it.
- The per-test gating granularity (leak regression tmux-only, plugin tests opt-in network) is
  PINNED by ``tests/test_tmux_e2e_gating.py`` — a separate, tamper-proof guard so a re-added
  blanket ``pytestmark`` can never silently re-hide the leak regression (INCIDENT follow-up a).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
import uuid
import socket
from pathlib import Path

import pytest

from riglib import tmux as tmod
from riglib.actions import runner


def _github_reachable() -> bool:
    """True if GitHub's https port is reachable — the e2e clones the real tmux plugins from there
    (it needs the REAL resurrect ``save.sh`` to write a real snapshot, the whole acceptance point).
    Even under the opt-in flag we still skip (not fail) when offline. Cheap TCP probe, 3s timeout."""
    try:
        with socket.create_connection(("github.com", 443), timeout=3):
            return True
    except OSError:
        return False


# Most tests here drive a REAL tmux server AND clone the real plugins from GitHub — real network +
# daemon access. The repo's plain `python -m pytest -q` is documented as fast + HERMETIC (AGENTS.md),
# so those e2e tests are OPT-IN via `RIG_TMUX_E2E=1` (codex finding): default CI/pytest stays hermetic
# and offline-safe; the BFS / artifact logic they exercise is ALSO covered hermetically (the unit suite
# + `test_pane_has_claude_*`, which run with no network). The acceptance gate the CTO runs is
# `RIG_TMUX_E2E=1 pytest tests/test_tmux_e2e.py`. Even when opted in, they skip (never fail) when
# tmux/git is absent or GitHub is unreachable. The autouse RIG_TMUX_DRY_RUN guard (conftest) is
# cleared per-test where a live step is exercised.
#
# The gate is applied PER-TEST (the decorators below), NOT as a blanket module-level `pytestmark`,
# because the gate's GRANULARITY differs by what a test actually needs (2026-06-17 INCIDENT
# follow-up (a)):
#   - `_requires_tmux_e2e` — the FULL opt-in + tmux + git + network gate, for the tests that clone
#     the real plugins from GitHub (the reboot-cycle acceptance, cc-save/restore, resurrect snapshot,
#     boot-cleanup). These genuinely need the network and stay opt-in so default CI is hermetic.
#   - `_requires_tmux` — tmux-present ONLY (no opt-in flag, NO network), for the socket-leak
#     REGRESSION (`test_teardown_unlinks_the_private_socket_file`). It boots a private `-L` server
#     running `tail -f /dev/null` and asserts teardown UNLINKS the socket — it clones nothing and
#     hits no network. Coupling it to `RIG_TMUX_E2E=1` + GitHub-reachability (the old blanket
#     `pytestmark`) meant the one regression guarding the leak that actually KILLED the dev's tmux
#     server (the ~185 leaked `rigtest-*` sockets of INCIDENT 2026-06-17 — 166 of them this fixture's)
#     NEVER ran in default hermetic CI or offline — exactly when it matters. Decoupled here so it
#     runs whenever tmux is installed.
_E2E_OPTED_IN = os.environ.get("RIG_TMUX_E2E", "").strip() in ("1", "true", "yes")

# Resolved ONCE at import — `_github_reachable()` does a 3s TCP probe, so calling it per-test (the
# 6 network tests each evaluating the decorator) would add up; one probe is enough for the gate.
# `_E2E_OPTED_IN` is the FIRST operand of the `and` chain ON PURPOSE: Python short-circuits, so the
# 3s probe is NEVER run unless the e2e is opted in — a default hermetic `pytest` (RIG_TMUX_E2E unset)
# pays ZERO network cost here, even offline. (Trade-off, same as the old `pytestmark`: the result is
# precomputed at import, so a transient outage at collection skips the 6 network tests for the whole
# session even if connectivity returns — acceptable; they are the opt-in acceptance gate, not CI.)
_NETWORK_E2E_AVAILABLE = (
    _E2E_OPTED_IN
    and shutil.which("tmux") is not None
    and shutil.which("git") is not None
    and _github_reachable()
)
_requires_tmux_e2e = pytest.mark.skipif(
    not _NETWORK_E2E_AVAILABLE,
    reason="real-tmux e2e is opt-in: set RIG_TMUX_E2E=1 (needs tmux + git + network; auto-skips offline)",
)
# tmux-only, no network, no opt-in flag — the socket-leak regression runs in default hermetic CI.
_requires_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="needs tmux installed (no network / no RIG_TMUX_E2E required)",
)


# ── a PATH tmux shim that pins every `tmux …` call to a private -L socket ────────────────────
def _install_tmux_shim(bindir: Path, socket: str) -> None:
    """Write a `tmux` wrapper on PATH that injects `-L <socket>` so the UNMODIFIED generated
    scripts (which call bare `tmux`) hit a private server, never the developer's default one."""
    real = shutil.which("tmux")
    shim = bindir / "tmux"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'exec {real} -L {socket} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)


def _socket_path_for(label: str, env: dict[str, str] | None = None) -> Path:
    """The on-disk path tmux uses for a ``-L <label>`` server: ``$TMUX_TMPDIR | /tmp`` +
    ``/tmux-<uid>/<label>``, resolved against ``env`` (defaults to ``os.environ``). Callers MUST
    pass the SAME env they launched the server with, else teardown resolves the wrong path and
    leaks the socket (see module docstring). Matches tmux's ``server_create_socket``:
    ``TMUX_TMPDIR`` is honored only when ABSOLUTE, else ``/tmp``."""
    src = os.environ if env is None else env
    tmpdir = src.get("TMUX_TMPDIR", "").strip()
    if not tmpdir or not os.path.isabs(tmpdir):
        tmpdir = "/tmp"
    return Path(tmpdir) / f"tmux-{os.getuid()}" / label


def _teardown_tmux_server(real_tmux: str, label: str, env: dict[str, str] | None = None) -> None:
    """Kill the private ``-L <label>`` server AND remove its socket file, resolving the socket
    path under ``env`` (the env the server was launched with). Both steps run best-effort: a
    failed/timed-out kill (e.g. no server ever started) must STILL let the unlink run, and a
    missing socket file must not raise. ``kill-server`` ends the process but leaks the socket
    inode on macOS — see the module docstring."""
    try:
        subprocess.run(
            [real_tmux, "-L", label, "kill-server"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass  # the unlink below must run regardless of how the kill went
    finally:
        _socket_path_for(label, env).unlink(missing_ok=True)


@pytest.fixture
def tmux_env(tmp_path, monkeypatch):
    """A throwaway HOME + a private tmux socket + a PATH shim, with teardown that kills the
    server. Yields (home, socket, run) where `run` executes a command with the shimmed PATH."""
    home = tmp_path / "home"
    home.mkdir()
    socket = f"rigtest-{uuid.uuid4().hex[:8]}"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _install_tmux_shim(bindir, socket)

    real_tmux = shutil.which("tmux")
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    # NB: the private `-L <socket>` already isolates the server; do NOT also set a deep
    # TMUX_TMPDIR — a unix socket path has a ~104-char limit on macOS and a pytest tmp dir blows
    # it ("File name too long"). The unique -L name under the default /tmp tmpdir is short + safe.
    # don't let an inherited $TMUX (we may run inside tmux) confuse nested calls.
    env.pop("TMUX", None)
    env.pop("TMUX_TMPDIR", None)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # CRITICAL: the generated boot script bakes its tmux path at GENERATION time via
    # `_resolve_tmux_bin` -> `shutil.which("tmux")`, which (in-process) is the REAL tmux on the
    # DEFAULT socket — NOT the shim. Left unpatched, the boot script would create a session on
    # the user's real tmux server (the exact thing the private -L socket is meant to prevent).
    # Point the resolver at the shim so EVERY tmux the rig artifacts invoke hits the private socket.
    from riglib import tmux as _tmod
    monkeypatch.setattr(_tmod, "_resolve_tmux_bin", lambda: str(bindir / "tmux"))

    def run(cmd, **kw):
        return subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=kw.pop("timeout", 30), **kw
        )

    try:
        yield home, socket, run
    finally:
        # kill the private server AND unlink its socket file — never leave a stray tmux server
        # OR a leaked socket inode behind (kill-server alone leaks the file on macOS). Resolve the
        # socket path under the SAME scrubbed `env` the server ran with (it pops TMUX_TMPDIR).
        _teardown_tmux_server(real_tmux, socket, env)


def _wait_for_claude_descendant(run, *, timeout_s=10):
    """Poll until a process whose `comm` is `claude` is a descendant of some tmux pane on the
    private socket — removes the race between launching the fake claude and `ps` seeing it.

    Uses the SAME tree-walk the cc-save script does, so if this sees the descendant, cc-save will
    too. Returns the pane addr when found; raises if it never appears (a real failure, not a flake).
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        panes = run(["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index} #{pane_pid}"])
        snap = subprocess.run(["ps", "-eo", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=10).stdout
        tree = {}
        comm = {}
        for ln in snap.splitlines():
            parts = ln.split(None, 2)
            if len(parts) == 3:
                pid, ppid, c = parts
                tree.setdefault(ppid, []).append(pid)
                comm[pid] = c
        for ln in panes.stdout.splitlines():
            addr, _, pane_pid = ln.partition(" ")
            stack = [pane_pid.strip()]
            seen = set()
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                if comm.get(cur, "").rsplit("/", 1)[-1] == "claude":
                    return addr
                stack.extend(tree.get(cur, []))
        time.sleep(0.3)
    raise AssertionError("fake claude never became a visible descendant of a pane within timeout")


def _action(home, **over):
    from riglib.plan import Action

    options = {
        "apply_mode": "import",
        "conf_path": str(home / ".tmux.conf"),
        "generated_dir": str(home / ".config" / "rig" / "tmux"),
        "resurrect": {},
        "continuum": {},
        "moshi": {},
        "cc_restore": {},
        "anti_sprawl": {"enabled": True, "session": "main"},
        "boot": {"enabled": True},
        "login_shell": {},
    }
    options.update(over)
    return Action(kind="provision_tmux", category="tmux", item="config",
                  source=home, target=home / ".tmux.conf", options=options)


def _apply_with_real_plugins(home, monkeypatch):
    """Run the full provision WITH real plugin clones + the resurrect dir, but keep launchctl
    stubbed (a test can't load a real launch agent without polluting the host). Returns the
    ActionResult. The boot script + cc scripts + config are all real on-disk artifacts."""
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    loads: list[str] = []
    monkeypatch.setattr(runner, "_launchctl", lambda verb, arg: 0)
    monkeypatch.setattr(runner, "_launchctl_loaded", lambda label: False)
    monkeypatch.setattr(runner, "_launchctl_load_enable", lambda plist: loads.append(str(plist)) or 0)
    monkeypatch.setattr(runner, "_clean_stale_continuum_boot", lambda plan: False)
    # Stub the in-apply first-save: it would boot a session on the host's DEFAULT tmux server
    # (the runner's _tmux_resurrect_save calls the boot script with the unshimmed PATH). The e2e
    # drives boot + save EXPLICITLY through the private-socket shim instead, so the assertions run
    # against an isolated server. We still let the REAL _git_clone + resurrect-dir creation run.
    monkeypatch.setattr(runner, "_tmux_resurrect_save", lambda plan: 0)
    res = runner._do_provision_tmux(_action(home), "backup")
    return res, loads


# ── the acceptance e2e ───────────────────────────────────────────────────────────────────────
@_requires_tmux_e2e
def test_clean_machine_apply_brings_tmux_up_with_config_and_session(tmux_env, monkeypatch):
    """DEFECTS 1/3/4/6: a clean-HOME apply installs plugins + scripts + config + boot agent, the
    boot script brings a REAL server up WITH the config loaded AND a session present, and the
    config sets a login-shell default-command."""
    home, socket, run = tmux_env
    res, loads = _apply_with_real_plugins(home, monkeypatch)
    assert res.status in ("created", "backed_up"), res.detail

    gen = home / ".config" / "rig" / "tmux"
    # DEFECT 6: plugins cloned so @plugin decls resolve.
    for name in ("tpm", "tmux-resurrect", "tmux-continuum"):
        assert (home / ".tmux" / "plugins" / name).is_dir(), f"{name} not installed"
    # DEFECT 4: resurrect snapshot dir exists.
    assert (home / ".tmux" / "resurrect").is_dir()
    # DEFECT 1: the boot agent plist points at the boot SCRIPT (not a bare start-server) and
    # rig launchctl-load-enabled it (the load call recorded; a test can't actually reboot).
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-boot.plist"
    if os.uname().sysname == "Darwin":
        assert plist.is_file()
        assert loads == [str(plist)], loads
    boot_script = gen / "tmux-boot.sh"
    assert boot_script.is_file() and os.access(boot_script, os.X_OK)

    # DEFECT 3: the generated config sets a login-shell default-command.
    conf_text = (gen / "rig.tmux.conf").read_text()
    assert "set -g default-command" in conf_text and "-l" in conf_text

    # the ~/.tmux.conf imports the generated file (so a new session loads it).
    assert f"source-file '{gen / 'rig.tmux.conf'}'" in (home / ".tmux.conf").read_text()

    # DEFECT 1 — EXECUTE the boot entrypoint (can't reboot in a test): it must bring a server UP
    # with the config LOADED and a session present. Run it via the shimmed PATH (private socket).
    r = run([str(boot_script)])
    assert r.returncode == 0, f"boot script failed: {r.stderr}"
    time.sleep(1.0)  # let the detached session + plugin inits settle before querying the server.
    # a server is now running with the canonical session.
    ls = run(["tmux", "ls"])
    assert ls.returncode == 0, f"`tmux ls` says no server: {ls.stderr or ls.stdout}"
    assert "main" in ls.stdout, ls.stdout
    # the CONFIG was actually loaded by that first session: a rig-set option is live on the server.
    # Use @continuum-restore (stable at 'on') rather than @continuum-save-interval — the latter is
    # now toggled by the independent-autosave feature (#138 sets it to 0 when the saver owns saving).
    opt = run(["tmux", "show-options", "-g", "@continuum-restore"])
    assert "on" in opt.stdout, f"config not loaded — continuum option absent: {opt.stdout!r} {opt.stderr!r}"
    # idempotent: a second boot does NOT create a duplicate session (anti-sprawl at boot).
    run([str(boot_script)])
    ls2 = run(["tmux", "ls"])
    assert ls2.stdout.count("main") == 1, f"boot spawned a duplicate session: {ls2.stdout}"


@_requires_tmux_e2e
def test_cc_save_populates_map_from_a_real_claude_child(tmux_env, monkeypatch):
    """DEFECT 2 (the headline reboot bug): a FAKE `claude` running as a CHILD of a pane's shell
    must make cc-save write a NON-EMPTY cwd->session-id map — the OLD `pane_current_command ==
    claude` filter wrote nothing because cc shows up as its version string, not `claude`."""
    home, socket, run = tmux_env
    _apply_with_real_plugins(home, monkeypatch)
    gen = home / ".config" / "rig" / "tmux"

    # A fake `claude` whose process `comm` reports `claude` (the production case: cc shows up as a
    # VERSION string in pane_current_command, the real `claude` is a CHILD). The process must have a
    # `comm` whose basename is `claude` on BOTH platforms — and the two single-trick approaches each
    # fail on one OS: `exec -a claude sleep` rewrites only argv[0] (Linux `comm` still reads `sleep`
    # → descendant invisible → CI failed); a COPY of the `sleep` binary won't run on macOS (SIP
    # refuses to exec an unsigned copy of a system binary). A SYMLINK named `claude` → the real
    # `sleep` works on both: the kernel sets `comm` from the invoked name, so `comm`'s basename is
    # `claude` on Linux AND macOS. Run by a LAUNCHER that keeps it a genuine child of the pane shell
    # (background it, the shell stays alive) — a bare send-keys `claude &` gets reparented by
    # job-control and detaches.
    work = home / "proj"
    work.mkdir()
    fake_claude = home / "fakebin" / "claude"
    fake_claude.parent.mkdir()
    real_sleep = shutil.which("sleep") or "/bin/sleep"
    fake_claude.symlink_to(real_sleep)  # symlink named `claude` → comm basename == claude on both OSes
    launcher = home / "launch.sh"
    launcher.write_text(
        f"#!/usr/bin/env bash\n{shlex.quote(str(fake_claude))} 300 &\nsleep 300\n", encoding="utf-8"
    )
    launcher.chmod(0o755)
    # the pane RUNS the launcher (so claude is a real descendant), in the known cwd.
    run(["tmux", "new-session", "-d", "-s", "main", "-c", str(work), str(launcher)])
    _wait_for_claude_descendant(run, timeout_s=10)

    # seed a Claude Code session file under the encoded projects dir for that cwd, so cc-save has
    # an id to record (encoding: every '/' and '.' -> '-').
    enc = str(work).replace("/", "-").replace(".", "-")
    proj = home / ".claude" / "projects" / enc
    proj.mkdir(parents=True)
    sid = "11111111-2222-3333-4444-555555555555"
    (proj / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")

    # RUN the real generated cc-save (via the shimmed tmux → private socket).
    r = run(["bash", str(gen / "cc-save.sh")])
    assert r.returncode == 0, f"cc-save failed: {r.stderr}"

    map_file = gen / "cc-sessions.map"
    assert map_file.is_file(), "cc-save wrote no map file"
    lines = [ln for ln in map_file.read_text().splitlines() if ln.strip()]
    assert lines, "DEFECT 2: cc-save map is EMPTY — the claude child was not detected via the tree"
    # the recorded line is <addr>\t<cwd>\t<session-id> for our claude pane.
    assert any(str(work) in ln and sid in ln for ln in lines), lines


@_requires_tmux_e2e
def test_cc_save_detects_the_versioned_binary_install(tmux_env, monkeypatch):
    """THE 2026-06-17 INCIDENT (versioned-binary half of DEFECT 2): cc installs as a symlink
    ``~/.local/bin/claude`` → ``…/claude/versions/<version>``. Launched by the RESOLVED path (not
    the ``claude`` symlink), the process's name is the VERSION string (``2.1.179``), NOT ``claude``
    — so a basename-only ``claude``/``*/claude`` match missed it and the map stayed empty (cc never
    resumed after a reboot, the live incident). cc-save must still detect it via the
    ``…/claude/versions/`` path arm of the tree-walk match (which reads ``ps -o args`` so the path
    is visible on both macOS and Linux — Linux ``comm`` is the truncated basename with no path).

    We reproduce the EXACT install shape: a ``claude/versions/<version>`` symlink → ``sleep`` run by
    its resolved PATH, as a real child of a pane shell, so its ``args`` (argv[0]) is
    ``…/claude/versions/<version>``. A SYMLINK (not a copy) is used so the launcher works on macOS
    too: macOS SIP refuses to exec an unsigned COPY of a protected system binary, but exec'ing a
    symlink to it is allowed, and ``args`` reflects the invoked path either way.
    """
    home, socket, run = tmux_env
    _apply_with_real_plugins(home, monkeypatch)
    gen = home / ".config" / "rig" / "tmux"

    work = home / "verproj"
    work.mkdir()
    version = "2.1.179"
    versions_dir = home / ".local" / "share" / "claude" / "versions"
    versions_dir.mkdir(parents=True)
    versioned = versions_dir / version
    real_sleep = shutil.which("sleep") or "/bin/sleep"
    versioned.symlink_to(real_sleep)  # argv[0] == the versioned path under claude/versions/
    launcher = home / "launch-ver.sh"
    # run the versioned binary BY ITS RESOLVED PATH (the failing production case), backgrounded so
    # it stays a genuine descendant of the launcher (which keeps the pane shell alive).
    launcher.write_text(
        f"#!/usr/bin/env bash\n{shlex.quote(str(versioned))} 300 &\nsleep 300\n", encoding="utf-8"
    )
    launcher.chmod(0o755)

    # seed a Claude Code session file for that cwd so cc-save has an id to record.
    enc = str(work).replace("/", "-").replace(".", "-")
    proj = home / ".claude" / "projects" / enc
    proj.mkdir(parents=True)
    sid = "99999999-8888-7777-6666-555555555555"
    (proj / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")

    run(["tmux", "new-session", "-d", "-s", "main", "-c", str(work), str(launcher)])
    # POLL the REAL generated cc-save until it records the pane (or time out). We assert on cc-save's
    # OWN output — the production acceptance criterion — instead of a separate `ps` probe, so the
    # test validates the exact production matcher on the exact platform (a flat `ps args` probe
    # diverged from how the shimmed-socket tree walk sees the pane and flaked on CI). This loop also
    # absorbs the launch→ps-visibility race. If cc-save can detect the versioned descendant, the map
    # is non-empty; if it can't (the regression / a real platform gap), the loop times out and fails.
    map_file = gen / "cc-sessions.map"
    deadline = time.time() + 15
    lines: list[str] = []
    found = False
    while time.time() < deadline:
        r = run(["bash", str(gen / "cc-save.sh")])
        assert r.returncode == 0, f"cc-save failed: {r.stderr}"
        # cc-save has finished writing (sequential — `run` returns before we read), so the read is
        # not racing a concurrent writer. Break ONLY when OUR versioned pane (this cwd + sid) is in
        # the map — not merely on any non-empty map: cc-save scans ALL panes, so an unrelated claude
        # process on the host (a parallel test, the dev's own session) could populate it without our
        # descendant yet being visible, and an early break would then flake the final assertion.
        if map_file.is_file():
            lines = [ln for ln in map_file.read_text().splitlines() if ln.strip()]
            if any(str(work) in ln and sid in ln for ln in lines):
                found = True
                break
        time.sleep(0.3)
    # the production matcher (the `.../claude/versions/` arm) found OUR versioned pane and recorded
    # its exact cwd→session-id. A timeout here is the INCIDENT regression (or a real platform gap).
    assert found, (
        "INCIDENT: cc-save did NOT record the VERSIONED-binary cc pane "
        f"(cwd={work}, sid={sid}); map lines={lines!r}"
    )


@_requires_tmux_e2e
def test_cc_restore_relaunches_claude_resume_into_fresh_shell(tmux_env, monkeypatch):
    """DEFECT 2 (restore half): with a seeded map, cc-restore sends `cd <cwd> && claude --resume
    <id>` into a FRESH shell pane (never on top of a running claude / an editor)."""
    home, socket, run = tmux_env
    _apply_with_real_plugins(home, monkeypatch)
    gen = home / ".config" / "rig" / "tmux"

    work = home / "proj"
    work.mkdir()
    # A FAKE `claude` on the pane's PATH so the resume that cc-restore types runs a harmless
    # sleep (NOT the real Claude Code, which would launch its onboarding TUI). `exec -a claude`
    # makes its `comm` report `claude`. We then assert the resume command was TYPED into the pane.
    fakebin = home / "fakebin"
    fakebin.mkdir()
    (fakebin / "claude").write_text(
        "#!/usr/bin/env bash\nexec -a claude sleep 300\n", encoding="utf-8"
    )
    (fakebin / "claude").chmod(0o755)
    # a fresh shell pane (bash, no rc) with the fake claude FIRST on PATH, in the known cwd.
    run(["tmux", "new-session", "-d", "-s", "main", "-c", str(work),
         f"PATH={fakebin}:$PATH exec bash --norc -i"])
    # resolve the real pane addr (window base-index may be 1, not 0).
    addr = run(["tmux", "list-panes", "-a", "-F",
                "#{session_name}:#{window_index}.#{pane_index}"]).stdout.splitlines()[0]
    # seed the projects session file (so the id is "live" → --resume, not --continue).
    enc = str(work).replace("/", "-").replace(".", "-")
    proj = home / ".claude" / "projects" / enc
    proj.mkdir(parents=True)
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    (proj / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
    # seed the map cc-restore reads.
    (gen / "cc-sessions.map").write_text(f"{addr}\t{work}\t{sid}\n", encoding="utf-8")

    # RUN the real cc-restore: it must send `cd <cwd> && claude --resume <id>` into the pane.
    r = run(["bash", str(gen / "cc-restore.sh")])
    assert r.returncode == 0, f"cc-restore failed: {r.stderr}"
    time.sleep(0.5)
    cap = run(["tmux", "capture-pane", "-t", addr, "-p"])
    # tmux HARD-WRAPS the pane at the terminal width, so a long command is split across lines
    # (`claud\ne`). Join the captured lines (drop the wrap newlines) before substring-matching.
    joined = cap.stdout.replace("\n", "")
    # the resume command line was typed into the fresh shell pane.
    assert "claude --resume" in joined, cap.stdout
    assert sid in joined, cap.stdout
    assert str(work).replace("\n", "") in joined, cap.stdout


@_requires_tmux_e2e
def test_resurrect_writes_a_real_snapshot(tmux_env, monkeypatch):
    """DEFECTS 4/6: with the resurrect dir present + the plugin installed, a real `resurrect save`
    writes a `tmux_resurrect_*.txt` snapshot — so a reboot has something to restore."""
    home, socket, run = tmux_env
    _apply_with_real_plugins(home, monkeypatch)

    resurrect_dir = home / ".tmux" / "resurrect"
    assert resurrect_dir.is_dir()
    save_script = home / ".tmux" / "plugins" / "tmux-resurrect" / "scripts" / "save.sh"
    assert save_script.is_file(), "resurrect plugin not installed (save.sh missing)"

    # a real session to snapshot, then run resurrect's own save (private socket via the shim).
    run(["tmux", "new-session", "-d", "-s", "main"])
    r = run(["bash", str(save_script)])
    assert r.returncode == 0, f"resurrect save failed: {r.stderr}"
    snaps = list(resurrect_dir.glob("tmux_resurrect_*.txt"))
    assert snaps, f"no resurrect snapshot written in {resurrect_dir}"


@_requires_tmux
def test_old_continuum_boot_cleanup_removes_stale_entries(tmux_env, monkeypatch):
    """DEFECT 5: a pre-existing stale continuum boot (its osx_disable.sh + an old Tmux.Start
    launch agent) is cleaned by the activation. We MOCK their presence and assert removal.

    Gated ``@_requires_tmux`` (tmux-only), NOT the network gate: this test clones NOTHING and hits
    no network — it fabricates the stale files, MOCKS ``_launchctl``, and calls
    ``_clean_stale_continuum_boot`` directly. It only needs the ``tmux_env`` fixture (which installs
    a tmux shim and tears a private server down, so it needs tmux on PATH but no GitHub). Running it
    in default hermetic CI catches a DEFECT-5 cleanup regression on every PR, not only opt-in runs."""
    home, socket, run = tmux_env
    # simulate continuum's stale boot: an osx_disable.sh under the plugin + a Tmux.Start plist.
    cont = home / ".tmux" / "plugins" / "tmux-continuum" / "scripts"
    cont.mkdir(parents=True)
    disable_ran = home / "disable-ran"
    (cont / "osx_disable.sh").write_text(
        f"#!/usr/bin/env bash\ntouch {disable_ran}\n", encoding="utf-8"
    )
    (cont / "osx_disable.sh").chmod(0o755)
    la = home / "Library" / "LaunchAgents"
    la.mkdir(parents=True)
    old_plist = la / "Tmux.Start.plist"
    old_plist.write_text("<plist></plist>\n", encoding="utf-8")

    boot_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(runner, "_launchctl", lambda verb, arg: boot_calls.append((verb, arg)) or 0)
    plan = tmod.build_tmux(repo_home=home)
    cleaned = runner._clean_stale_continuum_boot(plan)

    assert cleaned is True
    # continuum's documented disable script was run …
    assert disable_ran.is_file(), "osx_disable.sh was not executed"
    # … the old Tmux.Start plist was removed …
    assert not old_plist.exists(), "stale Tmux.Start.plist not removed"
    # … and we issued a bootout/unload for it.
    assert any("Tmux.Start" in arg or "Tmux.Start" in verb for verb, arg in boot_calls), boot_calls
    # idempotent: a second run (now nothing present) cleans nothing and doesn't error.
    assert runner._clean_stale_continuum_boot(plan) is False


# ── socket-leak regression (the leak in the module docstring: ~185 rigtest-* sockets) ───────
# Gated on `_requires_tmux` (tmux-present ONLY), NOT the full `_requires_tmux_e2e` network gate:
# this clones nothing and hits no network, so coupling it to RIG_TMUX_E2E + GitHub-reachability
# would hide the very regression that guards the leak which killed the dev's server (INCIDENT
# 2026-06-17 follow-up a). With this marker it runs in default hermetic CI whenever tmux exists.
@_requires_tmux
def test_teardown_unlinks_the_private_socket_file():
    """REGRESSION for the socket leak described in the module docstring: ``_teardown_tmux_server``
    must leave NO socket file on disk (``kill-server`` alone leaks the inode on macOS). Drives the
    SAME teardown the fixture uses, against a REAL server, and asserts the file — not just the
    process — is gone."""
    real_tmux = shutil.which("tmux")
    # The `@_requires_tmux` marker already guarantees tmux is on PATH; assert so that if it were
    # ever absent at runtime (marker bypassed / tmux removed mid-session) we fail with a clear
    # message instead of a confusing `None`-path TypeError further down.
    assert real_tmux, "tmux must be on PATH (guarded by @_requires_tmux)"
    label = f"rigtest-leakcheck-{uuid.uuid4().hex[:8]}"

    # scrub TMUX_TMPDIR/TMUX so the server lands at the default /tmp path, and resolve the socket
    # path under that SAME env — the whole bug being guarded is an env/path mismatch here.
    env = dict(os.environ)
    env.pop("TMUX", None)
    env.pop("TMUX_TMPDIR", None)
    sock = _socket_path_for(label, env)
    try:
        boot = subprocess.run(
            [real_tmux, "-L", label, "new-session", "-d", "-s", "probe", "tail -f /dev/null"],
            env=env, capture_output=True, text=True, timeout=15,
        )
        assert boot.returncode == 0, boot.stderr
        assert sock.exists(), f"server did not create its socket at {sock}"

        _teardown_tmux_server(real_tmux, label, env)

        assert not sock.exists(), f"LEAK: socket file lingered after teardown: {sock}"
    finally:
        # belt-and-suspenders if an assert fired mid-way — reuse the production teardown.
        _teardown_tmux_server(real_tmux, label, env)


# ── migrated-conf parse validity (the legacy-init neutralization fix, 2026-06-18) ───────────
# Gated `@_requires_tmux` (tmux-present ONLY, no network, no opt-in): it clones nothing and
# fabricates everything in a throwaway HOME, so it runs in default hermetic CI and guards the
# neutralization fix on every PR — a future change that produces a syntactically broken migrated
# ~/.tmux.conf (e.g. a mangled `# rig-migrated:` comment, an unbalanced if-shell brace) fails
# LOUDLY here instead of only on a live machine.
@_requires_tmux
def test_migrated_conf_with_neutralized_legacy_init_parses(tmux_env, monkeypatch):
    """A hand-written conf carrying the FULL old tpm/resurrect/continuum init + personal prefs is
    migrated (import mode) by the REAL runner, then loaded by a REAL tmux on a private socket:
    `tmux -L <iso> -f <migrated ~/.tmux.conf> new-session -d` must exit 0 (the neutralized lines
    are valid comments, the surviving prefs + source-file parse cleanly).

    @continuum-restore is forced OFF in the generated config (continuum.restore=False) so even if
    the (absent) continuum plugin's run-shell were reachable, NOTHING restores into the throwaway
    server. login_shell + boot are off to keep the load self-contained. Teardown kills the server
    AND unlinks the socket (the fixture's `finally`)."""
    home, socket, run = tmux_env
    real_tmux = shutil.which("tmux")
    assert real_tmux, "tmux must be on PATH (guarded by @_requires_tmux)"

    conf = home / ".tmux.conf"
    conf.write_text(
        "set -g mouse on\n"
        "set -g history-limit 100000\n"
        "set -g @plugin 'tmux-plugins/tmux-sensible'\n"   # third-party — survives, but no plugin dir
        "set -g @plugin 'tmux-plugins/tpm'\n"
        "set -g @plugin 'tmux-plugins/tmux-resurrect'\n"
        "set -g @plugin 'tmux-plugins/tmux-continuum'\n"
        "set -g @resurrect-processes 'ssh psql ~rails'\n"
        "set -g @continuum-restore 'on'\n"
        "set -g @continuum-boot 'on'\n"
        "run-shell ~/.tmux/plugins/tmux-resurrect/resurrect.tmux\n"
        "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n"
        "if-shell '[ -n \"$MOSHI_CLIENT\" ]' { set -g status-right '' }\n"
        # a rig-owned option a user GUARDED inside a NESTED non-Moshi conditional — left LIVE by
        # migration (under-reach); the resulting `{ { … } }` must still PARSE. (Proves the
        # structural brace-skip never leaves a dangling/empty brace that breaks the load, including
        # nested braces where a boolean flag would have re-exposed the outer block — review.)
        "if-shell '[ -n \"$SOME_FLAG\" ]' {\n"
        "  if-shell '[ -n \"$OTHER\" ]' {\n"
        "    set -g @continuum-save-interval '7'\n"
        "  }\n"
        "  set -g @resurrect-strategy-vim 'session'\n"
        "}\n"
        "run '~/.tmux/plugins/tpm/tpm'\n",
        encoding="utf-8",
    )

    # migrate via the REAL runner (no live activation: dry-run + boot/login_shell off). With
    # continuum.restore off and login_shell off, the generated rig.tmux.conf neither restores nor
    # bakes a default-command, so loading it is side-effect-free in the throwaway server.
    monkeypatch.setenv("RIG_TMUX_DRY_RUN", "1")
    action = _action(
        home,
        boot={"enabled": False},
        login_shell={"enabled": False},
        continuum={"restore": False},
        anti_sprawl={"enabled": False},
    )
    runner._do_provision_tmux(action, "backup")

    migrated = conf.read_text()
    # sanity: the legacy continuum init is neutralized (commented), the pref survives live.
    assert "# rig-migrated (now in rig.tmux.conf): run-shell ~/.tmux/plugins/tmux-continuum" in migrated
    assert any(
        ln.strip() == "set -g mouse on" for ln in migrated.splitlines()
    ), "personal pref must survive migration"

    # load the migrated user conf in a REAL tmux on the private socket — must parse with exit 0.
    res = run([
        "tmux", "-L", socket, "-f", str(conf),
        "new-session", "-d", "-s", "parsecheck", "tail -f /dev/null",
    ])
    assert res.returncode == 0, (
        f"migrated ~/.tmux.conf failed to parse (rc={res.returncode}):\n"
        f"STDERR:\n{res.stderr}\nCONF:\n{migrated}"
    )
    # the server came up with the session — a parse abort would have left no server.
    listed = run(["tmux", "-L", socket, "list-sessions"])
    assert "parsecheck" in listed.stdout, listed.stdout + listed.stderr


@_requires_tmux
def test_comment_only_brace_body_after_moshi_neutralize_parses(tmux_env):
    """A Moshi wipe block nested inside an OUTER user conditional: migration comments the inner
    Moshi block WHOLE, leaving the outer `{ … }` around a COMMENT-ONLY body. tmux must still parse
    `{ # … }` — proven against a REAL tmux on a private socket, not only on a live machine. This is
    the one structural shape the unit suite can't fully vouch for (it can't run the tmux parser)."""
    home, socket, run = tmux_env
    real_tmux = shutil.which("tmux")
    assert real_tmux, "tmux must be on PATH (guarded by @_requires_tmux)"

    conf = home / ".tmux.conf"
    raw = (
        "set -g mouse on\n"
        "if-shell '[ -n \"$OUTER\" ]' {\n"
        "  if-shell '[ -n \"$MOSHI_CLIENT\" ]' {\n"
        "    set -g status-right ''\n"
        "  }\n"
        "}\n"
    )
    conf.write_text(raw, encoding="utf-8")
    # neutralize via the pure helper (the same one apply uses), then load the RESULT in tmux.
    migrated = tmod.neutralize_inline_rig_lines(raw)
    conf.write_text(migrated, encoding="utf-8")
    # sanity: the inner Moshi block (incl. its braces) is commented; the OUTER braces stay live →
    # a comment-only body inside `{ … }`.
    lines = migrated.splitlines()
    assert any(ln.strip() == "if-shell '[ -n \"$OUTER\" ]' {" for ln in lines), "outer brace live"
    assert not any(
        "status-right ''" in ln and not ln.lstrip().startswith("#") for ln in lines
    ), "the inner Moshi wipe must be commented"
    assert any(
        ln.lstrip().startswith(tmod.NEUTRALIZE_PREFIX) and "status-right ''" in ln for ln in lines
    ), "the wipe survives as a rig-migrated comment inside the live outer braces"

    res = run([
        "tmux", "-L", socket, "-f", str(conf),
        "new-session", "-d", "-s", "braceparse", "tail -f /dev/null",
    ])
    assert res.returncode == 0, (
        f"comment-only-brace-body conf failed to parse (rc={res.returncode}):\n"
        f"STDERR:\n{res.stderr}\nCONF:\n{migrated}"
    )
    listed = run(["tmux", "-L", socket, "list-sessions"])
    assert "braceparse" in listed.stdout, listed.stdout + listed.stderr
