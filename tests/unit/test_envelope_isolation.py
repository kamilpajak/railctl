"""The framing bytes may appear only in the files listed below, and this test is why.

If a link, station or CLI file ever spells the prefix out, the envelope has
leaked upward and adding Z21Envelope stops being a zero-edit change. The needles
are assembled at run time so this file is not its own counter-example.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("src/railctl", "tests/unit", "tests/station", "tests/cli", "tests/hardware")
# Seven files, each for a stated reason. This is an allow-list, not a waiver
# list: a file goes in only when naming the prefix is the point of the file.
#
#   envelope/liusb.py            owns the prefix; it is the implementation.
#   test_envelope_liusb.py       the envelope's own tests; the ONE file in the
#                                link and transport suites allowed to spell it.
#   xbus/codec.py                its docstring states that the codec never
#                                prepends the prefix and never checksums it -
#                                the layering rule this whole part rests on.
#   xbus/commands.py             same statement, one module up.
#   test_codec.py (Task 4)       asserts encode() does NOT start with either
#                                prefix and that the XOR changes if the prefix
#                                is included. Deleting that assertion to satisfy
#                                this guard would remove the check that keeps
#                                the framing out of the checksum.
#   test_xbus_commands.py (Task 7) explains, in the step-126 test, why a payload
#                                byte of FF means the envelope must anchor on
#                                the prefix rather than search for a delimiter.
#   test_xbus_replies.py (Task 8) feeds prefixed bytes to parse() to prove it
#                                never raises on framing that reached it by
#                                mistake.
#
# Everything else - link.py, transport/, the station and CLI suites - must hold
# bare telegrams and render them through the envelope under test.
ALLOWED = {
    "src/railctl/envelope/liusb.py",
    "src/railctl/xbus/codec.py",
    "src/railctl/xbus/commands.py",
    "tests/unit/test_codec.py",
    "tests/unit/test_envelope_liusb.py",
    "tests/unit/test_xbus_commands.py",
    "tests/unit/test_xbus_replies.py",
}
# tools/, tests/probe/ and tests/test_layering.py are out of scope by SCANNED:
# the M1 probe is a separate throwaway tool that keeps its own copy of the
# framing, and the layering guard has to name the same bytes to grep for them.

_PAIRS = (("ff", "fe"), ("ff", "fd"))
_NEEDLES = tuple(f"\\x{a}\\x{b}" for a, b in _PAIRS) + tuple(f"{a} {b}" for a, b in _PAIRS)


def _offenders() -> set[str]:
    found: set[str] = set()
    for area in SCANNED:
        base = ROOT / area
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if path == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8").lower()
            if any(needle in text for needle in _NEEDLES):
                found.add(path.relative_to(ROOT).as_posix())
    return found


def test_the_framing_bytes_appear_only_where_they_are_allowed():
    assert _offenders() == ALLOWED
