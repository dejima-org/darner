# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
"""Markdown for agent-written text, rendered so it can't mislead.

Raw HTML stays escaped, and a link or image shows its destination in plain
sight instead of hiding it behind other text.
"""

from mautrix.util.markdown import HtmlEscapingRenderer, md_parser


def _only_text(node) -> str | None:
    child = node.first_child
    if child is not None and child is node.last_child and child.t == "text":
        return child.literal
    return None


class _Renderer(HtmlEscapingRenderer):
    def link(self, node, entering):
        if not entering and _only_text(node) != node.destination:
            self.out(f" ({node.destination})")

    def image(self, node, entering):
        if not entering:
            self.out(f" ({node.destination})")


_renderer = _Renderer()


def render(text: str) -> str:
    return _renderer.render(md_parser.parse(text))
