# tests/unit/test_catalog.py
"""The curated ZIMO catalog: data invariants from the spec, loader strictness, packaging.

The data tests pin the shipped `zimo.toml` to the spec's C3 table - the file is
transcribed reference data, and a wrong flag here becomes wrong restore
behaviour in M10 (`restorable = false` is the list of CVs a restore must never
write). The loader tests exercise strictness on synthetic files: duplicates are
load errors, a `[[cv]]` beats a `[[range]]`, and `curated_cvs` demands CV29.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from railctl.catalog import (
    ADDRESS_CVS,
    CATALOG_FAMILY,
    CATALOG_SCHEMA,
    CatalogEntry,
    curated_cvs,
    load_catalog,
)
from railctl.errors import CatalogError

REPO_ROOT = Path(__file__).resolve().parents[2]

CATALOG = load_catalog()

SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

SPEED_TABLE_CVS = frozenset(range(67, 95))
NEVER_RESTORED_CVS = frozenset({7, 8, 31, 32, 250, 251, 252, 253})
GROUPS = frozenset(
    {
        "address",
        "motor",
        "regulation",
        "consist",
        "config",
        "identity",
        "indexing",
        "function_map",
        "lights",
        "speed_table",
        "sound",
    }
)

#: CV29 values used to drive the speed-table split. 0x06 is the factory
#: default of the bench MS450 (bit 4 clear); 0x16 is the same with bit 4 set.
CV29_THREE_POINT = 0x06
CV29_SPEED_TABLE = 0x16


# --- the shipped data ------------------------------------------------------


def test_the_shipped_catalog_parses_and_meets_the_size_floor():
    assert len(CATALOG) >= 60


def test_every_num_is_a_valid_cv_number_and_matches_its_key():
    for num, entry in CATALOG.items():
        assert 1 <= entry.num <= 1024
        assert entry.num == num


def test_min_and_max_stay_inside_a_byte_and_ordered():
    for entry in CATALOG.values():
        assert 0 <= entry.min <= entry.max <= 255


def test_slugs_are_unique_and_well_formed():
    slugs = [entry.slug for entry in CATALOG.values()]
    assert len(slugs) == len(set(slugs))
    for slug in slugs:
        assert SLUG_PATTERN.match(slug), slug


def test_descriptions_are_single_line_prose_without_quotes_or_backslashes():
    for entry in CATALOG.values():
        assert entry.desc
        assert "\n" not in entry.desc
        assert '"' not in entry.desc
        assert "\\" not in entry.desc


def test_the_address_flag_marks_exactly_the_address_cvs():
    assert {entry.num for entry in CATALOG.values() if entry.address} == set(ADDRESS_CVS)
    assert set(ADDRESS_CVS) == {1, 17, 18, 29}


def test_restorable_false_marks_exactly_the_never_restored_cvs():
    flagged = {entry.num for entry in CATALOG.values() if not entry.restorable}
    assert flagged == NEVER_RESTORED_CVS


def test_needs_speed_table_marks_exactly_the_28_point_table():
    flagged = {entry.num for entry in CATALOG.values() if entry.needs_speed_table}
    assert flagged == SPEED_TABLE_CVS


def test_the_group_names_are_exactly_the_agreed_eleven():
    """A twelfth group must be a decision, not a typo."""
    assert {entry.group for entry in CATALOG.values()} == GROUPS


def test_the_three_rows_with_explicit_ranges_carry_them():
    """The C3 table names three non-default ranges; everything else is 0-255."""
    assert (CATALOG[1].min, CATALOG[1].max) == (1, 127)
    assert (CATALOG[17].min, CATALOG[17].max) == (192, 231)
    assert (CATALOG[56].min, CATALOG[56].max) == (0, 99)
    for entry in CATALOG.values():
        if entry.num not in {1, 17, 56}:
            assert (entry.min, entry.max) == (0, 255), entry.num


def test_the_verified_entry_counts():
    """The counts hold post-dedup: 80 default, 108 with the speed table."""
    assert len(CATALOG) == 108
    assert len([e for e in CATALOG.values() if e.needs_speed_table]) == 28


# --- the smoke generator (MS manual 3.21, bench 2026-08-21) ----------------


def test_the_three_smoke_curve_cvs_are_named_and_grouped():
    assert [CATALOG[cv].slug for cv in (137, 138, 139)] == [
        "smoke_pwm_standstill",
        "smoke_pwm_min_speed",
        "smoke_pwm_max_speed",
    ]
    # `lights` and not a group of its own: the curve is inert without an
    # effect code in CV127-132, which is already `lights`, so splitting them
    # would put one setting's two halves in two groups.
    assert {CATALOG[cv].group for cv in (137, 138, 139)} == {"lights"}


def test_the_smoke_curve_says_which_decoder_family_it_describes():
    """CV137-139 do not mean the same thing across the two families this
    catalog covers, and `cv read 137` prints one description for both.

    JMRI's own definitions disagree with each other on these three numbers:
    `zimo/CV107-CV199_MS-MN-FS.xml` (which lists MS450 version 5+) calls
    CV137 "Smoke PWM at standstill", and `Zimo_MX62_v22+.xml` and
    `Zimo_MX63_MX64H_v22-30.xml` call the same CV "Deactivating HLU direction
    bits". A description that stated one meaning flat would tell the reader of
    the other decoder something false, so each of the three names its family
    the way CV144 already does.
    """
    for cv in (137, 138, 139):
        desc = CATALOG[cv].desc
        assert "MS family:" in desc, cv
        assert "MX family:" in desc, cv


def test_the_effect_range_names_both_smoke_codes_and_the_curve_trap():
    """The one fact about CV127-132 that costs an afternoon: the effect code
    alone does nothing. The ZIMO MS manual, 3.21, is explicit that CV137-139
    must be given values or smoke stays off for good."""
    for index, cv in enumerate(range(127, 133), start=1):
        desc = CATALOG[cv].desc
        assert desc.startswith(f"Effect for output FA{index}:"), cv
        assert "72 = steam" in desc, cv
        assert "80 = diesel" in desc, cv
        assert "CV137-139" in desc, cv


def test_the_range_expansions_read_back_by_slug():
    assert CATALOG[35].slug == "fn_map_f01"
    assert CATALOG[46].slug == "fn_map_f12"
    assert CATALOG[67].slug == "speed_table_01"
    assert CATALOG[94].slug == "speed_table_28"
    assert CATALOG[127].slug == "effect_fa1"
    assert CATALOG[132].slug == "effect_fa6"


def test_the_family_and_schema_constants():
    assert CATALOG_FAMILY == "zimo-ms-mx"
    assert CATALOG_SCHEMA == 1


# --- curated_cvs -----------------------------------------------------------


def test_curated_cvs_without_bit_4_excludes_the_speed_table():
    cvs = curated_cvs(CATALOG, CV29_THREE_POINT)
    assert len(cvs) == 80
    assert not SPEED_TABLE_CVS & set(cvs)


def test_curated_cvs_with_bit_4_includes_the_speed_table():
    cvs = curated_cvs(CATALOG, CV29_SPEED_TABLE)
    assert len(cvs) == 108
    assert SPEED_TABLE_CVS <= set(cvs)


def test_curated_cvs_reads_bit_4_and_no_other_bit():
    with_other_bits = curated_cvs(CATALOG, 0xFF & ~0x10)
    assert not SPEED_TABLE_CVS & set(with_other_bits)
    only_bit_4 = curated_cvs(CATALOG, 0x10)
    assert SPEED_TABLE_CVS <= set(only_bit_4)


def test_curated_cvs_is_sorted_ascending():
    cvs = curated_cvs(CATALOG, CV29_SPEED_TABLE)
    assert cvs == sorted(cvs)


def test_curated_cvs_requires_cv29():
    """CV29 is not optional: guessing the speed-table bit is the failure this
    project exists to prevent. No default argument, and no out-of-byte value."""
    with pytest.raises(TypeError):
        curated_cvs(CATALOG)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="cv29"):
        curated_cvs(CATALOG, -1)
    with pytest.raises(ValueError, match="cv29"):
        curated_cvs(CATALOG, 256)


# --- loader strictness on synthetic files ----------------------------------

HEADER = f'family = "{CATALOG_FAMILY}"\nschema = {CATALOG_SCHEMA}\n'

CV_ONE = '[[cv]]\nnum = 1\nslug = "primary_address"\ndesc = "d"\ngroup = "address"\n'


def _write(tmp_path: Path, body: str, *, header: str = HEADER) -> Path:
    path = tmp_path / "catalog.toml"
    path.write_text(header + body, encoding="utf-8")
    return path


def test_load_catalog_reads_an_explicit_path(tmp_path: Path):
    catalog = load_catalog(_write(tmp_path, CV_ONE))
    assert catalog == {1: CatalogEntry(num=1, slug="primary_address", desc="d", group="address")}


def test_entry_defaults_are_the_spec_defaults(tmp_path: Path):
    entry = load_catalog(_write(tmp_path, CV_ONE))[1]
    assert (entry.min, entry.max) == (0, 255)
    assert entry.address is False
    assert entry.restorable is True
    assert entry.needs_speed_table is False


def test_a_duplicate_cv_num_is_a_load_error(tmp_path: Path):
    body = CV_ONE + '[[cv]]\nnum = 1\nslug = "other"\ndesc = "d"\ngroup = "address"\n'
    with pytest.raises(CatalogError, match="duplicate"):
        load_catalog(_write(tmp_path, body))


def test_a_duplicate_slug_is_a_load_error(tmp_path: Path):
    body = CV_ONE + '[[cv]]\nnum = 2\nslug = "primary_address"\ndesc = "d"\ngroup = "motor"\n'
    with pytest.raises(CatalogError, match="slug"):
        load_catalog(_write(tmp_path, body))


def test_two_ranges_covering_one_number_are_a_load_error(tmp_path: Path):
    body = (
        "[[range]]\nfirst = 10\nlast = 12\nindex_start = 1\n"
        'slug_template = "a_{i}"\ndesc_template = "d"\ngroup = "motor"\n'
        "[[range]]\nfirst = 12\nlast = 14\nindex_start = 1\n"
        'slug_template = "b_{i}"\ndesc_template = "d"\ngroup = "motor"\n'
    )
    with pytest.raises(CatalogError, match="range"):
        load_catalog(_write(tmp_path, body))


def test_a_cv_block_beats_a_range_on_the_same_number(tmp_path: Path):
    body = (
        "[[range]]\nfirst = 1\nlast = 3\nindex_start = 1\n"
        'slug_template = "r_{i}"\ndesc_template = "from range"\ngroup = "motor"\n'
        '[[cv]]\nnum = 2\nslug = "the_winner"\ndesc = "from cv"\ngroup = "motor"\n'
    )
    catalog = load_catalog(_write(tmp_path, body))
    assert catalog[2].slug == "the_winner"
    assert catalog[2].desc == "from cv"
    assert catalog[1].slug == "r_1"
    assert catalog[3].slug == "r_3"


def test_a_range_template_that_collides_with_itself_is_a_load_error(tmp_path: Path):
    body = (
        "[[range]]\nfirst = 10\nlast = 12\nindex_start = 1\n"
        'slug_template = "constant"\ndesc_template = "d"\ngroup = "motor"\n'
    )
    with pytest.raises(CatalogError, match="slug"):
        load_catalog(_write(tmp_path, body))


def test_a_template_with_an_unknown_placeholder_is_a_load_error(tmp_path: Path):
    body = (
        "[[range]]\nfirst = 10\nlast = 12\nindex_start = 1\n"
        'slug_template = "a_{nope}"\ndesc_template = "d"\ngroup = "motor"\n'
    )
    with pytest.raises(CatalogError, match="template"):
        load_catalog(_write(tmp_path, body))


def test_a_backwards_range_is_a_load_error(tmp_path: Path):
    body = (
        "[[range]]\nfirst = 12\nlast = 10\nindex_start = 1\n"
        'slug_template = "a_{i}"\ndesc_template = "d"\ngroup = "motor"\n'
    )
    with pytest.raises(CatalogError, match="first"):
        load_catalog(_write(tmp_path, body))


def test_an_unknown_key_is_a_load_error(tmp_path: Path):
    """`restoreable = false` must be a load error, not a silent default of true."""
    body = (
        '[[cv]]\nnum = 7\nslug = "decoder_version"\ndesc = "d"\ngroup = "identity"\n'
        "restoreable = false\n"
    )
    with pytest.raises(CatalogError, match="restoreable"):
        load_catalog(_write(tmp_path, body))


def test_a_missing_required_key_is_a_load_error(tmp_path: Path):
    body = '[[cv]]\nnum = 1\nslug = "primary_address"\ndesc = "d"\n'
    with pytest.raises(CatalogError, match="group"):
        load_catalog(_write(tmp_path, body))


def test_a_wrongly_typed_value_is_a_load_error(tmp_path: Path):
    body = '[[cv]]\nnum = 1\nslug = "primary_address"\ndesc = "d"\ngroup = "address"\nmin = "0"\n'
    with pytest.raises(CatalogError, match="min"):
        load_catalog(_write(tmp_path, body))
    body = '[[cv]]\nnum = 1\nslug = "primary_address"\ndesc = "d"\ngroup = "address"\nmax = true\n'
    with pytest.raises(CatalogError, match="max"):
        load_catalog(_write(tmp_path, body))
    body = '[[cv]]\nnum = 1\nslug = 1\ndesc = "d"\ngroup = "address"\n'
    with pytest.raises(CatalogError, match="slug"):
        load_catalog(_write(tmp_path, body))
    body = '[[cv]]\nnum = 1\nslug = "primary_address"\ndesc = "d"\ngroup = "address"\naddress = 1\n'
    with pytest.raises(CatalogError, match="address"):
        load_catalog(_write(tmp_path, body))


def test_a_wrong_family_or_schema_is_a_load_error(tmp_path: Path):
    with pytest.raises(CatalogError, match="family"):
        load_catalog(_write(tmp_path, CV_ONE, header='family = "acme"\nschema = 1\n'))
    with pytest.raises(CatalogError, match="schema"):
        load_catalog(_write(tmp_path, CV_ONE, header=f'family = "{CATALOG_FAMILY}"\nschema = 2\n'))


def test_an_unknown_top_level_key_is_a_load_error(tmp_path: Path):
    with pytest.raises(CatalogError, match="surprise"):
        load_catalog(_write(tmp_path, 'surprise = "x"\n' + CV_ONE))


def test_an_unparseable_file_is_a_load_error(tmp_path: Path):
    path = tmp_path / "broken.toml"
    path.write_text("[[cv\n", encoding="utf-8")
    with pytest.raises(CatalogError, match="parse"):
        load_catalog(path)


# --- packaging -------------------------------------------------------------


def test_the_wheel_ships_the_catalog(tmp_path: Path):
    """Build a real wheel and load the catalog out of it. This is the test the
    spec names: a packaging mistake must fail CI, not the first restore."""
    uv = shutil.which("uv")
    assert uv is not None, "uv is required to build the wheel under test"
    subprocess.run(  # noqa: S603 - fixed argv, absolute executable, no user input
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
    )
    wheel = next(tmp_path.glob("railctl-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert "railctl/catalog/zimo.toml" in archive.namelist()
        archive.extractall(tmp_path / "unpacked")
    extracted = tmp_path / "unpacked" / "railctl" / "catalog" / "zimo.toml"
    assert load_catalog(extracted) == CATALOG


@pytest.mark.parametrize(
    "body", ["cv = 3", "range = 3", "cv = [3]"], ids=["int", "range-int", "int-array"]
)
def test_a_malformed_container_is_a_catalog_error_not_a_type_error(tmp_path: Path, body: str):
    """`cv = 3` where `[[cv]]` was meant used to escape as a bare `TypeError`.

    The loader's contract is `CatalogError` on ANYTHING questionable, and a hand-edited
    file is the expected way the module breaks - the spec chose TOML precisely so the
    user can correct it by hand. Escaped, the CLI's safety net reported the mistake as
    `internal`: a data-file defect dressed up as a railctl bug, invisible to a script
    branching on `error.code == "catalog"`.
    """
    with pytest.raises(CatalogError, match="array of tables"):
        load_catalog(_write(tmp_path, body))


def test_a_hollow_catalog_is_damage_not_an_empty_answer(tmp_path: Path):
    """A correct header with zero blocks is what a truncated file looks like.

    It used to load as {} - and M9's backup would have read zero CVs and reported a
    complete, empty backup. The >=60 floor is test-only and never runs against an
    installed file, so the load-time guard is the only one production ever meets.
    """
    with pytest.raises(CatalogError, match="truncated"):
        load_catalog(_write(tmp_path, ""))
