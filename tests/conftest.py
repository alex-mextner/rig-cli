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


@pytest.fixture(autouse=True)
def _isolate_scheduler(monkeypatch):
    """Never let a test touch the REAL launchd / crontab — or write a real plist file.

    The model-freshness schedule (models:) is in the DEFAULT scaffold, so any e2e test that
    runs `rig setup --yes` / `rig init` end-to-end would otherwise (1) write a real
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

    # Patch BOTH the module attr and the dispatch-table entry: `run_plan` resolves the handler
    # from `_HANDLERS` (a dict built at import with a direct function reference), so patching
    # only the module attr would leave the e2e `run_plan` path calling the real installer.
    monkeypatch.setattr(runner, "_do_provision_schedule", _noop_provision)
    monkeypatch.setitem(runner._HANDLERS, "provision_schedule", _noop_provision)

    for mod in (runner, driftmod):
        monkeypatch.setattr(mod, "_launchctl", lambda verb, arg: 0, raising=False)
        monkeypatch.setattr(mod, "_launchctl_loaded", lambda label: False, raising=False)
        monkeypatch.setattr(mod, "_read_crontab", lambda: (crontab_store["has"], crontab_store["content"]))

    def _fake_write_crontab(contents):
        crontab_store["has"] = True
        crontab_store["content"] = contents
        return 0

    monkeypatch.setattr(runner, "_write_crontab", _fake_write_crontab)


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
        _write(root / "ci" / slot / "workflow.yml", f"name: {slot}\nrun: bash ci/{slot}/{slot}.sh\n")
        _write(root / "ci" / slot / f"{slot}.sh", f"#!/usr/bin/env bash\necho {slot}\n")
        _write(root / "ci" / slot / "README.md", f"# {slot}\n")
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

    # mcp
    _write(root / "mcp" / "review" / "README.md", "# review\nmulti-model review\n")

    # the model-freshness checker the `models:` schedule runs (its presence is checked by
    # plan._build_models before it provisions a cron).
    _write(root / "lib" / "checker" / "model_freshness.py", "#!/usr/bin/env python3\n")

    return root
