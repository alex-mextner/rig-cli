"""Local web app for `rig evolve`.

Accessed via: `rig evolve _serve` from the shared service manager, or directly in tests through
`EvolveApp`. The server is read-only in this first slice: it exposes project snapshots and a
self-contained interactive page, so there are no write endpoints to protect with CSRF yet.
"""

from __future__ import annotations

import html
import json
import posixpath
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .git_index import build_histogram, build_path_touches, git_health
from .structure import build_file_tree
from .model import PROVIDER_SCHEMA
from .symbols import extract_symbols

HOST = "127.0.0.1"
DEFAULT_PORT = 8797
PAGE_TITLE = "rig evolve"
_SNAPSHOT_TTL_S = 300.0
_SNAPSHOT_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
_SNAPSHOT_PATHS_CACHE: dict[tuple[str, str], tuple[float, set[str]]] = {}
_RELATIONSHIP_TTL_S = 300.0
_RELATIONSHIP_INDEX_CACHE: dict[tuple[str, str], tuple[float, dict[str, dict[str, Any]]]] = {}
_RELATIONSHIP_FILE_LIMIT = 1000
_RELATIONSHIP_LINE_LIMIT = 1200
_RELATIONSHIP_IMPORT_LIMIT = 80
_RELATIONSHIP_LIMIT_MESSAGE = (
    f"Heuristic import index, capped at {_RELATIONSHIP_FILE_LIMIT} files, "
    f"{_RELATIONSHIP_LINE_LIMIT} lines/file, {_RELATIONSHIP_IMPORT_LIMIT} imports/file."
)


def is_allowed_host(headers: Any) -> bool:
    host = (headers.get("Host") or "").strip()
    if not host:
        return True
    hostname = host.rsplit(":", 1)[0] if ":" in host else host
    return hostname in (HOST, "localhost") or hostname.endswith(".ts.net")


@dataclass
class EvolveApp:
    repo_root: Path

    def projects_payload(self) -> dict[str, Any]:
        from .projects import discover_projects

        projects = discover_projects(self.repo_root)
        return {
            "projects": projects,
            "health": {"projects": {"status": "ok", "message": f"{len(projects)} project(s) discovered"}},
        }

    def snapshot_payload(self, *, project_path: str | None = None, bucket: str = "month") -> dict[str, Any]:
        project = Path(project_path).expanduser().resolve() if project_path else self.repo_root.resolve()
        head = _git_head(project)
        key = (str(project), bucket, head)
        now = time.monotonic()
        cached = _SNAPSHOT_CACHE.get(key)
        if cached and now - cached[0] < _SNAPSHOT_TTL_S:
            payload = dict(cached[1])
            payload["cache"] = {"status": "hit", "ttl_s": int(_SNAPSHOT_TTL_S)}
            return payload
        with ThreadPoolExecutor(max_workers=3) as pool:
            hist_f = pool.submit(build_histogram, project, bucket=bucket, include_paths=False)
            tree_f = pool.submit(build_file_tree, project)
            git_f = pool.submit(git_health, project)
            payload = {
                "project": {"name": project.name, "path": str(project), "head": head},
                "histogram": hist_f.result(),
                "tree": tree_f.result(),
                "health": {"git": git_f.result()},
                "cache": {"status": "miss", "ttl_s": int(_SNAPSHOT_TTL_S)},
            }
        _SNAPSHOT_CACHE[key] = (now, payload)
        _trim_snapshot_cache()
        return payload

    def touches_payload(self, *, project_path: str | None = None, path: str = "", bucket: str = "month") -> dict[str, Any]:
        project = Path(project_path).expanduser().resolve() if project_path else self.repo_root.resolve()
        return {"path": path, "bucket_ids": build_path_touches(project, path, bucket=bucket)}

    def symbols_payload(self, *, project_path: str | None = None, path: str = "") -> dict[str, Any]:
        project = self._allowed_project(project_path)
        if project is None:
            return {"path": path, "symbols": [], "health": {"status": "error", "message": "project not allowed"}}
        rel = _safe_rel(path)
        target = _safe_project_file(project, rel)
        if not rel or rel not in _snapshot_file_paths(project) or target is None:
            return {"path": path, "symbols": [], "health": {"status": "warning", "message": "file not found"}}
        try:
            source = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return {"path": rel, "symbols": [], "health": {"status": "error", "message": str(exc)}}
        symbols = extract_symbols(target, source, repo_root=project)
        return {"path": rel, "symbols": symbols, "health": {"status": "ok", "message": f"{len(symbols)} root symbol(s)"}}

    def relationships_payload(self, *, project_path: str | None = None, path: str = "") -> dict[str, Any]:
        project = self._allowed_project(project_path)
        if project is None:
            return {
                "path": path,
                "relationships": {"uses": [], "used_by": [], "quality": "error", "message": "project not allowed"},
            }
        rel = _safe_rel(path)
        if not rel or rel not in _snapshot_file_paths(project) or _safe_project_file(project, rel) is None:
            return {
                "path": rel,
                "relationships": {"uses": [], "used_by": [], "quality": "warning", "message": "file not found"},
            }
        try:
            relationships = _relationships(project, rel)
        except Exception as exc:  # noqa: BLE001
            relationships = {"uses": [], "used_by": [], "quality": "error", "message": str(exc)}
        return {"path": rel, "relationships": relationships}

    def _allowed_project(self, project_path: str | None) -> Path | None:
        project = Path(project_path).expanduser().resolve() if project_path else self.repo_root.resolve()
        if project == self.repo_root.resolve():
            return project
        try:
            discovered = self.projects_payload()["projects"]
        except Exception:  # noqa: BLE001
            return None
        allowed = {Path(str(item.get("path") or "")).expanduser().resolve() for item in discovered}
        return project if project in allowed else None

    def providers_payload(self, *, project_path: str | None = None, refresh: bool = False) -> dict[str, Any]:
        from .cache import ProviderCache, ProviderCacheKey
        from .providers import collect_default, default_providers

        project = Path(project_path).expanduser().resolve() if project_path else self.repo_root.resolve()
        version = _git_head(project)
        cache = ProviderCache()
        provider_names = [provider.name for provider in default_providers()]
        payloads: list[dict[str, Any]] = []
        missing = False

        if not refresh:
            for provider in provider_names:
                key = ProviderCacheKey(project_path=project, version=version, provider=provider)
                cached = cache.get(key)
                if cached is None:
                    missing = True
                    break
                encoded = cached.to_dict()
                encoded["cache"] = _provider_cache_meta(cache, key, status="hit")
                payloads.append(encoded)

        if refresh or missing:
            payloads = []
            for payload in collect_default(project):
                key = ProviderCacheKey(project_path=project, version=version, provider=payload.source)
                cache_status = "refresh" if refresh else "miss"
                try:
                    cache.set(key, payload)
                except OSError as exc:
                    cache_meta = _provider_cache_meta(cache, key, status="write-error", message=str(exc))
                else:
                    cache_meta = _provider_cache_meta(cache, key, status=cache_status)
                encoded = payload.to_dict()
                encoded["cache"] = cache_meta
                payloads.append(encoded)

        return {
            "schema": PROVIDER_SCHEMA,
            "project": {"name": project.name, "path": str(project), "head": version},
            "cache": {"root": str(cache.root), "version": version, "status": "refresh" if refresh else "ready"},
            "providers": payloads,
        }

    def render_page(self) -> bytes:
        return _build_html(self.repo_root).encode("utf-8")

    def make_handler(self) -> type:
        import http.server
        from urllib.parse import parse_qs, urlparse

        app = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            timeout = 20

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                if code >= 300:
                    self.close_connection = True
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_json(self, code: int, body: dict[str, Any]) -> None:
                self._send(code, json.dumps(body).encode("utf-8"), "application/json; charset=utf-8")

            def do_GET(self) -> None:  # noqa: N802
                if not is_allowed_host(self.headers):
                    self._send(403, b"forbidden host", "text/plain; charset=utf-8")
                    return
                parsed = urlparse(self.path)
                path = _strip_base_path(parsed.path)
                if path in ("/", "/index.html"):
                    self._send(200, app.render_page(), "text/html; charset=utf-8")
                    return
                if path == "/favicon.ico":
                    self._send(204, b"", "image/x-icon")
                    return
                if path == "/api/projects":
                    self._send_json(200, app.projects_payload())
                    return
                if path == "/api/snapshot":
                    qs = parse_qs(parsed.query)
                    project = qs.get("path", [None])[0]
                    bucket = qs.get("bucket", ["month"])[0]
                    try:
                        self._send_json(200, app.snapshot_payload(project_path=project, bucket=bucket))
                    except Exception as exc:  # noqa: BLE001
                        self._send_json(500, {"error": str(exc)})
                    return
                if path == "/api/touches":
                    qs = parse_qs(parsed.query)
                    project = qs.get("project", [None])[0]
                    target_path = qs.get("path", [""])[0]
                    bucket = qs.get("bucket", ["month"])[0]
                    try:
                        self._send_json(200, app.touches_payload(project_path=project, path=target_path, bucket=bucket))
                    except Exception as exc:  # noqa: BLE001
                        self._send_json(500, {"error": str(exc)})
                    return
                if path == "/api/symbols":
                    qs = parse_qs(parsed.query)
                    project = qs.get("project", qs.get("path", [None]))[0]
                    target_path = qs.get("file", qs.get("path", [""]))[0]
                    try:
                        self._send_json(200, app.symbols_payload(project_path=project, path=target_path))
                    except Exception as exc:  # noqa: BLE001
                        self._send_json(500, {"error": str(exc)})
                    return
                if path == "/api/relationships":
                    qs = parse_qs(parsed.query)
                    project = qs.get("project", qs.get("path", [None]))[0]
                    target_path = qs.get("file", qs.get("path", [""]))[0]
                    try:
                        self._send_json(200, app.relationships_payload(project_path=project, path=target_path))
                    except Exception as exc:  # noqa: BLE001
                        self._send_json(500, {"error": str(exc)})
                    return
                if path == "/api/providers":
                    qs = parse_qs(parsed.query)
                    project = qs.get("project", qs.get("path", [None]))[0]
                    refresh = (qs.get("refresh", [""])[0] or "").lower() in {"1", "true", "yes"}
                    try:
                        self._send_json(200, app.providers_payload(project_path=project, refresh=refresh))
                    except Exception as exc:  # noqa: BLE001
                        self._send_json(500, {"error": str(exc)})
                    return
                self._send(404, b"not found", "text/plain; charset=utf-8")

            def log_message(self, *args: Any) -> None:
                return

        return _Handler

    def serve(self, *, port: int = DEFAULT_PORT, open_browser: bool = False) -> int:
        import threading
        import webbrowser

        httpd = self.make_server(port)
        bound = int(httpd.server_address[1])
        url = f"http://{HOST}:{bound}/"
        print(f"rig evolve — serving portal at {url}  (Ctrl-C to stop)")
        threading.Thread(target=lambda: self.snapshot_payload(project_path=str(self.repo_root)), daemon=True).start()
        if open_browser:
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")
        finally:
            httpd.server_close()
        return bound

    def make_server(self, port: int = DEFAULT_PORT) -> Any:
        import http.server

        class _ThreadingHTTPServer(http.server.ThreadingHTTPServer):
            daemon_threads = True

        return _ThreadingHTTPServer((HOST, port), self.make_handler())


