"""Capability checks. Every check is read-only: no decoder CV is ever written."""

from __future__ import annotations

from dataclasses import dataclass, field

from tools.probe import commands
from tools.probe.link import Link
from tools.probe.replies import (
    NO_ACK,
    TRANSIENT,
    UNSUPPORTED,
    CvValue,
    FunctionState13To28,
    LocoInfo,
    RegisterValue,
    Status,
    Version,
    parse,
)

POM_WINDOW = 5.0


def _hexdump(frames) -> list[str]:
    return [f"{'FE' if f.solicited else 'FD'} {f.telegram.hex(' ').upper()}" for f in frames]


@dataclass
class CheckResult:
    name: str
    value: object | None
    detail: str
    frames: list[str] = field(default_factory=list)


def _transient(replies: list) -> object | None:
    """The transient marker present in these replies, or None."""
    return next((marker for marker in TRANSIENT if marker in replies), None)


def _unresolved() -> dict[str, object | None]:
    """The R1 result dict with nothing established. Used for every path that
    does not reach a verdict, so `CheckResult.value` for check_pom_read is
    always a dict and never a bare None."""
    return {
        "pom_read": None,
        "pom_result_channel": "none",
        "pom_echo_zero_based": None,
        "pom_value": None,
    }


def check_pom_read(link: Link, address: int, cv: int = 8, *, poll: bool) -> CheckResult:
    """R1. CV8 is used by default because its ZIMO value is a known constant (145),
    so a plausible-but-wrong reading is detectable."""
    wire = commands.cv_wire(cv)

    # Drain first. 0x21 0x10 returns the station's STORED service-mode result, and
    # that store lives in the command station, not in this process - flushing the
    # host file descriptor does not clear it. A probe run after any service-mode
    # read could otherwise poll up a leftover value and record pom_read as True on
    # hardware that cannot do it at all. Whatever this discards belongs to an
    # earlier operation by definition, because the POM read has not been sent yet.
    if poll:
        link.exchange(commands.service_result(), window=1.0)

    frames = link.exchange(commands.pom_read(address, cv), window=POM_WINDOW)
    channel = "broadcast"

    # 0x21 0x10 is specified as the SERVICE mode result request (Lenz section
    # 2.2.10); using it to collect a POM result is speculative, which is exactly
    # why the channel that produced the value is recorded rather than assumed.
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
            else:
                # The reply is for a different CV than the one asked for, so it is
                # not an answer to this request. Treat it as nothing established
                # rather than reporting another CV's value under this CV's name.
                return CheckResult(
                    "pom_read",
                    _unresolved(),
                    f"a CV result came back but its echo is {reply.raw_cv}, which is"
                    f" neither CV{cv} nor its zero-based form {wire}: not an answer"
                    " to this request",
                    dump,
                )
            return CheckResult(
                "pom_read",
                {
                    "pom_read": True,
                    "pom_result_channel": channel,
                    "pom_echo_zero_based": echo_zero_based,
                    "pom_value": reply.value,
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
                "pom_value": None,
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
                "pom_value": None,
            },
            "decoder did not acknowledge: check RailCom"
            " (CV29 bit 3 = 1, CV28 bits 0 and 1 set) and retry",
            dump,
        )
    # Transient station conditions. Neither proves anything about the capability,
    # so pom_read stays None — but the dict shape is kept so every caller can
    # subscript CheckResult.value without a type check.
    transient = _transient(seen)
    if transient is not None:
        return CheckResult(
            "pom_read",
            _unresolved(),
            f"station reported {transient.name}; nothing established, retry",
            dump,
        )

    # Silence establishes nothing, so it must not be reported as a definite
    # "not supported". XpressNet section 2.2.23 states that a command station
    # which does not support Operations Mode Programming answers 61 82, and
    # Lenz 23151 section 1.4 states that "command not available" is always
    # coupled to the command that caused it, even for commands that normally
    # produce no reply. Non-support therefore has its own signal, and silence
    # is not it: it is equally produced by a result form this probe cannot
    # parse, by a missing RailCom receiver, or by the loco not being on the
    # track at all.
    return CheckResult(
        "pom_read",
        _unresolved(),
        "no reply of any kind, and neither 61 13 nor 61 82: nothing established."
        " An unsupported station is specified to answer 61 82, so silence points"
        " elsewhere - check that the locomotive is on a powered main track at this"
        " address, that RailCom is on (CV29 bit 3, CV28 bits 0 and 1), and inspect"
        " the raw frames for a result form this probe does not recognise",
        dump,
    )


