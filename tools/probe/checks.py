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
    LocoInfo,
    Status,
    Version,
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


def _unresolved() -> dict[str, object | None]:
    """The R1 result dict with nothing established. Used for every path that
    does not reach a verdict, so `CheckResult.value` for check_pom_read is
    always a dict and never a bare None."""
    return {
        "pom_read": None,
        "pom_result_channel": "none",
        "pom_echo_zero_based": None,
        "value": None,
    }


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
    # Transient station conditions. Neither proves anything about the capability,
    # so pom_read stays None — but the dict shape is kept so every caller can
    # subscript CheckResult.value without a type check.
    if SHORT_CIRCUIT in seen:
        return CheckResult(
            "pom_read", _unresolved(), "short circuit reported; fix the track and retry", dump
        )
    if BUSY in seen:
        return CheckResult("pom_read", _unresolved(), "station busy; retry", dump)

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


SERVICE_WINDOW = 8.0


def _read_value(link: Link, payload: bytes) -> tuple[int | None, list, bool]:
    """Returns (value, replies, was_rejected)."""
    frames = link.exchange(payload, window=SERVICE_WINDOW)
    replies = [parse(f.telegram) for f in frames]
    for reply in replies:
        if isinstance(reply, CvValue):
            return reply.value, frames, False
    return None, frames, UNSUPPORTED in replies


def check_service_ext_cv(link: Link) -> CheckResult:
    """R2. Compare the extended read of CV1 against the legacy direct read of CV1."""
    ext_value, ext_frames, rejected = _read_value(link, commands.service_ext_read(1))
    if rejected:
        return CheckResult(
            "service_ext_cv", False, "station answered 61 82 to 22 18: extended opcodes absent",
            _hexdump(ext_frames),
        )
    direct_value, direct_frames, _ = _read_value(link, commands.service_direct_read(1))
    dump = _hexdump(ext_frames) + _hexdump(direct_frames)
    if ext_value is None or direct_value is None:
        return CheckResult("service_ext_cv", None, "no value from one of the two reads", dump)
    if ext_value != direct_value:
        return CheckResult(
            "service_ext_cv", None,
            f"reads disagree: extended {ext_value}, direct {direct_value}", dump,
        )
    return CheckResult(
        "service_ext_cv", True, f"extended and direct reads of CV1 both returned {ext_value}", dump
    )


def check_z21_opcodes(link: Link) -> CheckResult:
    """R4. Only the READ opcode 23 11 is probed. Never 24 12, which would write."""
    z21_value, z21_frames, rejected = _read_value(link, commands.z21_service_read(29))
    if rejected:
        return CheckResult(
            "z21_cv_opcodes", False, "station answered 61 82 to 23 11: Z21 CV opcodes absent",
            _hexdump(z21_frames),
        )
    direct_value, direct_frames, _ = _read_value(link, commands.service_direct_read(29))
    dump = _hexdump(z21_frames) + _hexdump(direct_frames)
    if z21_value is None or direct_value is None:
        return CheckResult("z21_cv_opcodes", None, "no value from one of the two reads", dump)
    if z21_value != direct_value:
        return CheckResult(
            "z21_cv_opcodes", None,
            f"reads disagree: Z21 {z21_value}, direct {direct_value}", dump,
        )
    return CheckResult(
        "z21_cv_opcodes", True, f"Z21 and direct reads of CV29 both returned {z21_value}", dump
    )


def _accepted(link: Link, payload: bytes, window: float = 2.0) -> tuple[bool | None, list]:
    """Did the station accept this command? True / False / None-for-unresolved.

    A transient condition must NOT be read as acceptance: a station that
    answers `61 1F` (busy) or `61 12` (short circuit) has told us nothing about
    whether it implements the opcode, so the capability stays unresolved.
    Returning True there would record an unsupported command as supported.
    """
    frames = link.exchange(payload, window=window)
    replies = [parse(f.telegram) for f in frames]
    if UNSUPPORTED in replies:
        return False, frames
    if SHORT_CIRCUIT in replies or BUSY in replies:
        return None, frames
    if not frames:
        return None, frames
    return True, frames


def read_f0(link: Link, address: int) -> tuple[bool | None, list[str]]:
    """Read the current F0 state for the locomotive.

    Returns (True, frames) if F0 is on, (False, frames) if F0 is off, or
    (None, frames) if the locomotive information request did not return a valid reply.
    """
    frames = link.exchange(commands.loco_info(address), window=1.5)
    dump = _hexdump(frames)

    for frame in frames:
        reply = parse(frame.telegram)
        if isinstance(reply, LocoInfo):
            return reply.f0, dump

    return None, dump


