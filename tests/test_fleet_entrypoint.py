from __future__ import annotations

from riglib import fleet_entrypoint


def test_routes_fleet_config_before_generic_fleet(monkeypatch):
    import riglib.fleet_config as config_module

    seen = {}

    def fake_config(argv):
        seen["config"] = list(argv)
        return 17

    monkeypatch.setattr(config_module, "main", fake_config)
    assert fleet_entrypoint.main(["fleet", "config", "set", "ci.enabled", "true"]) == 17
    assert seen["config"] == ["set", "ci.enabled", "true"]


def test_routes_other_fleet_commands_to_fleet(monkeypatch):
    import riglib.fleet as fleet_module

    seen = {}

    def fake_fleet(argv):
        seen["fleet"] = list(argv)
        return 18

    monkeypatch.setattr(fleet_module, "main", fake_fleet)
    assert fleet_entrypoint.main(["fleet", "status", "--json"]) == 18
    assert seen["fleet"] == ["status", "--json"]


def test_non_fleet_still_uses_existing_cli(monkeypatch):
    import riglib.cli as cli_module

    monkeypatch.setattr(cli_module, "main", lambda argv: 19 if list(argv) == ["status"] else 99)
    assert fleet_entrypoint.main(["status"]) == 19


def test_routes_fleet_config_with_leading_registry_flag(monkeypatch):
    # rig fleet's own convention allows --registry before the subcommand
    # (see fleet.build_parser: --registry is a top-level option ahead of the
    # subparsers). "config" must resolve the same way, not just when it is
    # literally the second token — caught in review of PR #269.
    import riglib.fleet_config as config_module

    seen = {}

    def fake_config(argv):
        seen["config"] = list(argv)
        return 17

    monkeypatch.setattr(config_module, "main", fake_config)
    assert (
        fleet_entrypoint.main(["fleet", "--registry", "/tmp/reg.json", "config", "set", "ci.enabled", "true"]) == 17
    )
    assert seen["config"] == ["--registry", "/tmp/reg.json", "set", "ci.enabled", "true"]


def test_routes_fleet_config_with_leading_registry_equals_flag(monkeypatch):
    import riglib.fleet_config as config_module

    seen = {}

    def fake_config(argv):
        seen["config"] = list(argv)
        return 17

    monkeypatch.setattr(config_module, "main", fake_config)
    assert fleet_entrypoint.main(["fleet", "--registry=/tmp/reg.json", "config", "set", "ci.enabled", "true"]) == 17
    assert seen["config"] == ["--registry=/tmp/reg.json", "set", "ci.enabled", "true"]


def test_routes_fleet_config_with_trailing_registry_flag_unchanged(monkeypatch):
    # The config-level ordering (--registry AFTER "config", fleet_config.py's
    # own parser convention) must keep working exactly as before — this
    # routing change only widens the BEFORE case, it must not disturb this one.
    import riglib.fleet_config as config_module

    seen = {}

    def fake_config(argv):
        seen["config"] = list(argv)
        return 17

    monkeypatch.setattr(config_module, "main", fake_config)
    assert fleet_entrypoint.main(["fleet", "config", "--registry", "/tmp/reg.json", "set", "ci.enabled", "true"]) == 17
    assert seen["config"] == ["--registry", "/tmp/reg.json", "set", "ci.enabled", "true"]


def test_routes_non_config_fleet_command_with_leading_registry_flag_unchanged(monkeypatch):
    # A leading --registry ahead of a non-config subcommand must still route
    # to fleet_main with the flag intact — the index scan must not swallow it.
    import riglib.fleet as fleet_module

    seen = {}

    def fake_fleet(argv):
        seen["fleet"] = list(argv)
        return 18

    monkeypatch.setattr(fleet_module, "main", fake_fleet)
    assert fleet_entrypoint.main(["fleet", "--registry", "/tmp/reg.json", "status", "--json"]) == 18
    assert seen["fleet"] == ["--registry", "/tmp/reg.json", "status", "--json"]


def test_registry_value_that_is_literally_config_is_not_mistaken_for_the_subcommand(monkeypatch):
    # ["fleet", "--registry", "config"]: "config" here is --registry's VALUE,
    # not a subcommand — there is no subcommand token at all (index scan runs
    # past the end). Must fall through to fleet_main (which will itself then
    # error on a missing required subcommand), never to fleet_config.
    import riglib.fleet as fleet_module
    import riglib.fleet_config as config_module

    seen = {}

    def fake_config(argv):
        seen["config"] = list(argv)
        return 0

    def fake_fleet(argv):
        seen["fleet"] = list(argv)
        return 20

    monkeypatch.setattr(config_module, "main", fake_config)
    monkeypatch.setattr(fleet_module, "main", fake_fleet)

    assert fleet_entrypoint.main(["fleet", "--registry", "config"]) == 20
    assert "config" not in seen
    assert seen["fleet"] == ["--registry", "config"]
