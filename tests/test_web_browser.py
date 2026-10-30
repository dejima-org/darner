# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
"""darner-web's session page, showing the self-test, in a real browser.

Needs Node with Playwright and its Chromium, which a dejima sandbox has and
CI doesn't, so it's skipped without them and is on the release checklist.
"""

import asyncio
import json
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer

from darner.web import TEMPLATES, make_app
from tests.test_web import TOKEN, FakeApi


def _playwright() -> bool:
    if not shutil.which("node"):
        return False
    probe = "require('playwright').chromium.executablePath()"
    found = subprocess.run(["node", "-e", f"console.log({probe})"], capture_output=True, text=True)
    return found.returncode == 0 and Path(found.stdout.strip()).exists()


pytestmark = pytest.mark.skipif(not _playwright(), reason="needs Node, Playwright and its Chromium")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _load_selftest(aiohttp_server, unsafe: bool = False) -> dict:
    api = FakeApi()
    api.server = await aiohttp_server(api.app())
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    config = {"api_url": str(api.server.make_url("/")), "api_token": TOKEN, "public_origin": origin}
    app = make_app(config)
    if unsafe:
        from markupsafe import Markup

        app[TEMPLATES].filters["agent"] = Markup  # what a frontend that doesn't escape would do
    server = TestServer(app, host="127.0.0.1", port=port)
    await server.start_server()
    try:
        script = Path(__file__).parent / "browser" / "selftest.mjs"
        proc = await asyncio.create_subprocess_exec(
            "node", str(script), f"{origin}/sessions/dn-aaaaa", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        out, err = await proc.communicate()
        assert proc.returncode == 0, err.decode()
        result = json.loads(out)
    finally:
        await server.close()
    return result


async def test_the_selftest_does_nothing_in_a_real_browser(aiohttp_server):
    result = await _load_selftest(aiohttp_server)
    # Positive controls: the page loaded, showed the whole self-test, and the
    # request listener saw the page's own requests, so an empty result means something.
    assert result["status"] == 200
    assert "Check 10, the end." in result["text"] and "Test line 4:" in result["text"]
    assert any(url.endswith("/static/app.js") for url in result["own_requests"])
    assert result["dialogs"] == [], "check 2: a script ran"
    assert result["hostile_requests"] == [], "checks 2 and 5: something loaded from the agent's addresses"
    assert result["violations"] == [], "the page broke its own CSP"
    assert result["images"] == 0 and result["bold_test_line"] == 0, "checks 1 and 5"
    assert "evil.selftest.invalid" in result["text"].split("Test line 4:", 1)[1].split("Check", 1)[0]


async def test_the_browser_check_catches_a_frontend_that_doesn_t_escape(aiohttp_server):
    result = await _load_selftest(aiohttp_server, unsafe=True)
    assert result["bold_test_line"] > 0 or result["images"] > 0
    # The CSP is the second layer: the injected script and image are blocked, and say so.
    assert result["violations"] and result["dialogs"] == []