SERVICE_WINDOW = 8.0


@dataclass(frozen=True)
class ReadOutcome:
    """What a single CV read attempt established.

    status is one of:
      "ok"                a CV value came back
      "unsupported"       the station answered 61 82
      "register_fallback" the station answered 63 10, meaning the decoder does
                          not support Direct Mode and the station dropped to
                          Register/Paged mode. The number in that reply is a
                          register, not a CV, so it is NOT a CV value.
      "transient"         busy, short circuit or transfer error
      "silent"            nothing came back
    """

    status: str
    value: int | None
    reply_cv: int | None
    frames: list


SERVICE_POLLS = 4
SERVICE_POLL_WINDOW = 3.0


def _read_value(link: Link, payload: bytes) -> ReadOutcome:
    """Send a service-mode read and collect the result, polling if needed.

    The poll is not optional. Service-mode results are asynchronous, and the
    two opcode families differ in how they deliver them: measured on the YD7010
    on 2026-08-04, 0x23 0x11 returns its result unsolicited, while 0x22 0x15,
    0x22 0x18 and 0x22 0x19 return NOTHING until asked with 0x21 0x10 and then
    answer correctly every time.

    Without the poll this function reported the whole Lenz opcode family as
    silent, and that silence was read as "this station does not implement
    them" - a capability declared absent because of a defect in the instrument
    measuring it. That is the exact failure this probe exists to prevent.
    """
    frames = link.exchange(payload, window=SERVICE_WINDOW)
    for _ in range(SERVICE_POLLS):
        replies = [parse(f.telegram) for f in frames]
        for reply in replies:
            if isinstance(reply, CvValue):
                return ReadOutcome("ok", reply.value, reply.cv, frames)
        if UNSUPPORTED in replies:
            return ReadOutcome("unsupported", None, None, frames)
        if any(isinstance(reply, RegisterValue) for reply in replies):
            return ReadOutcome("register_fallback", None, None, frames)
        polled = link.exchange(commands.service_result(), window=SERVICE_POLL_WINDOW)
        if not polled:
            break
        frames = frames + polled

    replies = [parse(f.telegram) for f in frames]
    for reply in replies:
        if isinstance(reply, CvValue):
            return ReadOutcome("ok", reply.value, reply.cv, frames)
    if UNSUPPORTED in replies:
        return ReadOutcome("unsupported", None, None, frames)
    if any(isinstance(reply, RegisterValue) for reply in replies):
        return ReadOutcome("register_fallback", None, None, frames)
    if _transient(replies) is not None:
        return ReadOutcome("transient", None, None, frames)
    return ReadOutcome("silent", None, None, frames)


# CV265 is the ZIMO sound-project/loco-type selector. It is used as the high-band
# probe because it exists on the MS decoder on this layout and sits in the
# 256-511 band, which is where the CVs railctl actually needs to back up live
# (265, 266, 273-277, 287, 288, 313, 314, 395-397).
HIGH_BAND_CV = 265

# A CV whose correct value is known independently of any opcode. CV8 carries the
# NMRA manufacturer id and reads 145 on every ZIMO decoder.
#
# Anchoring on this rather than on a sibling opcode is deliberate. The checks used
# to validate one opcode by comparing it against another, so when the comparison
# opcode returned nothing the check reported "unknown" for a capability it had
# just demonstrated - the instrument recording its own gap as a fact about the
# hardware. That is the same failure as the missing 21 10 poll. A constant cannot
# fail to answer.
#
# It also catches what a peer comparison cannot: an off-by-one in the CV encoding
# returns a plausible number from the wrong CV, and two opcodes sharing the bug
# would agree with each other.
REFERENCE_CV = 8
REFERENCE_VALUE = 145


def _verdict(outcome: ReadOutcome, cv: int, expected: int | None) -> tuple[bool | None, str]:
    """Grade one read against a known constant. Only contradiction may say False."""
    if outcome.status == "unsupported":
        return False, "station answered 61 82: opcode not implemented"
    if outcome.status == "register_fallback":
        return None, "station fell back to Register/Paged mode; no CV value established"
    if outcome.status != "ok":
        return None, f"no value came back ({outcome.status})"
    if outcome.reply_cv is not None and outcome.reply_cv != cv:
        return None, f"reply decodes as CV{outcome.reply_cv}, not the CV{cv} requested"
    if expected is not None and outcome.value != expected:
        return None, f"CV{cv} read back {outcome.value}, expected the known {expected}"
    return True, f"CV{cv} read back {outcome.value}"


