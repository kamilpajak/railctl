"""Pins the command metadata table and `railctl schema`.

`COMMANDS` holds one row per command this commit actually registers - see the
note at the top of this task in the plan. `status`, `version` and `schema`
are real; the movement commands and `doctor` extend this same tuple in their
own later commits.
"""

from __future__ import annotations

import pytest

from railctl.cli import config
from railctl.cli._meta import (
    BASE_EXIT_CODES,
    COMMANDS,
    GLOBAL_OPTIONS,
    Argument,
    Option,
    command_meta,
    error_codes,
    global_option,
    help_epilog,
    manifest,
    typer_argument,
    typer_option,
)
from railctl.cli.result import RESERVED_CODES, RETRYABLE_CODES
from railctl.errors import EXIT_CODES

KNOWN_CODES = set(BASE_EXIT_CODES) | set(EXIT_CODES.values())


def test_command_meta_returns_the_row_for_a_known_path():
    assert command_meta("status").path == "status"
    assert command_meta("version").schema == "railctl/version/v1"
    assert command_meta("schema").mutates is False


def test_command_meta_unknown_path_orders_the_closest_match_first():
    with pytest.raises(ValueError, match="statuz") as caught:
        command_meta("statuz")
    message = str(caught.value)
    suggestions = message.split("closest known paths:")[1].strip()
    names = [n.strip() for n in suggestions.split(",")]
    assert names[0] == "status"  # the true near miss, ranked ahead of the other two


def test_global_options_cover_the_eight_design_flags():
    names = {o.name for o in GLOBAL_OPTIONS}
    assert names == {
        "--target",
        "--address",
        "--format",
        "--json",
        "--verbose",
        "--color",
        "--yes",
        "--non-interactive",
    }
    by_name = {o.name: o for o in GLOBAL_OPTIONS}
    assert by_name["--address"].short == "-a"
    assert by_name["--verbose"].short == "-v"
    assert by_name["--verbose"].repeatable is True
    assert by_name["--yes"].short == "-y"
    # None/False defaults are what let build_settings (Task 9) tell "not given"
    # from "given the human default" - a "helpful" non-None default here would
    # silently break every precedence test in tests/cli/test_config.py.
    assert by_name["--target"].default is None
    assert by_name["--format"].default is None
    assert by_name["--verbose"].default is None
    assert by_name["--color"].default == "auto"


def test_config_backed_global_options_match_config_keys():
    flag_names = {o.name.lstrip("-") for o in GLOBAL_OPTIONS}
    assert set(config.CONFIG_KEYS) <= flag_names


def test_every_real_manifest_exit_code_is_a_known_code():
    for meta in COMMANDS:
        # A subset check alone is satisfied by ANY subset, the empty set included:
        # `_VERSION.exit_codes = (0,)` passed it. The two assertions under it are the
        # cheap half of the floor; `test_the_observed_exit_code_is_one_the_command_
        # publishes` below is the half that drives the command and looks.
        assert set(meta.exit_codes) <= KNOWN_CODES, meta.path
        assert meta.exit_codes, meta.path
        assert 0 in meta.exit_codes, meta.path


def test_the_enum_rows_are_the_same_tuples_deps_validates_against():
    # The manifest's `enum` list and the check that rejects a bad value must be
    # one tuple, not two that agree today. `_meta` imports both from `deps`, so
    # a fourth format added there appears in the manifest with no edit here.
    from railctl.cli.deps import ALLOWED_COLORS, ALLOWED_FORMATS

    by_name = {o.name: o for o in GLOBAL_OPTIONS}
    assert by_name["--format"].enum is ALLOWED_FORMATS
    assert by_name["--color"].enum is ALLOWED_COLORS


def test_typer_option_attaches_no_click_level_callback():
    # An enum is published metadata, not a Click-level check. A `callback=` that
    # raised `typer.BadParameter` would make a bad `--format` exit through
    # Click's own usage text instead of the `railctl/error/v1` envelope - see
    # this task's report for the two tests that pinned the envelope first.
    option = Option(name="--format", help="x", type="enum", enum=("human", "json"), default=None)
    assert typer_option(option).callback is None


