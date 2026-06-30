"""Tests for the interim symbol extraction layer behind `rig evolve`."""

from __future__ import annotations

from pathlib import Path


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_python_symbols_include_classes_methods_nested_functions_and_docstrings():
    from riglib.evolve.symbols import extract_symbols, flatten_symbols

    source = '''class Greeter:
    """Greets users.

    Extra detail.
    """

    def greet(self, name):
        """Return greeting."""
        prefix = "hi"

        def format_name(value):
            """Normalize input."""
            return value.strip()

        return f"{prefix} {format_name(name)}"


def helper():
    """Top helper."""
    return Greeter()
'''

    tree = extract_symbols(Path("src/app.py"), source)
    flat = flatten_symbols(tree)
    by_key = {(node["scope"], node["kind"], node["name"]): node for node in flat}

    greeter = by_key[("", "class", "Greeter")]
    greet = by_key[("Greeter", "method", "greet")]
    nested = by_key[("Greeter.greet", "function", "format_name")]
    helper = by_key[("", "function", "helper")]

    assert greeter["id"] == "symbol:src/app.py:<module>:class:Greeter"
    assert greet["id"] == "symbol:src/app.py:Greeter:method:greet"
    assert nested["id"] == "symbol:src/app.py:Greeter.greet:function:format_name"
    assert helper["id"] == "symbol:src/app.py:<module>:function:helper"

    assert greeter["line_start"] == 1
    assert greeter["line_end"] >= greet["line_end"]
    assert nested["line_start"] > greet["line_start"]
    assert nested["size_lines"] == nested["line_end"] - nested["line_start"] + 1
    assert nested["size_bytes"] > 0
    assert nested["size"] == nested["size_bytes"]

    assert greeter["doc"]["format"] == "python-docstring"
    assert greeter["doc"]["summary"] == "Greets users."
    assert greet["doc"]["summary"] == "Return greeting."
    assert nested["doc"]["summary"] == "Normalize input."
    assert helper["doc"]["summary"] == "Top helper."


def test_typescript_symbols_include_classes_functions_methods_nested_functions_and_jsdoc():
    from riglib.evolve.symbols import extract_symbols, flatten_symbols

    source = """/** Build shared labels.
 * @param name User name.
 * @returns Display label.
 */
export function makeLabel(name: string): string {
  function normalize(value: string) {
    return value.trim()
  }
  return normalize(name)
}

/** Coordinates work. */
export class Runner {
  /** Start the job.
   * @returns result count
   */
  run(count: number): number {
    return count
  }
}
"""

    tree = extract_symbols(Path("web/app.ts"), source)
    flat = flatten_symbols(tree)
    by_key = {(node["scope"], node["kind"], node["name"]): node for node in flat}

    make_label = by_key[("", "function", "makeLabel")]
    normalize = by_key[("makeLabel", "function", "normalize")]
    runner = by_key[("", "class", "Runner")]
    run = by_key[("Runner", "method", "run")]

    assert make_label["id"] == "symbol:web/app.ts:<module>:function:makeLabel"
    assert normalize["id"] == "symbol:web/app.ts:makeLabel:function:normalize"
    assert runner["id"] == "symbol:web/app.ts:<module>:class:Runner"
    assert run["id"] == "symbol:web/app.ts:Runner:method:run"

    assert make_label["parser"] == "js-regex-v1"
    assert make_label["language"] == "typescript"
    assert make_label["line_start"] == 5
    assert make_label["line_end"] == 10
    assert make_label["size_bytes"] > normalize["size_bytes"]

    assert make_label["doc"]["format"] == "jsdoc"
    assert make_label["doc"]["summary"] == "Build shared labels."
    assert {"tag": "param", "text": "name User name."} in make_label["doc"]["tags"]
    assert {"tag": "returns", "text": "Display label."} in make_label["doc"]["tags"]
    assert runner["doc"]["summary"] == "Coordinates work."
    assert run["doc"]["summary"] == "Start the job."
    assert {"tag": "returns", "text": "result count"} in run["doc"]["tags"]


