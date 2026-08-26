"""Hypothesis profiles, and the exit-code canary.

Selected with the HYPOTHESIS_PROFILE environment variable; the default applies
when it is unset, so an ordinary `pytest` run needs no ceremony.
"""

from __future__ import annotations

import os

# -- the exit-code canary ------------------------------------------------------
#
# `RAILCTL_EXIT_CODE_CANARY=1 uv run pytest` must be GREEN. It moves every
# published exit code by +100 before any test module is imported; a test that
# still goes red is a test that asserts an exit code as a NUMBER rather than by
# name, and its file and line are printed for you.
#
# This exists because no regex can find those sites. `tests/cli/test_throttle.py`
# carries a parametrize row `[(0, 0, False), (1, 20, True)]` where `1` is a speed
# step and `20` is an exit code, in plain decimal, with nothing to tell them
# apart. Issue #65 collapsed twenty-one exit codes to eight and touched ~273
# assertions to do it; this is what stands between that work and the next silent
# literal, so it runs as its own CI step.
#
# Placed at the very top on purpose: this module is imported before every test
# module, so a test's `from railctl.exit_codes import USAGE_EXIT_CODE` binds the
# shifted value, and `cli/_meta`'s derived tuples are computed later still.
#
# `SUCCESS_EXIT_CODE` is deliberately NOT shifted - nothing folds into 0, and
# `ok`, `SystemExit` and every `CliRunner` result key on it structurally.
# `cli.main.TYPER_INTERRUPT_EXIT_CODE` is deliberately NOT shifted either: it is
# typer's number, not railctl's, and leaving it alone is what catches someone
# "tidying" `main()`'s sentinel into railctl's constant - a change that looks
# right and is wrong.
if os.environ.get("RAILCTL_EXIT_CODE_CANARY"):
    from types import MappingProxyType

    from railctl import errors, exit_codes

    _SHIFT = 100
    for _name in [n for n in dir(exit_codes) if n.endswith("_EXIT_CODE")]:
        _value = getattr(exit_codes, _name)
        if _value:
            setattr(exit_codes, _name, _value + _SHIFT)
    errors.EXIT_CODES.update({k: v + _SHIFT for k, v in errors.EXIT_CODES.items()})
    # The two derived tables are rebuilt, not just the scalars: both were evaluated
    # when `exit_codes` was imported, a moment before this runs, so shifting the
    # constants alone would leave `EXIT_MEANINGS` keyed by the old numbers and every
    # `--help` page would raise KeyError instead of the suite reporting a real result.
    exit_codes.EXIT_MEANINGS = MappingProxyType(
        {k + _SHIFT if k else k: v for k, v in exit_codes.EXIT_MEANINGS.items()}
    )
    exit_codes.PUBLISHED_EXIT_CODES = frozenset(
        c + _SHIFT if c else c for c in exit_codes.PUBLISHED_EXIT_CODES
    )

    def pytest_ignore_collect(collection_path, config):
        """`tests/unit/test_exit_codes.py` is the one file the canary must not run.

        That file is the contract's second copy and its literals are deliberate - it
        exists so that an edit to `railctl/exit_codes.py` shows up in a diff as a
        contract change rather than a refactor. Under the canary every one of those
        literals is wrong by design, so 33 red tests there would say nothing except
        that the shift happened, while burying the failures that mean something.
        """
        return collection_path.name == "test_exit_codes.py"


from hypothesis import HealthCheck, Verbosity, settings

# Everyday runs. The deadline is off because these tests do byte-level work in
# pure Python and a laptop under load can exceed 200 ms on an unlucky example,
# which would fail a test for being slow rather than for being wrong.
settings.register_profile("default", max_examples=100, deadline=None)

# Mutation runs. cosmic-ray executes the suite once per mutant, so the example
# count is the dominant cost. 25 examples still explores far more input than the
# example tests do, and a mutant that survives 25 draws of random bytes is
# almost always a genuine survivor rather than a lucky one - the borderline
# cases get re-checked at full strength when their pinning test is written.
#
# suppress_health_check matters here: a mutated strategy or filter can make
# Hypothesis complain about slow data generation, and a health-check failure
# would count as a KILL that the assertions never earned.
#
# derandomize is the important one, and it is about measurement rather than
# speed. With random draws, whether a marginal mutant dies depends on whether
# that run happened to generate the input exposing it, so the same code scores
# differently on consecutive runs and two runs cannot be compared. Two frames.py
# mutants flapped exactly this way before it was set. A fixed seed makes a
# mutation score a measurement instead of a sample; the default profile stays
# random, because there the randomness is the point.
settings.register_profile(
    "mutation",
    max_examples=25,
    deadline=None,
    derandomize=True,
    suppress_health_check=list(HealthCheck),
)

# CI: worth more examples than a developer wants to sit through.
# derandomize: a newly discovered example must not fail an unrelated CI run, and
# a mutation score computed from random draws is a sample rather than a
# measurement (docs/test-hardening.md).
settings.register_profile(
    "ci",
    max_examples=500,
    deadline=None,
    derandomize=True,
    verbosity=Verbosity.normal,
)

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))

# --- railctl fixtures -------------------------------------------------------
# The station and CLI suites take the envelope as a parameter and hold bare
# telegrams, so adding Z21Envelope re-runs every one of them against new framing
# with zero test edits. The list has one element today; retrofitting the
# parametrisation later is what fails.
import pytest  # noqa: E402

from railctl.envelope.liusb import LiUsbEnvelope  # noqa: E402

ENVELOPES = [LiUsbEnvelope]


@pytest.fixture(params=ENVELOPES, ids=lambda cls: cls.__name__)
def envelope_factory(request):
    """The envelope CLASS under test. Call it to get a fresh instance."""
    return request.param


# Every scripted suite runs twice: whole-frame, then one byte at a time. The
# byte-at-a-time run is the worst case a USB CDC port actually produces.
@pytest.fixture(params=[None, 1], ids=["whole-frame", "byte-at-a-time"])
def chunk_size(request):
    return request.param