def test_typer_option_builds_the_repeatable_count_flag_for_verbose():
    verbose = next(o for o in GLOBAL_OPTIONS if o.name == "--verbose")
    built = typer_option(verbose)
    assert built.count is True
    assert built.default is None


def test_typer_option_forwards_no_environment_variable_to_click():
    # `Option.env` is published metadata (the manifest names the variables) and
    # `build_settings` is the single place that reads them, at the environment
    # level of `pick()`. Click reading them first takes that level away: it
    # type-casts RAILCTL_ADDRESS itself, outside the `railctl/error/v1`
    # envelope, and it drops RAILCTL_FORMAT into the slot a typed `--format`
    # occupies, so `--json` then reads as a conflict with a flag nobody typed.
    for option in GLOBAL_OPTIONS:
        assert typer_option(option).envvar is None, option.name


def test_typer_argument_required_uses_ellipsis_and_optional_uses_none():
    required = typer_argument(Argument(name="cv", help="x"))
    optional = typer_argument(Argument(name="path", help="x", required=False))
    assert required.default is ...
    assert optional.default is None


def test_manifest_with_no_path_returns_the_tree_shape():
    payload = manifest(None)
    assert payload["schema"] == "railctl/schema/v1"
    assert [c["path"] for c in payload["commands"]] == ["status", "version", "schema"]
    assert {o["name"] for o in payload["global_options"]} == {o.name for o in GLOBAL_OPTIONS}


def test_manifest_for_a_single_path_matches_the_tree_entry_shape():
    tree = manifest(None)
    single = manifest(["status"])
    tree_entry = next(c for c in tree["commands"] if c["path"] == "status")
    assert single["command"] == tree_entry


def test_manifest_for_an_unknown_path_raises_value_error():
    with pytest.raises(ValueError, match="power on"):
        manifest(["power", "on"])


def test_global_option_builds_a_bare_copy_with_no_envvar():
    # Exercises all three `_bare_default` branches: a plain string/int field,
    # the repeatable counter, and a boolean flag.
    address = global_option("--address")
    assert address.envvar is None  # the root callback already resolved RAILCTL_ADDRESS once
    assert address.default is None
    verbose = global_option("--verbose")
    assert verbose.default == 0  # a per-command --verbose is a bare counter, never "not given"
    assert verbose.count is True  # the enum/count machinery still comes from typer_option
    yes = global_option("--yes")
    assert yes.default is False


def test_help_epilog_includes_headings_and_meanings_for_every_exit_code():
    epilog = help_epilog(command_meta("status"))
    assert "OUTPUT" in epilog
    assert "EXIT CODES" in epilog
    assert "EXAMPLES" in epilog
    assert "railctl/status/v1" in epilog
    # Code 5 is LinkTimeout - pin the actual meaning, not just "a line exists",
    # so a rewrite that hard-codes a placeholder string here goes red too.
    assert "5: No reply arrived within the budget" in epilog
    # Codes 0/1/2 have no exception class; this is the branch that does not
    # go through errors.EXIT_CODES at all.
    assert "2: usage error" in epilog


def test_help_epilog_names_the_required_arguments_in_its_example():
    # `schema`'s own argument is optional, so it must NOT appear in the example
    # line; the filter that drops it is the one a required argument depends on.
    assert "railctl schema --format json" in help_epilog(command_meta("schema"))


# -- A1 (issue #28): the manifest publishes the error CODE STRINGS ------------


def test_error_codes_list_exactly_the_exception_tree_plus_the_two_reserved_codes():
    """The published `code` strings, checked against the frozen second copy.

    `tests/cli/test_errors.py::PUBLISHED_ERROR_CODES` exists precisely so an
    edit to a contract string shows up in a diff. Reading it here - rather than
    walking the tree a second time in this file - is what makes this test able
    to fail: a manifest builder that walked the tree wrongly and a test that
    walked it the same wrong way would agree with each other and with nothing
    else.
    """
    from tests.cli.test_errors import PUBLISHED_ERROR_CODES

    published = {row["code"] for row in error_codes()}
    assert published == set(PUBLISHED_ERROR_CODES.values()) | RESERVED_CODES
    assert len(error_codes()) == len(published)  # no duplicate rows
    assert len(published) == 33


