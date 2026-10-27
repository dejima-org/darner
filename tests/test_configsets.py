import pytest

from meeseeks.configsets import ConfigSetError, available, load, parse

MEALIE = """# Adding a recipe to Mealie
ctx: mealie
harness: cline
backend: self-hosted
model: qwen3-14b
internal: recipes.example.com:8443
"""


def test_parse():
    found = parse("mealie", MEALIE)
    assert found["blurbs"] == ["mealie"] and found["internal"] == ["recipes.example.com:8443"]
    assert (found["harness"], found["backend"], found["model"], found["effort"]) == ("cline", "self-hosted", "qwen3-14b", None)


@pytest.mark.parametrize("text, message", [
    ("context: x", "line 1: expected one of"),
    ("harness: a b", "line 1: harness takes one value"),
    ("harness: a\nharness: b", "line 2: harness takes one value"),
    ("just words", "line 1: expected one of"),
    ("config: other", "line 1: expected one of"),
])
def test_parse_refuses(text, message):
    with pytest.raises(ConfigSetError, match=message):
        parse("x", text)


def test_load_combines_in_order(tmp_path):
    (tmp_path / "mealie").write_text(MEALIE)
    (tmp_path / "strong").write_text("harness: claude-code\nmodel: opus\nallow: pypi.org\nctx: mealie\n")
    combined = load(tmp_path, ["mealie", "strong"])
    assert combined["harness"] == "claude-code" and combined["model"] == "opus" and combined["backend"] == "self-hosted"
    assert combined["blurbs"] == ["mealie"] and combined["domains"] == ["pypi.org"]


def test_unknown_config_set_lists_the_available_ones(tmp_path):
    (tmp_path / "mealie").write_text(MEALIE)
    (tmp_path / "Not-Valid").write_text("")
    (tmp_path / "dir").mkdir()
    assert available(tmp_path) == ["mealie"]
    with pytest.raises(ConfigSetError, match=r"unknown config set '../etc' \(available: mealie\)"):
        load(tmp_path, ["../etc"])
