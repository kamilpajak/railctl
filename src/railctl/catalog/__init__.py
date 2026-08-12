# src/railctl/catalog/__init__.py
"""The curated ZIMO CV catalog: `CatalogEntry`, `load_catalog`, `curated_cvs`.

This package sits above `railctl.station` and speaks only in human CV numbers
(CV1 = 1) and plain integers. The data lives in `zimo.toml`, shipped as
package data: it is reference material transcribed from the design spec's C3
table, and the user must be able to correct a description against the sheet
that came with the decoder without touching code.

The loader is deliberately strict, because this data steers restore behaviour
in M10: `restorable = false` is the list of CVs a restore must never write,
and `min`/`max` are enforcing on write. A duplicate CV number or slug, an
unknown key (`restoreable = false` must not silently default to restorable),
a wrongly typed value, or a template that fails to expand is a `CatalogError`,
never a silent default. A `[[cv]]` block beats a `[[range]]` block covering
the same number.

`curated_cvs` requires CV29 - speed-table membership hangs on CV29 bit 4, and
if CV29 cannot be read the caller aborts rather than guess, because guessing a
capability is the exact failure this project exists to prevent.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Final

from railctl.errors import CatalogError

CATALOG_FAMILY: Final[str] = "zimo-ms-mx"
CATALOG_SCHEMA: Final[int] = 1

#: The CVs that decide which locomotive answers at which address. `address =
#: true` in zimo.toml marks exactly these, and a test pins the two together.
ADDRESS_CVS: Final[frozenset[int]] = frozenset({1, 17, 18, 29})

#: CV29 bit 4 selects the 28-point speed table over the 3-point curve.
_SPEED_TABLE_BIT: Final[int] = 0b0001_0000

_TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset({"family", "schema", "cv", "range"})
_ENTRY_OPTIONAL_KEYS: Final[frozenset[str]] = frozenset(
    {"min", "max", "address", "restorable", "needs_speed_table"}
)
_CV_REQUIRED_KEYS: Final[frozenset[str]] = frozenset({"num", "slug", "desc", "group"})
_RANGE_REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {"first", "last", "index_start", "slug_template", "desc_template", "group"}
)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One curated CV. `min`/`max` are advisory on read, enforcing on write;
    `restorable = False` means exactly one thing: restore never writes it."""

    num: int
    slug: str
    desc: str
    group: str
    min: int = 0
    max: int = 255
    address: bool = False
    restorable: bool = True
    needs_speed_table: bool = False


