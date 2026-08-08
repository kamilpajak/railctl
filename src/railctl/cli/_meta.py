# src/railctl/cli/_meta.py
"""The single source of every CLI parameter: `main.py`'s global options and
every command's own options and arguments are built from the rows below by
`typer_option`/`typer_argument`/`global_option`, and `railctl schema`'s
manifest is generated from the exact same rows. A flag name, default or help
string edited in only one of the two places is the drift
`tests/cli/test_schema.py` exists to catch.

Nothing in this module restates a fact that is already written down somewhere
else. The allowed `--format`/`--color` values are the tuples `deps.py`
validates against; an exit code's meaning is the exception class's own
docstring; a published error code, its exit code and its retryability are read
off `errors.py` and `result.py`. The one thing this module owns outright is the
meaning of exit codes 0/1/2 and of the two reserved codes, because those name
no class anywhere.

`COMMANDS` holds one row per command this commit registers, not the whole
design's nine-leaf tree - see this task's note in the plan for why a row with
no matching Typer command would be the wrong kind of "documented".

`--for SECONDS` on `drive` and `function` is deliberately absent from this
milestone's tree. It belongs with the operation-resource work planned for a
later plan, not with this table.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Literal

import typer

from railctl import errors
from railctl.cli.config import DEFAULT_TARGET
from railctl.cli.deps import (
    ALLOWED_COLORS,
    ALLOWED_FORMATS,
    DEFAULT_FORMAT,
    DEFAULT_VERBOSE,
    UsageProblem,
)
from railctl.cli.result import (
    ERROR_SCHEMA,
    INTERNAL_CODE,
    INTERNAL_EXIT_CODE,
    RESERVED_CODES,
    RETRYABLE_CODES,
    USAGE_CODE,
    USAGE_EXIT_CODE,
    error_code,
)

SCHEMA_SCHEMA: Final[str] = "railctl/schema/v1"

OptionType = Literal["string", "integer", "boolean", "enum"]


def _one_of(values: tuple[str, ...]) -> str:
    """ "a, b, or c" - the help-text form of an `enum` tuple, never a fourth copy of it.

    `--format`'s allowed values were spelled out as a literal in its help string, again in
    `help_epilog`'s OUTPUT section, and a third time in `ALLOWED_FORMATS` - the tuple the
    CLI actually validates against. A fourth format added to that tuple has to reach every
    place a caller reads the list, without an edit anywhere else.
    """
    return f"{', '.join(values[:-1])}, or {values[-1]}"


@dataclass(frozen=True, slots=True)
class Option:
    name: str
    help: str
    type: OptionType = "string"
    short: str | None = None
    #: The default this CLI documents, and the value the manifest publishes: what a caller
    #: gets when the flag, the environment and the config file are all silent. Never
    #: Typer's parse-time sentinel - publishing `null` for `--format` said this CLI has no
    #: default format, when it plainly has one, and `default` is a fact about the CLI while
    #: the sentinel is an implementation detail of how the default is applied.
    default: object = None
    #: True when `build_settings` applies `default` itself, at the bottom of `pick()`, so
    #: Typer must be handed `None` instead. `pick` has to tell "nothing was typed" from
    #: "typed the built-in default": handed the real default, Click would mark the flag
    #: given on every invocation and the CLI level would outrank the environment and the
    #: config file for a value nobody typed.
    late_default: bool = False
    enum: tuple[str, ...] | None = None
    required: bool = False
    env: str | None = None
    repeatable: bool = False


@dataclass(frozen=True, slots=True)
class Argument:
    name: str
    help: str
    type: OptionType = "string"
    required: bool = True
    enum: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class CommandMeta:
    path: str
    help: str
    schema: str
    mutates: bool
    exit_codes: tuple[int, ...]
    arguments: tuple[Argument, ...] = ()
    options: tuple[Option, ...] = ()
    confirms: bool = False


# Order is quoted from the design spec's global option table (L2). Each `default` is the
# value a caller actually gets when nothing is typed - read off the constant
# `build_settings` applies, never retyped - and `late_default` is what says Typer must be
# handed `None` so `pick()` can still tell "not typed" from "typed the default".
GLOBAL_OPTIONS: Final[tuple[Option, ...]] = (
    Option(
        name="--target",
        help="auto, serial:<path>, or z21:<host>:<port>",
        type="string",
        default=DEFAULT_TARGET,
        late_default=True,
        env="RAILCTL_TARGET",
    ),
    Option(
        name="--address",
        help="locomotive address, 1..9999",
        type="integer",
        short="-a",
        # No address is the real default, so the published value and Typer's sentinel are
        # the same `None` here - `late_default` would change nothing.
        default=None,
        env="RAILCTL_ADDRESS",
    ),
    Option(
        name="--format",
        help=_one_of(ALLOWED_FORMATS),
        type="enum",
        enum=ALLOWED_FORMATS,
        default=DEFAULT_FORMAT,
        late_default=True,
        env="RAILCTL_FORMAT",
    ),
    Option(name="--json", help="alias for --format=json", type="boolean", default=False),
    Option(
        name="--verbose",
        help="repeatable: -v decoded frames, -vv raw bytes",
        type="integer",
        short="-v",
        default=DEFAULT_VERBOSE,
        late_default=True,
        env="RAILCTL_VERBOSE",
        repeatable=True,
    ),
    # `--color`'s only environment input is NO_COLOR (design spec table), read
    # later at render time - `NO_COLOR`/`TERM=dumb` are a force-plain-text
    # override, not a value in the same precedence chain as the three above.
    # Like every other `env` in this table it is published metadata and nothing
    # more; `typer_option` below hands no environment variable to Click.
    Option(
        name="--color",
        help=_one_of(ALLOWED_COLORS),
        type="enum",
        enum=ALLOWED_COLORS,
        # Applied by Click, not by `pick()`: `--color` has no config-file level, so the
        # root parameter carries the real default and `check_choice` sees a valid value
        # even when the flag is absent.
        default="auto",
        env="NO_COLOR",
    ),
    Option(
        name="--yes",
        help="answer every confirmation yes",
        type="boolean",
        short="-y",
        default=False,
    ),
    Option(
        name="--non-interactive",
        help="never prompt, even on a real terminal",
        type="boolean",
        default=False,
    ),
)

_GLOBAL_BY_NAME: Final[dict[str, Option]] = {o.name: o for o in GLOBAL_OPTIONS}

BASE_EXIT_CODES: Final[tuple[int, ...]] = (0, 1, 2)

# Every code a command that opens a `Station` and goes through `Station.exchange()` can
# actually leave the process with: the base 0/1/2, transport 3, protocol 4, silence 5, the
# `61 82` refusal 6, out-of-scope 7 (a `z21:` target - one of the three `--target` forms this
# same table advertises), and `RailctlError`'s own 9. Two of these were missing while both
# were reachable, so `railctl status --target z21:...` exited 7 against a manifest that said
# it could not. `help_epilog` reads the two new lines off the exception docstrings on its own.
#
# This is a "can produce" set, not a "has been observed" one, and the two errors are not
# symmetric. A published code that never arrives costs a caller one unused branch. A code that
# arrives unpublished drops them into their unknown-exit-code arm on a failure this tool
# documents everywhere else. So when a code is arguable, publish it. Only 6 and 7 are driven by
# a test today (`tests/cli/test_schema.py`, the two reachability guards); 3, 4, 5 and 9 are
# reachable by reading `Station.exchange` and its callers but are not exercised end to end. Do
# NOT "tighten" this tuple to the observed four - that is the same defect the comment above
# describes, running the other way.
STATION_EXIT_CODES: Final[tuple[int, ...]] = (0, 1, 2, 3, 4, 5, 6, 7, 9)

_STATUS = CommandMeta(
    path="status",
    help="Command station status: raw byte and decoded bits",
    schema="railctl/status/v1",
    mutates=False,
    exit_codes=STATION_EXIT_CODES,
)
_VERSION = CommandMeta(
    path="version",
    help="XpressNet version and command station id",
    schema="railctl/version/v1",
    mutates=False,
    exit_codes=STATION_EXIT_CODES,
)
_SCHEMA = CommandMeta(
    path="schema",
    help="Machine-readable manifest of the command tree",
    schema=SCHEMA_SCHEMA,
    mutates=False,
    # Opens no station and reads no port, so the three base codes are the whole set.
    exit_codes=BASE_EXIT_CODES,
    arguments=(
        Argument(
            name="path",
            help="command path words, for example: status",
            type="string",
            required=False,
        ),
    ),
)

# Tree order per the design spec's L2 ASCII listing: status, version, ...,
# schema last. Each later task that adds a command rebuilds this literal in
# full, its own row inserted where the nine-path tree order puts it - never
# appended to the end - so `doctor`, added last (Task 12), still lands first.
#
# This tuple is the ONE place that order is decided. Typer lists commands in the order
# `register()` calls declare them, and `railctl --help` used to say version, status, schema
# against a manifest that said status, version, schema, with a comment right here claiming
# they matched. `tests/cli/test_schema.py` compares the Click tree against this tuple, so a
# command registered out of order fails rather than quietly giving an agent and an operator
# two different tables of contents.
COMMANDS: Final[tuple[CommandMeta, ...]] = (_STATUS, _VERSION, _SCHEMA)

_BY_PATH: Final[dict[str, CommandMeta]] = {c.path: c for c in COMMANDS}


def command_meta(path: str) -> CommandMeta:
    """The row for `path`, or a `UsageProblem` carrying something runnable.

    A bare `ValueError` published `"suggestions": []` and left the only recovery
    information in the prose of `message`, which an agent would have to parse back apart.
    The near misses stay in the message for a human; the array says what to RUN to get the
    real list, which is the one answer that is right whatever the mistyped path was.
    """
    try:
        return _BY_PATH[path]
    except KeyError:
        near = difflib.get_close_matches(path, _BY_PATH, n=3, cutoff=0.0)
        raise UsageProblem(
            f"no such command {path!r}; closest known paths: {', '.join(near)}",
            suggestions=[["railctl", _SCHEMA.path]],
        ) from None


def typer_option(option: Option) -> Any:
    """The one place a `typer.Option` is built, from one metadata row.

    No `callback=`. An `enum` row is published metadata that `deps.check_choice`
    enforces on the resolved value, NOT a Click-level check: a callback raising
    `typer.BadParameter` is handled by Click itself, which prints its own usage
    text and exits - so `railctl --color allways version` would stop publishing
    the `railctl/error/v1` envelope with `"code": "usage"` that Task 8 pinned,
    and would start taking the route issue #30 already tracks as a gap. Both
    positions of the flag are validated instead, in `build_settings` and
    `merge_settings`, which is what makes them fail identically.

    No `envvar=` either, for the same reason one layer down. `Option.env` is
    published metadata - the manifest names RAILCTL_TARGET, RAILCTL_ADDRESS,
    RAILCTL_FORMAT and RAILCTL_VERBOSE from these rows - and `build_settings`
    is the single place that READS those variables, at the environment level of
    `pick()`, below the CLI flag. Handing the same name to Click makes Click
    read and type-cast it first, before any railctl code runs, which costs two
    measured behaviours: `RAILCTL_ADDRESS=abc railctl schema` exits through
    Click's own rich usage box with no `railctl/error/v1` envelope on stderr at
    all, and `RAILCTL_FORMAT=human railctl --json status` is refused as a
    `--json`/`--format` conflict even though no `--format` was typed, because
    the environment value arrived in the slot a typed flag occupies.
    """
    names: list[str] = [option.name]
    if option.short is not None:
        names.append(option.short)
    return typer.Option(
        None if option.late_default else option.default,
        *names,
        help=option.help,
        count=option.repeatable,
    )


def typer_argument(argument: Argument) -> Any:
    """The one place a `typer.Argument` is built, from one metadata row."""
    return typer.Argument(... if argument.required else None, help=argument.help)


def _bare_default(option: Option) -> object:
    """The "nothing typed at the command level" default for `global_option`'s
    per-command copy - never the row's own real default, which belongs to the
    root callback alone."""
    if option.type == "boolean":
        return False
    if option.repeatable:
        return 0
    return None


def global_option(name: str) -> Any:
    """A per-command copy of a `GLOBAL_OPTIONS` row: same flags and help, but a
    bare "nothing typed here" default.

    Every registered command declares all eight of these because Click parses
    a Typer group's own options only *before* the subcommand name - without a
    per-command copy, `railctl doctor --address 3` is a usage error before
    `doctor` ever runs, even though the design's own examples put the flag
    after the verb. Only the default differs from the root copy: `merge_settings`
    layers this level over the already-resolved `Settings`, so a bare default is
    what tells "not typed after the verb" from "typed the same value again".
    Neither copy carries an `envvar` - see `typer_option`.
    """
    row = _GLOBAL_BY_NAME[name]
    return typer_option(replace(row, default=_bare_default(row), late_default=False))


_BASE_EXIT_MEANINGS: Final[dict[int, str]] = {
    0: "success",
    1: "unhandled internal error",
    2: "usage error - a bad flag, value, or missing argument",
}

# The two published codes that name no class in the exception tree, so these two sentences
# are the only error meanings this module owns. `result.RESERVED_CODES` is the set they are
# looked up from, so a third reserved code raises a KeyError here rather than going missing
# from the manifest.
_RESERVED_EXIT_CODES: Final[dict[str, int]] = {
    USAGE_CODE: USAGE_EXIT_CODE,
    INTERNAL_CODE: INTERNAL_EXIT_CODE,
}
_RESERVED_SUMMARIES: Final[dict[str, str]] = {
    USAGE_CODE: "The invocation was malformed. Fix the command line; do not retry.",
    INTERNAL_CODE: "A bug in railctl itself, never a domain answer and never the caller's fault.",
}


def _first_paragraph(text: str | None) -> str:
    """The opening paragraph of a docstring, rewrapped onto one line.

    The first PHYSICAL line is what this used to take, which made the summary depend on
    where the author's editor wrapped: `PortBusy`'s docstring breaks after a comma, so it
    published "The port exists but could not be opened - another process holds it," - a
    fragment, in the field a caller reads to find out what happened.

    A paragraph rather than a first sentence, deliberately. Cutting at the first full stop
    would publish "No reply arrived within the budget." for `LinkTimeout` and drop
    "Silence - never a negative answer.", which is the one clause this whole project turns
    on, and it would reduce `usage` to "The invocation was malformed." without the "do not
    retry" a caller is meant to act on. Everything after the first blank line is the
    reasoning behind the class, which belongs in the source and not in a manifest.

    Total by construction: a class with no docstring publishes an empty summary rather than
    taking the whole manifest down with an IndexError.
    """
    paragraph = (text or "").strip().split("\n\n", 1)[0]
    return " ".join(paragraph.split())


def _error_classes(root: type[errors.RailctlError]) -> set[type[errors.RailctlError]]:
    """Every exception class railctl defines, and ONLY those.

    The `__module__` filter is load-bearing, not tidiness, and it is the same one
    `tests/cli/test_errors.py::_tree` explains at length: CPython registers a class with its
    bases BEFORE running `__init_subclass__`, so a subclass that forgets its `code` stays in
    `__subclasses__()` as a zombie - defined enough to be walked, never finished enough to
    have a code to publish.

    Walked rather than listed so that a 32nd exception class appears in the manifest with no
    edit here. A hand-written table would be the second source of truth this whole module
    exists to prevent one layer up.
    """
    found = {root} if root.__module__ == "railctl.errors" else set()
    for sub in root.__subclasses__():
        found |= _error_classes(sub)
    return found


def _class_error_row(klass: type[errors.RailctlError]) -> dict[str, object]:
    # `klass.__new__(klass)` and never `klass(...)`: several of these take required keyword
    # arguments, and nothing here needs an initialised instance - `error_code` and
    # `exit_code_for` both answer off the class. Going through those two functions rather
    # than reading `klass.code` and `EXIT_CODES[klass]` directly is what keeps the manifest
    # answering exactly what the running CLI answers, including `StationError`'s inherited 9.
    probe = klass.__new__(klass)
    code = error_code(probe)
    return {
        "code": code,
        "exit_code": errors.exit_code_for(probe),
        "retryable": code in RETRYABLE_CODES,
        "summary": _first_paragraph(klass.__doc__),
    }


def _reserved_error_row(code: str) -> dict[str, object]:
    return {
        "code": code,
        "exit_code": _RESERVED_EXIT_CODES[code],
        "retryable": code in RETRYABLE_CODES,
        "summary": _RESERVED_SUMMARIES[code],
    }


def error_codes() -> list[dict[str, object]]:
    """Every machine-readable `code` string `railctl/error/v1` can carry, sorted by code.

    This is the half of the error contract the exit code cannot carry: the design's own rule
    is that domain detail belongs in `error.code` and not in the process status, so a
    manifest that published only `exit_codes` would document the coarse channel and leave a
    script to discover the useful one by triggering every failure in turn (issue #28).
    """
    rows = [_class_error_row(klass) for klass in _error_classes(errors.RailctlError)]
    rows += [_reserved_error_row(code) for code in RESERVED_CODES]
    return sorted(rows, key=lambda row: str(row["code"]))


def _output_lines(schema: str) -> list[str]:
    # `ALLOWED_FORMATS`, not the words "human, json, ndjson": the same list is already the
    # `enum` of `--format` and the tuple `deps.check_choice` validates against, and a fourth
    # format must not be able to reach the validator without reaching this line.
    return ["OUTPUT", f"  schema: {schema}", f"  formats: {', '.join(ALLOWED_FORMATS)}", ""]


def _exit_code_lines(codes: Sequence[int]) -> list[str]:
    by_code = {code: klass for klass, code in errors.EXIT_CODES.items()}
    lines = ["EXIT CODES"]
    for code in codes:
        meaning = _BASE_EXIT_MEANINGS.get(code)
        if meaning is None:
            meaning = _first_paragraph(by_code[code].__doc__)
        lines.append(f"  {code}: {meaning}")
    return [*lines, ""]


def _example(meta: CommandMeta) -> str:
    required_args = " ".join(f"<{a.name}>" for a in meta.arguments if a.required)
    return " ".join(w for w in (f"railctl {meta.path}", required_args, "--format json") if w)


def help_epilog(meta: CommandMeta) -> str:
    """The fixed `OUTPUT` / `EXIT CODES` / `EXAMPLES` sections appended as
    this command's Typer `epilog`. Click supplies `Usage:` and `Options:` on
    its own.

    Built from `meta`, `errors.EXIT_CODES` and `ALLOWED_FORMATS` alone, never a clock or a
    terminal size, so this STRING is byte-identical between two runs of the same command.
    What Click then does with it is not: `app`'s
    `context_settings={"max_content_width": 100}` (Task 9) caps the help width at 100
    columns, it does not fix it, so a 40-column terminal still rewraps every line of the
    text below. Determinism is a property of what this function returns, not of what the
    terminal shows.
    """
    return "\n".join(
        [
            *_output_lines(meta.schema),
            *_exit_code_lines(meta.exit_codes),
            "EXAMPLES",
            f"  {_example(meta)}",
        ]
    )


def root_epilog() -> str:
    """The same three headings for `railctl --help`, which had none of them.

    The project's rule is fixed headings at every level, and the root is the page an
    operator reaches first. Every fact here is read off `COMMANDS`: the exit codes are the
    union of what the registered commands publish, and there is one example per command, in
    tree order. The root emits no result envelope of its own - a failure while resolving a
    global option is the error envelope, and a success always belongs to some command - so
    the OUTPUT section names `ERROR_SCHEMA` and points at the manifest for the rest.
    """
    codes = sorted({code for meta in COMMANDS for code in meta.exit_codes})
    schema_line = f"{ERROR_SCHEMA} on failure; each command names its own, see railctl schema"
    return "\n".join(
        [
            *_output_lines(schema_line),
            *_exit_code_lines(codes),
            "EXAMPLES",
            *(f"  {_example(meta)}" for meta in COMMANDS),
        ]
    )


def _option_dict(option: Option) -> dict[str, object]:
    return {
        "name": option.name,
        "short": option.short,
        "help": option.help,
        "type": option.type,
        "default": option.default,
        "enum": list(option.enum) if option.enum is not None else None,
        "required": option.required,
        "env": option.env,
        "repeatable": option.repeatable,
    }


def _argument_dict(argument: Argument) -> dict[str, object]:
    return {
        "name": argument.name,
        "help": argument.help,
        "type": argument.type,
        "enum": list(argument.enum) if argument.enum is not None else None,
        "required": argument.required,
    }


def _command_dict(meta: CommandMeta) -> dict[str, object]:
    return {
        "path": meta.path,
        "help": meta.help,
        "schema": meta.schema,
        "mutates": meta.mutates,
        "confirms": meta.confirms,
        "exit_codes": list(meta.exit_codes),
        "arguments": [_argument_dict(a) for a in meta.arguments],
        "options": [_option_dict(o) for o in meta.options],
    }


def manifest(paths: Sequence[str] | None = None) -> dict[str, object]:
    """The `railctl/schema/v1` payload: the whole tree, or one command.

    `paths` is `None`/empty for the whole tree; otherwise its words are
    joined with a single space and looked up as one `CommandMeta.path` -
    `command_meta`'s `ValueError` on a miss is left to propagate, uncaught,
    so `run()` (Task 8/9) turns it into the standard exit-2 error envelope.

    `error_codes` rides along in both shapes. A `code` is not a property of one
    command, and a caller who asked about a single command must not have to
    fetch the whole tree to learn the strings it may have to branch on.
    """
    payload: dict[str, object] = {
        "schema": SCHEMA_SCHEMA,
        "global_options": [_option_dict(o) for o in GLOBAL_OPTIONS],
        "error_codes": error_codes(),
    }
    if not paths:
        payload["commands"] = [_command_dict(c) for c in COMMANDS]
        return payload
    payload["command"] = _command_dict(command_meta(" ".join(paths)))
    return payload
