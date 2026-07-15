"""Meta-test: pin the gating granularity of ``tests/test_tmux_e2e.py``.

What this is
------------
A tamper-proof guard for INCIDENT 2026-06-17 follow-up (a). The tmux tests that need NO network —
the socket-leak REGRESSION (``test_teardown_unlinks_the_private_socket_file``) and the DEFECT-5
boot-cleanup test — must run in DEFAULT hermetic CI, so they are gated tmux-only
(``@_requires_tmux``). The plugin-cloning e2e tests stay behind the opt-in network gate
(``@_requires_tmux_e2e``, ``RIG_TMUX_E2E=1`` + GitHub-reachability). The original bug was a single
blanket module-level ``pytestmark`` that gated EVERY test behind the network — silently hiding the
leak regression exactly when it mattered (offline / hermetic CI), so the leak that KILLED the dev's
tmux server went unguarded.

Why a SEPARATE file (not a test inside ``test_tmux_e2e.py``)
-----------------------------------------------------------
If this guard lived in ``test_tmux_e2e.py`` itself, a re-introduced blanket ``pytestmark`` would
ALSO skip the guard (a module-level mark applies to every test in its module), so the regression
it polices would re-hide the policing test along with it — the guard would be defeated by the very
change it exists to catch. Living in a DIFFERENT module, this test imports ``test_tmux_e2e`` as a
plain module and inspects its markers; no ``pytestmark`` over there can suppress a test over here.

How it is reached
-----------------
Plain ``pytest`` collects it (no marker), so a CI run that breaks the gating granularity fails on
every PR. The introspection itself touches neither tmux nor network; the only side effect is the
IMPORT of ``test_tmux_e2e``, whose module-level gate computation probes GitHub ONLY when
``RIG_TMUX_E2E`` is set (the opt-in path) — in the default CI path (flag unset) the import, and
thus this guard, is fully network-free.

Invariants
----------
- We key each test's gate on the MARKER OBJECT (the ``_requires_tmux`` / ``_requires_tmux_e2e``
  ``MarkDecorator`` identities pulled from the imported module), NOT on the ``reason`` text — a
  reword of a ``reason`` string can't fool identity, and identity reflects the ACTUAL condition the
  decorator was built from, not just its label.
- The guard asserts: (1) no blanket module ``skip``/``skipif`` mark; (2) the network-free tests
  carry the tmux-only gate and NOT the network gate; (3) the plugin-cloning tests keep the network
  gate; (4) EXHAUSTIVELY, every ``test_*`` in the module is classified + correctly gated, so a new
  ungated test fails here instead of slipping through.
- A SECOND guard (``test_ci_has_tmux_so_the_leak_regression_actually_runs``) closes the other half
  of the INCIDENT: correct per-test gating is necessary but NOT sufficient — a tmux-only gate still
  SKIPS on a runner without tmux. In CI (the ``CI`` env var) tmux MUST be on PATH, so a CI image
  that drops the ``apt-get install tmux`` step fails LOUDLY here instead of silently skipping the
  leak regression. Outside CI a tmux-less dev still gets the normal skip.
"""

from __future__ import annotations

import importlib
import os
import shutil

import pytest

# Tests that need tmux but NO network (run in default hermetic CI, @_requires_tmux):
#   - the socket-leak regression (boots a private `-L` server, asserts teardown unlinks the socket);
#   - the DEFECT-5 boot-cleanup test (fabricates stale files + MOCKS _launchctl, clones nothing);
#   - the migrated-conf parse-validity checks (migrate a legacy conf / a comment-only brace body,
#     load each in a private `-L` server, assert exit 0 — clone nothing, hit no network).
_TMUX_ONLY_TEST_NAMES = frozenset({
    "test_teardown_unlinks_the_private_socket_file",
    "test_old_continuum_boot_cleanup_removes_stale_entries",
    "test_migrated_conf_with_neutralized_legacy_init_parses",
    "test_comment_only_brace_body_after_moshi_neutralize_parses",
})
# Tests that git-clone the real plugins from GitHub → opt-in network gate (@_requires_tmux_e2e):
_NETWORK_TEST_NAMES = frozenset({
    "test_clean_machine_apply_brings_tmux_up_with_config_and_session",
    "test_cc_save_populates_map_from_a_real_claude_child",
    "test_cc_save_detects_the_versioned_binary_install",
    "test_cc_restore_relaunches_claude_resume_into_fresh_shell",
    "test_resurrect_writes_a_real_snapshot",
    # the launchd-minimal-PATH regression guard (#138): both apply real plugins (network) and run
    # the saver under `env -i` + the plist env. The negative control is additionally darwin-gated
    # in-body, but the network gate is what this pin tracks.
    "test_autosave_wrapper_saves_under_launchd_minimal_env",
    "test_autosave_wrapper_fails_without_plist_path_injection",
})
# Intentionally-ungated hermetic tests (none today). A new pure-introspection test that needs
# neither tmux nor network goes HERE — the explicit escape hatch the exhaustive check (4) demands,
# so adding such a test does not force a fake gate.
_HERMETIC_EXEMPT_TEST_NAMES: frozenset[str] = frozenset()


