"""Pinning tests written to kill surviving mutants in `report.py`.

Provenance: cosmic-ray run of 2026-08-04, 55 mutants, 47 killed by the prior
suite. See docs/test-hardening.md for the full triage.

The first test here is the one that matters, and it is a lesson about the limits
of generated inputs. A property test already asserts that a numeric capability
never renders as a verdict word, drawing integers from -1000..1000. The mutation
run still found `if value is True:` weakened to `if value == True:` alive -
because that edit misbehaves for exactly two integers in that range, 0 and 1,
and no sample of a thousand-wide range is obliged to contain them.

Those two integers are the whole bug. `0 == False` and `1 == True` in Python, so
the identity checks are the only thing keeping a CV value of 0 from rendering as
"no". They are named here rather than sampled for.
"""

from __future__ import annotations

import json

import pytest

from tools.probe.checks import CheckResult
from tools.probe.report import to_json, to_markdown

VERDICT_WORDS = ("yes", "no", "unknown")


@pytest.mark.parametrize("value", [0, 1])
def test_the_two_integers_python_confuses_with_booleans_are_not_verdicts(value: int):
    """CV265 reads back 0 on the decoder this probe was written for, so a
    capability carrying the integer 0 is an ordinary case, not an exotic one."""
    document = to_markdown(
        [CheckResult("pom_value", value, "read back from the decoder")],
        port="p",
        run_at="t",
    )
    assert f"| `pom_value` | {value} |" in document
    for word in VERDICT_WORDS:
        assert f"| `pom_value` | {word} |" not in document


@pytest.mark.parametrize(
    ("value", "word"),
    [(True, "yes"), (False, "no"), (None, "unknown")],
)
def test_each_verdict_keeps_its_own_word(value: object, word: str):
    document = to_markdown([CheckResult("z21_cv_opcodes", value, "")], port="p", run_at="t")
    assert f"| `z21_cv_opcodes` | {word} |" in document


def test_json_reports_the_two_confusable_integers_as_numbers():
    """The JSON path never had this bug, and must not acquire it."""
    results = [CheckResult("zero", 0, ""), CheckResult("one", 1, "")]
    capabilities = json.loads(to_json(results, port="p", run_at="t"))["capabilities"]
    assert capabilities["zero"] == 0
    assert capabilities["one"] == 1
    assert capabilities["zero"] is not False
    assert capabilities["one"] is not True


def test_capabilities_stay_in_the_order_the_checks_ran():
    """`sort_keys=False` is a decision, not a default.

    The report is read top to bottom by whoever ran the probe, and the checks run
    in a deliberate order: identity first, then the read paths, then the function
    paths. Sorting the keys alphabetically would scatter a run's narrative.
    """
    results = [
        CheckResult("identity", True, ""),
        CheckResult("aaa_last_alphabetically_first", True, ""),
        CheckResult("pom_read", None, ""),
    ]
    document = to_json(results, port="p", run_at="t")
    capabilities = json.loads(document)["capabilities"]
    assert list(capabilities) == [r.name for r in results]
    assert list(capabilities) != sorted(capabilities)
