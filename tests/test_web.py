# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
import asyncio
import hashlib
from html.parser import HTMLParser
from pathlib import Path

import pytest
from aiohttp import web

from darner.archive import clean_text
from darner.selftest import PAYLOAD
from darner.web import HEADERS, check_config, make_app
from darner.web.render import render

ORIGIN = "https://darner.example.com"
HOST = {"Host": "darner.example.com"}
POST = {**HOST, "Origin": ORIGIN, "X-Darner-Request": "1", "Sec-Fetch-Site": "same-origin"}
TOKEN = "w3b-0123456789abcdef0123456789abcdef"
SELFTEST = clean_text(PAYLOAD.encode())


CATALOG = {
    "harnesses": ["claude-code", "cline"], "backends": ["anthropic", "openrouter", "self-hosted"], "default_harness": "claude-code",
    "options": {
        "claude-code": {
            "backends": ["anthropic"], "efforts": ["low", "medium", "high", "xhigh"], "context_window": False,
            "defaults": {"backend": "anthropic"}, "models": {"anthropic": [{"id": "claude-opus-5-5[1m]"}]},
        },
        "cline": {
            "backends": ["openrouter", "self-hosted"], "efforts": ["none", "low", "medium", "high", "xhigh"], "context_window": True,
            "defaults": {"backend": "self-hosted"},
            "models": {"self-hosted": [{"id": "qwen3.5-4b", "context_window": 120000}], "openrouter": [{"id": "z-ai/glm-5.3"}]},
        },
    },
}


