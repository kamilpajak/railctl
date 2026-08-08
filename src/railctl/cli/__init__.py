"""railctl.cli - Typer commands, output rendering, and exception-to-exit-code mapping.

Nothing under this package touches an X-Bus opcode, a framing byte, a port name or a network
address - `tests/test_layering.py` rule 1 enforces that mechanically. Every command talks to a
`Station` facade object and to the modules in this package: `result` for the one shared envelope
type, `render` for turning it into bytes, `_errors` for the exception-to-exit-code decorator that
every command function is wrapped in.
"""

from __future__ import annotations