def test_error_code_rows_read_their_facts_off_the_class():
    rows = {row["code"]: row for row in error_codes()}
    assert rows["link_timeout"] == {
        "code": "link_timeout",
        "exit_code": 5,
        "retryable": True,
        "summary": "No reply arrived within the budget. Silence - never a negative answer.",
    }
    # StationError has no row in EXIT_CODES on purpose and resolves to the base 9.
    assert rows["station"]["exit_code"] == 9
    assert rows["unsupported_command"]["retryable"] is False
    assert set(rows) >= RETRYABLE_CODES


def test_the_two_reserved_codes_carry_the_cli_exit_codes_they_are_defined_by():
    rows = {row["code"]: row for row in error_codes()}
    assert rows["usage"]["exit_code"] == 2
    assert rows["internal"]["exit_code"] == 1
    assert rows["usage"]["retryable"] is False
    assert rows["internal"]["retryable"] is False
    for code in RESERVED_CODES:
        assert rows[code]["summary"]


def test_a_half_defined_exception_class_never_reaches_the_manifest():
    """CPython registers a class with its bases before `__init_subclass__` runs,
    so a subclass that forgets its `code` stays in `__subclasses__()` as a
    zombie. Without the `__module__` filter it would appear in the manifest
    with no code at all - and only for test runs that happened to define it
    first.
    """
    from railctl.errors import RailctlError

    before = error_codes()
    with pytest.raises(TypeError):

        class Zombie(RailctlError):  # pragma: no cover - never instantiated
            pass

    assert error_codes() == before


def test_the_manifest_publishes_the_error_codes():
    tree = manifest(None)
    single = manifest(["status"])
    assert tree["error_codes"] == error_codes()
    # A caller who asked about one command still gets the whole error contract:
    # `code` is not per command, and a script branching on it must not have to
    # ask for the whole tree to learn the names.
    assert single["error_codes"] == error_codes()


# -- build_schema ------------------------------------------------------------

from railctl.cli.commands.schema import build_schema  # noqa: E402


def test_build_schema_returns_the_tree_when_no_path_is_given():
    result = build_schema(None)
    assert result.schema == "railctl/schema/v1"
    assert result.command == "schema"
    assert [c["path"] for c in result.result["commands"]] == ["status", "version", "schema"]


def test_build_schema_raises_value_error_for_an_unknown_path():
    with pytest.raises(ValueError, match="power on"):
        build_schema(["power", "on"])


def test_build_schema_names_every_command_in_the_human_lines_too():
    # The human rendering prints `result.lines` and nothing else, so a manifest
    # with no lines answers `railctl schema` with the single word "schema: ok".
    lines = "\n".join(build_schema(None).lines)
    for meta in COMMANDS:
        assert meta.path in lines
        assert meta.help in lines


def test_build_schema_for_one_command_says_only_that_command():
    lines = "\n".join(build_schema(["status"]).lines)
    assert "status" in lines
    assert "version" not in lines


# -- the wired app: drift, option names, JSON shape, help, flag position -------

import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402

import typer  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from railctl.cli._meta import CommandMeta  # noqa: E402
from railctl.cli.main import app  # noqa: E402
from railctl.station import Station  # noqa: E402
from railctl.xbus.replies import StationStatus, StationVersion  # noqa: E402

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch, tmp_path):
    # Every test below invokes the real `app`, whose root `global_options`
    # callback always calls `load_config(config_path())` before a subcommand
    # runs - without this, any test here would read whatever real
    # ~/.config/railctl/config.toml happens to exist on the machine running
    # the suite (the same isolation tests/cli/test_wiring.py already applies).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for key in ("RAILCTL_TARGET", "RAILCTL_ADDRESS", "RAILCTL_VERBOSE", "RAILCTL_FORMAT"):
        # setenv then delenv, not delenv alone: the callback WRITES
        # RAILCTL_VERBOSE, and monkeypatch can only undo a variable it recorded
        # a value for.
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


class _FakeStatusStation:
    """Bare stand-in for the `status`-based invocation-order tests below.
    `status` is the pinned example (not `schema`), because it is the one
    command in this file that actually opens a `Station` - proving the
    --format-position parity on a command that never touches a port would not
    catch a per-command global-option block that forgot to route through the
    real `open_station`/`run()` plumbing.
    """

    identity = "serial:7010A0001194:3"

    def status(self) -> StationStatus:
        return StationStatus.from_raw(0x00)

    def version(self) -> StationVersion:
        return StationVersion(raw=0x40, station_id=0x12)

    def close(self) -> None:
        pass


