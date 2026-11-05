# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
import pytest

from darner.configsets import ConfigSetError, available, description, load, parse

MEALIE = """# Adding a recipe to Mealie
blurb: mealie
harness: cline
backend: self-hosted
model: qwen3-14b
context_window: 30000
internal: recipes.example.com:8443
"""


def test_parse():
    found = parse("mealie", MEALIE)
    assert found["blurbs"] == ["mealie"] and found["internal"] == ["recipes.example.com:8443"]
    assert (found["harness"], found["backend"], found["model"], found["effort"]) == ("cline", "self-hosted", "qwen3-14b", None)
    assert found["context_window"] == "30000"


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


def test_load_combines_lists_and_agreeing_values(tmp_path):
    (tmp_path / "mealie").write_text(MEALIE)
    (tmp_path / "strong").write_text("harness: cline\neffort: high\nallow: pypi.org\nblurb: mealie\n")
    combined = load(tmp_path, ["mealie", "strong"])
    assert combined["harness"] == "cline" and combined["effort"] == "high" and combined["backend"] == "self-hosted"
    assert combined["blurbs"] == ["mealie"] and combined["domains"] == ["pypi.org"]


def test_config_sets_that_disagree_are_refused_whatever_their_order(tmp_path):
    (tmp_path / "mealie").write_text(MEALIE)
    (tmp_path / "strong").write_text("harness: claude-code\n")
    (tmp_path / "local").write_text("harness: cline\n")
    for names in (["mealie", "strong", "local"], ["strong", "local", "mealie"]):
        with pytest.raises(ConfigSetError) as e:
            load(tmp_path, names)
        assert "disagree on harness" in str(e.value) and "strong gives claude-code" in str(e.value)
        assert "give cline" in str(e.value) and "Set harness for the task to choose" in str(e.value)


def test_a_disagreement_the_request_settles_is_left_to_it(tmp_path):
    (tmp_path / "mealie").write_text(MEALIE)
    (tmp_path / "strong").write_text("harness: claude-code\n")
    combined = load(tmp_path, ["mealie", "strong"], overridden={"harness"})
    assert combined["harness"] is None and combined["backend"] == "self-hosted"


def test_the_earlier_spellings_still_work():
    found = parse("x", "ctx: a\nblurb: b\ndeps: pip:x\ndependency: apt:y\n")
    assert found["blurbs"] == ["a", "b"] and found["dependencies"] == ["pip:x", "apt:y"]


def test_unknown_config_set_lists_the_available_ones(tmp_path):
    (tmp_path / "mealie").write_text(MEALIE)
    (tmp_path / "Not-Valid").write_text("")
    (tmp_path / "dir").mkdir()
    assert available(tmp_path) == ["mealie"]
    with pytest.raises(ConfigSetError, match=r"unknown config set '../etc' \(available: mealie\)"):
        load(tmp_path, ["../etc"])


def test_files_are_loaded_from_the_files_directory(tmp_path):
    from darner.configsets import load_files

    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "mealie-token").write_text("secret")
    (tmp_path / "mealie").write_text("file: mealie-token\n")
    assert load(tmp_path, ["mealie"])["files"] == ["mealie-token"]
    assert load_files(tmp_path, ["mealie-token"]) == [("mealie-token", b"secret")]
    for bad in ["missing", "../mealie", ".hidden"]:
        with pytest.raises(ConfigSetError):
            load_files(tmp_path, [bad])
    (tmp_path / "files" / "big").write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ConfigSetError, match="over"):
        load_files(tmp_path, ["big"])


def test_file_errors_name_no_host_path(tmp_path):
    from darner.configsets import load_files

    (tmp_path / "files").mkdir()
    with pytest.raises(ConfigSetError) as e:
        load_files(tmp_path, ["missing"])
    assert str(tmp_path) not in str(e.value)


@pytest.mark.parametrize("text,expected", [
    ("# Recipes, from Mealie\nharness: cline\n", "Recipes, from Mealie"),
    ("# Recipes,\n#  from Mealie\n#\nharness: cline\n", "Recipes, from Mealie"),
    ("harness: cline\n# not a description\n", None),
    ("\n# not at the top\n", None),
    ("#\nharness: cline\n", None),
])
def test_description_is_the_comment_lines_at_the_top(text, expected):
    assert description(text) == expected
    parse("x", text)


@pytest.mark.skipif(__import__("os").geteuid() == 0, reason="root ignores permissions")
def test_an_unreadable_set_is_refused_without_its_path(tmp_path):
    (tmp_path / "mealie").write_text(MEALIE)
    (tmp_path / "mealie").chmod(0)
    with pytest.raises(ConfigSetError) as e:
        load(tmp_path, ["mealie"])
    assert str(e.value) == "couldn't read config set 'mealie'"
