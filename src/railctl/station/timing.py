"""Every timing budget the station facade uses, gathered in one injectable
place so a unit test can run the 95 s service-mode path in microseconds
against a fake clock instead of a real one.

`li_ack_normal` and `li_ack_programming` restate `link.DEFAULT_TIMEOUT` and
`link.PROGRAMMING_TIMEOUT` as data. The station always passes an explicit
timeout to `Link`, so in practice this table is authoritative; `link.py`'s
constants exist for callers with no station layer above them, and a test
pins the two pairs equal so they cannot drift apart unnoticed.

Every value below is measured against the YD7010, not guessed
(docs/probe-results.md, 2026-08-04): one service-mode read takes about 1.7 s,
comfortably inside `service_result`'s 95 s whole-operation budget, and the
per-attempt POM budget is well above the reply time of the opcodes that
answer at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class Timing:
    li_ack_normal: float = 5.0  # per-exchange budget in normal operation
    li_ack_programming: float = 95.0  # per-exchange budget once in service mode
    min_exchange: float = 0.05  # floor when clamping to whatever budget remains
    power_settle: float = 0.5
    pom_result: float = 2.0  # per attempt
    pom_poll_interval: float = 0.10
    pom_read_attempts: int = 3
    pom_retry_delay: float = 0.25
    pom_write_settle: float = 0.5  # floor only - nothing reports track delivery
    service_result: float = 95.0  # whole-operation budget
    service_first_poll_delay: float = 0.20
    service_poll_interval: float = 0.50  # minimum gap between polls, not a deadline
    service_ready_limit: int = 8
    service_exit_settle: float = 0.10
    page_cache_ttl: float = 10.0


TIMING: Final[Timing] = Timing()