def load_catalog(path: Path | None = None) -> dict[int, CatalogEntry]:
    """Load and validate the catalog; the shipped `zimo.toml` when `path` is None.

    Raises `CatalogError` on anything questionable rather than defaulting.
    """
    if path is None:
        text = resources.files("railctl.catalog").joinpath("zimo.toml").read_text(encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CatalogError(f"the catalog does not parse as TOML: {exc}") from exc
    _check_header(document)

    entries: dict[int, CatalogEntry] = {}
    for block in document.get("range", []):
        for entry in _expand_range(block):
            if entry.num in entries:
                raise CatalogError(f"CV {entry.num} is produced by two [[range]] blocks")
            entries[entry.num] = entry
    seen_cv_nums: set[int] = set()
    for block in document.get("cv", []):
        entry = _cv_entry(block)
        if entry.num in seen_cv_nums:
            raise CatalogError(f"duplicate [[cv]] entry for CV {entry.num}")
        seen_cv_nums.add(entry.num)
        # A [[cv]] always wins over a [[range]] covering the same number.
        entries[entry.num] = entry

    _check_slugs_unique(entries)
    return dict(sorted(entries.items()))


def curated_cvs(cat: Mapping[int, CatalogEntry], cv29: int) -> list[int]:
    """The CV numbers a backup visits, ascending. `cv29` is required: bit 4
    decides speed-table membership, and it must be measured, never guessed."""
    if not 0 <= cv29 <= 255:
        raise ValueError(f"cv29 must be a CV byte in 0..255, got {cv29}")
    speed_table_selected = bool(cv29 & _SPEED_TABLE_BIT)
    return sorted(
        num for num, entry in cat.items() if speed_table_selected or not entry.needs_speed_table
    )


def _check_header(document: Mapping[str, object]) -> None:
    unknown = sorted(set(document) - _TOP_LEVEL_KEYS)
    if unknown:
        raise CatalogError(f"unknown top-level keys in the catalog: {unknown}")
    family = document.get("family")
    if family != CATALOG_FAMILY:
        raise CatalogError(f"catalog family is {family!r}, expected {CATALOG_FAMILY!r}")
    schema = document.get("schema")
    if schema != CATALOG_SCHEMA:
        raise CatalogError(f"catalog schema is {schema!r}, expected {CATALOG_SCHEMA}")


def _check_keys(block: Mapping[str, object], required: frozenset[str], kind: str) -> None:
    missing = sorted(required - set(block))
    if missing:
        raise CatalogError(f"[[{kind}]] block is missing {missing}: {dict(block)!r}")
    unknown = sorted(set(block) - required - _ENTRY_OPTIONAL_KEYS)
    if unknown:
        raise CatalogError(f"[[{kind}]] block has unknown keys {unknown}: {dict(block)!r}")


def _int_of(block: Mapping[str, object], key: str, default: int) -> int:
    value = block.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CatalogError(f"{key} must be an integer, got {value!r}")
    return value


def _str_of(block: Mapping[str, object], key: str) -> str:
    value = block[key]
    if not isinstance(value, str):
        raise CatalogError(f"{key} must be a string, got {value!r}")
    return value


def _bool_of(block: Mapping[str, object], key: str, default: bool) -> bool:
    value = block.get(key, default)
    if not isinstance(value, bool):
        raise CatalogError(f"{key} must be a boolean, got {value!r}")
    return value


def _cv_entry(block: Mapping[str, object]) -> CatalogEntry:
    _check_keys(block, _CV_REQUIRED_KEYS, "cv")
    return CatalogEntry(
        num=_int_of(block, "num", 0),
        slug=_str_of(block, "slug"),
        desc=_str_of(block, "desc"),
        group=_str_of(block, "group"),
        min=_int_of(block, "min", 0),
        max=_int_of(block, "max", 255),
        address=_bool_of(block, "address", False),
        restorable=_bool_of(block, "restorable", True),
        needs_speed_table=_bool_of(block, "needs_speed_table", False),
    )


def _expand_range(block: Mapping[str, object]) -> Iterator[CatalogEntry]:
    _check_keys(block, _RANGE_REQUIRED_KEYS, "range")
    first = _int_of(block, "first", 0)
    last = _int_of(block, "last", 0)
    index_start = _int_of(block, "index_start", 0)
    if not 1 <= first <= last:
        raise CatalogError(f"[[range]] needs 1 <= first <= last, got first={first} last={last}")
    slug_template = _str_of(block, "slug_template")
    desc_template = _str_of(block, "desc_template")
    for offset, cv in enumerate(range(first, last + 1)):
        i = index_start + offset
        try:
            slug = slug_template.format(i=i, cv=cv)
            desc = desc_template.format(i=i, cv=cv)
        except (IndexError, KeyError, ValueError) as exc:
            raise CatalogError(f"[[range]] template failed to expand: {exc!r}") from exc
        yield CatalogEntry(
            num=cv,
            slug=slug,
            desc=desc,
            group=_str_of(block, "group"),
            min=_int_of(block, "min", 0),
            max=_int_of(block, "max", 255),
            address=_bool_of(block, "address", False),
            restorable=_bool_of(block, "restorable", True),
            needs_speed_table=_bool_of(block, "needs_speed_table", False),
        )


def _check_slugs_unique(entries: Mapping[int, CatalogEntry]) -> None:
    by_slug: dict[str, int] = {}
    for entry in entries.values():
        other = by_slug.setdefault(entry.slug, entry.num)
        if other != entry.num:
            raise CatalogError(f"slug {entry.slug!r} is used by both CV {other} and CV {entry.num}")
