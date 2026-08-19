# src/railctl/cli/_parse_context.py
"""Every level of the command tree answers a parse failure with its OWN context.

`parse_failure_report` reads the failing level off `exc.ctx` - `details["command"]` and the
one `--help` argv array in `suggestions` both come from `ctx.command_path`. Click's own
`augment_usage_errors` fills that attribute in, but it wraps only `Context.invoke` and
`Parameter.handle_parse_result`; the raw option parser runs inside neither. So
`_ParsingState`-level failures arrive with no context at all:

    railctl cv read --mode        BadOptionUsage("Option '--mode' requires an argument.")

with `exc.ctx is None`, three words deep. Left alone the envelope answers that with
`railctl --help`, a page that lists neither `--mode` nor `--page` - the "wrong page" the
report builder's docstring exists to prevent.

The fix is the one Click already uses one layer up, applied at the only place that has both
the exception and the context: `parse_args`. `Command.make_context` calls it, so the object
is in hand exactly where the parser gives up, and the attribute is set only when it is
still empty - a `BadParameter` raised by a real parameter already carries the more precise
context Click gave it, and overwriting that would lose the level rather than find it.

`ParseContextTyper` exists so this is a property of the app, not a flag every registration
site has to remember. Typer builds a command's Click class from the `cls=` handed to
`@app.command(...)` and a group's from the `cls=` handed to `typer.Typer(...)`; defaulting
both in one subclass is what makes a new command inherit the behaviour by existing, rather
than by someone noticing. A registration that passes its own `cls` still wins.
"""

from __future__ import annotations

from typing import Any

import typer
from typer.core import TyperCommand, TyperGroup

from railctl.cli._click_errors import ClickUsageError


class _AttachesItsOwnContext:
    """Mixin for a Click `Command`: a `UsageError` leaving `parse_args` names the level."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)  # type: ignore[misc]
        except ClickUsageError as exc:
            if getattr(exc, "ctx", None) is None:
                exc.ctx = ctx  # type: ignore[attr-defined]
            raise


class ParseContextCommand(_AttachesItsOwnContext, TyperCommand):
    """`TyperCommand` for a leaf: `railctl cv read --mode` answers with `cv read`'s help."""


class ParseContextGroup(_AttachesItsOwnContext, TyperGroup):
    """`TyperGroup` for the root and for `cv`: `railctl --target` answers with the root's."""


class ParseContextTyper(typer.Typer):
    """`typer.Typer` whose commands and groups are the two classes above by default."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("cls", ParseContextGroup)
        super().__init__(**kwargs)

    def command(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("cls", ParseContextCommand)
        return super().command(*args, **kwargs)