def check_service_ext_cv(link: Link, high_cv: int = HIGH_BAND_CV) -> CheckResult:
    """R2. Two separate questions, each anchored on evidence that cannot fail to answer.

    1. Does the 4-byte format work at all? Read the reference CV with 0x22 0x18 and
       require the known constant back.
    2. Do the bands ABOVE CV255 work? Band 0x18 overlaps the legacy opcode, so a
       station could implement it and still reject 0x19. Only this second question
       decides whether railctl can reach the ZIMO CVs it needs. There is no known
       constant up there, so the check requires the reply to decode back to the CV
       that was asked for.

    Neither question is answered by comparing one opcode against another.
    """
    low = _read_value(link, commands.service_ext_read(REFERENCE_CV))
    low_ok, low_detail = _verdict(low, REFERENCE_CV, REFERENCE_VALUE)

    if low_ok is not True:
        return CheckResult(
            "service_ext_cv",
            {
                "service_ext_cv": low_ok,
                "service_ext_cv_high_band": None,
                "service_ext_cv_high_value": None,
            },
            f"22 18: {low_detail}; the high band was not probed",
            _hexdump(low.frames),
        )

    high = _read_value(link, commands.service_ext_read(high_cv))
    high_ok, high_detail = _verdict(high, high_cv, None)
    dump = _hexdump(low.frames) + _hexdump(high.frames)
    return CheckResult(
        "service_ext_cv",
        {
            "service_ext_cv": True,
            "service_ext_cv_high_band": high_ok,
            "service_ext_cv_high_value": high.value,
        },
        f"22 18: {low_detail}. 22 19 for CV{high_cv}: {high_detail}",
        dump,
    )


def check_z21_opcodes(link: Link) -> CheckResult:
    """R4. Only the READ opcode 23 11 is probed. Never 24 12, which would write.

    Validated against the reference constant, not against another opcode: a silent
    peer must never downgrade a capability this check has demonstrated.
    """
    z21 = _read_value(link, commands.z21_service_read(REFERENCE_CV))
    ok, detail = _verdict(z21, REFERENCE_CV, REFERENCE_VALUE)
    return CheckResult("z21_cv_opcodes", ok, f"23 11: {detail}", _hexdump(z21.frames))


def _accepted(link: Link, payload: bytes, window: float = 2.0) -> tuple[bool | None, list]:
    """Did the station accept this command? True / False / None-for-unresolved.

    A transient condition must NOT be read as acceptance. A station answering
    `61 1F` (programming busy), `61 12` (short circuit), `61 81` (command
    station busy) or `61 80` (transfer error) has told us nothing about whether
    it implements the opcode, so the capability stays unresolved. Returning True
    there would record an unsupported command as supported — which is exactly
    what happened while 61 80 and 61 81 went unparsed and fell through to the
    "some frame came back, so it must be accepted" branch below.
    """
    frames = link.exchange(payload, window=window)
    replies = [parse(f.telegram) for f in frames]
    if UNSUPPORTED in replies:
        return False, frames
    if _transient(replies) is not None:
        return None, frames
    if not frames:
        return None, frames
    return True, frames


def read_loco_info(link: Link, address: int) -> tuple[LocoInfo | None, list]:
    """Request locomotive information. Frames are raw Frame objects, not hex-dumped.

    Note what this reply cannot do: XpressNet section 2.1.14.1 defines it as
    `0xE4 <0000 BFFF> <speed> <FA> <FB>` — there is no address in it. So the
    answer cannot be verified against the address that was asked for; the only
    correlation is request/response ordering on a link used by one client.
    """
    frames = link.exchange(commands.loco_info(address), window=1.5)
    for frame in frames:
        reply = parse(frame.telegram)
        if isinstance(reply, LocoInfo):
            return reply, frames
    return None, frames


def read_f0(link: Link, address: int) -> tuple[bool | None, list]:
    """Current F0 state, or (None, frames) when no valid reply came back."""
    info, frames = read_loco_info(link, address)
    return (None if info is None else info.f0), frames