def _pytestmark_list(obj) -> list:
    """The ``pytestmark`` of a function OR module as a LIST. pytest accepts either a list of marks
    or a single bare ``MarkDecorator`` (``pytestmark = pytest.mark.X``); normalize both so callers
    can always iterate. A ``MarkDecorator`` is NOT iterable, so a naive ``list(...)`` would raise."""
    marks = getattr(obj, "pytestmark", [])
    if isinstance(marks, (list, tuple)):
        return list(marks)
    return [marks]  # a single bare MarkDecorator


def _marks(func) -> list:
    """Every ``pytest`` Mark decorating ``func`` (markers live on ``func.pytestmark``)."""
    return _pytestmark_list(func)


def test_tmux_e2e_gating_granularity_is_pinned():
    """The network-free tests stay tmux-only-gated and the plugin tests stay opt-in-gated.

    A future "cleanup" that re-adds a blanket module-level ``pytestmark`` (the original INCIDENT
    bug) — silently re-hiding the leak regression behind ``RIG_TMUX_E2E`` + GitHub-reachability —
    fails HERE. Network-free in the default CI path (see module docstring).
    """
    e2e = importlib.import_module("tests.test_tmux_e2e")

    # The two gate MARKER OBJECTS, by identity — the contract every test below is checked against.
    # Pulling them from the module (not reconstructing) means we compare the SAME decorator object
    # the tests were decorated with, so the check is immune to a `reason` reword and reflects the
    # real condition each gate carries.
    tmux_gate = e2e._requires_tmux
    network_gate = e2e._requires_tmux_e2e
    assert tmux_gate is not network_gate, "the two gates must be distinct marker objects"

    def has_gate(func, gate) -> bool:
        # A `MarkDecorator` applied to a function shows up as a `Mark` on `func.pytestmark`; compare
        # against the decorator's own `.mark`, which is that exact `Mark` object (stable identity).
        # An entry may itself be a bare `MarkDecorator` (rare) — unwrap via `.mark` for those too.
        target = gate.mark
        return any(getattr(m, "mark", m) is target for m in _marks(func))

    # 1) No blanket module-level skip/skipif mark — that is exactly the over-broad gate the INCIDENT
    #    follow-up removed. A reintroduced one would apply to EVERY e2e test, re-hiding the leak
    #    regression behind the network gate. (We reject ONLY skip/skipif module marks, so a benign
    #    future module mark like `filterwarnings` doesn't false-trip this guard.)
    module_marks = [
        m for m in _pytestmark_list(e2e)
        # an entry is a `Mark` (has `.name`) or a bare `MarkDecorator` (unwrap via `.mark`).
        if getattr(getattr(m, "mark", m), "name", "") in ("skip", "skipif")
    ]
    assert not module_marks, (
        "test_tmux_e2e.py has a module-level skip/skipif `pytestmark` — it re-hides the socket-leak "
        "regression behind the opt-in network gate (INCIDENT 2026-06-17 follow-up a). Gate per-test."
    )

    # 2) The network-FREE tests carry the tmux gate and NOT the network gate (→ default hermetic CI).
    for name in _TMUX_ONLY_TEST_NAMES:
        fn = getattr(e2e, name)
        assert has_gate(fn, tmux_gate), (
            f"{name} lost its @_requires_tmux gate — it must run in default hermetic CI"
        )
        assert not has_gate(fn, network_gate), (
            f"{name} must NOT carry @_requires_tmux_e2e — that would only run it with "
            "RIG_TMUX_E2E=1 + GitHub reachable, hiding it from default CI (the INCIDENT)"
        )

    # 3) The plugin-cloning tests keep the FULL network gate (opt-in) so default CI stays hermetic.
    for name in _NETWORK_TEST_NAMES:
        fn = getattr(e2e, name)
        assert has_gate(fn, network_gate), (
            f"{name} must keep the opt-in network gate (@_requires_tmux_e2e) — it clones plugins"
        )

    # 3a) Pin the network gate's CONDITION, not just its identity: a weakened gate (e.g. dropping the
    #     opt-in/network operands so it skips only on tmux-absence) is still a distinct MarkDecorator
    #     and would pass the identity checks above — yet would run the plugin-cloning tests in default
    #     hermetic CI, the exact bug class this guard exists to catch. The gate skips iff
    #     `not _NETWORK_E2E_AVAILABLE`, and `_NETWORK_E2E_AVAILABLE` requires the opt-in flag FIRST.
    #     So WITHOUT the flag (the default CI path) the gate MUST evaluate to "skip"; we only assert
    #     this in that path — under RIG_TMUX_E2E=1 the gate legitimately allows the tests to run.
    if not e2e._E2E_OPTED_IN:
        network_skip_condition = network_gate.mark.args[0]
        assert network_skip_condition is True, (
            "the @_requires_tmux_e2e gate no longer skips without RIG_TMUX_E2E — its opt-in/network "
            "condition was weakened, which would run the plugin-cloning tests in default hermetic CI"
        )
        assert e2e._NETWORK_E2E_AVAILABLE is False, (
            "_NETWORK_E2E_AVAILABLE is True without RIG_TMUX_E2E set — the opt-in gate is broken"
        )

    # 4) EXHAUSTIVE: every `test_*` in the e2e module is classified into exactly one bucket and
    #    gated to match — so a NEWLY added test that forgets a gate (the general form of the bug: an
    #    ungated test either fails on a tmux-less runner or runs unexpectedly) fails HERE. A new test
    #    must be added to _TMUX_ONLY_TEST_NAMES / _NETWORK_TEST_NAMES (and decorated), or, if it is a
    #    pure hermetic test, to _HERMETIC_EXEMPT_TEST_NAMES (the escape hatch).
    classified = _TMUX_ONLY_TEST_NAMES | _NETWORK_TEST_NAMES | _HERMETIC_EXEMPT_TEST_NAMES
    for name in dir(e2e):
        if not name.startswith("test_"):
            continue
        obj = getattr(e2e, name)
        if not callable(obj):
            continue
        # Only classify functions DEFINED in the e2e module — `dir()` also surfaces IMPORTED names
        # (e.g. a `from … import test_helper`), and classifying those would false-trip the check.
        if getattr(obj, "__module__", None) != e2e.__name__:
            continue
        assert name in classified, (
            f"{name} in test_tmux_e2e.py is unclassified — add it to _TMUX_ONLY_TEST_NAMES, "
            f"_NETWORK_TEST_NAMES (and decorate it), or _HERMETIC_EXEMPT_TEST_NAMES if it needs "
            f"neither tmux nor network (INCIDENT 2026-06-17 follow-up a)"
        )

    # 4a) REVERSE-exhaustive: every name in the classification sets must still exist as a `test_*`
    #     in the module — so a RENAME (which the forward check above flags only as a NEW unclassified
    #     name, never as a now-stale old entry) doesn't leave dead names silently accumulating in the
    #     sets, which would then mis-gate the renamed test or mask a real gap.
    for name in classified:
        obj = getattr(e2e, name, None)
        assert callable(obj) and getattr(obj, "__module__", None) == e2e.__name__, (
            f"classified name '{name}' is no longer a test defined in test_tmux_e2e.py — it was "
            f"renamed or removed; update the classification set in test_tmux_e2e_gating.py"
        )


