# Spec: rig cross-harness provisioning of skills + subagents

## 1. Problem

rig provisions **skills** today: copy each enabled skill into `skills_target`
(`~/.agents/skills`), then for *skills-directory* harnesses symlink each one into the
harness's own discovery dir; for *instruction-file* harnesses emit a `plan.notes`
status string. The mapping lives in one registry, `riglib/harness_skills.py`
(`HARNESS_SKILL_DIRS` + `HARNESS_INSTRUCTION_FILES`).

Two gaps:

1. **No subagents.** Harnesses now ship a native *subagent* primitive
   (`~/.claude/agents/*.md`, `~/.config/opencode/agents/*.md`, `~/.gemini/agents/*.md`)
   that is a sibling concept to skills but with its **own** discovery dir and its own
   per-harness frontmatter dialect. rig has no `subagents` catalog category, no
   discovery registry, no plan/drift/status path. (Task #43, greenfield — branch and
   worktree sit at main's HEAD with zero subagent code.)
2. **The skills registry is partly wrong on disk.** Verified against live installs on
   this machine:
   - `~/.config/opencode/skill` (registry value, singular) **does not exist**; opencode
     also has no `skills/` dir. opencode discovers skills natively from `~/.agents/skills`
     (= `skills_target`). The registered symlink target is dead.
   - codex ships a **native** skills system (`~/.codex/skills/`) and its loader reads
     `$HOME/.agents/skills` (= `skills_target`). It is mis-classified as instruction-file;
     it needs zero AGENTS.md skill block.
   - These two are correctness bugs independent of subagents and ship first.

## 2. Verified ground truth (this machine, 2026-06-22)

| Harness | skills dir on disk | subagent dir on disk | reads `~/.agents/skills`? |
|---|---|---|---|
| claude-code | `~/.claude/skills` (real) | `~/.claude/agents` (`*.md`, empty) | — |
| opencode | none (`skill`/`skills` absent) | `~/.config/opencode/agents/` (**plural**, has `read-only-reviewer.md`) | yes (native root) |
| codex | `~/.codex/skills/` (native) | none (no `~/.codex/agents`) | yes (loader root) |
| gemini | none | `~/.gemini/agents/` (documented, **not present yet**) | no |
| pi | not installed | by design **none** | reads `.agents/skills` per docs |
| commandcode | not installed (`~/.commandcode` absent) | none documented | `.agents/skills` back-compat per docs |

Hard facts that override the facets' guesses:
- opencode subagent dir is **`agents/` (plural)**, not the inferred singular `agent/`.
- opencode subagent file format (read from the live file): YAML frontmatter
  `description:` + `mode: primary|subagent` + a `permission:` map (`bash/edit/write/
  webfetch/task/todowrite/websearch/lsp/skill → deny|ask|allow`), body = system prompt.
- `~/.agents/subagents` does not exist yet.

## 3. Design

### 3.1 Two independent per-harness axes
A harness's skills-discovery and subagents-discovery are **separate** axes (gemini =
instruction-file for skills, dir-based for subagents). So model subagents with a parallel
registry, never folded into the skills table.

Add to `riglib/harness_skills.py`:

```python
HARNESS_SUBAGENT_DIRS: dict[str, str] = {
    "claude-code": "~/.claude/agents",
    "opencode":    "~/.config/opencode/agents",   # PLURAL (verified on disk)
    "gemini":      "~/.gemini/agents",             # documented native dir
}
# codex / pi / commandcode: no native subagent dir → instruction-file fallback note.
```
plus accessors `subagent_dir_for(kind)`, `harness_links_subagents(kind)` mirroring the
existing skill helpers. `KNOWN_HARNESS_KINDS` stays the union of skills + instruction
families and is unchanged (subagent kinds are a subset of already-known kinds).

### 3.2 Skills registry corrections (independent of subagents)
- **codex**: leave its `~/.codex/AGENTS.md` entry as its *instruction file* record, but
  make skills resolve via `skills_target` natively — emit a "discovers skills natively
  from `~/.agents/skills`; no link or AGENTS.md block needed" note, NOT an AGENTS.md skill
  listing. (Keeps the table honest; codex already loads `~/.agents/skills`.)
- **opencode**: its native skill root is `~/.agents/skills`. Either (a) drop the dead
  `~/.config/opencode/skill` symlink and emit a "discovers natively" note, or (b) if a
  belt-and-suspenders symlink is kept, fix it to the actually-loaded dir. Default to (a):
  the dead singular target is the smallest, safest fix and matches on-disk reality.
