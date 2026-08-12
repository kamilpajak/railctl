"""Pins the command metadata table and `railctl schema`.

`COMMANDS` holds one row per command this commit actually registers - see the
note at the top of this task in the plan. `status`, `version`, `power`, `stop`,
`drive`, `function` and `schema` are real; `doctor` and `monitor` extend this
same tuple in their own later commits.
"""

from __future__ import annotations

import dataclasses

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
    root_epilog,
    typer_argument,
    typer_option,
)
from railctl.cli.result import PARTIAL_EXIT_CODE, RESERVED_CODES, RETRYABLE_CODES
from railctl.errors import EXIT_CODES

# `PARTIAL_EXIT_CODE` names no exception class - a partial run is a RESULT, not an
# error - so it reaches this set from `result.py` rather than from the exit-code map.
KNOWN_CODES = set(BASE_EXIT_CODES) | set(EXIT_CODES.values()) | {PARTIAL_EXIT_CODE}


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
    # `default` is what a caller GETS when nothing is typed, which is what the manifest
    # publishes; `late_default` is what says Typer must still be handed `None` so
    # build_settings can tell "not given" from "given the built-in default". Publishing the
    # sentinel instead told a consumer that three options with a default have none.
    assert by_name["--target"].default == "auto"
    assert by_name["--format"].default == "human"
    assert by_name["--verbose"].default == 0
    assert by_name["--color"].default == "auto"
    for name in ("--target", "--format", "--verbose"):
        assert by_name[name].late_default is True, name
        assert typer_option(by_name[name]).default is None, name
    # `--color` has no config-file level and no `pick()` call, so Click carries its real
    # default and `check_choice` sees a valid value even when the flag is absent.
    assert by_name["--color"].late_default is False
    assert typer_option(by_name["--color"]).default == "auto"


@pytest.mark.parametrize("option", GLOBAL_OPTIONS, ids=lambda o: o.name)
def test_late_default_decides_what_click_is_handed_for_every_option(option: Option):
    """The rule behind `late_default`, checked on every row rather than three by name.

    `default` and `late_default` are two fields describing one decision, so they can drift:
    a row could publish `default="auto"` while handing Click `"auto"` as well, and `pick()`
    would then never see the difference between "not typed" and "typed the default" - the
    environment and config levels would be dead for that option and nothing would say so.

    The test above names `--target`, `--format` and `--verbose` individually, which is right
    for pinning their actual values but cannot cover a ninth option added by a later task.
    This is the invariant itself: `late_default` true means Click is handed `None`, false
    means Click is handed the row's real default. There is no third possibility.
    """
    handed_to_click = typer_option(option).default
    if option.late_default:
        assert handed_to_click is None, option.name
    else:
        assert handed_to_click == option.default, option.name


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


def test_every_command_publishes_its_exit_codes_in_order():
    """`--help` prints this tuple in the order it is written, and `power` appended the
    partial code 8 after the base set's 9, so its EXIT CODES section read
    0 1 2 3 4 5 6 7 9 8 20. Deterministic help in a deterministic order is the rule the
    whole help page is built on."""
    for meta in COMMANDS:
        assert list(meta.exit_codes) == sorted(meta.exit_codes), meta.path


def test_the_enum_rows_are_the_same_tuples_deps_validates_against():
    # The manifest's `enum` list and the check that rejects a bad value must be
    # one tuple, not two that agree today. `_meta` imports both from `deps`, so
    # a fourth format added there appears in the manifest with no edit here.
    from railctl.cli.deps import ALLOWED_COLORS, ALLOWED_FORMATS

    by_name = {o.name: o for o in GLOBAL_OPTIONS}
    assert by_name["--format"].enum is ALLOWED_FORMATS
    assert by_name["--color"].enum is ALLOWED_COLORS


def test_the_manifest_publishes_resume_as_a_power_state():
    """The third state has to be discoverable from the manifest alone.

    `power on` comes up HELD (measured 2026-08-09, docs/probe-results.md), so `power
    resume` is the only way to release the layout. An agent that reads `["on", "off"]`
    here has no way to find it except by guessing a word and reading the refusal - and
    the help text is built from the same tuple, so both say all three or neither does.
    """
    state = manifest(["power"])["command"]["arguments"][0]  # type: ignore[index]
    assert state["enum"] == ["on", "off", "resume"]
    assert state["type"] == "enum"
    assert "resume" in command_meta("power").arguments[0].help


def test_every_row_with_a_fixed_set_of_values_publishes_it_as_an_enum():
    """`type` and `enum` are two fields describing one fact, so they can drift.

    `FUNCTION_STATE_ARG` published `type: "string", enum: null` while `parse_state`
    accepted exactly on, off and toggle - an agent reading the manifest was told to
    discover the list by trying one and reading the error.
    """
    rows = [
        *GLOBAL_OPTIONS,
        *(row for meta in COMMANDS for row in (*meta.arguments, *meta.options)),
    ]
    for row in rows:
        assert (row.type == "enum") == (row.enum is not None), row.name


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
    assert [c["path"] for c in payload["commands"]] == [
        "doctor",
        "status",
        "version",
        "power",
        "stop",
        "drive",
        "function",
        "monitor",
        "cv read",
        "cv write",
        "backup",
        "schema",
    ]
    assert {o["name"] for o in payload["global_options"]} == {o.name for o in GLOBAL_OPTIONS}


