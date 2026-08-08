"""`~/.config/railctl/config.toml`: three keys, and the CLI-flag/env/file/default
precedence primitive every global option is resolved through.

A missing file is not an error - most runs have none. A file that IS there and
IS wrong always names the file, the line and the key: "invalid config" alone
is a support ticket six months later, not something anyone can fix on sight.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from railctl.cli.config import (
    CONFIG_KEYS,
    Config,
    capabilities_path,
    config_dir,
    config_path,
    load_config,
    pick,
)


def test_config_dir_honours_xdg_config_home(tmp_path: Path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    assert config_dir(env) == tmp_path / "railctl"


def test_config_dir_falls_back_to_dot_config_when_xdg_unset(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert config_dir({}) == tmp_path / ".config" / "railctl"


def test_config_path_and_capabilities_path_are_under_config_dir(tmp_path: Path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    assert config_path(env) == tmp_path / "railctl" / "config.toml"
    assert capabilities_path(env) == tmp_path / "railctl" / "capabilities.json"


def test_config_keys_are_exactly_target_address_verbose():
    assert CONFIG_KEYS == ("target", "address", "verbose")


def test_default_config_has_auto_target_and_no_address():
    config = Config()
    assert config.target == "auto"
    assert config.address is None
    assert config.verbose == 0


def test_load_config_on_a_missing_file_returns_defaults_and_creates_nothing(tmp_path: Path):
    path = tmp_path / "config.toml"
    assert load_config(path) == Config()
    assert not path.exists()


def test_load_config_reads_all_three_keys(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('target = "serial:auto"\naddress = 3\nverbose = 2\n', encoding="utf-8")
    assert load_config(path) == Config(target="serial:auto", address=3, verbose=2)


def test_load_config_rejects_an_unknown_key_naming_file_line_and_key(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('target = "auto"\nbogus = 1\n', encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_config(path)
    message = str(caught.value)
    assert str(path) in message
    assert "line 2" in message
    assert "bogus" in message


def test_load_config_rejects_bad_toml_syntax_naming_file_and_line(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('target = "auto"\naddress = \n', encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_config(path)
    message = str(caught.value)
    assert str(path) in message
    assert "line 2" in message


def test_load_config_rejects_an_out_of_range_address_naming_the_bound(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("address = 99999\n", encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_config(path)
    message = str(caught.value)
    assert str(path) in message
    assert "line 1" in message
    assert "address" in message
    assert "9999" in message


def test_load_config_rejects_a_non_string_target_naming_the_type(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("target = 3\n", encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_config(path)
    message = str(caught.value)
    assert "line 1" in message
    assert "target" in message


def test_load_config_rejects_a_non_integer_address_naming_the_type(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('address = "3"\n', encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_config(path)
    message = str(caught.value)
    assert "line 1" in message
    assert "address" in message
    assert "integer" in message


def test_load_config_falls_back_to_line_1_when_the_key_is_written_in_quoted_form(tmp_path: Path):
    # tomllib reads `"bogus" = 1` as the key `bogus`, which no `^bogus\s*=` scan can
    # find in the text. Line 1 is the honest fallback: the file and the key are still
    # named, and no line number is invented for a line that was never located.
    path = tmp_path / "config.toml"
    path.write_text('target = "auto"\n"bogus" = 1\n', encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_config(path)
    message = str(caught.value)
    assert str(path) in message
    assert "bogus" in message
    assert "line 1" in message


def test_load_config_rejects_a_negative_verbose_count(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("verbose = -1\n", encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_config(path)
    assert "verbose" in str(caught.value)


def test_pick_prefers_the_flag_over_everything():
    result = pick("cli-value", "env-value", "config-value", "default", name="target", cast=str)
    assert result == "cli-value"


def test_pick_prefers_env_over_config_and_default():
    result = pick(None, "7", "3", 0, name="address", cast=int)
    assert result == 7


def test_pick_prefers_config_over_the_default():
    result = pick(None, None, 3, 0, name="address", cast=int)
    assert result == 3


def test_pick_falls_back_to_the_built_in_default():
    result = pick(None, None, None, 0, name="address", cast=int)
    assert result == 0


def test_pick_applies_cast_to_the_environment_value_only():
    # config_value=3 (an int, already the right type) must be returned as-is;
    # cast is for parsing the environment STRING, never for re-typing a value
    # that came from a source that is already typed.
    result = pick(None, "9", 3, 0, name="address", cast=int)
    assert result == 9
    assert isinstance(result, int)


def test_pick_wraps_a_cast_failure_in_value_error():
    with pytest.raises(ValueError) as caught:
        pick(None, "not-a-number", None, 0, name="address", cast=int)
    assert "RAILCTL_ADDRESS" in str(caught.value)
