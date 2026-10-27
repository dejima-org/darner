# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
from meeseeks_bot.markdown import render


def test_formatting_is_rendered():
    html = render("# Found it\n\n- **one**\n- `two`\n\n```\ncode\n```")
    assert "<h1>Found it</h1>" in html and "<strong>one</strong>" in html
    assert "<code>two</code>" in html and "<pre><code>code\n</code></pre>" in html


def test_link_shows_its_destination():
    html = render("See [the docs](https://evil.example/phish) and <https://example.org/x>.")
    assert "<a" not in html
    assert "the docs (https://evil.example/phish)" in html
    assert html.count("https://example.org/x") == 1


def test_image_shows_its_destination():
    html = render("![diagram](https://example.org/a.png)")
    assert "<img" not in html and "diagram (https://example.org/a.png)" in html


def test_raw_html_is_escaped():
    html = render('<a href="https://evil.example">docs</a> <script>x</script>')
    assert "<a " not in html and "<script>" not in html
    assert "&lt;a href=" in html


def test_destination_is_escaped():
    html = render('[x](https://example.org/"><b>)')
    assert "<b>" not in html
