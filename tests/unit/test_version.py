"""The version string has exactly one home, and pyproject must agree with it."""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import version as installed_version
from pathlib import Path

import railctl

PYPROJECT = tomllib.loads(
    Path(__file__).resolve().parents[2].joinpath("pyproject.toml").read_text(encoding="utf-8")
)

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[.-][0-9A-Za-z.]+)?$")


def test_the_package_exposes_a_semver_string():
    assert SEMVER.match(railctl.__version__)


def test_pyproject_declares_the_version_dynamic_and_points_at_the_package():
    assert PYPROJECT["project"]["dynamic"] == ["version"]
    assert "version" not in PYPROJECT["project"]
    assert PYPROJECT["tool"]["hatch"]["version"]["path"] == "src/railctl/__init__.py"


def test_the_installed_distribution_agrees_with_the_module():
    """Fails while the editable install is stale. The M2 rename is the reason."""
    assert installed_version("railctl") == railctl.__version__


def test_typer_is_the_only_runtime_dependency():
    assert [d.split(">")[0].strip() for d in PYPROJECT["project"]["dependencies"]] == ["typer"]


def test_the_console_script_is_declared():
    """`:main`, not `:app`. `main()` is the wrapper that turns a bad config.toml or an
    out-of-range --address into a JSON error object and exit 2; the bare Typer `app` lets
    both out as a traceback. Pointing the installed script at `app` while `python -m
    railctl` goes through `main()` is how the same failure gets two different behaviours
    depending on which way the operator started the tool.
    """
    assert PYPROJECT["project"]["scripts"] == {"railctl": "railctl.cli.main:main"}


def test_the_hardware_marker_is_registered_and_deselected_by_default():
    ini = PYPROJECT["tool"]["pytest"]["ini_options"]
    assert any(m.startswith("hardware:") for m in ini["markers"])
    assert "-m 'not hardware'" in ini["addopts"]
    assert "--strict-markers" in ini["addopts"]
    assert "--strict-config" in ini["addopts"]


def test_coverage_is_configured_but_not_wired_into_addopts():
    """M3 turns the gate on. At M2 the package is too small for 90% to mean anything."""
    assert PYPROJECT["tool"]["coverage"]["report"]["fail_under"] == 90
    assert "--cov" not in PYPROJECT["tool"]["pytest"]["ini_options"]["addopts"]
