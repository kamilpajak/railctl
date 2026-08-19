# src/railctl/cli/commands/_sweep.py
"""The arithmetic behind `railctl backup --all` (design C6, milestone M11).

A sweep reads every CV the resolved mode can reach instead of the 77 the
curated catalog names, so a decoder's undocumented settings land in the file
too. Four decisions have to be made before a single byte goes down the wire -
how far the sweep may go, what each swept CV is called, how long the whole
thing will take, and how to say that duration to a person - and none of them
needs a station. They live here rather than in `backup.py` so they can be
pinned at a desk: `backup.py` is a thousand lines of run order and output
plumbing, and a bound that is wrong by a factor of four is not something to
discover at the bench.

This module never reads, never opens a port, and holds no state. What it can
and cannot claim is the point of every docstring below: `sweep_bound` refuses
rather than guess when nothing is proven, and `HIGHEST_EXERCISED_CV` records
where this bench's evidence stops - which is not where the hardware stops.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

from railctl.backup import SOURCE_CATALOG, SOURCE_SWEEP
from railctl.errors import ServiceEncodingUnknownError
from railctl.station import ProgMode
from railctl.xbus.cv import MAX_CV_DIRECT, MAX_CV_EXT, MAX_CV_Z21

if TYPE_CHECKING:
    from collections.abc import Mapping

    from railctl.catalog import CatalogEntry
    from railctl.station import Capabilities

#: Where this bench's CORROBORATED evidence stops, which is not where its
#: answers stop. Only the extended reply bands `63 14` (CV1..255) and `63 15`
#: (CV256..511) have ever been seen; `63 16` and `63 17` come from the Lenz
#: document alone.
#:
#: The first full sweep (2026-08-19) then got an answer for every CV from 512
#: to 1024 through the Z21 16-bit opcode, which carries no band byte at all -
#: so reads up there are no longer unexercised, and the warning's text says
#: so. The boundary stays at 511 anyway, because being answered is not the
#: same as being right: not one value above it has been checked against a
#: known quantity, and the 500-odd zeroes the sweep returned there are
#: exactly what an out-of-range read would also produce. Raising this number
#: means measuring a value up there against something, not reading it again.
HIGHEST_EXERCISED_CV: Final[int] = 511

#: Seconds per CV, measured 2026-08-19: 77 CVs in 185 s with groups of eight
#: (docs/probe-results.md, "Grouped service reads"). Used ONLY for the
#: up-front estimate; the run replaces it with what it observes.
SWEEP_SECONDS_PER_CV: Final[float] = 2.4

#: A sweep whose estimate passes this asks first (design L6).
SWEEP_CONFIRM_SECONDS: Final[float] = 60.0

#: How many CVs the revised estimate is based on. Small on purpose: the point
#: is to correct a wrong up-front guess early, not to average the whole run.
SWEEP_ESTIMATE_AFTER: Final[int] = 10

#: One progress line per this many CVs, on stderr.
SWEEP_PROGRESS_EVERY: Final[int] = 32

#: `set` and the default filename for a sweep, against `"curated"`.
SWEEP_SET_NAME: Final[str] = "all"

#: Time units for `format_duration`. Named because a bare 60 in that function
#: reads exactly like `SWEEP_CONFIRM_SECONDS`, which is a different 60.
_SECONDS_PER_MINUTE: Final[int] = 60
_MINUTES_PER_HOUR: Final[int] = 60

#: The service-mode encodings a sweep may lean on, widest reach first, each
#: with the CV it reaches. Ordered rather than compared: the widest PROVEN
#: encoding wins, and an encoding that was never probed is skipped whatever
#: its reach would have been.
_SERVICE_BOUNDS: Final[tuple[tuple[str, int], ...]] = (
    ("z21_cv_opcodes", MAX_CV_Z21),
    ("service_ext_cv", MAX_CV_EXT),
    ("service_direct_cv", MAX_CV_DIRECT),
)


def sweep_bound(mode: ProgMode, capabilities: Capabilities) -> int:
    """The highest CV a sweep may ask for, from measured capabilities only.

    Every capability must be exactly `True`. `None` is "nobody probed this",
    and an unprobed station never gets an opcode nobody has seen work, so a
    station with `z21_cv_opcodes` unknown and `service_direct_cv` proven
    sweeps to 255 rather than 1024. Service mode with nothing proven raises
    `ServiceEncodingUnknownError`: there is no honest bound to return, and
    the remedy is a probe rather than a different number.

    POM is 255 for the reason `reachable_bound` gives - CV256 and up sit
    behind the ZIMO CV31/CV32 index page, and selecting a page writes those
    selectors, which a backup never does. The two functions answer different
    questions and are both needed: `reachable_bound` answers "can this
    CURATED CV be read, or must its row say skipped", while this one answers
    "how far does the sweep go" and so decides which rows exist at all. A
    sweep asks only for CVs inside this bound, so it never produces the
    out-of-range skip that `_bound_detail` exists to explain.
    """
    if mode is ProgMode.POM:
        return MAX_CV_DIRECT
    for field, bound in _SERVICE_BOUNDS:
        if getattr(capabilities, field) is True:
            return bound
    unprobed = [field for field, _ in _SERVICE_BOUNDS if getattr(capabilities, field) is None]
    measured_no = [field for field, _ in _SERVICE_BOUNDS if getattr(capabilities, field) is False]
    state = f"unprobed: {', '.join(unprobed)}" if unprobed else "none of them answered"
    if measured_no:
        state += f"; rejected: {', '.join(measured_no)}"
    raise ServiceEncodingUnknownError(
        f"a sweep needs a proven service-mode encoding and this command station has none "
        f"({state}); the bound would be a guess, and a guessed bound decides how many "
        f"CVs get read",
        hint=_no_bound_hint(unprobed, capabilities),
    )


def _no_bound_hint(unprobed: list[str], capabilities: Capabilities) -> str:
    """The remedy that can actually change the answer, which depends on WHY
    nothing is proven - the same split `_service_encoding_for` makes in
    `station/programming.py`.

    A probe is the remedy only while something is still unprobed. When all
    three encodings are a measured `False` the probe has already returned its
    verdict - each `False` comes from a `61 82` rejection doctor recorded -
    and sending the operator back to `railctl doctor` names a cause that does
    not exist. What is left is the main track, which sweeps to
    `MAX_CV_DIRECT` and needs no service-mode encoding at all; that is only
    worth suggesting while `pom_read` is not itself a measured no.
    """
    if unprobed:
        return "run `railctl doctor` to probe the service-mode encodings"
    if capabilities.pom_read is True:
        return f"sweep the main track instead with `--mode pom`, which reaches CV1..{MAX_CV_DIRECT}"
    if capabilities.pom_read is None:
        # Offered, but never as a promise: nobody has measured POM reading on
        # this station, and on the reference hardware it answers nothing at
        # all (docs/probe-results.md R1). Saying "use --mode pom" flat would
        # send an operator to a channel that may be as silent as the one they
        # were just refused.
        return (
            f"`--mode pom` reaches CV1..{MAX_CV_DIRECT} on the main track, but POM reading "
            f"has never been probed on this station - run `railctl doctor` first to find "
            f"out whether it answers at all"
        )
    return (
        "this command station rejected every service-mode opcode (61 82) and POM reading "
        "is a measured no, so there is no channel left for a sweep to use"
    )


def sweep_name(cv: int, catalog: Mapping[int, CatalogEntry]) -> tuple[str, str]:
    """`(name, source)` for one swept CV.

    The catalog's slug and `SOURCE_CATALOG` when the catalog names it, so a
    sweep's file reads like a curated one wherever the two overlap and a
    diff against a curated backup lines up by name. Everything else is
    `cv0617` and `SOURCE_SWEEP` - zero-padded to four digits so the rows sort
    the way the CVs do in any text tool, and marked so `restore` can refuse
    to write back a value nothing documents.
    """
    entry = catalog.get(cv)
    if entry is not None:
        return entry.slug, SOURCE_CATALOG
    return f"cv{cv:04d}", SOURCE_SWEEP


def estimate_seconds(count: int, seconds_per_cv: float = SWEEP_SECONDS_PER_CV) -> float:
    """Wall-clock estimate for `count` CVs.

    Linear on purpose. The grouped service session amortises the setup cost
    across eight reads, which is already inside the measured 2.4 s, and the
    only thing this number is used for is a question an operator answers yes
    or no to. The run replaces the rate with the one it observes after
    `SWEEP_ESTIMATE_AFTER` CVs rather than trying to be cleverer here.
    """
    return count * seconds_per_cv


def format_duration(seconds: float) -> str:
    """`"32 min"`, `"1 h 42 min"`, `"45 s"` - what a human is asked to agree to.

    One unit below a minute, minutes below an hour, hours and minutes above
    it, each rounded to the nearest whole unit and carried when the rounding
    reaches a full one - 3599 s is `"1 h"`, never `"60 min"`. A duration
    whose minutes round to zero drops them, so an exact hour is `"1 h"`.
    """
    whole_seconds = math.floor(seconds + 0.5)
    if whole_seconds < _SECONDS_PER_MINUTE:
        return f"{whole_seconds} s"
    minutes = math.floor(whole_seconds / _SECONDS_PER_MINUTE + 0.5)
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes} min"
    hours, rest = divmod(minutes, _MINUTES_PER_HOUR)
    return f"{hours} h {rest} min" if rest else f"{hours} h"
