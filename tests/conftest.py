"""Shared fixtures — a tiny fake agent-tools checkout so tests don't depend on the real one."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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

    return root
