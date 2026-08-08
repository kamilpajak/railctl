# src/railctl/cli/commands/schema.py
"""`railctl schema`: the manifest generated from `railctl.cli._meta`'s table.

Opens no `Station` and touches no port - the one command an agent can use to
discover the whole CLI with the layout unplugged. Its only failure mode is an
unresolved command path, and that is a plain `ValueError` from `_meta.manifest`
left to propagate: `run()` (Task 8/9) already turns any `ValueError` into the
standard `railctl/error/v1` exit-2 envelope, so this module raises nothing of
its own and calls no rendering function directly.

`--format json` is what this command is for; the human rendering prints one
line per command rather than the whole payload, because a manifest read by a
person is a table of contents and a manifest read by a script is the contents.

Declares all eight global options a second time (`global_option`, `_meta.py`)
because Click parses a Typer group's own options only *before* the subcommand
name - without this, `railctl schema --format json` (flag after the verb, the
form every design-spec example uses) is a usage error. They are otherwise
inert here: `schema` never opens a `Station`, so `--address`/`--target` do
nothing beyond what `merged_output` lets `--format`/`--color` do to this
command's own rendering.
"""

from __future__ import annotations

from collections.abc import Sequence

import typer

from railctl.cli._errors import run
from railctl.cli._meta import (
    SCHEMA_SCHEMA,
    command_meta,
    global_option,
    help_epilog,
    manifest,
    typer_argument,
)
from railctl.cli.deps import merged_output
from railctl.cli.result import CommandResult

_META = command_meta("schema")

# Built once, at import time, into names the signature below references. A call to
# `typer_argument(...)`/`global_option(...)` written directly as a parameter default is a
# function call in a default argument - Ruff's B008 is in this project's `select` list, and
# its built-in allowlist covers a literal `typer.Option(...)` call, not a wrapper around one.
_PATH_ARGUMENT = typer_argument(_META.arguments[0])
_TARGET = global_option("--target")
_ADDRESS = global_option("--address")
_FORMAT = global_option("--format")
_JSON = global_option("--json")
_VERBOSE = global_option("--verbose")
_COLOR = global_option("--color")
_YES = global_option("--yes")
_NON_INTERACTIVE = global_option("--non-interactive")


def build_schema(paths: Sequence[str] | None) -> CommandResult:
    data = manifest(paths)
    outcome = CommandResult(schema=SCHEMA_SCHEMA, command="schema", result=data)
    entries = data["commands"] if "commands" in data else [data["command"]]
    for entry in entries:  # type: ignore[union-attr]
        outcome.say(f"{entry['path']}: {entry['help']}")
    outcome.say(f"error codes: {len(data['error_codes'])}")  # type: ignore[arg-type]
    return outcome


def register(app: typer.Typer) -> None:
    @app.command("schema", help=_META.help, epilog=help_epilog(_META))
    def schema_command(
        ctx: typer.Context,
        path: list[str] = _PATH_ARGUMENT,
        target: str | None = _TARGET,
        address: int | None = _ADDRESS,
        format_: str | None = _FORMAT,
        json_flag: bool = _JSON,
        verbose: int = _VERBOSE,
        color: str | None = _COLOR,
        yes: bool = _YES,
        non_interactive: bool = _NON_INTERACTIVE,
    ) -> None:
        cli_ctx = ctx.obj
        _, output = merged_output(
            cli_ctx.settings,
            cli_ctx.output,
            target=target,
            address=address,
            fmt=format_,
            json_flag=json_flag,
            verbose=verbose,
            color=color,
            yes=yes,
            non_interactive=non_interactive,
        )

        def work() -> CommandResult:
            return build_schema(path or None)

        run("schema", output, work)