- General rule introduced: **when `skills_target` IS a harness's native discovery root,
  emit no symlink and record a `discovers from skills_target natively` note** — applies to
  codex and opencode today, and is the cleanest answer to the "identity symlink?" question.

### 3.3 Subagents catalog category + scanner
- Add `"subagents"` to `_VALID_CATEGORIES` (`riglib/config.py`) and a schema entry — without
  it the validator fail-closes on a `subagents:` config block.
- Add `_scan_subagents()` to `catalog.py`. Source layout: `subagents/<name>/SUBAGENT.md`
  (folder-per-agent, mirrors `skills/<name>/SKILL.md`) carrying **canonical** frontmatter
  (`name`, `description`, optional `tools`, optional `permission`). Emits
  `Item(category="subagents", description=...)`.
- Do **not** tighten `is_agent_tools_checkout()` to require `subagents/` — an older checkout
  without it must degrade (scanner finds nothing), not error.
- Add `_BUILTIN_TARGETS["subagents"] = "~/.agents/subagents"` and
  `_DEFAULTS_KEY["subagents"] = "subagents_target"`.

### 3.4 Plan/drift/status — clone the skills path
Five touchpoints, each a sibling of the skills equivalent:
1. `_resolve_harness_subagent_dir()` + `_subagent_discovery_note()` (siblings of plan.py's
   `_resolve_harness_skill_dir`/`_skill_discovery_note`), keyed off the same harness-kind
   resolver.
2. `build()` emits `copy_subagent` (into `subagents_target`) + `link_subagent_harness`
   (symlink per enabled subagent into the harness dir) — parallel to
   `copy_skill`/`link_skill_harness`. Add both to the `Action.kind` enum.
3. **Per-harness frontmatter translation** (the real work, not the dir mapping): the
   canonical `SUBAGENT.md` is translated to each harness's dialect at link/render time:
   - claude-code: `name`/`description`/`tools` → `~/.claude/agents/<name>.md`.
   - opencode: `description` + `mode: subagent` + `permission:` map → `~/.config/opencode/agents/<name>.md`.
   - gemini: `name`/`description`/`tools` (+ optional `model`/`temperature`) → `~/.gemini/agents/<name>.md`.
   Because dialects diverge, `link_subagent_harness` is a **render-and-write**, not a bare
   symlink (skills are a symlink because `SKILL.md` is identical across harnesses; subagents
   are not). This is the one place the subagent path is NOT a literal clone of skills.
4. `drift.py`: `_check_subagent_harness_link()` clones `_check_skill_harness_link` —
   missing/modified, ignore a real hand-authored file.
5. Status: no new surface — `_print_plan` already prints `plan.actions` + `plan.notes`.

### 3.5 Instruction-file fallback for codex/pi/commandcode subagents
These have no native subagent dir. v1 behavior: **note-only**, symmetric with how skills
treat instruction-file harnesses today (skills do NOT auto-inject descriptions into
AGENTS.md). `_subagent_discovery_note()` returns e.g. `subagents: harness 'codex' has no
agent-discovery dir — reads ~/.codex/AGENTS.md`. For pi specifically the note states
"pi omits subagents by design". A generated `<!-- rig:subagents -->` managed block in the
instruction file is **deferred** (Phase 3) — it is advisory prose only and these harnesses
can't load a callable agent from it, so capability parity is not 1:1 regardless.

## 4. Open questions to resolve before Phase 2/3 (NOT blocking Phase 1)
- **pi path**: registry says `~/.config/pi/AGENTS.md`; pi docs say `~/.pi/agent/`. pi not
  installed — verify against pi source before relying on either. (Phase 3, pi-touching.)
- **commandcode**: is its global instruction file really `~/.commandcode/AGENTS.md`, and
  does it have a native `~/.commandcode/skills/` worth re-classifying to a skills-dir
  harness? Not installed — defer to Phase 3.
- **canonical subagent frontmatter** vs harness-native: spec picks canonical+translate;
  confirm with owner before coding the translation layer (3.4 step 3).
- **gemini `name` slug rule** (lowercase/digit/hyphen/underscore) — validate at render.

## 5. Non-goals (v1)
- No managed AGENTS.md/GEMINI.md skill or subagent *block injection* (note-only, matches
  skills).
- No pi/commandcode subagent provisioning (no native dir; not installed to verify).
- No change to `agents_md` (the repo AGENTS.md/CLAUDE.md guide) — orthogonal area.