def test_manifest_for_a_single_path_matches_the_tree_entry_shape():
    tree = manifest(None)
    single = manifest(["status"])
    tree_entry = next(c for c in tree["commands"] if c["path"] == "status")
    assert single["command"] == tree_entry


# The one field of a metadata row the manifest deliberately does not publish: it says
# whether Typer is handed `None` so `pick()` can tell "not typed" from "typed the default",
# which is how this CLI applies a default, not a fact about the CLI a caller can act on.
# Every other field must reach the manifest, and this set is where an exemption has to be
# argued for in writing rather than going unnoticed.
UNPUBLISHED_ROW_FIELDS = frozenset({"late_default"})


def _as_published(row: object) -> dict[str, object]:
    """Every field of an `Option`/`Argument`/`CommandMeta` row, in published form.

    `dataclasses.asdict`, not a hand-written expectation dict: restating the nine option
    fields here would be a third copy of the same table, and a copy written by the same
    hand that wrote `_option_dict` agrees with it for the same reason it is wrong. The
    only transformation the manifest applies is tuple -> list, so that is the only thing
    this function says. A field added to a row and forgotten in `_option_dict` fails
    here, and so does a published value that does not equal its source.
    """
    published = dataclasses.asdict(row)  # type: ignore[call-overload]
    for field in UNPUBLISHED_ROW_FIELDS:
        published.pop(field, None)
    for key, value in published.items():
        if isinstance(value, tuple):
            published[key] = list(value)
    if "arguments" in published:
        published["arguments"] = [_as_published(a) for a in row.arguments]  # type: ignore[attr-defined]
        published["options"] = [_as_published(o) for o in row.options]  # type: ignore[attr-defined]
    return published


@pytest.mark.parametrize("meta", COMMANDS, ids=lambda m: m.path)
def test_every_published_command_field_equals_the_row_it_came_from(meta: CommandMeta):
    # `path` was the only field any test looked at, so `"mutates": meta.mutates` could be
    # replaced by `"mutates": True` for every command in the tree and the suite stayed
    # green - and `mutates` is the field an agent reads to decide whether a command is
    # safe to run unattended.
    entry = next(c for c in manifest(None)["commands"] if c["path"] == meta.path)
    assert entry == _as_published(meta)


@pytest.mark.parametrize("option", GLOBAL_OPTIONS, ids=lambda o: o.name)
def test_every_published_global_option_field_equals_the_row_it_came_from(option: Option):
    entry = next(e for e in manifest(None)["global_options"] if e["name"] == option.name)
    assert entry == _as_published(option)


def test_the_single_command_shape_publishes_the_same_fields_as_the_tree():
    for meta in COMMANDS:
        assert manifest([meta.path])["command"] == _as_published(meta)


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


def test_a_command_explains_its_own_reason_for_an_exit_code_not_the_classs():
    """Exit 12 out of `drive` is the pre-flight finding a service-mode session.

    `StationBusyError`'s docstring opens "The station reported 61 1F: a programming
    operation is already running" - true of the class, and not why a throttle command
    ever exits 12: neither `drive` nor `function` sends anything that could provoke that
    reply. The class summary stays right where it describes the class, in the error-code
    table.
    """
    for path in ("drive", "function"):
        section = help_epilog(command_meta(path))
        assert "12: a service-mode programming session is active" in section, path
        assert "61 1F" not in section, path
    # Unchanged where no override applies: the class docstring is the right answer for
    # every other code, and this must not have become a blanket rewrite.
    assert "5: No reply arrived within the budget" in help_epilog(command_meta("drive"))
    # And the error-code table still publishes the class's own summary.
    rows = {row["code"]: row for row in error_codes()}
    assert rows["station_busy"]["summary"].startswith("The station reported 61 1F")


def test_every_place_that_lists_the_allowed_values_lists_all_of_them():
    # Four copies of one list: the tuple `deps` validates against, the manifest's `enum`,
    # the flag's help string, and the OUTPUT section of every epilog. A value added to the
    # tuple has to reach all of them, so this asserts membership rather than a spelling -
    # a literal that is merely correct today passes, a literal left behind does not.
    from railctl.cli.deps import ALLOWED_COLORS, ALLOWED_FORMATS

    by_name = {o.name: o for o in GLOBAL_OPTIONS}
    for value in ALLOWED_FORMATS:
        assert value in by_name["--format"].help, value
        assert value in help_epilog(command_meta("status")), value
        assert value in root_epilog(), value
    for value in ALLOWED_COLORS:
        assert value in by_name["--color"].help, value


