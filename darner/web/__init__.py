# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
"""darner-web: a frontend for darner's sessions, from a browser.

It holds its own API token and stores nothing: every page is read from the
API when it's asked for. It has no login of its own. The operator's proxy
authenticates and authorizes every request (docs/web.md), and this defends
against what a proxy can't: cross-site requests, hostile agent text and DNS
rebinding.
"""

import argparse
import hashlib
import json
import logging
import secrets
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp
import jinja2
from aiohttp import web

from darner.web.render import render

log = logging.getLogger("darner.web")

CONFIG = web.AppKey("config", dict)
API = web.AppKey("api", aiohttp.ClientSession)
TEMPLATES = web.AppKey("templates", jinja2.Environment)
HERE = Path(__file__).parent

DEFAULTS = {"listen": "127.0.0.1:8081", "user_header": None, "max_download": 100 * 1024 * 1024}
REQUIRED = ("api_url", "api_token", "public_origin")
# The custom header every state-changing request carries. A cross-site page
# can't send it without a CORS preflight, which this never answers.
REQUEST_HEADER = "X-Darner-Request"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
    "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
    "require-trusted-types-for 'script'; trusted-types 'none'"
)
# Set on a request once its response has started, after which a failure can
# only cut the connection: a fresh error page can't follow a started body.
STARTED = web.RequestKey("started", bool)
HEADERS = {
    "Content-Security-Policy": CSP,
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
TASK_ID = "{id:dn-[a-z2-7]{5}}"


def check_config(config: dict) -> dict:
    """The config with defaults filled in, or ValueError saying what's wrong."""
    missing = [key for key in REQUIRED if key not in config]
    if missing:
        raise ValueError(f"missing setting {', '.join(missing)}")
    unknown = set(config) - set(DEFAULTS) - set(REQUIRED)
    if unknown:
        raise ValueError(f"unknown setting {', '.join(sorted(unknown))}")
    full = {**DEFAULTS, **config}
    origin = urlsplit(full["public_origin"])
    try:
        port = origin.port
    except ValueError:
        port = -1
    if (
        origin.scheme not in ("http", "https") or not origin.hostname or origin.path not in ("", "/")
        or origin.query or origin.fragment or "@" in origin.netloc or port == -1
    ):
        raise ValueError("public_origin must be a scheme, host and optional port, such as https://darner.example.com")
    host = f"[{origin.hostname}]" if ":" in origin.hostname else origin.hostname
    # A browser leaves a default port out of Host and Origin, so it's left out here too.
    if port is not None and port != {"http": 80, "https": 443}[origin.scheme]:
        host += f":{port}"
    full["public_origin"] = f"{origin.scheme}://{host}".lower()
    if not urlsplit(full["api_url"]).scheme in ("http", "https"):
        raise ValueError("api_url must start with http:// or https://")
    if not isinstance(full["api_token"], str) or len(full["api_token"]) < 32 or full["api_token"].startswith("replace-with"):
        raise ValueError("api_token must be this frontend's token from api.toml's [tokens], at least 32 characters")
    if full["user_header"] is not None and not (isinstance(full["user_header"], str) and full["user_header"].strip()):
        raise ValueError("user_header must be a header name, such as X-authentik-username")
    return full


def _user(request: web.Request) -> str:
    header = request.app[CONFIG]["user_header"]
    return request.headers.get(header, "").strip() if header else "web"


@web.middleware
async def guard(request: web.Request, handler):
    """Refuse what a proxy can't: a foreign host, a cross-site change, a missing identity."""
    try:
        refusal = _refusal(request)
        if refusal:
            raise web.HTTPForbidden(text=refusal)
        response = await handler(request)
    except web.HTTPException as e:
        e.headers.update(HEADERS)
        raise
    except Exception:
        log.exception("%s %s failed", request.method, request.path)
        if request.get(STARTED):
            _cut(request)
            raise
        # Anything unexpected still goes out with the headers, never as aiohttp's bare 500.
        return web.Response(status=500, text="Something went wrong.", headers=HEADERS)
    if not response.prepared:  # a streamed download set its own before sending them
        response.headers.update(HEADERS)
    return response


def _refusal(request: web.Request) -> str | None:
    config = request.app[CONFIG]
    origin = config["public_origin"]
    if request.host.lower() != urlsplit(origin).netloc:
        return "This isn't the host darner-web serves."
    header = config["user_header"]
    if header and not request.headers.get(header, "").strip():
        # The proxy should set this on every request; one that doesn't wouldn't strip a forged one either.
        return f"The proxy didn't send {header}."
    if request.method not in ("GET", "HEAD"):
        if request.headers.get(REQUEST_HEADER) != "1":
            return "Missing request header."
        if request.headers.get("Origin", "").lower() != origin:
            return "Cross-origin request."
        if request.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
            return "Cross-site request."
    return None


def _cut(request: web.Request) -> None:
    """End a started response so the browser sees it fail, rather than waiting on a body that never ends."""
    if request.transport is not None:
        request.transport.close()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def api(request: web.Request, method: str, path: str, **kwargs):
    """A JSON call to darner-api, as this frontend."""
    config = request.app[CONFIG]
    try:
        async with request.app[API].request(
            method, config["api_url"].rstrip("/") + path,
            headers={"Authorization": f"Bearer {config['api_token']}"}, **kwargs,
        ) as resp:
            data = await resp.json(content_type=None) if resp.status != 204 else None
            if resp.status >= 400:
                error = data.get("error") if isinstance(data, dict) else None
                raise ApiError(resp.status, error or f"darner-api answered {resp.status}")
            return data
    except aiohttp.ClientError as e:
        raise ApiError(502, f"couldn't reach darner-api: {type(e).__name__}")


def page(request: web.Request, template: str, state: str | None = None, status: int = 200, **context) -> web.Response:
    html = request.app[TEMPLATES].get_template(template).render(state=state, **context)
    return web.Response(text=html, status=status, content_type="text/html")


def _version(data) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


async def _sessions_data(request: web.Request) -> dict:
    sessions = await api(request, "GET", "/v1/tasks")
    open_ids = {s["id"] for s in sessions}
    # An ended session stays until it's cleared: its events are left unacknowledged till then.
    ended = sorted({e["id"] for e in await api(request, "GET", "/v1/events") if e["type"] == "ended"} - open_ids)
    return {"sessions": sessions, "ended": ended}


async def _session_data(request: web.Request, task_id: str) -> dict:
    events = await api(request, "GET", f"/v1/tasks/{task_id}/events")
    try:
        session = await api(request, "GET", f"/v1/tasks/{task_id}")
    except ApiError as e:
        if e.status != 404:
            raise
        session = None
    return {"session": session, "events": events}


async def sessions_page(request: web.Request) -> web.Response:
    data = await _sessions_data(request)
    return page(request, "sessions.html", state="/state/", version=_version(data), **data)


async def session_page(request: web.Request) -> web.Response:
    task_id = request.match_info["id"]
    try:
        data = await _session_data(request, task_id)
    except ApiError as e:
        if e.status == 404:
            return page(request, "gone.html", status=404, task_id=task_id)
        raise
    return page(request, "session.html", state=f"/state/sessions/{task_id}", version=_version(data), task_id=task_id, **data)


async def state(request: web.Request) -> web.Response:
    """What a page shows, as a version its script compares, so it reloads only when something changed."""
    task_id = request.match_info.get("id")
    try:
        data = await (_session_data(request, task_id) if task_id else _sessions_data(request))
    except ApiError as e:
        return web.json_response({"version": None, "error": str(e)}, status=e.status if e.status < 500 else 502)
    return web.json_response({"version": _version(data)})


async def download(request: web.Request) -> web.StreamResponse:
    """An output's zip, streamed from the API with a size cap, always as an attachment.

    Over the cap, it's refused before anything is sent. Once sending has
    started, anything going wrong, the cap passed mid-stream or the API
    dropping, cuts the connection, so a browser marks the download failed
    rather than saving a truncated zip or waiting forever.
    """
    config = request.app[CONFIG]
    task_id, seq = request.match_info["id"], request.match_info["seq"]
    url = f"{config['api_url'].rstrip('/')}/v1/tasks/{task_id}/outputs/{seq}.zip"
    try:
        upstream = await request.app[API].get(url, headers={"Authorization": f"Bearer {config['api_token']}"})
    except aiohttp.ClientError as e:
        raise ApiError(502, f"couldn't reach darner-api: {type(e).__name__}")
    async with upstream:
        if upstream.status != 200:
            raise web.HTTPNotFound(text="That output isn't available.")
        if upstream.content_length is not None and upstream.content_length > config["max_download"]:
            raise web.HTTPBadGateway(text=f"That zip is {upstream.content_length} bytes, over this darner-web's max_download.")
        response = web.StreamResponse(headers={
            "Content-Type": "application/zip",
            "Content-Disposition": f'attachment; filename="{task_id}-{seq}.zip"',
        })
        response.headers.update(HEADERS)
        await response.prepare(request)
        request[STARTED] = True
        sent = 0
        try:
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                sent += len(chunk)
                if sent > config["max_download"]:
                    raise ValueError("max_download passed mid-stream")
                await response.write(chunk)
            await response.write_eof()
        except Exception as e:
            log.warning("download of %s output %s cut off: %s", task_id, seq, e)
            _cut(request)
        return response


async def blurbs_page(request: web.Request) -> web.Response:
    return page(request, "names.html", kind="blurbs", title="Context blurbs", names=await api(request, "GET", "/v1/blurbs"))


async def blurb_page(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    try:
        text = await api(request, "GET", f"/v1/blurbs/{quote(name, safe='')}")
    except ApiError as e:
        if e.status == 404:
            raise web.HTTPNotFound(text="There's no blurb by that name.")
        raise
    return page(request, "blurb.html", name=name, text=text)


async def configs_page(request: web.Request) -> web.Response:
    return page(request, "names.html", kind="configs", title="Config sets", names=await api(request, "GET", "/v1/configs"))


async def config_page(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    try:
        described = await api(request, "GET", f"/v1/configs/{quote(name, safe='')}")
    except ApiError as e:
        if e.status == 404:
            raise web.HTTPNotFound(text="There's no config set by that name.")
        raise
    return page(request, "config.html", name=name, described=described)


async def selftest_page(request: web.Request) -> web.Response:
    return page(request, "selftest.html")


async def run_selftest(request: web.Request) -> web.Response:
    source = {"kind": "web", "ref": secrets.token_hex(8)}
    try:
        result = await api(request, "POST", "/v1/selftest", json={"requester": _user(request), "source": source})
    except ApiError as e:
        return web.json_response({"error": str(e)}, status=e.status if e.status < 500 else 502)
    return web.json_response({"location": f"/sessions/{result['id']}"})


@web.middleware
async def api_errors(request: web.Request, handler):
    try:
        return await handler(request)
    except ApiError as e:
        log.warning("%s %s: %s", request.method, request.path, e)
        if request.get(STARTED):
            _cut(request)
            raise
        return page(request, "error.html", status=e.status if e.status < 500 else 502, message=str(e))


def _age(started) -> str:
    minutes = max(0, int(time.time() - started) // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours < 24 else f"{hours // 24}d{hours % 24}h"


def make_app(config: dict) -> web.Application:
    config = check_config(config)
    app = web.Application(middlewares=[guard, api_errors], client_max_size=64 * 1024)
    app[CONFIG] = config
    templates = jinja2.Environment(
        loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True, undefined=jinja2.StrictUndefined,
    )
    templates.filters["agent"] = render
    templates.filters["age"] = _age
    app[TEMPLATES] = templates

    async def client(app: web.Application):
        app[API] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        yield
        await app[API].close()

    app.cleanup_ctx.append(client)
    app.router.add_get("/", sessions_page)
    app.router.add_get(f"/sessions/{TASK_ID}", session_page)
    app.router.add_get(f"/sessions/{TASK_ID}/outputs/{{seq:\\d+}}.zip", download)
    app.router.add_get("/state/", state)
    app.router.add_get(f"/state/sessions/{TASK_ID}", state)
    app.router.add_get("/blurbs", blurbs_page)
    app.router.add_get("/blurbs/{name}", blurb_page)
    app.router.add_get("/configs", configs_page)
    app.router.add_get("/configs/{name}", config_page)
    app.router.add_get("/selftest", selftest_page)
    app.router.add_post("/selftest", run_selftest)
    app.router.add_static("/static", HERE / "static")
    return app


def main() -> None:
    p = argparse.ArgumentParser(prog="darner-web")
    p.add_argument("--config", default="/etc/darner/web.toml", type=Path)
    p.add_argument("--listen", help="host:port, overriding the config's listen")
    args = p.parse_args()
    try:
        config = tomllib.loads(args.config.read_text())
        if args.listen:
            config["listen"] = args.listen
        app = make_app(config)
        host, _, port = app[CONFIG]["listen"].rpartition(":")
        port = int(port)
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"darner-web: {args.config}: {e}")
    except (OSError, ValueError) as e:
        sys.exit(f"darner-web: {args.config}: {e}")
    logging.basicConfig(level=logging.INFO)
    log.info("serving %s on %s:%s", app[CONFIG]["public_origin"], host, port)
    web.run_app(app, host=host, port=port, print=None)
