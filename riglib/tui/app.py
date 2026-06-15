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

from pathlib import Path

from ..actions import run_plan
from ..catalog import Catalog, CatalogError
from ..config import LoadedConfig, validate
from ..detect import detect_environment
from ..plan import build
from ..state import SetupState

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
#body { height: 1fr; }
#cats { width: 45%; border: round $primary; }
#desc { width: 55%; border: round $secondary; padding: 1 2; }
#log { height: 12; border: round $accent; }
#buttons { height: auto; padding: 1 2; }
Button { margin: 0 1; }
"""


def _build_wizard_class():
    """Construct the RigWizard App subclass, importing textual lazily.

    Defined as a factory so the module-level import of ``riglib.tui.app`` never touches
    textual; the import only happens when the wizard is actually launched.
    """
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import Button, Footer, Header, RichLog, SelectionList, Static
    from textual.widgets.selection_list import Selection

    class RigWizard(App):
        """Single-screen setup wizard."""

        CSS = _CSS
        BINDINGS = [
            ("q", "quit", "Quit"),
            ("a", "apply", "Apply"),
            ("x", "export", "Export yaml"),
        ]

        def __init__(self, repo_root: Path) -> None:
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
            # source (mirrors the headless path; other machines re-detect it).
            self.state = SetupState.default(
                agent_tools_source=None,
                project_type=self.env.project_type,
            )

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            src = self._catalog.source if self._catalog else f"NOT FOUND ({self._catalog_error})"
            yield Static(
                f"repo: {self.env.repo_root}\n"
                f"stack: {self.env.stack}   type: {self.env.project_type}   "
                f"gh: {'authed' if self.env.gh_authed else 'no'}   "
                f"dispatcher: {'installed' if self.env.dispatcher_installed else 'no'}\n"
                f"agent-tools source: {src}",
                id="env",
            )
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
            yield RichLog(id="log", highlight=False, markup=True)
            with Horizontal(id="buttons"):
                yield Button("Export rig.yaml", id="btn-export", variant="primary")
                yield Button("Apply", id="btn-apply", variant="success")
                yield Button("Quit", id="btn-quit", variant="error")
            yield Footer()

        def on_mount(self) -> None:
            self.title = "rig setup"
            self.sub_title = "dev-environment umbrella driver"
            if self._catalog_error:
                self.query_one("#log", RichLog).write(
                    f"[red]agent-tools not found:[/red] {self._catalog_error}"
                )

        def on_selection_list_selection_highlighted(
            self, event: SelectionList.SelectionHighlighted
        ) -> None:
            name = event.selection.value
            self.query_one("#desc-body", Static).update(_CATEGORY_BLURB.get(name, ""))

        def _apply_category_toggles(self) -> None:
            selected = set(self.query_one("#cats", SelectionList).selected)
            for cat in ("skills", "agent_hooks", "ci", "mcp"):
                self.state.data.setdefault(cat, {})["enabled"] = cat in selected
            self.state.data.setdefault("git_hooks", {}).setdefault("dispatcher", {})[
                "enabled"
            ] = "git_hooks" in selected

        def action_export(self) -> None:
            self._do_export()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "btn-quit":
                self.exit(0)
            elif event.button.id == "btn-export":
                self._do_export()
            elif event.button.id == "btn-apply":
                self.action_apply()

        def _backup_existing_config(self) -> None:
            """Back up an existing rig.yaml before the wizard overwrites it with its own.

            Interactive overwrite is intentional (the user pressed Export/Apply), but the
            committed source of truth must never be lost silently — so we keep a timestamped
            backup, mirroring the install actions' on_conflict=backup discipline.
            """
            import shutil
            import time

            repo_yaml = self.repo_root / "rig.yaml"
            if repo_yaml.is_file():
                bak = repo_yaml.with_name(f"rig.yaml.rig-bak-{time.strftime('%Y%m%d-%H%M%S')}")
                shutil.copy2(str(repo_yaml), str(bak))
                self.query_one("#log", RichLog).write(f"[yellow]↩[/yellow] backed up existing rig.yaml → {bak}")

        def _do_export(self) -> None:
            self._apply_category_toggles()
            self._backup_existing_config()
            path = self.state.write(self.repo_root / "rig.yaml")
            self.query_one("#log", RichLog).write(f"[green]✔[/green] exported → {path}")

        def action_apply(self) -> None:
            log = self.query_one("#log", RichLog)
            if self._catalog is None:
                log.write("[red]cannot apply: agent-tools source not found[/red]")
                return
            self._apply_category_toggles()
            # validate + build the plan BEFORE writing rig.yaml — never leave a bad
            # committed config behind a failed apply (mirrors the headless ordering).
            try:
                validate(self.state.data)
                loaded = LoadedConfig(data=self.state.data, repo_root=self.repo_root)
                plan = build(loaded, self._catalog, project_type=self.env.project_type)
            except Exception as exc:  # noqa: BLE001 — surface to the log, don't crash the TUI
                log.write(f"[red]config error:[/red] {exc}")
                return
            self._backup_existing_config()
            self.state.write(self.repo_root / "rig.yaml")
            log.write(f"[bold]applying {len(plan)} action(s)…[/bold]")

            def _progress(res) -> None:  # noqa: ANN001
                mark = {"created": "✔", "updated": "✔", "backed_up": "↩", "skipped": "·", "error": "✗"}
                log.write(f"  {mark.get(res.status, '?')} {res.action.category}/{res.action.item}: {res.detail}")

            report = run_plan(plan, progress=_progress)
            summary = ", ".join(f"{k}={v}" for k, v in sorted(report.summary().items()))
            log.write(f"[bold green]done[/bold green] — {summary}")

    return RigWizard


def run_wizard(repo_root: Path) -> int:
    app = _build_wizard_class()(repo_root)
    app.run()
    return 0
