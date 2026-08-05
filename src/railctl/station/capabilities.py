"""What this station has been measured to do, kept as three-valued facts.

`None` means "not established" - never "no". A capability becomes `False`
only when the station gave a real negative answer (`61 82`, Unsupported) or a
`railctl doctor` check exhausted every alternative; everything else stays
`None` until something measures it. Collapsing "never asked" into "no" is the
recorded failure mode this whole package exists to avoid, and it is why every
field below defaults to `None`, not `False`.

The file is shaped `{"version": 1, "links": {"<identity>": {...}}}`, one entry
per `Link.identity`. The key is never a hardware serial number: the identity
that names a serial link comes from the USB descriptor, not from any
telegram, and a network link has no descriptor to read one from at all.
`UNKNOWN_IDENTITY` is what a transport reports when it cannot produce a
stable identity of its own, and `save()` refuses to persist it - see below.

Path resolution is deliberately NOT this module's job. `Station.open` takes
an explicit `capabilities_path`, and the CLI computes the default one; a
second place computing that default is how the two drift apart the first
time either one changes.

`notes` is a JSON list on disk. A hand-edited file that holds a bare string
instead loads as a one-element tuple rather than being rejected or walked
character by character - see `Capabilities._notes_from`.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Final, Literal

from railctl.errors import RailctlError

ResultChannel = Literal["broadcast", "poll", "none"]

CAPABILITIES_VERSION: Final[int] = 1
UNKNOWN_IDENTITY: Final[str] = "unknown"

# The only fields a normal operation - never a `doctor` probe - is allowed to
# learn on its own, because establishing anything else means sending an
# opcode a normal operation would never send. `with_learned` itself does not
# enforce this; it is the facade's job, and this set is what the facade
# checks against before calling `with_learned`.
LEARNABLE_FIELDS: Final[frozenset[str]] = frozenset(
    {"pom_read", "pom_result_channel", "pom_echo_zero_based", "service_direct_cv"}
)

_BOOL_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "pom_read",
        "pom_echo_zero_based",
        "service_direct_cv",
        "service_ext_cv",
        "z21_cv_opcodes",
        "function_groups_4_5",
        "single_function_cmd",
    }
)
_INT_FIELDS: Final[frozenset[str]] = frozenset({"command_station_id", "loco_address_threshold"})
_STR_FIELDS: Final[frozenset[str]] = frozenset({"xpressnet_version", "probed_at"})
_RESULT_CHANNELS: Final[frozenset[str]] = frozenset({"broadcast", "poll", "none"})

_DELETE_AND_RERUN_HINT: Final[str] = "delete {path} and run `railctl doctor` again"


def _malformed(path: Path, message: str) -> RailctlError:
    return RailctlError(message, hint=_DELETE_AND_RERUN_HINT.format(path=path))


@dataclass(frozen=True, slots=True)
class Capabilities:
    """One station's measured capabilities. Every field but `link_identity`
    and `notes` is a tri-state: `True`, `False`, or `None` for "not
    established"."""

    link_identity: str
    probed_at: str | None = None
    xpressnet_version: str | None = None
    command_station_id: int | None = None
    pom_read: bool | None = None
    pom_result_channel: ResultChannel | None = None
    pom_echo_zero_based: bool | None = None
    loco_address_threshold: int | None = None
    service_direct_cv: bool | None = None
    service_ext_cv: bool | None = None
    z21_cv_opcodes: bool | None = None
    function_groups_4_5: bool | None = None
    single_function_cmd: bool | None = None
    notes: tuple[str, ...] = ()

    @classmethod
    def unknown(cls, identity: str) -> Capabilities:
        """Every capability `None`, nothing probed - the starting point for a
        station that has never been measured, and the only shape `save()`
        refuses to persist."""
        return cls(link_identity=identity)

    @classmethod
    def load(cls, path: Path, identity: str) -> Capabilities:
        """Read `identity`'s entry from `path`, or `unknown(identity)` if the
        file or the entry is absent. Raises `RailctlError` on anything that
        looks wrong rather than guessing - a silently discarded measurement
        is exactly the failure mode this file format exists to prevent."""
        if not path.exists():
            return cls.unknown(identity)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise _malformed(path, f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("version") != CAPABILITIES_VERSION:
            raise _malformed(
                path, f"{path} is not a version {CAPABILITIES_VERSION} capabilities file"
            )
        links = raw.get("links")
        if not isinstance(links, dict):
            raise _malformed(path, f'{path} is missing its "links" table')
        entry = links.get(identity)
        if entry is None:
            return cls.unknown(identity)
        if not isinstance(entry, dict):
            raise _malformed(path, f"{path}: the entry for {identity!r} is not an object")
        return cls(link_identity=identity, **cls._fields_from(entry, identity, path))

    @classmethod
    def _fields_from(cls, entry: dict[str, object], identity: str, path: Path) -> dict[str, object]:
        """Recognised keys only. An unrecognised key is ignored, so a newer
        railctl reading an older file - or the reverse - never fails on that
        alone. A recognised key with the wrong type DOES fail: silently
        coercing `"pom_read": "yes"` to a boolean is the measurement
        corruption this module exists to catch."""
        kwargs: dict[str, object] = {}
        for name in _BOOL_FIELDS:
            if name not in entry:
                continue
            value = entry[name]
            if value is not None and not isinstance(value, bool):
                raise _malformed(
                    path, f"{path}: {identity!r}.{name} must be a boolean or null, got {value!r}"
                )
            kwargs[name] = value
        for name in _INT_FIELDS:
            if name not in entry:
                continue
            value = entry[name]
            if value is not None and not isinstance(value, int):
                raise _malformed(
                    path, f"{path}: {identity!r}.{name} must be an integer or null, got {value!r}"
                )
            kwargs[name] = value
        for name in _STR_FIELDS:
            if name not in entry:
                continue
            value = entry[name]
            if value is not None and not isinstance(value, str):
                raise _malformed(
                    path, f"{path}: {identity!r}.{name} must be a string or null, got {value!r}"
                )
            kwargs[name] = value
        if "pom_result_channel" in entry:
            value = entry["pom_result_channel"]
            if value is not None and value not in _RESULT_CHANNELS:
                raise _malformed(
                    path,
                    f"{path}: {identity!r}.pom_result_channel must be one of "
                    f"{sorted(_RESULT_CHANNELS)} or null, got {value!r}",
                )
            kwargs["pom_result_channel"] = value
        if "notes" in entry:
            kwargs["notes"] = cls._notes_from(entry["notes"], identity, path)
        return kwargs

    @staticmethod
    def _notes_from(value: object, identity: str, path: Path) -> tuple[str, ...]:
        if isinstance(value, str):
            # A hand-written file holding one bare string, not a list - see
            # the module docstring. One note, never split into characters.
            return (value,)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return tuple(value)
        raise _malformed(path, f"{path}: {identity!r}.notes must be a string or a list of strings")

    def save(self, path: Path) -> bool:
        """Write this station's entry into `path`, merged with whatever else
        is already there, atomically. Returns `False` and touches nothing
        when `link_identity` is `UNKNOWN_IDENTITY`: an identity with no
        stable name has nowhere safe to persist to, and inventing a key
        would silently merge two different stations' facts together."""
        if self.link_identity == UNKNOWN_IDENTITY:
            return False
        payload = {"version": CAPABILITIES_VERSION, "links": self._merged_links(path)}
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            # os.replace is atomic on the same filesystem: a reader never
            # sees a half-written file, and a process killed mid-write
            # leaves only the abandoned temp file behind, never a truncated
            # capabilities.json.
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return True

    def _merged_links(self, path: Path) -> dict[str, object]:
        links: dict[str, object] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raw = None
            if isinstance(raw, dict) and isinstance(raw.get("links"), dict):
                links = dict(raw["links"])
        links[self.link_identity] = self.as_json()
        return links

    def with_learned(self, **updates: object) -> Capabilities:
        """Return a new `Capabilities` with `updates` applied. Accepts any
        real field name; `LEARNABLE_FIELDS` is a narrower set the FACADE
        enforces before calling this, not a restriction this method applies
        itself - the doctor probe needs to set fields outside that set,
        `z21_cv_opcodes` among them."""
        valid = {f.name for f in fields(self)} - {"link_identity"}
        unknown = set(updates) - valid
        if unknown:
            raise ValueError(f"unknown capability field: {sorted(unknown)[0]!r}")
        return replace(self, **updates)

    def with_note(self, note: str) -> Capabilities:
        """Append `note`, unless it already is the exact text of an existing
        one - repeating the same probe should not grow the file forever."""
        if note in self.notes:
            return self
        return replace(self, notes=(*self.notes, note))

    def as_json(self) -> dict[str, object]:
        """This station's entry as written to disk - no `link_identity` key,
        because that name is the dict key one level up in the file."""
        return {
            "probed_at": self.probed_at,
            "xpressnet_version": self.xpressnet_version,
            "command_station_id": self.command_station_id,
            "pom_read": self.pom_read,
            "pom_result_channel": self.pom_result_channel,
            "pom_echo_zero_based": self.pom_echo_zero_based,
            "loco_address_threshold": self.loco_address_threshold,
            "service_direct_cv": self.service_direct_cv,
            "service_ext_cv": self.service_ext_cv,
            "z21_cv_opcodes": self.z21_cv_opcodes,
            "function_groups_4_5": self.function_groups_4_5,
            "single_function_cmd": self.single_function_cmd,
            "notes": list(self.notes),
        }

    @property
    def probed(self) -> bool:
        return self.probed_at is not None
