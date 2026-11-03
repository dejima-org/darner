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
    assert result["status"] == 200 and result["expand"] == "Collapse all"
    # Once in the help request leading the page, and once in each of the two in its history.
    assert result["text"].count("Check 11, the end.") == 3 and "Test line 4:" in result["text"]
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


async def test_the_actions_work_under_the_csp(aiohttp_server):
    api = FakeApi()
    api.events["dn-ccccc"] = []
    api.server = await aiohttp_server(api.app())
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    config = {"api_url": str(api.server.make_url("/")), "api_token": TOKEN, "public_origin": origin}
    server = TestServer(make_app(config), host="127.0.0.1", port=port)
    await server.start_server()
    try:
        script = Path(__file__).parent / "browser" / "actions.mjs"
        proc = await asyncio.create_subprocess_exec("node", str(script), origin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = await proc.communicate()
        assert proc.returncode == 0, err.decode()
        result = json.loads(out)
    finally:
        await server.close()
    posted = [(path, body) for method, path, body in api.calls if method == "POST"]
    assert ("/v1/tasks/dn-aaaaa/message", {"text": "use postgres", "requester": "web"}) in posted
    [task] = [body for path, body in posted if path == "/v1/tasks"]
    assert task["task"] == "add this recipe" and task["blurbs"] == ["mealie"] and task["grants"]["domains"] == ["allrecipes.example.org"]
    assert (task["harness"], task["backend"], task["model"], task["name"]) == ("cline", "openrouter", "z-ai/glm-5.3", "recipes")
    assert result["named"]["Name"] == "dn-recipes"
    assert "context_window" not in task and "effort" not in task
    preview = result["preview"]
    # With nothing chosen, the catalog's defaults.
    assert preview["harness"] == "No change: claude-code, default"
    assert preview["before"]["Harness"] == "claude-codedefault" and preview["before"]["Backend"] == "anthropicdefault"
    assert preview["before"]["Model"] == "dejima decides" and "Context window" not in preview["before"]
    # A config set's values are what No change means, and narrow what's offered below them.
    recipes = preview["recipes"]
    assert recipes["harness"] == "No change: cline, from config sets"
    assert recipes["backends"] == ["", "openrouter", "self-hosted"]
    assert recipes["context_window"] == "No change: 30000, from config sets"
    assert recipes["shown"]["Harness"] == "clinefrom config sets" and recipes["shown"]["Backend"] == "self-hosteddefault"
    assert recipes["shown"]["Context window"] == "30000from config sets" and recipes["shown"]["Files"] == "token"
    # Blurbs are there to read, folded.
    assert recipes["blurbs"] == [["Blurb: mealie", False]]
    # A config set's backend the default harness can't use is refused before Start.
    assert preview["local"] == "Start would be refused: claude-code can't use the openrouter backend, only anthropic"
    agent = result["agent"]
    # With no harness chosen, the default harness's.
    assert agent["default"] == {"backends": ["", "anthropic"], "models": ["claude-opus-5-5[1m]"]}
    assert agent["cline"] == {
        "backends": ["", "openrouter", "self-hosted"], "efforts": ["", "none", "low", "medium", "high", "xhigh"],
        "models": ["qwen3.5-4b"], "context_window": "120000",
    }
    # Back to a harness that can't take them, the backend, model and context window go back to its defaults.
    assert agent["back"] == {"backend": "", "model": "", "context_window": "", "disabled": True}
    assert result["stop_failed"] == {
        "button": "Stop the session", "disabled": False, "busy": False, "status_hidden": True, "alert": "dn-aaaaa is not running",
    }
    assert result["stopping"]["button"] == "Stopping the session" and "s so far." in result["stopping"]["status"]
    assert ("/v1/tasks/dn-aaaaa/stop", {"requester": "web"}) in posted
    assert result["starting"]["button"] == "Starting the session" and "s so far." in result["starting"]["status"]
    assert result["landed"] == f"{origin}/sessions/dn-ccccc"
    assert result["violations"] == [] and result["errors"] == []


async def test_agent_chosen_addresses_are_seen_whole_on_a_phone(aiohttp_server):
    # An address long enough to run past a phone's screen hides its real host
    # unless it wraps: in a link's destination, and in a domain request above Allow.
    link = "https://github.com.exampleprojectreleaseartifactsmirrorcdn.download0123456789abcdefghij.evil.example/x"
    domain = "pypi.org.filespythonhostedmirrorcdndownload0123456789abcdefghijklmnopq.evil.example"
    api = FakeApi()
    api.events["dn-aaaaa"] = [
        {"seq": 1, "id": "dn-aaaaa", "type": "output", "summary": f"See [https://github.com/example/project/releases]({link}).",
         "size": 0, "signalled": True, "data": None},
        {"seq": 2, "id": "dn-aaaaa", "type": "domains", "summary": "", "size": 0,
         "data": {"request": 1, "rejected": 0, "domains": [{"domain": domain, "internal": False, "port": None, "denied": False}]}},
    ]
    api.server = await aiohttp_server(api.app())
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    config = {"api_url": str(api.server.make_url("/")), "api_token": TOKEN, "public_origin": origin}
    server = TestServer(make_app(config), host="127.0.0.1", port=port)
    await server.start_server()
    try:
        script = Path(__file__).parent / "browser" / "wrap.mjs"
        proc = await asyncio.create_subprocess_exec(
            "node", str(script), f"{origin}/sessions/dn-aaaaa", "github.com.example", "ghij.evil.example/x", "pypi.org.files", "opq.evil.example",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        out, err = await proc.communicate()
        assert proc.returncode == 0, err.decode()
        result = json.loads(out)
    finally:
        await server.close()
    # Positive controls: each address's start is found and inside its pane and the screen, so a missing one can't pass.
    assert result["found"] == {"github.com.example": True, "ghij.evil.example/x": True, "pypi.org.files": True, "opq.evil.example": True}
    assert not result["scrolls_sideways"] and result["header_lines"] == 1
