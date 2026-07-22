#!/usr/bin/env bash
# install.sh — install the `rig` CLI (Python 3).
# Works from a local clone (./install.sh) and piped from curl:
#   curl -fsSL https://raw.githubusercontent.com/alex-mextner/rig-cli/main/install.sh | bash
set -euo pipefail

# ── identity ──────────────────────────────────────────────────────────────────
TOOL="rig"
REPO="rig-cli"
GITHUB_USER="alex-mextner"
ENTRY="bin/rig"   # path inside repo root
CLONE_BASE="${XDG_DATA_HOME:-$HOME/.local/share}"

# ── locate source dir ─────────────────────────────────────────────────────────
_script_dir=""
if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "bash" ]]; then
  _script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [[ -n "$_script_dir" && -f "$_script_dir/$ENTRY" ]]; then
  SRC="$_script_dir"
  echo "rig: using local clone at $SRC"
else
  mkdir -p "$CLONE_BASE"
  CLONE_DIR="$CLONE_BASE/$REPO"
  EXPECT_URL="https://github.com/$GITHUB_USER/$REPO.git"
  if [[ -d "$CLONE_DIR/.git" ]]; then
    actual_url="$(git -C "$CLONE_DIR" remote get-url origin 2>/dev/null || echo "")"
    if [[ "$actual_url" != "$EXPECT_URL" ]]; then
      echo "ERROR: $CLONE_DIR exists but its origin is '$actual_url', not $EXPECT_URL." >&2
      echo "       Remove that directory or fix its remote, then re-run." >&2
      exit 1
    fi
    echo "rig: updating existing clone at $CLONE_DIR"
    git -C "$CLONE_DIR" pull --ff-only
  else
    echo "rig: cloning $EXPECT_URL into $CLONE_DIR"
    git clone "$EXPECT_URL" "$CLONE_DIR"
  fi
  SRC="$CLONE_DIR"
fi

# ── bin dir ───────────────────────────────────────────────────────────────────
BIN="$HOME/.local/bin"
mkdir -p "$BIN"

if [[ ":$PATH:" != *":$BIN:"* ]]; then
  echo ""
  echo "  NOTE: $BIN is not on your PATH."
  echo "  Add this to your ~/.bashrc or ~/.zshrc and restart your shell:"
  echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
  echo ""
fi

# ── core runtime dependencies ─────────────────────────────────────────────────
# pyyaml, textual, and rich are all CORE deps (not optional extras). Install them
# into the SAME interpreter rig runs under. Prefer `uv` (the toolchain rig users
# standardize on) over a bare `pip`; fall back to `python3 -m pip --user` only when uv is
# absent. NOTE: this is best-effort and non-fatal — on an externally-managed system Python
# (PEP-668) BOTH uv and pip refuse a system-wide install, so the warning below fires and
# `rig doctor` / a managed (uv-tool) install is the real fix.
_missing_core=()
python3 -c 'import yaml' 2>/dev/null     || _missing_core+=("pyyaml")
python3 -c 'import textual' 2>/dev/null  || _missing_core+=("textual>=0.50")
python3 -c 'import rich' 2>/dev/null     || _missing_core+=("rich>=13")
if [[ "${#_missing_core[@]}" -gt 0 ]]; then
  py3="$(command -v python3)"
  pkgs="${_missing_core[*]}"
  if command -v uv >/dev/null 2>&1; then
    echo "rig: core deps missing, attempting: uv pip install --python $py3 $pkgs"
    uv pip install --python "$py3" "${_missing_core[@]}" 2>/dev/null && _core_ok=1
  else
    echo "rig: core deps missing, attempting: python3 -m pip install --user $pkgs"
    python3 -m pip install --user "${_missing_core[@]}" 2>/dev/null && _core_ok=1
  fi
  if [[ "${_core_ok:-0}" != "1" ]]; then
    echo ""
    echo "  WARNING: could not install core deps ($pkgs)."
    echo "  rig init TUI wizard and config parsing will fail until they are present."
    echo "  Install manually: uv pip install $pkgs   (or run: rig doctor --yes)"
    echo ""
  fi
fi

# ── symlink entry ─────────────────────────────────────────────────────────────
ENTRY_PATH="$SRC/$ENTRY"
chmod +x "$ENTRY_PATH"
ln -sfn "$ENTRY_PATH" "$BIN/$TOOL"
echo "rig: symlinked $BIN/$TOOL -> $ENTRY_PATH"

# ── register skill ────────────────────────────────────────────────────────────
if ! "$BIN/$TOOL" install-skill; then
  echo "  WARNING: '$TOOL install-skill' failed — $TOOL is installed but agents may not"
  echo "           auto-discover it. Re-run '$TOOL install-skill' manually to fix."
fi

# ── done ──────────────────────────────────────────────────────────────────────
echo ""
echo "  rig is installed."
echo "  Usage: rig doctor              — check/install dependencies"
echo "         rig init                — onboarding: scaffold rig.yaml + preview (wizard/--yes; --apply to apply)"
echo "         rig apply               — apply: reconcile the repo to rig.yaml (idempotent)"
echo "         rig status              — report config↔disk drift (both ways)"
echo "         rig config get|set      — read/change one key (dot path), then reconcile"
echo "         rig --help              — full usage"
echo ""
