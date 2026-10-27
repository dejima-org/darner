import pytest

from meeseeks_bot.parse import ParseError, Request, parse_reply, strip_mention

NAMES = ["@meeseeks:example.org", "Meeseeks"]


def parse(body):
    return parse_reply(body, NAMES)


def test_design_example():
    req = parse(
        "@meeseeks fix the flaky tests, use pytest-xdist\n"
        "ctx: myapp\n"
        "allow: pypi.org, files.pythonhosted.org\n"
        "deps: apt:python3-venv pip:pytest-xdist\n"
        "harness: claude-code\n"
        "backend: openrouter\n"
    )
    assert req == Request(
        context="fix the flaky tests, use pytest-xdist",
        blurbs=["myapp"],
        domains=["pypi.org", "files.pythonhosted.org"],
        dependencies=["apt:python3-venv", "pip:pytest-xdist"],
        harness="claude-code",
        backend="openrouter",
    )


def test_mixed_lines_and_case():
    req = parse("first\nALLOW: a.org\nmiddle\n  Deps : pip:x,apt:y\nlast")
    assert req.context == "first\nmiddle\nlast"
    assert req.domains == ["a.org"]
    assert req.dependencies == ["pip:x", "apt:y"]
    assert req.harness is None and req.backend is None


def test_repeated_keywords_append_and_dedup():
    req = parse("allow: a.org b.org\nallow: b.org, c.org,a.org\nctx: x\nctx: x y")
    assert req.domains == ["a.org", "b.org", "c.org"]
    assert req.blurbs == ["x", "y"]


def test_empty_keyword_value():
    assert parse("allow:\ndo it").domains == []


def test_keyword_mid_line_is_context():
    req = parse("please do not allow: evil.org here\nnote ctx: nope")
    assert req.context == "please do not allow: evil.org here\nnote ctx: nope"
    assert req.domains == [] and req.blurbs == []


@pytest.mark.parametrize("key", ["harness", "backend"])
def test_single_value_keywords(key):
    assert getattr(parse(f"{key}: a\n{key}: a"), key) == "a"
    with pytest.raises(ParseError, match=f"{key} takes one value"):
        parse(f"{key}: a b")
    with pytest.raises(ParseError, match=f"{key} takes one value"):
        parse(f"{key}: a\n{key}: b")


@pytest.mark.parametrize(
    "body",
    [
        "@meeseeks:example.org do it",
        "@meeseeks:example.org: do it",
        "meeseeks: do it",
        "Meeseeks do it",
        "@Meeseeks do it",
        "@meeseeks: do it",
        "MEESEEKS: do it",
    ],
)
def test_leading_mention_forms(body):
    assert parse(body).context == "do it"


def test_mention_mid_line():
    assert parse("hey @meeseeks:example.org do it").context == "hey do it"
    assert parse("thanks Meeseeks!").context == "thanks !"


def test_mention_not_inside_word():
    assert parse("Meeseeks: the meeseeksbox repo").context == "the meeseeksbox repo"


def test_mention_before_keyword_line():
    req = parse("@meeseeks:example.org ctx: myapp")
    assert req.blurbs == ["myapp"] and req.context == ""


def test_keyword_values_keep_bot_name():
    assert parse("Meeseeks\nctx: meeseeks").blurbs == ["meeseeks"]


def test_empty_context():
    assert parse("@meeseeks:example.org\nallow: a.org").context == ""
    assert parse("").context == ""


def test_reply_fallback_stripped():
    body = "> <@alice:example.org> the note\n> more\n\nmeeseeks: go"
    assert parse(body).context == "go"


def test_no_names():
    assert parse_reply("meeseeks: go", []).context == "meeseeks: go"


def test_model_and_effort_keywords():
    req = parse_reply("@meeseeks:example.org do it\nmodel: claude-opus-5[1m]\neffort: high", ["@meeseeks:example.org"])
    assert req.model == "claude-opus-5[1m]" and req.effort == "high" and req.context == "do it"


@pytest.mark.parametrize("key", ["model", "effort"])
def test_model_and_effort_take_one_value(key):
    with pytest.raises(ParseError, match=f"{key} takes one value"):
        parse_reply(f"go\n{key}: a b", ["@meeseeks:example.org"])


BOT_ID = "@meeseeks:example.org"


@pytest.mark.parametrize(
    "link",
    [
        "[@meeseeks:example.org](https://matrix.to/#/@meeseeks:example.org)",
        "[Meeseeks](https://matrix.to/#/%40meeseeks%3Aexample.org)",
        "[@meeseeks:example.org](https://matrix.to/#/@meeseeks:example.org?via=example.org)",
        "[Meeseeks](https://chat.example.org/#/user/@meeseeks:example.org)",
    ],
)
def test_markdown_pill_is_stripped(link):
    assert strip_mention(f"{link} help", [BOT_ID, "Meeseeks"]).strip() == "help"
    req = parse_reply(f"{link} fix it\nallow: pypi.org", [BOT_ID, "Meeseeks"])
    assert req.context == "fix it" and req.domains == ["pypi.org"]


def test_links_to_others_are_kept():
    body = "[@meeseeks:example.org](https://matrix.to/#/@meeseeks:example.org) ask [Alice](https://matrix.to/#/@alice:example.org) and see [docs](https://example.org/x)"
    assert parse_reply(body, [BOT_ID]).context == "ask [Alice](https://matrix.to/#/@alice:example.org) and see [docs](https://example.org/x)"


def test_internal_keyword():
    req = parse_reply("go\ninternal: recipes.example.com:8443, wiki.example.com", ["@meeseeks:example.org"])
    assert req.internal_domains == ["recipes.example.com:8443", "wiki.example.com"] and req.domains == []
