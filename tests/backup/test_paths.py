# tests/backup/test_paths.py
"""`backup_path`: the default location, the stdout sentinel, and the
directory-appends-name rule. HOME is monkeypatched wherever the home
directory takes part, so no test reads the developer's real one."""

from __future__ import annotations

from pathlib import Path

from railctl.backup import backup_path


def test_no_out_resolves_to_the_default_directory_and_name(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert (
        backup_path(3, "curated", None) == tmp_path / "railctl-backups" / "loco-0003-curated.json"
    )


def test_the_address_is_zero_padded_to_four_digits(tmp_path, monkeypatch):
    # 999 and 1000 sit either side of the last padded width change; 9999 is
    # the top of the address range and must not gain a fifth digit.
    monkeypatch.setenv("HOME", str(tmp_path))
    names = {
        address: backup_path(address, "curated", None).name for address in (3, 999, 1000, 9999)
    }
    assert names == {
        3: "loco-0003-curated.json",
        999: "loco-0999-curated.json",
        1000: "loco-1000-curated.json",
        9999: "loco-9999-curated.json",
    }


def test_the_set_name_is_part_of_the_file_name(tmp_path, monkeypatch):
    # The design's reason: a curated run must not overwrite a full sweep.
    monkeypatch.setenv("HOME", str(tmp_path))
    assert backup_path(3, "all", None).name == "loco-0003-all.json"


def test_dash_means_stdout_and_resolves_to_no_path():
    assert backup_path(3, "curated", "-") is None


def test_an_out_path_that_is_a_directory_gets_the_generated_name_appended(tmp_path):
    assert backup_path(3, "curated", str(tmp_path)) == tmp_path / "loco-0003-curated.json"


def test_an_out_path_that_does_not_exist_is_taken_as_given(tmp_path):
    target = tmp_path / "mine.json"
    assert backup_path(3, "curated", str(target)) == target


def test_an_out_path_that_exists_as_a_file_is_taken_as_given(tmp_path):
    # Whether it may be overwritten is the caller's --force gate, not path
    # resolution's business.
    existing = tmp_path / "old.json"
    existing.write_text("{}", encoding="utf-8")
    assert backup_path(3, "curated", str(existing)) == existing


def test_a_tilde_in_out_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert backup_path(3, "curated", "~/kept/mine.json") == tmp_path / "kept" / "mine.json"


def test_the_default_is_an_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved = backup_path(3, "curated", None)
    assert isinstance(resolved, Path)
    assert resolved.is_absolute()
