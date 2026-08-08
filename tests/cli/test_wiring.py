"""Global-option resolution, logging levels, confirmation, and the two
commands every railctl session starts with.

Split by the module under test with a comment banner per section, because
this is the one test file this task's contract allows for `deps.py`,
`commands/basics.py` and `main.py` together.
"""

from __future__ import annotations

import importlib
import io
import json
import logging
import os
import runpy
import sys

import pytest
import typer
from typer.testing import CliRunner

import railctl.cli.main as cli_main
from railctl.cli._errors import OutputContext, run
from railctl.cli.commands import basics
from railctl.cli.commands.basics import STATUS_SCHEMA, VERSION_SCHEMA, build_status, build_version
from railctl.cli.config import Config
from railctl.cli.deps import (
    Settings,
    UsageProblem,
    build_settings,
    configure_logging,
    confirm,
    link_info,
    merge_settings,
    open_station,
    require_address,
    station_info,
)
from railctl.cli.result import CommandResult
from railctl.errors import AbortedError, ConfirmationRequiredError, TransportError
from railctl.station import TIMING, Station
from railctl.xbus.replies import StationStatus, StationVersion


def _config(**overrides) -> Config:
    return Config(**overrides)


def _settings(**overrides) -> Settings:
    base = {
        "target": "auto",
        "address": None,
        "fmt": "human",
        "verbose": 0,
        "color": "auto",
        "assume_yes": False,
        "interactive": False,
    }
    base.update(overrides)
    return Settings(**base)


# -- Settings / build_settings precedence -----------------------------------


def test_target_precedence_cli_over_env_over_config_over_default():
    common = {
        "address": None,
        "fmt": None,
        "json_flag": False,
        "verbose": None,
        "color": "auto",
        "yes": False,
        "non_interactive": True,
        "stdin": io.StringIO(),
    }
    all_four = dict(
        target="cli-target",
        env={"RAILCTL_TARGET": "env-target"},
        config=_config(target="config-target"),
        **common,
    )
    assert build_settings(**all_four).target == "cli-target"
    assert build_settings(**{**all_four, "target": None}).target == "env-target"
    assert build_settings(**{**all_four, "target": None, "env": {}}).target == "config-target"
    assert (
        build_settings(**{**all_four, "target": None, "env": {}, "config": _config()}).target
        == "auto"
    )


def test_address_precedence_cli_over_env_over_config_over_default():
    common = {
        "target": None,
        "fmt": None,
        "json_flag": False,
        "verbose": None,
        "color": "auto",
        "yes": False,
        "non_interactive": True,
        "stdin": io.StringIO(),
    }
    all_four = dict(
        address=1,
        env={"RAILCTL_ADDRESS": "2"},
        config=_config(address=3),
        **common,
    )
    assert build_settings(**all_four).address == 1
    assert build_settings(**{**all_four, "address": None}).address == 2
    assert build_settings(**{**all_four, "address": None, "env": {}}).address == 3
    assert (
        build_settings(**{**all_four, "address": None, "env": {}, "config": _config()}).address
        is None
    )


def test_verbose_precedence_cli_over_env_over_config_over_default():
    common = {
        "target": None,
        "address": None,
        "fmt": None,
        "json_flag": False,
        "color": "auto",
        "yes": False,
        "non_interactive": True,
        "stdin": io.StringIO(),
    }
    # One distinct value per source, the way the other three keys already do it. With env
    # and config both at 1 the second and third assertions read the same number, so neither
    # can tell which source produced it and dropping the config level went unnoticed.
    all_four = dict(
        verbose=3,
        env={"RAILCTL_VERBOSE": "2"},
        config=_config(verbose=1),
        **common,
    )
    assert build_settings(**all_four).verbose == 3
    assert build_settings(**{**all_four, "verbose": None}).verbose == 2
    assert build_settings(**{**all_four, "verbose": None, "env": {}}).verbose == 1
    assert (
        build_settings(
            **{**all_four, "verbose": None, "env": {}, "config": _config(verbose=0)}
        ).verbose
        == 0
    )


def test_format_precedence_cli_over_env_over_default():
    # `format` has no config-file key at all (design spec L3): only three
    # keys ever live in config.toml, and format is not one of them.
    common = {
        "target": None,
        "address": None,
        "verbose": None,
        "color": "auto",
        "yes": False,
        "non_interactive": True,
        "stdin": io.StringIO(),
        "config": _config(),
    }
    # Format is `Literal["human", "json", "ndjson"]`, not an enum class - there
    # is nothing to call it with, so the comparison is against the plain string.
    assert (
        build_settings(fmt="json", json_flag=False, env={"RAILCTL_FORMAT": "ndjson"}, **common).fmt
        == "json"
    )
    assert (
        build_settings(fmt=None, json_flag=False, env={"RAILCTL_FORMAT": "ndjson"}, **common).fmt
        == "ndjson"
    )
    assert build_settings(fmt=None, json_flag=False, env={}, **common).fmt == "human"