def test_file_tree_preserves_file_level_default_without_symbol_children(tmp_path: Path):
    from riglib.evolve.structure import build_file_tree

    repo = tmp_path / "repo"
    _write(
        repo / "src" / "app.py",
        '''class Greeter:
    """Greets users."""

    def greet(self):
        return "hi"
''',
    )

    tree = build_file_tree(repo)
    src = next(child for child in tree["children"] if child["name"] == "src")
    app = next(child for child in src["children"] if child["name"] == "app.py")

    assert app["kind"] == "file"
    assert app["path"] == "src/app.py"
    assert app["size"] == (repo / "src" / "app.py").stat().st_size
    assert app["children"] == []


def test_file_tree_can_include_python_and_typescript_symbol_children(tmp_path: Path):
    from riglib.evolve.symbols import flatten_symbols
    from riglib.evolve.structure import build_file_tree

    repo = tmp_path / "repo"
    _write(
        repo / "src" / "app.py",
        '''class Greeter:
    """Greets users."""

    def greet(self, name):
        """Return greeting."""

        def format_name(value):
            """Normalize input."""
            return value.strip()

        return format_name(name)
''',
    )
    _write(
        repo / "web" / "app.ts",
        """/** Build shared labels.
 * @returns Display label.
 */
export function makeLabel(name: string): string {
  function normalize(value: string) {
    return value.trim()
  }
  return normalize(name)
}
""",
    )
    _write(
        repo / "web" / "util.js",
        """/** Build status text. */
const buildStatus = (value) => {
  function clamp(input) {
    return String(input).trim()
  }
  return clamp(value)
}
""",
    )

    tree = build_file_tree(repo, include_symbols=True)
    src = next(child for child in tree["children"] if child["name"] == "src")
    web = next(child for child in tree["children"] if child["name"] == "web")
    py_file = next(child for child in src["children"] if child["name"] == "app.py")
    ts_file = next(child for child in web["children"] if child["name"] == "app.ts")
    js_file = next(child for child in web["children"] if child["name"] == "util.js")

    assert py_file["size"] == (repo / "src" / "app.py").stat().st_size
    assert ts_file["size"] == (repo / "web" / "app.ts").stat().st_size
    assert js_file["size"] == (repo / "web" / "util.js").stat().st_size

    py_symbols = flatten_symbols(py_file["children"])
    ts_symbols = flatten_symbols(ts_file["children"])
    js_symbols = flatten_symbols(js_file["children"])
    by_py = {(node["scope"], node["kind"], node["name"]): node for node in py_symbols}
    by_ts = {(node["scope"], node["kind"], node["name"]): node for node in ts_symbols}
    by_js = {(node["scope"], node["kind"], node["name"]): node for node in js_symbols}

    greeter = by_py[("", "class", "Greeter")]
    greet = by_py[("Greeter", "method", "greet")]
    nested = by_py[("Greeter.greet", "function", "format_name")]
    make_label = by_ts[("", "function", "makeLabel")]
    normalize = by_ts[("makeLabel", "function", "normalize")]
    build_status = by_js[("", "function", "buildStatus")]
    clamp = by_js[("buildStatus", "function", "clamp")]

    assert greeter["id"] == "symbol:src/app.py:<module>:class:Greeter"
    assert greet["scope"] == "Greeter"
    assert nested["line_start"] > greet["line_start"]
    assert nested["doc"]["summary"] == "Normalize input."
    assert make_label["id"] == "symbol:web/app.ts:<module>:function:makeLabel"
    assert make_label["doc"]["format"] == "jsdoc"
    assert make_label["doc"]["summary"] == "Build shared labels."
    assert normalize["size"] == normalize["size_bytes"]
    assert build_status["id"] == "symbol:web/util.js:<module>:function:buildStatus"
    assert build_status["language"] == "javascript"
    assert build_status["doc"]["summary"] == "Build status text."
    assert clamp["scope"] == "buildStatus"