def test_a_summary_is_a_whole_thought_not_the_first_physical_line():
    # PortBusy's docstring wraps after a comma, and the first-line rule published the
    # fragment "...another process holds it," as the summary a caller reads.
    rows = {row["code"]: row for row in error_codes()}
    assert rows["port_busy"]["summary"] == (
        "The port exists but could not be opened - another process holds it, or permission "
        "was denied. The message carries the OS strerror either way."
    )
    # And the clause this whole project turns on survives, which a first-SENTENCE rule
    # would have cut off after "budget.".
    assert rows["link_timeout"]["summary"].endswith("Silence - never a negative answer.")


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
    assert len(published) == 37


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
    assert [c["path"] for c in result.result["commands"]] == [
        "doctor",
        "status",
        "version",
        "power",
        "stop",
        "drive",
        "function",
        "monitor",
        "cv read",
        "cv write",
        "backup",
        "schema",
    ]


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

from railctl.cli._meta import _COMMAND_EXIT_MEANINGS, CommandMeta  # noqa: E402
from railctl.cli.main import app  # noqa: E402
from railctl.errors import (  # noqa: E402
    LinkTimeout,
    TrackPowerError,
    UnsupportedCommandError,
)
from railctl.station import (  # noqa: E402
    Capabilities,
    Check,
    CvEncoding,
    CvReadOutcome,
    CvResult,
    DoctorReport,
    LayoutState,
    ProgMode,
    Station,
)
from railctl.xbus.replies import LocoInfo, StationStatus, StationVersion  # noqa: E402
from railctl.xbus.speed import Direction  # noqa: E402

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


_STANDING_LOCO = LocoInfo(
    raw_ident=0b10000100,
    raw_speed=0x80,
    speed_steps=128,
    in_use_by_other=False,
    function_bits=(False,) * 13,
    speed=0,
    direction=Direction.FORWARD,
    emergency_stopped=False,
)


class _FakeStatusStation:
    """Bare stand-in for the invocation-order tests below.

    `status` is the pinned example (not `schema`), because it was the first
    command in this file that actually opens a `Station` - proving the
    --format-position parity on a command that never touches a port would not
    catch a per-command global-option block that forgot to route through the
    real `open_station`/`run()` plumbing.

    The mutating half answers every throttle call and records nothing: what
    this file checks is the CLI contract - exit codes, the envelope, the
    parameter surface - and `tests/cli/test_throttle.py` owns the question of
    which facade method each command calls, in what order, with what arguments.
    `raw_status` is settable so a test can drive a pre-flight refusal without a
    second fake class; it defaults to a healthy powered track.
    """

    identity = "serial:7010A0001194:3"

    # Service proven and POM a measured no, so the cv commands' AUTO resolves
    # to SERVICE and no invocation in this file needs an address for them.
    capabilities = Capabilities(link_identity=identity, pom_read=False, service_direct_cv=True)

    def __init__(self, raw_status: int = 0x00) -> None:
        self.raw_status = raw_status

    def status(self) -> StationStatus:
        return StationStatus.from_raw(self.raw_status)

    def cv_read(self, cv, *, address=None, mode=ProgMode.SERVICE, page=None):
        # `backup` reads the CV31/CV32 selectors and CV29 as singletons. The
        # selectors answer 0 - the default page - so the generic drives here
        # never hit backup's exit-17 page refusal; everything else answers the
        # same 145 the batch below does.
        return CvResult(
            cv=cv,
            value=0 if cv in (31, 32) else 145,
            mode=ProgMode.SERVICE,
            encoding=CvEncoding.SERVICE_DIRECT,
            operation="read",
            verified=None,
            elapsed=0.01,
        )

    def cv_read_many(self, specs, *, address=None, mode=ProgMode.SERVICE, on_progress=None):
        return [
            CvReadOutcome(
                spec=spec,
                result=CvResult(
                    cv=spec.cv,
                    value=145,
                    mode=ProgMode.SERVICE,
                    encoding=CvEncoding.SERVICE_DIRECT,
                    operation="read",
                    verified=None,
                    elapsed=0.01,
                ),
                error=None,
            )
            for spec in specs
        ]

    def cv_write(self, cv, value, *, address=None, mode=ProgMode.SERVICE, page=None, verify=True):
        return CvResult(
            cv=cv,
            value=value,
            mode=mode,
            encoding=CvEncoding.SERVICE_DIRECT,
            operation="write",
            verified=bool(verify),
            elapsed=0.01,
        )

    def version(self) -> StationVersion:
        return StationVersion(raw=0x40, station_id=0x12)

    def power_on(self) -> None:
        pass

    def power_off(self) -> None:
        pass

    def emergency_stop(self, address: int | None = None) -> None:
        pass

    def drive(self, address: int, speed: int, direction: Direction) -> None:
        pass

    def loco_info(self, address: int) -> LocoInfo:
        return _STANDING_LOCO

    def function_set(
        self, address: int, function: int, on: bool, *, force_group: bool = False
    ) -> None:
        pass

    def function_toggle(self, address: int, function: int, *, force_group: bool = False) -> bool:
        return True

    def probe(
        self,
        *,
        address: int | None = None,
        allow_power_on: bool = False,
        use_programming_track: bool = True,
    ) -> DoctorReport:
        """A report whose D0-D2 all passed, so `doctor` exits 0 like every other row
        this file drives. The capabilities carry `identity` above, which is what lets
        `save()` write into the tmp config directory `_isolated_environment` points at
        rather than refusing an unknown identity."""
        return DoctorReport(
            checks=(
                Check("D0", "link", "ok", "opened"),
                Check("D1", "link alive", "ok", "XpressNet 4.0"),
                Check("D2", "station status", "ok", "decoded"),
            ),
            capabilities=Capabilities.unknown(self.identity),
        )

    def events(self, *, interval: float = 0.25):
        """No broadcasts, and the generator ends rather than blocking. `monitor` is
        the only command that reads this, and a fake that never returns would hang the
        suite instead of failing it."""
        return iter(())

    def close(self) -> None:
        pass


