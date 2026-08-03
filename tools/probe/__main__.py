"""Run the YD7010 capability probe.

Read-only: no decoder CV is ever written. Function checks command a function to
the value it already holds, so nothing on the layout changes.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys

from tools.probe import checks, report
from tools.probe.commands import version as version_cmd
from tools.probe.link import SerialLink, discover_ports
from tools.probe.replies import Version, parse


def find_xpressnet_port() -> str:
    """The XpressNet port is the one that answers the version request."""
    candidates = discover_ports()
    if not candidates:
        raise SystemExit("no /dev/cu.usbmodem7010* ports found; is the YD7010 connected?")
    for path in candidates:
        try:
            with SerialLink(path) as link:
                frames = link.exchange(version_cmd(), window=1.5)
        except OSError:
            continue
        if any(isinstance(parse(f.telegram), Version) for f in frames):
            return path
    raise SystemExit(
        "none of these ports answered a version request: "
        + ", ".join(candidates)
        + "\nIs the YaMoRC tool holding the port open?"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.probe")
    parser.add_argument("--port", help="serial port; auto-detected when omitted")
    parser.add_argument("--address", type=int, default=3, help="locomotive address (default 3)")
    parser.add_argument("--band-address", type=int, help="an address in 100..127 to test the band")
    parser.add_argument(
        "--no-programming-track", action="store_true", help="skip the service-mode checks R2 and R4"
    )
    parser.add_argument("--format", choices=("human", "json", "markdown"), default="human")
    args = parser.parse_args(argv)

    port = args.port or find_xpressnet_port()
    run_at = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    results = []
    with SerialLink(port) as link:
        results.append(checks.check_identity(link))
        results.append(checks.check_pom_read(link, args.address, poll=True))
        results.append(checks.check_single_function(link, args.address))
        results.append(checks.check_function_groups(link, args.address))
        if args.band_address:
            results.append(checks.check_address_band(link, args.band_address))
        if not args.no_programming_track:
            results.append(checks.check_service_ext_cv(link))
            results.append(checks.check_z21_opcodes(link))

    if args.format == "json":
        print(report.to_json(results, port=port, run_at=run_at))
    elif args.format == "markdown":
        print(report.to_markdown(results, port=port, run_at=run_at))
    else:
        print(f"port {port}")
        for result in results:
            print(f"  {result.name:24} {result.value!r}")
            print(f"  {'':24} {result.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