class FakeApi:
    """darner-api as darner-web sees it, recording every call."""

    def __init__(self):
        self.calls = []
        self.sessions = [{
            "id": "dn-aaaaa", "target": "darner:dn-aaaaa", "link": "https://dejima.example.com/?arg=x",
            "requester": "<b>alice</b>", "started": 0, "state": "running", "answerable": True,
            "waiting": {"answer": "which db?", "domains": None},
            "started_with": {"configs": ["recipes"], "harness": "<i>cline</i>", "context_window": 30000, "domains": ["pypi.org"]},
            "name": None,
        }]
        self.events = {
            "dn-aaaaa": [{"seq": 1, "id": "dn-aaaaa", "type": "output", "summary": SELFTEST, "size": 4, "signalled": True, "data": None},
                         {"seq": 2, "id": "dn-aaaaa", "type": "help", "summary": SELFTEST, "size": 0, "data": None},
                         {"seq": 3, "id": "dn-aaaaa", "type": "waiting", "summary": SELFTEST, "size": 0, "data": {"quiet": 300}},
                         {"seq": 4, "id": "dn-aaaaa", "type": "domains", "summary": "", "size": 0,
                          "data": {"request": 1, "rejected": 0, "domains": [{"domain": "pypi.org", "internal": False, "port": None, "denied": False}]}}],
            "dn-bbbbb": [{"seq": 5, "id": "dn-bbbbb", "type": "output", "summary": "done", "size": 0, "signalled": True, "data": None},
                         {"seq": 6, "id": "dn-bbbbb", "type": "ended", "summary": "", "size": 0, "data": None}],
        }
        self.zip = b"PK" + b"z" * 100
        self.chunked = False
        self.delay = 0
        self.create_status = 201
        self.upload_delay = 0
        self.check_status = 200

    def app(self):
        async def record(request, result, status=200):
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            await asyncio.sleep(self.delay)
            body = await request.json() if request.can_read_body else None
            self.calls.append((request.method, request.path, body))
            return web.json_response(result, status=status)

        async def tasks(request):
            return await record(request, self.sessions)

        # darner-api answers an unknown task, blurb or config set with aiohttp's plain-text 404.
        async def task(request):
            found = [s for s in self.sessions if s["id"] == request.match_info["id"]]
            if not found:
                raise web.HTTPNotFound()
            return await record(request, found[0])

        async def task_events(request):
            found = self.events.get(request.match_info["id"])
            if found is None:
                raise web.HTTPNotFound()
            return await record(request, found)

        async def events(request):
            return await record(request, [e for es in self.events.values() for e in es])

        async def output(request):
            self.calls.append((request.method, request.path, None))
            if not self.chunked:
                return web.Response(body=self.zip, content_type="application/zip")
            # No Content-Length, so the cap only shows mid-stream.
            response = web.StreamResponse(headers={"Content-Type": "application/zip"})
            response.enable_chunked_encoding()
            await response.prepare(request)
            for i in range(0, len(self.zip), 16):
                await response.write(self.zip[i:i + 16])
            await response.write_eof()
            return response

        async def blurbs(request):
            return await record(request, ["mealie"])

        async def blurb(request):
            if request.match_info["name"] != "mealie":
                raise web.HTTPNotFound()
            return await record(request, "Use <script>the API</script>.\n")

        async def configs(request):
            return await record(request, ["recipes", "local"])

        async def config(request):
            sets = {
                "recipes": {"blurb": ["mealie"], "harness": "<i>cline</i>", "file": ["token"], "description": "Adds <b>recipes</b>."},
                "local": {"backend": "openrouter"},
            }
            if request.match_info["name"] not in sets:
                raise web.HTTPNotFound()
            return await record(request, sets[request.match_info["name"]])

        async def selftest(request):
            return await record(request, {"id": "dn-ccccc"}, 201)

        async def action(request):
            return await record(request, {"granted": ["pypi.org"]} if request.path.endswith("/allow") else {}, 200)

        async def no_content(request):
            await record(request, None)
            return web.Response(status=204)

        async def ack(request):
            self.calls.append((request.method, request.path, None))
            return web.Response(status=204)

        async def files(request):
            self.uploaded = await request.read()
            await asyncio.sleep(self.upload_delay)
            self.calls.append((request.method, request.path, dict(request.query)))
            return web.json_response({"path": "~/shared/uploads/" + request.query["name"], "size": len(self.uploaded), "told": False})

        async def catalog(request):
            return await record(request, CATALOG)

        async def preview(request):
            body = await request.json()
            sets = {"recipes": {"harness": "cline", "context_window": 30000}, "local": {"backend": "openrouter"}}
            from_configs = {}
            for name in body.get("configs", []):
                from_configs |= sets.get(name, {})
            settings = from_configs | {k: body[k] for k in ("harness", "backend", "model", "effort", "context_window", "network") if k in body}
            self.calls.append((request.method, request.path, body))
            if settings.get("harness", "claude-code") == "claude-code" and settings.get("backend") == "openrouter":
                return web.json_response({"error": "claude-code can't use the openrouter backend, only anthropic"}, status=400)
            return web.json_response({
                "name": body.get("name"), "settings": settings, "from_configs": from_configs, "domains": body["grants"]["domains"], "internal_domains": [],
                "dependencies": [], "files": ["token"] if "recipes" in body.get("configs", []) else [],
                "blurbs": [{"name": n, "text": "Use <script>the API</script>.\n"} for n in body.get("blurbs", [])],
            })

        async def create(request):
            if self.create_status >= 400:
                return await record(request, {"error": "launch failed: dejima exited 1"}, self.create_status)
            return await record(request, {"id": "dn-ccccc", "target": "darner:dn-ccccc"}, self.create_status)

        app = web.Application()
        app.router.add_get("/v1/tasks", tasks)
        app.router.add_get("/v1/tasks/{id}", task)
        app.router.add_get("/v1/tasks/{id}/events", task_events)
        app.router.add_get("/v1/tasks/{id}/outputs/{seq}.zip", output)
        app.router.add_get("/v1/events", events)
        app.router.add_get("/v1/blurbs", blurbs)
        app.router.add_get("/v1/blurbs/{name}", blurb)
        app.router.add_get("/v1/configs", configs)
        app.router.add_get("/v1/configs/{name}", config)
        app.router.add_post("/v1/selftest", selftest)
        app.router.add_post("/v1/tasks/{id}/message", no_content)
        app.router.add_post("/v1/tasks/{id}/stop", no_content)

        async def check(request):
            if self.check_status != 200:
                return await record(request, {"error": "checked in the last 15s, perhaps by someone else on this frontend; try again in 9s"}, self.check_status)
            return await record(request, self.sessions[0])

        app.router.add_post("/v1/tasks/{id}/check", check)
        app.router.add_post("/v1/tasks/{id}/domains/{n}/{verb}", action)
        app.router.add_post("/v1/events/{seq}/ack", ack)
        app.router.add_post("/v1/tasks/{id}/files", files)
        app.router.add_get("/v1/catalog", catalog)
        app.router.add_post("/v1/tasks", create)
        app.router.add_post("/v1/tasks/preview", preview)
        return app


@pytest.fixture
async def fake(aiohttp_server):
    api = FakeApi()
    api.server = await aiohttp_server(api.app())
    return api


def config_for(api, **overrides):
    return {"api_url": str(api.server.make_url("/")), "api_token": TOKEN, "public_origin": ORIGIN, **overrides}


@pytest.fixture
async def client(aiohttp_client, fake):
    return await aiohttp_client(make_app(config_for(fake)))


async def get(client, path, headers=HOST):
    resp = await client.get(path, headers=headers)
    return resp, await resp.text()