def test_precedence_is_decided_independently_per_key():
    # A config file supplying `address` and an environment variable supplying
    # `target` must BOTH take effect in the same run - proof that build_settings
    # walks each key's own four levels rather than picking one winning SOURCE
    # for the whole call.
    settings = build_settings(
        target=None,
        address=None,
        fmt=None,
        json_flag=False,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=True,
        env={"RAILCTL_TARGET": "z21:192.168.0.111:21105"},
        config=_config(address=7),
        stdin=io.StringIO(),
    )
    assert settings.target == "z21:192.168.0.111:21105"
    assert settings.address == 7


def test_json_flag_is_an_alias_for_format_json():
    settings = build_settings(
        target=None,
        address=None,
        fmt=None,
        json_flag=True,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=True,
        env={},
        config=_config(),
        stdin=io.StringIO(),
    )
    assert settings.fmt == "json"


def test_json_flag_conflicts_with_an_explicit_non_json_format():
    with pytest.raises(ValueError) as caught:
        build_settings(
            target=None,
            address=None,
            fmt="ndjson",
            json_flag=True,
            verbose=None,
            color="auto",
            yes=False,
            non_interactive=True,
            env={},
            config=_config(),
            stdin=io.StringIO(),
        )
    assert "--json" in str(caught.value)
    assert "ndjson" in str(caught.value)


def test_address_outside_bounds_is_rejected_before_any_link_is_opened(monkeypatch):
    def fail_if_called(*a, **k):
        raise AssertionError("Station.open must not run when --address is out of range")

    monkeypatch.setattr(Station, "open", staticmethod(fail_if_called))
    with pytest.raises(ValueError) as caught:
        build_settings(
            target=None,
            address=20000,
            fmt=None,
            json_flag=False,
            verbose=None,
            color="auto",
            yes=False,
            non_interactive=True,
            env={},
            config=_config(),
            stdin=io.StringIO(),
        )
    assert "9999" in str(caught.value)


def test_railctl_port_env_var_has_no_effect():
    with_port = build_settings(
        target=None,
        address=None,
        fmt=None,
        json_flag=False,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=True,
        env={"RAILCTL_PORT": "/dev/whatever-this-would-be"},
        config=_config(),
        stdin=io.StringIO(),
    )
    without_port = build_settings(
        target=None,
        address=None,
        fmt=None,
        json_flag=False,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=True,
        env={},
        config=_config(),
        stdin=io.StringIO(),
    )
    assert with_port == without_port


def test_an_empty_environment_variable_is_treated_as_unset_for_both_keys():
    # `export RAILCTL_TARGET="$MAYBE_UNSET"` must not make the tool open target `''` and then
    # report a transport failure, and `export RAILCTL_ADDRESS=""` must not exit 2. Both fall
    # through to the config file, exactly as an unset variable does.
    settings = build_settings(
        target=None,
        address=None,
        fmt=None,
        json_flag=False,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=True,
        env={"RAILCTL_TARGET": "", "RAILCTL_ADDRESS": ""},
        config=_config(target="config-target", address=7),
        stdin=io.StringIO(),
    )
    assert settings.target == "config-target"
    assert settings.address == 7


def test_interactive_is_decided_by_stdin_isatty():
    class _Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    settings = build_settings(
        target=None,
        address=None,
        fmt=None,
        json_flag=False,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=False,
        env={},
        config=_config(),
        stdin=_Terminal(),
    )
    assert settings.interactive is True

    settings = build_settings(
        target=None,
        address=None,
        fmt=None,
        json_flag=False,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=False,
        env={},
        config=_config(),
        stdin=io.StringIO(),
    )
    assert settings.interactive is False

    # The discriminating half: a stdin that DOES report a terminal, with the flag set. Every
    # other call in this file pairs `non_interactive=True` with a `StringIO` whose `isatty()`
    # is already False, so both operands agree and `and not non_interactive` can be dropped
    # without anything noticing - and then `railctl --non-interactive restore` over a pseudo
    # terminal prompts and blocks on `stdin.readline()` forever instead of failing fast.
    settings = build_settings(
        target=None,
        address=None,
        fmt=None,
        json_flag=False,
        verbose=None,
        color="auto",
        yes=False,
        non_interactive=True,
        env={},
        config=_config(),
        stdin=_Terminal(),
    )
    assert settings.interactive is False


# -- merge_settings -----------------------------------------------------
#
# Tasks 10-12 give every registered command its own copy of all eight global
# options (Click parses group options before the subcommand name, so a bare
# `railctl doctor --address 3` would otherwise be a usage error) and layer
# them over `ctx.obj.settings` with this function. It is pinned here, where
# `Settings` and the sentinel rule are defined, rather than left for the first
# downstream task to invent its own version.