@pytest.fixture
def fake_station(monkeypatch):
    monkeypatch.setattr(Station, "open", staticmethod(lambda *a, **k: _FakeStatusStation()))


# The required positional arguments each command needs before it can run at
# all, plus the `--address` the two throttle commands refuse to guess. Written
# once: three tests below drive every row of COMMANDS, and a fourth drives the
# two with a pre-flight. `drive` uses a POSITIVE speed on purpose - speed 0
# skips the pre-flight, so a 0 here would make the refusal drives prove nothing.
_EXTRA_ARGV: dict[str, list[str]] = {
    "power": ["off"],
    "drive": ["30", "--address", "3"],
    "function": ["f2", "on", "--address", "3"],
    # CV8 for the read (never in the confirmation set), CV3=20 for the write
    # (the design's own worked-session example, also unconfirmed). The fakes
    # below carry service-proven capabilities, so AUTO resolves to SERVICE and
    # neither invocation needs an address.
    "cv read": ["8"],
    "cv write": ["3", "20"],
    # `backup` always needs an address (the file is named after the
    # locomotive), and `--out -` keeps these generic drives from touching the
    # real ~/railctl-backups - the environment fixture isolates XDG, not HOME.
    "backup": ["--address", "3", "--out", "-"],
}


def _invocation(meta: CommandMeta) -> list[str]:
    return [*meta.path.split(), *_EXTRA_ARGV.get(meta.path, [])]


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


def _parsed_surface(command) -> dict[str, object]:
    """Every parameter Click will actually parse for `command`.

    Long names alone left two thirds of the surface compared against nothing: the
    manifest could advertise a positional that does not exist, omit one that does, or
    name the wrong short flag, and no test would say so. Positionals are a LIST, not a
    set - the order is what decides which word of `railctl cv read 8 3` is the CV.

    `param.param_type_name`, not `isinstance(param, click.Option)`: Typer 0.27 vendors
    Click as the private `typer._click`, and importing from a private module is how this
    file would break on a Typer upgrade that moves it.
    """
    long_names: set[str] = set()
    shorts: set[str] = set()
    helps: dict[str, str | None] = {}
    positionals: list[tuple[str, bool]] = []
    for param in command.params:
        if param.param_type_name == "option":
            for opt in param.opts:
                target = long_names if opt.startswith("--") else shorts
                target.add(opt)
                if opt.startswith("--"):
                    helps[opt] = param.help
        else:
            positionals.append((param.name, param.required))
    helps.pop("--help", None)
    return {
        "long": long_names - {"--help"},
        "short": shorts,
        "help": helps,
        "positional": positionals,
    }


def _declared_surface(meta: CommandMeta) -> dict[str, object]:
    """The same facts, read off the metadata row - the manifest's own claim.

    `help` is here because the two name sets cannot tell `stop`'s `--address`
    from the global one: both spell the flag the same way, so the union collapsed
    to a single entry and the command-scoped row went unchecked. `stop`'s row
    means "stop only this locomotive", the global one means "the locomotive every
    command acts on", and swapping them would leave the parameter surface test
    green while `stop --help` described the fallback that command exists to
    refuse. The command's own rows are layered LAST, which is exactly the
    precedence Click applies when a command redeclares a flag name.
    """
    options = (*GLOBAL_OPTIONS, *meta.options)
    return {
        "long": {o.name for o in options},
        "short": {o.short for o in options if o.short is not None},
        "help": {o.name: o.help for o in options},
        "positional": [(a.name, a.required) for a in meta.arguments],
    }


def test_every_registered_command_has_a_metadata_row_and_vice_versa():
    assert registered_paths(app) == {c.path for c in COMMANDS}


