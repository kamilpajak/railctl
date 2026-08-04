"""Render capability results. true / false / null must never be conflated."""

from __future__ import annotations

import json

from tools.probe.checks import CheckResult


def _word(value: object) -> str:
    """The Result column for one check.

    Dispatch is on identity, deliberately, and not through a lookup table. The
    table version read `_WORDS.get(value, "see below")` with `_WORDS` keyed on
    True/False/None, which is wrong in a way that is invisible on inspection:
    Python hashes 0 equal to False and 1 equal to True, so a capability whose
    value was the integer 0 rendered as the word "no" and 1 rendered as "yes".

    That is this project's characteristic failure written into the renderer. A
    CV that reads back 0 is ordinary - CV265 reads 0 on the decoder this probe
    was built for - and it would have appeared in the human report as a
    capability the station does not have, while the JSON report of the same run
    said 0. Only the machine output was ever right.
    """
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if value is None:
        return "unknown"
    if isinstance(value, dict):
        return "see below"
    return str(value)


def _flatten(results: list[CheckResult]) -> dict[str, object]:
    """Merge every check into one capability namespace.

    Raises on a duplicate key rather than letting the last writer win. This
    output is a versioned contract (`railctl/probe-results/v1`) where fields may
    be added but never renamed or repurposed, so a new check silently destroying
    an existing field is exactly the failure the version number is supposed to
    rule out. No collision exists today; the guard is here so that adding one is
    a loud test failure instead of a quiet hole in the report.
    """
    capabilities: dict[str, object] = {}

    def claim(key: str, value: object, owner: str) -> None:
        if key in capabilities:
            raise ValueError(
                f"capability key {key!r} claimed twice; check {owner!r} would"
                " overwrite a field already published by another check"
            )
        capabilities[key] = value

    for result in results:
        if isinstance(result.value, dict):
            for key, value in result.value.items():
                claim(key, value, result.name)
        else:
            claim(result.name, result.value, result.name)
    return capabilities


def to_json(results: list[CheckResult], *, port: str, run_at: str) -> str:
    return json.dumps(
        {
            "schema": "railctl/probe-results/v1",
            "port": port,
            "run_at": run_at,
            "capabilities": _flatten(results),
            "checks": [
                {"name": r.name, "value": r.value, "detail": r.detail, "frames": r.frames}
                for r in results
            ],
        },
        indent=2,
        sort_keys=False,
    )


def to_markdown(results: list[CheckResult], *, port: str, run_at: str) -> str:
    lines = [
        "# YD7010 probe results",
        "",
        f"- Port: `{port}`",
        f"- Run at: {run_at}",
        "",
        "| Capability | Result | Detail |",
        "| --- | --- | --- |",
    ]
    for result in results:
        lines.append(f"| `{result.name}` | {_word(result.value)} | {result.detail} |")

    lines += [
        "",
        "## Flattened capabilities",
        "",
        "```json",
        json.dumps(_flatten(results), indent=2),
        "```",
        "",
        "## Raw frames",
        "",
    ]
    for result in results:
        lines.append(f"### {result.name}")
        lines.append("")
        if result.frames:
            lines += ["```", *result.frames, "```", ""]
        else:
            lines += ["(no frames received)", ""]
    return "\n".join(lines)
