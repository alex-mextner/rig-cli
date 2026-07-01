"""Shared fixtures — a tiny fake agent-tools checkout so tests don't depend on the real one."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch, tmp_path):
    """Never let a test write into the REAL ``$HOME`` via a HOME-relative default path.

    The skill harness-link default discovery dir is ``~/.claude/skills`` (HOME-expanded at
    apply time). A test that builds a plan with skills enabled but no ``harness_skill_dir``
    pinned would otherwise symlink into the developer's real ``~/.claude/skills``. Point HOME
    at a throwaway dir suite-wide so no plan/apply can ever touch the real home — tests that
    need a specific HOME (the dispatcher/schedule/CLI tests) override this with their own
    ``monkeypatch.setenv("HOME", ...)``, which wins because it runs inside the test body.
    """
    home = tmp_path / "isolated-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    # Also pin XDG_CONFIG_HOME under the throwaway home. The global-excludes default
    # (``~/.config/git/ignore``) expands ``~/.config`` via ``$XDG_CONFIG_HOME`` when set — so a
    # developer who exports XDG_CONFIG_HOME globally would otherwise have a full-plan e2e test
    # write into their REAL ``$XDG_CONFIG_HOME/git/ignore``. Force it under the isolated home so no
    # test can ever touch a real XDG config dir. Tests that need a specific XDG override it inline.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))


@pytest.fixture(autouse=True)
def _isolate_tui_lib_dir(monkeypatch, tmp_path):
    """Never let a test prepend the REAL TUI overlay dir onto this process's ``sys.path``.

    ``cmd_setup`` and ``_auto_install_tui`` call ``_inject_tui_lib_dir()``, which ``sys.path.insert``s
    the rig-owned overlay (``~/.local/share/rig/tui-libs``) when it exists — a PROCESS-GLOBAL mutation
    that monkeypatch does not revert. On a dev box / CI where that overlay already holds textual+rich
    (after a real ``rig init``), any ``init`` test would leak it into ``sys.path`` for the rest of the
    pytest session, making textual spuriously importable in later tests that assert it absent. Point the
    overlay at a per-test throwaway path (which does not exist → the injection is a guaranteed no-op).
    Tests exercising the overlay set ``RIG_TUI_LIB_DIR`` (and isolate ``sys.path``) inline, which wins.
    """
    monkeypatch.setenv("RIG_TUI_LIB_DIR", str(tmp_path / "tui-overlay-absent"))


@pytest.fixture(autouse=True)
def _isolate_scheduler(monkeypatch):
    """Never let a test touch the REAL launchd / crontab — or write a real plist file.

    The model-freshness schedule (models:) is in the DEFAULT scaffold, so any e2e test that
    runs `rig init --yes` end-to-end would otherwise (1) write a real
    `~/Library/LaunchAgents/...plist` (the file write follows `Path.home()`, which HOME
    isolation doesn't always cover) and (2) shell out to the real `launchctl` / `crontab`.

    So this guard NEUTRALIZES the whole `provision_schedule` action suite-wide: it becomes a
    no-op returning a `skipped` result. The DEDICATED schedule tests
    (test_models_schedule.py) call `runner._do_provision_schedule` / `_provision_*` and
    install their OWN mocks (HOME-isolated tmp dirs + stubbed daemon seams), which override
    this autouse stub for those tests — so they exercise the real install logic safely while
    every OTHER test is fully insulated from the host scheduler.

    It ALSO stubs the daemon/crontab seams on BOTH the `runner` and `drift` modules (drift
    imports them by name, so patching only `runner` would leave `drift`'s bindings live).
    """
    from riglib import drift as driftmod
    from riglib.actions import runner
    from riglib.plan import Action  # noqa: F401 — imported for the stub's type clarity

    crontab_store = {"has": False, "content": ""}

    def _noop_provision(action, on_conflict):
        from riglib.actions.runner import ActionResult

        return ActionResult(action, "skipped", "models/model-freshness: scheduler stubbed in tests")

    def _noop_tg_ctl(action, on_conflict):
        from riglib.actions.runner import ActionResult

        return ActionResult(action, "skipped", "tg_ctl/boot: daemon provisioner stubbed in tests")

    def _noop_tools(action, on_conflict):
        from riglib.actions.runner import ActionResult

        return ActionResult(action, "skipped", "tools: ecosystem installer stubbed in tests")

    # Patch BOTH the module attr and the dispatch-table entry: `run_plan` resolves the handler
    # from `_HANDLERS` (a dict built at import with a direct function reference), so patching
    # only the module attr would leave the e2e `run_plan` path calling the real installer.
    monkeypatch.setattr(runner, "_do_provision_schedule", _noop_provision)
    monkeypatch.setitem(runner._HANDLERS, "provision_schedule", _noop_provision)
    # tg_ctl: (boot:) is ALSO in the DEFAULT scaffold (default-on), so an e2e `rig init --yes`
    # would otherwise write a real ~/Library/LaunchAgents/ai.hyperide.tg-ctl.plist AND shell out
    # to the real `launchctl bootstrap`. Neutralize it suite-wide exactly like the schedule; the
    # dedicated tests (test_tg_ctl.py) call `_do_provision_tg_ctl` with their own HOME-isolated
    # tmp dirs + stubbed launchctl seams, which override this stub for those tests.
    monkeypatch.setattr(runner, "_do_provision_tg_ctl", _noop_tg_ctl)
    monkeypatch.setitem(runner._HANDLERS, "provision_tg_ctl", _noop_tg_ctl)
    # tools: is DEFAULT-OFF (opt-in), so no default e2e plan emits a provision_tools action and
    # nothing here is strictly required. But a test that DOES enable a tools: block would otherwise
    # shell out to a real install.sh (clone/symlink/install-skill on the host). Neutralize it
    # suite-wide, symmetric with schedule/tg_ctl; the dedicated test_tools.py restores the real
    # handler with its own HOME-isolated temp repos + fake install.sh.
    monkeypatch.setattr(runner, "_do_provision_tools", _noop_tools)
    monkeypatch.setitem(runner._HANDLERS, "provision_tools", _noop_tools)
    # tg_ctl is default-on, so a provision_tg_ctl action exists in EVERY e2e plan. Its drift
    # check reads the REAL ``_on_darwin()`` (true on a macOS dev box) and would flag the missing
    # plist — but the provisioner above is stubbed, so apply never writes it, leaving a permanent
    # phantom drift in e2e tests. Neutralize the drift check suite-wide too (the dedicated
    # test_tg_ctl.py restores the real one). Symmetric with the provisioner stub.
    monkeypatch.setattr(driftmod, "_check_tg_ctl", lambda action, report: None)

    for mod in (runner, driftmod):
        monkeypatch.setattr(mod, "_launchctl", lambda verb, arg: 0, raising=False)
        monkeypatch.setattr(mod, "_launchctl_loaded", lambda label: False, raising=False)
        # the gui-domain verbs the tg-ctl provisioner uses — stub them so neither runner nor
        # drift can EVER reach the real launchd domain in any test (a test that mutates the real
        # launchd domain is a FAIL). Dedicated tg-ctl tests install their own stubs/spies.
        monkeypatch.setattr(mod, "_launchctl_bootstrap", lambda plist: 0, raising=False)
        monkeypatch.setattr(mod, "_launchctl_bootout", lambda plist: 0, raising=False)
        monkeypatch.setattr(mod, "_launchctl_gui_loaded", lambda label: False, raising=False)
        monkeypatch.setattr(mod, "_read_crontab", lambda: (crontab_store["has"], crontab_store["content"]))

    def _fake_write_crontab(contents):
        crontab_store["has"] = True
        crontab_store["content"] = contents
        return 0

    monkeypatch.setattr(runner, "_write_crontab", _fake_write_crontab)


@pytest.fixture(autouse=True)
def _isolate_global_git_config(monkeypatch):
    """Never let a test read or WRITE the real ``git config --global``.

    The global-excludes block (gitignore:) is in the DEFAULT scaffold, so any e2e test that runs
    a full ``build`` + ``run_plan`` would otherwise shell out to real ``git config --global
    core.excludesfile`` — and, on a machine where that is UNSET, would WRITE the real global
    config. That is a hard-fail (a test must never mutate the user's git config). So this guard
    stubs both git-config seams suite-wide on BOTH the runner and drift modules (drift imports
    ``_git_global`` by name):

      - reads (``_git_global``) return ``None`` — i.e. ``core.excludesfile`` is UNSET — so e2e
        apply takes the clean-machine path: set it to the XDG default and write the block. With
        HOME isolated, the XDG default expands UNDER the throwaway home, never the real one.
      - writes (``_set_git_global``) are captured in an in-memory store (so a subsequent read
        could see them if a test wants), never touching real git config.

    The DEDICATED global-excludes tests (test_global_excludes.py) install their OWN seam mocks in
    the test body — those run after this autouse fixture and win — so they can exercise both the
    set-vs-unset target resolution explicitly.
    """
    from riglib import drift as driftmod
    from riglib.actions import runner

    store: dict[str, str] = {}

    for mod in (runner, driftmod):
        monkeypatch.setattr(mod, "_git_global", lambda key: store.get(key), raising=False)
    monkeypatch.setattr(runner, "_set_git_global", lambda key, value: store.__setitem__(key, value) or 0)


@pytest.fixture(autouse=True)
def _isolate_tmux_activation(monkeypatch):
    """Never let a test run the LIVE tmux activation (clone plugins / launchctl / first save).

    ``_do_provision_tmux`` now ALSO activates the rig-managed tmux on a clean machine: it clones
    tpm/resurrect/continuum, creates ~/.tmux/resurrect, ``launchctl load -w``s the boot agent,
    takes a first ``resurrect save``, and cleans continuum's stale macOS boot Login Items
    (DEFECTS 1/4/5/6). Those are real network + daemon + ``tmux``-server effects. This guard sets
    ``RIG_TMUX_DRY_RUN=1`` suite-wide so the file-write path still runs (and is asserted) while the
    live activation is skipped. The DEDICATED activation tests + the REAL e2e clear/override this
    (``monkeypatch.delenv`` or ``setenv(..., "0")``) and stub the seams, so they exercise the real
    logic safely. Mirrors ``_isolate_scheduler``.
    """
    monkeypatch.setenv("RIG_TMUX_DRY_RUN", "1")


@pytest.fixture
def fake_agent_tools(tmp_path: Path) -> Path:
    """A minimal but structurally-valid agent-tools checkout."""
    root = tmp_path / "agent-tools"

    # universal skills
    for name in ("shell-timeouts", "naming", "push-regularly"):
        _write(
            root / "skills" / "universal" / name / "SKILL.md",
            f"---\nname: {name}\ndescription: what {name} gives you\n---\n# {name}\nbody\n",
        )
    # by-type skills
    for kind, skill in (("cli", "lazy-imports"), ("backend", "atomic-tx"), ("frontend", "tokens")):
        _write(
            root / "skills" / "by-type" / kind / skill / "SKILL.md",
            f"---\nname: {skill}\ndescription: {kind} skill {skill}\n---\n# {skill}\n",
        )

    # an agent hook
    _write(
        root / "agent-hooks" / "block-no-verify" / "block-no-verify.pre-bash.json",
        json.dumps(
            {
                "id": "block-no-verify",
                "point": "pre-bash",
                "cmd": "/ABSOLUTE/PATH/TO/agent-hooks/block-no-verify/block_no_verify.py",
                "on_error": "closed",
            }
        ),
    )
    _write(root / "agent-hooks" / "block-no-verify" / "block_no_verify.py", "#!/usr/bin/env python3\n")
    _write(root / "agent-hooks" / "block-no-verify" / "README.md", "# block-no-verify\nblocks bypass\n")

    # CI slots: workflow.yml + a variant + a slot-named file + the slots the default
    # scaffold (riglib/state.py) references, so `setup --yes` (default) is satisfiable.
    _write(root / "ci" / "codeql" / "workflow.yml", "name: codeql\n")
    _write(root / "ci" / "codeql" / "workflow-selfgate.yml", "name: codeql-selfgate\n")
    _write(root / "ci" / "codeql" / "README.md", "# codeql\nsemantic SAST\n")
    _write(root / "ci" / "secret-scan" / "secret-scan.yml", "name: secret-scan\n")
    _write(root / "ci" / "secret-scan" / "gitleaks.toml", "# not a workflow\n")
    _write(root / "ci" / "secret-scan" / "README.md", "# secret-scan\ngitleaks\n")
    for slot in ("dependency-review", "leftover-grep", "review-threads"):
        # review-threads is a merge-gating gate: model its real JOB structure (a `jobs:` block whose
        # job `name:` is the check-run context `required_status_checks` matches) so the fixture is a
        # faithful proxy. The other two are not in CI_GATE_CHECK_CONTEXTS, so a flat file suffices.
        if slot == "review-threads":
            wf = "name: review-threads\non: pull_request_target\njobs:\n  review-threads:\n    name: review-threads\n    runs-on: ubuntu-latest\n"
        else:
            wf = f"name: {slot}\nrun: bash ci/{slot}/{slot}.sh\n"
        _write(root / "ci" / slot / "workflow.yml", wf)
        _write(root / "ci" / slot / f"{slot}.sh", f"#!/usr/bin/env bash\necho {slot}\n")
        _write(root / "ci" / slot / "README.md", f"# {slot}\n")
    # pr-checklist imports its script from .github/scripts/ rather than ci/<slot>/, so it has no
    # sibling .sh. Its check-run CONTEXT (what required_status_checks matches) is the JOB's `name:`
    # ("PR Checklist"), which differs from the slot id — mirror the real workflow's job structure so
    # the fixture is a faithful proxy for the lockout-safe context mapping.
    _write(
        root / "ci" / "pr-checklist" / "workflow.yml",
        "name: PR Checklist\non: pull_request_target\njobs:\n  checklist:\n    name: PR Checklist\n    runs-on: ubuntu-latest\n",
    )
    _write(root / "ci" / "pr-checklist" / "README.md", "# pr-checklist\nverify checkboxes\n")
    _write(root / "ci" / "ship" / "ship.sh", "#!/usr/bin/env bash\necho ship\n")
    _write(root / "ci" / "ship" / "README.md", "# ship\nmerge gate\n")

    # the global dispatcher: runner + composers (core.hooksPath target) + fragments
    disp = root / "git-hooks" / "global-dispatcher"
    _write(disp / "run-global-hooks", "#!/usr/bin/env bash\necho run\n")
    _write(disp / "install-local-hooks.sh", "#!/usr/bin/env bash\necho retrofit\n")
    for composer in ("pre-commit", "commit-msg", "pre-push", "review-gate"):
        _write(disp / "hooks" / composer, "#!/usr/bin/env bash\nexec ../run-global-hooks\n")
    _write(disp / "global-hooks.d" / "pre-commit" / "10-secret-scan", "#!/usr/bin/env bash\n")
    _write(disp / "global-hooks.d" / "commit-msg" / "10-conventional-commit", "#!/usr/bin/env bash\n")
    _write(disp / "README.md", "# dispatcher\nglobal hooks\n")

    # mcp — a CLEARLY-SYNTHETIC item ("fake-mcp"). It must MIRROR catalog reality: the real
    # agent-tools `mcp/` ships only a README + the one removed slot `mcp/review` (see
    # riglib.errors._REMOVED_SLOTS). Never fabricate a slot rig classifies as removed — a fake
    # `mcp/review` made `review` resolve VALID here while it errors against the real catalog,
    # the exact divergence that let a dead `mcp.items.review` ship green (issue #61). The
    # test_fake_catalog_never_fabricates_a_removed_slot guard fails if this regresses.
    _write(root / "mcp" / "fake-mcp" / "README.md", "# fake-mcp\nsynthetic mcp fixture\n")

    # the model-freshness checker the `models:` schedule runs (its presence is checked by
    # plan._build_models before it provisions a cron).
    _write(root / "lib" / "checker" / "model_freshness.py", "#!/usr/bin/env python3\n")

    # the cc_hook_bridge dispatcher the `harness.hook_bridge` wiring points at (its presence
    # is checked by plan._build_hook_bridge before it registers the settings.json hooks).
    _write(root / "lib" / "cc_hook_bridge" / "dispatch.py", "#!/usr/bin/env python3\n")

    return root
