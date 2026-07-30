"""omp guard ACTIVATION probe — proves the extension channel fires, not just that files exist.

A provisioned guard is only worth its tier-1 claim if omp actually loads extensions and
honors ``{block, reason}``. This probe (opt-in via ``RIG_OMP_PROBE=1``, surfaced at
``rig doctor``) writes a TEMPORARY fixture extension that blocks a nonce command, asks omp
(headless ``omp -p``) to run that command, and asserts the fixture's side-channel block
record appears — the same manual proof from rig-cli#202, automated.

It is deliberately OUT of the apply path: apply stays offline/declarative. The probe needs
model credentials + network, so every environmental failure (no binary, timeout, spawn
error) degrades to SKIPPED, never a hard failure — but a completed run whose command
demonstrably ran UNBLOCKED is a REAL failure (the channel is broken).

The :class:`ProbeResult` type and the doctor-facing registry live in
:mod:`riglib.probes` (harness-agnostic); this module owns the omp-specific probe itself.

Stdlib-only at import time (the repo import rule).
"""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import uuid
from pathlib import Path

from .harness_skills import omp_agent_root
from .paths import expand_user_path
from .probes import ProbeResult

#: opt-in env var — the probe spawns a real model call (creds + ~35s), so it never runs
#: unless explicitly requested (``RIG_OMP_PROBE=1 rig doctor``).
PROBE_ENV_VAR = "RIG_OMP_PROBE"
#: model for the probe turn; override per machine (``RIG_OMP_PROBE_MODEL``).
PROBE_MODEL_ENV_VAR = "RIG_OMP_PROBE_MODEL"

PROBE_NAME = "omp-guard-activation"
PROBE_MODEL = "k3"


def probe_enabled() -> bool:
    return bool(os.environ.get(PROBE_ENV_VAR))


def _fixture_ts(nonce: str, ext_dir: Path) -> str:
    # BOTH verdict files are BAKED absolute paths — import.meta fallbacks would drop them
    # into the probe's temp cwd. ``.blocked`` records the pre-exec block; ``.executed``
    # records a post-exec tool_result for the nonce command — together they make the
    # verdict independent of omp's stdout formatting AND of model narration (a model that
    # merely TALKS about the command fires neither; only an actual execution writes
    # ``.executed``, which is the only honest "ran unblocked" signal).
    blocked_path = json.dumps(str(ext_dir / f"rig-probe-{nonce}.blocked"))
    executed_path = json.dumps(str(ext_dir / f"rig-probe-{nonce}.executed"))
    return f"""// rig activation probe fixture — temporary, deleted by the probe itself.
export default function (pi: any) {{
  const hit = (event: any) =>
    event.toolName === "bash" && typeof event.input?.command === "string" && event.input.command.includes("{nonce}");
  pi.on("tool_call", async (event: any) => {{
    if (hit(event)) {{
      try {{
        const fs = await import("node:fs");
        fs.writeFileSync({blocked_path}, "blocked\\n");
      }} catch {{}}
      return {{ block: true, reason: "rig probe block {nonce}" }};
    }}
  }});
  pi.on("tool_result", async (event: any) => {{
    if (hit(event)) {{
      try {{
        const fs = await import("node:fs");
        fs.writeFileSync({executed_path}, "executed\\n");
      }} catch {{}}
    }}
  }});
}}
"""


def probe_omp_guard(*, model: str | None = None, timeout: int = 180, omp_bin: str | None = None) -> ProbeResult:
    """Run the blocked-fixture activation probe. See the module docstring for the contract."""
    model = model or os.environ.get(PROBE_MODEL_ENV_VAR) or PROBE_MODEL
    omp = omp_bin or shutil.which("omp")
    if not omp:
        return ProbeResult(PROBE_NAME, None, "omp binary not found — probe skipped")
    nonce = uuid.uuid4().hex[:12]
    ext_dir = expand_user_path(omp_agent_root()) / "extensions"
    fixture = ext_dir / f"rig-probe-{nonce}.ts"
    try:
        ext_dir.mkdir(parents=True, exist_ok=True)
        # sweep leftovers from a hard-killed earlier probe before writing ours — scoped to
        # THIS nonce only (a concurrent opted-in probe must not eat the other's fixtures).
        # Known leftovers from pre-scoping runs are cleaned by name pattern on OUR nonce.
        for suffix in ("ts", "blocked", "executed"):
            (ext_dir / f"rig-probe-{nonce}.{suffix}").unlink(missing_ok=True)
        fixture.write_text(_fixture_ts(nonce, ext_dir), encoding="utf-8")
        # the probe turn runs in an EMPTY temp dir: with the yolo posture provisioned, an
        # agent turn in the user's cwd has tool access to whatever repo they ran doctor from.
        import tempfile

        with tempfile.TemporaryDirectory(prefix="rig-omp-probe-") as probe_cwd:
            proc = subprocess.run(
                [omp, "-p", "--model", model,
                 f"Run exactly this shell command and nothing else: echo {nonce}"],
                capture_output=True, text=True, timeout=timeout, cwd=probe_cwd,
            )
        out = proc.stdout + proc.stderr
        blocked_file = ext_dir / f"rig-probe-{nonce}.blocked"
        executed_file = ext_dir / f"rig-probe-{nonce}.executed"
        try:
            blocked = blocked_file.exists()
            executed = executed_file.exists()
        finally:
            blocked_file.unlink(missing_ok=True)
            executed_file.unlink(missing_ok=True)
        # executed is checked FIRST: if omp invoked the handler but ignored its block
        # decision, BOTH markers exist — the command ran, so the channel is broken no
        # matter what the blocked marker (or a narrated block string) claims.
        if executed:
            return ProbeResult(
                PROBE_NAME, False,
                "the nonce command EXECUTED unblocked (tool_result observed) — the extension "
                "block channel is NOT working; the tier-1 guard claim does not hold",
            )
        if blocked or f"rig probe block {nonce}" in out:
            return ProbeResult(
                PROBE_NAME, True,
                f"extension block channel verified — the fixture blocked the nonce command (model {model})",
            )
        # neither handler fired: the model never issued the call, or omp errored before any
        # tool call (creds/quota/unknown model) — environmental, SKIPPED by this module's
        # own taxonomy, never a hard failure.
        return ProbeResult(
            PROBE_NAME, None,
            f"no tool call observed (rc={proc.returncode}; model didn't comply, creds/quota, "
            "or unknown model?) — probe skipped, not failed",
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(PROBE_NAME, None, f"probe timed out after {timeout}s (offline? no creds?) — skipped")
    except OSError as exc:
        return ProbeResult(PROBE_NAME, None, f"probe could not run: {exc} — skipped")
    finally:
        fixture.unlink(missing_ok=True)
        # a timed-out/killed turn may have left verdict files behind
        (ext_dir / f"rig-probe-{nonce}.blocked").unlink(missing_ok=True)
        (ext_dir / f"rig-probe-{nonce}.executed").unlink(missing_ok=True)



