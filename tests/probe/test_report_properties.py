"""Property tests for the report renderers.

The whole probe exists to distinguish three answers: the hardware can do this,
the hardware cannot do this, and we have not established it. The report is the
last place that distinction can be destroyed, and destroying it is silent - a
capability rendered as "no" instead of "unknown" reads as a measurement that
was never taken.

So the properties here are mostly about what must NOT happen: `null` must never
arrive as `false`, and two checks must never quietly overwrite each other's
field in a document that calls itself a versioned contract.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tools.probe.checks import CheckResult
from tools.probe.report import to_json, to_markdown

SCHEMA = "railctl/probe-results/v1"

# The three-valued capability answer, plus the scalars checks actually publish
# (speed step mode, status_raw, the result channel).
# The alphabet excludes the markdown cell separator and anything that would end
# a table row, so that a generated value cannot break the row it is rendered in.
# That is a limitation of the assertions here, not a claim about the renderer.
CAPABILITY_VALUES = st.one_of(
    st.booleans(),
    st.none(),
    st.integers(min_value=-1000, max_value=1000),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789 -_", max_size=12),
)

NAMES = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=10)


@st.composite
def check_results(draw: st.DrawFn) -> CheckResult:
    name = draw(NAMES)
    value = draw(
        st.one_of(
            CAPABILITY_VALUES,
            st.dictionaries(NAMES, CAPABILITY_VALUES, max_size=4),
        )
    )
    return CheckResult(
        name=name,
        value=value,
        detail=draw(st.text(max_size=30)),
        frames=draw(st.lists(st.text(max_size=12), max_size=3)),
    )


def claimed_keys(results: list[CheckResult]) -> list[str]:
    """Every capability key these results would publish, duplicates included."""
    keys: list[str] = []
    for result in results:
        if isinstance(result.value, dict):
            keys.extend(result.value)
        else:
            keys.append(result.name)
    return keys


@st.composite
def collision_free_results(draw: st.DrawFn) -> list[CheckResult]:
    results = draw(st.lists(check_results(), max_size=6))
    keys = claimed_keys(results)
    if len(keys) != len(set(keys)):
        # Rebuilding is cheaper than filtering: assume() on a list this wide
        # rejects most draws and Hypothesis gives up before finding anything.
        results = [
            CheckResult(
                f"{index}_{r.name}",
                r.value
                if not isinstance(r.value, dict)
                else {f"{index}_{k}": v for k, v in r.value.items()},
                r.detail,
                r.frames,
            )
            for index, r in enumerate(results)
        ]
    return results


@given(collision_free_results(), NAMES, st.text(max_size=25))
def test_the_json_report_is_exactly_one_parseable_document(
    results: list[CheckResult], port: str, run_at: str
):
    """Machine output is a contract: one JSON value on stdout, nothing else."""
    document = json.loads(to_json(results, port=port, run_at=run_at))
    assert document["schema"] == SCHEMA
    assert document["port"] == port
    assert document["run_at"] == run_at


@given(collision_free_results())
def test_every_capability_value_survives_json_unchanged(results: list[CheckResult]):
    """True, False and None must come out as themselves.

    The failure this rules out is the one that cannot be seen by reading the
    output: `null` collapsed into `false` looks like a finished measurement.
    """
    document = json.loads(to_json(results, port="p", run_at="t"))
    capabilities = document["capabilities"]

    expected: dict[str, object] = {}
    for result in results:
        if isinstance(result.value, dict):
            expected.update(result.value)
        else:
            expected[result.name] = result.value

    assert capabilities == expected
    for key, value in expected.items():
        if value is None:
            assert capabilities[key] is None, f"{key} lost its unknown state"
        elif isinstance(value, bool):
            assert capabilities[key] is value


@given(collision_free_results())
def test_no_check_is_dropped_from_the_report(results: list[CheckResult]):
    document = json.loads(to_json(results, port="p", run_at="t"))
    assert [entry["name"] for entry in document["checks"]] == [r.name for r in results]
    for entry, result in zip(document["checks"], results, strict=True):
        assert entry["detail"] == result.detail
        assert entry["frames"] == result.frames


@given(st.lists(check_results(), max_size=6))
def test_a_duplicate_capability_key_is_refused_rather_than_overwritten(
    results: list[CheckResult],
):
    """Last-writer-wins in a document that advertises a schema version is a hole
    the version number is supposed to rule out."""
    keys = claimed_keys(results)
    if len(keys) == len(set(keys)):
        to_json(results, port="p", run_at="t")
    else:
        with pytest.raises(ValueError):
            to_json(results, port="p", run_at="t")


@given(collision_free_results())
def test_markdown_gives_each_kind_of_value_its_own_word(results: list[CheckResult]):
    """`unknown` and `no` are different findings, and the human report is where a
    reader decides whether a capability still needs measuring.

    A scalar must not borrow a verdict word either. `_WORDS.get(value)` used to
    render the integer 0 as "no" and 1 as "yes", because Python hashes them
    equal to False and True - so a CV reading back 0 looked like a capability
    the station does not have.
    """
    document = to_markdown(results, port="p", run_at="t")
    for result in results:
        value = result.value
        if value is True:
            expected = "yes"
        elif value is False:
            expected = "no"
        elif value is None:
            expected = "unknown"
        elif isinstance(value, dict):
            expected = "see below"
        else:
            expected = str(value)
        assert f"| `{result.name}` | {expected} |" in document


@given(st.integers(min_value=-1000, max_value=1000))
def test_a_numeric_capability_never_renders_as_a_verdict_word(value: int):
    """The regression, stated directly: 0 and 1 are values, not verdicts."""
    document = to_markdown(
        [CheckResult("pom_value", value, f"CV265 read back {value}")], port="p", run_at="t"
    )
    row = f"| `pom_value` | {value} |"
    assert row in document
    assert "| `pom_value` | no |" not in document
    assert "| `pom_value` | yes |" not in document


@given(collision_free_results())
def test_markdown_carries_every_check_and_its_frames(results: list[CheckResult]):
    document = to_markdown(results, port="p", run_at="t")
    for result in results:
        assert f"### {result.name}" in document
        for frame in result.frames:
            assert frame in document
    embedded = json.loads(document.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert embedded == json.loads(to_json(results, port="p", run_at="t"))["capabilities"]


@given(st.lists(check_results(), max_size=6))
def test_both_renderers_agree_on_whether_the_results_are_publishable(
    results: list[CheckResult],
):
    """A set of checks that JSON refuses must not render as valid markdown, or a
    collision would be visible in one format and hidden in the other."""
    json_failed = False
    try:
        to_json(results, port="p", run_at="t")
    except ValueError:
        json_failed = True

    markdown_failed = False
    try:
        to_markdown(results, port="p", run_at="t")
    except ValueError:
        markdown_failed = True

    assert json_failed == markdown_failed
