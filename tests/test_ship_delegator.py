"""`gh ship` delegator provisioning — the ``ship_delegator`` block.

The DURABLE fix for "gh ship must work in every repo": rig provisions the per-repo
``.claude/scripts/pr-ship.sh`` thin delegator (which the repo-keyed ``gh ship`` alias execs)
into every managed repo, and ignores it in ``.git/info/exclude`` so it never dirties the
worktree (ship refuses a dirty tree). Without this, the delegator existed ONLY in agent-tools
and ``gh ship`` failed everywhere else — papered over by a runtime alias fallback.

Covers: the deterministic delegator content, default-ON + opt-out plan gating, config
validation, the idempotent file write, the ``.git/info/exclude`` ignore reconcile (incl. git
worktrees where ``info/exclude`` lives in the common gitdir), and drift parity (apply and drift
read the SAME ``resolve_ship_delegator`` state, so status never misreports the on-disk state).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from riglib.actions.runner import (
    _do_provision_ship_delegator,
    resolve_ship_delegator,
    ship_delegator_content,
    ship_env_file_content,
    ship_env_file_path,
    run_plan,
)
from riglib.config import ConfigError, validate
from riglib.drift import detect
from riglib.plan import Action, InstallPlan, build


# ── helpers ──────────────────────────────────────────────────────────────────────
def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def _canonical_ship(tmp_path: Path) -> Path:
    src = tmp_path / "agent-tools" / "ci" / "ship" / "ship.sh"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("#!/usr/bin/env bash\necho ship\n", encoding="utf-8")
    return src


def _action(repo: Path, canonical: Path) -> Action:
    return Action(
        kind="provision_ship_delegator",
        category="ship_delegator",
        item="delegator",
        source=repo,
        target=repo,
        options={"canonical_ship": str(canonical)},
    )


def _apply(repo: Path, canonical: Path, on_conflict: str = "backup"):
    return _do_provision_ship_delegator(_action(repo, canonical), on_conflict)


def _plan_with_action(repo: Path, canonical: Path) -> InstallPlan:
    plan = InstallPlan()
    plan.actions.append(_action(repo, canonical))
    return plan


def _delegator_path(repo: Path) -> Path:
    return repo / ".claude" / "scripts" / "pr-ship.sh"


def _exclude_path(repo: Path) -> Path:
    return repo / ".git" / "info" / "exclude"


def _loaded(cfg: dict, repo: Path):
    from riglib.config import LoadedConfig

    validate(cfg)
    return LoadedConfig(data=cfg, repo_root=repo)


def _write_env_file(canonical: Path) -> Path:
    """Write the machine env file for ``canonical`` (under the suite-isolated XDG_CONFIG_HOME)."""
    p = ship_env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(ship_env_file_content(canonical), encoding="utf-8")
    return p


def _script_env(**extra: str) -> dict:
    """A subprocess env with no inherited AGENT_TOOLS_ROOT (the delegator prefers the env var)."""
    env = dict(os.environ)
    env.pop("AGENT_TOOLS_ROOT", None)
    env.update(extra)
    return env


# ── delegator content ──────────────────────────────────────────────────────────────
def test_content_is_executable_bash_that_delegates():
    text = ship_delegator_content()
    assert text.startswith("#!/usr/bin/env bash\n")
    # must reference ci/ship/ship.sh (both the repo-local branch and the AGENT_TOOLS_ROOT fallback)
    assert "ci/ship/ship.sh" in text
    # resolves AGENT_TOOLS_ROOT from the env or the machine-level env file — never a baked path
    assert "AGENT_TOOLS_ROOT" in text
    assert "agent-tools/env" in text
    assert "exec" in text


def test_content_is_a_portable_byte_stable_constant(tmp_path):
    # The fix for rig-cli#108: the rendered delegator is a pure constant — byte-stable across
    # renders and with NO machine-specific absolute path baked in — so a repo that COMMITS the
    # file (agent-tools#151) byte-matches what rig renders and a re-apply never dirties the tree.
    text = ship_delegator_content()
    assert ship_delegator_content() == text  # byte-stable
    assert str(tmp_path) not in text
    assert "/Users/" not in text and "/home/" not in text  # no machine home path
    # the machine root is reached only through the portable XDG env-file location
    # (${HOME:-} so `set -u` survives a sanitized env with an explicit AGENT_TOOLS_ROOT)
    assert "${XDG_CONFIG_HOME:-${HOME:-}/.config}/agent-tools/env" in text
    # the script is valid bash
    import subprocess as _sp

    rc = _sp.run(["bash", "-n"], input=text, capture_output=True, text=True)
    assert rc.returncode == 0, rc.stderr


def test_env_file_shell_quotes_a_dangerous_root(tmp_path):
    # agent_tools_source derives from user-controlled config and the delegator SOURCES the env
    # file, so a root with $(...) / backticks / spaces must be INERT — assigned literally, never
    # executed when `gh ship` runs.
    import subprocess as _sp

    pwned = tmp_path / "pwned"
    evil_root = f"/tmp/$(touch {pwned})/`id`"
    evil = Path(evil_root) / "ci" / "ship" / "ship.sh"
    content = ship_env_file_content(evil)
    # the raw injection must NOT appear unquoted
    assert "AGENT_TOOLS_ROOT=/tmp/$(touch" not in content
    # the ROOT (parent.parent.parent of ship.sh) is present, single-quoted; ship.sh path is not
    assert f"'{evil_root}'" in content
    assert "ci/ship/ship.sh'" not in content
    # sourcing the file yields the LITERAL path and executes nothing
    res = _sp.run(
        ["bash", "-c", 'source /dev/stdin; printf %s "$AGENT_TOOLS_ROOT"'],
        input=content, capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == evil_root
    assert not pwned.exists(), "the injection was EXECUTED while sourcing the env file"


def test_delegator_execs_canonical_ship_via_env_file(tmp_path):
    # END-TO-END through the machine env file: no AGENT_TOOLS_ROOT in the environment, the
    # delegator sources ${XDG_CONFIG_HOME}/agent-tools/env (isolated by conftest) and execs the
    # canonical ship.sh, forwarding args. The canonical writes its args to a file as proof.
    out_marker = tmp_path / "ran.txt"
    canonical = tmp_path / "agent-tools" / "ci" / "ship" / "ship.sh"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(f'#!/usr/bin/env bash\necho "ship-ran $*" > {out_marker}\n', encoding="utf-8")
    canonical.chmod(0o755)
    _write_env_file(canonical)
    script = tmp_path / "pr-ship.sh"
    script.write_text(ship_delegator_content(), encoding="utf-8")
    script.chmod(0o755)
    # run from a NON-git dir so the repo-local branch is skipped and the env-file path is used
    import subprocess as _sp

    rundir = tmp_path / "run"
    rundir.mkdir()
    res = _sp.run([str(script), "42", "--dry-run"], cwd=rundir, capture_output=True, text=True,
                  env=_script_env())
    assert res.returncode == 0, res.stderr
    assert out_marker.read_text(encoding="utf-8").strip() == "ship-ran 42 --dry-run"


def test_delegator_env_var_wins_over_env_file(tmp_path):
    # an explicit AGENT_TOOLS_ROOT in the environment must SHADOW the env file (point a shell at
    # another checkout without re-running rig apply).
    import subprocess as _sp

    file_marker = tmp_path / "file_ran.txt"
    env_marker = tmp_path / "env_ran.txt"
    file_root = tmp_path / "at-file"
    env_root = tmp_path / "at-env"
    for root, marker in ((file_root, file_marker), (env_root, env_marker)):
        ship = root / "ci" / "ship" / "ship.sh"
        ship.parent.mkdir(parents=True, exist_ok=True)
        ship.write_text(f'#!/usr/bin/env bash\necho x > {marker}\n', encoding="utf-8")
        ship.chmod(0o755)
    _write_env_file(file_root / "ci" / "ship" / "ship.sh")
    script = tmp_path / "pr-ship.sh"
    script.write_text(ship_delegator_content(), encoding="utf-8")
    script.chmod(0o755)
    rundir = tmp_path / "run"
    rundir.mkdir()
    res = _sp.run([str(script)], cwd=rundir, capture_output=True, text=True,
                  env=_script_env(AGENT_TOOLS_ROOT=str(env_root)))
    assert res.returncode == 0, res.stderr
    assert env_marker.exists() and not file_marker.exists(), "env var must win over the env file"


def test_delegator_works_in_sanitized_env_without_home(tmp_path):
    # env -i style: no HOME, no XDG_CONFIG_HOME, only an explicit AGENT_TOOLS_ROOT. Under `set -u`
    # a bare $HOME in the env_file expansion would abort with "unbound variable"; ${HOME:-} must
    # keep the explicit root working.
    import subprocess as _sp

    out_marker = tmp_path / "ran.txt"
    root = tmp_path / "agent-tools"
    ship = root / "ci" / "ship" / "ship.sh"
    ship.parent.mkdir(parents=True, exist_ok=True)
    ship.write_text(f'#!/usr/bin/env bash\necho ok > {out_marker}\n', encoding="utf-8")
    ship.chmod(0o755)
    script = tmp_path / "pr-ship.sh"
    script.write_text(ship_delegator_content(), encoding="utf-8")
    script.chmod(0o755)
    rundir = tmp_path / "run"
    rundir.mkdir()
    res = _sp.run(
        [str(script)], cwd=rundir, capture_output=True, text=True,
        env={"PATH": os.environ["PATH"], "AGENT_TOOLS_ROOT": str(root)},  # no HOME, no XDG
    )
    assert res.returncode == 0, res.stderr
    assert out_marker.read_text(encoding="utf-8").strip() == "ok"


def test_delegator_exits_127_with_diagnostic_when_unresolvable(tmp_path):
    # no repo-local ship, no AGENT_TOOLS_ROOT, no env file → a clear diagnostic + exit 127.
    import subprocess as _sp

    script = tmp_path / "pr-ship.sh"
    script.write_text(ship_delegator_content(), encoding="utf-8")
    script.chmod(0o755)
    rundir = tmp_path / "run"
    rundir.mkdir()
    res = _sp.run([str(script)], cwd=rundir, capture_output=True, text=True, env=_script_env())
    assert res.returncode == 127
    assert "AGENT_TOOLS_ROOT=<unset>" in res.stderr
    assert "rig apply" in res.stderr


def test_delegator_prefers_repo_local_ship_over_canonical(tmp_path):
    # the PRIMARY branch: a repo-local ci/ship/ship.sh must win over the canonical (this is how
    # agent-tools self-hosts). Prove it execs the repo-local, NOT the canonical, even with
    # AGENT_TOOLS_ROOT set AND the env file present.
    import subprocess as _sp

    canonical_marker = tmp_path / "canon_ran.txt"
    local_marker = tmp_path / "local_ran.txt"
    canonical = tmp_path / "agent-tools" / "ci" / "ship" / "ship.sh"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(f'#!/usr/bin/env bash\necho x > {canonical_marker}\n', encoding="utf-8")
    canonical.chmod(0o755)
    _write_env_file(canonical)
    # a git repo that CARRIES its own ci/ship/ship.sh
    repo = _git_repo(tmp_path / "repo")
    repo_ship = repo / "ci" / "ship" / "ship.sh"
    repo_ship.parent.mkdir(parents=True, exist_ok=True)
    repo_ship.write_text(f'#!/usr/bin/env bash\necho local > {local_marker}\n', encoding="utf-8")
    repo_ship.chmod(0o755)
    script = repo / "pr-ship.sh"
    script.write_text(ship_delegator_content(), encoding="utf-8")
    script.chmod(0o755)
    # run from INSIDE the repo → git rev-parse finds the toplevel → repo-local ship wins
    res = _sp.run([str(script)], cwd=repo, capture_output=True, text=True,
                  env=_script_env(AGENT_TOOLS_ROOT=str(tmp_path / "agent-tools")))
    assert res.returncode == 0, res.stderr
    assert local_marker.exists() and not canonical_marker.exists(), "repo-local ship must shadow canonical"


def test_delegator_handles_canonical_path_with_spaces(tmp_path):
    # the common real-world case: agent_tools_source under a dir with a space. shlex.quote in the
    # env file handles it; prove the round-trip (path → env file → source → exec) runs the canonical.
    out_marker = tmp_path / "ran.txt"
    canonical = tmp_path / "agent tools dir" / "ci" / "ship" / "ship.sh"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(f'#!/usr/bin/env bash\necho ok > {out_marker}\n', encoding="utf-8")
    canonical.chmod(0o755)
    _write_env_file(canonical)
    script = tmp_path / "pr-ship.sh"
    script.write_text(ship_delegator_content(), encoding="utf-8")
    script.chmod(0o755)
    import subprocess as _sp

    rundir = tmp_path / "run"
    rundir.mkdir()
    res = _sp.run([str(script)], cwd=rundir, capture_output=True, text=True, env=_script_env())
    assert res.returncode == 0, res.stderr
    assert out_marker.read_text(encoding="utf-8").strip() == "ok"


# ── the machine env file: written on apply, idempotent, stale-repaired ──────────────
def test_apply_writes_env_file_idempotently(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    p = ship_env_file_path()
    assert p.is_file()
    assert p.read_text(encoding="utf-8") == ship_env_file_content(canonical)
    # second apply: no rewrite (mtime unchanged), action reads as a no-op
    m = p.stat().st_mtime_ns
    res2 = _apply(repo, canonical)
    assert res2.status == "skipped"
    assert p.stat().st_mtime_ns == m
    # a STALE rig-owned env file (header present, wrong root) is repaired in place, and the
    # action reports a change (the headerless user-file branches are covered separately below)
    from riglib.actions.runner import _SHIP_ENV_HEADER

    p.write_text(f"{_SHIP_ENV_HEADER}: machine-level pointer.\nAGENT_TOOLS_ROOT='/stale/root'\n",
                 encoding="utf-8")
    res3 = _apply(repo, canonical)
    assert res3.status == "updated"
    assert p.read_text(encoding="utf-8") == ship_env_file_content(canonical)
    assert list(p.parent.glob("env.rig-bak-*")) == []  # rig-owned repair keeps no backup


def test_user_env_file_without_rig_header_is_backed_up(tmp_path):
    # a pre-existing agent-tools/env rig did NOT write (no ownership header) is USER content:
    # replace it only WITH a backup, never a silent clobber. A rig-owned stale file (header
    # present) is rewritten in place with no backup (covered above).
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    p = ship_env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# my hand-rolled env\nAGENT_TOOLS_ROOT=/my/own\nEXTRA=1\n", encoding="utf-8")
    _apply(repo, canonical)
    assert p.read_text(encoding="utf-8") == ship_env_file_content(canonical)
    backups = list(p.parent.glob("env.rig-bak-*"))
    assert len(backups) == 1
    assert "hand-rolled" in backups[0].read_text(encoding="utf-8")


def test_user_env_file_honors_skip_and_overwrite(tmp_path):
    # a USER-owned env file (no rig header) follows on_conflict: `skip` leaves it untouched
    # (and the action is still a success), `overwrite` replaces it with NO backup.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    p = ship_env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    user_content = "# my hand-rolled env\nAGENT_TOOLS_ROOT=/my/own\n"
    p.write_text(user_content, encoding="utf-8")
    res = _apply(repo, canonical, on_conflict="skip")
    # the delegator itself WAS written, but the unresolved user env file must not vanish into a
    # "created" success — status keeps flagging env drift, so the action reports "skipped"
    # (aligned with the delegator-already-ok branch), with the write named in the detail.
    assert res.status == "skipped"
    assert "left user env file" in res.detail  # the unreconciled skip is surfaced, never silent
    assert _delegator_path(repo).is_file()  # the delegator write itself still happened
    assert p.read_text(encoding="utf-8") == user_content  # left as-is
    assert list(p.parent.glob("env.rig-bak-*")) == []
    # and the skip is NOT clean: status keeps flagging the stale env file
    report = detect(_plan_with_action(repo, canonical))
    env_items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert env_items and env_items[0].direction == "modified"
    res = _apply(repo, canonical, on_conflict="overwrite")
    assert res.status != "error"
    assert p.read_text(encoding="utf-8") == ship_env_file_content(canonical)
    assert list(p.parent.glob("env.rig-bak-*")) == []  # overwrite keeps no backup


def test_drift_reports_directory_at_env_path_as_modified(tmp_path):
    # a DIRECTORY at the env-file path makes apply ERROR, so drift must say `modified` with the
    # real failure — not a misleading "missing (apply rewrites it)" (status/apply parity).
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    p = ship_env_file_path()
    p.unlink()
    p.mkdir()
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert items and items[0].direction == "modified"
    assert "non-file" in items[0].detail
    res = _apply(repo, canonical)
    assert res.status == "error"


def test_drift_reports_file_at_env_parent_as_modified(tmp_path):
    # a FILE where the PARENT dir should be (~/.config/agent-tools is a file) blocks apply's
    # mkdir(parents=True) — apply ERRORS, it does NOT "rewrite" anything. drift must classify
    # this `modified` naming the blocker, not a misleading "missing (apply rewrites it)"
    # (status/apply parity — the codex P2 on PR #112).
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    p = ship_env_file_path()
    p.parent.parent.mkdir(parents=True, exist_ok=True)
    p.parent.write_text("not a directory\n", encoding="utf-8")  # agent-tools/ as a file
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert items and items[0].direction == "modified"
    assert "non-directory" in items[0].detail
    res = _apply(repo, canonical)
    assert res.status == "error"  # parity: apply really does error, not rewrite


def test_non_git_status_env_check_still_runs_for_dropped_action(tmp_path):
    # `rig status` outside git drops repo-scoped actions before drift detection — but apply does
    # NOT drop them and still reconciles the MACHINE env file there. The dropped ship_delegator
    # action's GLOBAL ship_env check must therefore still run (the codex P2 on PR #112): a
    # missing/stale env file must surface even from a non-git cwd.
    from riglib.drift import DriftReport, check_ship_env_for_dropped_repo_action

    nongit = tmp_path / "plain-dir"
    nongit.mkdir()
    canonical = _canonical_ship(tmp_path)
    report = DriftReport()
    check_ship_env_for_dropped_repo_action(_action(nongit, canonical), report)
    items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert items and items[0].direction == "missing"
    # stale content is flagged too
    p = _write_env_file(canonical)
    p.write_text("AGENT_TOOLS_ROOT='/somewhere/else'\n", encoding="utf-8")
    report2 = DriftReport()
    check_ship_env_for_dropped_repo_action(_action(nongit, canonical), report2)
    items2 = [i for i in report2.items if i.category == "ship_env" and i.item == "env-file"]
    assert items2 and items2[0].direction == "modified"
    # an up-to-date env file is clean
    _write_env_file(canonical)
    report3 = DriftReport()
    check_ship_env_for_dropped_repo_action(_action(nongit, canonical), report3)
    assert not [i for i in report3.items if i.category == "ship_env"]
    # a malformed action (no canonical_ship) fails CLOSED — apply errors on it, so status must
    # flag it too, under the GLOBAL ship_env category (renderable outside git)
    bad = _action(nongit, canonical)
    bad.options = {"canonical_ship": ""}
    report4 = DriftReport()
    check_ship_env_for_dropped_repo_action(bad, report4)
    bad_items = [i for i in report4.items if i.category == "ship_env"]
    assert bad_items and bad_items[0].direction == "modified"
    assert "malformed plan" in bad_items[0].detail


def test_dangling_symlink_at_env_path_is_refused_not_replaced(tmp_path):
    # a DANGLING symlink at the env path is a non-file: `exists()` is False (it follows the
    # link), so a naive check would classify it "absent" and clobber it outside the conflict
    # policy. Both apply (refuses, errors) and drift (`modified`, never `missing`) must use
    # lexists and agree.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    p = ship_env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.symlink_to(tmp_path / "nowhere")
    res = _apply(repo, canonical)
    assert res.status == "error"
    assert "non-file" in res.detail or "env file" in res.detail
    assert p.is_symlink()  # NOT silently replaced
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert items and items[0].direction == "modified"


def test_symlink_to_regular_file_at_env_path_is_refused(tmp_path):
    # even a symlink RESOLVING to a regular file is refused: the rig-owned rewrite goes through
    # os.replace, which would swap the SYMLINK for a real file — silently breaking a centrally
    # managed (dotfiles-repo) symlink instead of updating its target. apply refuses; drift agrees.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    real = tmp_path / "real-env"
    real.write_text("AGENT_TOOLS_ROOT='/somewhere'\n", encoding="utf-8")
    p = ship_env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.symlink_to(real)
    res = _apply(repo, canonical)
    assert res.status == "error"
    assert p.is_symlink()  # the symlink survives
    assert real.read_text(encoding="utf-8") == "AGENT_TOOLS_ROOT='/somewhere'\n"  # target untouched
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert items and items[0].direction == "modified"


def test_rig_owned_stale_env_file_rewritten_in_place_without_backup(tmp_path):
    # a differing env file that CARRIES the rig ownership header is rig-OWNED: rewritten in
    # place, no *.rig-bak-* backup, regardless of on_conflict (the authoritative value is
    # agent_tools_source in config) — unlike a user file (no header), which gets the conflict
    # policy. The distinct branch must be pinned directly, not only via the user-file tests.
    from riglib.actions.runner import _SHIP_ENV_HEADER

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    p = ship_env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{_SHIP_ENV_HEADER}: machine-level pointer.\nAGENT_TOOLS_ROOT='/stale/root'\n",
                 encoding="utf-8")
    res = _apply(repo, canonical, on_conflict="skip")  # even `skip` rewrites a rig-owned file
    assert res.status in ("created", "updated")
    assert p.read_text(encoding="utf-8") == ship_env_file_content(canonical)
    assert list(p.parent.glob("env.rig-bak-*")) == []


def test_selfhosting_repo_skips_env_file_drift(tmp_path):
    # a repo carrying its own ci/ship/ship.sh never reads the env file (repo-local wins), and
    # apply degrades an unwritable env path to non-fatal there — so drift must NOT flag a
    # missing env file for it (status/apply parity: no eternal drift apply never treats as
    # fatal). A plain repo still gets the check.
    selfhost = _git_repo(tmp_path / "selfhost")
    ship = selfhost / "ci" / "ship" / "ship.sh"
    ship.parent.mkdir(parents=True, exist_ok=True)
    ship.write_text("#!/usr/bin/env bash\necho local\n", encoding="utf-8")
    canonical = _canonical_ship(tmp_path)
    assert not ship_env_file_path().exists()
    report = detect(_plan_with_action(selfhost, canonical))
    assert not [i for i in report.items if i.category == "ship_env"]
    plain = _git_repo(tmp_path / "plain")
    report2 = detect(_plan_with_action(plain, canonical))
    env_items = [i for i in report2.items if i.category == "ship_env"]
    assert env_items and env_items[0].direction == "missing"


def test_old_rig_delegator_upgrade_rewrites_in_place_no_backup_clean_tree(tmp_path):
    # the 0.8→0.9 upgrade scenario: EVERY managed repo carries the previous rig-rendered
    # delegator (rig provenance header, baked machine path — different bytes). The first apply
    # after the upgrade must rewrite it IN PLACE, with no .rig-bak-* sibling and a clean
    # `git status` — a backup would dirty the worktree in every repo at once and `gh ship`
    # (which refuses a dirty tree) would be broken by its own provisioning.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    old = (
        "#!/usr/bin/env bash\n"
        "# Provisioned by rig (ship_delegator). The global `gh ship` alias runs\n"
        "# <repo>/.claude/scripts/pr-ship.sh.\n"
        "set -euo pipefail\n"
        "_rig_default=/some/old/machine/path/agent-tools\n"
        'AGENT_TOOLS_ROOT="${AGENT_TOOLS_ROOT:-$_rig_default}"\n'
        'exec "$AGENT_TOOLS_ROOT/ci/ship/ship.sh" "$@"\n'
    )
    deleg.write_text(old, encoding="utf-8")
    deleg.chmod(0o755)
    res = _apply(repo, canonical)  # default on_conflict=backup — must NOT back up rig's own file
    assert res.status in ("created", "updated")
    assert deleg.read_text(encoding="utf-8") == ship_delegator_content()
    assert list(deleg.parent.glob("*.rig-bak-*")) == []
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", ".claude/scripts/"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    assert status.stdout.strip() == "", f"upgrade dirtied the worktree: {status.stdout!r}"


def test_old_rig_delegator_is_rewritten_even_under_skip_policy(tmp_path):
    # on_conflict protects USER content; a rig-headered delegator is rig's own prior output.
    # Even `skip` must not leave the stale generation in place (the upgrade would silently
    # never land in a defaults.on_conflict: skip setup).
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text(
        "#!/usr/bin/env bash\n# Provisioned by rig (ship_delegator). old generation\nexit 1\n",
        encoding="utf-8",
    )
    res = _apply(repo, canonical, on_conflict="skip")
    assert res.status in ("created", "updated")
    assert deleg.read_text(encoding="utf-8") == ship_delegator_content()
    assert list(deleg.parent.glob("*.rig-bak-*")) == []


def test_non_utf8_env_file_is_reported_not_crashed(tmp_path):
    # UnicodeDecodeError is a ValueError, not an OSError: one non-UTF-8 byte in the env file
    # must yield a readable apply error + a drift item, never a traceback out of detect().
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    p = ship_env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"AGENT_TOOLS_ROOT='/caf\xe9'\n")  # latin-1 byte, invalid UTF-8
    res = _apply(repo, canonical)
    assert res.status == "error"
    assert "could not read env file" in res.detail
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert items and items[0].direction == "modified"
    assert "unreadable" in items[0].detail


def test_exclude_block_covers_rig_bak_siblings(tmp_path):
    # a USER-owned (headerless) delegator DOES get displaced to a .rig-bak-* sibling under the
    # default policy — the exclude block must cover that sibling too, or the backup itself
    # dirties the worktree and breaks `gh ship`.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text("#!/usr/bin/env bash\necho my hand-rolled shim\n", encoding="utf-8")
    res = _apply(repo, canonical)
    assert res.status == "backed_up" and res.backup is not None
    assert res.backup.name.startswith("pr-ship.sh.rig-bak-")
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", ".claude/scripts/"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    assert status.stdout.strip() == "", f"the .rig-bak backup dirties the worktree: {status.stdout!r}"


def test_delegator_refuses_symlinked_env_file(tmp_path):
    # apply/drift REFUSE a symlink at the env path; the delegator must draw the same line at
    # runtime — never source (execute) a symlink target rig itself refuses to manage.
    import shlex
    import subprocess as _sp

    out_marker = tmp_path / "ran.txt"
    canonical = _canonical_ship(tmp_path)
    real_env = tmp_path / "real-env"
    real_env.write_text(
        f"AGENT_TOOLS_ROOT={shlex.quote(str(tmp_path / 'agent-tools'))}\n"
        f"echo pwned > {shlex.quote(str(out_marker))}\n",
        encoding="utf-8",
    )
    env_path = ship_env_file_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.symlink_to(real_env)
    script = tmp_path / "pr-ship.sh"
    script.write_text(ship_delegator_content(), encoding="utf-8")
    script.chmod(0o755)
    rundir = tmp_path / "run"  # non-git dir → the repo-local branch is skipped
    rundir.mkdir()
    res = _sp.run([str(script)], cwd=rundir, capture_output=True, text=True, env=_script_env())
    assert res.returncode == 127, (res.returncode, res.stdout, res.stderr)
    assert not out_marker.exists(), "the symlinked env file was sourced"


def test_corrupt_git_dir_with_local_ship_is_not_selfhosting(tmp_path):
    # `repo_self_hosts_ship` must run the SAME probe as the delegator's runtime
    # (`git rev-parse --show-toplevel`), not approximate with `.git`-exists: a corrupt/fake
    # .git passes the filesystem check but fails the real probe — the runtime would skip the
    # repo-local branch and need the env file, so apply must write it.
    from riglib.actions.runner import repo_self_hosts_ship

    broken = tmp_path / "broken"
    (broken / "ci" / "ship").mkdir(parents=True)
    (broken / "ci" / "ship" / "ship.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (broken / ".git").write_text("not a gitdir pointer\n", encoding="utf-8")  # fake/corrupt
    assert not repo_self_hosts_ship(broken)
    real = _git_repo(tmp_path / "real")
    (real / "ci" / "ship").mkdir(parents=True)
    (real / "ci" / "ship" / "ship.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    assert repo_self_hosts_ship(real)


def test_non_git_dir_with_local_ship_still_gets_the_env_file(tmp_path):
    # a PLAIN (non-git) dir carrying a stray ci/ship/ship.sh is NOT self-hosting: the delegator's
    # repo-local branch resolves via `git rev-parse`, which fails there, so at runtime the env
    # file IS needed. apply must write it and drift must check it — the self-hosting skip keys on
    # the RUNTIME contract (git repo + script), not the filesystem alone (a pre-fix regression:
    # the old baked default covered this dir; the env file must now do that job).
    plain = tmp_path / "plain"
    (plain / "ci" / "ship").mkdir(parents=True)
    (plain / "ci" / "ship" / "ship.sh").write_text("#!/usr/bin/env bash\necho local\n",
                                                   encoding="utf-8")
    canonical = _canonical_ship(tmp_path)
    report = detect(_plan_with_action(plain, canonical))
    env_items = [i for i in report.items if i.category == "ship_env"]
    assert env_items and env_items[0].direction == "missing"
    res = _apply(plain, canonical)
    assert res.status != "error"
    p = ship_env_file_path()
    assert p.is_file() and p.read_text(encoding="utf-8") == ship_env_file_content(canonical)


def test_ship_env_is_intentionally_absent_from_config_schema():
    # ship_env is a STATUS-ONLY area (like `ship` and `ship_delegator`, which are also absent
    # from the wizard/config-web registry): it has no config options of its own — the machine
    # env file is derived from the ship_delegator action's canonical_ship. Pin the intent so a
    # future registry-parity sweep doesn't "fix" it into the wizard.
    from riglib.schema import AREAS as SCHEMA_AREAS

    assert "ship_env" not in {a.category for a in SCHEMA_AREAS}


def test_apply_only_ship_env_aliases_to_the_owning_action():
    # `ship_env` is a drift/status-only category; the action that repairs it is ship_delegator.
    # `rig apply --only ship_env` must therefore scope to the ship_delegator action, never a
    # silent no-op that can't fix the drift status just named.
    from riglib.cli import _scope_categories

    assert "ship_delegator" in _scope_categories("ship_env")
    assert _scope_categories("skills,ci") == {"skills", "ci"}


def test_divergent_roots_last_apply_wins_and_other_repo_drifts(tmp_path):
    # INVARIANT: one agent-tools checkout per machine. Two repos declaring DIVERGENT roots is an
    # ambiguous setup — each apply points the ONE machine env file at its own root (last apply
    # wins) and status in the other repo honestly reports env-file drift, never silent corruption.
    repo_a = _git_repo(tmp_path / "repo-a")
    repo_b = _git_repo(tmp_path / "repo-b")
    canonical_a = _canonical_ship(tmp_path)  # tmp/agent-tools
    other = tmp_path / "other-agent-tools" / "ci" / "ship" / "ship.sh"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("#!/usr/bin/env bash\necho other\n", encoding="utf-8")
    _apply(repo_a, canonical_a)
    _apply(repo_b, other)  # last apply wins the machine pointer
    assert ship_env_file_path().read_text(encoding="utf-8") == ship_env_file_content(other)
    # repo A's plan now sees env-file drift (stale for ITS declared root) — the honest signal
    report = detect(_plan_with_action(repo_a, canonical_a))
    env_items = [i for i in report.items if i.category == "ship_env" and i.item == "env-file"]
    assert env_items and env_items[0].direction == "modified"
    # repo B's plan reads clean
    report_b = detect(_plan_with_action(repo_b, other))
    assert not [i for i in report_b.items if i.category in ("ship_delegator", "ship_env")]


def test_apply_errors_when_env_file_unwritable(tmp_path, monkeypatch):
    # if the machine env file cannot be written, the delegator cannot resolve the checkout on a
    # clean machine → the action must ERROR, not report a misleading success.
    import riglib.actions.runner as runner

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    monkeypatch.setattr(
        runner, "_reconcile_ship_env_file",
        lambda c, oc: (False, "could not write env file: boom", "unchanged"),
    )
    res = _apply(repo, canonical)
    assert res.status == "error"
    assert "env file" in res.detail


def test_selfhosting_repo_apply_never_touches_the_env_file(tmp_path, monkeypatch):
    # a repo that CARRIES its own ci/ship/ship.sh (agent-tools self-hosts) never reads the env
    # file — the delegator's repo-local branch wins first — so apply must not reconcile it AT
    # ALL there (status skips the check for such a repo; parity cuts both ways: no rewrite, no
    # backup, no error on an unwritable $XDG_CONFIG_HOME, nothing status never flagged).
    import riglib.actions.runner as runner
    from riglib.actions.runner import _SHIP_ENV_HEADER

    repo = _git_repo(tmp_path / "repo")
    repo_ship = repo / "ci" / "ship" / "ship.sh"
    repo_ship.parent.mkdir(parents=True, exist_ok=True)
    repo_ship.write_text("#!/usr/bin/env bash\necho local\n", encoding="utf-8")
    canonical = _canonical_ship(tmp_path)
    # a stale RIG-OWNED env file that an ordinary repo's apply would rewrite in place
    p = ship_env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    stale = f"{_SHIP_ENV_HEADER}: machine-level pointer.\nAGENT_TOOLS_ROOT='/stale/root'\n"
    p.write_text(stale, encoding="utf-8")
    # drift agrees there is nothing to do for this repo…
    report = detect(_plan_with_action(repo, canonical))
    assert not [i for i in report.items if i.category == "ship_env"]
    # …and apply indeed does nothing to the env file (no rewrite, no .rig-bak)
    res = _apply(repo, canonical)
    assert res.status != "error"
    assert _delegator_path(repo).is_file()
    assert p.read_text(encoding="utf-8") == stale
    assert list(p.parent.glob("env.rig-bak-*")) == []
    # even an UNWRITABLE env path can't fail apply for this repo — the reconcile is never called
    monkeypatch.setattr(
        runner, "_reconcile_ship_env_file",
        lambda c, oc: (_ for _ in ()).throw(AssertionError("env reconcile must not run")),
    )
    res2 = _apply(repo, canonical)
    assert res2.status == "skipped"


def test_env_file_path_mirrors_bash_reader_exactly(monkeypatch, tmp_path):
    # the python WRITER and the bash READER (`${XDG_CONFIG_HOME:-${HOME:-}/.config}`) must agree
    # expansion-for-expansion, or apply writes one file and the delegator reads another (exit
    # 127 in a sanitized env). XDG set → XDG wins; else HOME; else the same degenerate
    # `/.config` bash expands to — never os.path.expanduser's passwd-database home.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert ship_env_file_path() == tmp_path / "xdg" / "agent-tools" / "env"
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert ship_env_file_path() == tmp_path / "home" / ".config" / "agent-tools" / "env"
    monkeypatch.delenv("HOME", raising=False)
    assert ship_env_file_path() == Path("/.config/agent-tools/env")


def test_ship_env_drift_is_classified_global_layer():
    # the machine env file is a MACHINE artifact: `rig status` must render its drift under the
    # GLOBAL heading (not blame the repo), via its own GLOBAL area — whose configured-ness keys
    # off the repo-scoped ship_delegator action that writes the file (configured_by), so a
    # configured ship gate never renders the env area as a false "not configured".
    from riglib.areas import AREAS, area_matches_action, area_matches_drift
    from riglib.layers import GLOBAL, REPO, layer_for_category

    assert layer_for_category("ship_env") == GLOBAL
    assert layer_for_category("ship_delegator") == REPO
    area = next(a for a in AREAS if a.key == "ship_env")
    assert area.layer == GLOBAL
    assert area_matches_drift(area, "ship_env", "env-file", "modified")
    assert area_matches_drift(area, "ship_env", "env-file", "missing")
    # the ship_delegator ACTION marks the env area configured…
    assert area_matches_action(area, "ship_delegator", {})
    # …but the delegator's own drift stays out of the env area (and vice versa)
    assert not area_matches_drift(area, "ship_delegator", "delegator", "modified")
    deleg_area = next(a for a in AREAS if a.key == "ship_delegator")
    assert not area_matches_drift(deleg_area, "ship_env", "env-file", "modified")


def test_committed_delegator_stays_byte_clean_across_applies(tmp_path):
    # the agent-tools#151 scenario (rig-cli#108): the repo COMMITS the portable delegator. Apply
    # must byte-match it → the file is never rewritten, no .rig-bak appears, and git status for
    # .claude/scripts/ stays EMPTY across repeated applies.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text(ship_delegator_content(), encoding="utf-8")
    deleg.chmod(0o755)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "commit delegator"],
        cwd=repo, check=True,
    )
    before = deleg.stat().st_mtime_ns
    _apply(repo, canonical)  # first apply may add the exclude entry + env file
    res2 = _apply(repo, canonical)
    assert res2.status == "skipped"
    assert deleg.stat().st_mtime_ns == before, "a committed, matching delegator was rewritten"
    assert list(deleg.parent.glob("*.rig-bak-*")) == []
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", ".claude/scripts/"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    assert status.stdout.strip() == "", f"committed delegator drifted: {status.stdout!r}"


def test_drift_reports_missing_and_stale_env_file(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    p = ship_env_file_path()
    # stale root → modified
    p.write_text("AGENT_TOOLS_ROOT='/stale/root'\n", encoding="utf-8")
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_env"]
    assert [(i.item, i.direction) for i in items] == [("env-file", "modified")]
    assert not [i for i in report.items if i.category == "ship_delegator"]
    # absent → missing
    p.unlink()
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_env"]
    assert [(i.item, i.direction) for i in items] == [("env-file", "missing")]


# ── create / idempotency ───────────────────────────────────────────────────────────
def test_create_writes_delegator_and_ignores_it(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    assert resolve_ship_delegator(repo).state == "create"

    res = _apply(repo, canonical)
    assert res.status == "created"

    deleg = _delegator_path(repo)
    assert deleg.is_file()
    assert deleg.read_text(encoding="utf-8") == ship_delegator_content()
    # executable bit set
    assert deleg.stat().st_mode & 0o111
    # ignored in .git/info/exclude → never dirties the worktree
    excl = _exclude_path(repo).read_text(encoding="utf-8")
    assert ".claude/scripts/pr-ship.sh" in excl


def test_provisioned_file_does_not_dirty_the_worktree(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    # a committed file so the tree starts clean
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    )
    # the provisioned delegator must NOT show up as untracked/modified
    assert status.stdout.strip() == "", f"worktree dirtied: {status.stdout!r}"


def test_idempotent_second_apply_skips(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    assert _apply(repo, canonical).status == "created"
    second = _apply(repo, canonical)
    assert resolve_ship_delegator(repo).state == "ok"
    assert second.status == "skipped"


def test_stale_delegator_is_updated(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text("#!/usr/bin/env bash\n# stale\n", encoding="utf-8")
    assert resolve_ship_delegator(repo).state == "update"
    res = _apply(repo, canonical)
    assert res.status in ("updated", "backed_up")
    assert deleg.read_text(encoding="utf-8") == ship_delegator_content()


# ── CRLF / line-ending agreement between drift and reconcile ────────────────────────
def test_crlf_exclude_file_drift_and_reconcile_agree(tmp_path):
    # a .git/info/exclude with CRLF endings must not make drift ("ok") and reconcile ("rewrite")
    # disagree: both read RAW, so once provisioned a re-apply is a no-op and status stays clean.
    from riglib.actions.runner import ship_delegator_exclude_block_text

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    excl = _exclude_path(repo)
    excl.parent.mkdir(parents=True, exist_ok=True)
    # seed a CRLF user line, then provision
    with excl.open("w", encoding="utf-8", newline="") as fh:
        fh.write("*.log\r\n")
    _apply(repo, canonical)
    # provisioned: now in sync; a second apply is a true no-op (no rewrite churn)
    assert resolve_ship_delegator(repo).state == "ok"
    before = excl.read_bytes()
    res2 = _apply(repo, canonical)
    assert res2.status == "skipped"
    assert excl.read_bytes() == before  # byte-identical → drift & reconcile agreed
    # the rig block itself is present and the user CRLF line survived verbatim
    assert b"*.log\r\n" in excl.read_bytes()
    assert ship_delegator_exclude_block_text() in excl.read_text(encoding="utf-8")


def test_whitespace_only_exclude_content_is_preserved(tmp_path):
    # a whitespace-only exclude file must keep its blank lines (verbatim) when the block is appended.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    excl = _exclude_path(repo)
    excl.parent.mkdir(parents=True, exist_ok=True)
    excl.write_text("\n\n\n", encoding="utf-8")
    _apply(repo, canonical)
    after = excl.read_text(encoding="utf-8")
    assert after.startswith("\n\n\n")  # pre-existing blanks preserved
    assert ".claude/scripts/pr-ship.sh" in after


# ── non-git directory: file written, exclude step skipped, drift reports the file only ─
def test_non_git_dir_writes_file_skips_exclude(tmp_path):
    plain = tmp_path / "plain"  # NOT a git repo
    plain.mkdir()
    canonical = _canonical_ship(tmp_path)
    # drift: the missing delegator file (no ignore concept without git) — plus the not-yet-written
    # machine env file, which is legitimate pre-apply drift; check the delegator item specifically.
    report = detect(_plan_with_action(plain, canonical))
    items = [i for i in report.items if i.category == "ship_delegator" and i.item == "delegator"]
    assert len(items) == 1 and items[0].direction == "missing"
    # apply writes the file; no exclude (no git), reported as a non-error
    res = _apply(plain, canonical)
    assert res.status in ("created", "updated")
    assert _delegator_path(plain).is_file()
    assert "no git repo" in res.detail
    # now in sync
    assert resolve_ship_delegator(plain).state == "ok"


def test_missing_canonical_ship_option_errors(tmp_path):
    # a malformed action with no canonical_ship must ERROR, not write a broken `canonical=` delegator.
    repo = _git_repo(tmp_path / "repo")
    action = Action(
        kind="provision_ship_delegator", category="ship_delegator", item="delegator",
        source=repo, target=repo, options={},  # no canonical_ship
    )
    res = _do_provision_ship_delegator(action, "backup")
    assert res.status == "error" and "canonical_ship" in res.detail
    assert not _delegator_path(repo).exists()


# ── git worktree: info/exclude lives in the COMMON gitdir ───────────────────────────
def test_ignore_resolves_through_git_worktree(tmp_path):
    main = _git_repo(tmp_path / "main")
    (main / "README.md").write_text("# m\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=main, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=main,
        check=True,
    )
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(wt), "-b", "feat"], cwd=main, check=True
    )
    canonical = _canonical_ship(tmp_path)
    res = _apply(wt, canonical)
    assert res.status == "created"
    assert _delegator_path(wt).is_file()
    # NON-circular proof: git itself must HONOR the written entry — ask git whether it ignores the
    # delegator (check-ignore exits 0 + names the rule's source file only when an exclude matched).
    ci = subprocess.run(
        ["git", "-C", str(wt), "check-ignore", "-v", ".claude/scripts/pr-ship.sh"],
        capture_output=True, text=True,
    )
    assert ci.returncode == 0, f"git does not ignore the delegator in the worktree: {ci.stderr}"
    assert "info/exclude" in ci.stdout, ci.stdout
    # and the worktree stays clean (the user-facing invariant)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True, check=True
    )
    assert status.stdout.strip() == "", f"worktree dirtied: {status.stdout!r}"


# ── plan gating: default ON + opt-out ───────────────────────────────────────────────
def test_plan_includes_delegator_by_default(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {"version": 1, "agent_tools_source": str(fake_agent_tools)}
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_loaded(cfg, repo), cat, project_type="cli")
    acts = [a for a in plan.actions if a.kind == "provision_ship_delegator"]
    assert len(acts) == 1
    assert acts[0].options.get("canonical_ship", "").endswith("ci/ship/ship.sh")


def test_plan_opt_out(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {
        "version": 1,
        "agent_tools_source": str(fake_agent_tools),
        "ship_delegator": {"enabled": False},
    }
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_loaded(cfg, repo), cat, project_type="cli")
    assert not [a for a in plan.actions if a.kind == "provision_ship_delegator"]


def test_plan_skips_when_no_canonical_ship_in_checkout(fake_agent_tools, tmp_path):
    import shutil

    from riglib.catalog import Catalog

    # a checkout lacking ci/ship/ship.sh → no delegator action, with a note (fail-closed, no
    # broken delegator pointing at a non-existent canonical script). Work on a COPY of the fixture
    # tree so the unlink never mutates the shared fixture (it is function-scoped today, but a copy
    # keeps this test self-contained regardless of any future scope change).
    src = tmp_path / "at-copy"
    shutil.copytree(fake_agent_tools, src)
    (src / "ci" / "ship" / "ship.sh").unlink()
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {"version": 1, "agent_tools_source": str(src)}
    cat = Catalog.scan(str(src))
    plan = build(_loaded(cfg, repo), cat, project_type="cli")
    assert not [a for a in plan.actions if a.kind == "provision_ship_delegator"]
    assert any("ship_delegator" in n for n in plan.notes)


# ── config validation ───────────────────────────────────────────────────────────────
def test_validate_rejects_non_mapping():
    with pytest.raises(ConfigError):
        validate({"version": 1, "ship_delegator": "yes"})


def test_validate_rejects_unknown_key():
    with pytest.raises(ConfigError):
        validate({"version": 1, "ship_delegator": {"nope": True}})


def test_validate_rejects_non_bool_enabled():
    with pytest.raises(ConfigError):
        validate({"version": 1, "ship_delegator": {"enabled": "yes"}})


def test_validate_accepts_enabled_bool():
    validate({"version": 1, "ship_delegator": {"enabled": False}})


def test_validate_rejects_top_level_null_block():
    # `ship_delegator: ~` at the top level → None, which is not a mapping → ConfigError (consistent
    # with the non-mapping guard; an absent KEY is the way to take the default, not an explicit null).
    with pytest.raises(ConfigError):
        validate({"version": 1, "ship_delegator": None})


# ── drift parity ────────────────────────────────────────────────────────────────────
def test_drift_missing_delegator(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator" and i.item == "delegator"]
    assert items and items[0].direction == "missing"


def test_drift_clean_after_apply(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    report = detect(_plan_with_action(repo, canonical))
    assert not [i for i in report.items if i.category == "ship_delegator"]


def test_drift_modified_delegator(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    _delegator_path(repo).write_text("#!/usr/bin/env bash\n# tampered\n", encoding="utf-8")
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator"]
    assert items and items[0].direction == "modified"


def test_drift_missing_ignore_entry(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    # remove the exclude entry → drift should flag the unignored delegator
    _exclude_path(repo).write_text("", encoding="utf-8")
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator"]
    assert items and items[0].direction == "missing"


# ── io_error: a directory / unreadable file at the delegator path ───────────────────
def test_io_error_directory_at_delegator_path(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    # a DIRECTORY where the delegator file should be → io_error (apply errors, drift modified).
    deleg = _delegator_path(repo)
    deleg.mkdir(parents=True)
    assert resolve_ship_delegator(repo).state == "io_error"
    res = _apply(repo, canonical)
    assert res.status == "error"
    assert "not a regular file" in res.detail
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator"]
    assert items and items[0].direction == "modified"


# ── exclude entry handling: unbalanced markers, multi-pair collapse, no spurious backup ─
def test_unbalanced_exclude_marker_is_not_treated_as_present(tmp_path):
    # a begin marker with NO end is malformed; _reconcile_ship_exclude refuses to touch it, so it
    # must report as drift (NOT a false "ok"), else status reads clean while the file is broken.
    from riglib.actions.runner import SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)  # provision a correct delegator first
    # now corrupt the exclude: a lone begin marker (no end)
    _exclude_path(repo).write_text(SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER + "\n", encoding="utf-8")
    r = resolve_ship_delegator(repo)
    assert r.state == "update" and not r.exclude_ok
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator"]
    assert items and items[0].direction == "missing"


def test_duplicate_exclude_blocks_collapse_to_one(tmp_path):
    from riglib.actions.runner import ship_delegator_exclude_block_text

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    block = ship_delegator_exclude_block_text()
    excl = _exclude_path(repo)
    # a hand-added SECOND identical block + a user line between them
    excl.write_text(f"{block}\nkeep-me.txt\n{block}\n", encoding="utf-8")
    # re-apply collapses to exactly one block, preserving the user line
    _apply(repo, canonical)
    after = excl.read_text(encoding="utf-8")
    assert after.count("# >>> rig-managed ship delegator (do not edit) >>>") == 1
    assert "keep-me.txt" in after
    # and the resolution now reads clean (balanced, single block)
    assert resolve_ship_delegator(repo).state == "ok"


def test_mixed_correct_and_wrong_duplicate_blocks_collapse_to_one(tmp_path):
    # two managed blocks where ONE is correct and the other has a wrong body, with user content
    # before/between/after. Reconcile must collapse to exactly one CANONICAL block and preserve every
    # user line verbatim (the splice cursor/newline logic is where an off-by-one would land).
    from riglib.actions.runner import (
        SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER,
        SHIP_DELEGATOR_EXCLUDE_END_MARKER,
        ship_delegator_exclude_block_text,
    )

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    excl = _exclude_path(repo)
    good = ship_delegator_exclude_block_text()
    wrong = f"{SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER}\n# WRONG\n/nope\n{SHIP_DELEGATOR_EXCLUDE_END_MARKER}"
    excl.write_text(f"# head\n{good}\nmid-user-line\n{wrong}\n# tail\n", encoding="utf-8")
    _apply(repo, canonical)
    after = excl.read_text(encoding="utf-8")
    assert after.count(SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER) == 1  # collapsed to one block
    assert "/nope" not in after and "# WRONG" not in after  # the wrong block is gone
    assert good in after  # the surviving block is canonical
    for line in ("# head", "mid-user-line", "# tail"):
        assert line in after, f"user line {line!r} not preserved"
    assert resolve_ship_delegator(repo).state == "ok"


def test_exclude_write_is_atomic_no_partial_on_failure(tmp_path, monkeypatch):
    # if the atomic rename fails mid-reconcile, the pre-existing user content must survive intact
    # (no truncated/empty file) — the whole point of the temp-file + os.replace dance.
    import riglib.actions.runner as runner

    repo = _git_repo(tmp_path / "repo")
    excl = _exclude_path(repo)
    excl.parent.mkdir(parents=True, exist_ok=True)
    excl.write_text("# precious user content\n*.secret\n", encoding="utf-8")
    original = excl.read_text(encoding="utf-8")

    real_replace = os.replace

    def _boom(src, dst, *a, **k):
        if str(dst) == str(excl):
            raise OSError("simulated rename failure")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(runner.os, "replace", _boom)
    ok, note = runner._reconcile_ship_exclude(excl)
    assert ok is False
    # the original content is untouched (no partial write clobbered it)
    assert excl.read_text(encoding="utf-8") == original
    # no leftover temp files
    assert list(excl.parent.glob("*.rig-tmp")) == []


def test_single_block_with_wrong_body_is_reconciled(tmp_path):
    # a single managed block whose BODY was hand-edited (wrong comment) must be detected as drift and
    # rewritten in place to the canonical block (the common real-world drift, distinct from dupes).
    from riglib.actions.runner import (
        SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER,
        SHIP_DELEGATOR_EXCLUDE_END_MARKER,
        ship_delegator_exclude_block_text,
    )

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    excl = _exclude_path(repo)
    # tamper the block body (a wrong comment + wrong path inside correct, balanced markers)
    excl.write_text(
        f"# top\n{SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER}\n# tampered\n/wrong/path\n"
        f"{SHIP_DELEGATOR_EXCLUDE_END_MARKER}\n# bottom\n",
        encoding="utf-8",
    )
    assert not resolve_ship_delegator(repo).exclude_ok  # drift
    _apply(repo, canonical)
    after = excl.read_text(encoding="utf-8")
    assert ship_delegator_exclude_block_text() in after  # canonical block restored
    assert "/wrong/path" not in after  # tampered body gone
    assert "# top" in after and "# bottom" in after  # surrounding user content preserved
    assert resolve_ship_delegator(repo).state == "ok"


def test_unreadable_delegator_file_is_io_error(tmp_path):
    # a delegator file rig cannot READ (mode 000) → io_error (apply errors, drift modified). Skipped
    # when running as root (root bypasses the permission bit, so the read would succeed).
    if os.geteuid() == 0:
        pytest.skip("root bypasses file permissions; the unreadable-file branch can't be exercised")
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text("#!/usr/bin/env bash\n# x\n", encoding="utf-8")
    deleg.chmod(0o000)
    try:
        assert resolve_ship_delegator(repo).state == "io_error"
        res = _apply(repo, canonical)
        assert res.status == "error"
        report = detect(_plan_with_action(repo, canonical))
        items = [i for i in report.items if i.category == "ship_delegator"]
        assert items and items[0].direction == "modified"
    finally:
        deleg.chmod(0o644)  # so tmp cleanup can remove it


def test_no_spurious_backup_when_only_exclude_entry_is_missing(tmp_path):
    # the delegator FILE is already correct; only the exclude entry is gone. apply must NOT rewrite
    # the (correct) file and must NOT create a *.rig-bak-* backup of it — only add the exclude.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    _exclude_path(repo).write_text("", encoding="utf-8")  # drop the ignore entry, keep the file
    r = resolve_ship_delegator(repo)
    assert r.state == "update" and r.file_correct and not r.exclude_ok
    res = _apply(repo, canonical)
    assert res.status == "updated" and res.backup is None
    # no backup file was created next to the delegator
    backups = list(_delegator_path(repo).parent.glob("*.rig-bak-*"))
    assert backups == []
    # the exclude entry is back; now in sync
    assert resolve_ship_delegator(repo).state == "ok"


def test_skip_conflict_still_ignores_the_left_file_and_keeps_its_mode(tmp_path):
    # on_conflict=skip leaves a hand-edited delegator AS-IS (content AND mode untouched), but the
    # rig-owned exclude is still reconciled so the left file is ignored (status won't re-flag a
    # non-ignored delegator).
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text("#!/usr/bin/env bash\n# hand-edited, keep me\n", encoding="utf-8")
    deleg.chmod(0o644)  # NON-executable on purpose
    res = _apply(repo, canonical, on_conflict="skip")
    assert res.status == "skipped"
    # file content untouched
    assert "hand-edited" in deleg.read_text(encoding="utf-8")
    # mode untouched — rig must NOT chmod +x a user file it declined to write
    assert deleg.stat().st_mode & 0o111 == 0
    # but the exclude entry was added regardless
    assert ".claude/scripts/pr-ship.sh" in _exclude_path(repo).read_text(encoding="utf-8")


def test_overwrite_conflict_replaces_without_backup(tmp_path):
    # on_conflict=overwrite replaces a hand-edited delegator with the canonical one, NO backup kept.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text("#!/usr/bin/env bash\n# stale\n", encoding="utf-8")
    res = _apply(repo, canonical, on_conflict="overwrite")
    assert res.status == "updated" and res.backup is None
    assert deleg.read_text(encoding="utf-8") == ship_delegator_content()
    assert list(deleg.parent.glob("*.rig-bak-*")) == []
    assert resolve_ship_delegator(repo).state == "ok"


def test_apply_errors_when_exclude_unwritable(tmp_path, monkeypatch):
    # if .git/info/exclude cannot be written, the delegator is on disk but NOT ignored → the action
    # must report ERROR (not a misleading "created"), because the worktree would be dirtied.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)

    import riglib.actions.runner as runner

    monkeypatch.setattr(runner, "_reconcile_ship_exclude", lambda p: (False, f"could not write {p}: boom"))
    res = _apply(repo, canonical)
    assert res.status == "error"
    assert "NOT git-ignored" in res.detail


def test_drift_reports_both_modified_file_and_missing_ignore(tmp_path):
    # when BOTH the file content differs AND the ignore entry is gone, status must show BOTH issues.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    _delegator_path(repo).write_text("#!/usr/bin/env bash\n# tampered\n", encoding="utf-8")
    _exclude_path(repo).write_text("", encoding="utf-8")  # drop the ignore entry too
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator"]
    dirs = sorted(i.direction for i in items)
    assert dirs == ["missing", "modified"], f"expected both, got {[(i.direction, i.item) for i in items]}"


def test_exclude_preserves_user_content_around_the_block(tmp_path):
    # user lines BEFORE and AFTER the managed block must survive a reconcile (splice-cursor regression).
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    excl = _exclude_path(repo)
    excl.parent.mkdir(parents=True, exist_ok=True)
    excl.write_text("# my own ignore\n*.log\n", encoding="utf-8")
    _apply(repo, canonical)
    after = excl.read_text(encoding="utf-8")
    assert "# my own ignore" in after and "*.log" in after
    assert ".claude/scripts/pr-ship.sh" in after
    # idempotent: a re-apply preserves it all and adds nothing
    _apply(repo, canonical)
    assert excl.read_text(encoding="utf-8") == after


# ── default-on validation: {} and {enabled: true} are accepted (the production defaults) ─
def test_validate_accepts_empty_block_and_enabled_true():
    validate({"version": 1, "ship_delegator": {}})
    validate({"version": 1, "ship_delegator": {"enabled": True}})
    validate({"version": 1})  # absent block → default ON


def test_validate_rejects_enabled_null():
    # YAML `enabled: ~` → None. We REJECT it (the JSON Schema declares boolean; null is invalid
    # there) — to take the default, OMIT the key rather than set it to null.
    with pytest.raises(ConfigError):
        validate({"version": 1, "ship_delegator": {"enabled": None}})


def test_misordered_exclude_markers_reported_as_drift_not_rewritten(tmp_path):
    # an end marker BEFORE a begin marker is misordered; reconcile refuses it and it reads as drift.
    from riglib.actions.runner import (
        SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER,
        SHIP_DELEGATOR_EXCLUDE_END_MARKER,
        _reconcile_ship_exclude,
    )

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    excl = _exclude_path(repo)
    excl.write_text(
        f"{SHIP_DELEGATOR_EXCLUDE_END_MARKER}\n{SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER}\n", encoding="utf-8"
    )
    # not in sync (drift)
    r = resolve_ship_delegator(repo)
    assert not r.exclude_ok
    # reconcile refuses (ok=False) and leaves the file untouched
    before = excl.read_text(encoding="utf-8")
    ok, note = _reconcile_ship_exclude(excl)
    assert ok is False and "misordered" in note
    assert excl.read_text(encoding="utf-8") == before


# ── full plan round-trip via run_plan ───────────────────────────────────────────────
def test_run_plan_provisions_and_is_idempotent(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    plan = _plan_with_action(repo, canonical)
    plan.on_conflict = "backup"
    report = run_plan(plan)
    assert all(r.status != "error" for r in report.results)
    assert _delegator_path(repo).is_file()
    # second run is a no-op
    report2 = run_plan(plan)
    deleg = [r for r in report2.results if r.action.kind == "provision_ship_delegator"]
    assert deleg and deleg[0].status == "skipped"
