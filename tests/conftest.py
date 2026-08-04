"""Hypothesis profiles.

Selected with the HYPOTHESIS_PROFILE environment variable; the default applies
when it is unset, so an ordinary `pytest` run needs no ceremony.
"""

from __future__ import annotations

import os

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
settings.register_profile("ci", max_examples=500, deadline=None, verbosity=Verbosity.normal)

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))
