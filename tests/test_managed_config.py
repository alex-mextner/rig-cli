from riglib.managed_config import managed_header, with_managed_header


def test_comment_capable_config_gets_ownership_and_direct_edit_contract():
    header = managed_header("oxlint.config.ts")
    assert header.startswith("// Managed by Rig.")
    assert "Source of truth" in header
    assert "Do not edit directly" in header
    assert "Temporary diagnostic edits are allowed locally" in header


def test_hash_comment_config_gets_valid_header():
    header = managed_header("ruff.toml", source="agent-tools/linters/ruff.toml + rig.yaml selection")
    assert header.startswith("# Managed by Rig.")
    assert "agent-tools/linters/ruff.toml" in header


def test_strict_json_is_not_corrupted_with_comments():
    assert managed_header("biome.json") == ""
    assert with_managed_header("biome.json", '{"x": 1}\n') == '{"x": 1}\n'


def test_header_is_idempotent():
    content = "export default {};\n"
    once = with_managed_header("config.ts", content)
    twice = with_managed_header("config.ts", once)
    assert twice == once
    assert once.count("Managed by Rig") == 1
