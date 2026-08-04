import json

from tools.probe.checks import CheckResult
from tools.probe.report import to_json, to_markdown

RESULTS = [
    CheckResult("service_ext_cv", True, "both reads agreed", ["FE 63 14 00 03"]),
    CheckResult("z21_cv_opcodes", False, "station answered 61 82", []),
    CheckResult("single_function_cmd", None, "no reply", []),
]


def test_json_keeps_true_false_and_null_distinct():
    payload = json.loads(to_json(RESULTS, port="/dev/cu.usbmodem0", run_at="2026-08-03T20:00:00Z"))
    assert payload["capabilities"]["service_ext_cv"] is True
    assert payload["capabilities"]["z21_cv_opcodes"] is False
    assert payload["capabilities"]["single_function_cmd"] is None


def test_json_records_the_port_and_the_run_time():
    payload = json.loads(to_json(RESULTS, port="/dev/cu.usbmodem0", run_at="2026-08-03T20:00:00Z"))
    assert payload["port"] == "/dev/cu.usbmodem0"
    assert payload["run_at"] == "2026-08-03T20:00:00Z"


def test_markdown_renders_unknown_as_a_word_not_as_no():
    text = to_markdown(RESULTS, port="/dev/cu.usbmodem0", run_at="2026-08-03T20:00:00Z")
    assert "| `single_function_cmd` | unknown |" in text
    assert "| `z21_cv_opcodes` | no |" in text
    assert "| `service_ext_cv` | yes |" in text


def test_markdown_includes_the_detail_text_for_each_check():
    text = to_markdown(RESULTS, port="/dev/cu.usbmodem0", run_at="2026-08-03T20:00:00Z")
    assert "both reads agreed" in text


def test_markdown_includes_the_raw_frames_for_auditing():
    text = to_markdown(RESULTS, port="/dev/cu.usbmodem0", run_at="2026-08-03T20:00:00Z")
    assert "FE 63 14 00 03" in text


def test_markdown_renders_dict_valued_results_as_see_below():
    # check_pom_read and check_identity have dict-valued results
    dict_result = CheckResult(
        "pom_read",
        {
            "pom_read": True,
            "pom_result_channel": "broadcast",
            "pom_echo_zero_based": False,
            "pom_value": 145,
        },
        "POM read of CV8 returned 145 via broadcast",
        ["FE E6 30 00 03 E4 07 00 36"],
    )
    text = to_markdown([dict_result], port="/dev/cu.usbmodem0", run_at="2026-08-03T20:00:00Z")
    assert "| `pom_read` | see below |" in text


def test_json_flattens_dict_valued_results():
    dict_result = CheckResult(
        "pom_read",
        {
            "pom_read": True,
            "pom_result_channel": "broadcast",
            "pom_echo_zero_based": False,
            "pom_value": 145,
        },
        "POM read of CV8 returned 145 via broadcast",
        [],
    )
    json_str = to_json([dict_result], port="/dev/cu.usbmodem0", run_at="2026-08-03T20:00:00Z")
    payload = json.loads(json_str)
    assert payload["capabilities"]["pom_read"] is True
    assert payload["capabilities"]["pom_result_channel"] == "broadcast"
    assert payload["capabilities"]["pom_echo_zero_based"] is False
    assert payload["capabilities"]["pom_value"] == 145


def test_mixed_scalar_and_dict_valued_results_render_correctly():
    results = [
        CheckResult(
            "pom_read",
            {
                "pom_read": True,
                "pom_result_channel": "broadcast",
                "pom_echo_zero_based": None,
                "pom_value": 145,
            },
            "POM read succeeded",
            [],
        ),
        CheckResult("single_function_cmd", False, "station rejected", []),
    ]
    payload = json.loads(to_json(results, port="/dev/cu.usbmodem0", run_at="2026-08-03T20:00:00Z"))
    # Dict keys should be flattened into capabilities
    assert payload["capabilities"]["pom_read"] is True
    assert payload["capabilities"]["pom_echo_zero_based"] is None
    # Scalar should also be there
    assert payload["capabilities"]["single_function_cmd"] is False


def test_a_duplicate_capability_key_raises_instead_of_overwriting():
    # The flattened output is a versioned contract: fields may be added, never
    # renamed or repurposed. A new check quietly clobbering an existing field is
    # the failure the version number exists to rule out, so it must be loud.
    import pytest

    from tools.probe.checks import CheckResult
    from tools.probe.report import _flatten

    clash = [CheckResult("first", {"shared": 1}, ""), CheckResult("second", {"shared": 2}, "")]
    with pytest.raises(ValueError, match="claimed twice"):
        _flatten(clash)


def test_a_check_name_colliding_with_a_dict_key_also_raises():
    import pytest

    from tools.probe.checks import CheckResult
    from tools.probe.report import _flatten

    clash = [CheckResult("owner", {"pom_read": True}, ""), CheckResult("pom_read", False, "")]
    with pytest.raises(ValueError, match="claimed twice"):
        _flatten(clash)
