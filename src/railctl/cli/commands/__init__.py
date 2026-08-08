# src/railctl/cli/commands/__init__.py
"""Typer command modules.

Each module exposes `register(app: typer.Typer) -> None`. `cli/main.py` calls
every module's `register` once, in the order commands should appear in
`--help`.
"""

from __future__ import annotations