def check_single_function(link: Link, address: int, *, f0_is_on: bool) -> CheckResult:
    """R5. Commands F0 to the value it already holds, so a negative result changes nothing.

    The caller is responsible for reading the current F0 state first. This function
    takes a required keyword argument f0_is_on to re-assert F0's existing value.
    """
    action = 1 if f0_is_on else 0
    accepted, frames = _accepted(link, commands.single_function(address, 0, action=action))
    detail = {
        True: "station accepted E4 F8: single-function commands work, no shadow state needed",
        False: "station answered 61 82 to E4 F8: fall back to function group commands",
        None: "no reply to E4 F8; capability not established",
    }[accepted]
    return CheckResult("single_function_cmd", accepted, detail, _hexdump(frames))


def check_function_groups(link: Link, address: int) -> CheckResult:
    """Groups 4 (F13-F20) and F21-F28 (group 5). All bits zero, so nothing switches on."""
    g4, g4_frames = _accepted(link, commands.function_group(address, 0x23, 0x00))
    g5, g5_frames = _accepted(link, commands.function_group(address, 0x28, 0x00))
    dump = _hexdump(g4_frames) + _hexdump(g5_frames)
    # Three-valued AND (Kleene): a confirmed rejection of either group settles the
    # pair as unusable, even if the other group never answered. Testing for None
    # first would throw that knowledge away and report "unknown" for something we
    # already know is False.
    if g4 is False or g5 is False:
        value: bool | None = False
        detail = "at least one group rejected: F13-F28 unavailable on the group path"
    elif g4 is None or g5 is None:
        value = None
        detail = "no reply to E4 23 or E4 28"
    else:
        value = True
        detail = "groups 4 and 5 accepted: F13-F28 reachable"
    return CheckResult("function_groups_4_5", value, detail, dump)


DECODER_TYPES = {
    6: "MS450", 7: "MS990", 8: "MS590", 9: "MS950", 10: "MS560",
    11: "MS001", 12: "MS491", 13: "MS581", 14: "MS540", 15: "MS591",
}


def check_identity(link: Link) -> CheckResult:
    version_frames = link.exchange(commands.version(), window=2.0)
    version = next(
        (r for r in map(lambda f: parse(f.telegram), version_frames) if isinstance(r, Version)),
        None,
    )
    if version is None:
        return CheckResult("identity", None, "no version reply; is this the XpressNet port?",
                           _hexdump(version_frames))

    status_frames = link.exchange(commands.status(), window=2.0)
    status = next(
        (r for r in map(lambda f: parse(f.telegram), status_frames) if isinstance(r, Status)),
        None,
    )
    dump = _hexdump(version_frames) + _hexdump(status_frames)
    value = {
        "xpressnet": f"{version.xpressnet_major}.{version.xpressnet_minor}",
        "command_station_id": version.command_station_id,
        "status_raw": status.raw if status else None,
        "auto_start_mode": status.auto_start_mode if status else None,
        "emergency_off": status.emergency_off if status else None,
        "emergency_stop": status.emergency_stop if status else None,
        "service_mode": status.service_mode if status else None,
    }
    station_id_hex = f"0x{version.command_station_id:02X}"
    detail = f"XpressNet {value['xpressnet']}, command station id {station_id_hex}"
    if status and status.auto_start_mode:
        detail += (
            "; start mode is AUTOMATIC, so every locomotive resumes its last known speed "
            "when the station powers up - send an emergency stop before restoring track power"
        )
    return CheckResult("identity", value, detail, dump)


def check_address_band(link: Link, address: int) -> CheckResult:
    """Addresses 100..127 are the XpressNet/Z21 divergence band."""
    if not 100 <= address <= 127:
        return CheckResult("loco_address_threshold", None,
                           f"address {address} is outside the 100..127 divergence band", [])
    short_high, short_low = commands.loco_address_bytes(address, threshold=128)
    long_high, long_low = commands.loco_address_bytes(address, threshold=100)
    short_frames = link.exchange(bytes([0xE3, 0x00, short_high, short_low]), window=2.0)
    long_frames = link.exchange(bytes([0xE3, 0x00, long_high, long_low]), window=2.0)
    dump = _hexdump(short_frames) + _hexdump(long_frames)
    if bool(short_frames) == bool(long_frames):
        return CheckResult("loco_address_threshold", None,
                           "both encodings behaved identically; threshold not established", dump)
    threshold = 100 if long_frames else 128
    form = "long" if long_frames else "short"
    detail = f"only the {form} form answered; threshold is {threshold}"
    return CheckResult("loco_address_threshold", threshold, detail, dump)