def test_ci_has_tmux_so_the_leak_regression_actually_runs():
    """In CI, tmux MUST be on PATH — otherwise the `@_requires_tmux` socket-leak regression and the
    DEFECT-5 boot-cleanup test SILENTLY SKIP and the leak that killed the dev's server goes unguarded
    (INCIDENT 2026-06-17 follow-up a). Correct per-test gating (pinned above) is necessary but NOT
    sufficient: a tmux-only gate still skips on a runner that lacks tmux. The unit CI job
    (`.github/workflows/ci.yml`) installs tmux for exactly this reason; this assertion makes a CI
    image that DROPS that step fail LOUDLY here instead of reverting to a silent skip.

    Gated to CI only (the `CI` env var, set to `true` by GitHub Actions and most runners): a local
    dev machine without tmux still gets the normal `@_requires_tmux` skip, never a spurious failure.
    """
    if os.environ.get("CI", "").strip().lower() not in ("1", "true", "yes"):
        pytest.skip("not in CI — tmux is required only on CI runners (set CI=true to enforce)")
    assert shutil.which("tmux") is not None, (
        "tmux is not installed on this CI runner — the @_requires_tmux socket-leak regression and "
        "the DEFECT-5 boot-cleanup test would SILENTLY SKIP, leaving the leak that killed the dev's "
        "tmux server unguarded (INCIDENT 2026-06-17 follow-up a). Add an `apt-get install -y tmux` "
        "step before pytest in the unit job of .github/workflows/ci.yml."
    )
