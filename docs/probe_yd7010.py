#!/usr/bin/env python3
"""
Sonda identyfikacyjna portów USB YD7010 — TYLKO ODCZYT.

Wysyla wylacznie dwa zapytania XpressNet, oba bezstanowe i nieszkodliwe:
  0x21 0x21 0x00  - "Command station software version"
  0x21 0x24 0x05  - "Command station status"

NIE wlacza/wylacza toru, NIE rusza lokomotywami, NIE zapisuje zadnego CV.

Sprawdza kazdy port w dwoch wariantach ramkowania:
  A) "bare"   - <header> <data...> <XOR>            (styl LI101F / RS-232)
  B) "li-usb" - FF FE <header> <data...> <XOR>      (styl Lenz 23151 LI-USB)

Uzycie:  python3 probe_yd7010.py
"""

import glob
import os
import select
import sys
import termios
import time

BAUD = termios.B57600  # USB CDC i tak zwykle ignoruje, ale zgodnie z konwencja DR5000/LI-USB
READ_TIMEOUT = 1.5     # sekundy na odpowiedz
LI_USB_PREFIX = b"\xff\xfe"


def xor_frame(payload: bytes) -> bytes:
    """Dokleja bajt XOR liczony po calym telegramie XpressNet."""
    x = 0
    for b in payload:
        x ^= b
    return payload + bytes([x])


# Telegramy sond (bez bajtu XOR - dokladany nizej)
PROBES = {
    "get_version": b"\x21\x21",
    "get_status": b"\x21\x24",
}


def open_raw(path: str) -> int:
    """Otwiera port szeregowy w trybie raw. Zwraca deskryptor."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs

    iflag = 0
    oflag = 0
    lflag = 0
    cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
    cc = list(cc)
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0

    termios.tcsetattr(
        fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, BAUD, BAUD, cc]
    )
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def read_for(fd: int, seconds: float) -> bytes:
    """Zbiera wszystko, co przyjdzie w zadanym oknie czasowym."""
    deadline = time.monotonic() + seconds
    buf = b""
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        r, _, _ = select.select([fd], [], [], max(0.0, min(0.2, remaining)))
        if r:
            try:
                chunk = os.read(fd, 256)
            except BlockingIOError:
                continue
            if chunk:
                buf += chunk
    return buf


def decode_version_reply(data: bytes) -> str:
    """Rozpoznaje odpowiedz 0x63 0x21 <wersja> <id centrali> <XOR>."""
    for i in range(len(data) - 4):
        if data[i] == 0x63 and data[i + 1] == 0x21:
            ver, cs_id = data[i + 2], data[i + 3]
            return (
                f"XpressNet v{ver >> 4}.{ver & 0x0F} "
                f"(0x{ver:02X}), command station ID 0x{cs_id:02X}"
            )
    return ""


def probe_port(path: str) -> None:
    print(f"\n=== {path} ===")
    try:
        fd = open_raw(path)
    except OSError as exc:
        print(f"  nie udalo sie otworzyc: {exc}")
        return

    try:
        for framing, prefix in (("bare", b""), ("li-usb", LI_USB_PREFIX)):
            for name, payload in PROBES.items():
                frame = prefix + xor_frame(payload)
                termios.tcflush(fd, termios.TCIOFLUSH)
                try:
                    os.write(fd, frame)
                except OSError as exc:
                    print(f"  [{framing}/{name}] blad zapisu: {exc}")
                    continue

                reply = read_for(fd, READ_TIMEOUT)
                if not reply:
                    print(f"  [{framing:6}/{name:11}] -> (cisza)")
                    continue

                pretty = " ".join(f"{b:02X}" for b in reply)
                print(f"  [{framing:6}/{name:11}] -> {pretty}")
                decoded = decode_version_reply(reply)
                if decoded:
                    print(f"      ^^ ODPOWIEDZ XPRESSNET: {decoded}")
    finally:
        os.close(fd)


def main() -> int:
    ports = sorted(glob.glob("/dev/cu.usbmodem7010*"))
    if not ports:
        print("Nie znaleziono portow /dev/cu.usbmodem7010* — czy YD7010 jest podlaczony?")
        return 1

    print("Sonda YD7010 — tylko zapytania odczytowe (wersja + status).")
    print(f"Znalezione porty: {len(ports)}")
    for path in ports:
        probe_port(path)

    print(
        "\nPort, ktory odpowiedzial ramka zaczynajaca sie od 0x63 0x21, to XpressNet.\n"
        "Wariant ramkowania ('bare' albo 'li-usb'), przy ktorym przyszla odpowiedz,\n"
        "to ten, ktorego trzeba uzyc w CLI."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
