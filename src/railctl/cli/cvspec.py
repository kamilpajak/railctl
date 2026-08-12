# src/railctl/cli/cvspec.py
"""`parse_cv_spec` - the one grammar CV lists are typed in.

`cv read` reads its positional CVSPEC tokens through this function, and M9's
`--only`/`--range` consumers will read theirs through the same one (design
L2): `29`; `3-8`; `1,3,29`; or a catalog slug such as `accel_rate`. Tokens
concatenate, duplicates collapse, and first-appearance order is kept.

Two failure families, split by whose mistake it is - the same split
`xbus/cv.py` documents at length. A token that does not parse (an empty
piece, a backwards range, an unknown slug) is a `UsageProblem`: the
invocation is malformed, exit 2, with runnable suggestions - an unknown slug
names the three closest catalog slugs, ranked, the same way `command_meta`
answers a mistyped command path. A CV NUMBER outside 1..1024 is
`CvOutOfRangeError`, exit 15, naming the bound: that is the design's rule for
a CV above the bound of the resolved mode, and both modes share the same
CLI-level bound (`MAX_CV_POM == MAX_CV_EXT == 1024`), so nothing here needs
to know which mode later resolves. The deeper per-encoding bounds - the
direct opcodes stop at CV255 - stay with the station layer, which raises the
same class, and `cli/_errors.default_suggestions` turns the class into the
documented `railctl doctor` suggestion in both cases.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from typing import Final

from railctl.catalog import CatalogEntry
from railctl.cli.deps import UsageProblem
from railctl.errors import CvOutOfRangeError
from railctl.xbus.cv import CV_MIN, MAX_CV_POM

#: One user-facing CV space bounds every mode at this level. Imported off
#: `xbus/cv.py` rather than retyped, so the wire layer's own bound and the
#: one this grammar enforces cannot drift.
CV_MAX: Final[int] = MAX_CV_POM

#: How many near misses an unknown slug names - the same three
#: `_meta.command_meta` offers for a mistyped command path.
_CLOSEST: Final[int] = 3

_RANGE: Final[re.Pattern[str]] = re.compile(r"^(\d+)-(\d+)$")


def parse_cv_spec(
    tokens: Sequence[str],
    catalog: Mapping[int, CatalogEntry],
    *,
    argv_prefix: Sequence[str],
) -> list[int]:
    """The CV numbers `tokens` name, deduplicated, in first-appearance order.

    `argv_prefix` is the runnable command the tokens were typed after (for
    `cv read`: `["railctl", "cv", "read"]`), so every suggestion this grammar
    raises is a command the caller can run, not a sentence to parse.
    """
    ordered: list[int] = []
    seen: set[int] = set()
    for token in tokens:
        for piece in token.split(","):
            for cv in _piece_cvs(
                piece.strip(), catalog, tokens=tokens, argv_prefix=argv_prefix
            ):
                if cv not in seen:
                    seen.add(cv)
                    ordered.append(cv)
    if not ordered:
        # Unreachable through Typer (`cv read` requires at least one token and
        # an empty piece raises above), but this is a library function M9 will
        # call with computed lists, and an empty answer to "which CVs?" must
        # be a refusal, never a silent zero-CV sweep that reports success.
        raise UsageProblem(
            "no CVs given",
            suggestions=[[*argv_prefix, "8"]],
            details={"reason": "no_cvs", "tokens": list(tokens)},
        )
    return ordered


def _checked(cv: int) -> int:
    if not CV_MIN <= cv <= CV_MAX:
        raise CvOutOfRangeError(
            f"CV {cv} is outside {CV_MIN}..{CV_MAX}, the bound for every programming "
            f"mode this tool speaks",
            cv=cv,
        )
    return cv


def _argv_with(
    tokens: Sequence[str], piece: str, replacement: str, argv_prefix: Sequence[str]
) -> list[str]:
    """The caller's FULL argv with the one failing piece corrected.

    A suggestion that carried only `[*argv_prefix, replacement]` dropped every
    other token the caller typed - `cv read 3-8 accel_rte` was answered with
    `railctl cv read accel_rate`, a runnable command that no longer reads
    CV3-8. The suggestion contract is "the runnable next command", which means
    the whole invocation, corrected in place.
    """
    corrected = [
        ",".join(replacement if part.strip() == piece else part for part in token.split(","))
        for token in tokens
    ]
    return [*argv_prefix, *corrected]


def _piece_cvs(
    piece: str,
    catalog: Mapping[int, CatalogEntry],
    *,
    tokens: Sequence[str],
    argv_prefix: Sequence[str],
) -> list[int]:
    """One comma-separated piece: a number, a `first-last` range, or a slug."""
    if not piece:
        raise UsageProblem(
            "empty CV token: write `1,3,29` with no doubled or trailing comma",
            suggestions=[[*argv_prefix, "1,3,29"]],
            details={"reason": "empty_token"},
        )
    matched = _RANGE.match(piece)
    if matched:
        first, last = int(matched.group(1)), int(matched.group(2))
        if first > last:
            raise UsageProblem(
                f"CV range {piece!r} runs backwards: {first} > {last}",
                suggestions=[_argv_with(tokens, piece, f"{last}-{first}", argv_prefix)],
                details={"reason": "backwards_range", "first": first, "last": last},
            )
        return [_checked(cv) for cv in range(first, last + 1)]
    if piece.isdigit():
        return [_checked(int(piece))]
    slugs = {entry.slug: entry.num for entry in catalog.values()}
    number = slugs.get(piece)
    if number is not None:
        return [number]
    near = difflib.get_close_matches(piece, slugs, n=_CLOSEST, cutoff=0.0)
    raise UsageProblem(
        f"{piece!r} is not a CV number, a range or a catalog slug; closest catalog "
        f"names: {', '.join(near)}",
        suggestions=[_argv_with(tokens, piece, name, argv_prefix) for name in near],
        details={"reason": "unknown_slug", "token": piece, "closest": near},
    )
