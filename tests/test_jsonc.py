import pytest

from fucc.jsonc import edit, parse, parse_document


def test_surgical_values_and_number_spellings():
    text = '{\n// retain\n"precise":9007199254740993,"exponent":1.2300e+06,"env":{"KEEP":"x","TZ":null},\n}\n'
    updated = edit(text, ["env", "TZ"], "Asia/Tokyo")
    assert '"precise":9007199254740993,"exponent":1.2300e+06' in updated
    assert '// retain' in updated
    assert '"KEEP":"x"' in updated
    assert parse(updated)["env"]["TZ"] == "Asia/Tokyo"
    removed = edit(updated, ["env", "TZ"])
    assert parse(removed)["env"] == {"KEEP": "x"}


def test_removes_rows_without_discarding_comments():
    text = '{"rows":[\n// one\n{"name":"KEEP","value":1},\n{"name":"TZ", /* inner */ "value":"x"},\n// after\n]}\n'
    updated = edit(text, ["rows", 1])
    assert parse(updated) == {"rows": [{"name": "KEEP", "value": 1}]}
    for comment in ("// one", "/* inner */", "// after"):
        assert comment in updated


def test_adds_nested_containers_and_rows():
    text = edit("{}\n", ["env", "TZ"], "Asia/Tokyo")
    text = edit(text, ["env", "LANG"], "ja_JP.UTF-8")
    assert parse(text) == {"env": {"TZ": "Asia/Tokyo", "LANG": "ja_JP.UTF-8"}}
    text = edit(text, ["rows"], [])
    text = edit(text, ["rows", -1], {"name": "TZ", "value": None})
    assert parse(text)["rows"] == [{"name": "TZ", "value": None}]
    assert edit(text, ["missing", "field"]) == text


def test_tree_retains_duplicate_properties():
    tree = parse_document('{"env":{"TZ":"one","TZ":"two"}}')
    assert len(tree.properties("env")[0].children[0].properties("TZ")) == 2


@pytest.mark.parametrize("text", ['{"x":01}', '{"x":1e}', '{"x":"unterminated}', '{"x":NaN}', '{"x":true false}', '{"x":1,,}', '[] {}', '{/* missing'])
def test_rejects_malformed_documents(text):
    with pytest.raises(ValueError):
        parse(text)


def test_strict_json_rejects_comments_and_trailing_commas():
    for text in ('{"x":1,}', '{/*c*/"x":1}'):
        with pytest.raises(ValueError):
            parse(text, comments=False, trailing=False)