def check_loco_info(link: Link, address: int) -> tuple[CheckResult, LocoInfo | None, list]:
    """Report what the locomotive information reply already tells us.

    The speed step mode answers a question the design left open, and the busy
    flag says whether another throttle is holding this locomotive — which is
    the one situation where re-asserting F0 in R5 could race someone else.
    """
    info, frames = read_loco_info(link, address)
    dump = _hexdump(frames)
    if info is None:
        detail = f"no locomotive information for address {address}"
        return CheckResult("loco_info", None, detail, dump), None, frames
    value = {
        "speed_step_mode": info.speed_step_mode,
        "loco_busy": info.busy,
        "f0": info.f0,
    }
    steps = info.speed_step_mode or "reserved bit pattern"
    detail = f"address {address}: {steps} speed steps, F0 {'on' if info.f0 else 'off'}"
    if info.busy:
        detail += "; another XpressNet device is controlling this locomotive"
    return CheckResult("loco_info", value, detail, dump), info, frames


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


def read_function_state_13_28(link: Link, address: int) -> tuple[FunctionState13To28 | None, list]:
    """Current ON/OFF state of F13-F28, or (None, frames) if it could not be read."""
    frames = link.exchange(commands.function_state_13_28(address), window=1.5)
    for frame in frames:
        reply = parse(frame.telegram)
        if isinstance(reply, FunctionState13To28):
            return reply, frames
    return None, frames


def check_function_groups(link: Link, address: int, *, f13_f20: int, f21_f28: int) -> CheckResult:
    """Groups 4 (F13-F20) and 5 (F21-F28), re-asserting the values they already hold.

    Sending all-zero bits would switch OFF every function currently on in
    F13-F28 - on a sound locomotive that is most of the interesting ones. The
    caller must read the current state first and pass it here, exactly as the
    R5 check requires the current F0 state. The two bytes come off the wire in
    the layout these commands expect, so they can be handed straight back.
    """
    g4, g4_frames = _accepted(link, commands.function_group(address, 0x23, f13_f20))
    g5, g5_frames = _accepted(link, commands.function_group(address, 0x28, f21_f28))
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
    6: "MS450",
    7: "MS990",
    8: "MS590",
    9: "MS950",
    10: "MS560",
    11: "MS001",
    12: "MS491",
    13: "MS581",
    14: "MS540",
    15: "MS591",
}


def check_identity(link: Link) -> CheckResult:
    version_frames = link.exchange(commands.version(), window=2.0)
    version = next(
        (r for r in map(lambda f: parse(f.telegram), version_frames) if isinstance(r, Version)),
        None,
    )
    if version is None:
        return CheckResult(
            "identity",
            None,
            "no version reply; is this the XpressNet port?",
            _hexdump(version_frames),
        )

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
        return CheckResult(
            "loco_address_threshold",
            None,
            f"address {address} is outside the 100..127 divergence band",
            [],
        )
    short_high, short_low = commands.loco_address_bytes(address, threshold=128)
    long_high, long_low = commands.loco_address_bytes(address, threshold=100)
    short_frames = link.exchange(bytes([0xE3, 0x00, short_high, short_low]), window=2.0)
    long_frames = link.exchange(bytes([0xE3, 0x00, long_high, long_low]), window=2.0)
    dump = _hexdump(short_frames) + _hexdump(long_frames)

    # "Some frame came back" is not an answer. A 61 82 rejection is a frame, and
    # so is an unsolicited broadcast; counting either as a successful reply makes
    # the only informative outcome — one form answering and the other not —
    # indistinguishable from both forms answering.
    def answered(frames: list) -> bool:
        return any(isinstance(parse(f.telegram), LocoInfo) for f in frames)

    short_ok, long_ok = answered(short_frames), answered(long_frames)
    if short_ok == long_ok:
        both = (
            "both forms returned locomotive information"
            if short_ok
            else ("neither form returned locomotive information")
        )
        return CheckResult(
            "loco_address_threshold", None, f"{both}; threshold not established", dump
        )
    threshold = 100 if long_ok else 128
    form = "long" if long_ok else "short"
    detail = f"only the {form} form returned locomotive information; threshold is {threshold}"
    return CheckResult("loco_address_threshold", threshold, detail, dump)
