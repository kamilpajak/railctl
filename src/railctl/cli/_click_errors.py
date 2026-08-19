# src/railctl/cli/_click_errors.py
"""The two vendored Click exception classes `main()` catches, named through public typer.

Typer 0.27 does not depend on Click any more: it vendors it as `typer._click`, and every
argument-parsing failure the app raises is one of those vendored classes. `main()` has to
name them to turn a parse failure into this tool's own JSON error envelope, and typer
re-exports only the leaves of that tree (`typer.BadParameter`, `typer.Exit`, `typer.Abort`)
- not `UsageError`, not `ClickException`.

They are reached with `__base__` rather than `from typer._click.exceptions import ...`:

- the module path is private, and a typer release may rename or move it without that being
  a breaking change for anyone;
- what this tool actually depends on is the class HIERARCHY - one `except UsageError` has to
  cover an unknown command, an unknown option, a bad value and a missing argument - and that
  hierarchy is reachable from the public re-export `typer.BadParameter`, whose MRO is
  `BadParameter -> UsageError -> ClickException -> Exception`;
- `tests/cli/test_usage_envelope.py` pins both names and both `issubclass` relations, so a
  typer upgrade that breaks the assumption fails in CI instead of silently dropping the
  envelope back to a Rich box on stderr.

`typer.Exit` and `typer.Abort` are deliberately NOT re-exported here. They are the same
class objects, already public under those names, and a second spelling for a public name is
one more thing that can drift.
"""

from __future__ import annotations

from typing import Final

import typer

#: Click's `UsageError`: the operator's invocation is malformed. `exit_code` is 2, the same
#: value `result.USAGE_EXIT_CODE` publishes, and `NoSuchOption`, `NoArgsIsHelpError`,
#: `MissingParameter` and `BadParameter` are all subclasses of it.
ClickUsageError: Final[type[Exception]] = typer.BadParameter.__base__  # type: ignore[assignment]

#: Click's `ClickException`, the root of the tree. `exit_code` is 1. Nothing in railctl
#: raises one directly, so catching it is a safety net rather than a documented path.
ClickException: Final[type[Exception]] = ClickUsageError.__base__  # type: ignore[assignment]
