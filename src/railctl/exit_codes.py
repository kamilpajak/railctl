# src/railctl/exit_codes.py
"""The process exit codes railctl publishes, and what each one means.

Eight values, and the list is the contract. Before 0.3.0 there were twenty-one:
eleven of them (10-20) named one exception class each, which made `$?` a second
spelling of `error.code` and made a caller carry a twenty-one-branch table where
the project's own CLI rules promise six. Three of the numbers that overlap the
convention meant something else here - `4` was framing rather than "not found",
`7` was `unsupported_feature`, which is permanent and `retryable: false`, where
the convention promises "transient, retry", and `130` was never emitted at all.
Issue #65 and `docs/cli-audit-2026-08-22.md` (finding G3) have the reasoning.

WHAT `$?` IS FOR NOW: the one decision a caller can make without reading
anything. `RETRYABLE_EXIT_CODE` means the same invocation may succeed later;
every other non-zero value means it will not. Which failure it was is
`error.code`, and `railctl schema` lists every value that field can take.

WHY THIS MODULE EXISTS RATHER THAN `errors.py` OR `cli/result.py`. `errors.py`
writes `EXIT_CODES` in terms of these names, so they must sit at or below it;
`cli/result.py` imports `errors` and cannot be imported by it. The old split -
codes naming a class in `errors.py`, codes naming none in `cli/result.py` - had
a real reason that has now died: after the collapse NONE of the eight names a
class, because `DOMAIN_FAILURE_EXIT_CODE` is shared by some thirty of them. The
reason generalised to all eight, so all eight moved here.

`RETRYABLE_EXIT_CODE` is named after `result.RETRYABLE_CODES` on purpose. The
two must describe the same set, and naming them alike turns that into something
a test can assert as an identity rather than a coincidence:
`{row.code for row in error_codes() if row.exit_code == RETRYABLE_EXIT_CODE}`
must equal `RETRYABLE_CODES`. That single assertion is what catches the defect
this collapse was made to fix - a caller treating a permanent refusal as
transient - and it only exists because the names line up.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

#: Nothing went wrong. Never shifted by the canary in `tests/conftest.py`:
#: nothing folds into it, and `CommandResult.ok`, `SystemExit` and every
#: `CliRunner` assertion key on it structurally rather than as a value.
SUCCESS_EXIT_CODE: Final[int] = 0

#: A bug in railctl. Also what `exit_code_for` returns for an exception outside
#: this project's tree - "no row matched" and "railctl has a bug" are the same
#: fact, which is why the old separate `UNMAPPED_EXIT_CODE` was retired rather
#: than kept as a second name for 1.
INTERNAL_EXIT_CODE: Final[int] = 1

#: The invocation was malformed. Fix the command line; never retry.
USAGE_EXIT_CODE: Final[int] = 2

#: The target resolved and is not there. Reserved for exactly that: a named
#: port that does not exist. NOT for a malformed reply, which is a domain
#: failure - that mix-up is what made the old `4` contradict the convention.
NOT_FOUND_EXIT_CODE: Final[int] = 4

#: The same invocation may succeed later. Exactly the classes whose `code` is in
#: `result.RETRYABLE_CODES`, and nothing else: a script that retries on any
#: other value is retrying a real answer or a bug, and neither improves.
RETRYABLE_EXIT_CODE: Final[int] = 7

#: Some steps of a multi-step mutation ran and a later one failed. Not an error
#: envelope: the whole value of this code is the report of WHAT completed, and
#: that report is a result, so it goes to stdout like any other.
PARTIAL_EXIT_CODE: Final[int] = 8

#: The command failed for a real reason - the hardware, the decoder, or a file.
#: The process status deliberately does not say which. This is the value that
#: absorbed the eleven per-class codes, and `error.code` is where the detail
#: went; a command's `--help` names the codes its own 9 can carry.
DOMAIN_FAILURE_EXIT_CODE: Final[int] = 9

#: The operator stopped the run. 130 is the shell's convention for a process
#: ended by SIGINT, and railctl now honours it; before 0.3.0 an interrupt exited
#: 9 and 130 was a value typer produced that nothing published.
#:
#: `cli/main.py` keeps its own `TYPER_INTERRUPT_EXIT_CODE`, deliberately NOT
#: this constant, even though both are 130. They are two different facts that
#: happen to coincide - typer's invented number for a parse-time Ctrl-C, and
#: railctl's published status - and `main()`'s sentinel branch is precisely
#: about not confusing them.
INTERRUPTED_EXIT_CODE: Final[int] = 130

#: Every value any command may exit with. A published code missing from
#: `EXIT_MEANINGS` below is a `KeyError` when its `--help` is built, never a
#: help page with a silent hole.
PUBLISHED_EXIT_CODES: Final[frozenset[int]] = frozenset(
    {
        SUCCESS_EXIT_CODE,
        INTERNAL_EXIT_CODE,
        USAGE_EXIT_CODE,
        NOT_FOUND_EXIT_CODE,
        RETRYABLE_EXIT_CODE,
        PARTIAL_EXIT_CODE,
        DOMAIN_FAILURE_EXIT_CODE,
        INTERRUPTED_EXIT_CODE,
    }
)

#: The one-line meaning of each code, for the `EXIT CODES` block of every
#: `--help` page. Keyed by the constants, so a new code with no sentence fails
#: at import rather than printing a blank line.
#:
#: This table used to be derived: `_meta._exit_code_lines` inverted
#: `errors.EXIT_CODES` and read the docstring of the class owning each number.
#: That worked only while the mapping was one-to-one, and under a collapse it
#: does not raise - it silently publishes whichever of thirty classes sorts
#: last. Prose about the CONTRACT belongs with the contract.
EXIT_MEANINGS: Final[Mapping[int, str]] = MappingProxyType(
    {
        SUCCESS_EXIT_CODE: "success",
        INTERNAL_EXIT_CODE: "unhandled internal error",
        USAGE_EXIT_CODE: "usage error - a bad flag, value, or missing argument",
        NOT_FOUND_EXIT_CODE: "the target was named and is not there",
        RETRYABLE_EXIT_CODE: (
            "transient - the same invocation may succeed later; error.code says which of "
            "the three retryable conditions it was"
        ),
        PARTIAL_EXIT_CODE: (
            "partial - some steps of this command completed and a later one failed; the "
            "result names which"
        ),
        DOMAIN_FAILURE_EXIT_CODE: (
            "the operation failed for a real reason - the hardware, the decoder, or a "
            "file. The status does not say which; error.code does, and railctl schema "
            "lists every value it can take"
        ),
        INTERRUPTED_EXIT_CODE: "the operator interrupted the run (error.code aborted)",
    }
)
