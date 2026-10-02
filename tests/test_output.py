import json
import os

import pytest

from fucc import output


def test_clean_removes_terminal_controls_and_emoji_but_keeps_cjk():
    text = '\x1b[31m中文\x1b[0m\tOK😀👩\u200d💻🇯🇵1\ufe0f\u20e3\x00\u202e\r\nnext'
    assert output.clean(text) == "中文 OK1\nnext"
    for sequence in ('\x1b]0;secret\x07', '\x1b]0;secret\x1b\\', '\x9d0;secret\x9c', '\x1bPsecret\x1b\\', '\x1b]unterminated', '\x9b31m'):
        assert output.clean("before" + sequence) == "before"


def test_ascii_cjk_combining_widths_and_lossless_wrap():
    text = "ASCII中文e\u0301测试"
    assert output.width("中文") == 4
    assert output.width("e\u0301") == 1
    assert output.width("\x1b[31m中文\x1b[0m") == 4
    wrapped = output.wrap(text, 4)
    assert "".join(wrapped) == text
    assert all(output.width(line) <= 4 for line in wrapped)
    with pytest.raises(ValueError):
        output.wrap(text, 1)


@pytest.mark.parametrize("columns", [24, 40, 60, 96, 120])
def test_tables_fit_display_width_with_cjk_and_long_paths(columns):
    text = output.table("中文 Title", ["Key", "Value"], [("中文字段", "/home/folder with spaces/very/long/路径/文件"), ("missing", None), ("short-row",)], columns=columns, weights=[1, 3])
    assert all(output.width(line) == min(columns, 96) for line in text.splitlines())
    assert "N/A" in text
    assert "".join(output.wrap("路径文件", 2)) == "路径文件"
    assert all(ord(character) < 128 for character in text.splitlines()[0])


def test_minimum_width_and_invalid_column_count():
    text = output.table("Title", ["Key", "Value"], [("key", "value")], columns=10)
    assert all(output.width(line) == 24 for line in text.splitlines())
    with pytest.raises(ValueError):
        output.table("Title", [], [], columns=24)
    with pytest.raises(ValueError):
        output.table("Title", list("abcdef"), [], columns=24)


def test_report_displays_risk_state_and_narrow_terminal_layout(monkeypatch, capsys):
    response = {"score": 0, "riskLevel": "low", "signals": [
        {"id": "locale", "label": "Python Locale", "value": "en_US.UTF-8", "contribution": 0, "risk": "low", "availability": "available"},
        {"id": "dns", "label": "DNS", "value": "Unavailable", "contribution": 0, "risk": "medium", "availability": "limited"},
    ], "ipIntelligence": None, "recommendations": ["Retry detection."]}
    monkeypatch.setattr(output.shutil, "get_terminal_size", lambda fallback: os.terminal_size((40, 24)))
    output.check_report(response, "us")
    text = capsys.readouterr().out
    assert "LOW" in text and "LIMITED" in text and "N/A" in text
    assert "AVAILABLE" not in text
    assert "Not verified" in text
    assert all(output.width(line) == 40 for line in text.splitlines() if line)


def test_json_output_preserves_machine_readable_content(capsys):
    payload = {"language": "中文", "accessVerified": False}
    output.json_output(payload)
    assert json.loads(capsys.readouterr().out) == payload
    assert output.display(True) == "yes" and output.display(False) == "no"
    assert output.display(None) == "N/A"