def _ordered_leaf_paths(group, prefix: str = "") -> list[str]:
    """Every leaf path in the order the help page lists it, groups expanded in
    place - `cv` contributes `cv read` then `cv write` where the group sits.

    Walked through `list_commands`, not the `commands` dict: Typer assembles
    `add_typer` groups after every plain command whatever order `register()`
    ran in, and `main._TreeOrderGroup.list_commands` is the override that puts
    the tree order back. The dict would test the defect away.
    """
    paths: list[str] = []
    for name in group.list_commands(None):
        cmd = group.commands[name]
        path = f"{prefix} {name}".strip()
        if hasattr(cmd, "commands"):
            paths.extend(_ordered_leaf_paths(cmd, path))
        else:
            paths.append(path)
    return paths


def test_the_help_lists_the_commands_in_the_order_the_manifest_does():
    # `railctl --help` said version, status, schema while the manifest said status,
    # version, schema - two tables of contents for one tool, with a comment beside
    # COMMANDS claiming they matched. Typer lists commands in registration order, so
    # this compares the Click tree itself, not the rendered page. Groups are
    # expanded in place: the `cv` group's leaves must sit where the manifest
    # puts them, between `monitor` and `schema`.
    click_app = typer.main.get_command(app)
    assert _ordered_leaf_paths(click_app) == [c.path for c in COMMANDS]


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
def test_the_whole_parameter_surface_matches_between_typer_and_metadata(meta: CommandMeta):
    # Every registered command declares its own metadata options PLUS the
    # eight global ones a second time (see the per-command global-option
    # note in `_meta.global_option`) - a command that forgot that block would
    # otherwise look complete against only its own (possibly empty) option
    # list, so the union with GLOBAL_OPTIONS is what makes an omission fail.
    assert _parsed_surface(_leaf_command(meta.path)) == _declared_surface(meta)


@pytest.mark.parametrize("meta", [c for c in COMMANDS if c.path != "schema"], ids=lambda m: m.path)
def test_the_observed_exit_code_is_one_the_command_publishes(meta: CommandMeta):
    """Drive the command for real and check the answer against its own row.

    `z21:` is one of the three `--target` forms the same manifest advertises, and no
    command serves it yet, so every station command reaches `UnsupportedFeatureError`
    and exit 7 - a code both station rows omitted while publishing 3, 4, 5 and 9. A
    subset check over hand-written literals cannot see that; only running the thing can.
    """
    result = runner.invoke(
        app,
        [*_invocation(meta), "--target", "z21:1.2.3.4", "--format", "json", "--non-interactive"],
    )
    # Not 0: this target is deliberately one nothing can serve, so a success here would
    # mean the invocation never reached the station and the check below proved nothing.
    assert result.exit_code != 0
    assert result.exit_code in meta.exit_codes
    # The process status and the envelope are one answer, never two.
    assert json.loads(result.stderr)["exit_code"] == result.exit_code


@pytest.mark.parametrize("meta", [c for c in COMMANDS if c.path != "schema"], ids=lambda m: m.path)
def test_a_station_that_refuses_exits_with_a_code_the_command_publishes(meta, monkeypatch):
    """The second reachable code, and the reason one drive is not enough.

    The `z21:` run above only ever produces exit 7, so it cannot see whether 6 is published.
    Dropping 6 from `STATION_EXIT_CODES` left the whole suite green until this test existed -
    the same silent-omission bug the tuple already had once, one code over.

    `UnsupportedCommandError` is what `Station.exchange` raises when the station answers
    `61 82`: a real refusal, not silence. Raising it from a patched station rather than
    scripting the bytes keeps this test about the CLI contract - that the exit code a command
    can produce is one its own manifest row advertises. `tests/station/` already owns the
    question of which wire reply produces this exception.
    """

    class Refusing:
        description = "fake"
        identity = "fake:refusing"
        # A real attribute, so `__getattr__` never turns the capabilities the
        # cv commands read into a refusal function; the refusal must come from
        # the first station CALL, exactly as it does for every other command.
        capabilities = Capabilities(
            link_identity="fake:refusing", pom_read=False, service_direct_cv=True
        )

        def __getattr__(self, name: str):
            def refuse(*_args, **_kwargs):
                raise UnsupportedCommandError("station answered 61 82")

            return refuse

        def close(self) -> None:
            pass

    monkeypatch.setattr(Station, "open", staticmethod(lambda *a, **k: Refusing()))
    result = runner.invoke(app, [*_invocation(meta), "--format", "json", "--non-interactive"])
    assert result.exit_code == 6, result.stderr
    assert result.exit_code in meta.exit_codes
    # The LAST line, not the whole stream: stderr carries logs, progress notices and
    # warnings as well as the error object, by design - `monitor` prints "monitoring
    # broadcasts" there before it reads anything. stdout is the stream that holds
    # exactly one value; stderr is the mixed one, and a consumer reads its last line.
    assert json.loads(result.stderr.strip().splitlines()[-1])["code"] == "unsupported_command"


PREFLIGHT_COMMANDS = [c for c in COMMANDS if c.path in ("drive", "function")]


