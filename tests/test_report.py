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