class Shown(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags, self.text = [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def assert_selftest_shown_safely(html: str) -> None:
    """Each PASS in the self-test's checks, on a page of darner-web's that shows it."""
    shown = Shown(html)
    visible = "".join(shown.text)
    for tag, attrs in shown.tags:
        assert tag not in ("img", "iframe", "object", "embed", "b", "i") and not (tag == "script" and "src" not in attrs), tag
        assert not any(name.startswith("on") for name in attrs), "check 2: a handler came through"
        if tag == "a" and attrs.get("href", "").startswith(("http", "mailto")) and "selftest" in attrs["href"]:
            assert attrs["href"] in visible and attrs.get("rel") == "noopener noreferrer", "checks 3, 4, 7: a link hides where it goes"

    def test_line(n: int) -> str:
        return visible.split(f"Test line {n}:", 1)[1].split("Check ", 1)[0].strip()

    assert "<b>" in test_line(1) and "<script>" in test_line(2) and "<img" in test_line(2)
    assert '<a href="https://evil.selftest.invalid">' in test_line(3)
    assert "evil.selftest.invalid" in test_line(4) and "pixel.selftest.invalid" in test_line(5)
    assert "evil.selftest.invalid" in test_line(7) and test_line(8) == "abc"
    assert test_line(9).endswith("evil.selftest.invalid/x)")
    assert "Check 11, the end." in visible


async def test_sessions_page_lists_open_and_ended_sessions(client, fake):
    resp, html = await get(client, "/")
    assert resp.status == 200
    assert '<li class="card">' in html and 'href="/sessions/dn-aaaaa"' in html and "Waiting on a help request" in html
    assert 'href="/sessions/dn-bbbbb"' in html and "Ended, not yet cleared" in html
    assert "&lt;b&gt;alice&lt;/b&gt;" in html and "<b>alice" not in html
    assert 'href="https://dejima.example.com/?arg=x" rel="noopener noreferrer"' in html


async def test_session_page_shows_the_selftest_safely(client):
    resp, html = await get(client, "/sessions/dn-aaaaa")
    assert resp.status == 200
    sections = html.split('class="event ')[1:]
    assert [s.split('"')[0] for s in sections] == ["help", "domains", "waiting", "output"]
    for section in sections[:1] + sections[2:]:
        assert_selftest_shown_safely(section)
    assert 'href="/sessions/dn-aaaaa/outputs/1.zip"' in html and "pypi.org" in html


async def test_a_session_page_leads_with_what_it_waits_on_then_the_rest_newest_first(client, fake):
    resp, html = await get(client, "/sessions/dn-aaaaa")
    latest, rest = html.split('<section class="latest"', 1)[1].split('<section class="history">', 1)
    # Waiting on a help answer, so the help request leads, above the answer box.
    assert latest.index('class="event help"') < latest.index('id="message"')
    assert [int(x.split('"')[0]) for x in rest.split('data-seq="')[1:]] == [4, 3, 1]
    assert "<details" in rest and "<details class=\"event domains\" data-seq=\"4\" open" not in rest
    # Waiting on a domain decision, the request leads, with its buttons.
    fake.sessions[0]["waiting"] = {"answer": None, "domains": {"request": 1}}
    resp, html = await get(client, "/sessions/dn-aaaaa")
    latest = html.split('<section class="latest"', 1)[1].split("</section>", 1)[0]
    assert 'class="event domains"' in latest and "/domains/1/allow" in latest
    # Waiting on nothing, the newest leads.
    fake.sessions[0]["waiting"] = {"answer": None, "domains": None}
    resp, html = await get(client, "/sessions/dn-aaaaa")
    assert 'class="event domains"' in html.split('<section class="latest"', 1)[1].split("</section>", 1)[0]


async def test_a_session_page_says_what_it_was_started_with(client):
    resp, html = await get(client, "/sessions/dn-aaaaa")
    started = html.split('<section class="well started">', 1)[1].split("</section>", 1)[0]
    assert "<dt>Config sets</dt><dd>recipes</dd>" in started and "<dt>Context window</dt><dd>30000</dd>" in started
    assert "&lt;i&gt;cline&lt;/i&gt;" in started and "<i>" not in started


async def test_the_selftest_check_catches_unsafe_html():
    with pytest.raises(AssertionError):
        assert_selftest_shown_safely(f"<p>{SELFTEST}</p>")
    assert_selftest_shown_safely(str(render(SELFTEST)))


async def test_an_ended_session_shows_its_output_and_a_purged_one_is_gone(client):
    resp, html = await get(client, "/sessions/dn-bbbbb")
    assert resp.status == 200 and "Only its output is kept" in html and "done" in html
    # It leads with the output, not the ended notice the summary already gives.
    assert 'class="event output"' in html.split('<section class="latest"', 1)[1].split("</section>", 1)[0]
    resp, html = await get(client, "/sessions/dn-zzzzz")
    assert resp.status == 404 and "darner keeps nothing about a session once it's cleared" in html


async def test_blurbs_and_config_sets_are_read_only_and_escaped(client):
    _, html = await get(client, "/blurbs")
    assert 'href="/blurbs/mealie"' in html
    resp, html = await get(client, "/blurbs/mealie")
    assert resp.status == 200 and "Use &lt;script&gt;the API&lt;/script&gt;." in html
    assert (await get(client, "/blurbs/nope"))[0].status == 404
    _, html = await get(client, "/configs/recipes")
    assert "harness: &lt;i&gt;cline&lt;/i&gt;" in html and "blurb: mealie" in html and "contents aren" in html
    # The description is shown as such, not as a keyword.
    assert "<p>Adds &lt;b&gt;recipes&lt;/b&gt;.</p>" in html and "description:" not in html


async def test_every_response_carries_the_security_headers(client):
    for path, headers in (("/", HOST), ("/static/app.js", HOST), ("/sessions/dn-zzzzz", HOST), ("/", {"Host": "evil.example"})):
        resp = await client.get(path, headers=headers)
        for name, value in HEADERS.items():
            assert resp.headers.get(name) == value, (path, name)
    assert "require-trusted-types-for 'script'" in HEADERS["Content-Security-Policy"]


async def test_a_foreign_host_is_refused(client, fake):
    resp, text = await get(client, "/", {"Host": "darner.example.com.evil.example"})
    assert resp.status == 403 and fake.calls == []


@pytest.mark.parametrize("drop, change", [
    ("X-Darner-Request", None), ("Origin", None),
    (None, ("Origin", "https://evil.example")), (None, ("Sec-Fetch-Site", "cross-site")),
])
async def test_a_cross_site_post_is_refused(client, fake, drop, change):
    headers = {k: v for k, v in POST.items() if k != drop}
    if change:
        headers[change[0]] = change[1]
    resp = await client.post("/selftest", headers=headers, json={})
    assert resp.status == 403 and fake.calls == []


async def test_gets_change_nothing(client, fake):
    for path in ("/", "/sessions/dn-aaaaa", "/blurbs", "/blurbs/mealie", "/configs", "/configs/recipes", "/selftest", "/state/", "/new"):
        await client.get(path, headers=HOST)
    assert all(method == "GET" for method, _, _ in fake.calls)


async def test_the_selftest_runs_from_a_post(client, fake):
    resp = await client.post("/selftest", headers=POST, json={})
    assert resp.status == 200 and await resp.json() == {"location": "/sessions/dn-ccccc"}
    [(method, path, body)] = [c for c in fake.calls if c[0] == "POST"]
    assert path == "/v1/selftest" and body["requester"] == "web" and body["source"]["kind"] == "web"


async def test_the_identity_header_labels_the_user_and_is_required(aiohttp_client, fake):
    client = await aiohttp_client(make_app(config_for(fake, user_header="X-authentik-username")))
    assert (await client.get("/", headers=HOST)).status == 403
    assert fake.calls == []
    resp = await client.post("/selftest", headers={**POST, "X-authentik-username": "brad"}, json={})
    assert resp.status == 200
    assert [c[2]["requester"] for c in fake.calls if c[0] == "POST"] == ["brad"]


async def test_state_changes_when_the_page_would(client, fake):
    first = await (await client.get("/state/", headers=HOST)).json()
    assert first == await (await client.get("/state/", headers=HOST)).json()
    fake.sessions[0]["state"] = "starting"
    assert (await (await client.get("/state/", headers=HOST)).json())["version"] != first["version"]


async def test_a_zip_downloads_as_an_attachment_within_the_cap(client, fake):
    resp = await client.get("/sessions/dn-aaaaa/outputs/1.zip", headers=HOST)
    assert resp.status == 200 and await resp.read() == fake.zip
    assert resp.headers["Content-Disposition"] == 'attachment; filename="dn-aaaaa-1.zip"'
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


async def test_a_zip_over_the_cap_is_refused_before_anything_is_sent(aiohttp_client, fake):
    client = await aiohttp_client(make_app(config_for(fake, max_download=10)))
    resp, text = await get(client, "/sessions/dn-aaaaa/outputs/1.zip")
    assert resp.status == 502 and "over this darner-web's max_download" in text
    assert resp.headers["Content-Security-Policy"] == HEADERS["Content-Security-Policy"]


async def test_a_zip_that_passes_the_cap_mid_stream_fails_rather_than_truncating(aiohttp_client, fake):
    import aiohttp

    fake.chunked = True
    client = await aiohttp_client(make_app(config_for(fake, max_download=40)))
    resp = await client.get("/sessions/dn-aaaaa/outputs/1.zip", headers=HOST)
    assert resp.status == 200
    with pytest.raises(aiohttp.ClientPayloadError):
        await resp.read()


async def test_the_api_dropping_mid_download_fails_the_download_rather_than_hanging(aiohttp_server, aiohttp_client):
    import asyncio

    import aiohttp

    async def dropping(request):
        resp = web.StreamResponse(headers={"Content-Type": "application/zip"})
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        await resp.write(b"PK" + b"x" * 50)
        await asyncio.sleep(0.1)
        request.transport.close()
        return resp

    api = web.Application()
    api.router.add_get("/v1/tasks/{id}/outputs/{seq}.zip", dropping)
    server = await aiohttp_server(api)
    client = await aiohttp_client(make_app({"api_url": str(server.make_url("/")), "api_token": TOKEN, "public_origin": ORIGIN}))
    resp = await client.get("/sessions/dn-aaaaa/outputs/1.zip", headers=HOST, timeout=aiohttp.ClientTimeout(total=5))
    assert resp.status == 200
    with pytest.raises(aiohttp.ClientPayloadError):
        await asyncio.wait_for(resp.read(), 3)


async def test_a_download_with_the_api_down_is_an_error_page_with_the_headers(aiohttp_client, fake):
    client = await aiohttp_client(make_app(config_for(fake, api_url="http://127.0.0.1:1")))
    resp, html = await get(client, "/sessions/dn-aaaaa/outputs/1.zip")
    assert resp.status == 502 and "couldn&#39;t reach darner-api" in html
    for name, value in HEADERS.items():
        assert resp.headers.get(name) == value


async def test_an_unexpected_error_still_carries_the_headers(aiohttp_client, fake):
    app = make_app(config_for(fake))

    async def broken(request):
        raise KeyError("a template's data wasn't what it expected")

    app.router.add_get("/broken", broken)
    client = await aiohttp_client(app)
    resp, text = await get(client, "/broken")
    assert resp.status == 500 and text == "Something went wrong." and "KeyError" not in text
    for name, value in HEADERS.items():
        assert resp.headers.get(name) == value


@pytest.mark.parametrize("origin, host", [("https://darner.example.com:443", "darner.example.com"), ("http://darner.example.com:80", "darner.example.com"), ("https://darner.example.com:8443", "darner.example.com:8443")])
async def test_a_default_port_in_public_origin_matches_what_browsers_send(aiohttp_client, fake, origin, host):
    client = await aiohttp_client(make_app(config_for(fake, public_origin=origin)))
    browser_origin = origin.rsplit(":", 1)[0] if host == "darner.example.com" else origin
    assert (await client.get("/", headers={"Host": host})).status == 200
    resp = await client.post("/selftest", headers={**POST, "Host": host, "Origin": browser_origin}, json={})
    assert resp.status == 200


async def test_the_api_being_down_is_an_error_page(aiohttp_client, fake):
    client = await aiohttp_client(make_app(config_for(fake, api_url="http://127.0.0.1:1")))
    resp, html = await get(client, "/")
    assert resp.status == 502 and "couldn&#39;t reach darner-api" in html


@pytest.mark.parametrize("markdown, html", [
    ("[click](https://evil.example)", 'click (<a href="https://evil.example" rel="noopener noreferrer">https://evil.example</a>)'),
    ("<https://x.example>", '<a href="https://x.example" rel="noopener noreferrer">https://x.example</a>'),
    ("[x](ftp://files.example/a)", "x (ftp://files.example/a)"),
    ("[x](javascript:alert(1))", "[x](javascript:alert(1))"),
    ("![p](https://px.example/a.png)", "p (https://px.example/a.png)"),
    ("<b>hi</b>", "&lt;b&gt;hi&lt;/b&gt;"),
])
def test_agent_markdown_rules(markdown, html):
    assert html in render(markdown)


@pytest.mark.parametrize("change, message", [
    ({"public_origin": "https://darner.example.com/web"}, "public_origin"),
    ({"api_token": "short"}, "api_token"),
    ({"api_token": "replace-with-this-frontend's-token"}, "api_token"),
    ({"api_url": "ftp://x"}, "api_url"),
    ({"listne": "x"}, "unknown setting listne"),
    ({"user_header": ""}, "user_header"),
    ({"public_origin": "https://user@darner.example.com"}, "public_origin"),
    ({"launch_timeout": "300"}, "launch_timeout must be a positive number"),
    ({"max_upload": 0}, "max_upload must be a positive number"),
    ({"max_download": True}, "max_download must be a positive number"),
    ({"public_origin": "https://darner.example.com:notaport"}, "public_origin"),
])
def test_a_config_that_wouldn_t_work_is_refused(change, message):
    config = {"api_url": "http://127.0.0.1:8080", "api_token": TOKEN, "public_origin": ORIGIN, **change}
    with pytest.raises(ValueError, match=message):
        check_config(config)


def streaming_api(gaps):
    """An API whose zip arrives in chunks, with these pauses before each."""
    import asyncio

    async def out(request):
        resp = web.StreamResponse(headers={"Content-Type": "application/zip"})
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        for gap in gaps:
            await asyncio.sleep(gap)
            await resp.write(b"z" * 1000)
        await resp.write_eof()
        return resp

    api = web.Application()
    api.router.add_get("/v1/tasks/{id}/outputs/{seq}.zip", out)
    return api


async def test_a_slow_download_isn_t_held_to_the_api_calls_time_limit(aiohttp_server, aiohttp_client, monkeypatch):
    import darner.web

    monkeypatch.setattr(darner.web, "API_TIMEOUT", 1)
    monkeypatch.setattr(darner.web, "DOWNLOAD_READ_TIMEOUT", 0.6)
    server = await aiohttp_server(streaming_api([0.3] * 6))
    client = await aiohttp_client(make_app({"api_url": str(server.make_url("/")), "api_token": TOKEN, "public_origin": ORIGIN}))
    resp = await client.get("/sessions/dn-aaaaa/outputs/1.zip", headers=HOST)
    assert resp.status == 200 and len(await resp.read()) == 6000


async def test_a_stalled_api_still_cuts_a_download_off(aiohttp_server, aiohttp_client, monkeypatch, caplog):
    import asyncio

    import aiohttp

    import darner.web

    monkeypatch.setattr(darner.web, "DOWNLOAD_READ_TIMEOUT", 0.3)
    server = await aiohttp_server(streaming_api([0, 2]))
    client = await aiohttp_client(make_app({"api_url": str(server.make_url("/")), "api_token": TOKEN, "public_origin": ORIGIN}))
    resp = await client.get("/sessions/dn-aaaaa/outputs/1.zip", headers=HOST)
    with pytest.raises(aiohttp.ClientPayloadError):
        await asyncio.wait_for(resp.read(), 3)
    assert "Timeout" in caplog.text


def posted(fake):
    return [(path, body) for method, path, body in fake.calls if method == "POST"]


@pytest.mark.parametrize("path, api_path, body", [
    ("/sessions/dn-aaaaa/message", "/v1/tasks/dn-aaaaa/message", {"text": "use postgres"}),
    ("/sessions/dn-aaaaa/stop", "/v1/tasks/dn-aaaaa/stop", {}),
    ("/sessions/dn-aaaaa/domains/1/allow", "/v1/tasks/dn-aaaaa/domains/1/allow", {}),
    ("/sessions/dn-aaaaa/domains/1/deny", "/v1/tasks/dn-aaaaa/domains/1/deny", {}),
])
async def test_each_action_reaches_the_api_as_the_signed_in_user(aiohttp_client, fake, path, api_path, body):
    client = await aiohttp_client(make_app(config_for(fake, user_header="X-Remote-User")))
    resp = await client.post(path, headers={**POST, "X-Remote-User": "brad"}, json=body)
    assert resp.status == 200, await resp.text()
    [(sent_path, sent)] = posted(fake)
    assert sent_path == api_path and sent == {**body, "requester": "brad"}


async def test_an_empty_message_isn_t_sent(client, fake):
    resp = await client.post("/sessions/dn-aaaaa/message", headers=POST, json={"text": "  "})
    assert resp.status == 400 and posted(fake) == []


async def test_clear_refuses_a_running_session_and_acknowledges_an_ended_one(client, fake):
    resp = await client.post("/sessions/dn-aaaaa/clear", headers=POST, json={})
    assert resp.status == 409 and "still running" in (await resp.json())["error"] and posted(fake) == []
    resp = await client.post("/sessions/dn-bbbbb/clear", headers=POST, json={})
    assert resp.status == 200 and await resp.json() == {"location": "/"}
    assert [path for path, _ in posted(fake)] == ["/v1/events/5/ack", "/v1/events/6/ack"]


async def test_an_upload_streams_to_the_api(client, fake):
    resp = await client.post("/sessions/dn-aaaaa/files?name=notes.txt", headers=POST, data=b"some notes")
    assert resp.status == 200 and fake.uploaded == b"some notes"
    [(path, query)] = posted(fake)
    assert path == "/v1/tasks/dn-aaaaa/files" and query == {"name": "notes.txt", "requester": "web"}


async def test_an_upload_over_max_upload_is_refused(aiohttp_client, fake):
    client = await aiohttp_client(make_app(config_for(fake, max_upload=4)))
    resp = await client.post("/sessions/dn-aaaaa/files?name=big.bin", headers=POST, data=b"too large")
    assert resp.status == 413 and posted(fake) == []

    async def chunks():
        for _ in range(4):
            yield b"xx"

    resp = await client.post("/sessions/dn-aaaaa/files?name=big.bin", headers=POST, data=chunks())
    assert resp.status == 413 and "max_upload" in (await resp.json())["error"]


async def test_the_new_task_page_offers_what_the_api_has(client):
    resp, html = await get(client, "/new")
    assert resp.status == 200
    assert '<option>cline</option>' in html and '<option>self-hosted</option>' in html
    # The default harness reaches the page's script as the catalog's, never as darner's own choice.
    assert '"default_harness": "claude-code"' in html
    assert 'name="blurbs" value="mealie"' in html and 'name="configs" value="recipes"' in html
    assert 'name="ref" value="' in html


async def test_the_preview_asks_the_api_with_the_request_start_would_send(client, fake):
    body = {"task": "", "configs": ["recipes"], "blurbs": ["mealie"], "domains": "pypi.org", "harness": "", "network": "sealed"}
    resp = await client.post("/tasks/preview", headers=POST, json=body)
    assert resp.status == 200
    shown = await resp.json()
    assert shown["settings"] == {"harness": "cline", "context_window": 30000, "network": "sealed"} and shown["files"] == ["token"]
    [(path, sent)] = posted(fake)
    assert path == "/v1/tasks/preview" and sent["configs"] == ["recipes"] and sent["network"] == "sealed" and "harness" not in sent
    assert (await client.post("/tasks/preview", headers=HOST, json=body)).status == 403


async def test_a_refused_preview_says_why(client):
    resp = await client.post("/tasks/preview", headers=POST, json={"harness": "claude-code", "backend": "openrouter"})
    assert resp.status == 400 and "can't use the openrouter backend" in (await resp.json())["error"]


async def test_starting_a_task_builds_the_request_as_chat_would(client, fake):
    body = {
        "task": "add this recipe", "ref": "a" * 32,
        "blurbs": ["mealie"], "configs": ["recipes"], "domains": "allrecipes.example.org, cdn.example.org",
        "internal": "recipes.example.com:8443", "dependencies": "pip:requests  apt:jq",
        "harness": "cline", "backend": "", "model": " qwen3 ", "effort": "", "context_window": "30000", "network": "sealed",
        "name": " recipes ",
    }
    resp = await client.post("/tasks", headers=POST, json=body)
    assert resp.status == 200 and await resp.json() == {"location": "/sessions/dn-ccccc"}
    [(path, sent)] = posted(fake)
    assert path == "/v1/tasks" and sent == {
        "task": "add this recipe", "blurbs": ["mealie"], "configs": ["recipes"],
        "grants": {"domains": ["allrecipes.example.org", "cdn.example.org"], "internal": ["recipes.example.com:8443"],
                   "dependencies": ["pip:requests", "apt:jq"]},
        "requester": "web", "source": {"kind": "web", "ref": "a" * 32},
        "harness": "cline", "model": "qwen3", "context_window": "30000", "network": "sealed", "name": "recipes",
    }


async def test_check_now_asks_the_api_and_says_when_it_can_t_yet(client, fake):
    _, html = await get(client, "/sessions/dn-aaaaa")
    assert 'data-post="/sessions/dn-aaaaa/check"' in html
    resp = await client.post("/sessions/dn-aaaaa/check", headers=POST, json={})
    assert resp.status == 200 and posted(fake) == [("/v1/tasks/dn-aaaaa/check", None)]
    fake.check_status = 429
    resp = await client.post("/sessions/dn-aaaaa/check", headers=POST, json={})
    assert resp.status == 429 and "perhaps by someone else" in (await resp.json())["error"]


async def test_a_named_session_goes_by_its_name(client, fake):
    fake.sessions[0]["name"] = "recipes"
    _, html = await get(client, "/")
    assert '<a href="/sessions/dn-aaaaa" class="id">dn-recipes</a>' in html and '<p class="meta id">dn-aaaaa</p>' in html
    _, html = await get(client, "/sessions/dn-aaaaa")
    assert '<h1 class="id">dn-recipes</h1>' in html and "<title>dn-recipes · darner</title>" in html


@pytest.mark.parametrize("body, message", [
    ({"ref": "a" * 32}, "Say what the task is"), ({"task": "x", "ref": "../x"}, "Reload"),
    ({"task": "x", "ref": "A" * 32}, "Reload"), ({"task": "x", "ref": "\uff41" * 32}, "Reload"), ({"task": "x", "ref": "a" * 31}, "Reload"),
])
async def test_a_task_without_a_task_or_a_proper_ref_isn_t_started(client, fake, body, message):
    resp = await client.post("/tasks", headers=POST, json=body)
    assert resp.status == 400 and message in (await resp.json())["error"] and posted(fake) == []


@pytest.mark.parametrize("path", [
    "/tasks", "/sessions/dn-aaaaa/message", "/sessions/dn-aaaaa/stop", "/sessions/dn-aaaaa/check", "/sessions/dn-bbbbb/clear",
    "/sessions/dn-aaaaa/files?name=x", "/sessions/dn-aaaaa/domains/1/allow",
])
async def test_every_action_is_refused_cross_site(client, fake, path):
    headers = {**POST, "Origin": "https://evil.example"}
    assert (await client.post(path, headers=headers, json={"task": "x", "text": "x"})).status == 403
    assert (await client.post(path, headers={k: v for k, v in POST.items() if k != "X-Darner-Request"}, json={})).status == 403
    assert fake.calls == []


async def test_the_session_page_offers_actions_only_where_they_apply(client, fake):
    _, html = await get(client, "/sessions/dn-aaaaa")
    assert 'data-action="/sessions/dn-aaaaa/message"' in html and "Answer its help request" in html
    assert 'data-upload="/sessions/dn-aaaaa/files"' in html and 'data-post="/sessions/dn-aaaaa/stop"' in html
    assert "/domains/1/allow" not in html and "/clear" not in html
    fake.sessions[0]["waiting"] = {"answer": None, "domains": {"request": 1}}
    _, html = await get(client, "/sessions/dn-aaaaa")
    assert 'data-post="/sessions/dn-aaaaa/domains/1/allow"' in html and "Send it a message" in html
    _, html = await get(client, "/sessions/dn-bbbbb")
    assert 'data-post="/sessions/dn-bbbbb/clear"' in html and "/message" not in html


async def test_a_task_at_the_api_s_limits_in_a_three_byte_script_reaches_it(client, fake):
    task = "\u4e2d" * 32768
    resp = await client.post("/tasks", headers=POST, json={"task": task, "ref": "a" * 32})
    assert resp.status == 200, await resp.text()
    [(_, sent)] = posted(fake)
    assert sent["task"] == task and "context" not in sent


async def test_a_request_over_the_cap_gets_an_error_the_page_can_show(client, fake):
    resp = await client.post("/tasks", headers=POST, json={"task": "x" * (2 * 1024 * 1024), "ref": "a" * 32})
    assert resp.status == 413 and "more than darner-web takes" in (await resp.json())["error"]
    assert resp.headers["Content-Security-Policy"] == HEADERS["Content-Security-Policy"] and posted(fake) == []


async def test_a_slow_api_is_a_timeout_on_pages_and_actions(aiohttp_client, fake, monkeypatch):
    import darner.web

    monkeypatch.setattr(darner.web, "API_TIMEOUT", 0.3)
    fake.delay = 1
    client = await aiohttp_client(make_app(config_for(fake)))
    resp, html = await get(client, "/")
    assert resp.status == 504 and "darner-api didn&#39;t answer in time" in html
    assert resp.headers["Content-Security-Policy"] == HEADERS["Content-Security-Policy"]
    resp = await client.post("/sessions/dn-aaaaa/message", headers=POST, json={"text": "hi"})
    assert resp.status == 504 and (await resp.json())["error"] == "darner-api didn't answer in time"


async def test_a_slow_launch_says_the_task_may_still_be_starting(aiohttp_client, fake):
    fake.delay = 1
    client = await aiohttp_client(make_app(config_for(fake, launch_timeout=0.3)))
    resp = await client.post("/tasks", headers=POST, json={"task": "x", "ref": "a" * 32})
    error = (await resp.json())["error"]
    assert resp.status == 504 and "didn't answer in time" in error and "may still be starting: check Sessions" in error


async def test_a_form_that_already_started_a_task_says_so(client, fake):
    fake.create_status = 200
    resp = await client.post("/tasks", headers=POST, json={"task": "edited", "ref": "a" * 32})
    assert resp.status == 409
    assert (await resp.json())["error"] == "This form already started dn-ccccc. Reload the page to start another task."


async def test_an_upload_the_api_is_slow_to_confirm_may_still_have_arrived(aiohttp_client, fake):
    fake.upload_delay = 1
    client = await aiohttp_client(make_app(config_for(fake, launch_timeout=0.3)))
    resp = await client.post("/sessions/dn-aaaaa/files?name=notes.txt", headers=POST, data=b"some notes")
    error = (await resp.json())["error"]
    assert resp.status == 504 and "may still have reached the session" in error and fake.uploaded == b"some notes"


async def test_a_socket_timeout_is_a_timeout_not_an_unreachable_api(aiohttp_server, aiohttp_client, monkeypatch):
    import darner.web

    async def stalls(request):
        await asyncio.sleep(1)
        return web.Response(body=b"PK", content_type="application/zip")

    api = web.Application()
    api.router.add_get("/v1/tasks/{id}/outputs/{seq}.zip", stalls)
    server = await aiohttp_server(api)
    monkeypatch.setattr(darner.web, "DOWNLOAD_READ_TIMEOUT", 0.3)
    client = await aiohttp_client(make_app({"api_url": str(server.make_url("/")), "api_token": TOKEN, "public_origin": ORIGIN}))
    resp, html = await get(client, "/sessions/dn-aaaaa/outputs/1.zip")
    assert resp.status == 504 and "darner-api didn&#39;t answer in time" in html


async def test_a_launch_the_api_says_failed_is_shown_as_it_is(client, fake):
    fake.create_status = 502
    resp = await client.post("/tasks", headers=POST, json={"task": "x", "ref": "a" * 32})
    assert resp.status == 502 and (await resp.json())["error"] == "launch failed: dejima exited 1"


async def test_a_session_the_api_no_longer_lists_as_open_shows_as_ended(client, fake):
    # As a self-test's session does: it's ended from the start, and darner-api
    # answers GET /v1/tasks/<id> with a plain-text 404.
    resp, html = await get(client, "/sessions/dn-bbbbb")
    assert resp.status == 200 and "Only its output is kept" in html


async def test_a_config_set_without_files_renders(client):
    resp, html = await get(client, "/configs/local")
    assert resp.status == 200 and "backend: openrouter" in html and "contents aren" not in html
    assert (await get(client, "/configs/nope"))[0].status == 404


async def test_an_answer_that_isn_t_json_is_an_error_page(aiohttp_server, aiohttp_client):
    async def text(request):
        return web.Response(text="not json")

    api = web.Application()
    api.router.add_get("/v1/tasks", text)
    server = await aiohttp_server(api)
    client = await aiohttp_client(make_app({"api_url": str(server.make_url("/")), "api_token": TOKEN, "public_origin": ORIGIN}))
    resp, html = await get(client, "/")
    assert resp.status == 502 and "answer wasn&#39;t JSON" in html


def test_the_vendored_static_files_are_the_recorded_ones():
    static = Path(__file__).parent.parent / "darner" / "web" / "static"
    recorded = dict(reversed(line.split("  ", 1)) for line in (static / "sources.sha256").read_text().splitlines())
    # Text files are reviewed in diffs; anything else must be listed with its hash.
    reviewed = {"app.css", "app.js", "README.md", "sources.sha256"}
    present = {str(f.relative_to(static)) for f in static.rglob("*") if f.is_file() and "__pycache__" not in f.parts}
    assert set(recorded) == {name for name in present if name not in reviewed and not name.startswith("fonts/LICENSE-")}
    for name, digest in recorded.items():
        assert hashlib.sha256((static / name).read_bytes()).hexdigest() == digest, name
