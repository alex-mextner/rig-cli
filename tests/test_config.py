"""Config cascade + schema validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib import config


def _w(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


def test_repo_overrides_global(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _w(
        config.global_config_path(),
        "version: 1\ndefaults: {on_conflict: skip}\nskills: {enabled: false}\n",
    )
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "defaults: {on_conflict: backup}\nskills: {enabled: true}\n")

    loaded = config.load(repo)
    # per-repo wins for overlapping keys
    assert loaded.data["defaults"]["on_conflict"] == "backup"
    assert loaded.data["skills"]["enabled"] is True
    # both layers recorded
    assert any(layer.startswith("global:") for layer in loaded.layers)
    assert any(layer.startswith("repo:") for layer in loaded.layers)


def test_deep_merge_keeps_nonoverlapping_global_keys(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _w(config.global_config_path(), "version: 1\ndefaults: {skills_target: ~/g, on_conflict: backup}\n")
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "defaults: {ci_target: .github/workflows}\n")
    loaded = config.load(repo)
    # global key survives because the repo dict merges, not replaces
    assert loaded.data["defaults"]["skills_target"] == "~/g"
    assert loaded.data["defaults"]["ci_target"] == ".github/workflows"


def test_explicit_config_replaces_repo_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "skills: {enabled: false}\n")
    explicit = tmp_path / "other.yaml"
    _w(explicit, "skills: {enabled: true}\n")
    loaded = config.load(repo, explicit_config=explicit)
    assert loaded.data["skills"]["enabled"] is True


def test_committed_repo_rig_yaml_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = Path(__file__).resolve().parent.parent

    loaded = config.load(repo)

    assert loaded.primary_config_path == repo / "rig.yaml"
    assert any(layer.startswith("repo:") for layer in loaded.layers)


def test_validate_rejects_unknown_top_key():
    with pytest.raises(config.ConfigError, match="unknown top-level key"):
        config.validate({"version": 1, "bogus": 1})


def test_validate_accepts_project_dev_script_blocks():
    config.validate({
        "version": 1,
        "scripts": {"test": "uv run pytest"},
        "dev": {"server": {"script": "server", "ports": [5173]}},
    })


def test_load_round_trips_valid_scripts_and_dev_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(
        repo / "rig.yaml",
        "version: 1\n"
        "scripts:\n"
        "  test: uv run pytest\n"
        "  custom:\n"
        "    cmd: ./scripts/custom.sh\n"
        "dev:\n"
        "  server:\n"
        "    script: test\n"
        "    ports: [5173]\n",
    )

    loaded = config.load(repo)

    assert loaded.data["scripts"]["custom"]["cmd"] == "./scripts/custom.sh"
    assert loaded.data["dev"]["server"]["ports"] == [5173]


def test_load_rejects_unknown_scripts_key(tmp_path, monkeypatch):
    # scripts was a loose accept-and-preserve pass-through before the rich dev-server schema;
    # it is now strict like every other block — an unknown key is REJECTED, not silently
    # preserved.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(
        repo / "rig.yaml",
        "version: 1\n"
        "scripts:\n"
        "  test: uv run pytest\n"
        "  custom:\n"
        "    cmd: ./scripts/custom.sh\n"
        "    unexpected: [still, preserved]\n",
    )

    with pytest.raises(config.ConfigError, match=r"unknown scripts\.custom key: unexpected"):
        config.load(repo)


def test_load_rejects_unknown_dev_key(tmp_path, monkeypatch):
    # dev was a loose accept-and-preserve pass-through before the rich dev-server schema; it
    # is now strict like every other block — an unknown key is REJECTED, not silently
    # preserved.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(
        repo / "rig.yaml",
        "version: 1\n"
        "dev:\n"
        "  servver:\n"
        "    typo_owned_by_dev_helper: true\n",
    )

    with pytest.raises(config.ConfigError, match=r"unknown dev key: servver"):
        config.load(repo)


@pytest.mark.parametrize("key", ["scripts", "dev"])
def test_validate_rejects_non_mapping_project_dev_blocks(key):
    with pytest.raises(config.ConfigError) as ei:
        config.validate({"version": 1, key: ["test"]})
    assert ei.value.schema_path == key


# ── roadmap §5: every block REJECTS an unknown key (no silent no-op), with a schema path ──
@pytest.mark.parametrize(
    "doc, schema_path, msg",
    [
        ({"version": 1, "harness": {"aut_mode": True}}, "harness.aut_mode", "unknown harness key"),
        ({"version": 1, "skills": {"enabld": True}}, "skills.enabld", "unknown skills key"),
        ({"version": 1, "defaults": {"on_conflic": "skip"}}, "defaults.on_conflic", "unknown defaults key"),
        ({"version": 1, "git_hooks": {"dispatcher": {"enabld": True}}}, "git_hooks.dispatcher.enabld", "unknown git_hooks.dispatcher key"),
        ({"version": 1, "ci": {"enabld": True}}, "ci.enabld", "unknown ci key"),
        ({"version": 1, "mcp": {"enabld": True}}, "mcp.enabld", "unknown mcp key"),
        ({"version": 1, "agent_hooks": {"enabld": True}}, "agent_hooks.enabld", "unknown agent_hooks key"),
        ({"version": 1, "harness": {"hook_bridge": {"enabld": True}}}, "harness.hook_bridge.enabld", "unknown harness.hook_bridge key"),
        ({"version": 1, "skills": {"universal": {"al": True}}}, "skills.universal.al", "unknown skills.universal key"),
        ({"version": 1, "skills": {"by_type": {"enabl": []}}}, "skills.by_type.enabl", "unknown skills.by_type key"),
        ({"version": 1, "models": {"schedule": {"tim": "12:00"}}}, "models.schedule.tim", "unknown models.schedule key"),
        ({"version": 1, "github": {"ruleset": {"nam": "x"}}}, "github.ruleset.nam", "unknown github.ruleset key"),
        ({"version": 1, "tmux": {"continuum": {"save_intervall": 5}}}, "tmux.continuum.save_intervall", "unknown tmux.continuum key"),
        ({"version": 1, "tmux": {"boot": {"labl": "x"}}}, "tmux.boot.labl", "unknown tmux.boot key"),
        ({"version": 1, "permissions": {"tols": []}}, "permissions.tols", "unknown permissions key"),
        ({"version": 1, "gitignore": {"entres": []}}, "gitignore.entres", "unknown gitignore key"),
        ({"version": 1, "tg_ctl": {"labl": "x"}}, "tg_ctl.labl", "unknown tg_ctl key"),
        ({"version": 1, "agents_md": {"symlnk": True}}, "agents_md.symlnk", "unknown agents_md key"),
        ({"version": 1, "scripts": {"test": {"command": "pytest"}}}, "scripts.test.command", "unknown scripts.test key"),
        ({"version": 1, "dev": {"serve": {"script": "server"}}}, "dev.serve", "unknown dev key"),
        ({"version": 1, "dev": {"server": {"command": "vite"}}}, "dev.server.command", "unknown dev.server key"),
        ({"version": 1, "dev": {"e2e": {"command": "playwright"}}}, "dev.e2e.command", "unknown dev.e2e key"),
    ],
)
def test_validate_rejects_unknown_block_key_with_schema_path(doc, schema_path, msg):
    with pytest.raises(config.ConfigError) as ei:
        config.validate(doc)
    err = ei.value
    assert msg in err.what
    assert err.schema_path == schema_path
    assert err.fix  # an unknown-key error always offers the accepted keys


def test_validate_rejects_bad_value_with_schema_path():
    with pytest.raises(config.ConfigError) as ei:
        config.validate({"version": 1, "harness": {"auto_mode": "yes"}})
    assert ei.value.schema_path == "harness.auto_mode"
    with pytest.raises(config.ConfigError) as ei2:
        config.validate({"version": 1, "defaults": {"on_conflict": "nuke"}})
    assert ei2.value.schema_path == "defaults.on_conflict"


def test_render_config_error_is_three_part_with_pointer():
    with pytest.raises(config.ConfigError) as ei:
        config.validate({"version": 1, "harness": {"aut_mode": True}})
    block = config.render_config_error(ei.value, color=False)
    assert "error:" in block  # WHAT
    assert "why:" in block and "fix:" in block  # WHY + FIX
    # the schema path is shown both dotted and as a resolvable JSON pointer into the published file
    # (the typo points at the PARENT block node, which exists, not the non-existent typo leaf).
    assert "harness.aut_mode" in block
    assert "schema/rig.schema.json#/properties/harness" in block


def test_git_hooks_dispatcher_now_validated():
    # git_hooks previously had NO validator — a typo'd dispatcher knob was silently ignored.
    # It is now fail-closed, like every other block.
    config.validate({"version": 1, "git_hooks": {"dispatcher": {"enabled": False}}})  # valid
    with pytest.raises(config.ConfigError, match="git_hooks.dispatcher.enabled must be a bool"):
        config.validate({"version": 1, "git_hooks": {"dispatcher": {"enabled": "nope"}}})


def test_open_item_maps_still_accept_arbitrary_names():
    # the strictness must NOT break the open `items`/`fragments` maps (catalog item names).
    config.validate({"version": 1, "ci": {"items": {"secret-scan": {"tier": "block"}, "my-gate": {}}}})
    config.validate({
        "version": 1,
        "mcp": {
            "items": {
                "review": {
                    "command": "review",
                    "args": ["--serve"],
                    "env": {"NODE_ENV": "test"},
                }
            }
        },
    })
    config.validate({"version": 1, "agent_hooks": {"items": {"block-no-verify": {"on_error": "closed"}}}})
    config.validate({"version": 1, "git_hooks": {"dispatcher": {"fragments": {"secret-scan": {"enabled": True}}}}})


def test_fixed_typo_rejected_even_alongside_a_valid_open_map():
    # a real item map must NOT mask a fixed-knob typo in the SAME block — `items` stays valid,
    # `enabld` is rejected (the open map whitelists only its own key, not arbitrary fixed keys).
    with pytest.raises(config.ConfigError) as ei:
        config.validate({"version": 1, "ci": {"items": {"secret-scan": {"tier": "block"}}, "enabld": True}})
    assert ei.value.schema_path == "ci.enabld"


@pytest.mark.parametrize(
    "doc, schema_path",
    [
        ({"version": 1, "skills": {"enabled": "yes"}}, "skills.enabled"),
        ({"version": 1, "ci": {"enabled": "yes"}}, "ci.enabled"),
        ({"version": 1, "ci": {"target": 5}}, "ci.target"),
        ({"version": 1, "agent_hooks": {"all": "no"}}, "agent_hooks.all"),
        ({"version": 1, "mcp": {"enabled": "yes"}}, "mcp.enabled"),
        ({"version": 1, "permissions": {"enabled": "yes"}}, "permissions.enabled"),
    ],
)
def test_open_map_block_fixed_knobs_are_type_checked(doc, schema_path):
    # roadmap §5: the runtime and the JSON schema agree — a bad-typed fixed knob in ci/mcp/skills/
    # agent_hooks/permissions is rejected at runtime, not only by an editor.
    with pytest.raises(config.ConfigError) as ei:
        config.validate(doc)
    assert ei.value.schema_path == schema_path


def test_validate_accepts_scripts_strings_and_cmd_mappings():
    config.validate({
        "version": 1,
        "scripts": {
            "test": "python -m pytest -q",
            "lint": {"cmd": "ruff check ."},
        },
    })


def test_validate_rejects_mixed_type_script_keys_without_crashing():
    # A YAML entry can mix a string typo key with a numeric/boolean key (PyYAML preserves the
    # native type), e.g. `cmd: ...` alongside a bare `1: ...`. sorted() over a set of mixed str
    # and int keys raises TypeError instead of the intended ConfigError — this must surface as
    # a structured config error naming the malformed mapping, not an unhandled traceback out of
    # `rig apply`/`status`, and not a misleading "unknown key: 1".
    with pytest.raises(config.ConfigError, match=r"scripts\.bad keys must be strings \(got 1\)"):
        config.validate({
            "version": 1,
            "scripts": {"bad": {"cmd": "echo hi", "command": "typo", 1: "oops"}},
        })


def test_validate_rejects_mixed_type_dev_e2e_job_keys_without_crashing():
    with pytest.raises(config.ConfigError, match=r"dev\.e2e\.jobs\.bad keys must be strings \(got 1\)"):
        config.validate({
            "version": 1,
            "dev": {"e2e": {"jobs": {"bad": {"script": "e2e", "commnd": "typo", 1: "oops"}}}},
        })


def test_validate_rejects_non_string_script_key_with_no_other_typo():
    # Pins the ordering: the non-string-key check must run BEFORE the unknown-key check, not
    # rely on a coincidental typo key to avoid reaching the crashing sorted() call.
    with pytest.raises(config.ConfigError, match=r"scripts\.bad keys must be strings \(got 1\)"):
        config.validate({"version": 1, "scripts": {"bad": {"cmd": "echo hi", 1: "oops"}}})


def test_validate_rejects_bool_script_key_without_crashing():
    # YAML's `yes:`/`true:` parses as a bool key — a different TypeError path than int.
    with pytest.raises(config.ConfigError, match=r"scripts\.bad keys must be strings \(got True\)"):
        config.validate({"version": 1, "scripts": {"bad": {"cmd": "echo hi", True: "oops"}}})


def test_validate_rejects_non_string_script_name():
    with pytest.raises(config.ConfigError, match=r"scripts keys must be strings \(got 1\)"):
        config.validate({"version": 1, "scripts": {1: "echo hi"}})


def test_validate_rejects_non_string_dev_e2e_job_name():
    with pytest.raises(config.ConfigError, match=r"dev\.e2e\.jobs keys must be strings \(got 1\)"):
        config.validate({
            "version": 1,
            "dev": {"e2e": {"jobs": {1: {"script": "e2e"}}}},
        })


@pytest.mark.parametrize(
    "doc, schema_path, msg",
    [
        ({"version": 1, "scripts": []}, "scripts", "scripts must be a mapping"),
        ({"version": 1, "scripts": {"test": None}}, "scripts.test", "must be a string or a mapping"),
        ({"version": 1, "scripts": {"test": []}}, "scripts.test", "must be a string or a mapping"),
        ({"version": 1, "scripts": {"test": {}}}, "scripts.test.cmd", "requires a cmd string"),
        ({"version": 1, "scripts": {"test": {"cmd": 123}}}, "scripts.test.cmd", "requires a cmd string"),
    ],
)
def test_validate_rejects_bad_scripts(doc, schema_path, msg):
    with pytest.raises(config.ConfigError) as ei:
        config.validate(doc)
    assert msg in ei.value.what
    assert ei.value.schema_path == schema_path


def test_validate_accepts_dev_server_and_e2e_metadata():
    config.validate({
        "version": 1,
        "scripts": {
            "server": "npm run dev",
            "e2e": {"cmd": "npx playwright test"},
        },
        "dev": {
            "server": {
                "script": "server",
                "url": "http://localhost:3000",
                "ready_url": "http://localhost:3000/health",
                "ports": [3000, 5173],
                "process_matchers": ["vite", "npm run dev"],
                "logs_root": ".dev/logs/server",
            },
            "e2e": {
                "script": "e2e",
                "requires_server": True,
                "artifacts_root": "test-results",
                "logs_root": ".dev/logs/e2e",
                "jobs": {
                    "smoke": {
                        "script": "e2e-smoke",
                        "requires_server": True,
                        "artifacts_root": "test-results/smoke",
                        "logs_root": ".dev/logs/e2e-smoke",
                    },
                },
            },
        },
    })


@pytest.mark.parametrize(
    "doc, schema_path, msg",
    [
        ({"version": 1, "dev": []}, "dev", "dev must be a mapping"),
        ({"version": 1, "dev": {"server": []}}, "dev.server", "dev.server must be a mapping"),
        ({"version": 1, "dev": {"e2e": []}}, "dev.e2e", "dev.e2e must be a mapping"),
        ({"version": 1, "dev": {"server": {"script": 1}}}, "dev.server.script", "dev.server.script must be a string"),
        ({"version": 1, "dev": {"server": {"url": 1}}}, "dev.server.url", "dev.server.url must be a string"),
        ({"version": 1, "dev": {"server": {"ready_url": 1}}}, "dev.server.ready_url", "dev.server.ready_url must be a string"),
        ({"version": 1, "dev": {"server": {"ports": "3000"}}}, "dev.server.ports", "dev.server.ports must be a list of ints"),
        ({"version": 1, "dev": {"server": {"ports": [True]}}}, "dev.server.ports", "dev.server.ports must be a list of ints"),
        ({"version": 1, "dev": {"server": {"ports": [0]}}}, "dev.server.ports", "dev.server.ports entries must be ints from 1 to 65535"),
        ({"version": 1, "dev": {"server": {"ports": [65536]}}}, "dev.server.ports", "dev.server.ports entries must be ints from 1 to 65535"),
        ({"version": 1, "dev": {"server": {"process_matchers": "vite"}}}, "dev.server.process_matchers", "dev.server.process_matchers must be a list of strings"),
        ({"version": 1, "dev": {"server": {"process_matchers": ["vite", 1]}}}, "dev.server.process_matchers", "dev.server.process_matchers must be a list of strings"),
        ({"version": 1, "dev": {"server": {"logs_root": 1}}}, "dev.server.logs_root", "dev.server.logs_root must be a string"),
        ({"version": 1, "dev": {"e2e": {"script": 1}}}, "dev.e2e.script", "dev.e2e.script must be a string"),
        ({"version": 1, "dev": {"e2e": {"requires_server": "yes"}}}, "dev.e2e.requires_server", "dev.e2e.requires_server must be a bool"),
        ({"version": 1, "dev": {"e2e": {"artifacts_root": 1}}}, "dev.e2e.artifacts_root", "dev.e2e.artifacts_root must be a string"),
        ({"version": 1, "dev": {"e2e": {"logs_root": 1}}}, "dev.e2e.logs_root", "dev.e2e.logs_root must be a string"),
        ({"version": 1, "dev": {"e2e": {"jobs": []}}}, "dev.e2e.jobs", "dev.e2e.jobs must be a mapping"),
        ({"version": 1, "dev": {"e2e": {"jobs": {"smoke": []}}}}, "dev.e2e.jobs.smoke", "dev.e2e.jobs.smoke must be a mapping"),
        ({"version": 1, "dev": {"e2e": {"jobs": {"smoke": {"script": 1}}}}}, "dev.e2e.jobs.smoke.script", "dev.e2e.jobs.smoke.script must be a string"),
        ({"version": 1, "dev": {"e2e": {"jobs": {"smoke": {"requires_server": "yes"}}}}}, "dev.e2e.jobs.smoke.requires_server", "dev.e2e.jobs.smoke.requires_server must be a bool"),
        ({"version": 1, "dev": {"e2e": {"jobs": {"smoke": {"artifacts_root": 1}}}}}, "dev.e2e.jobs.smoke.artifacts_root", "dev.e2e.jobs.smoke.artifacts_root must be a string"),
        ({"version": 1, "dev": {"e2e": {"jobs": {"smoke": {"logs_root": 1}}}}}, "dev.e2e.jobs.smoke.logs_root", "dev.e2e.jobs.smoke.logs_root must be a string"),
    ],
)
def test_validate_rejects_bad_dev_metadata(doc, schema_path, msg):
    with pytest.raises(config.ConfigError) as ei:
        config.validate(doc)
    assert msg in ei.value.what
    assert ei.value.schema_path == schema_path


def test_agent_hooks_target_kind_is_accepted_as_ignored_legacy_key():
    config.validate({"version": 1, "agent_hooks": {"target_kind": "claude-code"}})
    config.validate({"version": 1, "agent_hooks": {"target_kind": "generic"}})


def test_agent_hooks_target_kind_rejects_bad_legacy_value():
    with pytest.raises(config.ConfigError) as ei:
        config.validate({"version": 1, "agent_hooks": {"target_kind": "bogus"}})
    assert ei.value.schema_path == "agent_hooks.target_kind"


def test_mcp_non_dict_block_rejected_cleanly():
    # a non-dict mcp must fail with a clean "must be a mapping" (the category check catches it
    # first in validate()), never a nonsensical char-set error from set() over a string.
    with pytest.raises(config.ConfigError, match="mcp.* must be a mapping"):
        config.validate({"version": 1, "mcp": "nope"})
    # and the dedicated validator's own guard is clean when called directly (belt-and-suspenders)
    with pytest.raises(config.ConfigError, match="mcp must be a mapping"):
        config._validate_mcp("nope")


@pytest.mark.parametrize(
    "doc, schema_path",
    [
        ({"version": 1, "mcp": {"items": {"fake-mcp": {"argz": []}}}}, "mcp.items.fake-mcp.argz"),
        ({"version": 1, "mcp": {"items": {"fake-mcp": "nope"}}}, "mcp.items.fake-mcp"),
        ({"version": 1, "mcp": {"items": {"fake-mcp": {"enabled": "yes"}}}}, "mcp.items.fake-mcp.enabled"),
        ({"version": 1, "mcp": {"items": {"fake-mcp": {"server": 3}}}}, "mcp.items.fake-mcp.server"),
        ({"version": 1, "mcp": {"items": {"fake-mcp": {"command": ["node"]}}}}, "mcp.items.fake-mcp.command"),
        ({"version": 1, "mcp": {"items": {"fake-mcp": {"args": "--serve"}}}}, "mcp.items.fake-mcp.args"),
        ({"version": 1, "mcp": {"items": {"fake-mcp": {"args": ["ok", 1]}}}}, "mcp.items.fake-mcp.args"),
        ({"version": 1, "mcp": {"items": {"fake-mcp": {"env": []}}}}, "mcp.items.fake-mcp.env"),
        ({"version": 1, "mcp": {"items": {"fake-mcp": {"env": {"PORT": 3000}}}}}, "mcp.items.fake-mcp.env.PORT"),
    ],
)
def test_mcp_item_specs_are_validated_fail_closed(doc, schema_path):
    with pytest.raises(config.ConfigError) as ei:
        config.validate(doc)
    assert ei.value.schema_path == schema_path


def test_validate_ignores_legacy_scope():
    # `scope` was removed (location-based cascade); a legacy key is tolerated, not rejected.
    config.validate({"version": 1, "scope": "everywhere"})


def test_load_drops_legacy_scope(tmp_path, monkeypatch):
    # a committed rig.yaml that still carries the removed `scope` key loads fine, and the key
    # is dropped from the result so it never lingers/re-serializes or reads as a live setting.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "version: 1\nscope: both\nskills: {enabled: false}\n")
    loaded = config.load(repo)
    assert "scope" not in loaded.data


def test_key_sources_track_layer_provenance(tmp_path, monkeypatch):
    # a key set only in the GLOBAL config maps to the global path; a key the repo sets maps to
    # the repo path. This is what source_for_key() uses to name the right file in an error.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _w(config.global_config_path(), "version: 1\nmcp: {enabled: true}\nskills: {enabled: true}\n")
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "version: 1\nci: {enabled: false}\nskills: {enabled: false}\n")
    loaded = config.load(repo)
    assert loaded.key_sources["mcp"] == config.global_config_path()  # global-only key
    assert loaded.key_sources["ci"] == repo / "rig.yaml"  # repo-only key
    assert loaded.key_sources["skills"] == repo / "rig.yaml"  # repo OVERRIDES global
    assert loaded.source_for_key("mcp.items.x") == config.global_config_path()


def test_load_strips_scope_from_key_sources(tmp_path, monkeypatch):
    # the removed legacy `scope` key is dropped from provenance too (not just from data), so a
    # stray `scope:` never lingers as a tracked source. source_for_key falls back when untracked.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "version: 1\nscope: both\nskills: {enabled: false}\n")
    loaded = config.load(repo)
    assert "scope" not in loaded.key_sources
    assert loaded.source_for_key("scope") == loaded.primary_config_path  # fallback, not KeyError


def test_validate_rejects_bad_on_conflict():
    with pytest.raises(config.ConfigError, match="on_conflict"):
        config.validate({"version": 1, "defaults": {"on_conflict": "nuke"}})


def test_validate_rejects_bad_ci_tier():
    with pytest.raises(config.ConfigError, match="tier"):
        config.validate({"version": 1, "ci": {"items": {"x": {"tier": "loud"}}}})


def test_validate_rejects_unsupported_version():
    with pytest.raises(config.ConfigError, match="version"):
        config.validate({"version": 2})


def test_missing_explicit_config_raises(tmp_path):
    with pytest.raises(config.ConfigError, match="not found"):
        config.load(tmp_path, explicit_config=tmp_path / "nope.yaml")


def test_invalid_yaml_wrapped_as_configerror(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "version: 1\n  bad: : indent\n:::\n")
    with pytest.raises(config.ConfigError, match="invalid YAML"):
        config.load(repo)


# ── harness block ────────────────────────────────────────────────────────────────
def test_validate_accepts_harness_block():
    # a well-formed harness block passes (claude-code is the supported kind)
    config.validate({"version": 1, "harness": {"kind": "claude-code", "auto_mode": True}})


def test_validate_rejects_unknown_harness_kind():
    with pytest.raises(config.ConfigError, match="harness.kind"):
        config.validate({"version": 1, "harness": {"kind": "bogus-harness"}})


def test_validate_rejects_non_string_harness_kind():
    with pytest.raises(config.ConfigError) as ei:
        config.validate({"version": 1, "harness": {"kind": ["codex"]}})
    assert ei.value.schema_path == "harness.kind"


def test_validate_accepts_all_supported_harness_kinds():
    # rig-cli#9: every harness rig knows a skill/instruction discovery convention for is now
    # ACCEPTED in harness.kind (skills-dir harnesses claude-code/opencode + instruction-file
    # harnesses codex/pi/commandcode). Previously opencode (and the rest) were rejected.
    # gemini is DEPRECATED (removed everywhere) — see the dedicated rejection test below.
    for kind in ("claude-code", "opencode", "codex", "pi", "commandcode"):
        config.validate({"version": 1, "harness": {"kind": kind}})


def test_validate_rejects_deprecated_gemini_harness_kind():
    # gemini is deprecated and no longer a supported harness; a config that still names it
    # fails closed with a clear "no longer supported (deprecated)" message, not the generic
    # typo error — so a user migrating off gemini knows WHY, not just that it's unknown.
    with pytest.raises(config.ConfigError, match="no longer supported") as ei:
        config.validate({"version": 1, "harness": {"kind": "gemini"}})
    assert ei.value.schema_path == "harness.kind"
    assert "gemini" in str(ei.value)


def test_validate_rejects_deprecated_gemini_in_additional_kinds():
    with pytest.raises(config.ConfigError, match="no longer supported"):
        config.validate({"version": 1, "harness": {"kind": "claude-code", "kinds": ["gemini"]}})


def test_validate_accepts_additional_harness_kinds():
    config.validate({"version": 1, "harness": {"kind": "claude-code", "kinds": ["codex", "opencode"]}})


def test_validate_rejects_typo_harness_kind_with_supported_list():
    # a typo still fails closed — and the message names the supported kinds so the fix is obvious.
    with pytest.raises(config.ConfigError, match="harness.kind must be one of"):
        config.validate({"version": 1, "harness": {"kind": "claudecode"}})


def test_validate_rejects_non_bool_auto_mode():
    with pytest.raises(config.ConfigError, match="auto_mode"):
        config.validate({"version": 1, "harness": {"auto_mode": "yes"}})


@pytest.mark.parametrize("bad", ["false", "true", 0, 1])
def test_validate_rejects_non_bool_self_merge(bad):
    # self_merge gates a security-sensitive global carve-out; a non-bool like the string
    # "false" would coerce truthy via bool(...) and ENABLE the carve-out the user meant to
    # disable. Fail closed, mirroring auto_mode.
    with pytest.raises(config.ConfigError, match="self_merge"):
        config.validate({"version": 1, "harness": {"self_merge": bad}})


def test_validate_accepts_bool_self_merge():
    config.validate({"version": 1, "harness": {"self_merge": True}})
    config.validate({"version": 1, "harness": {"self_merge": False}})


def test_validate_accepts_hook_bridge_block():
    config.validate({"version": 1, "harness": {
        "kind": "claude-code", "hook_bridge": {"enabled": True, "python": "python3.12"}}})
    config.validate({"version": 1, "harness": {"hook_bridge": {"enabled": False}}})


def test_validate_rejects_non_bool_hook_bridge_enabled():
    with pytest.raises(config.ConfigError, match="hook_bridge.enabled"):
        config.validate({"version": 1, "harness": {"hook_bridge": {"enabled": "yes"}}})


def test_validate_rejects_non_string_hook_bridge_python():
    with pytest.raises(config.ConfigError, match="hook_bridge.python"):
        config.validate({"version": 1, "harness": {"hook_bridge": {"python": 3}}})


def test_validate_rejects_non_mapping_hook_bridge():
    with pytest.raises(config.ConfigError, match="hook_bridge must be a mapping"):
        config.validate({"version": 1, "harness": {"hook_bridge": "on"}})


# ── skill harness-link knobs ───────────────────────────────────────────────────────
def test_validate_accepts_skill_harness_link_knobs():
    config.validate(
        {"version": 1, "skills": {"harness_link": True, "harness_skill_dir": "~/.claude/skills"}}
    )
    config.validate({"version": 1, "skills": {"harness_link": False}})


def test_validate_rejects_non_bool_harness_link():
    with pytest.raises(config.ConfigError, match="harness_link"):
        config.validate({"version": 1, "skills": {"harness_link": "yes"}})


def test_validate_rejects_non_string_harness_skill_dir():
    with pytest.raises(config.ConfigError, match="harness_skill_dir"):
        config.validate({"version": 1, "skills": {"harness_skill_dir": 42}})


def test_validate_spotlight_accepts_valid():
    config.validate({"version": 1, "spotlight": {"enabled": True, "roots": ["~/work"],
                                                 "deny": ["node_modules"], "extra": ["foo"],
                                                 "label": "x", "max_depth": 4}})
    config.validate({"version": 1, "spotlight": {}})  # empty block is a no-op


def test_validate_spotlight_rejects_unknown_key():
    with pytest.raises(config.ConfigError, match="unknown spotlight key"):
        config.validate({"version": 1, "spotlight": {"bogus": 1}})


def test_validate_spotlight_rejects_non_bool_enabled():
    with pytest.raises(config.ConfigError, match="spotlight.enabled must be a bool"):
        config.validate({"version": 1, "spotlight": {"enabled": "yes"}})


def test_validate_spotlight_rejects_non_string_list_roots():
    with pytest.raises(config.ConfigError, match="spotlight.roots must be a list of strings"):
        config.validate({"version": 1, "spotlight": {"roots": [1, 2]}})
    with pytest.raises(config.ConfigError, match="spotlight.deny must be a list of strings"):
        config.validate({"version": 1, "spotlight": {"deny": "node_modules"}})


def test_validate_spotlight_rejects_bad_max_depth():
    with pytest.raises(config.ConfigError, match="spotlight.max_depth must be a positive int"):
        config.validate({"version": 1, "spotlight": {"max_depth": 0}})
    with pytest.raises(config.ConfigError, match="spotlight.max_depth must be a positive int"):
        config.validate({"version": 1, "spotlight": {"max_depth": True}})  # bool guard


def test_validate_spotlight_rejects_non_string_label():
    with pytest.raises(config.ConfigError, match="spotlight.label must be a string"):
        config.validate({"version": 1, "spotlight": {"label": 5}})


# ── stack preset validation (config._validate_stack via validate) ───────────────────────
@pytest.mark.parametrize(
    "value",
    ["mobile/swift/swiftui", "frontend/ts/react", "backend/python", "system/rust", "backend/zig"],
)
def test_validate_accepts_well_formed_stack(value):
    config.validate({"version": 1, "stack": value})


@pytest.mark.parametrize(
    "value",
    ["", "mobile", "web/ts/react", "a/b/c/d", "mobile//swiftui", "backend/python/"],
)
def test_validate_rejects_malformed_stack(value):
    with pytest.raises(config.ConfigError) as exc:
        config.validate({"version": 1, "stack": value})
    assert exc.value.schema_path == "stack"


def test_validate_rejects_non_string_stack():
    with pytest.raises(config.ConfigError):
        config.validate({"version": 1, "stack": ["mobile", "swift"]})


def test_validate_allows_missing_stack_soft_require():
    # a MISSING stack is not a hard error (soft-require migration phase)
    config.validate({"version": 1})


def test_loaded_config_exposes_stack(tmp_path):
    lc = config.LoadedConfig(data={"version": 1, "stack": "  backend/go  "}, repo_root=tmp_path)
    assert lc.stack == "backend/go"
    lc2 = config.LoadedConfig(data={"version": 1}, repo_root=tmp_path)
    assert lc2.stack is None


def test_stack_requirement_warning(tmp_path):
    lc = config.LoadedConfig(data={"version": 1, "stack": "backend/go"}, repo_root=tmp_path)
    assert config.stack_requirement_warning(lc) is None
    lc2 = config.LoadedConfig(data={"version": 1}, repo_root=tmp_path)
    warn = config.stack_requirement_warning(lc2)
    assert warn and "stack: not set" in warn


@pytest.mark.parametrize(
    "items",
    [
        {"by-stack/mobile/swift/x": "yes"},  # spec not a mapping
        {"by-stack/mobile/swift/x": {"enabled": "false"}},  # enabled not a bool
    ],
)
def test_validate_rejects_malformed_by_stack_items(items):
    with pytest.raises(config.ConfigError) as exc:
        config.validate({"version": 1, "skills": {"by_stack": {"items": items}}})
    assert exc.value.schema_path.startswith("skills.by_stack.items")


def test_validate_accepts_well_formed_by_stack_items():
    config.validate(
        {"version": 1, "skills": {"by_stack": {"items": {"by-stack/mobile/swift/x": {"enabled": False}}}}}
    )