def test_an_unknown_format_is_rejected_naming_the_three_that_exist():
    with pytest.raises(ValueError) as caught:
        build_settings(
            target=None,
            address=None,
            fmt="yaml",
            json_flag=False,
            verbose=None,
            color="auto",
            yes=False,
            non_interactive=True,
            env={},
            config=_config(),
            stdin=io.StringIO(),
        )
    message = str(caught.value)
    assert "yaml" in message
    for known in ("human", "json", "ndjson"):
        assert known in message


def test_an_unknown_colour_is_rejected_naming_the_three_that_exist():
    # `--color` is the sibling of `--format`, so it must answer a bad value the same way.
    # Unvalidated, `--color allways version` exits 0 and paints the output anyway, because
    # `want_color` falls through any unrecognised choice to `stream.isatty()`.
    with pytest.raises(ValueError) as caught:
        build_settings(
            target=None,
            address=None,
            fmt=None,
            json_flag=False,
            verbose=None,
            color="allways",
            yes=False,
            non_interactive=True,
            env={},
            config=_config(),
            stdin=io.StringIO(),
        )
    message = str(caught.value)
    assert "allways" in message
    for known in ("auto", "always", "never"):
        assert known in message


def test_an_unknown_colour_exits_2_through_the_wired_callback(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["railctl", "--color", "allways", "version"])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["code"] == "usage"


def test_merge_settings_overrides_only_the_typed_fields():
    base = _settings(target="auto", address=None, fmt="human", color="auto")
    merged = merge_settings(base, address=7, fmt="json")
    assert merged.address == 7
    assert merged.fmt == "json"
    # Untyped fields (the sentinel: None/False/0) pass `base`'s value through
    # unchanged - this is what lets a command declare all eight options and
    # merge unconditionally, without first checking which ones the operator
    # actually passed on this particular invocation.
    assert merged.target == "auto"
    assert merged.color == "auto"
    assert merged.assume_yes is False


def test_merge_settings_json_flag_is_an_alias_for_format_json():
    merged = merge_settings(_settings(fmt="human"), json_flag=True)
    assert merged.fmt == "json"


def test_merge_settings_leaves_base_untouched_when_nothing_is_typed():
    base = _settings()
    assert merge_settings(base) == base


def test_merge_settings_covers_every_one_of_the_eight_global_options():
    # Tasks 10-12 hand all eight through on every invocation. A field this function
    # silently ignores would look like the operator's flag simply having no effect.
    base = _settings(target="auto", verbose=0, color="auto", assume_yes=False, interactive=True)
    merged = merge_settings(
        base,
        target="serial:auto",
        address=9,
        fmt="ndjson",
        verbose=2,
        color="never",
        yes=True,
        non_interactive=True,
    )
    assert merged.target == "serial:auto"
    assert merged.address == 9
    assert merged.fmt == "ndjson"
    assert merged.verbose == 2
    assert merged.color == "never"
    assert merged.assume_yes is True
    assert merged.interactive is False


# -- require_address ----------------------------------------------------


def test_require_address_returns_the_configured_address():
    assert require_address(_settings(address=3), argv_hint=["railctl", "drive", "40"]) == 3


def test_require_address_raises_a_usage_problem_with_the_documented_suggestion():
    with pytest.raises(UsageProblem) as caught:
        require_address(_settings(address=None), argv_hint=["railctl", "drive", "40"])
    assert caught.value.suggestions == [["railctl", "drive", "40", "--address", "3"]]


