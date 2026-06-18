"""The repo-local pre-commit smoke GATE — installer + hook + the `tests/smoke.sh --fast` subset.

The CTO's 2026-06-16 requirement had two halves: smoke must run in CI (already true) AND a
commit that breaks the real `rig status`/CLI flow must be blocked LOCALLY, before the push.
The local half is wired by ``scripts/install-smoke-precommit.sh`` (writes a thin
``.git/hooks/pre-commit`` shim) → ``scripts/smoke-precommit-hook.sh`` (runs
``bash tests/smoke.sh --fast`` and, when no global composer/foreign hook owns it, chains the
global dispatcher).

These tests exercise the REAL shell scripts end-to-end in hermetic, HOME-isolated throwaway git
repos (no mocks of the scripts themselves) — they drive a real ``git commit`` through a real
installed hook and assert the commit is allowed/blocked. Each repo pins its own
``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` and a raw ``core.hooksPath`` so the developer's real
global composer/review-gate never bleeds in.

The ``--fast`` subset of the project's own ``tests/smoke.sh`` is also asserted directly: it must
exit 0, skip the heavy apply + pytest legs, and still run the real-catalog ``rig status`` legs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "scripts" / "install-smoke-precommit.sh"
GATE = REPO_ROOT / "scripts" / "smoke-precommit-hook.sh"
SMOKE = REPO_ROOT / "tests" / "smoke.sh"


def _git(repo: Path, *args: str, env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env, check=check, capture_output=True, text=True,
    )


def _isolated_env(home: Path) -> dict[str, str]:
    """A git env fully decoupled from the developer's real global/system config.

    Pins GIT_CONFIG_GLOBAL/SYSTEM so the real ~/.config/git composer + review-gate can't fire,
    and HOME/XDG so the gate's own ``~/.config/git/run-global-hooks`` lookup resolves under tmp.
    """
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        GIT_CONFIG_GLOBAL=str(home / ".gitconfig"),
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_TERMINAL_PROMPT="0",
    )
    return env


def _make_repo(tmp_path: Path, smoke_body: str) -> tuple[Path, dict[str, str]]:
    """A throwaway git repo vendoring the real gate+installer and a STUB tests/smoke.sh.

    The stub stands in for the project's real smoke so a test can deterministically make the
    gate pass or fail without running the full CLI; the real ``smoke.sh --fast`` is exercised
    separately in :func:`test_real_smoke_fast_*`.
    """
    home = tmp_path / "home"
    env = _isolated_env(home)
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    shutil.copy2(INSTALLER, repo / "scripts" / INSTALLER.name)
    shutil.copy2(GATE, repo / "scripts" / GATE.name)
    smoke = repo / "tests" / "smoke.sh"
    smoke.write_text(smoke_body, encoding="utf-8")
    smoke.chmod(0o755)

    _git(repo.parent, "init", "-q", str(repo), env=env)
    # RAW repo: pin core.hooksPath to the standard dir so no global composer is in play and the
    # gate takes its "chain the dispatcher" branch (which, with no dispatcher present, no-ops).
    _git(repo, "config", "core.hooksPath", ".git/hooks", env=env)
    _git(repo, "config", "user.email", "t@rig.test", env=env)
    _git(repo, "config", "user.name", "rig-test", env=env)
    return repo, env


def _install_stub_dispatcher(env: dict[str, str]) -> Path:
    """Drop a stub ``run-global-hooks`` under the isolated HOME's ~/.config/git that APPENDS a
    line to a marker file each time it runs. Lets a test assert the dispatcher fired exactly
    once (the 'never double-run secret-scan' contract) by counting marker lines.
    """
    cfg = Path(env["XDG_CONFIG_HOME"]) / "git"
    cfg.mkdir(parents=True, exist_ok=True)
    marker = cfg / "dispatcher-ran.log"
    disp = cfg / "run-global-hooks"
    disp.write_text(f'#!/bin/sh\necho "ran:$1" >> "{marker}"\nexit 0\n', encoding="utf-8")
    disp.chmod(0o755)
    return marker


_PASS_SMOKE = '#!/usr/bin/env bash\n[ "$1" = "--fast" ] && { echo "stub smoke --fast OK"; exit 0; }\nexit 0\n'
_FAIL_SMOKE = '#!/usr/bin/env bash\n[ "$1" = "--fast" ] && { echo "stub smoke --fast FAIL"; exit 1; }\nexit 0\n'


def _commit(repo: Path, env: dict[str, str], msg: str) -> subprocess.CompletedProcess:
    _git(repo, "add", "-A", env=env)
    return subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", msg],
        env={**env, "GIT_EDITOR": "true"}, capture_output=True, text=True,
    )


# ── installer ─────────────────────────────────────────────────────────────────────────────

def test_installer_writes_executable_pre_commit_hook(tmp_path):
    repo, env = _make_repo(tmp_path, _PASS_SMOKE)
    res = subprocess.run(["sh", str(repo / "scripts" / INSTALLER.name)],
                         cwd=repo, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    hook = repo / ".git" / "hooks" / "pre-commit"
    assert hook.is_file()
    assert os.access(hook, os.X_OK), "installed pre-commit hook is not executable"
    body = hook.read_text(encoding="utf-8")
    assert "rig-smoke-precommit" in body, "idempotency marker missing from installed hook"
    assert "scripts/smoke-precommit-hook.sh" in body, "hook does not exec the tracked gate"


def test_installer_is_idempotent(tmp_path):
    repo, env = _make_repo(tmp_path, _PASS_SMOKE)
    sh = ["sh", str(repo / "scripts" / INSTALLER.name)]
    first = subprocess.run(sh, cwd=repo, env=env, capture_output=True, text=True)
    assert first.returncode == 0
    before = (repo / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    second = subprocess.run(sh, cwd=repo, env=env, capture_output=True, text=True)
    assert second.returncode == 0
    assert "no-op" in second.stdout
    after = (repo / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    assert before == after, "second install mutated the hook (not idempotent)"


def test_installer_prepends_into_existing_foreign_hook(tmp_path):
    repo, env = _make_repo(tmp_path, _PASS_SMOKE)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho FOREIGN-HOOK-RAN\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    res = subprocess.run(["sh", str(repo / "scripts" / INSTALLER.name)],
                         cwd=repo, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    body = hook.read_text(encoding="utf-8")
    # our gate is prepended (after the shebang) AND the foreign body survives below it
    assert "rig-smoke-precommit" in body
    assert "FOREIGN-HOOK-RAN" in body
    # this foreign hook does NOT call run-global-hooks → the gate must KEEP chaining the
    # dispatcher (so secret-scan still fires); the disable flag must NOT be set.
    assert "RIG_SMOKE_GATE_CHAIN_DISPATCHER=0" not in body
    # the foreign echo still runs at commit time (gate prepended, did not clobber it)
    out = _commit(repo, env, "with foreign hook")
    assert out.returncode == 0, out.stderr + out.stdout
    assert "FOREIGN-HOOK-RAN" in (out.stdout + out.stderr)


def test_installer_never_disables_chaining_in_prepend(tmp_path):
    # FAIL SAFE: the installer must NEVER emit RIG_SMOKE_GATE_CHAIN_DISPATCHER=0 when prepending,
    # even into a hook that itself invokes the dispatcher. Dropping our chain risks losing
    # secret-scan; a (rare) double read-only scan is harmless. The gate's own composer check is
    # what reliably de-dupes the composer case — not a fragile grep of the foreign body.
    repo, env = _make_repo(tmp_path, _PASS_SMOKE)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        '#!/bin/sh\n'
        'DISP="${XDG_CONFIG_HOME:-$HOME/.config}/git/run-global-hooks"\n'
        '[ -x "$DISP" ] && { "$DISP" pre-commit "$@" || exit $?; }\n'
        'exit 0\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)
    res = subprocess.run(["sh", str(repo / "scripts" / INSTALLER.name)],
                         cwd=repo, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    body = hook.read_text(encoding="utf-8")
    assert "RIG_SMOKE_GATE_CHAIN_DISPATCHER=0" not in body, "installer disabled chaining (drop risk)"
    assert "run-global-hooks" in body  # the foreign body survives below our gate


# ── the gate at commit time ─────────────────────────────────────────────────────────────────

def test_commit_blocked_when_fast_smoke_fails(tmp_path):
    repo, env = _make_repo(tmp_path, _FAIL_SMOKE)
    subprocess.run(["sh", str(repo / "scripts" / INSTALLER.name)], cwd=repo, env=env, check=True)
    out = _commit(repo, env, "should be blocked")
    assert out.returncode != 0, "commit was allowed despite a failing fast smoke"
    assert "BLOCKED" in (out.stdout + out.stderr)
    # nothing landed: HEAD has no commits
    log = _git(repo, "log", "--oneline", env=env, check=False)
    assert log.returncode != 0 or log.stdout.strip() == "", "a commit landed despite the block"


def test_commit_allowed_when_fast_smoke_passes(tmp_path):
    repo, env = _make_repo(tmp_path, _PASS_SMOKE)
    subprocess.run(["sh", str(repo / "scripts" / INSTALLER.name)], cwd=repo, env=env, check=True)
    out = _commit(repo, env, "should pass")
    assert out.returncode == 0, out.stderr + out.stdout
    log = _git(repo, "log", "--oneline", env=env)
    assert "should pass" in log.stdout


def test_gate_passes_fast_flag_to_smoke(tmp_path):
    # the gate must invoke smoke with EXACTLY --fast (not the heavy full run): a stub that fails
    # unless it sees --fast proves the flag is threaded through.
    only_fast = '#!/usr/bin/env bash\n[ "$1" = "--fast" ] || { echo "MISSING --fast"; exit 3; }\nexit 0\n'
    repo, env = _make_repo(tmp_path, only_fast)
    subprocess.run(["sh", str(repo / "scripts" / INSTALLER.name)], cwd=repo, env=env, check=True)
    out = _commit(repo, env, "fast flag threaded")
    assert out.returncode == 0, out.stdout + out.stderr


def test_gate_bypass_env_skips_smoke(tmp_path):
    # SKIP_RIG_SMOKE=1 must let a commit through even when the smoke would fail (documented escape).
    repo, env = _make_repo(tmp_path, _FAIL_SMOKE)
    subprocess.run(["sh", str(repo / "scripts" / INSTALLER.name)], cwd=repo, env=env, check=True)
    _git(repo, "add", "-A", env=env)
    out = subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "bypassed"],
        env={**env, "GIT_EDITOR": "true", "SKIP_RIG_SMOKE": "1"}, capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "skipping the fast smoke gate" in (out.stdout + out.stderr)


def test_gate_no_op_without_smoke_script(tmp_path):
    # A repo with NO tests/smoke.sh must not be blocked by the gate (the gate is a no-op there).
    repo, env = _make_repo(tmp_path, _PASS_SMOKE)
    (repo / "tests" / "smoke.sh").unlink()
    subprocess.run(["sh", str(repo / "scripts" / INSTALLER.name)], cwd=repo, env=env, check=True)
    out = _commit(repo, env, "no smoke present")
    assert out.returncode == 0, out.stdout + out.stderr


# ── dispatcher-chaining contract (secret-scan runs exactly once) ─────────────────────────────

def test_gate_chains_dispatcher_once_in_raw_repo(tmp_path):
    # Raw repo, NO composer: the gate must invoke the global dispatcher exactly once.
    repo, env = _make_repo(tmp_path, _PASS_SMOKE)
    marker = _install_stub_dispatcher(env)
    subprocess.run(["sh", str(repo / "scripts" / INSTALLER.name)], cwd=repo, env=env, check=True)
    out = _commit(repo, env, "raw chains dispatcher")
    assert out.returncode == 0, out.stdout + out.stderr
    runs = marker.read_text().splitlines() if marker.exists() else []
    assert runs == ["ran:pre-commit"], f"dispatcher did not run exactly once: {runs!r}"


def test_gate_skips_dispatcher_when_composer_active(tmp_path):
    # When core.hooksPath IS the composer dir (~/.config/git/hooks with an executable
    # pre-commit), the composer owns the dispatcher → the gate must NOT chain it (no double run).
    repo, env = _make_repo(tmp_path, _PASS_SMOKE)
    marker = _install_stub_dispatcher(env)
    composer_dir = Path(env["XDG_CONFIG_HOME"]) / "git" / "hooks"
    composer_dir.mkdir(parents=True, exist_ok=True)
    # a REAL composer pre-commit: one that actually invokes the dispatcher (run-global-hooks).
    # The gate must see it WILL run the dispatcher and therefore not chain it itself.
    (composer_dir / "pre-commit").write_text(
        '#!/bin/sh\n"$(dirname "$0")/../run-global-hooks" pre-commit "$@" || exit $?\nexit 0\n',
        encoding="utf-8",
    )
    (composer_dir / "pre-commit").chmod(0o755)
    # point THIS repo's hooks at the composer dir (as a global hooksPath would), and run the
    # gate directly (the composer would normally invoke $git_dir/hooks/pre-commit; we call the
    # gate to assert its own decision).
    _git(repo, "config", "core.hooksPath", str(composer_dir), env=env)
    res = subprocess.run(
        ["sh", str(repo / "scripts" / GATE.name)],
        cwd=repo, env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert not marker.exists(), "gate chained the dispatcher despite an active composer (double run)"


def test_gate_skips_dispatcher_with_tilde_hookspath(tmp_path):
    # core.hooksPath can be a literal `~/.config/git/hooks` (git stores it verbatim). The gate's
    # canon() must expand `~` so the composer match still fires — otherwise it would NOT detect the
    # composer and chain the dispatcher in ADDITION to it (the double secret-scan the dedup avoids).
    # Pin XDG_CONFIG_HOME under $HOME so the `~`-expanded path and the dispatcher's XDG path agree.
    home = tmp_path / "home"
    home.mkdir()
    env = _isolated_env(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    shutil.copy2(INSTALLER, repo / "scripts" / INSTALLER.name)
    shutil.copy2(GATE, repo / "scripts" / GATE.name)
    (repo / "tests" / "smoke.sh").write_text(_PASS_SMOKE, encoding="utf-8")
    (repo / "tests" / "smoke.sh").chmod(0o755)
    _git(repo.parent, "init", "-q", str(repo), env=env)
    _git(repo, "config", "user.email", "t@rig.test", env=env)
    _git(repo, "config", "user.name", "rig-test", env=env)
    marker = _install_stub_dispatcher(env)
    composer_dir = home / ".config" / "git" / "hooks"
    composer_dir.mkdir(parents=True, exist_ok=True)
    # a REAL composer that invokes the dispatcher (so the gate trusts it to run it).
    (composer_dir / "pre-commit").write_text(
        '#!/bin/sh\n"$(dirname "$0")/../run-global-hooks" pre-commit "$@" || exit $?\nexit 0\n',
        encoding="utf-8",
    )
    (composer_dir / "pre-commit").chmod(0o755)
    # LITERAL tilde — the path git returns verbatim; canon() must expand it.
    _git(repo, "config", "core.hooksPath", "~/.config/git/hooks", env=env)
    res = subprocess.run(["sh", str(repo / "scripts" / GATE.name)],
                         cwd=repo, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
    assert not marker.exists(), "tilde core.hooksPath was not recognized as the composer (double run)"


def test_gate_strips_git_env_before_running_smoke(tmp_path):
    # REGRESSION (found by dogfooding): git exports GIT_DIR / GIT_INDEX_FILE into the hook env,
    # which would leak into smoke's own `git`/`rig status` calls and break a leg (clean-sample
    # exit 3). The gate must strip the GIT_* hook env for the smoke run. Use a stub smoke that
    # FAILS iff it sees a leaked GIT_DIR — so a pass proves the env was stripped.
    leak_detector_smoke = (
        '#!/usr/bin/env bash\n'
        '[ "$1" = "--fast" ] || exit 0\n'
        'if [ -n "${GIT_DIR:-}" ] || [ -n "${GIT_INDEX_FILE:-}" ]; then\n'
        '  echo "LEAKED GIT_DIR=$GIT_DIR GIT_INDEX_FILE=$GIT_INDEX_FILE"; exit 5\n'
        'fi\n'
        'echo "no git env leak"; exit 0\n'
    )
    repo, env = _make_repo(tmp_path, leak_detector_smoke)
    subprocess.run(["sh", str(repo / "scripts" / INSTALLER.name)], cwd=repo, env=env, check=True)
    # commit naturally → git sets GIT_DIR/GIT_INDEX_FILE in the hook env; the gate must strip them.
    out = _commit(repo, env, "git env must be stripped for smoke")
    assert out.returncode == 0, "git env leaked into smoke:\n" + out.stdout + out.stderr
    assert "no git env leak" in (out.stdout + out.stderr)


def test_gate_chains_when_composer_does_not_run_dispatcher(tmp_path):
    # FAIL SAFE: core.hooksPath points at the composer DIR, but that dir's pre-commit does NOT
    # invoke the dispatcher (a bare `exit 0`). The gate must NOT treat it as composer-active —
    # otherwise it skips its own chain and secret-scan is DROPPED. It must chain (run once).
    repo, env = _make_repo(tmp_path, _PASS_SMOKE)
    marker = _install_stub_dispatcher(env)
    composer_dir = Path(env["XDG_CONFIG_HOME"]) / "git" / "hooks"
    composer_dir.mkdir(parents=True, exist_ok=True)
    (composer_dir / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")  # no dispatcher
    (composer_dir / "pre-commit").chmod(0o755)
    _git(repo, "config", "core.hooksPath", str(composer_dir), env=env)
    res = subprocess.run(["sh", str(repo / "scripts" / GATE.name)],
                         cwd=repo, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
    runs = marker.read_text().splitlines() if marker.exists() else []
    assert runs == ["ran:pre-commit"], f"gate dropped the dispatcher (composer doesn't run it): {runs!r}"


def test_gate_bypass_still_chains_dispatcher(tmp_path):
    # SKIP_RIG_SMOKE=1 skips only the SMOKE leg — the dispatcher (secret-scan) must STILL run.
    repo, env = _make_repo(tmp_path, _FAIL_SMOKE)
    marker = _install_stub_dispatcher(env)
    subprocess.run(["sh", str(repo / "scripts" / INSTALLER.name)], cwd=repo, env=env, check=True)
    _git(repo, "add", "-A", env=env)
    out = subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "bypass keeps dispatcher"],
        env={**env, "GIT_EDITOR": "true", "SKIP_RIG_SMOKE": "1"}, capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    runs = marker.read_text().splitlines() if marker.exists() else []
    assert runs == ["ran:pre-commit"], f"bypass dropped the dispatcher: {runs!r}"


def test_installer_refuses_symlinked_hook(tmp_path):
    # A symlinked pre-commit (husky / pre-commit-framework / dotfiles) must NOT be mangled.
    repo, env = _make_repo(tmp_path, _PASS_SMOKE)
    real = repo / "real-hook.sh"
    real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real.chmod(0o755)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.symlink_to(real)
    res = subprocess.run(["sh", str(repo / "scripts" / INSTALLER.name)],
                         cwd=repo, env=env, capture_output=True, text=True)
    assert res.returncode == 3, res.stdout + res.stderr
    assert "SYMLINK" in (res.stdout + res.stderr)
    assert hook.is_symlink(), "installer detached the symlink instead of refusing"
    assert hook.resolve() == real.resolve()


def test_installer_keeps_chaining_when_dispatcher_only_mentioned_in_comment(tmp_path):
    # FAIL CLOSED: a foreign hook that only MENTIONS run-global-hooks in a comment does NOT own
    # the dispatcher → the gate must KEEP chaining it (so secret-scan still runs). The marker
    # must NOT be set, and at commit time the dispatcher must fire exactly once.
    repo, env = _make_repo(tmp_path, _PASS_SMOKE)
    marker = _install_stub_dispatcher(env)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\n# TODO: maybe wire run-global-hooks here someday\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    res = subprocess.run(["sh", str(repo / "scripts" / INSTALLER.name)],
                         cwd=repo, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "RIG_SMOKE_GATE_CHAIN_DISPATCHER=0" not in hook.read_text(encoding="utf-8")
    out = _commit(repo, env, "comment mention keeps chaining")
    assert out.returncode == 0, out.stdout + out.stderr
    runs = marker.read_text().splitlines() if marker.exists() else []
    assert runs == ["ran:pre-commit"], f"dispatcher did not run exactly once: {runs!r}"


def test_installer_prepends_above_no_shebang_exit_hook(tmp_path):
    # A no-shebang hook whose FIRST line is real code (`exit 0`) would skip our gate entirely if
    # we inserted after line 1. We must insert at the TOP so the gate runs before that `exit 0`.
    repo, env = _make_repo(tmp_path, _FAIL_SMOKE)  # failing smoke → if the gate runs, commit blocks
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("exit 0\n", encoding="utf-8")  # no shebang, immediate exit
    hook.chmod(0o755)
    res = subprocess.run(["sh", str(repo / "scripts" / INSTALLER.name)],
                         cwd=repo, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
    body = hook.read_text(encoding="utf-8")
    # our gate block precedes the original `exit 0`
    assert body.index("smoke-precommit-hook.sh") < body.index("exit 0")
    # and at commit time the gate actually fires (and blocks, since the stub smoke fails) — proving
    # it was NOT short-circuited by the leading `exit 0`.
    out = _commit(repo, env, "no-shebang exit-first hook")
    assert out.returncode != 0, "gate was skipped by a leading exit 0 (inserted too low)"
    assert "BLOCKED" in (out.stdout + out.stderr)


def test_installer_refuses_non_shell_foreign_hook(tmp_path):
    # A Python (or other non-shell) hook must NOT be corrupted by inlined shell — refuse it.
    repo, env = _make_repo(tmp_path, _PASS_SMOKE)
    hook = repo / ".git" / "hooks" / "pre-commit"
    original = "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n"
    hook.write_text(original, encoding="utf-8")
    hook.chmod(0o755)
    res = subprocess.run(["sh", str(repo / "scripts" / INSTALLER.name)],
                         cwd=repo, env=env, capture_output=True, text=True)
    assert res.returncode == 3, res.stdout + res.stderr
    assert "non-shell shebang" in (res.stdout + res.stderr)
    assert hook.read_text(encoding="utf-8") == original, "installer mangled the non-shell hook"


def test_installer_worktree_install(tmp_path):
    # A git worktree's .git is a FILE → the installer must resolve the real git-dir and install
    # the hook there (a documented use case: parallel agents work in worktrees). Use the DEFAULT
    # core.hooksPath (unset) — git then resolves each worktree's own git-dir/hooks, the only
    # config under which worktree hooks fire (a RELATIVE core.hooksPath like ".git/hooks" is
    # git-broken for worktrees, independent of this installer).
    repo, env = _make_repo(tmp_path, _PASS_SMOKE)
    _git(repo, "config", "--unset", "core.hooksPath", env=env, check=False)
    # the worktree base needs at least one commit to branch from.
    subprocess.run(["sh", str(repo / "scripts" / INSTALLER.name)], cwd=repo, env=env, check=True)
    assert _commit(repo, env, "base").returncode == 0
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "wt-branch", env=env)
    res = subprocess.run(["sh", str(wt / "scripts" / INSTALLER.name)],
                         cwd=wt, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
    # the hook lands in the worktree's OWN git-dir hooks (not the main repo's).
    git_dir = _git(wt, "rev-parse", "--absolute-git-dir", env=env).stdout.strip()
    hook = Path(git_dir) / "hooks" / "pre-commit"
    assert hook.is_file() and os.access(hook, os.X_OK)
    assert "rig-smoke-precommit" in hook.read_text(encoding="utf-8")
    # and the gate actually FIRES on a commit made from the worktree: a failing stub smoke must
    # block it (asserts the worktree-resolved hook runs, not just that the file landed).
    (wt / "tests" / "smoke.sh").write_text(_FAIL_SMOKE, encoding="utf-8")
    (wt / "tests" / "smoke.sh").chmod(0o755)
    out = _commit(wt, env, "worktree commit should be blocked")
    assert out.returncode != 0, "gate did not fire on a commit from the worktree"
    assert "BLOCKED" in (out.stdout + out.stderr)


# ── smoke.sh --fast / --help / arg-parse (no agent-tools dependency) ─────────────────────────
# These drive the project's real tests/smoke.sh but stay hermetic: --help and the unknown-arg
# path exit before any CLI/catalog work, so they need no agent-tools checkout and touch nothing.

def test_real_smoke_rejects_unknown_arg():
    res = subprocess.run(["bash", str(SMOKE), "--bogus"], capture_output=True, text=True, timeout=30)
    assert res.returncode == 2
    assert "unknown argument" in res.stderr


def test_real_smoke_fast_self_skips_without_agent_tools(tmp_path):
    # HERMETIC (runs in default CI / pytest -q, no opt-in flag): with NO agent-tools checkout
    # reachable, `smoke.sh --fast` must self-skip the catalog/apply legs and exit 0 — so the
    # pre-commit gate never blocks a contributor who lacks one. Point HOME at an empty dir and
    # clear RIG_AGENT_TOOLS_SOURCE so no checkout is found.
    # fully isolated env (HOME/XDG/git-config pinned under tmp) so a developer's global config or
    # a PATH-discoverable agent-tools can't flip this from "self-skip" to a real catalog run.
    env = _isolated_env(tmp_path / "empty-home")
    env.pop("RIG_AGENT_TOOLS_SOURCE", None)
    res = subprocess.run(
        ["bash", str(SMOKE), "--fast"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout + res.stderr
    # the CLI-surface legs still ran …
    assert "rig --help / --version" in out
    # … the apply leg self-skipped (no checkout) and pytest was skipped under --fast …
    assert "no agent-tools checkout found" in out
    assert "skip" in out and "pytest" in out
    assert "smoke OK (--fast)" in out
    # … and it did NOT run the heavy apply or the full pytest.
    assert "installed skills + CI + dispatcher" not in out
    assert "running pytest" not in out


def test_real_smoke_help_prints_only_the_comment_block():
    # --help must print the leading doc comment and STOP at the first non-comment line — it must
    # NOT leak shell implementation lines (the `set -euo pipefail` / `FAST=0` / `for arg` parser).
    res = subprocess.run(["bash", str(SMOKE), "--help"], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "--fast" in out and "PRE-COMMIT subset" in out  # the doc is there
    for leak in ("set -euo pipefail", "FAST=0", "for arg in", "case \"$arg\""):
        assert leak not in out, f"--help leaked an implementation line: {leak!r}"


# ── the real `tests/smoke.sh --fast` subset (OPT-IN e2e — needs a real agent-tools checkout) ──
# Gated behind RIG_SMOKE_FAST_E2E=1 so the default `pytest -q` stays hermetic (the autouse
# HOME-isolation + offline contract): this leg runs the REAL CLI against the REAL agent-tools
# catalog and a real $HOME, which the unit suite must never do. CI opts in (it has the checkout).
# Mirrors the test_tmux_e2e.py / test_cleanroom_e2e.py opt-in convention.

def _agent_tools_source() -> str | None:
    cand = os.environ.get("RIG_AGENT_TOOLS_SOURCE")
    candidates = [cand] if cand else []
    candidates += [str(Path.home() / p) for p in ("xp/agent-tools", "work/agent-tools", "agent-tools")]
    for c in candidates:
        if c and (Path(c) / "skills").is_dir() and (Path(c) / "agent-hooks").is_dir():
            return c
    return None


@pytest.mark.skipif(
    os.environ.get("RIG_SMOKE_FAST_E2E") != "1",
    reason="opt-in real-catalog smoke e2e (set RIG_SMOKE_FAST_E2E=1)",
)
def test_real_smoke_fast_exits_zero_and_skips_heavy_legs():
    src = _agent_tools_source()
    if src is None:
        pytest.skip("no agent-tools checkout (set RIG_AGENT_TOOLS_SOURCE)")
    env = dict(os.environ, RIG_AGENT_TOOLS_SOURCE=src)
    res = subprocess.run(
        ["bash", str(SMOKE), "--fast"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout + res.stderr
    # the cheap REAL-catalog regression legs ran …
    assert "removed slot → 3-part error + exit 4" in out
    assert "clean sample → exit 0" in out
    # … and the heavy legs were skipped: NO skill-install apply, NO tg-ctl apply, NO full pytest.
    assert "skip" in out and "pytest" in out, "fast mode did not skip the pytest leg"
    assert "installed skills + CI + dispatcher" not in out, "fast mode ran the heavy init --yes apply"
    assert "rig status: in sync" not in out, "fast mode ran the post-apply in-sync leg"
    assert "running pytest" not in out, "fast mode ran the full pytest leg"
    assert "smoke OK (--fast)" in out
