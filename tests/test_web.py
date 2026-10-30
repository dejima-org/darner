# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
from html.parser import HTMLParser

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


class FakeApi:
    """darner-api as darner-web sees it, recording every call."""

    def __init__(self):
        self.calls = []
        self.sessions = [{
            "id": "dn-aaaaa", "target": "darner:dn-aaaaa", "link": "https://dejima.example.com/?arg=x",
            "requester": "<b>alice</b>", "started": 0, "state": "running", "answerable": True,
            "waiting": {"answer": "which db?", "domains": None},
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

    def app(self):
        async def record(request, result, status=200):
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            body = await request.json() if request.can_read_body else None
            self.calls.append((request.method, request.path, body))
            return web.json_response(result, status=status)

        async def tasks(request):
            return await record(request, self.sessions)

        async def task(request):
            found = [s for s in self.sessions if s["id"] == request.match_info["id"]]
            return await record(request, found[0] if found else {"error": "not found"}, 200 if found else 404)

        async def task_events(request):
            found = self.events.get(request.match_info["id"])
            return await record(request, found if found is not None else {"error": "not found"}, 200 if found is not None else 404)

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
                return await record(request, {"error": "not found"}, 404)
            return await record(request, "Use <script>the API</script>.\n")

        async def configs(request):
            return await record(request, ["recipes"])

        async def config(request):
            return await record(request, {"ctx": ["mealie"], "harness": "<i>cline</i>", "file": ["token"]})

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
            self.calls.append((request.method, request.path, dict(request.query)))
            return web.json_response({"path": "~/shared/uploads/" + request.query["name"], "size": len(self.uploaded), "told": False})

        async def catalog(request):
            return await record(request, {"harnesses": ["claude-code", "cline"], "backends": ["self-hosted"], "default_harness": "claude-code"})

        async def create(request):
            return await record(request, {"id": "dn-ccccc", "target": "darner:dn-ccccc"}, 201)

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
        app.router.add_post("/v1/tasks/{id}/domains/{n}/{verb}", action)
        app.router.add_post("/v1/events/{seq}/ack", ack)
        app.router.add_post("/v1/tasks/{id}/files", files)
        app.router.add_get("/v1/catalog", catalog)
        app.router.add_post("/v1/tasks", create)
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
    assert "Check 10, the end." in visible


async def test_sessions_page_lists_open_and_ended_sessions(client, fake):
    resp, html = await get(client, "/")
    assert resp.status == 200
    assert 'href="/sessions/dn-aaaaa"' in html and "a help request" in html
    assert 'href="/sessions/dn-bbbbb"' in html and "Ended, not yet cleared" in html
    assert "&lt;b&gt;alice&lt;/b&gt;" in html and "<b>alice" not in html
    assert 'href="https://dejima.example.com/?arg=x" rel="noopener noreferrer"' in html


async def test_session_page_shows_the_selftest_safely(client):
    resp, html = await get(client, "/sessions/dn-aaaaa")
    assert resp.status == 200
    for section in html.split('<li class="event ')[1:4]:
        assert_selftest_shown_safely(section)
    assert 'href="/sessions/dn-aaaaa/outputs/1.zip"' in html and "pypi.org" in html


async def test_the_selftest_check_catches_unsafe_html():
    with pytest.raises(AssertionError):
        assert_selftest_shown_safely(f"<p>{SELFTEST}</p>")
    assert_selftest_shown_safely(str(render(SELFTEST)))


async def test_an_ended_session_shows_its_output_and_a_purged_one_is_gone(client):
    resp, html = await get(client, "/sessions/dn-bbbbb")
    assert resp.status == 200 and "Only its output is kept" in html and "done" in html
    resp, html = await get(client, "/sessions/dn-zzzzz")
    assert resp.status == 404 and "darner keeps nothing about a session once it's cleared" in html


async def test_blurbs_and_config_sets_are_read_only_and_escaped(client):
    _, html = await get(client, "/blurbs")
    assert 'href="/blurbs/mealie"' in html
    resp, html = await get(client, "/blurbs/mealie")
    assert resp.status == 200 and "Use &lt;script&gt;the API&lt;/script&gt;." in html
    assert (await get(client, "/blurbs/nope"))[0].status == 404
    _, html = await get(client, "/configs/recipes")
    assert "harness: &lt;i&gt;cline&lt;/i&gt;" in html and "ctx: mealie" in html and "contents aren" in html


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
    assert '<option>cline</option>' in html and "the default (claude-code)" in html and '<option>self-hosted</option>' in html
    assert 'name="blurbs" value="mealie"' in html and 'name="configs" value="recipes"' in html
    assert 'name="ref" value="' in html


async def test_starting_a_task_builds_the_request_as_chat_would(client, fake):
    body = {
        "task": "add this recipe", "context": "from the site", "ref": "a" * 32,
        "blurbs": ["mealie"], "configs": ["recipes"], "domains": "allrecipes.example.org, cdn.example.org",
        "internal": "recipes.example.com:8443", "dependencies": "pip:requests  apt:jq",
        "harness": "cline", "backend": "", "model": " qwen3 ", "effort": "", "context_window": "30000",
    }
    resp = await client.post("/tasks", headers=POST, json=body)
    assert resp.status == 200 and await resp.json() == {"location": "/sessions/dn-ccccc"}
    [(path, sent)] = posted(fake)
    assert path == "/v1/tasks" and sent == {
        "task": "add this recipe", "context": "from the site", "blurbs": ["mealie"], "configs": ["recipes"],
        "grants": {"domains": ["allrecipes.example.org", "cdn.example.org"], "internal": ["recipes.example.com:8443"],
                   "dependencies": ["pip:requests", "apt:jq"]},
        "requester": "web", "source": {"kind": "web", "ref": "a" * 32},
        "harness": "cline", "model": "qwen3", "context_window": "30000",
    }


@pytest.mark.parametrize("body, message", [({"ref": "a" * 32}, "Say what the task is"), ({"task": "x", "ref": "../x"}, "Reload")])
async def test_a_task_without_a_task_or_a_proper_ref_isn_t_started(client, fake, body, message):
    resp = await client.post("/tasks", headers=POST, json=body)
    assert resp.status == 400 and message in (await resp.json())["error"] and posted(fake) == []


@pytest.mark.parametrize("path", [
    "/tasks", "/sessions/dn-aaaaa/message", "/sessions/dn-aaaaa/stop", "/sessions/dn-bbbbb/clear",
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