def test_a_usage_problems_argv_suggestion_survives_into_the_json_envelope():
    # Asserting `caught.value.suggestions` at the raise site (above) passes even when
    # `run()` drops the array on the way out, which is what made `UsageProblem` decoration
    # rather than a contract. This asserts the array a script actually receives.
    ctx = OutputContext(
        fmt="json",
        stdout_color=False,
        stderr_color=False,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    def work():
        return require_address(_settings(address=None), argv_hint=["railctl", "drive", "40"])

    with pytest.raises(typer.Exit) as caught:
        run("drive", ctx, work)
    assert caught.value.exit_code == 2
    assert ctx.stdout.getvalue() == ""
    payload = json.loads(ctx.stderr.getvalue())
    assert payload["code"] == "usage"
    assert payload["suggestions"] == [["railctl", "drive", "40", "--address", "3"]]
    # argv arrays, never a shell string: no element may need splitting to be runnable.
    for argv in payload["suggestions"]:
        for word in argv:
            assert " " not in word


# -- confirm --------------------------------------------------------------


def test_confirm_with_yes_returns_immediately_without_reading_stdin():
    class _NeverRead(io.StringIO):
        def readline(self, *a, **k):  # pragma: no cover - must never run
            raise AssertionError("confirm() must not read stdin when --yes was given")

    confirm(
        "really restore?",
        settings=_settings(assume_yes=True),
        stdin=_NeverRead(),
        stderr=io.StringIO(),
    )


def test_confirm_noninteractive_raises_confirmation_required_naming_yes():
    # `ConfirmationRequiredError` (Task 8) takes only `hint`/`details`, no
    # `suggestions` - the runnable `[..., "--yes"]` array a script sees in the
    # JSON envelope is assembled later, by `_errors.py`'s `default_suggestions`,
    # from the exception's type. All this layer can pin is that `confirm()`
    # itself still tells a human reader how to get past the prompt: this test
    # goes red the moment `confirm()`'s message stops mentioning `--yes`.
    class _NeverRead(io.StringIO):
        def readline(self, *a, **k):  # pragma: no cover - must never run
            raise AssertionError("confirm() must never block on a non-interactive stdin")

    with pytest.raises(ConfirmationRequiredError) as caught:
        confirm(
            "really restore?",
            settings=_settings(assume_yes=False, interactive=False),
            stdin=_NeverRead(),
            stderr=io.StringIO(),
        )
    assert "--yes" in str(caught.value)


def test_confirm_interactive_proceeds_on_y_and_prompts_on_stderr(capsys):
    # The prompt itself, and the stream it goes to. Left unasserted, `print(...)` can be
    # deleted outright - an interactive `restore` then waits with no question on screen -
    # or moved to stdout, which breaks "stdout holds exactly one JSON value" with nothing
    # in the suite to say so.
    stderr = io.StringIO()
    confirm(
        "really restore?",
        settings=_settings(assume_yes=False, interactive=True),
        stdin=io.StringIO("y\n"),
        stderr=stderr,
    )
    assert stderr.getvalue() == "really restore? [y/N] "
    assert capsys.readouterr().out == ""


def test_confirm_interactive_aborts_on_anything_else():
    with pytest.raises(AbortedError):
        confirm(
            "really restore?",
            settings=_settings(assume_yes=False, interactive=True),
            stdin=io.StringIO("n\n"),
            stderr=io.StringIO(),
        )


# -- configure_logging ------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_loggers():
    yield
    for name in ("railctl", "railctl.wire"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


def test_configure_logging_verbose_1_enables_decoded_diagnostics_and_keeps_wire_quiet(capsys):
    stderr = io.StringIO()
    configure_logging(1, stderr)
    logging.getLogger("railctl.station").info("decoded frame: status")
    logging.getLogger("railctl.wire").debug("TX 21 24 05")
    output = stderr.getvalue()
    assert "decoded frame: status" in output
    assert "TX 21 24 05" not in output
    assert capsys.readouterr().out == ""


def test_configure_logging_verbose_2_enables_wire_debug(capsys):
    stderr = io.StringIO()
    configure_logging(2, stderr)
    logging.getLogger("railctl.wire").debug("TX 21 24 05")
    assert "TX 21 24 05" in stderr.getvalue()
    assert capsys.readouterr().out == ""


# -- open_station / link_info / station_info --------------------------------


class _FakeStation:
    def __init__(self, *, identity="serial:7010A0001194:3", raw_version=0x40, station_id=0x12):
        self.identity = identity
        self._version = StationVersion(raw=raw_version, station_id=station_id)
        self.closed = False

    def version(self) -> StationVersion:
        return self._version

    def close(self) -> None:
        self.closed = True


def test_open_station_forwards_target_address_capabilities_path_and_timing(monkeypatch, tmp_path):
    calls = []

    def fake_open(target, *, default_address, capabilities_path, timing):
        calls.append((target, default_address, capabilities_path, timing))
        return _FakeStation()

    monkeypatch.setattr(Station, "open", staticmethod(fake_open))
    caps = tmp_path / "capabilities.json"
    settings = _settings(target="serial:auto", address=3)
    open_station(settings, capabilities_path=caps)
    assert calls == [("serial:auto", 3, caps, TIMING)]


def test_open_station_lets_transport_error_propagate(monkeypatch):
    def fake_open(*a, **k):
        raise TransportError("port vanished")

    monkeypatch.setattr(Station, "open", staticmethod(fake_open))
    with pytest.raises(TransportError):
        open_station(_settings(), capabilities_path=None)


def test_link_info_reads_identity_and_target():
    station = _FakeStation(identity="serial:7010A0001194:3")
    info = link_info(station, _settings(target="serial:auto"))
    assert info.identity == "serial:7010A0001194:3"
    assert info.target == "serial:auto"


def test_station_info_reads_protocol_facts_from_version():
    station = _FakeStation(raw_version=0x40, station_id=0x12)
    info = station_info(station)
    assert info.protocol == "xpressnet"
    assert info.protocol_version == "4.0"
    assert info.command_station_id == 18


# -- commands/basics.py: build_version / build_status ------------------------


def test_build_version_schema_and_command_fields():
    version = StationVersion(raw=0x40, station_id=0x12)
    result = build_version(version, tool_version="0.1.0")
    assert result.schema == VERSION_SCHEMA
    assert result.command == "version"


def test_build_version_result_and_lines_carry_the_same_three_facts():
    version = StationVersion(raw=0x40, station_id=0x12)
    result = build_version(version, tool_version="0.1.0")
    joined = " ".join(result.lines)
    for fact in ("4.0", "18", "0.1.0"):
        assert fact in json.dumps(result.result)
        assert fact in joined


def test_build_status_result_and_lines_carry_the_raw_byte_and_decoded_names():
    status = StationStatus.from_raw(0x04)  # bit 2 only: auto_start_mode
    result = build_status(status)
    assert result.schema == STATUS_SCHEMA
    assert result.result["raw"] == 0x04
    assert result.result["raw_hex"] == "0x04"
    assert result.result["auto_start_mode"] is True
    assert any("0x04" in line for line in result.lines)
    assert any("start mode" in line for line in result.lines)


def test_build_status_never_calls_bit_2_short_circuit():
    status = StationStatus.from_raw(0x04)
    result = build_status(status)
    assert "short" not in json.dumps(result.result).lower()
    assert "short" not in " ".join(result.lines).lower()


def test_build_status_track_power_is_false_when_emergency_off_is_set():
    # Bit 1 (0x02), NOT bit 0. The plan this test came from used the Lenz
    # order; the measured YD7010 order is the reverse (docs/probe-results.md,
    # and StationStatus's own docstring): bit 0 is emergency STOP, bit 1 is
    # emergency OFF, and only emergency off cuts track power.
    status = StationStatus.from_raw(0x02)
    result = build_status(status)
    assert result.result["emergency_off"] is True
    assert result.result["emergency_stop"] is False
    assert result.result["track_power"] is False
    assert "track power: off" in result.lines
    # The human lines too, not only `result`: these two labels are the exact bit pair this
    # project records as REVERSED from the Lenz spec, so they are the line most likely to be
    # wrong, and swapping the two labels leaves every `result[...]` assertion green.
    assert "emergency off: True" in result.lines
    assert "emergency stop: False" in result.lines


def test_build_status_emergency_stop_alone_leaves_track_power_on():
    # The state that separates the two documents: 80 80 sets bit 0 and the
    # track stays powered. A build_status that read bit 0 as emergency off
    # would report a live track as dead here.
    status = StationStatus.from_raw(0x01)
    result = build_status(status)
    assert result.result["emergency_stop"] is True
    assert result.result["emergency_off"] is False
    assert result.result["track_power"] is True
    assert "track power: on" in result.lines
    assert "emergency stop: True" in result.lines
    assert "emergency off: False" in result.lines


# -- main.py: app wiring, error paths, __main__ -------------------------------


@pytest.fixture(autouse=True)
def _isolated_config_dir(monkeypatch, tmp_path):
    # Every test below either builds `app`/`main()` for real or imports a
    # module that resolves `config_path()` at call time - none of them may
    # ever touch a developer's real ~/.config/railctl.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for key in ("RAILCTL_TARGET", "RAILCTL_ADDRESS", "RAILCTL_VERBOSE", "RAILCTL_FORMAT"):
        # setenv then delenv, not delenv alone: `global_options` WRITES RAILCTL_VERBOSE, and
        # monkeypatch can only undo a variable it recorded a value for. A bare
        # `delenv(..., raising=False)` on an absent key records nothing, so that write would
        # survive into the next test and be read there as an inherited environment value.
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def _patch_station(monkeypatch, station):
    monkeypatch.setattr(Station, "open", staticmethod(lambda *a, **k: station))


class _StatusStation(_FakeStation):
    def status(self) -> StationStatus:
        return StationStatus.from_raw(0x04)


class _TerminalStdin(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_both_wired_commands_open_the_station_with_the_resolved_arguments(monkeypatch, tmp_path):
    # `_patch_station` throws every argument away, so nothing used to constrain what the two
    # command bodies actually pass. Passing `capabilities_path=None` here is issue #15: the
    # doctor's measurements are written to that file, and a command that opens the station
    # without it silently discards them.
    calls = []

    def fake_open(target, *, default_address, capabilities_path, timing):
        calls.append((target, default_address, capabilities_path, timing))
        return _StatusStation()

    monkeypatch.setattr(Station, "open", staticmethod(fake_open))
    runner = CliRunner()
    common = ["--target", "z21:192.168.0.111:21105", "--address", "7"]
    for command in ("version", "status"):
        assert runner.invoke(cli_main.app, [*common, command]).exit_code == 0

    expected = (tmp_path / "railctl" / "capabilities.json",)
    assert calls == [("z21:192.168.0.111:21105", 7, *expected, TIMING)] * 2


def test_both_wired_commands_publish_the_link_block_that_names_the_station(monkeypatch):
    # `link.identity` is the only thing that tells two stations on one machine apart, and a
    # script reading `railctl --json version` reads it from here. Deleting both
    # `outcome.link = link_info(...)` lines left every other assertion in this file green.
    _patch_station(monkeypatch, _StatusStation(identity="serial:7010A0001194:3"))
    runner = CliRunner()
    for command in ("version", "status"):
        result = runner.invoke(
            cli_main.app, ["--target", "serial:auto", "--format", "json", command]
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout)["link"] == {
            "identity": "serial:7010A0001194:3",
            "target": "serial:auto",
        }


def _settings_a_command_read(monkeypatch, argv, *, stdin=None):
    """Run `railctl <argv> version` through the real callback and hand back the `Settings`
    the command actually read off `ctx.obj`.

    The spy replaces `open_station` inside `commands/basics.py`, which is the first thing
    either command does with `ctx.obj.settings` - so what it records is the resolved object
    the command works from, not an argument on its way into `build_settings`.
    """
    captured: list[Settings] = []

    def spy(settings, *, capabilities_path):
        captured.append(settings)
        return _FakeStation()

    monkeypatch.setattr(basics, "open_station", spy)
    monkeypatch.setattr(sys, "stdin", io.StringIO() if stdin is None else stdin)
    monkeypatch.setattr(sys, "argv", ["railctl", *argv, "version"])
    with pytest.raises(SystemExit):
        cli_main.main()
    assert captured, "the command never read ctx.obj.settings"
    return captured[0]


def test_version_command_json_output_is_one_value_with_station_facts(monkeypatch):
    _patch_station(monkeypatch, _FakeStation(raw_version=0x40, station_id=0x12))
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["--format", "json", "version"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["station"]["protocol_version"] == "4.0"
    assert payload["station"]["command_station_id"] == 18
    assert payload["result"]["tool_version"] == "0.1.0"


def test_version_command_human_output_contains_the_same_facts(monkeypatch):
    _patch_station(monkeypatch, _FakeStation(raw_version=0x40, station_id=0x12))
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["version"])
    assert result.exit_code == 0
    for fact in ("4.0", "18", "0.1.0"):
        assert fact in result.stdout


def test_status_command_json_carries_raw_byte_and_decoded_names(monkeypatch):
    _patch_station(monkeypatch, _StatusStation())
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["--format", "json", "status"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["result"]["raw_hex"] == "0x04"
    assert payload["result"]["auto_start_mode"] is True
    assert "short" not in json.dumps(payload).lower()


def test_status_command_human_carries_raw_byte_and_decoded_names(monkeypatch):
    _patch_station(monkeypatch, _StatusStation())
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["status"])
    assert result.exit_code == 0
    assert "0x04" in result.stdout
    assert "start mode" in result.stdout
    assert "short" not in result.stdout.lower()


def test_open_station_failure_exits_3_with_empty_stdout_and_json_stderr(monkeypatch):
    def fake_open(*a, **k):
        raise TransportError("the port vanished")

    monkeypatch.setattr(Station, "open", staticmethod(fake_open))
    runner = CliRunner()
    # `--format json` is what makes the error a JSON object: `render_error` (Task 8)
    # writes machine-readable errors in every mode BUT human, and human is the default.
    result = runner.invoke(cli_main.app, ["--format", "json", "version"])
    assert result.exit_code == 3
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["exit_code"] == 3


def test_open_station_failure_in_human_mode_writes_plain_text_and_empty_stdout(monkeypatch):
    def fake_open(*a, **k):
        raise TransportError("the port vanished")

    monkeypatch.setattr(Station, "open", staticmethod(fake_open))
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["version"])
    assert result.exit_code == 3
    assert result.stdout == ""
    assert "the port vanished" in result.stderr


def test_station_is_closed_even_when_the_command_body_raises(monkeypatch):
    class _FailingStation(_FakeStation):
        def version(self):
            raise TransportError("station went away mid-read")

    station = _FailingStation()
    _patch_station(monkeypatch, station)
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["version"])
    assert station.closed is True
    assert result.exit_code == 3


def test_address_out_of_range_exits_2_before_any_command_runs(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["railctl", "--address", "20000", "status"])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()
    assert caught.value.code == 2


def test_address_out_of_range_writes_json_error_and_empty_stdout(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["railctl", "--address", "20000", "status"])
    with pytest.raises(SystemExit):
        cli_main.main()
    captured = capsys.readouterr()
    assert captured.out == ""
    # Exactly one JSON value on stderr, on one line - the same shape `run()` writes once a
    # command has started, so a script does not need two parsers for the same failure.
    assert captured.err.count("\n") == 1
    payload = json.loads(captured.err)
    assert payload["exit_code"] == 2
    # The envelope must agree with the process status. Built with `report_for` instead of
    # `usage_report`, a plain ValueError publishes 1/"internal" here while the process still
    # exits 2 - a script reading the JSON would be told this tool has a bug.
    assert payload["code"] == "usage"


def test_bad_config_file_exits_2_naming_file_line_and_key(monkeypatch, capsys, tmp_path):
    bad = tmp_path / "railctl" / "config.toml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("bogus = 1\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["railctl", "status"])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()
    assert caught.value.code == 2
    message = json.loads(capsys.readouterr().err)["message"]
    assert str(bad) in message
    assert "bogus" in message


