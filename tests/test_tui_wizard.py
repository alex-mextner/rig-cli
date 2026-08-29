"""Real, headless-Pilot-driven tests for the `rig init` interactive wizard (riglib/tui/app.py).

Unlike test_stack.py's monkeypatched-`run_wizard` coverage (which never mounts a real widget
tree), these actually build the RigWizard App and drive it with Textual's test Pilot — so a
layout/wiring regression in compose()/on_mount()/action_apply() gets caught, not just a broken
call signature.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

# minversion (not a bare presence check): a review finding — on an environment with textual
# BELOW the wizard's own floor (riglib/doctor.py's `_TEXTUAL_MIN_VERSION`), this whole module
# would otherwise fail hard with a real TypeError (`Button(tooltip=...)`, `refresh_bindings`)
# instead of skipping — the exact crash-on-launch class the floor exists to prevent,
# reproduced inside the test suite meant to guard against it.
pytest.importorskip("textual", minversion="0.66")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


@pytest.fixture
def wizard_env(tmp_path, monkeypatch, fake_agent_tools):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    return _repo(tmp_path)


def _slow_run_plan(plan, *, on_start=None, progress=None):  # noqa: ANN001, ARG001
    """A fake `run_plan` that takes 1s -- long enough that a test driving a real keypress or
    click mid-apply is guaranteed to land while the worker is still in flight, rather than
    racing the real fake-catalog plan (which can finish within a single `pilot.pause()`). Was
    0.3s; a review finding flagged that margin as tight on a loaded CI runner (two awaits
    between `action_apply()` and the "still applying" assertion) -- bumped to fail rather than
    pass vacuously if the margin is ever eaten again."""
    import time

    class _FakeAction:
        category = "skills"
        item = "demo"

    class _FakeResult:
        action = _FakeAction()
        status = "created"
        detail = "installed"

    class _FakeReport:
        results = [_FakeResult()]

        def summary(self):
            return {"created": 1}

    action = _FakeAction()
    if on_start:
        on_start(action)
    time.sleep(1)
    if progress:
        progress(_FakeResult())
    return _FakeReport()


# No pytest-asyncio/anyio in this repo's test deps — drive each Pilot session through a plain
# sync test via asyncio.run() rather than adding a new dependency for one test file.


def test_wizard_mounts_and_previews_plan(wizard_env) -> None:
    """On mount, the plan-preview panel (previously a static one-line blurb) shows a real
    resolved action count instead of staying blank — the fix for "lots of empty space"."""
    from riglib.tui.app import _build_wizard_class
    from textual.widgets import Static

    async def _run() -> None:
        app = _build_wizard_class()(wizard_env)
        async with app.run_test() as pilot:
            await pilot.pause()
            preview = app.query_one("#desc-preview", Static)
            text = str(preview.content)
            assert "action(s) on Apply" in text
            assert "(none)" not in text  # the fake catalog has real skills/hooks/ci/mcp items

    asyncio.run(_run())


def test_wizard_preview_updates_on_toggle(wizard_env) -> None:
    """Unchecking every category shrinks the previewed plan (some baseline actions — repo
    registration, gh/github wiring, etc. — aren't gated by these 5 category checkboxes, so
    the count doesn't hit zero) — proves the preview is live-wired to the selection list, not
    a static string computed once at mount."""
    from riglib.tui.app import _build_wizard_class
    from textual.widgets import SelectionList, Static

    async def _run() -> None:
        app = _build_wizard_class()(wizard_env)
        async with app.run_test() as pilot:
            await pilot.pause()
            preview = app.query_one("#desc-preview", Static)
            before = str(preview.content)
            before_count = int(before.split(" action(s)")[0].split("]")[-1].strip())

            cats = app.query_one("#cats", SelectionList)
            for value in list(cats.selected):
                cats.deselect(value)
            await pilot.pause()

            after = str(preview.content)
            after_count = int(after.split(" action(s)")[0].split("]")[-1].strip())
            assert after_count < before_count, f"{before!r} -> {after!r} did not shrink"
            # category-gated item kinds must be gone once every category is unchecked
            for gone in ("skills×", "agent_hooks×", "ci×", "mcp×"):
                assert gone not in after, f"{gone!r} still present in {after!r}"

    asyncio.run(_run())


def test_apply_runs_off_the_ui_thread_and_streams_progress(wizard_env) -> None:
    """The regression this whole change exists for: Apply used to call run_plan() directly
    inside the synchronous button handler, so Textual never got a chance to repaint until the
    ENTIRE run finished — progress was invisible, then dumped all at once. Now it runs in a
    worker thread; assert the buttons disable immediately and the log ends up populated with
    real per-action lines once the worker completes, proving the callback wiring is intact.
    """
    from riglib.tui.app import _build_wizard_class
    from textual.widgets import Button, RichLog

    async def _run() -> None:
        app = _build_wizard_class()(wizard_env)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_apply()
            # immediately after kicking off Apply, buttons must already be disabled — this is
            # only observable because Apply no longer blocks the event loop synchronously.
            assert app.query_one("#btn-apply", Button).disabled is True
            assert app.query_one("#btn-export", Button).disabled is True
            assert app._applying is True

            for _ in range(200):
                await pilot.pause(0.05)
                if not app._applying:
                    break
            assert app._applying is False, "worker never signalled completion"
            assert app.query_one("#btn-apply", Button).disabled is False

            log_text = "\n".join(str(line) for line in app.query_one("#log", RichLog).lines)
            assert "applying" in log_text
            assert "done" in log_text

            # Apply always writes rig.yaml before running the plan
            assert (app.repo_root / "rig.yaml").is_file()

    asyncio.run(_run())


def test_apply_is_a_noop_while_already_running(wizard_env) -> None:
    """A second Apply press (double-click, or the 'a' keybind repeated) while one run is still
    in flight must not kick off a second overlapping worker."""
    from riglib.tui.app import _build_wizard_class

    async def _run() -> None:
        app = _build_wizard_class()(wizard_env)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_apply()
            assert app._applying is True
            workers_before = len(app.workers)
            app.action_apply()  # should be a no-op — _applying guard
            assert len(app.workers) == workers_before

            for _ in range(200):
                await pilot.pause(0.05)
                if not app._applying:
                    break
            assert app._applying is False

    asyncio.run(_run())


async def _wait_until_idle(pilot, app, rounds: int = 200) -> None:
    for _ in range(rounds):
        await pilot.pause(0.05)
        if not app._applying:
            return
    raise AssertionError("apply never finished")


def test_quit_and_export_are_blocked_while_applying(wizard_env, monkeypatch) -> None:
    """Regression for a real finding from review: before this guard, Quit/Export were only
    disabled visually (the button), but the underlying action + keybinding stayed live. A user
    pressing 'q' or 'x' mid-apply could tear the app down mid-install or overwrite the
    rig.yaml/backup an in-flight Apply just wrote. Both actions must be real no-ops while
    `_applying` is True, not just cosmetically disabled buttons."""
    from riglib.tui.app import _build_wizard_class

    exit_calls = []

    async def _run() -> None:
        app = _build_wizard_class()(wizard_env)
        monkeypatch.setattr(app, "exit", lambda *a, **k: exit_calls.append((a, k)))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_apply()
            assert app._applying is True
            assert app.query_one("#cats").disabled is True

            export_calls = []
            monkeypatch.setattr(app, "_do_export", lambda: export_calls.append(1))
            app.action_export()
            assert export_calls == [], "export ran while an apply was in flight"

            app.action_quit()
            assert exit_calls == [], "quit tore the app down while an apply was in flight"

            await _wait_until_idle(pilot, app)
            assert app.query_one("#cats").disabled is False

            # now that it's idle, both actions must work again
            app.action_export()
            assert export_calls == [1]
            app.action_quit()
            assert len(exit_calls) == 1

    asyncio.run(_run())


def test_apply_exception_does_not_wedge_the_wizard(wizard_env, monkeypatch) -> None:
    """Regression for the review finding that `_run_plan_worker` had no try/finally: a
    `run_plan` that raises (a backend bug, a re-raised OS error) must still re-enable the
    controls and clear `_applying` -- not leave the wizard permanently stuck with every
    button disabled and no way to retry or even quit cleanly."""
    from riglib.tui import app as app_module
    from textual.widgets import Button, RichLog

    def _boom(plan, *, on_start=None, progress=None):  # noqa: ANN001, ARG001
        raise RuntimeError("simulated backend failure")

    async def _run() -> None:
        app = app_module._build_wizard_class()(wizard_env)
        monkeypatch.setattr(app_module, "run_plan", _boom)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_apply()
            assert app._applying is True

            await _wait_until_idle(pilot, app)
            assert app.query_one("#btn-apply", Button).disabled is False
            assert app.query_one("#btn-quit", Button).disabled is False
            # the user-facing half of this path: not just "controls re-enable", but the
            # failure itself must actually be visible in the log, not swallowed silently.
            log_text = "\n".join(str(line) for line in app.query_one("#log", RichLog).lines)
            assert "apply failed" in log_text
            assert "simulated backend failure" in log_text

            # a second Apply must actually run again — proves the guard cleared for real,
            # not just that the flag happened to flip without re-enabling controls.
            app.action_apply()
            assert app._applying is True
            # let it finish INSIDE run_test()'s context — leaving one in flight and exiting
            # the `async with` block races the worker's call_from_thread finally-clause
            # against app teardown (a review finding on its own).
            await _wait_until_idle(pilot, app)

    asyncio.run(_run())


def test_apply_streams_progress_before_completion(wizard_env, monkeypatch) -> None:
    """Prove the fix is really live streaming, not just a fast synchronous run that merely
    LOOKS async: a slowed-down fake run_plan must let the test observe a per-action '…' line
    in the log WHILE `_applying` is still True and before 'done' appears -- the old
    synchronous code produced the exact same final log text, so only an interleaved
    observation (not just the end state) distinguishes the two."""
    from riglib.tui import app as app_module
    from textual.widgets import RichLog

    async def _run() -> None:
        app = app_module._build_wizard_class()(wizard_env)
        monkeypatch.setattr(app_module, "run_plan", _slow_run_plan)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_apply()

            mid_flight_saw_progress = False
            for _ in range(60):
                await pilot.pause(0.02)
                log_text = "\n".join(str(line) for line in app.query_one("#log", RichLog).lines)
                if app._applying and "skills/demo" in log_text and "done" not in log_text:
                    mid_flight_saw_progress = True
                    break
            assert mid_flight_saw_progress, "never observed a live in-flight progress line"

            await _wait_until_idle(pilot, app)
            log_text = "\n".join(str(line) for line in app.query_one("#log", RichLog).lines)
            assert "done" in log_text

    asyncio.run(_run())


def test_preview_reports_missing_catalog_without_crashing(tmp_path, monkeypatch) -> None:
    """When agent-tools can't be found at all, the preview panel must say so plainly instead
    of silently staying blank or raising out of on_mount()."""
    from riglib.tui.app import _build_wizard_class
    from textual.widgets import Static

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(tmp_path / "does-not-exist"))
    repo = _repo(tmp_path)

    async def _run() -> None:
        app = _build_wizard_class()(repo)
        assert app._catalog is None
        async with app.run_test() as pilot:
            await pilot.pause()
            text = str(app.query_one("#desc-preview", Static).content)
            assert "cannot preview plan" in text

    asyncio.run(_run())


def test_preview_error_with_brackets_does_not_raise_markup_error(wizard_env, monkeypatch) -> None:
    """Regression for the review finding that an exception message containing bracket syntax
    would be parsed as Rich/Textual markup and raise `MarkupError` out of the "never crashes"
    preview path. `['skills', 'ci']` (a Python list repr) is NOT actually tag-shaped and never
    raised even unescaped — confirmed empirically against the installed Textual (8.2.8) before
    writing this test, which is exactly the trap the original version of this test fell into
    (review finding: "the bracket regression test doesn't test the escape"). `[/]` (an
    auto-closing tag with nothing open) DOES raise `MarkupError` when unescaped, confirmed the
    same way — that's the string used here.
    """
    from riglib.tui import app as app_module

    def _boom_validate(data):  # noqa: ANN001, ARG001
        raise ValueError("bad state near [/] in the config")

    async def _run() -> None:
        app = app_module._build_wizard_class()(wizard_env)
        monkeypatch.setattr(app_module, "validate", _boom_validate)
        async with app.run_test() as pilot:
            await pilot.pause()  # on_mount() calls _update_preview() — must not raise
            from textual.widgets import Static

            text = str(app.query_one("#desc-preview", Static).content)
            assert "plan preview unavailable" in text
            assert "[/]" in text

    asyncio.run(_run())


def test_export_refuses_invalid_config_like_preview_and_apply_do(wizard_env, monkeypatch) -> None:
    """Regression for the review finding that Export bypassed validate()/build() entirely:
    it could write an invalid rig.yaml to disk with a green checkmark while the preview panel
    right next to it was already saying "unavailable" for the exact same config. Export must
    now refuse the same way Preview/Apply do, and never write the file."""
    from riglib.tui import app as app_module
    from textual.widgets import RichLog

    def _boom_validate(data):  # noqa: ANN001, ARG001
        raise ValueError("simulated invalid config")

    async def _run() -> None:
        app = app_module._build_wizard_class()(wizard_env)
        monkeypatch.setattr(app_module, "validate", _boom_validate)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_export()
            log_text = "\n".join(str(line) for line in app.query_one("#log", RichLog).lines)
            assert "config error" in log_text
            assert "simulated invalid config" in log_text
            assert not (app.repo_root / "rig.yaml").is_file(), "export wrote an invalid config"

    asyncio.run(_run())


def test_apply_refuses_invalid_config_without_starting_a_worker(wizard_env, monkeypatch) -> None:
    """The mirror image of the Export tests: `action_apply()`'s own docstring says "validate
    + build the plan BEFORE writing rig.yaml — never leave a bad committed config behind a
    failed apply", but only Export had a test for the reject path. A raising validate() must
    log `config error`, write no rig.yaml, start no worker, and leave `_applying` False with
    the controls still enabled."""
    from riglib.tui import app as app_module
    from textual.widgets import Button, RichLog

    def _boom_validate(data):  # noqa: ANN001, ARG001
        raise ValueError("simulated invalid config")

    async def _run() -> None:
        app = app_module._build_wizard_class()(wizard_env)
        monkeypatch.setattr(app_module, "validate", _boom_validate)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_apply()

            log_text = "\n".join(str(line) for line in app.query_one("#log", RichLog).lines)
            assert "config error" in log_text
            assert "simulated invalid config" in log_text
            assert not (app.repo_root / "rig.yaml").is_file(), "apply wrote an invalid config"
            assert app._applying is False, "a rejected config must never start a worker"
            assert app.query_one("#btn-apply", Button).disabled is False

    asyncio.run(_run())


def test_export_refuses_invalid_config_even_without_a_catalog(tmp_path, monkeypatch) -> None:
    """Regression for a review finding on the FIRST export fix: it only routed through
    validation when `self._catalog` was found, so an invalid config with agent-tools ALSO
    missing still got a silent free pass straight to state.write(). `_sync_and_validate()` is
    catalog-independent, so this must refuse the same way regardless."""
    from riglib.tui import app as app_module
    from textual.widgets import RichLog

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(tmp_path / "does-not-exist"))
    repo = _repo(tmp_path)

    def _boom_validate(data):  # noqa: ANN001, ARG001
        raise ValueError("simulated invalid config")

    async def _run() -> None:
        app = app_module._build_wizard_class()(repo)
        assert app._catalog is None  # the exact combination the original fix missed
        monkeypatch.setattr(app_module, "validate", _boom_validate)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_export()
            log_text = "\n".join(str(line) for line in app.query_one("#log", RichLog).lines)
            assert "config error" in log_text
            assert not (app.repo_root / "rig.yaml").is_file(), "export wrote an invalid config"

    asyncio.run(_run())


def test_quit_keybinding_is_a_real_noop_while_applying(wizard_env, monkeypatch) -> None:
    """Regression for the review finding that every test drove `_applying`/quit/export
    through DIRECT method calls, which bypass Textual's action-dispatch/binding machinery
    entirely — so a drift between `BINDINGS` and `check_action`'s hard-coded action-name
    tuple would go undetected. Drive it through the REAL keypress path instead.

    Uses the slow fake `run_plan` rather than the real fake-catalog plan: a review finding
    caught that with the FAST real plan, the worker can finish (and `_finish_apply` can be
    serviced) before the keypress even lands, making the "blocked" assertion pass vacuously
    -- because there was nothing left running to block, not because the guard did its job.
    The slow fake guarantees the run is still genuinely in flight when 'q' is pressed.
    """
    from riglib.tui import app as app_module

    exit_calls = []

    async def _run() -> None:
        app = app_module._build_wizard_class()(wizard_env)
        monkeypatch.setattr(app_module, "run_plan", _slow_run_plan)
        monkeypatch.setattr(app, "exit", lambda *a, **k: exit_calls.append((a, k)))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_apply()
            assert app._applying is True

            await pilot.press("q")
            await pilot.pause()
            assert app._applying is True, "the run finished before the keypress could be tested"
            assert exit_calls == [], "the real 'q' keypress tore the app down mid-apply"

            await _wait_until_idle(pilot, app)
            await pilot.press("q")
            await pilot.pause()
            assert len(exit_calls) == 1, "quit stopped working once idle again"

    asyncio.run(_run())


def test_apply_button_click_starts_a_worker(wizard_env, monkeypatch) -> None:
    """Regression for the review finding that no test exercised the actual button-click ->
    on_button_pressed -> action_apply path (every other test calls action_apply() directly).
    A real mouse click on the Apply button must start the same worker — proven by spying on
    `run_worker` rather than snapshotting `_applying` right after the click, since (as with
    the keypress test above) the fake catalog's plan can finish within the same event-loop
    tick a `pilot.pause()` processes."""
    from riglib.tui.app import _build_wizard_class

    calls = []

    async def _run() -> None:
        app = _build_wizard_class()(wizard_env)
        real_run_worker = app.run_worker
        monkeypatch.setattr(
            app, "run_worker", lambda *a, **k: (calls.append(1), real_run_worker(*a, **k))[1]
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#btn-apply")
            await pilot.pause()
            assert calls, "clicking Apply never called run_worker — action_apply didn't fire"

            await _wait_until_idle(pilot, app)

    asyncio.run(_run())


def test_footer_actions_greyed_out_while_applying(wizard_env) -> None:
    """`check_action` must grey out quit/export/apply in the footer while `_applying` is
    True (visible-but-disabled, per Textual's `None` return), and go back to fully enabled
    once the run finishes — the passive UX signal the review asked for so a blocked
    keybinding reads as "busy right now", not as if the app ignored the key entirely."""
    from riglib.tui.app import _build_wizard_class

    async def _run() -> None:
        app = _build_wizard_class()(wizard_env)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.check_action("quit", ()) is True
            assert app.check_action("export", ()) is True
            assert app.check_action("apply", ()) is True

            app.action_apply()
            assert app.check_action("quit", ()) is None
            assert app.check_action("export", ()) is None
            assert app.check_action("apply", ()) is None

            await _wait_until_idle(pilot, app)
            assert app.check_action("quit", ()) is True
            assert app.check_action("export", ()) is True
            assert app.check_action("apply", ()) is True

    asyncio.run(_run())


def test_every_binding_is_covered_by_the_busy_guard(wizard_env) -> None:
    """Pins the drift class the review flagged: `check_action` blocks a hard-coded set of
    action names (`_BUSY_BLOCKED_ACTIONS`), separate from `BINDINGS`. A future binding added
    to one without the other would silently stay live mid-apply. Every action currently bound
    in `BINDINGS` mutates state or tears the app down, so every one of them belongs in the
    guarded set today — if that stops being true for a NEW binding, this test is exactly
    where to update the exemption, not a place it should fail unnoticed."""
    from riglib.tui.app import _build_wizard_class

    app = _build_wizard_class()(wizard_env)
    bound_actions = {binding[1] for binding in app.BINDINGS}
    assert bound_actions == set(app._BUSY_BLOCKED_ACTIONS)


def test_export_keybinding_is_a_real_noop_while_applying(wizard_env, monkeypatch) -> None:
    """The 'x' sibling of test_quit_keybinding_is_a_real_noop_while_applying — the review
    noted only 'q' was exercised via a real keypress, leaving 'x'/export's binding-vs-guard
    wiring covered only by direct method calls. Uses the slow fake `run_plan` for the same
    reason as the 'q' test: the real fake-catalog plan can finish before the keypress lands,
    which would make the "blocked" assertion pass vacuously rather than for real."""
    from riglib.tui import app as app_module

    export_calls = []

    async def _run() -> None:
        app = app_module._build_wizard_class()(wizard_env)
        monkeypatch.setattr(app_module, "run_plan", _slow_run_plan)
        monkeypatch.setattr(app, "_do_export", lambda: export_calls.append(1))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_apply()
            assert app._applying is True

            await pilot.press("x")
            await pilot.pause()
            assert app._applying is True, "the run finished before the keypress could be tested"
            assert export_calls == [], "the real 'x' keypress exported mid-apply"

            await _wait_until_idle(pilot, app)
            await pilot.press("x")
            await pilot.pause()
            assert export_calls == [1], "export stopped working once idle again"

    asyncio.run(_run())


def test_apply_recovers_if_run_worker_itself_raises(wizard_env, monkeypatch) -> None:
    """Regression for a review finding: the try/finally that clears `_applying` lives INSIDE
    `_run_plan_worker`'s body, which only runs if `run_worker()` itself successfully launches
    a thread. If `run_worker()` raises synchronously (before the worker ever starts), that
    safety net never engages — `_applying` would stay True and every control would stay
    disabled forever with no way to recover, not even by retrying."""
    from riglib.tui import app as app_module
    from textual.widgets import Button

    def _boom_run_worker(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise RuntimeError("simulated Textual internal failure")

    async def _run() -> None:
        app = app_module._build_wizard_class()(wizard_env)
        monkeypatch.setattr(app, "run_worker", _boom_run_worker)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_apply()
            await pilot.pause()

            assert app._applying is False, "a launch-time failure left _applying stuck True"
            assert app.query_one("#btn-apply", Button).disabled is False
            assert app.query_one("#btn-quit", Button).disabled is False

    asyncio.run(_run())


def test_apply_recovers_if_set_controls_enabled_raises_before_launch(wizard_env, monkeypatch) -> None:
    """Regression for a review finding (GLM, round 23): `_applying = True` and
    `_set_controls_enabled(False)` used to run BEFORE the try/except guarding
    `run_worker()`, not inside it -- so if `_set_controls_enabled(False)` itself raised
    (e.g. a `NoMatches` query error during a screen-teardown race), the exception
    propagated straight out of `action_apply` uncaught, with `_applying` already `True`
    and no `except` in scope to recover it. Every control would stay wedged behind the
    apply-in-flight guard forever -- the same wedge class `test_apply_recovers_if_run_worker_itself_raises`
    already covers for `run_worker` itself. Simulates exactly that: the FIRST
    `_set_controls_enabled` call (disabling controls before launch) raises; the SECOND
    call (`_finish_apply`'s own re-enable, during recovery) succeeds normally, proving the
    recovery path itself still works even though the disable call failed."""
    from riglib.tui import app as app_module
    from textual.widgets import Button

    async def _run() -> None:
        app = app_module._build_wizard_class()(wizard_env)
        real_set_controls_enabled = app._set_controls_enabled
        call_count = {"n": 0}

        def _flaky_set_controls_enabled(enabled):  # noqa: ANN001
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated NoMatches during a screen-teardown race")
            return real_set_controls_enabled(enabled)

        monkeypatch.setattr(app, "_set_controls_enabled", _flaky_set_controls_enabled)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_apply()
            await pilot.pause()

            assert app._applying is False, (
                "a pre-launch _set_controls_enabled failure left _applying stuck True"
            )
            assert app.query_one("#btn-apply", Button).disabled is False
            assert app.query_one("#btn-quit", Button).disabled is False
            assert call_count["n"] == 2, "expected exactly one failed call plus one recovery call"

    asyncio.run(_run())


def test_backup_preserves_source_mode_and_mtime(tmp_path) -> None:
    """`_atomic_timestamped_backup` hand-rolls the metadata half of `shutil.copy2` it replaced
    (`os.fchmod`/`os.utime` through the claimed fd instead of a path-based copy) -- every other
    backup test asserts only content and paths, so a future cleanup that drops those two lines
    (or loses the `& 0o777` discipline) would ship fully green while silently regressing
    restore-point fidelity versus the `copy2` behavior this replaced."""
    from riglib.tui.app import _atomic_timestamped_backup

    target = tmp_path / "rig.yaml"
    target.write_text("original: true\n", encoding="utf-8")
    target.chmod(0o600)
    known_ns = 1_700_000_000_000_000_000  # an arbitrary, fixed ns-resolution timestamp
    os.utime(target, ns=(known_ns, known_ns))

    bak = _atomic_timestamped_backup(target)

    src_stat = target.stat()
    bak_stat = bak.stat()
    assert bak_stat.st_mode & 0o777 == src_stat.st_mode & 0o777 == 0o600
    assert bak_stat.st_mtime_ns == src_stat.st_mtime_ns == known_ns


def test_export_then_apply_backups_do_not_collide(wizard_env) -> None:
    """Regression for a review finding: Export immediately followed by Apply (the exact flow
    the Export button's own tooltip recommends — "inspect ... before applying") can land both
    backups within the same second-resolution timestamp. Without a disambiguator, the second
    backup would silently overwrite the first via shutil.copy2, discarding whatever config was
    backed up first. Both backups must survive as distinct files."""
    from riglib.tui.app import _build_wizard_class

    async def _run() -> None:
        app = _build_wizard_class()(wizard_env)
        rig_yaml = app.repo_root / "rig.yaml"
        async with app.run_test():
            rig_yaml.write_text("original: true\n", encoding="utf-8")
            app._backup_existing_config()
            rig_yaml.write_text("second: true\n", encoding="utf-8")
            app._backup_existing_config()

        backups = sorted(app.repo_root.glob("rig.yaml.rig-bak-*"))
        assert len(backups) == 2, f"expected 2 distinct backups, found {[b.name for b in backups]}"
        contents = {b.read_text(encoding="utf-8") for b in backups}
        assert contents == {"original: true\n", "second: true\n"}, "a backup was overwritten"

    asyncio.run(_run())


def test_concurrent_backups_never_clobber_each_other(tmp_path) -> None:
    """Regression for a real TOCTOU race a review caught: the original `while bak.exists():
    ...` disambiguation loop checks for a free name and only THEN copies -- two callers (e.g.
    two wizard instances, or Export racing an in-flight Apply's own backup) can both observe
    the same name as free and one's copy2() clobbers the other's, discarding whichever config
    was backed up first. The fix (`_atomic_timestamped_backup`) claims the filename atomically
    with O_CREAT|O_EXCL, which fails outright if another caller already won that exact name.

    Exercises the pure stdlib helper directly with a real threading.Barrier so both threads hit
    `os.open` in the same instant, every run -- a bare sequential call (like the test above)
    can never actually exercise the race window. Deliberately does NOT go through the wizard's
    `_backup_existing_config`/RichLog logging, since concurrent writes to a live Textual widget
    from raw threads (rather than via `call_from_thread`) would be a second, unrelated hazard
    this test isn't meant to probe."""
    import threading

    from riglib.tui.app import _atomic_timestamped_backup

    target = tmp_path / "rig.yaml"
    target.write_text("original: true\n", encoding="utf-8")

    barrier = threading.Barrier(2)
    results: list[Path] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def _race() -> None:
        try:
            barrier.wait(timeout=5)
            bak = _atomic_timestamped_backup(target)
            with lock:
                results.append(bak)
        except Exception as exc:  # noqa: BLE001 — surface any thread failure to the test
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_race) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"backup thread(s) raised: {errors}"
    assert len(results) == 2 and len(set(results)) == 2, f"expected 2 distinct paths, got {results}"
    for bak in results:
        assert bak.is_file()
        assert bak.read_text(encoding="utf-8") == "original: true\n", "a backup was clobbered"


def test_backup_write_never_follows_a_swapped_symlink(tmp_path, monkeypatch) -> None:
    """Regression for a review finding on the FIRST version of the atomic-claim fix: it
    claimed the backup name with O_CREAT|O_EXCL but then closed that fd and let shutil.copy2
    REOPEN the path -- a narrow TOCTOU window where a concurrent process with write access to
    the directory could unlink the just-claimed name and replace it with a symlink before that
    reopen, and copy2 would follow the symlink and overwrite whatever it points at.

    Simulates exactly that attacker step inside the one path-based `os.open` call the fixed
    function makes (there's only one -- the atomic claim itself; everything after writes
    through the returned fd, never re-resolving the path except for the post-write identity
    check). A SECOND review round caught that just writing through the fd wasn't enough on
    its own: without verifying the returned path still names the inode just written to, the
    function would silently hand back a "successful" backup path pointing at the swapped
    symlink instead -- so it must now raise instead of returning. The property that must hold
    either way: an arbitrary file elsewhere on disk (`victim`, standing in for something like
    the user's SSH key or another repo's file) is NEVER written to, no matter what the path
    is swapped to after the claim.
    """
    from riglib.tui import app as app_module

    target = tmp_path / "rig.yaml"
    target.write_text("original: true\n", encoding="utf-8")
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch\n", encoding="utf-8")

    real_open = os.open
    swapped_once = {"done": False}

    def _open_then_swap_to_symlink(path, flags, mode=0o777):
        fd = real_open(path, flags, mode)
        if not swapped_once["done"] and (flags & os.O_EXCL):
            swapped_once["done"] = True
            os.unlink(path)
            os.symlink(str(victim), path)
        return fd

    monkeypatch.setattr(app_module.os, "open", _open_then_swap_to_symlink)
    with pytest.raises(RuntimeError, match="replaced during write"):
        app_module._atomic_timestamped_backup(target)

    assert swapped_once["done"], "the simulated attack never actually ran"
    assert victim.read_text(encoding="utf-8") == "do not touch\n", (
        "the backup write followed the swapped symlink and clobbered an unrelated file"
    )
    # A FIFTH review round found that cleaning up the swapped-in symlink here was itself a
    # (narrower) stat-then-unlink TOCTOU: the type snapshot the cleanup decision relied on
    # could go stale between the check and the unlink. The mismatch branch now never touches
    # the path again once a swap is detected -- the attacker's symlink is deliberately left
    # in place (a stray backup-shaped name is a cheap, inert cost).
    leftover_symlinks = [p for p in tmp_path.glob("rig.yaml.rig-bak-*") if p.is_symlink()]
    assert len(leftover_symlinks) == 1, (
        f"expected the untouched attacker symlink to remain, got {leftover_symlinks}"
    )


def test_backup_identity_check_runs_before_the_destination_fd_closes(tmp_path, monkeypatch) -> None:
    """Regression for a review finding (k3, round 18): an earlier version of the post-write
    identity check ran `os.stat(bak)` AFTER `with dst:` had already closed the destination
    fd -- releasing the inode-reuse pin invariants 2/3 otherwise rely on. In that close->stat
    window, a concurrent unlink+recreate at the same path can be allocated the just-freed
    inode number on filesystems that prefer immediate reuse (ext4 notably), making the
    identity check pass against a file this call never wrote -- directly contradicting
    invariant 3. Real inode reuse can't be forced portably in a test (filesystem/OS-dependent
    allocator behavior), so this pins the ORDERING invariant directly instead: the
    identity-check `os.stat` call must observe the destination fd as still open, proving our
    own inode cannot have been freed for reuse by the time the check runs."""
    from riglib.tui import app as app_module

    target = tmp_path / "rig.yaml"
    target.write_text("original: true\n", encoding="utf-8")

    real_fdopen = os.fdopen
    real_stat = os.stat
    state: dict[str, object] = {"dst": None, "stat_saw_fd_open": None}

    def _spy_fdopen(fd, *args, **kwargs):
        f = real_fdopen(fd, *args, **kwargs)
        state["dst"] = f
        return f

    def _spy_stat(path, *args, **kwargs):
        dst = state["dst"]
        if dst is not None and str(path) != str(target):
            state["stat_saw_fd_open"] = not dst.closed
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(app_module.os, "fdopen", _spy_fdopen)
    monkeypatch.setattr(app_module.os, "stat", _spy_stat)

    app_module._atomic_timestamped_backup(target)

    assert state["stat_saw_fd_open"] is True, (
        "the identity check ran AFTER the destination fd was already closed -- "
        "reopens the inode-reuse TOCTOU window this fix closes"
    )


def test_export_happy_path_writes_config_and_backs_up(wizard_env) -> None:
    """Every other Export test covers a REJECTION path (invalid config, missing catalog). Add
    the successful case: a real Export against a valid config must write rig.yaml, back up
    a pre-existing one, and log both with a green checkmark -- the actual "did the feature
    work at all" case, previously only exercised indirectly through Apply's shared
    `_write_config`."""
    from riglib.tui.app import _build_wizard_class
    from textual.widgets import RichLog

    async def _run() -> None:
        app = _build_wizard_class()(wizard_env)
        rig_yaml = app.repo_root / "rig.yaml"
        rig_yaml.write_text("preexisting: true\n", encoding="utf-8")
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_export()
            await pilot.pause()

            assert rig_yaml.is_file()
            assert rig_yaml.read_text(encoding="utf-8") != "preexisting: true\n"
            backups = list(app.repo_root.glob("rig.yaml.rig-bak-*"))
            assert len(backups) == 1
            assert backups[0].read_text(encoding="utf-8") == "preexisting: true\n"

            log_text = "\n".join(str(line) for line in app.query_one("#log", RichLog).lines)
            assert "backed up existing rig.yaml" in log_text
            assert "exported" in log_text

    asyncio.run(_run())


def test_export_refuses_when_build_fails_even_though_validate_passes(wizard_env, monkeypatch) -> None:
    """Regression for a review finding on the SECOND version of the Export fix: routing Export
    through `_sync_and_validate()` alone closed the "validate() rejects it" gap but left a
    narrower one open -- `validate()` can pass schema checks while `build()` still raises (an
    unresolvable by-stack skill/hook, an unsupported project_type combination). With only
    `_sync_and_validate()`, Export would export green with a checkmark for that exact config
    while the preview panel right next to it was already saying "unavailable". Export must now
    resolve the full plan via `_resolve_plan()` whenever a catalog is available, matching
    Preview/Apply exactly, and refuse the same way Preview does."""
    from riglib.tui import app as app_module
    from textual.widgets import RichLog

    def _boom_build(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ARG001
        raise ValueError("simulated build failure")

    async def _run() -> None:
        app = app_module._build_wizard_class()(wizard_env)
        assert app._catalog is not None  # the exact combination the narrower gap needed
        monkeypatch.setattr(app_module, "build", _boom_build)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_export()
            log_text = "\n".join(str(line) for line in app.query_one("#log", RichLog).lines)
            assert "config error" in log_text
            assert "simulated build failure" in log_text
            assert not (app.repo_root / "rig.yaml").is_file(), (
                "export wrote a config that build() itself rejects"
            )

    asyncio.run(_run())


def test_backup_cleanup_never_deletes_a_second_legitimate_claimants_backup(tmp_path, monkeypatch) -> None:
    """Regression for a review finding on the symlink-swap hardening itself: both cleanup
    paths used to `os.unlink(str(bak))` by PATH, which is its own TOCTOU -- if a concurrent
    writer swaps the claimed name (as in the symlink test above) and, before OUR cleanup runs,
    a SECOND legitimate caller claims that now-free exact name and finishes writing their own
    real backup, a blind path-based unlink would delete that second caller's real data. Unlike
    the symlink case (which this function never creates itself, so is unambiguously an
    attacker's artifact), an unrelated REGULAR file at the claimed name is indistinguishable
    from a second legitimate backup -- it must never be deleted, even though the identity
    check still correctly reports "replaced during write" and raises."""
    from riglib.tui import app as app_module

    target = tmp_path / "rig.yaml"
    target.write_text("original: true\n", encoding="utf-8")

    real_open = os.open
    swapped_once = {"done": False}

    def _open_then_swap_to_regular_file(path, flags, mode=0o644):
        fd = real_open(path, flags, mode)
        if not swapped_once["done"] and (flags & os.O_EXCL):
            swapped_once["done"] = True
            os.unlink(path)
            with open(path, "wb") as f:  # simulates a second caller's own real backup
                f.write(b"second legitimate backup\n")
        return fd

    monkeypatch.setattr(app_module.os, "open", _open_then_swap_to_regular_file)
    with pytest.raises(RuntimeError, match="replaced during write"):
        app_module._atomic_timestamped_backup(target)

    assert swapped_once["done"], "the simulated race never actually ran"
    survivors = list(tmp_path.glob("rig.yaml.rig-bak-*"))
    assert len(survivors) == 1, f"expected the second claimant's backup to survive, got {survivors}"
    assert survivors[0].read_bytes() == b"second legitimate backup\n", (
        "the second legitimate claimant's backup was deleted by our cleanup"
    )


def test_backup_name_claim_falls_back_to_random_suffix_after_sequential_collisions(
    tmp_path, monkeypatch
) -> None:
    """Regression for a review finding: the original name-claim retry loop had no upper bound
    -- a directory pre-seeded with every sequential `<stamp>`/`<stamp>-N` name (a stale
    leftover pile, or a directory an adversary deliberately fills) would retry FileExistsError
    forever, spinning the calling thread (the UI thread, for both Export and Apply) with no way
    out. Pre-seed every name the bounded sequential phase would try and confirm the call still
    succeeds -- via the random-suffix fallback -- instead of hanging."""
    from riglib.tui import app as app_module

    target = tmp_path / "rig.yaml"
    target.write_text("original: true\n", encoding="utf-8")
    monkeypatch.setattr(app_module.time, "strftime", lambda _fmt: "20260101-000000")

    for attempt in range(app_module._BACKUP_NAME_SEQUENTIAL_ATTEMPTS):
        suffix = "" if attempt == 0 else f"-{attempt}"
        (tmp_path / f"rig.yaml.rig-bak-20260101-000000{suffix}").write_text(
            "occupied\n", encoding="utf-8"
        )

    bak = app_module._atomic_timestamped_backup(target)

    assert bak.is_file()
    assert bak.read_text(encoding="utf-8") == "original: true\n"
    assert bak.name.startswith("rig.yaml.rig-bak-20260101-000000-")
    # the fallback suffix is random, not the next sequential integer
    next_sequential = f"rig.yaml.rig-bak-20260101-000000-{app_module._BACKUP_NAME_SEQUENTIAL_ATTEMPTS}"
    assert bak.name != next_sequential


def test_backup_name_claim_raises_instead_of_hanging_when_every_attempt_collides(
    tmp_path, monkeypatch
) -> None:
    """The other half of the bounded-retry fix: if EVERY attempt collides -- even past the
    random-suffix fallback, astronomically unlikely in practice, but the loop must still
    terminate -- the call must raise a clear RuntimeError rather than hang forever."""
    from riglib.tui import app as app_module

    target = tmp_path / "rig.yaml"
    target.write_text("original: true\n", encoding="utf-8")

    real_open = os.open

    def _always_collide(path, flags, mode=0o644):
        if flags & os.O_EXCL:
            raise FileExistsError(17, "simulated: every name is taken")
        return real_open(path, flags, mode)

    monkeypatch.setattr(app_module.os, "open", _always_collide)
    with pytest.raises(RuntimeError, match="could not claim a backup name"):
        app_module._atomic_timestamped_backup(target)


def test_apply_leaves_no_worker_started_when_the_backup_step_fails(wizard_env, monkeypatch) -> None:
    """Regression for the untested-but-safety-critical branch two independent review passes
    flagged: `_write_config` converting a backup/write failure into "log + return None" is a
    contract Apply depends on -- Apply must never start the worker (and must leave `_applying`
    False with every control re-enabled) against a config it failed to persist. A regression
    here (e.g. reordering `_applying = True` above the write) would run a full install with no
    committed rig.yaml on disk and nothing in this suite would have caught it."""
    from riglib.tui import app as app_module
    from textual.widgets import Button, RichLog, SelectionList

    def _boom_backup(target):  # noqa: ANN001, ARG001
        raise OSError("simulated unwritable directory")

    async def _run() -> None:
        app = app_module._build_wizard_class()(wizard_env)
        (app.repo_root / "rig.yaml").write_text("preexisting: true\n", encoding="utf-8")
        monkeypatch.setattr(app_module, "_atomic_timestamped_backup", _boom_backup)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_apply()

            log_text = "\n".join(str(line) for line in app.query_one("#log", RichLog).lines)
            assert "failed to write rig.yaml" in log_text
            assert "simulated unwritable directory" in log_text
            assert (app.repo_root / "rig.yaml").read_text(encoding="utf-8") == "preexisting: true\n", (
                "a failed backup step must never let the write through"
            )
            assert app._applying is False, "a failed write must never start the worker"
            assert app.query_one("#btn-apply", Button).disabled is False
            assert app.query_one("#btn-export", Button).disabled is False
            assert app.query_one("#btn-quit", Button).disabled is False
            assert app.query_one("#cats", SelectionList).disabled is False

    asyncio.run(_run())


def test_env_panel_and_catalog_error_survive_bracket_bearing_paths(tmp_path, monkeypatch) -> None:
    """Regression for the review finding that `compose()`'s env-status line and `on_mount()`'s
    catalog-not-found log line both interpolate values that can come from outside the wizard's
    control (a repo path, `$RIG_AGENT_TOOLS_SOURCE`) -- the escaping there was asserted by code
    comment but had no direct test; only the plan-preview error path was actually exercised.
    Give both sinks a value containing `[/]` (confirmed elsewhere in this file to raise
    `MarkupError` when unescaped) and confirm the wizard still mounts."""
    from riglib.tui.app import _build_wizard_class
    from textual.widgets import RichLog, Static

    # "/" is always a real path separator (can't appear inside one directory-name segment on
    # POSIX), so a naive "repo[/]weird" name is unconstructible. A directory named "repo["
    # containing a subdirectory named "]weird" stringifies with the OS's OWN "/" landing
    # right between them -- producing the exact same "[/]" substring in a fully realistic
    # nested path, no monkeypatching of the OS required.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(tmp_path / "agent-tools[" / "]weird"))
    repo = tmp_path / "repo[" / "]odd"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()

    async def _run() -> None:
        app = _build_wizard_class()(repo)
        assert app._catalog is None  # bad_source doesn't exist -> CatalogError, as intended
        async with app.run_test() as pilot:
            await pilot.pause()
            env_text = str(app.query_one("#env", Static).content)
            assert "[/]" in env_text  # rendered literally, not parsed as a closing tag
            log_text = "\n".join(str(line) for line in app.query_one("#log", RichLog).lines)
            assert "agent-tools not found" in log_text
            assert "[/]" in log_text

    asyncio.run(_run())
