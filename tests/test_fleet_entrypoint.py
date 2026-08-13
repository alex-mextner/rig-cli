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