def test_an_unreadable_config_file_is_one_internal_envelope_not_a_traceback(
    monkeypatch, capsys, tmp_path
):
    # `Path.read_text` raises PermissionError, which is neither RailctlError nor ValueError.
    # Without a final safety net in `main()` the process ends in a Python traceback with no
    # `code` field, and a wrapper doing `json.loads(stderr)` raises instead of branching.
    config = tmp_path / "railctl" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('target = "auto"\n', encoding="utf-8")
    config.chmod(0o000)
    if os.access(config, os.R_OK):  # pragma: no cover - only when the suite runs as root
        config.chmod(0o600)
        pytest.skip("this user can read a mode-000 file, so it cannot be made unreadable")
    try:
        monkeypatch.setattr(sys, "argv", ["railctl", "--json", "status"])
        with pytest.raises(SystemExit) as caught:
            cli_main.main()
    finally:
        config.chmod(0o600)
    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    payload = json.loads(captured.err)
    assert payload["schema"] == "railctl/error/v1"
    assert payload["code"] == "internal"
    assert payload["exit_code"] == 1


def test_a_railctl_error_out_of_the_callback_keeps_its_own_exit_code(monkeypatch, capsys):
    # `load_config` raises only ValueError today, but the callback is where Task 12's
    # capabilities loading lands, and `Capabilities.load` raises RailctlError. The point
    # pinned here is that such a failure exits with the code its class is mapped to (3 for
    # TransportError), not the flat 2 a usage error gets.
    def explode(_path):
        raise TransportError("no station on this target")

    monkeypatch.setattr(cli_main, "load_config", explode)
    monkeypatch.setattr(sys, "argv", ["railctl", "status"])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()
    assert caught.value.code == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["code"] == "transport"
    assert payload["exit_code"] == 3


