"""Render capability results. true / false / null must never be conflated."""

from __future__ import annotations

import json

from tools.probe.checks import CheckResult

_WORDS = {True: "yes", False: "no", None: "unknown"}


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
        if isinstance(result.value, dict):
            word = "see below"
        else:
            word = _WORDS.get(result.value, "see below")
        lines.append(f"| `{result.name}` | {word} | {result.detail} |")

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