@pytest.mark.parametrize("meta", PREFLIGHT_COMMANDS, ids=lambda m: m.path)
@pytest.mark.parametrize(
    ("raw_status", "expected_exit"),
    [(0x02, 20), (0x01, 20), (0x08, 12)],
    ids=["emergency-off", "emergency-stop", "service-mode"],
)
def test_a_preflight_refusal_exits_with_a_code_the_command_publishes(
    monkeypatch, meta: CommandMeta, raw_status: int, expected_exit: int
):
    """The third and fourth reachable codes, and the reason two drives are not enough.

    `drive SPEED>0` and `function` refuse on emergency off (20), emergency stop (20) and an
    active service-mode session (12). None of those can arrive through the `z21:` target or
    through a station that refuses everything, so without this drive both codes could be
    dropped from `THROTTLE_EXIT_CODES` with the whole suite still green - the same silent
    omission that once shipped for 6 and 7.

    Bits 0 and 1 are the MEASURED order on this hardware, the reverse of the Lenz spec
    (docs/probe-results.md), so 0x01 is emergency stop and 0x02 is emergency off. This is a
    check on our own refusal paths and their exit codes; it is not a measurement of the
    station, and no locomotive was watched not moving.
    """
    monkeypatch.setattr(
        Station, "open", staticmethod(lambda *a, **k: _FakeStatusStation(raw_status))
    )
    result = runner.invoke(app, [*_invocation(meta), "--format", "json", "--non-interactive"])
    assert result.exit_code == expected_exit, result.stderr
    assert result.exit_code in meta.exit_codes
    assert json.loads(result.stderr)["exit_code"] == result.exit_code