# -- main.py: every global option, end to end through the real callback -------
#
# One test per option, each asserting the value a command reads off `ctx.obj.settings`.
# Only `--format` and `--address` used to be pinned this way, and dropping any of the other
# six from the `build_settings(...)` call left the whole suite green: `railctl --target
# serial:auto status` would then auto-detect a different port, open the wrong station, and
# report its status as the one that was asked for.


def test_target_flag_reaches_the_settings_a_command_reads(monkeypatch):
    settings = _settings_a_command_read(monkeypatch, ["--target", "z21:192.168.0.111:21105"])
    assert settings.target == "z21:192.168.0.111:21105"


def test_address_flag_reaches_the_settings_a_command_reads(monkeypatch):
    settings = _settings_a_command_read(monkeypatch, ["--address", "7"])
    assert settings.address == 7


def test_format_flag_reaches_the_settings_a_command_reads(monkeypatch):
    settings = _settings_a_command_read(monkeypatch, ["--format", "ndjson"])
    assert settings.fmt == "ndjson"


def test_json_flag_reaches_the_settings_a_command_reads(monkeypatch):
    settings = _settings_a_command_read(monkeypatch, ["--json"])
    assert settings.fmt == "json"


def test_verbose_flag_reaches_the_settings_a_command_reads(monkeypatch):
    settings = _settings_a_command_read(monkeypatch, ["-vv"])
    assert settings.verbose == 2