@pytest.fixture
def fake_station(monkeypatch):
    monkeypatch.setattr(Station, "open", staticmethod(lambda *a, **k: _FakeStatusStation()))


def registered_paths(app: typer.Typer) -> set[str]:
    """Every leaf command path Typer actually routes, at any nesting depth -
    there is no nesting yet, but the walk costs nothing and later tasks add
    `power on`/`power off` under a `power` group without this helper changing.
    """
    click_app = typer.main.get_command(app)
    paths: set[str] = set()

    def _walk(group, prefix: str) -> None:
        for name, cmd in group.commands.items():
            path = f"{prefix} {name}".strip()
            if hasattr(cmd, "commands"):
                _walk(cmd, path)
            else:
                paths.add(path)

    _walk(click_app, "")
    return paths


def _leaf_command(path: str):
    click_app = typer.main.get_command(app)
    for word in path.split(" "):
        click_app = click_app.commands[word]
    return click_app


def _long_option_names(command) -> set[str]:
    # `param.param_type_name`, not `isinstance(param, click.Option)`: Typer 0.27
    # vendors Click as the private `typer._click`, and importing from a private
    # module is how this file would break on a Typer upgrade that moves it.
    names: set[str] = set()
    for param in command.params:
        if param.param_type_name == "option":
            names.update(opt for opt in param.opts if opt.startswith("--"))
    return names


def test_every_registered_command_has_a_metadata_row_and_vice_versa():
    assert registered_paths(app) == {c.path for c in COMMANDS}


def test_a_missing_registration_would_fail_the_drift_check():
    # A fresh app, deliberately NOT the shared `app` above, carrying one
    # command with no metadata row at all. Goes red if `registered_paths`
    # stops walking the Click tree for real - a version that returned `set()`
    # unconditionally, or a hard-coded literal, fails this immediately.
    copy = typer.Typer(add_completion=False)

    @copy.callback()
    def _root() -> None:  # pragma: no cover - never invoked
        ...

    @copy.command("throwaway")
    def _throwaway() -> None:  # pragma: no cover - never invoked
        ...

    assert registered_paths(copy) - {c.path for c in COMMANDS} == {"throwaway"}


@pytest.mark.parametrize("meta", COMMANDS, ids=lambda m: m.path)
def test_option_names_match_between_typer_and_metadata(meta: CommandMeta):
    # Every registered command declares its own metadata options PLUS the
    # eight global ones a second time (see the per-command global-option
    # note in `_meta.global_option`) - a command that forgot that block would
    # otherwise look complete against only its own (possibly empty) option
    # list, so the union with GLOBAL_OPTIONS is what makes an omission fail.
    typer_names = _long_option_names(_leaf_command(meta.path)) - {"--help"}
    assert typer_names == {o.name for o in meta.options} | {o.name for o in GLOBAL_OPTIONS}


@pytest.mark.parametrize("meta", [c for c in COMMANDS if c.path != "schema"], ids=lambda m: m.path)
def test_the_observed_exit_code_is_one_the_command_publishes(meta: CommandMeta):
    """Drive the command for real and check the answer against its own row.

    `z21:` is one of the three `--target` forms the same manifest advertises, and no
    command serves it yet, so every station command reaches `UnsupportedFeatureError`
    and exit 7 - a code both station rows omitted while publishing 3, 4, 5 and 9. A
    subset check over hand-written literals cannot see that; only running the thing can.
    """
    result = runner.invoke(
        app, [meta.path, "--target", "z21:1.2.3.4", "--format", "json", "--non-interactive"]
    )
    # Not 0: this target is deliberately one nothing can serve, so a success here would
    # mean the invocation never reached the station and the check below proved nothing.
    assert result.exit_code != 0
    assert result.exit_code in meta.exit_codes
    # The process status and the envelope are one answer, never two.
    assert json.loads(result.stderr)["exit_code"] == result.exit_code


