"""Capability checks. Every check is read-only: no decoder CV is ever written."""

from __future__ import annotations

from dataclasses import dataclass, field

from tools.probe import commands
from tools.probe.link import Link
from tools.probe.replies import (
    BUSY,
    NO_ACK,
    SHORT_CIRCUIT,
    UNSUPPORTED,
    CvValue,
    parse,
)

POM_WINDOW = 5.0
POLL_INTERVAL = 0.25


def _hexdump(frames) -> list[str]:
    return [f"{'FE' if f.solicited else 'FD'} {f.telegram.hex(' ').upper()}" for f in frames]


@dataclass
class CheckResult:
    name: str
    value: object | None
    detail: str
    frames: list[str] = field(default_factory=list)


def check_pom_read(link: Link, address: int, cv: int = 8, *, poll: bool) -> CheckResult:
    """R1. CV8 is used by default because its ZIMO value is a known constant (145),
    so a plausible-but-wrong reading is detectable."""
    wire = commands.cv_wire(cv)
    frames = link.exchange(commands.pom_read(address, cv), window=POM_WINDOW)
    channel = "broadcast"

    if poll and not any(isinstance(parse(f.telegram), CvValue) for f in frames):
        polled = link.exchange(commands.service_result(), window=POM_WINDOW)
        if any(isinstance(parse(polled_frame.telegram), CvValue) for polled_frame in polled):
            channel = "poll"
        frames = frames + polled

    seen = [parse(f.telegram) for f in frames]
    dump = _hexdump(frames)

    for reply in seen:
        if isinstance(reply, CvValue):
            echo_zero_based = None
            if reply.raw_cv == wire:
                echo_zero_based = True
            elif reply.raw_cv == cv:
                echo_zero_based = False
            return CheckResult(
                "pom_read",
                {
                    "pom_read": True,
                    "pom_result_channel": channel,
                    "pom_echo_zero_based": echo_zero_based,
                    "value": reply.value,
                },
                f"POM read of CV{cv} returned {reply.value} via {channel}",
                dump,
            )

    if UNSUPPORTED in seen:
        return CheckResult(
            "pom_read",
            {
                "pom_read": False,
                "pom_result_channel": "none",
                "pom_echo_zero_based": None,
                "value": None,
            },
            "station answered 61 82 (instruction not supported): POM read is not implemented",
            dump,
        )
    if NO_ACK in seen:
        return CheckResult(
            "pom_read",
            {
                "pom_read": None,
                "pom_result_channel": "none",
                "pom_echo_zero_based": None,
                "value": None,
            },
            "decoder did not acknowledge: check RailCom"
            " (CV29 bit 3 = 1, CV28 bits 0 and 1 set) and retry",
            dump,
        )
    if SHORT_CIRCUIT in seen:
        return CheckResult(
            "pom_read",
            None,
            "short circuit reported; fix the track and retry",
            dump,
        )
    if BUSY in seen:
        return CheckResult("pom_read", None, "station busy; retry", dump)

    return CheckResult(
        "pom_read",
        {
            "pom_read": False,
            "pom_result_channel": "none",
            "pom_echo_zero_based": None,
            "value": None,
        },
        "no result on either channel and neither 61 13 nor 61 82:"
        " concluded from silence",
        dump,
    )