def test_color_flag_reaches_the_settings_a_command_reads(monkeypatch):
    settings = _settings_a_command_read(monkeypatch, ["--color", "never"])
    assert settings.color == "never"


def test_yes_flag_reaches_the_settings_a_command_reads(monkeypatch):
    settings = _settings_a_command_read(monkeypatch, ["--yes"])
    assert settings.assume_yes is True


def test_non_interactive_flag_reaches_the_settings_a_command_reads(monkeypatch):
    # Both halves over a stdin that reports a terminal, which is the only stdin the two
    # operands disagree on. Paired with a StringIO - `isatty()` already False - the flag can
    # be dropped entirely and nothing changes: `railctl --non-interactive restore` over a
    # pseudo terminal would then prompt and block on `stdin.readline()` forever.
    forced = _settings_a_command_read(monkeypatch, ["--non-interactive"], stdin=_TerminalStdin())
    assert forced.interactive is False
    left_alone = _settings_a_command_read(monkeypatch, [], stdin=_TerminalStdin())
    assert left_alone.interactive is True


def _explode_on_open(monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(Station, "open", staticmethod(explode))


def test_double_verbose_puts_a_traceback_on_stderr_for_an_unexpected_exception(monkeypatch):
    # `-vv` is the documented way to get a traceback. Before `global_options` wrote the
    # resolved verbosity into RAILCTL_VERBOSE, the flag configured logging and nothing else,
    # and the only way to reach the traceback switch was a variable no help text mentions.
    _explode_on_open(monkeypatch)
    result = CliRunner().invoke(cli_main.app, ["-vv", "--json", "version"])
    assert result.exit_code == 1
    assert "Traceback" in result.stderr
    # The traceback is extra diagnostics on stderr, never a replacement for the envelope.
    assert json.loads(result.stderr.splitlines()[-1])["code"] == "internal"


def test_without_verbose_the_same_failure_is_one_envelope_and_no_traceback(monkeypatch):
    _explode_on_open(monkeypatch)
    result = CliRunner().invoke(cli_main.app, ["version"])
    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert result.stdout == ""


def test_the_resolved_verbosity_reaches_the_environment_the_traceback_switch_reads(monkeypatch):
    # The write itself, pinned separately from its effect: `-v` and RAILCTL_VERBOSE must
    # never be able to disagree, whichever of the four sources decided the number.
    _settings_a_command_read(monkeypatch, ["-v"])
    assert os.environ["RAILCTL_VERBOSE"] == "1"


def _write_config(tmp_path, body: str) -> None:
    path = tmp_path / "railctl" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.mark.parametrize("argv", [["--help"], ["status", "--help"], ["version", "--help"]])
def test_help_works_at_every_level_even_with_an_unusable_config_file(tmp_path, argv):
    # One typo in config.toml must not hide the page that names the recognised keys.
    # Resolving in the group callback broke this: Click runs a group callback BEFORE a
    # subcommand's eager --help, so `railctl status --help` exited 2 with empty stdout.
    _write_config(tmp_path, 'targt = "auto"\n')
    result = CliRunner().invoke(cli_main.app, argv)
    assert result.exit_code == 0
    assert "Usage" in result.stdout


def test_help_still_works_when_a_global_option_would_not_resolve(tmp_path):
    # The other two ways resolution used to fail before --help was reached: an out-of-range
    # --address and an unknown RAILCTL_FORMAT.
    runner = CliRunner()
    assert runner.invoke(cli_main.app, ["--address", "20000", "status", "--help"]).exit_code == 0
    assert (
        runner.invoke(cli_main.app, ["status", "--help"], env={"RAILCTL_FORMAT": "xml"}).exit_code
        == 0
    )


def test_a_valid_config_file_is_still_resolved_for_a_real_command(monkeypatch, tmp_path):
    # Deferring resolution must not turn it off: a command still reads the config file.
    _write_config(tmp_path, "address = 7\n")
    settings = _settings_a_command_read(monkeypatch, [])
    assert settings.address == 7


def test_a_bare_invocation_writes_the_error_to_stderr_and_leaves_stdout_empty():
    # "stdout carries the result only" holds for the no-arguments case too: a script that
    # ran `railctl` by mistake must not have to tell 944 bytes of help text apart from a
    # result. `no_args_is_help=True` puts the help on stdout with exit 2, which is both.
    result = CliRunner().invoke(cli_main.app, [])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr != ""


class _ColourStream(io.StringIO):
    """A stream that answers `isatty()` the way the test asks it to."""

    def __init__(self, *, terminal: bool) -> None:
        super().__init__()
        self._terminal = terminal

    def isatty(self) -> bool:
        return self._terminal


def _streams_after(settings, *, stdout_terminal: bool, stderr_terminal: bool, work):
    stdout = _ColourStream(terminal=stdout_terminal)
    stderr = _ColourStream(terminal=stderr_terminal)
    ctx = cli_main.context_for(settings, stdout=stdout, stderr=stderr)
    with pytest.raises(typer.Exit):
        run("status", ctx, work)
    return stdout.getvalue(), stderr.getvalue()


def test_context_for_decides_colour_for_each_stream_separately(monkeypatch):
    # `railctl status 2> errors.log` from a terminal must leave the log greppable, and the
    # converse - stdout to a pipe, stderr still on the terminal - must keep the error painted.
    # One shared flag fails one of the two whichever stream it is read from.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    settings = _settings(fmt="human", color="auto")

    def fail():
        raise TransportError("the port vanished")

    def succeed():
        return CommandResult(schema=STATUS_SCHEMA, command="status")

    _, quiet_stderr = _streams_after(
        settings, stdout_terminal=True, stderr_terminal=False, work=fail
    )
    assert "\x1b" not in quiet_stderr

    _, painted_stderr = _streams_after(
        settings, stdout_terminal=False, stderr_terminal=True, work=fail
    )
    assert "\x1b" in painted_stderr

    painted_stdout, _ = _streams_after(
        settings, stdout_terminal=True, stderr_terminal=False, work=succeed
    )
    assert "\x1b" in painted_stdout

    quiet_stdout, _ = _streams_after(
        settings, stdout_terminal=False, stderr_terminal=True, work=succeed
    )
    assert "\x1b" not in quiet_stdout


def test_dunder_main_module_calls_main(monkeypatch):
    calls = []
    monkeypatch.setattr(cli_main, "main", lambda: calls.append(True))
    runpy.run_module("railctl.__main__", run_name="__main__")
    assert calls == [True]


def test_importing_dunder_main_as_a_module_does_not_run_main(monkeypatch):
    # `import railctl.__main__` happens on every `python -m railctl` before the module is
    # re-executed under `__main__`. Without the name guard the CLI would run twice.
    calls = []
    monkeypatch.setattr(cli_main, "main", lambda: calls.append(True))
    module = importlib.import_module("railctl.__main__")
    importlib.reload(module)
    assert calls == []
    monkeypatch.undo()
    importlib.reload(module)