def test_global_options_match_the_root_group():
    root = typer.main.get_command(app)
    typer_names = _long_option_names(root) - {"--help"}
    assert typer_names == {o.name for o in GLOBAL_OPTIONS}


def test_schema_json_prints_one_envelope_with_the_registered_paths_in_tree_order():
    result = runner.invoke(app, ["--format", "json", "schema"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)["result"]
    assert payload["schema"] == "railctl/schema/v1"
    assert [c["path"] for c in payload["commands"]] == ["status", "version", "schema"]


def test_schema_for_a_not_yet_implemented_command_is_exit_2_with_near_misses():
    result = runner.invoke(app, ["--format", "json", "schema", "power", "on"])
    assert result.exit_code == 2
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["code"] == "usage"
    assert "power on" in payload["message"]
    assert "status" in payload["message"]  # one of the three known paths, named


def test_schema_for_a_single_command_matches_the_tree_entry_shape():
    tree = json.loads(runner.invoke(app, ["--format", "json", "schema"]).stdout)["result"]
    single = json.loads(runner.invoke(app, ["--format", "json", "schema", "status"]).stdout)
    entry = single["result"]["command"]
    tree_entry = next(c for c in tree["commands"] if c["path"] == "status")
    assert entry == tree_entry
    assert set(entry) == set(tree_entry)


def test_help_is_deterministic_offline_and_unwrapped():
    first = runner.invoke(app, ["schema", "--help"])
    second = runner.invoke(app, ["schema", "--help"])
    assert first.exit_code == 0
    assert first.stdout == second.stdout  # two consecutive runs, byte-identical
    assert "schema" in first.stdout
    assert all(heading in first.stdout for heading in ("OUTPUT", "EXIT CODES", "EXAMPLES"))


def test_help_still_works_when_the_config_file_is_unreadable(tmp_path):
    # `--help` must work offline at every level. `CliContext` resolves settings
    # lazily for exactly this reason, and a per-command option block that
    # resolved them eagerly would put the one page naming the recognised keys
    # behind the broken file it explains.
    broken = tmp_path / "railctl" / "config.toml"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("target = [oops\n", encoding="utf-8")
    result = runner.invoke(app, ["schema", "--help"])
    assert result.exit_code == 0
    assert "EXIT CODES" in result.stdout


def _broken_config(tmp_path) -> None:
    broken = tmp_path / "railctl" / "config.toml"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("target = [oops\n", encoding="utf-8")


def test_schema_prints_the_manifest_when_the_config_file_cannot_be_parsed(tmp_path):
    # The manifest is a compile-time constant, and `schema` is what an agent runs
    # first, on a machine with nothing plugged in and possibly nothing configured.
    # A stray bracket in `config.toml` used to take down that one command.
    _broken_config(tmp_path)
    result = runner.invoke(app, ["schema", "--format=json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["result"]["schema"] == "railctl/schema/v1"


def test_schema_prints_the_manifest_when_the_config_file_cannot_be_read(tmp_path):
    if os.geteuid() == 0:  # pragma: no cover - the suite does not run as root
        pytest.skip("mode 000 does not stop root from reading the file")
    unreadable = tmp_path / "railctl" / "config.toml"
    unreadable.parent.mkdir(parents=True, exist_ok=True)
    unreadable.write_text("target = 'auto'\n", encoding="utf-8")
    unreadable.chmod(0o000)
    try:
        result = runner.invoke(app, ["schema", "--format=json"])
    finally:
        unreadable.chmod(0o600)
    assert result.exit_code == 0
    assert json.loads(result.stdout)["result"]["schema"] == "railctl/schema/v1"


def test_a_command_that_does_read_the_config_file_still_reports_a_broken_one(
    monkeypatch, capsys, tmp_path, fake_station
):
    # The other half: `schema` skipping the file is a property of `schema`, not the
    # config file quietly ceasing to be checked for everything else.
    import railctl.cli.main as cli_main

    _broken_config(tmp_path)
    monkeypatch.setattr(sys, "argv", ["railctl", "status", "--format=json"])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()
    assert caught.value.code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "usage"
    assert "config.toml" in payload["message"]


@pytest.mark.parametrize("path", ["status", "version", "schema"])
def test_none_of_the_registered_commands_mutates_anything(path: str):
    assert command_meta(path).mutates is False
    assert command_meta(path).confirms is False


def test_global_options_carry_their_env_vars_and_no_color_on_color():
    by_name = {o.name: o for o in GLOBAL_OPTIONS}
    assert by_name["--target"].env == "RAILCTL_TARGET"
    assert by_name["--address"].env == "RAILCTL_ADDRESS"
    assert by_name["--format"].env == "RAILCTL_FORMAT"
    assert by_name["--verbose"].env == "RAILCTL_VERBOSE"
    assert by_name["--color"].env == "NO_COLOR"


def test_schema_never_opens_a_station(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("schema must never open a station")

    monkeypatch.setattr(Station, "open", staticmethod(_boom))
    result = runner.invoke(app, ["schema", "status"])
    assert result.exit_code == 0


def test_no_fuzzy_abbreviation_for_status():
    result = runner.invoke(app, ["st"])
    assert result.exit_code == 2
    assert "track power" not in result.stdout.lower()


@pytest.mark.parametrize("meta", COMMANDS, ids=lambda m: m.path)
def test_the_envelope_carries_the_schema_string_the_manifest_publishes(
    fake_station, meta: CommandMeta
):
    # The manifest says what a command emits; the envelope says what it emitted.
    # Written as two literals, a bump to `/v2` in one of them leaves a consumer
    # keyed on `schema` matching nothing, with no test saying so.
    result = runner.invoke(app, [meta.path, "--format", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["schema"] == meta.schema


@pytest.mark.parametrize("meta", COMMANDS, ids=lambda m: m.path)
def test_command_help_text_matches_the_metadata_row(meta: CommandMeta):
    # `version`/`status` carried their own hand-written `help=` before this
    # task; this is what makes the manifest and `--help` one string rather
    # than two that happen to agree.
    assert _leaf_command(meta.path).help == meta.help


@pytest.mark.parametrize("meta", COMMANDS, ids=lambda m: m.path)
def test_every_command_publishes_its_epilog_sections(meta: CommandMeta):
    assert _leaf_command(meta.path).epilog == help_epilog(meta)


def test_a_command_still_runs_with_no_color_set_in_the_environment():
    # `--color`'s row keeps `env="NO_COLOR"` as documentation (asserted
    # above) but `typer_option` must never forward it to Click: Click would
    # resolve `--color` to "1", which is not one of auto/always/never, and
    # every command would exit 2 for everyone who sets NO_COLOR globally.
    result = runner.invoke(app, ["schema"], env={"NO_COLOR": "1"})
    assert result.exit_code == 0
    assert "\x1b[" not in result.stdout


def test_format_after_the_subcommand_name_is_accepted(fake_station):
    result = runner.invoke(app, ["status", "--format", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["schema"] == "railctl/status/v1"


def test_format_before_or_after_the_subcommand_produces_identical_stdout(fake_station):
    # M6's acceptance sentence in miniature: `railctl status --format json`
    # and `railctl --format json status` must be indistinguishable to a
    # script. task-11.md and task-12.md pin the same property for `drive`
    # and `doctor` once those commands exist.
    before = runner.invoke(app, ["--format", "json", "status"])
    after = runner.invoke(app, ["status", "--format", "json"])
    assert before.exit_code == after.exit_code == 0
    assert json.loads(before.stdout) == json.loads(after.stdout)


def test_json_alias_after_the_subcommand_name_is_accepted(fake_station):
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["schema"] == "railctl/status/v1"


def test_address_after_the_subcommand_name_reaches_the_station(monkeypatch):
    # The reason every command repeats the eight global options at all: Click
    # parses a group's own options only before the subcommand name, so without
    # the copy this invocation is a usage error before `status` ever runs.
    seen: list[int | None] = []

    def fake_open(target, *, default_address, capabilities_path, timing):
        seen.append(default_address)
        return _FakeStatusStation()

    monkeypatch.setattr(Station, "open", staticmethod(fake_open))
    assert runner.invoke(app, ["status", "--address", "7"]).exit_code == 0
    assert runner.invoke(app, ["--address", "7", "status"]).exit_code == 0
    assert seen == [7, 7]


def test_a_bad_format_after_the_subcommand_fails_exactly_like_one_before_it(fake_station):
    # Both positions must refuse the same way. A Click-level `callback=` on the
    # enum would have made this pair disagree: the one before the verb keeps
    # producing the `railctl/error/v1` envelope, the one after it would print
    # Click's own usage text instead.
    before = runner.invoke(app, ["--format", "xml", "status"])
    after = runner.invoke(app, ["status", "--format", "xml"])
    assert before.stdout == after.stdout == ""
    assert isinstance(before.exception, ValueError)
    assert isinstance(after.exception, ValueError)
    assert str(before.exception) == str(after.exception)


@pytest.mark.parametrize(
    "argv",
    [
        ["status", "--format", "xml"],
        ["status", "--color", "allways"],
        ["status", "--json", "--format", "ndjson"],
    ],
)
def test_a_bad_value_after_the_subcommand_exits_2_with_the_usage_envelope(
    monkeypatch, capsys, fake_station, argv: list[str]
):
    # Through `main()`, the real entry point: exit 2, empty stdout, one
    # `railctl/error/v1` object on stderr. This is the contract a Click-level
    # enum guard would have broken, so it is pinned before the shape of the
    # guard is chosen.
    import railctl.cli.main as cli_main

    monkeypatch.setattr(sys, "argv", ["railctl", *argv])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["code"] == "usage"


@pytest.mark.parametrize(
    "argv",
    [
        ["--format", "ndjson", "--json", "status"],
        ["status", "--format", "ndjson", "--json"],
        ["--format", "ndjson", "status", "--json"],
        ["--json", "status", "--format", "ndjson"],
    ],
    ids=["both-before", "both-after", "split-format-first", "split-json-first"],
)
def test_json_and_a_different_format_are_refused_wherever_the_verb_sits(
    fake_station, argv: list[str]
):
    # The last two orderings used to be accepted, each silently producing the
    # format named on ITS side of the verb: the conflict check saw one level's
    # copy at a time, so the two flags never met.
    result = runner.invoke(app, argv)
    assert result.exit_code != 0
    assert result.stdout == ""
    assert isinstance(result.exception, ValueError)
    assert "--json conflicts with --format=ndjson" in str(result.exception)


def test_json_alone_after_the_verb_is_still_accepted(fake_station):
    # The reason the check reads `fmt_flag` and not `fmt`: `base.fmt` is already
    # resolved to "human" by default, so comparing `--json` against it would
    # refuse this, the most ordinary invocation there is.
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["schema"] == "railctl/status/v1"


@pytest.mark.parametrize("variable", ["RAILCTL_ADDRESS", "RAILCTL_VERBOSE"])
def test_an_unusable_environment_value_still_answers_with_the_error_envelope(
    monkeypatch, capsys, variable: str
):
    # Click resolving the variable itself put the failure outside the envelope:
    # exit 2 with a rich usage box on stderr and no JSON at all, so a wrapper
    # calling `json.loads(stderr)` on a non-zero exit got a JSONDecodeError.
    # `build_settings` owns these variables, so the failure is a `usage` report
    # like every other bad value.
    import railctl.cli.main as cli_main

    monkeypatch.setenv(variable, "abc")
    monkeypatch.setattr(sys, "argv", ["railctl", "schema"])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["code"] == "usage"
    assert variable in payload["message"]


def test_the_format_variable_is_not_a_typed_flag_so_json_does_not_conflict_with_it(
    monkeypatch, fake_station
):
    # `--json` is documented to win over RAILCTL_FORMAT (`config.pick`: flag
    # beats environment). With Click reading the variable, it arrived in the
    # same slot as a typed `--format` and this invocation was refused as a
    # conflict with a flag that appears nowhere on the command line.
    monkeypatch.setenv("RAILCTL_FORMAT", "human")
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["schema"] == "railctl/status/v1"


def test_double_verbose_after_the_subcommand_reaches_the_traceback_switch(fake_station):
    # `-vv` after the verb has to do what `-vv` before it does. Merging the
    # per-command copy into `Settings` and stopping there left this flag
    # resolved and inert: no decoded diagnostics, no traceback.
    runner.invoke(app, ["status", "-vv"])
    assert os.environ["RAILCTL_VERBOSE"] == "2"