def test_the_status_that_refuses_every_other_speed_does_not_refuse_the_stop(monkeypatch):
    """`drive 0` on the same station that produced exit 20 above.

    Named for what it covers, not for what one would like to be true: this drives ONE
    station state, the emergency-off status, through the pre-flight guard. It said "never
    refused" and could not see the hole that actually existed - a `loco_info` reply the
    station refused aborted the same command before `station.drive` was reached, without
    the status ever being consulted. `tests/cli/test_throttle.py` owns that half now.
    """
    monkeypatch.setattr(Station, "open", staticmethod(lambda *a, **k: _FakeStatusStation(0x02)))
    result = runner.invoke(
        app, ["drive", "0", "--address", "3", "--format", "json", "--non-interactive"]
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["result"]["speed"] == 0


def test_power_off_reaches_the_track_power_exit_code_it_publishes(monkeypatch):
    """`POWER_EXIT_CODES` adds 20 and no test drove `power` to it.

    `Station._settle_power` raises `TrackPowerError` when the station still disagrees
    after the settle pause, and the reachability rule the two throttle drives already
    follow says a published refusal code needs a run, not a reading of the source.

    `off` is the state that still ends in a plain 20. For `on` and `resume` the same
    exception now arrives after a telegram that may have energised or released the
    layout, so it is reported as a partial - see the test below.
    """

    class _WillNotSettle(_FakeStatusStation):
        def power_off(self) -> None:
            raise TrackPowerError("commanded track power off but the station still reports on")

    monkeypatch.setattr(Station, "open", staticmethod(lambda *a, **k: _WillNotSettle()))
    result = runner.invoke(
        app, ["power", "off", "--address", "3", "--format", "json", "--non-interactive"]
    )
    assert result.exit_code == 20, result.stderr
    assert result.exit_code in command_meta("power").exit_codes
    assert json.loads(result.stderr)["code"] == "track_power"


def test_power_resume_on_a_dead_track_reaches_the_same_published_code(monkeypatch):
    """The second path to 20, and the one that matters.

    `power resume` sends `21 81`, which is the telegram that ENERGISES a dead track and
    clears both emergency bits at once. Run it from `0x06` or `0x07` - `0x07` is what
    `power on` followed by `power off` leaves - and it produced a live layout with
    nothing holding it, which is the runaway of run 1 reached through the CLI
    (docs/probe-results.md, "`power on`'s stop-all was in the wrong order").

    It refuses instead, before any telegram, with a condition a caller can branch on and
    a suggestion naming the command that energises AND holds.
    """
    monkeypatch.setattr(Station, "open", staticmethod(lambda *a, **k: _FakeStatusStation(0x07)))
    result = runner.invoke(
        app, ["power", "resume", "--address", "3", "--format", "json", "--non-interactive"]
    )
    assert result.exit_code == 20, result.stderr
    assert result.exit_code in command_meta("power").exit_codes
    payload = json.loads(result.stderr)
    assert payload["code"] == "track_power"
    assert payload["details"]["condition"] == "track_dead"
    assert payload["suggestions"] == [["railctl", "power", "on"]]


@pytest.mark.parametrize("state", ["on", "resume"])
def test_a_settle_failure_in_the_two_energising_states_is_a_partial(monkeypatch, state):
    """`Station.power_on()` writes `21 81` and only then verifies, so the settle failure
    it raises arrives with the telegram already sent. Both states report that as a
    partial naming the step, never as a bare refusal that reads as "nothing happened"."""

    class _WillNotSettle(_FakeStatusStation):
        def power_on(self) -> None:
            raise TrackPowerError("commanded track power on but the station still reports off")

    monkeypatch.setattr(Station, "open", staticmethod(lambda *a, **k: _WillNotSettle(0x01)))
    result = runner.invoke(
        app, ["power", state, "--address", "3", "--format", "json", "--non-interactive"]
    )
    assert result.exit_code == PARTIAL_EXIT_CODE, result.stderr
    payload = json.loads(result.stdout)
    assert payload["result"]["failed_step"] == "power_on"
    assert payload["warnings"][0]["details"]["error_code"] == "track_power"


def test_power_on_reaches_the_partial_exit_code_it_publishes(monkeypatch):
    """`POWER_EXIT_CODES` publishes 8, so something has to be able to produce it.

    A published code nobody drives is the omission the two tests above were written for,
    running the other way: the tuple says a caller may see 8, and only a run says they can.
    """

    class _DiesAfterPowerOn(_FakeStatusStation):
        def status(self) -> StationStatus:
            if self.powered_on:
                raise LinkTimeout("no status reply after track power on")
            return super().status()

        powered_on = False

        def power_on(self) -> None:
            self.powered_on = True

    monkeypatch.setattr(Station, "open", staticmethod(lambda *a, **k: _DiesAfterPowerOn()))
    result = runner.invoke(
        app, ["power", "on", "--address", "3", "--format", "json", "--non-interactive"]
    )
    assert result.exit_code == PARTIAL_EXIT_CODE, result.stderr
    assert result.exit_code in command_meta("power").exit_codes
    payload = json.loads(result.stdout)
    assert payload["exit_code"] == result.exit_code
    assert payload["ok"] is False


def test_power_resume_reaches_the_partial_exit_code_too(monkeypatch):
    """The same published 8, from the state that releases the layout.

    `power resume` sends `21 81` and only then reads the status back. If that read
    fails, the hold is already gone - measured 2026-08-09, that is the moment stored
    speeds start locomotives - so the caller gets a partial result naming the step,
    not a bare error that reads as "nothing happened".
    """

    class _DiesAfterRelease(_FakeStatusStation):
        released = False

        def status(self) -> StationStatus:
            if self.released:
                raise LinkTimeout("no status reply after the release")
            return super().status()

        def power_on(self) -> None:
            self.released = True

    monkeypatch.setattr(Station, "open", staticmethod(lambda *a, **k: _DiesAfterRelease(0x01)))
    result = runner.invoke(
        app, ["power", "resume", "--address", "3", "--format", "json", "--non-interactive"]
    )
    assert result.exit_code == PARTIAL_EXIT_CODE, result.stderr
    assert result.exit_code in command_meta("power").exit_codes
    payload = json.loads(result.stdout)
    assert payload["result"]["state"] == "resume"
    assert payload["result"]["failed_step"] == "read_status"


def test_doctor_reaches_the_partial_exit_code_it_publishes(monkeypatch):
    """`DOCTOR_EXIT_CODES` publishes 8, so something has to be able to produce it.

    A `--power-on` run that energised the track and then read NO emergency stop back
    off the station is the measured runaway (docs/probe-results.md, runs 1 and 2): the
    probe itself succeeded, so this is a partial result carrying the hazard, not an
    error saying nothing happened.
    """

    class _Unheld(_FakeStatusStation):
        def probe(self, **kwargs) -> DoctorReport:
            report = super().probe(**kwargs)
            return DoctorReport(
                checks=report.checks,
                capabilities=report.capabilities,
                layout=LayoutState(energised=True, track_power=True, held=False),
            )

    monkeypatch.setattr(Station, "open", staticmethod(lambda *a, **k: _Unheld()))
    result = runner.invoke(app, ["doctor", "--power-on", "--format", "json", "--non-interactive"])
    assert result.exit_code == PARTIAL_EXIT_CODE, result.stderr
    assert result.exit_code in command_meta("doctor").exit_codes
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["warnings"][0]["name"] == "hold_not_confirmed"


def test_the_root_group_carries_the_global_options_and_no_positionals():
    root = typer.main.get_command(app)
    assert _parsed_surface(root) == {
        "long": {o.name for o in GLOBAL_OPTIONS},
        "short": {o.short for o in GLOBAL_OPTIONS if o.short is not None},
        "help": {o.name: o.help for o in GLOBAL_OPTIONS},
        # The root takes the verb and nothing else; a positional here would swallow
        # the subcommand name.
        "positional": [],
    }


def test_schema_json_prints_one_envelope_with_the_registered_paths_in_tree_order():
    result = runner.invoke(app, ["--format", "json", "schema"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)["result"]
    assert payload["schema"] == "railctl/schema/v1"
    assert [c["path"] for c in payload["commands"]] == [
        "doctor",
        "status",
        "version",
        "power",
        "stop",
        "drive",
        "function",
        "monitor",
        "cv read",
        "cv write",
        "backup",
        "schema",
    ]


def test_schema_for_a_not_yet_implemented_command_is_exit_2_with_near_misses():
    result = runner.invoke(app, ["--format", "json", "schema", "power", "on"])
    assert result.exit_code == 2
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["code"] == "usage"
    assert "power on" in payload["message"]
    # A near miss is named, so the operator sees what the tool does know. `power`
    # is a registered path and `power on` is not - the design's tree has no
    # sub-group under `power`, the state is a positional argument.
    assert "power" in payload["message"]
    # A runnable argv array, not recovery advice buried in prose: `UsageProblem` carries
    # it, `usage_report` publishes it, and a bare ValueError published `[]`.
    assert payload["suggestions"] == [["railctl", "schema"]]


def test_schema_for_a_single_command_matches_the_tree_entry_shape():
    tree = json.loads(runner.invoke(app, ["--format", "json", "schema"]).stdout)["result"]
    single = json.loads(runner.invoke(app, ["--format", "json", "schema", "status"]).stdout)
    entry = single["result"]["command"]
    tree_entry = next(c for c in tree["commands"] if c["path"] == "status")
    assert entry == tree_entry
    # `set(entry) == set(tree_entry)` used to sit here and could not fail - the line above
    # had already compared the values. What the single-command shape actually claims is
    # that it answers about ONE command while still carrying the whole error contract.
    assert "commands" not in single["result"]
    assert single["result"]["error_codes"] == tree["error_codes"]


@pytest.mark.parametrize("argv", [["--help"], ["schema", "--help"]], ids=["root", "leaf"])
def test_help_is_deterministic_offline_and_carries_the_fixed_headings(argv: list[str]):
    # The headings are required at EVERY level, and the root had none of them: no OUTPUT,
    # no EXIT CODES, no EXAMPLES on the page an operator reaches first.
    first = runner.invoke(app, argv)
    second = runner.invoke(app, argv)
    assert first.exit_code == 0
    assert first.stdout == second.stdout  # two consecutive runs, byte-identical
    assert all(heading in first.stdout for heading in ("OUTPUT", "EXIT CODES", "EXAMPLES"))


def test_the_root_help_answers_for_every_command_and_every_exit_code():
    epilog = root_epilog()
    for meta in COMMANDS:
        assert f"railctl {meta.path}" in epilog
        for code in meta.exit_codes:
            assert f"\n  {code}: " in epilog
    assert "railctl/error/v1" in epilog  # the one schema a root-level failure emits


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
def test_the_read_only_commands_mutate_nothing(path: str):
    assert command_meta(path).mutates is False
    assert command_meta(path).confirms is False


@pytest.mark.parametrize("path", ["power", "stop", "drive", "function"])
def test_every_throttle_command_is_published_as_mutating_and_never_confirming(path: str):
    """`mutates` is the field an agent reads to decide whether a command is safe to run
    unattended, and all four of these change the layout's state. `confirms` is false on
    purpose and is not an oversight: the design's L6 rule is that `power`, `drive`, `stop`
    and `function` are never confirmed, because a prompt on every throttle change trains an
    operator to type `-y` reflexively - which then answers yes to the `restore` and the
    `cv write` that genuinely need asking.
    """
    meta = command_meta(path)
    assert meta.mutates is True
    assert meta.confirms is False


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
    result = runner.invoke(app, [*_invocation(meta), "--format", "json"])
    assert result.exit_code == 0, result.stderr
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

    def fake_open(target, *, default_address, capabilities_path, timing, on_event=None):
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


@pytest.mark.parametrize(
    "argv",
    [["--format", "ndjson", "status"], ["status", "--format", "ndjson"]],
    ids=["before", "after"],
)
def test_a_root_format_with_no_json_flag_still_produces_that_format(fake_station, argv):
    # The other side of the union check: only a CONTRADICTION is refused. A format asked
    # for on one side of the verb, with nothing contradicting it on the other, must still
    # be the format that comes out - NDJSON, ending in the summary event a line reader
    # waits for.
    result = runner.invoke(app, argv)
    assert result.exit_code == 0
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[-1]["type"] == "summary"


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


@pytest.mark.parametrize("path", sorted(_COMMAND_EXIT_MEANINGS))
def test_a_command_specific_exit_meaning_reaches_that_command_s_help(path: str):
    """Every override in the table is printed, and printed for the right command.

    `_COMMAND_EXIT_MEANINGS` exists because the class docstring behind an exit code is
    not always what the code means for a particular command - `drive` exits 12 because a
    service-mode session is open, not because "a programming operation is already
    running". Nothing constrained the table: renaming a key left the whole suite green,
    so an override could be added, misspelled, and silently never used.
    """
    meta = command_meta(path)
    epilog = help_epilog(meta)
    for code, meaning in _COMMAND_EXIT_MEANINGS[path].items():
        assert code in meta.exit_codes, f"{path} overrides {code} but does not publish it"
        assert meaning in epilog, f"{path}'s override for {code} never reaches its help"
