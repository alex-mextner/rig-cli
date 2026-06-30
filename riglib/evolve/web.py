"""Local web app for `rig evolve`.

Accessed via: `rig evolve _serve` from the shared service manager, or directly in tests through
`EvolveApp`. The server is read-only in this first slice: it exposes project snapshots and a
self-contained interactive page, so there are no write endpoints to protect with CSRF yet.
"""

from __future__ import annotations

import html
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .git_index import build_histogram, build_path_touches, git_health
from .structure import build_file_tree
from .model import PROVIDER_SCHEMA

HOST = "127.0.0.1"
DEFAULT_PORT = 8797
PAGE_TITLE = "rig evolve"
_SNAPSHOT_TTL_S = 300.0
_SNAPSHOT_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}


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
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>{PAGE_TITLE}</title>
<style>
  :root {{ color-scheme: dark; --bg:#111318; --fg:#eef2f6; --surface:#151922; --panel:#1b1f27; --border:#2d3442; --muted:#9aa6b2; --accent:#f59e0b; --focus:#93c5fd; --ok:#22c55e; --warn:#f59e0b; --bad:#ef4444; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg); font:13px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif; overflow:hidden; -webkit-tap-highlight-color:rgba(147,197,253,.22); }}
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
  #map {{ position:relative; min-width:0; overflow:hidden; background:#10141b; touch-action:pan-x pan-y pinch-zoom; }}
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
  .label {{ fill:var(--fg); font-size:11px; pointer-events:none; }}
  .frameLabel {{ fill:#cbd5e1; font-size:11px; font-weight:650; pointer-events:none; }}
  .muted {{ color:var(--muted); }}
  code {{ color:#bfdbfe; }}
  @media (prefers-reduced-motion: reduce) {{ *,*::before,*::after {{ animation-duration:.001ms !important; animation-iteration-count:1 !important; scroll-behavior:auto !important; transition-duration:.001ms !important; }} }}
  @media (max-width: 760px) {{ header {{ gap:8px; padding:0 10px; }} #projects {{ min-width:130px; max-width:36vw; }} #work {{ grid-template-columns:1fr; }} #detail {{ display:none; }} main {{ grid-template-rows:140px 1fr; }} }}
</style></head><body>
<a class="skip-link" href="#main">Skip to Project Surface</a>
<header><h1 translate="no">rig evolve</h1><span class="muted projectLabel">project</span><select id="projects" name="project" aria-label="Project"></select><div id="bucket-controls" data-testid="bucket-controls" role="group" aria-label="Timeline Bucket"><button type="button" data-bucket="day" aria-label="Show Day Buckets">Day</button><button type="button" data-bucket="week" aria-label="Show Week Buckets">Week</button><button type="button" data-bucket="month" aria-label="Show Month Buckets" class="active" aria-pressed="true">Month</button></div><button id="reload" aria-label="Reload Snapshot">Reload</button><span class="muted projectPath" title="{repo}">{repo}</span></header>
<main id="main" tabindex="-1"><section id="hist" aria-label="Project activity histogram"></section><section id="work"><div id="map" aria-label="Project code surface"></div><aside id="detail" aria-label="Selection details"><section id="provider-health" data-testid="provider-health" aria-live="polite"><h2>Providers</h2><div class="healthRow" data-provider="git"><span>git</span><span class="healthStatus health-pending">pending</span><span class="muted">cache</span><span class="muted">errors</span><span class="muted">Snapshot not loaded.</span></div></section><section id="selection-detail"><div class="muted">Select a rectangle.</div></section></aside></section></main>
<script>
let snapshot = null;
let selected = null;
let projectHealth = {{}};
let providerSnapshot = null;
let bucket = 'month';
let resizeFrame = 0;
const MAX_TILES_PER_FRAME = 180;
const BASE = location.pathname.startsWith('/evolve') ? '/evolve' : '';
const qs = (s) => document.querySelector(s);

async function loadProjects() {{
  const data = await fetch(BASE + '/api/projects').then(r => r.json());
  projectHealth = data.health || {{}};
  const sel = qs('#projects');
  sel.innerHTML = '';
  data.projects.forEach(p => {{
    const opt = document.createElement('option');
    const aliases = (p.aliases || []).length ? ' aliases: ' + p.aliases.join(',') : '';
    opt.value = p.path; opt.textContent = p.name + aliases + ' · ' + (p.sources || []).join(',');
    opt.title = p.path;
    sel.appendChild(opt);
  }});
  renderHealth();
}}
async function loadSnapshot() {{
  const path = encodeURIComponent(qs('#projects').value || {repo!r});
  updateBucketControls();
  snapshot = await fetch(BASE + '/api/snapshot?path=' + path + '&bucket=' + encodeURIComponent(bucket)).then(r => r.json());
  selected = null; render();
  loadProviders().catch(err => {{
    providerSnapshot = {{providers:[{{source:'providers', status:'error', message:String(err), errors:[{{message:String(err)}}], cache:{{status:'error'}}}}]}};
    renderHealth();
  }});
}}
async function loadProviders() {{
  const project = encodeURIComponent(qs('#projects').value || {repo!r});
  providerSnapshot = await fetch(BASE + '/api/providers?project=' + project).then(r => r.json());
  renderHealth();
}}
function render() {{ renderHistogram(); renderTreemap(); renderHealth(); renderDetail(null); }}
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
  const rects = layoutTree(tree, 0, 0, w, h, 0);
  root.dataset.layout = 'squarified';
  root.dataset.bucket = bucket;
  root.innerHTML = `<svg viewBox="0 0 ${{w}} ${{h}}" data-testid="treemap-canvas" data-layout="squarified" data-bucket="${{escapeHtml(bucket)}}"></svg>`;
  const svg = root.firstChild;
  rects.forEach(r => {{
    if (r.w < 1 || r.h < 1) return;
    const isFrame = r.role === 'frame';
    const color = isFrame ? '#151b24' : fileColor(r.node.path || r.node.name);
    const aspect = aspectRatio(r.w, r.h);
    const tile = el('rect', {{
      x:r.x, y:r.y, width:Math.max(0,r.w), height:Math.max(0,r.h), fill:color, class:isFrame ? 'frame' : 'tile',
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
    tile.addEventListener('click', (ev) => {{ ev.stopPropagation(); selectNode(r.node, tile); }});
    tile.addEventListener('keydown', (ev) => handleTileKey(ev, r.node, tile));
    svg.appendChild(tile);
    if (isFrame && r.w > 58 && r.h > 18) svg.appendChild(el('text', {{x:r.x+5, y:r.y+14, class:'frameLabel'}}, r.node.name));
    else if (!isFrame && r.w > 48 && r.h > 18) svg.appendChild(el('text', {{x:r.x+4, y:r.y+13, class:'label'}}, r.node.name));
  }});
}}

function activateBar(bar) {{
  document.querySelectorAll('.bar').forEach(n => n.classList.remove('active'));
  bar.classList.add('active');
}}

function selectNode(node, tile) {{
  selected = node;
  renderDetail(node);
  document.querySelectorAll('.tile,.frame').forEach(n => n.classList.remove('selected'));
  tile.classList.add('selected');
  highlightBars(node.path || '');
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
  detail.innerHTML = n ? `<h2>${{escapeHtml(n.name)}}</h2><p class="muted">${{escapeHtml(n.kind)}} · ${{n.size}} bytes</p><p><code>${{escapeHtml(n.path||'')}}</code></p>` : '<div class="muted">Select a rectangle.</div>';
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
updateBucketControls();
loadProjects().then(loadSnapshot).catch(e => {{ qs('#selection-detail').textContent = 'Load failed: ' + e; }});
window.addEventListener('resize', () => {{
  if (!snapshot || resizeFrame) return;
  resizeFrame = requestAnimationFrame(() => {{
    resizeFrame = 0;
    render();
  }});
}});
</script></body></html>"""


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
