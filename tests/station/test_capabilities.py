"""The tri-state capability file: None means "not established", never "no".

Every test here exists because collapsing "never measured" into "false" is
the recorded failure mode this project is built around - a POM CV read that
returns nothing at all must stay `None` forever, not become `False` the
moment it touches a JSON file.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from railctl.errors import RailctlError
from railctl.station.capabilities import (
    CAPABILITIES_VERSION,
    LEARNABLE_FIELDS,
    PERSISTED_FIELDS,
    UNKNOWN_IDENTITY,
    Capabilities,
)

IDENTITY = "7010A0001194"


def test_unknown_has_every_capability_field_none_and_is_not_probed():
    caps = Capabilities.unknown(IDENTITY)
    assert caps.link_identity == IDENTITY
    assert caps.probed_at is None
    assert caps.probed is False
    for field in dataclasses.fields(caps):
        if field.name in ("link_identity", "notes"):
            continue
        assert getattr(caps, field.name) is None, field.name
    assert caps.notes == ()


def test_probed_is_true_once_probed_at_is_set():
    caps = Capabilities.unknown(IDENTITY).with_learned(probed_at="2026-08-05T10:00:00Z")
    assert caps.probed is True


def test_load_on_a_missing_file_returns_unknown_and_creates_nothing(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    assert Capabilities.load(path, IDENTITY) == Capabilities.unknown(IDENTITY)
    assert not path.exists()


def test_save_on_the_unknown_identity_writes_nothing(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    caps = Capabilities.unknown(UNKNOWN_IDENTITY)
    assert caps.save(path) is False
    assert not path.exists()


def test_the_tri_state_survives_the_file(tmp_path: Path):
    """A saved None reloads as None, a saved False reloads as False, and a key
    that was never written AT ALL loads as None too - never as False.

    Cases 1 and 2 alone would not catch a loader written as
    `entry.get("pom_read", False)` instead of `entry.get("pom_read")`, because
    `as_json()` always writes every field, including the nulls. Case 3 hand-
    writes an entry with the key missing outright, which is the only way to
    tell the two loader implementations apart.
    """
    path = tmp_path / "capabilities.json"

    caps = Capabilities.unknown(IDENTITY).with_learned(pom_read=None)
    assert caps.save(path) is True
    assert Capabilities.load(path, IDENTITY).pom_read is None

    caps = Capabilities.unknown(IDENTITY).with_learned(pom_read=False)
    assert caps.save(path) is True
    assert Capabilities.load(path, IDENTITY).pom_read is False

    path.write_text(json.dumps({"version": 1, "links": {IDENTITY: {}}}), encoding="utf-8")
    assert Capabilities.load(path, IDENTITY).pom_read is None


def test_save_merges_with_an_existing_entry_for_another_station(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    other = Capabilities.unknown("other-station").with_learned(pom_read=True)
    assert other.save(path) is True

    mine = Capabilities.unknown(IDENTITY).with_learned(pom_read=False)
    assert mine.save(path) is True

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == CAPABILITIES_VERSION
    assert raw["links"]["other-station"]["pom_read"] is True
    assert raw["links"][IDENTITY]["pom_read"] is False


def test_save_is_atomic_no_stray_temp_file_survives(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    Capabilities.unknown(IDENTITY).with_learned(pom_read=True).save(path)

    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []
    # A half-written file would fail this parse, not the leftover check above.
    json.loads(path.read_text(encoding="utf-8"))


def test_save_removes_the_temp_file_when_the_write_fails(tmp_path: Path, monkeypatch):
    """A failure between the temp file being created and the atomic rename
    must not leave the abandoned temp file behind, and must not swallow the
    original error."""
    path = tmp_path / "capabilities.json"

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("railctl.station.capabilities.os.replace", _boom)
    caps = Capabilities.unknown(IDENTITY).with_learned(pom_read=True)
    with pytest.raises(OSError, match="disk full"):
        caps.save(path)
    assert list(tmp_path.iterdir()) == []


def test_save_discards_a_corrupt_existing_file_instead_of_merging_it(tmp_path: Path):
    """`_merged_links` is a second, independent reader of the same file
    `load` reads. A corrupt file there must not raise - `save` is how a
    fresh probe recovers from a capabilities.json that got mangled by
    something else - it just starts the links table over rather than
    merging garbage into it."""
    path = tmp_path / "capabilities.json"
    path.write_text("{not json", encoding="utf-8")

    caps = Capabilities.unknown(IDENTITY).with_learned(pom_read=True)
    assert caps.save(path) is True

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw["links"]) == {IDENTITY}


def test_load_on_corrupt_json_raises_railctl_error_naming_the_path(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RailctlError) as caught:
        Capabilities.load(path, IDENTITY)
    assert str(path) in str(caught.value)
    assert "doctor" in caught.value.hint


def test_load_on_the_wrong_version_raises_railctl_error(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps({"version": 2, "links": {}}), encoding="utf-8")
    with pytest.raises(RailctlError) as caught:
        Capabilities.load(path, IDENTITY)
    assert str(path) in str(caught.value)
    assert "doctor" in caught.value.hint


def test_load_raises_when_links_is_not_a_dict(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps({"version": 1, "links": []}), encoding="utf-8")
    with pytest.raises(RailctlError) as caught:
        Capabilities.load(path, IDENTITY)
    assert "links" in str(caught.value)


def test_load_returns_unknown_when_the_file_holds_only_another_station(tmp_path: Path):
    """The ordinary second-station case: a file that exists and parses fine
    but simply has no entry for this identity yet. Exercised on every real
    run the first time a new station is seen."""
    path = tmp_path / "capabilities.json"
    other = {"version": 1, "links": {"other-station": {"pom_read": True}}}
    path.write_text(json.dumps(other), encoding="utf-8")
    assert Capabilities.load(path, IDENTITY) == Capabilities.unknown(IDENTITY)


def test_load_raises_when_the_entry_for_this_identity_is_not_an_object(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps({"version": 1, "links": {IDENTITY: []}}), encoding="utf-8")
    with pytest.raises(RailctlError) as caught:
        Capabilities.load(path, IDENTITY)
    assert "not an object" in str(caught.value)


def test_load_ignores_an_unrecognised_key_in_a_link_entry(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    entry = {"pom_read": True, "a_field_from_a_future_railctl": "x"}
    path.write_text(json.dumps({"version": 1, "links": {IDENTITY: entry}}), encoding="utf-8")
    assert Capabilities.load(path, IDENTITY).pom_read is True


@pytest.mark.parametrize(
    "entry",
    [
        {"pom_read": "yes"},
        {"command_station_id": "12"},
        {"xpressnet_version": 4},
        {"pom_result_channel": "maybe"},
        {"pom_read_provenance": "probably"},
        {"notes": 7},
    ],
    ids=[
        "bool-field",
        "int-field",
        "str-field",
        "result-channel-enum",
        "provenance-enum",
        "notes-wrong-type",
    ],
)
def test_load_raises_on_a_recognised_field_with_the_wrong_type(
    tmp_path: Path, entry: dict[str, object]
):
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps({"version": 1, "links": {IDENTITY: entry}}), encoding="utf-8")
    with pytest.raises(RailctlError) as caught:
        Capabilities.load(path, IDENTITY)
    assert next(iter(entry)) in str(caught.value)


def test_load_reads_a_bare_string_note_as_a_one_element_tuple(tmp_path: Path):
    path = tmp_path / "capabilities.json"
    entry = {"notes": "hand-written note"}
    path.write_text(json.dumps({"version": 1, "links": {IDENTITY: entry}}), encoding="utf-8")
    assert Capabilities.load(path, IDENTITY).notes == ("hand-written note",)


def test_with_learned_returns_a_new_object_and_leaves_the_original_unchanged():
    original = Capabilities.unknown(IDENTITY)
    learned = original.with_learned(pom_read=True)
    assert learned is not original
    assert learned.pom_read is True
    assert original.pom_read is None


def test_with_learned_raises_value_error_naming_an_unknown_field():
    caps = Capabilities.unknown(IDENTITY)
    with pytest.raises(ValueError, match="bogus_field"):
        caps.with_learned(bogus_field=True)


def test_with_learned_can_set_z21_cv_opcodes_though_it_is_not_a_learnable_field():
    """with_learned enforces only "is this a real field" - LEARNABLE_FIELDS is
    the FACADE's restriction, checked one layer up, because the doctor probe
    (a later task) needs to set fields outside it, z21_cv_opcodes among them.
    """
    assert "z21_cv_opcodes" not in LEARNABLE_FIELDS
    caps = Capabilities.unknown(IDENTITY).with_learned(z21_cv_opcodes=True)
    assert caps.z21_cv_opcodes is True


def test_with_note_appends_and_does_not_duplicate_an_identical_note():
    caps = Capabilities.unknown(IDENTITY).with_note("D4 concluded false from silence")
    again = caps.with_note("D4 concluded false from silence")
    assert caps.notes == ("D4 concluded false from silence",)
    assert again is caps


def test_as_json_carries_no_identity_key():
    payload = Capabilities.unknown(IDENTITY).with_learned(pom_read=True).as_json()
    assert "link_identity" not in payload
    assert payload["pom_read"] is True


def test_capabilities_is_frozen():
    caps = Capabilities.unknown(IDENTITY)
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.pom_read = True


# -- the writer: every persistable field, and what a narrower run may not erase --

#: One non-default value per persisted field. Compared against `PERSISTED_FIELDS`
#: below, so a field added to `Capabilities` fails this file until someone gives it
#: a value here - which is what makes the round-trip test cover the NEXT field as
#: well as today's thirteen. `pom_read_provenance` is the one that was missing from
#: the writer while the loader, `LEARNABLE_FIELDS` and the CLI envelope all carried
#: it.
_SAMPLE_VALUES: dict[str, object] = {
    "probed_at": "2026-08-09T12:00:00Z",
    "xpressnet_version": "4.0",
    "command_station_id": 0x12,
    "pom_read": False,
    "pom_read_provenance": "silence",
    "pom_result_channel": "none",
    "pom_echo_zero_based": True,
    "loco_address_threshold": 128,
    "service_direct_cv": True,
    "service_ext_cv": False,
    "z21_cv_opcodes": True,
    "function_groups_4_5": False,
    "single_function_cmd": True,
    "notes": ("POM read produced no result at all",),
}


def test_every_persistable_field_is_named_in_this_files_sample_values():
    """The guard on the guard: the round-trip test below can only cover a field it
    has a value for, so a new field must fail HERE, loudly, rather than quietly
    dropping out of the coverage of every test under it."""
    assert set(_SAMPLE_VALUES) == set(PERSISTED_FIELDS)


def test_every_persistable_field_survives_a_save_and_a_load(tmp_path: Path):
    """The writer used to enumerate its keys by hand, and `pom_read_provenance` was
    not among them: `Capabilities(pom_read=False, pom_read_provenance="silence")`
    saved and reloaded came back with the provenance gone. That field is what tells
    "the station refused" (`61 82`) from "nothing came back", and it is the entire
    reason `pom_read=False` from silence is allowed at all.
    """
    path = tmp_path / "capabilities.json"
    caps = Capabilities(link_identity=IDENTITY, **_SAMPLE_VALUES)
    assert caps.save(path) is True
    assert Capabilities.load(path, IDENTITY) == caps


def test_the_provenance_of_a_false_from_silence_reaches_the_file(tmp_path: Path):
    """The measured case, written out rather than derived, because it is the one
    exception to "only a 61 82 earns a false" and a reader that must not act on a
    guess reads this field and not `pom_read`."""
    path = tmp_path / "capabilities.json"
    Capabilities(link_identity=IDENTITY, pom_read=False, pom_read_provenance="silence").save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["links"][IDENTITY]["pom_read_provenance"] == "silence"


def test_a_narrower_run_does_not_erase_what_a_wider_run_measured(tmp_path: Path):
    """`doctor --power-on` establishes four capabilities; a later plain `doctor` -
    track dead, so D4 never runs, and D5-D8 may be skipped outright - holds `None`
    for all four. `None` is "this run learned nothing here", so it must not be
    written over a measurement that cost 6.7 s per silent POM attempt to make.
    """
    path = tmp_path / "capabilities.json"
    wide = Capabilities(
        link_identity=IDENTITY,
        pom_read=False,
        pom_read_provenance="silence",
        service_direct_cv=True,
        z21_cv_opcodes=True,
    )
    assert wide.save(path) is True

    narrow = Capabilities(link_identity=IDENTITY, xpressnet_version="4.0")
    assert narrow.save(path) is True

    after = Capabilities.load(path, IDENTITY)
    assert after.pom_read is False
    assert after.pom_read_provenance == "silence"
    assert after.service_direct_cv is True
    assert after.z21_cv_opcodes is True
    assert after.xpressnet_version == "4.0"  # what this run DID measure still lands


def test_a_run_that_measured_something_new_overwrites_the_stored_value(tmp_path: Path):
    """The other half: merging must not freeze the file. A re-probe that reaches a
    different verdict replaces it - only `None` is barred from overwriting."""
    path = tmp_path / "capabilities.json"
    Capabilities(link_identity=IDENTITY, pom_read=False, pom_read_provenance="silence").save(path)
    Capabilities(link_identity=IDENTITY, pom_read=True).save(path)
    assert Capabilities.load(path, IDENTITY).pom_read is True


def test_a_re_probed_pom_read_does_not_keep_the_reason_for_the_verdict_it_replaced(
    tmp_path: Path,
):
    """A provenance belongs to the verdict it explains, and dies with it.

    The merge rule is "`None` means this run learned nothing here, so leave what was
    stored". `pom_read_provenance` is the one field that rule must not reach:
    `CvProgrammer.pom_read` writes `{"pom_read": True, "pom_read_provenance": None}` in
    one call, deliberately, so a stale reason cannot outlive the answer it was about.

    Fit a RailCom detector and re-run the doctor, and the merge put the old reason back:
    the file held `pom_read: true` beside `pom_read_provenance: "silence"` - "it works,
    because nothing came back". CLAUDE.md names this as the field a reader who must not
    act on a guess consults instead of `pom_read`, so a wrong value here is worse than
    none. The test above writes the same pair and asserts only `pom_read`, which is how
    it walked past.
    """
    path = tmp_path / "capabilities.json"
    Capabilities(
        link_identity=IDENTITY,
        pom_read=False,
        pom_read_provenance="silence",
        service_direct_cv=True,
    ).save(path)
    Capabilities(link_identity=IDENTITY, pom_read=True, pom_read_provenance=None).save(path)

    reloaded = Capabilities.load(path, IDENTITY)
    assert reloaded.pom_read is True
    assert reloaded.pom_read_provenance is None
    # A field this run said nothing about is still kept - the merge rule itself is intact,
    # and this exception is narrow rather than a hole in it.
    assert reloaded.service_direct_cv is True


def test_a_run_that_did_not_touch_pom_read_leaves_its_reason_alone(tmp_path: Path):
    """The other side of the exception. `pom_read_provenance` follows `pom_read`, so a
    run that established nothing about the verdict may not discard the reason either -
    otherwise a plain `railctl doctor` on a dead track would strip the provenance off a
    measurement that is still perfectly current."""
    path = tmp_path / "capabilities.json"
    Capabilities(link_identity=IDENTITY, pom_read=False, pom_read_provenance="silence").save(path)
    Capabilities(link_identity=IDENTITY, service_ext_cv=True).save(path)

    reloaded = Capabilities.load(path, IDENTITY)
    assert reloaded.pom_read is False
    assert reloaded.pom_read_provenance == "silence"


def test_a_run_that_re_probed_pom_clears_the_note_that_described_the_old_verdict(
    tmp_path: Path,
):
    """D4 clears the silence note before it re-probes, and the note itself tells the
    operator to fit a RailCom detector and run the doctor again. A run where POM then
    answers must be able to erase it, or the file keeps telling them to fix something
    that is already fixed."""
    path = tmp_path / "capabilities.json"
    Capabilities(
        link_identity=IDENTITY,
        pom_read=False,
        pom_read_provenance="silence",
        notes=("POM read produced no result at all",),
    ).save(path)

    Capabilities(link_identity=IDENTITY, pom_read=True).save(path)
    assert Capabilities.load(path, IDENTITY).notes == ()


def test_a_run_that_established_no_pom_verdict_keeps_the_stored_notes(tmp_path: Path):
    """The note describes the stored `pom_read`, and `merged_over` is keeping that
    value, so it keeps the prose with it: the two are decided by one condition and
    cannot drift apart."""
    path = tmp_path / "capabilities.json"
    Capabilities(
        link_identity=IDENTITY,
        pom_read=False,
        pom_read_provenance="silence",
        notes=("POM read produced no result at all",),
    ).save(path)

    Capabilities(link_identity=IDENTITY, xpressnet_version="4.0").save(path)
    after = Capabilities.load(path, IDENTITY)
    assert after.notes == ("POM read produced no result at all",)
    assert after.pom_read is False


def test_a_note_from_a_run_that_probed_no_pom_is_still_written(tmp_path: Path):
    """Nothing but D4 writes a note today. The first thing that does must not have it
    dropped by the writer - which is exactly how `pom_read_provenance` was lost."""
    path = tmp_path / "capabilities.json"
    Capabilities(link_identity=IDENTITY, notes=("something else worth saying",)).save(path)
    assert Capabilities.load(path, IDENTITY).notes == ("something else worth saying",)


def test_merged_over_refuses_another_links_record():
    """Two links' facts must never be folded together - the same reason `save`
    refuses `UNKNOWN_IDENTITY`."""
    mine = Capabilities.unknown(IDENTITY)
    theirs = Capabilities.unknown("other-station").with_learned(pom_read=True)
    with pytest.raises(ValueError, match="two different links"):
        mine.merged_over(theirs)


def test_a_malformed_entry_for_this_link_is_replaced_rather_than_blocking_the_save(
    tmp_path: Path,
):
    """Same policy as a file that is not JSON at all (`_merged_links`): `save` is how
    a fresh probe recovers a mangled capabilities.json. A merge that raised here
    would leave the file permanently unwritable, with the doctor - the one command
    that could fix it - the one command unable to."""
    path = tmp_path / "capabilities.json"
    path.write_text(
        json.dumps({"version": 1, "links": {IDENTITY: {"pom_read": "yes"}}}), encoding="utf-8"
    )
    assert Capabilities(link_identity=IDENTITY, pom_read=True).save(path) is True
    assert Capabilities.load(path, IDENTITY).pom_read is True