def _build_html(repo_root: Path) -> str:
    repo = html.escape(str(repo_root.resolve()))
    repo_name = html.escape(repo_root.name or str(repo_root.resolve()))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>{PAGE_TITLE}</title>
<style>
  :root {{ color-scheme: dark; --bg:#111318; --fg:#eef2f6; --surface:#151922; --panel:#1b1f27; --border:#2d3442; --muted:#9aa6b2; --accent:#f59e0b; --focus:#93c5fd; --ok:#22c55e; --warn:#f59e0b; --bad:#ef4444; --link:#7dd3fc; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg); font:12px/1.4 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif; overflow:hidden; -webkit-tap-highlight-color:rgba(147,197,253,.22); }}
  .skip-link {{ position:absolute; left:12px; top:8px; z-index:10; transform:translateY(-140%); background:var(--panel); color:var(--fg); border:1px solid var(--focus); border-radius:6px; padding:6px 9px; }}
  .skip-link:focus-visible {{ transform:translateY(0); }}
  header {{ height:48px; min-width:0; display:flex; align-items:center; gap:12px; padding:0 16px; border-bottom:1px solid var(--border); background:var(--surface); }}
  h1 {{ font-size:15px; margin:0; font-weight:650; }}
  select,button {{ background:#202632; color:var(--fg); border:1px solid #3a4352; border-radius:6px; padding:5px 8px; touch-action:manipulation; }}
  select:focus-visible,button:focus-visible,.skip-link:focus-visible {{ outline:2px solid var(--focus); outline-offset:2px; }}
  button.active,button[aria-pressed="true"] {{ border-color:var(--accent); color:#fff7ed; }}
  #projects {{ min-width:180px; max-width:min(360px, 32vw); }}
  #bucket-controls {{ flex:0 0 auto; display:flex; gap:4px; align-items:center; }}
  .projectPath {{ min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  main {{ display:grid; grid-template-rows:170px 1fr; height:calc(100vh - 48px); }}
  #hist {{ border-bottom:1px solid var(--border); background:#121722; padding:14px 16px 8px; }}
  #work {{ display:grid; grid-template-columns:minmax(0,1fr) 360px; min-height:0; }}
  #map {{ position:relative; min-width:0; overflow:hidden; background:#10141b; cursor:grab; touch-action:none; }}
  #map.is-panning {{ cursor:grabbing; user-select:none; }}
  #map:focus-visible {{ outline:2px solid var(--focus); outline-offset:-2px; }}
  #detail {{ min-width:0; border-left:1px solid var(--border); background:var(--panel); padding:14px; overflow:auto; }}
  #provider-health {{ border-bottom:1px solid var(--border); margin:-2px 0 14px; padding-bottom:12px; }}
  #provider-health h2,#selection-detail h2 {{ font-size:13px; margin:0 0 8px; }}
  .healthRow {{ display:grid; grid-template-columns:82px 62px 70px 54px minmax(0,1fr); gap:8px; align-items:center; padding:4px 0; border-top:1px solid rgba(148,163,184,.12); }}
  .healthRow > * {{ min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .healthStatus {{ font-size:11px; text-transform:uppercase; letter-spacing:.04em; }}
  .health-ok {{ color:var(--ok); }}
  .health-error {{ color:var(--bad); }}
  .health-warning,.health-pending,.health-unknown,.health-hit,.health-miss,.health-refresh,.health-write-error {{ color:var(--warn); }}
  svg {{ width:100%; height:100%; display:block; }}
  .bar {{ fill:#526071; cursor:pointer; transition:fill .12s; }}
  .bar:hover,.bar.active {{ fill:var(--accent); }}
  .tile {{ stroke:#10141b; stroke-width:1; cursor:pointer; transition:opacity .12s,stroke .12s; }}
  .tile:hover {{ stroke:var(--fg); stroke-width:2; }}
  .tile.selected {{ stroke:var(--accent); stroke-width:3; }}
  .frame {{ fill:#151b24; stroke:#3a4352; stroke-width:1.2; cursor:pointer; }}
  .frame:hover {{ stroke:var(--fg); stroke-width:2; }}
  .frame.selected {{ stroke:var(--accent); stroke-width:3; }}
  .tile:focus-visible,.frame:focus-visible,.bar:focus-visible {{ outline:2px solid var(--focus); outline-offset:2px; stroke:var(--focus); stroke-width:3; }}
  .label {{ fill:var(--fg); font-size:9px; pointer-events:none; }}
  .frameLabel {{ fill:#cbd5e1; font-size:10px; font-weight:650; pointer-events:none; }}
  .symbolTile {{ fill:rgba(125,211,252,.24); stroke:rgba(125,211,252,.8); stroke-width:1; pointer-events:none; }}
  .symbolLabel {{ fill:#dff6ff; font-size:8px; pointer-events:none; }}
  .loadingOverlay {{ position:fixed; inset:48px 0 0; z-index:5; display:none; align-items:center; justify-content:center; background:rgba(10,14,20,.54); backdrop-filter:blur(2px); }}
  .loadingOverlay.visible {{ display:flex; }}
  .loadingCard {{ width:min(420px, calc(100vw - 32px)); border:1px solid var(--border); border-radius:8px; background:#161b24; padding:14px; box-shadow:0 18px 48px rgba(0,0,0,.34); }}
  .loadingBar {{ height:6px; overflow:hidden; border-radius:999px; background:#273040; margin-top:10px; }}
  .loadingBar span {{ display:block; width:42%; height:100%; background:var(--accent); animation:loadbar 1.1s ease-in-out infinite alternate; }}
  @keyframes loadbar {{ from {{ transform:translateX(-40%); }} to {{ transform:translateX(180%); }} }}
  .detailGrid {{ display:grid; grid-template-columns:88px minmax(0,1fr); gap:5px 9px; margin:8px 0 12px; }}
  .detailGrid code,.relList code,.symbolList code {{ overflow-wrap:anywhere; word-break:break-word; }}
  .relList,.symbolList {{ margin:6px 0 12px; padding:0; list-style:none; }}
  .relList li,.symbolList li {{ padding:3px 0; border-top:1px solid rgba(148,163,184,.12); overflow-wrap:anywhere; word-break:break-word; }}
  .pill {{ display:inline-flex; align-items:center; min-height:18px; padding:1px 6px; border:1px solid #3a4352; border-radius:999px; color:#cbd5e1; font-size:10px; }}
  .zoomHud {{ position:absolute; right:10px; bottom:10px; display:flex; gap:6px; align-items:center; padding:6px 8px; border:1px solid rgba(148,163,184,.35); border-radius:8px; background:rgba(17,19,24,.82); color:#cbd5e1; pointer-events:none; }}
  .muted {{ color:var(--muted); }}
  code {{ color:#bfdbfe; }}
  @media (prefers-reduced-motion: reduce) {{ *,*::before,*::after {{ animation-duration:.001ms !important; animation-iteration-count:1 !important; scroll-behavior:auto !important; transition-duration:.001ms !important; }} }}
  @media (max-width: 760px) {{ header {{ gap:8px; padding:0 10px; }} #projects {{ min-width:130px; max-width:36vw; }} #work {{ grid-template-columns:1fr; }} #detail {{ display:none; }} main {{ grid-template-rows:140px 1fr; }} }}
</style></head><body>
<a class="skip-link" href="#main">Skip to Project Surface</a>
<header><h1 translate="no">rig evolve</h1><span class="muted projectLabel">project</span><select id="projects" name="project" aria-label="Project"><option value="{repo}">{repo_name}</option></select><div id="bucket-controls" data-testid="bucket-controls" role="group" aria-label="Timeline Bucket"><button type="button" data-bucket="day" aria-label="Show Day Buckets">Day</button><button type="button" data-bucket="week" aria-label="Show Week Buckets">Week</button><button type="button" data-bucket="month" aria-label="Show Month Buckets" class="active" aria-pressed="true">Month</button></div><button id="reload" aria-label="Reload Snapshot">Reload</button><span class="muted projectPath" title="{repo}">{repo}</span></header>
<main id="main" tabindex="-1"><section id="hist" aria-label="Project activity histogram"></section><section id="work"><div id="map" aria-label="Project code surface" tabindex="0"><div class="zoomHud" id="zoomHud" aria-hidden="true">100%</div></div><aside id="detail" aria-label="Selection details"><section id="provider-health" data-testid="provider-health" aria-live="polite"><h2>Providers</h2><div class="healthRow" data-provider="git"><span>git</span><span class="healthStatus health-pending">pending</span><span class="muted">cache</span><span class="muted">errors</span><span class="muted">Snapshot not loaded.</span></div></section><section id="selection-detail" aria-live="polite"><div class="muted">Loading snapshot…</div></section></aside></section></main>
<div id="loading" class="loadingOverlay visible" role="status" aria-live="polite"><div class="loadingCard"><strong id="loading-title">Loading project surface…</strong><div class="muted" id="loading-message">Reading git history and file sizes.</div><div class="loadingBar"><span></span></div></div></div>
<script>
let snapshot = null;
let selected = null;
let selectedSymbols = null;
let selectedSymbolError = null;
let selectedRelationships = null;
let projectHealth = {{}};
let providerSnapshot = null;
let bucket = 'month';
let resizeFrame = 0;
let symbolFrame = 0;
let view = null;
let world = {{w: 1, h: 1}};
let activePointers = new Map();
let panStart = null;
let pinchStart = null;
let lastPanAt = 0;
const MAX_TILES_PER_FRAME = 180;
const SYMBOL_ZOOM_THRESHOLD = 2.15;
const PAN_CLICK_SUPPRESS_MS = 250;
const BASE = location.pathname.startsWith('/evolve') ? '/evolve' : '';
const qs = (s) => document.querySelector(s);

function showLoading(title, message) {{
  const overlay = qs('#loading');
  if (!overlay) return;
  qs('#loading-title').textContent = title || 'Loading…';
  qs('#loading-message').textContent = message || '';
  overlay.classList.add('visible');
}}
function hideLoading() {{ const overlay = qs('#loading'); if (overlay) overlay.classList.remove('visible'); }}

async function loadProjects() {{
  const data = await fetch(BASE + '/api/projects').then(r => r.json());
  projectHealth = data.health || {{}};
  const sel = qs('#projects');
  const current = sel.value || (snapshot && snapshot.project && snapshot.project.path) || {repo!r};
  sel.innerHTML = '';
  data.projects.forEach(p => {{
    const opt = document.createElement('option');
    const aliases = (p.aliases || []).length ? ' aliases: ' + p.aliases.join(',') : '';
    const sources = (p.sources || []).join(',');
    opt.value = p.path; opt.textContent = p.name + aliases;
    opt.title = p.path;
    opt.dataset.sources = sources;
    sel.appendChild(opt);
  }});
  if (Array.from(sel.options).some(opt => opt.value === current)) sel.value = current;
  renderHealth();
}}
async function loadSnapshot() {{
  showLoading('Loading project surface…', 'Reading git activity and file sizes.');
  const requestedProject = qs('#projects').value || {repo!r};
  const path = encodeURIComponent(requestedProject);
  const previousPath = selected && selected.path;
  const previousProject = snapshot && snapshot.project && snapshot.project.path;
  updateBucketControls();
  try {{
    snapshot = await fetch(BASE + '/api/snapshot?path=' + path + '&bucket=' + encodeURIComponent(bucket)).then(r => r.json());
    const sameProject = previousProject && snapshot.project && snapshot.project.path === previousProject;
    selected = sameProject && previousPath ? findNodeByPath(snapshot.tree, previousPath) : null;
    selectedSymbols = null;
    selectedSymbolError = null;
    selectedRelationships = null;
    resetView();
    render();
    if (selected) fetchSelectionDetails(selected);
    loadProviders().catch(err => {{
      providerSnapshot = {{providers:[{{source:'providers', status:'error', message:String(err), errors:[{{message:String(err)}}], cache:{{status:'error'}}}}]}};
      renderHealth();
    }});
  }} finally {{
    hideLoading();
  }}
}}
async function loadProviders() {{
  const project = encodeURIComponent(qs('#projects').value || {repo!r});
  providerSnapshot = await fetch(BASE + '/api/providers?project=' + project).then(r => r.json());
  renderHealth();
}}
function render() {{
  renderHistogram();
  renderTreemap();
  renderHealth();
  renderDetail(selected);
  if (selected) highlightBars(selected.path || '');
}}
function renderHistogram() {{
  const root = qs('#hist'); const data = snapshot.histogram || [];
  root.dataset.bucket = bucket;
  const max = Math.max(1, ...data.map(b => b.commits + b.changed_files));
  const w = root.clientWidth || 800, h = root.clientHeight || 150, pad = 22;
  const bw = Math.max(4, (w - pad * 2) / Math.max(1, data.length) - 4);
  root.innerHTML = `<svg viewBox="0 0 ${{w}} ${{h}}"></svg>`;
  const svg = root.firstChild;
  data.forEach((b, i) => {{
    const bh = Math.max(1, (h - 42) * (b.commits + b.changed_files) / max);
    const x = pad + i * (bw + 4), y = h - 24 - bh;
    const r = el('rect', {{
      x, y, width:bw, height:bh, rx:2, class:'bar', tabindex:'0', role:'button',
      'aria-label':`Show snapshot bucket ${{b.id}} with ${{b.commits}} commits and ${{b.changed_files}} changed files`,
      'data-bucket-id':b.id,
      'data-commits':b.commits,
      'data-changed-files':b.changed_files
    }});
    r.addEventListener('click', () => activateBar(r));
    r.addEventListener('keydown', (ev) => handleBarKey(ev, r));
    svg.appendChild(r);
    if (i % Math.ceil(data.length / 8 || 1) === 0) svg.appendChild(el('text', {{x, y:h-6, class:'label'}}, b.id));
  }});
}}
function renderTreemap() {{
  const root = qs('#map'), tree = snapshot.tree;
  const w = root.clientWidth || 800, h = root.clientHeight || 600;
  const previousWorld = world;
  world = {{w, h}};
  if (!view || previousWorld.w !== w || previousWorld.h !== h) resetView();
  const rects = layoutTree(tree, 0, 0, w, h, 0);
  root.dataset.layout = 'squarified';
  root.dataset.bucket = bucket;
  root.innerHTML = `<svg viewBox="${{view.x}} ${{view.y}} ${{view.w}} ${{view.h}}" data-testid="treemap-canvas" data-layout="squarified" data-bucket="${{escapeHtml(bucket)}}"></svg><div class="zoomHud" id="zoomHud" aria-hidden="true">${{Math.round(currentZoom()*100)}}%</div>`;
  const svg = root.firstChild;
  const rectByPath = new Map();
  rects.forEach(r => {{
    if (r.w < 1 || r.h < 1) return;
    const isFrame = r.role === 'frame';
    const selectedClass = selected && selected.path && selected.path === (r.node.path || '') ? ' selected' : '';
    const color = isFrame ? '#151b24' : fileColor(r.node.path || r.node.name);
    const aspect = aspectRatio(r.w, r.h);
    const tile = el('rect', {{
      x:r.x, y:r.y, width:Math.max(0,r.w), height:Math.max(0,r.h), fill:color, class:(isFrame ? 'frame' : 'tile') + selectedClass,
      'data-testid':isFrame ? 'treemap-frame' : 'treemap-tile',
      'data-probe':'treemap-tile',
      'data-role':r.role,
      'data-depth':r.depth,
      'data-node-id':r.node.id || '',
      'data-node-kind':r.node.kind || '',
      'data-node-path':r.node.path || '',
      'data-layout':'squarified',
      'data-layout-orientation':r.orientation || 'unknown',
      'data-w':r.w.toFixed(2),
      'data-h':r.h.toFixed(2),
      'data-aspect-ratio':aspect.toFixed(3),
      tabindex:'0',
      role:'button',
      'aria-label':`${{isFrame ? 'Open group' : 'Select file'}} ${{r.node.name}} ${{r.node.size || 0}} bytes`
    }});
    tile.addEventListener('click', (ev) => {{
      ev.stopPropagation();
      if (performance.now() - lastPanAt < PAN_CLICK_SUPPRESS_MS) return;
      selectNode(r.node, tile);
    }});
    tile.addEventListener('keydown', (ev) => handleTileKey(ev, r.node, tile));
    svg.appendChild(tile);
    if (r.node.path) rectByPath.set(r.node.path, r);
    if (isFrame && r.w > 40 && r.h > 16) addWrappedLabel(svg, r, r.node.name, 'frameLabel', 2);
    else if (!isFrame && r.w > 24 && r.h > 12) addWrappedLabel(svg, r, r.node.name, 'label', currentZoom() > 1.8 ? 5 : 3);
  }});
  if (selected && selected.path && selectedSymbols && currentZoom() >= SYMBOL_ZOOM_THRESHOLD) {{
    const targetRect = rectByPath.get(selected.path);
    if (targetRect) renderSymbolOverlay(svg, targetRect, selectedSymbols);
  }}
  bindPanZoom(root, svg);
  updateZoomHud();
}}

function activateBar(bar) {{
  document.querySelectorAll('.bar').forEach(n => n.classList.remove('active'));
  bar.classList.add('active');
}}

function selectNode(node, tile) {{
  selected = node;
  selectedSymbols = null;
  selectedSymbolError = null;
  selectedRelationships = null;
  renderDetail(node);
  document.querySelectorAll('.tile,.frame').forEach(n => n.classList.remove('selected'));
  tile.classList.add('selected');
  highlightBars(node.path || '');
  fetchSelectionDetails(node);
}}

function handleBarKey(ev, bar) {{
  if (ev.key !== 'Enter' && ev.key !== ' ') return;
  ev.preventDefault();
  activateBar(bar);
}}

function handleTileKey(ev, node, tile) {{
  if (ev.key !== 'Enter' && ev.key !== ' ') return;
  ev.preventDefault();
  selectNode(node, tile);
}}

async function fetchSelectionDetails(node) {{
  if (!node || !node.path || node.kind !== 'file') return;
  const projectPath = snapshot.project.path;
  const project = encodeURIComponent(projectPath);
  const file = encodeURIComponent(node.path);
  try {{
    const [symbols, relationships] = await Promise.all([
      fetch(BASE + '/api/symbols?project=' + project + '&file=' + file).then(r => r.json()),
      fetch(BASE + '/api/relationships?project=' + project + '&file=' + file).then(r => r.json())
    ]);
    if (!selected || selected.path !== node.path || !snapshot || snapshot.project.path !== projectPath) return;
    selectedSymbols = symbols.symbols || [];
    selectedSymbolError = symbols.health && symbols.health.status === 'error' ? symbols.health.message || 'Symbol provider error.' : null;
    selectedRelationships = relationships.relationships || {{}};
    renderDetail(selected);
    renderTreemap();
  }} catch (err) {{
    selectedSymbols = [];
    selectedSymbolError = String(err);
    selectedRelationships = {{quality:'error', uses:[], used_by:[], message:String(err)}};
    renderDetail(node);
  }}
}}

function resetView() {{
  const root = qs('#map');
  const w = root ? (root.clientWidth || 800) : 800;
  const h = root ? (root.clientHeight || 600) : 600;
  world = {{w, h}};
  view = {{x:0, y:0, w, h}};
}}

function currentZoom() {{
  if (!view || !world.w) return 1;
  return Math.max(1, world.w / Math.max(1, view.w));
}}

function setView(next) {{
  if (!next) return;
  const minW = Math.max(32, world.w / 12);
  const minH = Math.max(32, world.h / 12);
  const w = Math.min(world.w, Math.max(minW, next.w));
  const h = Math.min(world.h, Math.max(minH, next.h));
  const x = Math.min(Math.max(0, next.x), Math.max(0, world.w - w));
  const y = Math.min(Math.max(0, next.y), Math.max(0, world.h - h));
  view = {{x, y, w, h}};
  const svg = qs('#map svg');
  if (svg) svg.setAttribute('viewBox', `${{x}} ${{y}} ${{w}} ${{h}}`);
  updateZoomHud();
  if (selectedSymbols && selected && currentZoom() >= SYMBOL_ZOOM_THRESHOLD && !symbolFrame) {{
    symbolFrame = requestAnimationFrame(() => {{
      symbolFrame = 0;
      renderTreemap();
    }});
  }}
}}

function updateZoomHud() {{
  const hud = qs('#zoomHud');
  const zoom = currentZoom();
  const zoomText = Math.round(zoom * 100) + '%';
  if (hud) {{
    hud.textContent = zoomText;
    hud.title = zoom >= SYMBOL_ZOOM_THRESHOLD ? 'Symbol overlay enabled' : 'Zoom in to show symbol rectangles';
  }}
  document.querySelectorAll('.zoomPill').forEach(node => {{ node.textContent = zoomText + ' zoom'; }});
}}

function bindPanZoom(root, svg) {{
  root.onwheel = (ev) => {{
    ev.preventDefault();
    const box = svg.getBoundingClientRect();
    const px = (ev.clientX - box.left) / Math.max(1, box.width);
    const py = (ev.clientY - box.top) / Math.max(1, box.height);
    const factor = ev.deltaY < 0 ? 0.84 : 1.19;
    const nw = view.w * factor;
    const nh = view.h * factor;
    setView({{x:view.x + view.w * px - nw * px, y:view.y + view.h * py - nh * py, w:nw, h:nh}});
  }};
  root.onkeydown = (ev) => {{
    if (!view) return;
    const stepX = view.w * 0.08;
    const stepY = view.h * 0.08;
    if (ev.key === '+' || ev.key === '=') {{
      ev.preventDefault();
      setView({{x:view.x + view.w * 0.08, y:view.y + view.h * 0.08, w:view.w * 0.84, h:view.h * 0.84}});
    }} else if (ev.key === '-' || ev.key === '_') {{
      ev.preventDefault();
      setView({{x:view.x - view.w * 0.095, y:view.y - view.h * 0.095, w:view.w * 1.19, h:view.h * 1.19}});
    }} else if (ev.key === 'ArrowLeft') {{
      ev.preventDefault();
      setView({{...view, x:view.x - stepX}});
    }} else if (ev.key === 'ArrowRight') {{
      ev.preventDefault();
      setView({{...view, x:view.x + stepX}});
    }} else if (ev.key === 'ArrowUp') {{
      ev.preventDefault();
      setView({{...view, y:view.y - stepY}});
    }} else if (ev.key === 'ArrowDown') {{
      ev.preventDefault();
      setView({{...view, y:view.y + stepY}});
    }} else if (ev.key === '0') {{
      ev.preventDefault();
      resetView();
      setView(view);
    }}
  }};
  root.onpointerdown = (ev) => {{
    root.setPointerCapture(ev.pointerId);
    activePointers.set(ev.pointerId, {{x:ev.clientX, y:ev.clientY}});
    root.classList.add('is-panning');
    if (activePointers.size === 1) panStart = {{x:ev.clientX, y:ev.clientY, view:{{...view}}}};
    if (activePointers.size === 2) {{
      const pts = Array.from(activePointers.values());
      pinchStart = {{distance:pointerDistance(pts[0], pts[1]), view:{{...view}}, center:pointerCenter(pts[0], pts[1]), box:svg.getBoundingClientRect()}};
    }}
  }};
  root.onpointermove = (ev) => {{
    if (!activePointers.has(ev.pointerId)) return;
    activePointers.set(ev.pointerId, {{x:ev.clientX, y:ev.clientY}});
    if (activePointers.size >= 2 && pinchStart) {{
      lastPanAt = performance.now();
      const pts = Array.from(activePointers.values());
      const dist = pointerDistance(pts[0], pts[1]);
      const center = pointerCenter(pts[0], pts[1]);
      const scale = pinchStart.distance / Math.max(1, dist);
      const px = (center.x - pinchStart.box.left) / Math.max(1, pinchStart.box.width);
      const py = (center.y - pinchStart.box.top) / Math.max(1, pinchStart.box.height);
      const nw = pinchStart.view.w * scale;
      const nh = pinchStart.view.h * scale;
      setView({{x:pinchStart.view.x + pinchStart.view.w * px - nw * px, y:pinchStart.view.y + pinchStart.view.h * py - nh * py, w:nw, h:nh}});
      return;
    }}
    if (!panStart) return;
    if (Math.hypot(ev.clientX - panStart.x, ev.clientY - panStart.y) > 4) lastPanAt = performance.now();
    const dx = (ev.clientX - panStart.x) * panStart.view.w / Math.max(1, svg.clientWidth);
    const dy = (ev.clientY - panStart.y) * panStart.view.h / Math.max(1, svg.clientHeight);
    setView({{...panStart.view, x:panStart.view.x - dx, y:panStart.view.y - dy}});
  }};
  const end = (ev) => {{
    activePointers.delete(ev.pointerId);
    if (!activePointers.size) {{ root.classList.remove('is-panning'); panStart = null; pinchStart = null; }}
  }};
  root.onpointerup = end;
  root.onpointercancel = end;
}}

function pointerDistance(a, b) {{ return Math.hypot(a.x - b.x, a.y - b.y); }}
function pointerCenter(a, b) {{ return {{x:(a.x + b.x)/2, y:(a.y + b.y)/2}}; }}

function addWrappedLabel(svg, r, text, klass, maxLines) {{
  const labelRect = visibleLabelRect(r);
  if (!labelRect) return;
  const zoom = currentZoom();
  if (labelRect.w * zoom < 46 || labelRect.h * zoom < 14) return;
  // ViewBox zoom scales SVG text; convert fixed screen-pixel label metrics back to world units.
  const pad = 4 / zoom;
  const baseFont = klass === 'symbolLabel' ? 8 : (klass === 'frameLabel' ? 10 : 9);
  const lineHeight = 10 / zoom;
  const maxVisibleLines = Math.max(1, Math.floor((labelRect.h * zoom - 4) / 10));
  const label = el('text', {{x:labelRect.x + pad, y:labelRect.y + 11 / zoom, class:klass, 'font-size':Math.max(3, baseFont / zoom)}});
  const chars = Math.max(2, Math.floor((labelRect.w * zoom - 8) / (klass === 'symbolLabel' ? 4.4 : 5.2)));
  const lines = wrapByChars(String(text || ''), chars, Math.min(maxLines, maxVisibleLines));
  lines.forEach((line, i) => {{
    const tspan = el('tspan', {{x:labelRect.x + pad, dy:i ? lineHeight : 0}}, line);
    label.appendChild(tspan);
  }});
  svg.appendChild(label);
}}

function visibleLabelRect(r) {{
  if (!view) return r;
  const x1 = Math.max(r.x, view.x);
  const y1 = Math.max(r.y, view.y);
  const x2 = Math.min(r.x + r.w, view.x + view.w);
  const y2 = Math.min(r.y + r.h, view.y + view.h);
  const w = x2 - x1;
  const h = y2 - y1;
  return w > 0 && h > 0 ? {{...r, x:x1, y:y1, w, h}} : null;
}}

function wrapByChars(text, chars, maxLines) {{
  const clean = text.replace(/\\s+/g, ' ').trim();
  if (!clean) return [];
  const chunks = [];
  let rest = clean;
  while (rest.length && chunks.length < maxLines) {{
    chunks.push(rest.slice(0, chars));
    rest = rest.slice(chars);
  }}
  if (rest && chunks.length) chunks[chunks.length - 1] = chunks[chunks.length - 1].replace(/.$/, '…');
  return chunks;
}}

function renderSymbolOverlay(svg, rect, symbols) {{
  const flat = flattenSymbols(symbols).slice(0, 80);
  const zoom = currentZoom();
  if (!flat.length || rect.w * zoom < 88 || rect.h * zoom < 54) return;
  const total = Math.max(1, flat.reduce((sum, s) => sum + Math.max(1, s.size || 1), 0));
  const pad = 4 / zoom;
  const gap = 2 / zoom;
  const minH = 12 / zoom;
  let y = rect.y + pad;
  flat.forEach(sym => {{
    const h = Math.max(minH, Math.min(rect.h - pad * 2, (rect.h - pad * 2) * Math.max(1, sym.size || 1) / total));
    if (y + h > rect.y + rect.h - pad) return;
    const r = {{x:rect.x + pad, y, w:Math.max(0, rect.w - pad * 2), h, node:sym}};
    svg.appendChild(el('rect', {{x:r.x, y:r.y, width:r.w, height:r.h, rx:2, class:'symbolTile', 'data-symbol-kind':sym.kind || ''}}));
    if (r.w * zoom > 42 && r.h * zoom > 12) addWrappedLabel(svg, r, (sym.kind || 'symbol') + ' ' + sym.name, 'symbolLabel', 2);
    y += h + gap;
  }});
}}

function flattenSymbols(nodes) {{
  const out = [];
  (nodes || []).forEach(function visit(node) {{
    out.push(node);
    (node.children || []).forEach(visit);
  }});
  return out;
}}

function layoutTree(node, x, y, w, h, depth) {{
  const children = (node.children || []).filter(n => (n.size || 0) > 0);
  if (!children.length || w <= 0 || h <= 0) return [];
  const rects = squarify(children, x, y, w, h);
  const out = [];
  rects.forEach(r => {{
    const kids = r.node.children || [];
    const canNest = kids.length && r.w > 72 && r.h > 54;
    if (canNest && depth === 0) {{
      out.push({{...r, role:'frame', depth}});
      const header = Math.min(20, Math.max(14, r.h * 0.12));
      const inset = 3;
      const leaves = compactLeaves(collectLeaves(r.node), MAX_TILES_PER_FRAME);
      out.push(...squarify(
        leaves,
        r.x + inset,
        r.y + header,
        Math.max(0, r.w - inset * 2),
        Math.max(0, r.h - header - inset)
      ).map(tile => ({{...tile, role:'leaf', depth:depth + 1}})));
    }} else {{
      out.push({{...r, role:'leaf', depth}});
    }}
  }});
  return out;
}}

function collectLeaves(node) {{
  const kids = (node.children || []).filter(n => (n.size || 0) > 0);
  if (!kids.length) return [node];
  return kids.flatMap(collectLeaves);
}}

function compactLeaves(leaves, limit) {{
  if (leaves.length <= limit) return leaves;
  const sorted = leaves.slice().sort((a, b) => (b.size || 0) - (a.size || 0));
  const keep = sorted.slice(0, limit - 1);
  const rest = sorted.slice(limit - 1);
  const restSize = rest.reduce((sum, node) => sum + (node.size || 1), 0);
  keep.push({{id:'__other__', name:'other files', path:'', kind:'group', size:restSize, children:[]}});
  return keep;
}}

function squarify(nodes, x, y, w, h) {{
  const total = Math.max(1, nodes.reduce((s, n) => s + Math.max(1, n.size || 1), 0));
  const items = nodes
    .slice()
    .sort((a, b) => (b.size || 0) - (a.size || 0))
    .map(node => ({{node, area: Math.max(1, node.size || 1) * w * h / total}}));
  const rects = [];
  let row = [];
  let box = {{x, y, w, h}};
  while (items.length) {{
    const next = items[0];
    if (!row.length || worst(row.concat([next]), Math.min(box.w, box.h)) <= worst(row, Math.min(box.w, box.h))) {{
      row.push(next);
      items.shift();
    }} else {{
      const laid = layoutRow(row, box);
      rects.push(...laid.rects);
      box = laid.rest;
      row = [];
    }}
  }}
  if (row.length) rects.push(...layoutRow(row, box).rects);
  return rects;
}}

function worst(row, side) {{
  if (!row.length || side <= 0) return Infinity;
  const sum = row.reduce((s, i) => s + i.area, 0);
  const min = Math.min(...row.map(i => i.area));
  const max = Math.max(...row.map(i => i.area));
  if (min <= 0 || sum <= 0) return Infinity;
  return Math.max((side * side * max) / (sum * sum), (sum * sum) / (side * side * min));
}}

function layoutRow(row, box) {{
  const sum = row.reduce((s, i) => s + i.area, 0);
  const rects = [];
  if (box.w < box.h) {{
    const rowH = Math.min(box.h, sum / Math.max(1, box.w));
    let cx = box.x;
    row.forEach((item, idx) => {{
      const rw = idx === row.length - 1 ? box.x + box.w - cx : item.area / Math.max(1, rowH);
      rects.push({{node:item.node, x:cx, y:box.y, w:Math.max(0, rw), h:Math.max(0, rowH), orientation:'row'}});
      cx += rw;
    }});
    return {{rects, rest:{{x:box.x, y:box.y + rowH, w:box.w, h:Math.max(0, box.h - rowH)}}}};
  }}
  const colW = Math.min(box.w, sum / Math.max(1, box.h));
  let cy = box.y;
  row.forEach((item, idx) => {{
    const rh = idx === row.length - 1 ? box.y + box.h - cy : item.area / Math.max(1, colW);
    rects.push({{node:item.node, x:box.x, y:cy, w:Math.max(0, colW), h:Math.max(0, rh), orientation:'column'}});
    cy += rh;
  }});
  return {{rects, rest:{{x:box.x + colW, y:box.y, w:Math.max(0, box.w - colW), h:box.h}}}};
}}
function renderHealth() {{
  const panel = qs('#provider-health');
  if (!panel) return;
  const projects = (projectHealth && projectHealth.projects) || {{status:'pending', message:'Project discovery not loaded.'}};
  const git = (snapshot && snapshot.health && snapshot.health.git) || {{status:'pending', message:'Snapshot not loaded.'}};
  const cache = (snapshot && snapshot.cache) || {{status:'pending', message:'Snapshot cache not checked.'}};
  const rows = [
    {{name:'projects', status:projects.status, cache:'-', errors:'0', message:projects.message || '', rawRef:''}},
    {{name:'snapshot', status:git.status, cache:cache.status || '-', errors:'0', message:git.message || '', rawRef:''}}
  ];
  (providerSnapshot && providerSnapshot.providers || []).forEach(provider => {{
    const cacheInfo = provider.cache || {{}};
    const age = typeof cacheInfo.age_s === 'number' ? Math.round(cacheInfo.age_s) + 's' : '-';
    const rawRef = provider.raw_ref || '';
    const errors = Array.isArray(provider.errors) ? provider.errors.length : 0;
    rows.push({{
      name: provider.source || provider.provider || 'provider',
      status: provider.status || (provider.health && provider.health.status) || 'unknown',
      cache: (cacheInfo.status || '-') + (age === '-' ? '' : ' ' + age),
      errors: String(errors),
      message: provider.message || (provider.health && provider.health.message) || '',
      rawRef
    }});
  }});
  panel.innerHTML = '<h2>Providers</h2>' + rows.map(row => {{
    const normalized = String(row.status || 'unknown').toLowerCase();
    const cacheStatus = String(row.cache || '-');
    const raw = row.rawRef ? 'raw: ' + row.rawRef : row.message;
    return `<div class="healthRow" data-provider="${{escapeHtml(row.name)}}" data-status="${{escapeHtml(normalized)}}" data-cache="${{escapeHtml(cacheStatus)}}" data-error-count="${{escapeHtml(row.errors)}}" title="${{escapeHtml(raw || '')}}"><span>${{escapeHtml(row.name)}}</span><span class="healthStatus health-${{escapeHtml(normalized)}}">${{escapeHtml(normalized)}}</span><span class="muted">${{escapeHtml(cacheStatus)}}</span><span class="muted">${{escapeHtml(row.errors)}}</span><span class="muted">${{escapeHtml(raw || '')}}</span></div>`;
  }}).join('');
}}
function renderDetail(n) {{
  const detail = qs('#selection-detail');
  if (!detail) return;
  if (!n) {{
    detail.innerHTML = '<div class="muted">Select a rectangle.</div>';
    return;
  }}
  const symbols = flattenSymbols(selectedSymbols || []);
  const rel = selectedRelationships || {{}};
  const uses = rel.uses || [];
  const usedBy = rel.used_by || [];
  const symbolNote = n.kind === 'file'
    ? (selectedSymbolError ? 'Symbol provider error' : (selectedSymbols ? `${{symbols.length}} symbol(s)` : 'Loading symbols…'))
    : 'Zoom or select a file to load symbols.';
  detail.innerHTML = `
    <h2>${{escapeHtml(n.name)}}</h2>
    <div class="detailGrid">
      <span class="muted">Kind</span><span>${{escapeHtml(n.kind)}}</span>
      <span class="muted">Size</span><span>${{formatBytes(n.size || 0)}}</span>
      <span class="muted">Path</span><code>${{escapeHtml(n.path||'')}}</code>
      <span class="muted">Symbols</span><span>${{escapeHtml(symbolNote)}} <span class="pill zoomPill">${{Math.round(currentZoom()*100)}}% zoom</span></span>
      <span class="muted">Links</span><span><span class="pill">${{escapeHtml(rel.quality || 'loading')}}</span> <span class="muted">${{escapeHtml(rel.message || '')}}</span></span>
    </div>
    <h2>Uses</h2>
    ${{renderRelList(uses)}}
    <h2>Used By</h2>
    ${{renderRelList(usedBy)}}
    <h2>Symbols</h2>
    ${{renderSymbolList(symbols, selectedSymbolError)}}
  `;
}}

function renderRelList(items) {{
  if (!items || !items.length) return '<p class="muted">No relationships found by current provider.</p>';
  return '<ul class="relList">' + items.slice(0, 32).map(item => `<li><code>${{escapeHtml(item)}}</code></li>`).join('') + '</ul>';
}}

function renderSymbolList(items, error) {{
  if (error) return `<p class="muted">Symbol provider error: ${{escapeHtml(error)}}</p>`;
  if (!items || !items.length) return '<p class="muted">No symbols loaded for this selection.</p>';
  return '<ul class="symbolList">' + items.slice(0, 80).map(item => `<li><span class="pill">${{escapeHtml(item.kind || 'symbol')}}</span> <code>${{escapeHtml(item.name || '')}}</code> <span class="muted">L${{item.line_start || '?'}}</span></li>`).join('') + '</ul>';
}}

function formatBytes(size) {{
  return new Intl.NumberFormat(undefined, {{maximumFractionDigits:1}}).format(size) + ' bytes';
}}

function findNodeByPath(node, path) {{
  if (!node || !path) return null;
  if (node.path === path) return node;
  for (const child of (node.children || [])) {{
    const found = findNodeByPath(child, path);
    if (found) return found;
  }}
  return null;
}}
async function highlightBars(path) {{
  document.querySelectorAll('.bar').forEach(bar => bar.classList.remove('active'));
  if (!path) return;
  const project = encodeURIComponent(snapshot.project.path);
  const target = encodeURIComponent(path);
  const data = await fetch(BASE + '/api/touches?project=' + project + '&path=' + target + '&bucket=' + encodeURIComponent(bucket)).then(r => r.json());
  const touched = new Set(data.bucket_ids || []);
  document.querySelectorAll('.bar').forEach((bar, i) => {{
    const b = snapshot.histogram[i];
    bar.classList.toggle('active', touched.has(b.id));
  }});
}}
function updateBucketControls() {{
  document.querySelectorAll('#bucket-controls [data-bucket]').forEach(btn => {{
    const active = btn.dataset.bucket === bucket;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  }});
}}
function setBucket(next) {{
  if (!['day', 'week', 'month'].includes(next) || next === bucket) return;
  bucket = next;
  updateBucketControls();
  const url = new URL(location.href);
  url.searchParams.set('bucket', bucket);
  history.replaceState(null, '', url);
  loadSnapshot();
}}
function aspectRatio(w, h) {{
  const small = Math.max(1, Math.min(Math.abs(w), Math.abs(h)));
  const large = Math.max(Math.abs(w), Math.abs(h));
  return large / small;
}}
window.rigEvolveTreemapProbe = function rigEvolveTreemapProbe() {{
  const nodes = Array.from(document.querySelectorAll('[data-probe="treemap-tile"]'));
  const rects = nodes.map(node => {{
    const box = node.getBoundingClientRect();
    const ratio = aspectRatio(box.width, box.height);
    return {{
      id: node.dataset.nodeId || '',
      path: node.dataset.nodePath || '',
      role: node.dataset.role || '',
      kind: node.dataset.nodeKind || '',
      orientation: node.dataset.layoutOrientation || 'unknown',
      width: box.width,
      height: box.height,
      aspectRatio: ratio
    }};
  }}).filter(rect => rect.width > 0 && rect.height > 0);
  const orientations = Array.from(new Set(rects.map(rect => rect.orientation).filter(Boolean)));
  const aspects = rects.map(rect => rect.aspectRatio);
  const frameCount = rects.filter(rect => rect.role === 'frame').length;
  const leafCount = rects.filter(rect => rect.role === 'leaf').length;
  const maxAspect = aspects.length ? Math.max(...aspects) : 0;
  const minAspect = aspects.length ? Math.min(...aspects) : 0;
  const hasMixedOrientation = orientations.includes('row') && orientations.includes('column');
  return {{
    bucket,
    count: rects.length,
    frameCount,
    leafCount,
    orientations,
    hasMixedOrientation,
    minAspect,
    maxAspect,
    stripeOnly: rects.length > 4 && !hasMixedOrientation,
    rects
  }};
}};
function fileColor(path) {{ let h=0; for (const c of path) h=(h*31+c.charCodeAt(0))>>>0; return `hsl(${{h%360}} 48% 42%)`; }}
function el(name, attrs, text) {{ const n=document.createElementNS('http://www.w3.org/2000/svg',name); for (const [k,v] of Object.entries(attrs)) n.setAttribute(k,v); if (text) n.textContent=text; return n; }}
function escapeHtml(s) {{ return String(s).replace(/[&<>"]/g, c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c])); }}
qs('#reload').onclick = loadSnapshot; qs('#projects').onchange = loadSnapshot;
document.querySelectorAll('#bucket-controls [data-bucket]').forEach(btn => btn.addEventListener('click', () => setBucket(btn.dataset.bucket)));
const initialBucket = new URL(location.href).searchParams.get('bucket');
if (['day', 'week', 'month'].includes(initialBucket)) bucket = initialBucket;
updateBucketControls();
loadSnapshot().catch(e => {{
  hideLoading();
  qs('#selection-detail').textContent = 'Load failed: ' + e;
}});
loadProjects().catch(e => {{
  projectHealth = {{projects:{{status:'error', message:String(e)}}}};
  renderHealth();
}});
window.addEventListener('resize', () => {{
  if (!snapshot || resizeFrame) return;
  resizeFrame = requestAnimationFrame(() => {{
    resizeFrame = 0;
    render();
  }});
}});
</script></body></html>"""


def _safe_rel(path: str) -> str:
    rel = str(path or "").replace("\\", "/").strip("/")
    parts = [part for part in rel.split("/") if part and part not in {".", ".."}]
    return "/".join(parts)


def _snapshot_file_paths(project: Path) -> set[str]:
    key = (str(project.resolve()), _git_head(project))
    now = time.monotonic()
    cached = _SNAPSHOT_PATHS_CACHE.get(key)
    if cached and now - cached[0] < _SNAPSHOT_TTL_S:
        return set(cached[1])
    tree = build_file_tree(project)
    paths: set[str] = set()

    def visit(node: dict[str, Any]) -> None:
        if node.get("kind") == "file":
            path = str(node.get("path") or "")
            if path:
                paths.add(path)
        for child in node.get("children", []):
            visit(child)

    visit(tree)
    _SNAPSHOT_PATHS_CACHE[key] = (now, paths)
    _trim_snapshot_paths_cache()
    return paths


def _safe_project_file(project: Path, rel: str) -> Path | None:
    try:
        root = project.resolve(strict=True)
        target = (root / rel).resolve(strict=True)
        target.relative_to(root)
    except (OSError, ValueError):
        return None
    return target if target.is_file() else None


def _relationships(project: Path, rel: str) -> dict[str, Any]:
    if not rel:
        return {"uses": [], "used_by": [], "quality": "none"}
    head = _git_head(project)
    key = (str(project.resolve()), head)
    now = time.monotonic()
    cached = _RELATIONSHIP_INDEX_CACHE.get(key)
    if cached and now - cached[0] < _RELATIONSHIP_TTL_S:
        index = cached[1]
    else:
        index = _relationship_index(project)
        _RELATIONSHIP_INDEX_CACHE[key] = (now, index)
        _trim_relationship_cache()
    return _copy_relationship(
        index.get(rel, {"uses": [], "used_by": [], "quality": "heuristic-imports-v1", "message": _RELATIONSHIP_LIMIT_MESSAGE})
    )


def _relationship_index(project: Path) -> dict[str, dict[str, Any]]:
    files = _relationship_files(project)
    imports_by_file: dict[str, list[str]] = {}
    for candidate in files:
        target = _safe_project_file(project, candidate.as_posix())
        imports_by_file[candidate.as_posix()] = _imports_for(target) if target is not None else []
    uses_by_file = {candidate: _resolve_imports(candidate, imports, files) for candidate, imports in imports_by_file.items()}
    used_by_file: dict[str, list[str]] = {candidate.as_posix(): [] for candidate in files}
    for candidate, uses in uses_by_file.items():
        for target in uses:
            if target in used_by_file and candidate != target and candidate not in used_by_file[target]:
                used_by_file[target].append(candidate)
    return {
        candidate.as_posix(): {
            "uses": uses_by_file.get(candidate.as_posix(), [])[:32],
            "used_by": used_by_file.get(candidate.as_posix(), [])[:32],
            "quality": "heuristic-imports-v1",
            "message": _RELATIONSHIP_LIMIT_MESSAGE,
        }
        for candidate in files
    }


def _copy_relationship(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "uses": list(payload.get("uses", [])),
        "used_by": list(payload.get("used_by", [])),
        "quality": str(payload.get("quality", "heuristic-imports-v1")),
        "message": str(payload.get("message") or ""),
    }


def _relationship_files(project: Path) -> list[Path]:
    tree = build_file_tree(project)
    out: list[Path] = []

    def visit(node: dict[str, Any]) -> None:
        if node.get("kind") == "file":
            rel = Path(str(node.get("path") or ""))
            if rel.suffix.lower() in {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}:
                out.append(rel)
        for child in node.get("children", []):
            visit(child)

    visit(tree)
    return out[:_RELATIONSHIP_FILE_LIMIT]


def _imports_for(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    imports: list[str] = []
    for line in text.splitlines()[:_RELATIONSHIP_LINE_LIMIT]:
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            imports.append(stripped)
        elif " from " in stripped and ("import" in stripped or "export" in stripped):
            imports.append(stripped)
    return imports[:_RELATIONSHIP_IMPORT_LIMIT]


def _resolve_imports(rel: str, imports: list[str], files: list[Path]) -> list[str]:
    by_module = {path.with_suffix("").as_posix().replace("/", "."): path.as_posix() for path in files}
    by_stem: dict[str, list[str]] = {}
    for path in files:
        by_stem.setdefault(path.stem, []).append(path.as_posix())
    unique_by_stem = {stem: paths[0] for stem, paths in by_stem.items() if len(paths) == 1}
    file_set = {path.as_posix() for path in files}
    resolved: list[str] = []
    base = Path(rel).parent
    for imp in imports:
        token = _import_token(imp)
        if not token:
            continue
        if token.startswith("."):
            match = _resolve_relative_import(base.as_posix(), token, file_set)
        else:
            match = by_module.get(token) or unique_by_stem.get(token.rsplit(".", 1)[-1])
        if match and match not in resolved:
            resolved.append(match)
    return resolved


def _resolve_relative_import(base: str, token: str, files: set[str]) -> str | None:
    token = _normalize_relative_import_token(token)
    raw = posixpath.normpath(posixpath.join(base, token))
    suffixes = ["", ".ts", ".tsx", ".js", ".jsx", ".py", ".mjs", ".cjs", ".mts", ".cts"]
    candidates = [raw]
    if not Path(raw).suffix:
        candidates.extend(raw + suffix for suffix in suffixes[1:])
    for suffix in suffixes[1:]:
        candidates.append(posixpath.join(raw, "index" + suffix))
    for candidate in dict.fromkeys(candidates):
        if candidate in files:
            return candidate
    return None


def _normalize_relative_import_token(token: str) -> str:
    if not token.startswith(".") or "/" in token:
        return token
    dots = len(token) - len(token.lstrip("."))
    module = token[dots:].replace(".", "/")
    return posixpath.join(*([".."] * max(0, dots - 1)), module) if module else ""


def _import_token(line: str) -> str:
    text = line.strip().strip(";")
    if " from " in text:
        return text.rsplit(" from ", 1)[-1].strip().strip("'\"")
    if text.startswith("from "):
        return text.split()[1].strip("'\"")
    if text.startswith("import "):
        return text[len("import ") :].split(",", 1)[0].strip().strip("'\"")
    return ""


def _strip_base_path(path: str) -> str:
    if path == "/evolve":
        return "/"
    if path.startswith("/evolve/"):
        stripped = path[len("/evolve") :]
        return stripped or "/"
    return path


def _git_head(repo_root: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "nogit"
    return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else "nogit"


def _trim_snapshot_cache() -> None:
    if len(_SNAPSHOT_CACHE) <= 16:
        return
    for key, _ in sorted(_SNAPSHOT_CACHE.items(), key=lambda item: item[1][0])[: len(_SNAPSHOT_CACHE) - 16]:
        _SNAPSHOT_CACHE.pop(key, None)


def _trim_snapshot_paths_cache() -> None:
    if len(_SNAPSHOT_PATHS_CACHE) <= 16:
        return
    for key, _ in sorted(_SNAPSHOT_PATHS_CACHE.items(), key=lambda item: item[1][0])[: len(_SNAPSHOT_PATHS_CACHE) - 16]:
        _SNAPSHOT_PATHS_CACHE.pop(key, None)


def _trim_relationship_cache() -> None:
    if len(_RELATIONSHIP_INDEX_CACHE) <= 16:
        return
    for key, _ in sorted(_RELATIONSHIP_INDEX_CACHE.items(), key=lambda item: item[1][0])[
        : len(_RELATIONSHIP_INDEX_CACHE) - 16
    ]:
        _RELATIONSHIP_INDEX_CACHE.pop(key, None)


def _provider_cache_meta(
    cache: Any,
    key: Any,
    *,
    status: str,
    message: str = "",
) -> dict[str, Any]:
    path = cache.path_for(key)
    meta: dict[str, Any] = {
        "status": status,
        "path": str(path),
        "key": key.digest(),
    }
    try:
        meta["age_s"] = max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        meta["age_s"] = None
    if message:
        meta["message"] = message
    return meta
