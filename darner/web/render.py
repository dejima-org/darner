# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
"""Markdown for agent-written text, rendered so it can't mislead.

The same rules as the Matrix bot's renderer, which this can't share: the
plugin ships to maubot on its own. Raw HTML stays escaped. A link shows its
destination in plain sight, and only an http, https or mailto destination
is ever clickable. An image is never loaded: its address is shown as text.
"""

from html import escape

from markdown_it import MarkdownIt
from markupsafe import Markup

SCHEMES = ("http://", "https://", "mailto:")


def _link_text(tokens, idx) -> str:
    """The plain text between a link_open and its link_close."""
    text, depth = [], 0
    for token in tokens[idx + 1:]:
        if token.type == "link_open":
            depth += 1
        elif token.type == "link_close":
            if depth == 0:
                break
            depth -= 1
        elif token.type in ("text", "code_inline"):
            text.append(token.content)
    return "".join(text)


def _link_open(self, tokens, idx, options, env):
    href = tokens[idx].attrGet("href") or ""
    clickable = href.lower().startswith(SCHEMES)
    # A link whose text is its own destination hides nothing, so its text is the link.
    wraps = clickable and _link_text(tokens, idx) == href
    env.setdefault("links", []).append((href, clickable, wraps))
    return f'<a href="{escape(href)}" rel="noopener noreferrer">' if wraps else ""


def _link_close(self, tokens, idx, options, env):
    href, clickable, wraps = env["links"].pop()
    if wraps:
        return "</a>"
    if clickable:
        return f' (<a href="{escape(href)}" rel="noopener noreferrer">{escape(href)}</a>)'
    return f" ({escape(href)})"


def _image(self, tokens, idx, options, env):
    token = tokens[idx]
    alt = "".join(child.content for child in token.children or [])
    return f"{escape(alt)} ({escape(token.attrGet('src') or '')})"


_md = MarkdownIt("commonmark", {"html": False})
_md.add_render_rule("link_open", _link_open)
_md.add_render_rule("link_close", _link_close)
_md.add_render_rule("image", _image)


def render(text: str) -> Markup:
    return Markup(_md.render(text, {}))
