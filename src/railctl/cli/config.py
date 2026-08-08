# src/railctl/cli/config.py
"""Config file loading and the generic CLI-flag/env/file/default precedence.

`~/.config/railctl/config.toml` carries three keys and nothing else - target,
address, verbose (design spec L3). A missing file is not an error: most
invocations have none. A file that IS there and IS wrong - bad TOML, an
unknown key, a value outside its bound - is exit 2, and the message always
names the file, the line and the key.

`pick()` is the one place the four-level precedence (CLI flag > environment >
config file > built-in default) is decided, generically, so `cli/deps.py`
calls it once per key instead of writing the same if/elif/else four times
over.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from railctl.xbus.address import LOCO_ADDR_MAX, LOCO_ADDR_MIN

CONFIG_KEYS: Final[tuple[str, ...]] = ("target", "address", "verbose")
DEFAULT_TARGET: Final[str] = "auto"

_APP_DIRNAME: Final[str] = "railctl"
_XDG_FALLBACK: Final[str] = ".config"
_CONFIG_FILENAME: Final[str] = "config.toml"
_CAPABILITIES_FILENAME: Final[str] = "capabilities.json"

_TOML_LINE_RE: Final[re.Pattern[str]] = re.compile(r"line (\d+)")


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    """`$XDG_CONFIG_HOME/railctl`, or `~/.config/railctl` when unset.

    `env` defaults to the real process environment so the CLI's own entry
    point needs no special case; every test in this task passes a mapping
    with `XDG_CONFIG_HOME` already pointing at a `tmp_path`, which is what
    keeps the suite from ever touching a real home directory.
    """
    mapping = os.environ if env is None else env
    xdg = mapping.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / _XDG_FALLBACK
    return base / _APP_DIRNAME


def config_path(env: Mapping[str, str] | None = None) -> Path:
    return config_dir(env) / _CONFIG_FILENAME


def capabilities_path(env: Mapping[str, str] | None = None) -> Path:
    # The ONLY definition of this function in the whole plan. Task 12's
    # `doctor` command imports it from here (`from railctl.cli.config import
    # capabilities_path`) rather than defining its own - a second copy that
    # drifts to a different XDG fallback is exactly how two commands would end
    # up reading two different `capabilities.json` files on the same machine.
    return config_dir(env) / _CAPABILITIES_FILENAME


@dataclass(frozen=True, slots=True)
class Config:
    target: str = DEFAULT_TARGET
    address: int | None = None
    verbose: int = 0


def _line_for_key(text: str, key: str) -> int:
    """First line whose left-hand side is `key`, or line 1 when not found.

    "Not found" only happens for a key tomllib never actually accepted (a
    syntax error before the key was even parsed), so line 1 is the closest
    honest answer, not a guess dressed up as one.
    """
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for number, line in enumerate(text.splitlines(), start=1):
        if pattern.match(line):
            return number
    return 1


def _config_error(path: Path, *, line: int, key: str, detail: str) -> ValueError:
    return ValueError(f"{path}: line {line}: key {key!r}: {detail}")


def load_config(path: Path) -> Config:
    """Missing file -> defaults. Bad file -> `ValueError` naming file, line, key."""
    if not path.exists():
        return Config()
    text = path.read_text(encoding="utf-8")
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        match = _TOML_LINE_RE.search(str(exc))
        line = int(match.group(1)) if match else 1
        lines = text.splitlines()
        offending = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        # A syntax error still names a "key": the text left of the first `=`
        # on the offending line, or the whole stripped line when there is no
        # `=` at all (a stray bracket, a missing quote). Either way the file,
        # the line and something to grep for all land in one message.
        key = offending.split("=", 1)[0].strip() or offending or "<syntax>"
        raise _config_error(path, line=line, key=key, detail=f"invalid TOML ({exc})") from exc

    for key in raw:
        if key not in CONFIG_KEYS:
            raise _config_error(
                path,
                line=_line_for_key(text, key),
                key=key,
                detail=f"not a recognised config key (expected one of {CONFIG_KEYS})",
            )

    target = raw.get("target", DEFAULT_TARGET)
    if not isinstance(target, str):
        raise _config_error(
            path,
            line=_line_for_key(text, "target"),
            key="target",
            detail=f"must be a string, got {target!r}",
        )

    address = raw.get("address")
    if address is not None:
        if not isinstance(address, int) or isinstance(address, bool):
            raise _config_error(
                path,
                line=_line_for_key(text, "address"),
                key="address",
                detail=f"must be an integer, got {address!r}",
            )
        if not LOCO_ADDR_MIN <= address <= LOCO_ADDR_MAX:
            raise _config_error(
                path,
                line=_line_for_key(text, "address"),
                key="address",
                detail=f"{address} is outside {LOCO_ADDR_MIN}..{LOCO_ADDR_MAX}",
            )

    verbose = raw.get("verbose", 0)
    if not isinstance(verbose, int) or isinstance(verbose, bool) or verbose < 0:
        raise _config_error(
            path,
            line=_line_for_key(text, "verbose"),
            key="verbose",
            detail=f"must be a non-negative integer, got {verbose!r}",
        )

    return Config(target=target, address=address, verbose=verbose)


def pick(
    flag: object | None,
    env_value: str | None,
    config_value: object | None,
    default: object,
    *,
    name: str,
    cast: Callable[[str], object],
) -> object:
    """CLI flag > environment > config file > built-in default, for one key.

    The four levels are separate arguments, not a single merged mapping, so a
    caller cannot let one key's environment value leak into another key's
    decision - the quiet cross-talk between two measurements that were
    supposed to stay separate is exactly the failure mode this project exists
    to catch.
    """
    if flag is not None:
        return flag
    if env_value is not None:
        try:
            return cast(env_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"environment variable RAILCTL_{name.upper()}={env_value!r} is invalid: {exc}"
            ) from exc
    if config_value is not None:
        return config_value
    return default
