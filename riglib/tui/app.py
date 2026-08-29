"""The textual wizard app — a minimal, working v0.1 front-end over the rig engine.

Scope for v0.1 (per build-plan: "a minimal working wizard is acceptable"): one screen
that (1) shows detected environment, (2) lists the five categories as toggles with a
description pane, (3) shows the resolved plan, and (4) on confirm writes rig.yaml and runs
the same headless executor with a streaming log. The deep per-item screens from
tui-design.md are deferred to v0.2 — the engine and config are fully expressive headless,
so the wizard is a convenience, not the source of capability.

**Lazy optional dependency:** ``textual`` is imported ONLY inside the factory that builds
the App class, so this module stays stdlib-importable (the repo rule). Importing
``riglib.tui.app`` never requires textual; only calling ``run_wizard`` does (it raises
``ImportError`` if textual is absent, which the CLI catches and falls back from).
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import time
from pathlib import Path

from ..actions import run_plan
from ..catalog import Catalog, CatalogError
from ..config import LoadedConfig, load, resolve_init_stack, validate
from ..detect import Environment, detect_environment
from ..plan import build
from ..state import SetupState

# _atomic_timestamped_backup's name-claim retry loop is bounded, not infinite: it tries the
# bare/`-N` sequence first (readable names, matches every existing test's expectations), then
# falls back to a random suffix so a directory pre-seeded with every sequential name (a stale
# leftover pile, or a directory an adversary deliberately fills) can't spin the UI thread
# forever — see rig-cli#292 review history.
_BACKUP_NAME_SEQUENTIAL_ATTEMPTS = 20
_BACKUP_NAME_MAX_ATTEMPTS = 200


def _atomic_timestamped_backup(target: Path) -> Path:
    """Copy ``target`` to a fresh ``<target>.rig-bak-<timestamp>[-N]`` and return that path.

    Pure stdlib, no Textual — testable (including under real thread concurrency) without
    building a wizard at all.

    Invariants (each pinned by a dedicated test in ``tests/test_tui_wizard.py``):

    1. The backup NAME is claimed atomically (``O_CREAT | O_EXCL``), never stat-then-write —
       two concurrent callers (two wizard instances, or Export racing an in-flight Apply's
       own backup) can't both observe the same name as free and clobber each other.
    2. The COPY writes through the SAME fd the claim returned, never reopening the path — a
       concurrent unlink+replace (a symlink swap) between the claim and the write can't be
       followed; whatever the write touches is provably the inode this call actually claimed.
    3. A post-write identity check (inode/dev) proves the claimed name still names OUR data
       before this returns it as a trustworthy restore point; a mismatch raises instead of
       silently handing back a path whose data may already be gone.
    4. NO path-based cleanup, ever, once the live claimed fd is no longer held — not on an
       ordinary copy failure, not on a detected identity mismatch. A path is only safe to
       unlink through the SAME fd that claimed it; re-resolving the path later to ask "is
       this still mine" (even backed by an inode check) is itself a fresh TOCTOU once that fd
       is gone — a second legitimate claimant could have taken the freed name in the
       meantime. The cost is a stray partial/empty/swapped-symlink file left behind on
       failure — cheap and inert, versus the alternative of deleting someone else's real
       backup.
    5. The name-claim retry loop is BOUNDED (``_BACKUP_NAME_MAX_ATTEMPTS``): a directory
       pre-seeded with every candidate name can't spin the caller (the UI thread, for both
       Export and Apply) forever — it falls back to a random suffix after the readable
       ``-N`` sequence, then raises rather than hang.

    This function has been through six rounds of adversarial review (fd-lifecycle bugs,
    TOCTOU windows in the write path and in every cleanup attempt, an unbounded retry loop,
    Windows portability) — see the PR/commit history for the specific failure each round
    found rather than re-deriving that archaeology here every time the contract changes.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fd = None
    bak: Path | None = None
    for attempt in range(_BACKUP_NAME_MAX_ATTEMPTS):
        if attempt < _BACKUP_NAME_SEQUENTIAL_ATTEMPTS:
            suffix = "" if attempt == 0 else f"-{attempt}"
        else:
            suffix = f"-{secrets.token_hex(4)}"
        candidate = target.with_name(f"{target.name}.rig-bak-{stamp}{suffix}")
        try:
            # Claim narrow (0o600), then `os.fchmod` up to the SOURCE's real mode once the
            # copy finishes below — against this function's own stated adversarial threat
            # model (a concurrent writer with directory access), claiming world-readable
            # first would briefly expose a restore point that may hold secrets (a hand-edited
            # rig.yaml can carry tokens) before the real permissions land. Costs nothing:
            # nobody is meant to read this file before the copy completes anyway.
            # `O_BINARY` (a no-op / absent everywhere but Windows) guards against the CRT
            # defaulting a raw `os.open` fd to text mode there, which would translate
            # "\n"->"\r\n" while copying YAML through the fd below and corrupt the backup
            # relative to the source — the one other portability class `fchmod`/`utime`
            # above are already guarded for (a review finding).
            fd = os.open(
                str(candidate),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                0o600,
            )
        except FileExistsError:
            continue
        bak = candidate
        break
    if bak is None or fd is None:
        raise RuntimeError(
            f"could not claim a backup name for {target} after "
            f"{_BACKUP_NAME_MAX_ATTEMPTS} attempts — the backup directory may be full of "
            "stale rig-bak files"
        )

    fd_owned_by_fdopen = False
    try:
        with open(str(target), "rb") as src:
            src_stat = os.fstat(src.fileno())
            dst = os.fdopen(fd, "wb")
            fd_owned_by_fdopen = True  # `dst`'s own `with` now owns closing `fd`
            with dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
                # `os.fchmod` doesn't exist on Windows before Python 3.13 (this project
                # targets >=3.10) — the `shutil.copy2` this replaced was portable, so guard
                # rather than let a real Windows install hard-fail every Export/Apply against
                # a pre-existing rig.yaml (a review finding).
                if hasattr(os, "fchmod"):
                    os.fchmod(dst.fileno(), src_stat.st_mode & 0o777)
                # `os.utime` accepting a raw fd (rather than a path) is POSIX-only in CPython
                # too — same portability class as `fchmod` above (a review finding caught
                # this one was still unguarded). `os.supports_fd` is the documented way to
                # check per-function fd support.
                if os.utime in os.supports_fd:
                    os.utime(dst.fileno(), ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
                dst_stat = os.fstat(dst.fileno())
                # The identity check MUST run HERE, while `dst`'s fd is still open — a
                # review finding (k3, round 18): an earlier version ran this `os.stat(bak)`
                # AFTER `with dst:` below had already closed the fd, which releases the
                # inode-reuse pin. In that close→stat window, a concurrent unlink+recreate
                # at the same path can be allocated the just-freed inode number on
                # filesystems that prefer immediate reuse (ext4 notably) — making the
                # (st_ino, st_dev) comparison below match a file THIS call never wrote,
                # directly contradicting invariant 3's promise. Holding the fd open through
                # the check pins our own inode so it cannot be freed/reused out from under
                # it in the first place — the SAME "hold the fd, don't re-resolve the path"
                # principle invariant 2 already applies to the write itself.
                try:
                    path_stat = os.stat(str(bak), follow_symlinks=False)
                except OSError as exc:
                    # A concurrent writer that unlinks our claimed name WITHOUT replacing it
                    # (rather than swapping in a symlink/regular file, the case below
                    # handles) would otherwise leak a raw FileNotFoundError here —
                    # technically fail-closed either way (no overwrite happens,
                    # `_write_config` still logs and aborts), but a misleading one: "failed
                    # to write rig.yaml: No such file" reads as "the write itself failed"
                    # when actually the write succeeded and the backup was stolen out from
                    # under it. Raise the same curated message the branch below uses.
                    raise RuntimeError(f"backup at {bak} was replaced during write; aborting") from exc
                if (path_stat.st_ino, path_stat.st_dev) != (dst_stat.st_ino, dst_stat.st_dev):
                    # Already proven not-ours — NEVER touch the path again, symlink or
                    # regular file alike. A review finding on an earlier version of this
                    # branch: unlinking "only when it's a symlink" still stat-then-acts on a
                    # STALE type snapshot — between the `os.stat` above and an unlink here,
                    # the name could be swapped AGAIN (a second legitimate caller claiming
                    # it, or another attacker step), and the type-based unlink would delete
                    # whatever now sits there with no re-verification. There is no way to
                    # re-check "do I still own this" without re-introducing the exact TOCTOU
                    # this whole claim-then-write protocol exists to close. Fail closed and
                    # leave whatever is there: a stray backup-shaped name left behind is a
                    # cheap, inert cost; deleting a legitimate second claimant's real data
                    # is not.
                    raise RuntimeError(f"backup at {bak} was replaced during write; aborting")
    except BaseException:
        if not fd_owned_by_fdopen:
            # `os.fdopen(fd, ...)` itself raising can, on some platforms/failure modes,
            # already have closed `fd` as part of its own cleanup before propagating — a
            # bare `os.close(fd)` here would then raise EBADF and MASK the real failure
            # (a review finding). Best-effort: close it if it's still open, don't let a
            # redundant close-of-an-already-closed-fd replace the actual error.
            with contextlib.suppress(OSError):
                os.close(fd)
        # A SIXTH round of review found the same stat-then-unlink race here that the
        # identity-mismatch branch above already gives up on: once the fd is closed (always
        # true by the time a COPY failure reaches this handler — either via `os.close(fd)`
        # above or via `with dst:`'s own cleanup), there is no way left to verify "the name
        # still names OUR write" without re-introducing a path-based check-then-act. A
        # failed copy leaves a stray partial/empty backup behind — a cheap, inert cost, the
        # same trade this function makes everywhere else once it can't hold the live fd as
        # proof of ownership. (The identity check itself no longer hits this window — see
        # the round-18 finding above — it now runs BEFORE the fd closes.)
        raise

    return bak


def _global_stack(repo_root: Path) -> str | None:
    """The GLOBAL-layer stack default only (never the repo layer).

    Deliberately ``include_repo=False``: the wizard's cascade uses this as the *global
    default*, and reading the repo layer here would (a) let an existing ``rig.yaml`` shadow
    the global default and (b) fail-closed on a MALFORMED existing ``rig.yaml`` — which must
    NOT stop the wizard from opening (opening it is how the user fixes that file)."""
    return load(repo_root, include_global=True, include_repo=False).stack


def _initial_wizard_state(env: Environment, explicit_stack: str | None = None) -> SetupState:
    """The default ``SetupState`` the interactive wizard opens with.

    Seeds the stack preset (via the SHARED :func:`resolve_init_stack` cascade the headless
    ``rig init`` uses: explicit ``--stack`` → global default → repo-file detection) so
    Export/Apply from the TUI writes a ``rig.yaml`` carrying the same ``stack`` — otherwise
    the by-stack skills would go unselected on the canonical interactive path. Kept
    module-level (textual-free) so it is unit-testable without instantiating the App.
    ``agent_tools_source`` stays unpinned: the committed rig.yaml must be portable
    (re-detected per machine)."""
    stack = resolve_init_stack(
        env.repo_root, explicit=explicit_stack, global_stack=_global_stack(env.repo_root)
    )
    return SetupState.default(
        agent_tools_source=None, project_type=env.project_type, stack=stack
    )

_CATEGORY_BLURB = {
    "skills": "Advisory markdown rules copied into your agent skills dir (opt-out model).",
    "agent_hooks": "Programmatic guards that block before a side effect (no-verify, secrets).",
    "git_hooks": "The global-hook dispatcher: your hooks run in EVERY repo, even hijacked ones.",
    "ci": "Vendor-neutral CI gates (secret-scan, codeql, dependency-review, ship, …).",
    "mcp": "MCP registrations (review, code-search) — callable from any agent.",
}

_CSS = """
Screen { layout: vertical; }
#env { height: auto; padding: 1 2; background: $panel; }
#body { height: 40%; min-height: 5; }
#cats { width: 45%; border: round $primary; }
#desc { width: 55%; border: round $secondary; padding: 1 2; }
#desc-preview { margin-top: 1; color: $text-muted; }
#log { height: 1fr; min-height: 5; border: round $accent; }
#buttons { height: auto; padding: 1 2; }
Button { margin: 0 1; }
"""


def _build_wizard_class():
    """Construct the RigWizard App subclass, importing textual lazily.

    Defined as a factory so the module-level import of ``riglib.tui.app`` never touches
    textual; the import only happens when the wizard is actually launched.
    """
    from rich.markup import escape as _esc
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import Button, Footer, Header, RichLog, SelectionList, Static
    from textual.widgets.selection_list import Selection

    # If a future binding is genuinely safe mid-apply, add its action name here rather than
    # hand-editing the derivation below. Defined OUTSIDE the class body deliberately: a
    # comprehension inside a class body can't see other names defined earlier in that same
    # class body (a Python scoping quirk — comprehensions chain through enclosing FUNCTION
    # scopes, not enclosing CLASS scopes), so `_BUSY_BLOCKED_ACTIONS`'s derivation below
    # needs this one level up, in `_build_wizard_class`'s own scope.
    _busy_exempt_actions: frozenset[str] = frozenset()

    class RigWizard(App):
        """Single-screen setup wizard."""

        CSS = _CSS
        BINDINGS = [
            ("q", "quit", "Quit"),
            ("a", "apply", "Apply"),
            ("x", "export", "Export yaml"),
        ]
        # Every bound action here mutates state or tears down the app, so all are unsafe
        # mid-apply — DERIVED from BINDINGS, not hand-maintained (a review finding: the old
        # hand-kept tuple only had a TEST catching drift between it and BINDINGS, when the
        # tuple could simply never drift in the first place).
        _BUSY_BLOCKED_ACTIONS = tuple(b[1] for b in BINDINGS if b[1] not in _busy_exempt_actions)

        def __init__(self, repo_root: Path, stack: str | None = None) -> None:
            super().__init__()
            self.env = detect_environment(repo_root)
            # write/plan at the detected git root, so the wizard matches headless
            # apply/status (which also operate on the root) and rig.yaml lands at the root.
            self.repo_root = self.env.repo_root
            self._catalog: Catalog | None = None
            self._catalog_error: str | None = None
            try:
                self._catalog = Catalog.scan(None)
            except CatalogError as exc:
                self._catalog_error = str(exc)
            # keep the committed rig.yaml portable: do NOT pin the auto-detected absolute
            # source (mirrors the headless path; other machines re-detect it). Seed the
            # stack preset through the shared cascade — honoring an explicit `--stack` so the
            # interactive path never silently discards it — so the wizard selects the by-stack
            # skills just like headless `rig init`.
            self.state = _initial_wizard_state(self.env, explicit_stack=stack)
            self._applying = False

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            src = self._catalog.source if self._catalog else f"NOT FOUND ({self._catalog_error})"
            # Two distinct concepts: `toolchain` is the build stack (bun-node/python-uv/go),
            # `stack preset` is the l1/lang[/framework] value THIS wizard will write into
            # rig.yaml and use to select by-stack skills. Show the preset so the user can
            # verify/override the heuristic before Export/Apply, not the unrelated toolchain.
            preset = self.state.data.get("stack") or "unset (add via --stack / config)"
            # repo_root/src/preset are filesystem paths and (for src) a possibly
            # exception-derived string — escape before interpolating into a markup=True
            # Static, or a path/error containing "[" raises MarkupError out of compose().
            env_lines = [
                f"repo: {_esc(str(self.env.repo_root))}",
                f"stack preset: {_esc(str(preset))}   toolchain: {_esc(str(self.env.stack))}   "
                f"type: {_esc(str(self.env.project_type))}   "
                f"gh: {'authed' if self.env.gh_authed else 'no'}   "
                f"dispatcher: {'installed' if self.env.dispatcher_installed else 'no'}",
                f"agent-tools source: {_esc(str(src))}",
            ]
            if self.env.stack == "unknown" or self.state.data.get("stack") is None:
                # `rig init` only DETECTS an existing stack/toolchain from files already in the
                # repo (package.json, pyproject.toml, ...) — it never scaffolds a new project
                # (no `git init`, no package-manager bootstrap). "unknown" commonly means the
                # repo genuinely has no manifest yet, not that detection failed. Point at the
                # full config wizard rather than leaving these as unexplained dead status text.
                env_lines.append(
                    "[dim]stack/toolchain unset or undetected — set explicitly with "
                    "`rig setup` or `rig config set stack <preset>`[/dim]"
                )
            yield Static("\n".join(env_lines), id="env", markup=True)
            with Horizontal(id="body"):
                cats = SelectionList[str](
                    *[
                        Selection(f"{name}", name, True)
                        for name in ("skills", "agent_hooks", "git_hooks", "ci", "mcp")
                    ],
                    id="cats",
                )
                cats.border_title = "categories (space to toggle)"
                yield cats
                with VerticalScroll(id="desc"):
                    yield Static(_CATEGORY_BLURB["skills"], id="desc-body")
                    yield Static("", id="desc-preview", markup=True)
            yield RichLog(id="log", highlight=False, markup=True)
            with Horizontal(id="buttons"):
                yield Button(
                    "Export rig.yaml",
                    id="btn-export",
                    variant="primary",
                    tooltip="Write rig.yaml to disk only — does NOT install/run anything. "
                    "Use this to inspect or commit the config before applying.",
                )
                yield Button(
                    "Apply",
                    id="btn-apply",
                    variant="success",
                    tooltip="Write rig.yaml AND run every enabled action now "
                    "(installs skills/hooks/CI/MCP for real).",
                )
                yield Button("Quit", id="btn-quit", variant="error", tooltip="Exit without writing anything.")
            yield Footer()

        def on_mount(self) -> None:
            self.title = "rig init"
            self.sub_title = "dev-environment umbrella driver"
            if self._catalog_error:
                # `_catalog_error` embeds whatever path/detail `Catalog.scan` was given (e.g.
                # $RIG_AGENT_TOOLS_SOURCE) — same untrusted-string class as every other _esc()
                # call in this file; a value containing "[/]" would otherwise raise
                # MarkupError out of on_mount() before the wizard ever renders.
                self.query_one("#log", RichLog).write(
                    f"[red]agent-tools not found:[/red] {_esc(str(self._catalog_error))}"
                )
            self._update_preview()

        def on_selection_list_selection_highlighted(
            self, event: SelectionList.SelectionHighlighted
        ) -> None:
            name = event.selection.value
            self.query_one("#desc-body", Static).update(_CATEGORY_BLURB.get(name, ""))

        def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
            self._update_preview()

        def _update_preview(self) -> None:
            """Best-effort live preview of what Apply would actually do right now.

            Fills the previously-empty right-hand panel with the resolved plan's real action
            count, so toggling a category shows its concrete effect instead of leaving the
            user to guess. Never raises — an invalid in-progress toggle state just shows the
            error inline rather than crashing the TUI (mirrors action_apply's own handling).
            """
            preview = self.query_one("#desc-preview", Static)
            if self._catalog is None:
                preview.update("[red]agent-tools source not found — cannot preview plan[/red]")
                return
            try:
                plan = self._resolve_plan()
            except Exception as exc:  # noqa: BLE001 — preview is best-effort, never crash the TUI
                preview.update(f"[yellow]plan preview unavailable:[/yellow] {_esc(str(exc))}")
                return
            by_cat: dict[str, int] = {}
            for action in plan.actions:
                by_cat[action.category] = by_cat.get(action.category, 0) + 1
            # category names come from the catalog on disk, same trust level as the
            # per-action detail strings the worker escapes below — escape here too so one
            # `_esc` policy covers every catalog/plan-derived string, not just the error path.
            counts = ", ".join(f"{_esc(cat)}×{n}" for cat, n in sorted(by_cat.items())) or "(none)"
            preview.update(f"[bold]{len(plan)} action(s) on Apply:[/bold] {counts}")

        def _apply_category_toggles(self) -> None:
            selected = set(self.query_one("#cats", SelectionList).selected)
            for cat in ("skills", "agent_hooks", "ci", "mcp"):
                self.state.data.setdefault(cat, {})["enabled"] = cat in selected
            self.state.data.setdefault("git_hooks", {}).setdefault("dispatcher", {})[
                "enabled"
            ] = "git_hooks" in selected

        def _sync_and_validate(self) -> None:
            """Sync ``state.data`` with the current category toggles, then validate.

            The catalog-INDEPENDENT half of the pipeline: every caller (export/preview/apply)
            needs this, even when ``self._catalog`` is ``None`` (agent-tools not found) and a
            real plan can't be built at all — that used to mean Export skipped validation
            entirely instead of just skipping the build. Raises on invalid config.
            """
            self._apply_category_toggles()
            validate(self.state.data)

        def _resolve_plan(self):
            """``_sync_and_validate()`` plus resolving the actual plan against the catalog.

            Single source of truth for the preview/apply/export pipeline (validate → build) —
            previously each re-implemented this, so a future change to one could silently
            stop matching what the others report. Requires ``self._catalog`` to be set; when
            there is no catalog to build against at all, callers fall back to
            ``_sync_and_validate()`` alone (Export does; Preview shows "unavailable").
            """
            self._sync_and_validate()
            loaded = LoadedConfig(data=self.state.data, repo_root=self.repo_root)
            return build(loaded, self._catalog, project_type=self.env.project_type)

        def _write_config(self, *, log_prefix: str) -> Path | None:
            """Back up any existing rig.yaml, write the current state, and log it.

            Shared by Export and Apply so the backup-then-write sequence — and its log
            line — can't drift between the two call sites. Returns ``None`` (having already
            logged the failure) rather than letting an OSError/RuntimeError from the backup
            or write step escape to Textual's default error screen — every other failure path
            in this file follows a "surface to the log, don't crash the TUI" policy, and this
            step (backup+write, run right before Apply would start executing the plan) is the
            one place that used to violate it.
            """
            log = self.query_one("#log", RichLog)
            try:
                self._backup_existing_config()
                path = self.state.write(self.repo_root / "rig.yaml")
            except Exception as exc:  # noqa: BLE001 — surface to the log, don't crash the TUI
                log.write(f"[red]failed to write rig.yaml:[/red] {_esc(str(exc))}")
                return None
            log.write(f"{log_prefix} {_esc(str(path))}")
            return path

        def action_export(self) -> None:
            # `check_action` below is the REAL user-facing signal for the keybinding (Textual
            # never dispatches to this method at all when it returns non-True — verified: a
            # simulated keypress with check_action -> None never invokes the bound action) and
            # the button is `.disabled` while applying, so this can only be reached by a direct
            # call (a test, or future code). Kept as silent defense-in-depth, not UI feedback —
            # anything logged here would never be seen by an actual user.
            if self._applying:
                return
            self._do_export()

        def action_quit(self) -> None:  # noqa: D102 — overrides App.action_quit
            # See action_export's comment: unreachable via real input while `_applying`
            # (check_action + the disabled button both intercept it first); this guard exists
            # only so a direct call can never tear the app down mid-run. Thread workers (see
            # _run_plan_worker) have no cooperative cancellation, so there is no safe way to
            # exit while a plan is still running headless with nothing attached to its output.
            if self._applying:
                return
            # Textual's signature is `exit(result=None, return_code=0, ...)` — a bare
            # `exit()` (not `exit(0)`, which sets `result`, not the process return code) is
            # the plain "quit clean" call.
            self.exit()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "btn-quit":
                self.action_quit()
            elif event.button.id == "btn-export":
                self.action_export()
            elif event.button.id == "btn-apply":
                self.action_apply()

        def _backup_existing_config(self) -> None:
            """Back up an existing rig.yaml before the wizard overwrites it with its own.

            Interactive overwrite is intentional (the user pressed Export/Apply), but the
            committed source of truth must never be lost silently — so we keep a timestamped
            backup, mirroring the install actions' on_conflict=backup discipline.
            """
            repo_yaml = self.repo_root / "rig.yaml"
            if repo_yaml.is_file():
                bak = _atomic_timestamped_backup(repo_yaml)
                self.query_one("#log", RichLog).write(
                    f"[yellow]↩[/yellow] backed up existing rig.yaml → {_esc(str(bak))}"
                )

        def _do_export(self) -> None:
            # Route through the SAME pipeline Preview and Apply use — Export used to skip
            # straight to state.write(), so an invalid config (a bad --stack preset, a
            # hand-edited rig.yaml) could be written to disk with a green checkmark while the
            # preview panel right next to it was already saying "unavailable". A first fix
            # here only re-ran `validate()`, which still left a gap: `validate()` can pass
            # while `build()` raises (an unresolvable by-stack skill/hook, an unsupported
            # project_type combination) — Export would still export green while Preview said
            # "unavailable" for that exact config. Resolve the full plan via `_resolve_plan()`
            # whenever a catalog is available, matching Preview/Apply exactly; only fall back
            # to validate-only when there is no catalog to build against at all (mirrors
            # Preview's own `self._catalog is None` early return).
            try:
                if self._catalog is not None:
                    self._resolve_plan()
                else:
                    self._sync_and_validate()
            except Exception as exc:  # noqa: BLE001 — surface to the log, don't crash the TUI
                self.query_one("#log", RichLog).write(f"[red]config error:[/red] {_esc(str(exc))}")
                return
            self._write_config(log_prefix="[green]✔[/green] exported →")

        def action_apply(self) -> None:
            if self._applying:
                return  # already running — a bare double-click/keybind repeat is a no-op
            log = self.query_one("#log", RichLog)
            if self._catalog is None:
                log.write("[red]cannot apply: agent-tools source not found[/red]")
                return
            # validate + build the plan BEFORE writing rig.yaml — never leave a bad
            # committed config behind a failed apply (mirrors the headless ordering).
            try:
                plan = self._resolve_plan()
            except Exception as exc:  # noqa: BLE001 — surface to the log, don't crash the TUI
                log.write(f"[red]config error:[/red] {_esc(str(exc))}")
                return
            if self._write_config(log_prefix="[green]✔[/green] wrote") is None:
                return  # failure already logged by _write_config; never start the worker
            log.write(f"[bold]applying {len(plan)} action(s)…[/bold]")
            try:
                # `_applying = True` and disabling controls now live INSIDE this try too (a
                # review finding, GLM, round 23): they used to run BEFORE it, so if
                # `_set_controls_enabled(False)` itself raised (e.g. a `NoMatches` query
                # error during a screen-teardown race) the exception propagated straight out
                # of `action_apply` uncaught — `_applying` would already be `True` by then,
                # with no `except` in scope to run `_finish_apply()` and clear it. Every
                # control would stay wedged behind the apply-in-flight guard forever, the
                # exact class this handler is otherwise hardened against. Setting `_applying`
                # first, still inside the try, means even a raise from
                # `_set_controls_enabled` itself now reaches the `except` below and gets
                # unwedged by `_finish_apply()` — which clears `_applying = False` before its
                # OWN `_set_controls_enabled(True)` call, so that invariant lands even if the
                # secondary re-enable call fails the same way.
                self._applying = True
                self._set_controls_enabled(False)
                # capture `log` here (UI thread) and pass it in — querying the DOM from the
                # worker thread itself is unsafe even though RichLog.write() is thread-safe.
                self.run_worker(
                    lambda: self._run_plan_worker(plan, log), thread=True, exclusive=True, group="apply"
                )
            except Exception as exc:  # noqa: BLE001 — the try/finally that normally clears
                # `_applying` lives INSIDE the worker body; if launching the worker itself
                # (or disabling controls before it, per the comment above) raises before the
                # worker ever runs, that safety net never engages and every control would
                # stay disabled forever with no way to recover.
                log.write(f"[red]failed to start apply:[/red] {_esc(str(exc))}")
                self._finish_apply()

        def _run_plan_worker(self, plan, log) -> None:  # noqa: ANN001
            """Runs OFF the UI thread so the log actually repaints between actions.

            ``run_plan`` used to be called directly inside the (synchronous) button handler:
            every ``log.write()`` queued a message, but Textual never got a chance to redraw
            until the whole call returned, so the entire run appeared to hang — "status
            invisible" — then dumped everything at once at the end. Running it in a worker
            thread and pushing each update via ``call_from_thread`` lets the screen repaint
            live, per action, as it actually happens.

            The whole body is wrapped in try/finally: ``run_plan`` re-raising (a bug in a
            backend, an unexpected OS error) must never leave ``_finish_apply`` unrun — that
            would leave ``_applying`` stuck ``True`` forever, wedging every control behind the
            apply-in-flight guard with no way to recover except killing the process.

            ``_on_start``/``_progress`` fire DURING ``run_plan``'s loop, so a failure inside
            either one propagates straight out of ``run_plan`` and aborts every remaining
            action mid-plan — not just a UI glitch, a truncated install. ``call_from_thread``
            raises ``RuntimeError`` if the app's event loop is already gone (a fatal UI
            exception, a SIGTERM-driven shutdown, test teardown racing the worker); EVERY
            ``call_from_thread`` in this method — not just the two during the loop — is
            routed through ``_post`` for the same reason: if the loop is gone, there is no UI
            left to report to and nothing left for ``_finish_apply`` to reset, so raising here
            would only ever mask the real cause (a `run_plan` exception in the `except`
            branch) behind a second, unrelated `RuntimeError` out of the `finally`.
            """
            mark = {"created": "✔", "updated": "✔", "backed_up": "↩", "skipped": "·", "error": "✗"}

            def _post(fn, *args) -> None:  # noqa: ANN001, ANN002
                with contextlib.suppress(RuntimeError):
                    self.call_from_thread(fn, *args)

            def _on_start(action) -> None:  # noqa: ANN001
                _post(log.write, f"  … {_esc(f'{action.category}/{action.item}')}")

            def _progress(res) -> None:  # noqa: ANN001
                _post(
                    log.write,
                    f"  {mark.get(res.status, '?')} "
                    f"{_esc(f'{res.action.category}/{res.action.item}: {res.detail}')}",
                )

            try:
                report = run_plan(plan, on_start=_on_start, progress=_progress)
            except Exception as exc:  # noqa: BLE001 — never let a backend bug wedge the wizard
                _post(log.write, f"[red]apply failed:[/red] {_esc(str(exc))}")
            else:
                summary = ", ".join(f"{k}={v}" for k, v in sorted(report.summary().items()))
                _post(log.write, f"[bold green]done[/bold green] — {summary}")
            finally:
                _post(self._finish_apply)

        def _finish_apply(self) -> None:
            self._applying = False
            self._set_controls_enabled(True)

        def _set_controls_enabled(self, enabled: bool) -> None:
            for bid in ("btn-export", "btn-apply", "btn-quit"):
                self.query_one(f"#{bid}", Button).disabled = not enabled
            self.query_one("#cats", SelectionList).disabled = not enabled
            # invalidate Textual's cached binding-enabled state so the footer immediately
            # reflects check_action() below, instead of showing a live "q"/"x" while the
            # button/action-level guards silently no-op them.
            self.refresh_bindings()

        def check_action(
            self, action: str, parameters: tuple[object, ...]
        ) -> bool | None:
            """Grey out (not hide) quit/export/apply in the footer while a plan is running.

            This is a passive UX signal, not the enforcement — action_quit/action_export/
            action_apply each still guard on ``self._applying`` themselves, since a direct
            method call (a button press, a test) bypasses Textual's action-dispatch/binding
            machinery entirely and never reaches check_action. Returning ``None`` (rather
            than ``False``) keeps the binding visible-but-disabled, so the user sees WHY
            nothing happens instead of the keybind just silently vanishing.
            """
            if self._applying and action in self._BUSY_BLOCKED_ACTIONS:
                return None
            return True

    return RigWizard


def run_wizard(repo_root: Path, stack: str | None = None) -> int:
    app = _build_wizard_class()(repo_root, stack=stack)
    app.run()
    return 0
