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
import re
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
# A JSON call to the API has two minutes in all. A download has no total,
# since it goes at the browser's pace, but the API going quiet for longer
# than DOWNLOAD_READ_TIMEOUT between reads still ends it.
API_TIMEOUT = 120
DOWNLOAD_READ_TIMEOUT = 60

DEFAULTS = {
    "listen": "127.0.0.1:8081", "user_header": None, "max_download": 100 * 1024 * 1024, "max_upload": 50 * 1024 * 1024,
    # Longer than the API's own launch_timeout, so a slow launch is answered rather than abandoned.
    "launch_timeout": 300,
}
# Room for the API's longest text fields, 32,768 characters each, at JSON's
# worst case of six bytes a character, with the rest of a request beside them.
# Uploads stream, so they aren't held to it.
MAX_REQUEST = 1024 * 1024
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
# What a session was started with, as its page labels it, in order.
SETTING_LABELS = {
    "configs": "Config sets", "blurbs": "Blurbs", "harness": "Harness", "backend": "Backend", "model": "Model",
    "effort": "Effort", "context_window": "Context window", "network": "Reach", "domains": "Domains",
    "internal_domains": "Internal domains", "dependencies": "Dependencies", "files": "Files",
}


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
    for key in ("max_download", "max_upload", "launch_timeout"):
        if type(full[key]) not in (int, float) or full[key] <= 0:
            raise ValueError(f"{key} must be a positive number")
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
    except web.HTTPRequestEntityTooLarge:
        # The page's script shows an action's error as a sentence, so it gets one.
        return web.json_response({"error": "That's more than darner-web takes in one request."}, status=413, headers=HEADERS)
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


def _status(e: "ApiError") -> int:
    """What to answer for an API error: its own status, a timeout as a timeout, anything else upstream as 502."""
    return e.status if e.status < 500 or e.status == 504 else 502


class ApiError(Exception):
    """A call to darner-api that failed. answered says the API itself said so, rather
    than the call timing out or not getting through, which leaves the outcome unknown."""

    def __init__(self, status: int, message: str, answered: bool = True):
        super().__init__(message)
        self.status = status
        self.answered = answered


async def _json(resp: aiohttp.ClientResponse):
    """A response's JSON, or None if it has none. darner-api answers some errors,
    such as an unknown task's 404, in aiohttp's plain text."""
    if resp.status == 204:
        return None
    try:
        return await resp.json(content_type=None)
    except ValueError:
        return None


async def api(request: web.Request, method: str, path: str, with_status: bool = False, **kwargs):
    """A JSON call to darner-api, as this frontend: its result, with its status if asked."""
    config = request.app[CONFIG]
    try:
        async with request.app[API].request(
            method, config["api_url"].rstrip("/") + path,
            headers={"Authorization": f"Bearer {config['api_token']}"}, **kwargs,
        ) as resp:
            data = await _json(resp)
            if resp.status >= 400:
                error = data.get("error") if isinstance(data, dict) else None
                raise ApiError(resp.status, error or f"darner-api answered {resp.status}")
            if data is None and resp.status != 204:
                raise ApiError(502, "darner-api's answer wasn't JSON")
            return (resp.status, data) if with_status else data
    # First: aiohttp's socket timeouts are ClientErrors as well as TimeoutErrors.
    except TimeoutError:
        raise ApiError(504, "darner-api didn't answer in time", answered=False)
    except aiohttp.ClientError as e:
        raise ApiError(502, f"couldn't reach darner-api: {type(e).__name__}", answered=False)


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
    latest, history = _latest(data["session"], data["events"])
    return page(
        request, "session.html", state=f"/state/sessions/{task_id}", version=_version(data), task_id=task_id,
        latest=latest, history=history, **data,
    )


def _latest(session: dict | None, events: list[dict]) -> tuple[dict | None, list[dict]]:
    """The event a session's page leads with, and the rest, newest first.

    That's what the session is waiting on you for, a domain request or else a
    help request, or failing that the newest event, passing over the ended
    notice the page already says in its summary.
    """
    waiting = session["waiting"] if session else {"answer": None, "domains": None}

    def newest(match) -> dict | None:
        return next((e for e in reversed(events) if match(e)), None)

    chosen = (
        (waiting["domains"] and newest(lambda e: e["type"] == "domains" and e["data"]["request"] == waiting["domains"]["request"]))
        or (waiting["answer"] is not None and newest(lambda e: e["type"] == "help"))
        or newest(lambda e: e["type"] != "ended")
        or (events[-1] if events else None)
    )
    return chosen, [e for e in reversed(events) if e is not chosen]


async def state(request: web.Request) -> web.Response:
    """What a page shows, as a version its script compares, so it reloads only when something changed."""
    task_id = request.match_info.get("id")
    try:
        data = await (_session_data(request, task_id) if task_id else _sessions_data(request))
    except ApiError as e:
        return web.json_response({"version": None, "error": str(e)}, status=_status(e))
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
        upstream = await request.app[API].get(
            url, headers={"Authorization": f"Bearer {config['api_token']}"},
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=DOWNLOAD_READ_TIMEOUT),
        )
    except TimeoutError:  # first, as in api()
        raise ApiError(504, "darner-api didn't answer in time", answered=False)
    except aiohttp.ClientError as e:
        raise ApiError(502, f"couldn't reach darner-api: {type(e).__name__}", answered=False)
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
            log.warning("download of %s output %s cut off: %r", task_id, seq, e)
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
    description = described.pop("description", None)
    return page(request, "config.html", name=name, described=described, description=description)


async def selftest_page(request: web.Request) -> web.Response:
    return page(request, "selftest.html")


async def run_selftest(request: web.Request) -> web.Response:
    source = {"kind": "web", "ref": secrets.token_hex(8)}
    try:
        result = await api(request, "POST", "/v1/selftest", json={"requester": _user(request), "source": source})
    except ApiError as e:
        return web.json_response({"error": str(e)}, status=_status(e))
    return web.json_response({"location": f"/sessions/{result['id']}"})


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="The request must be JSON.")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="The request must be a JSON object.")
    return body


async def _act(request: web.Request, method: str, path: str, location: str | None = None, **kwargs) -> web.Response:
    """An action on the API, answered as JSON the page's script reads: where to go next, or what went wrong."""
    try:
        result = await api(request, method, path, **kwargs)
    except ApiError as e:
        return web.json_response({"error": str(e)}, status=_status(e))
    return web.json_response({"location": location, "result": result})


async def send_message(request: web.Request) -> web.Response:
    """Type a message into the session, which also answers a help request it's waiting on."""
    task_id = request.match_info["id"]
    text = (await _body(request)).get("text")
    if not isinstance(text, str) or not text.strip():
        return web.json_response({"error": "Write a message first."}, status=400)
    return await _act(request, "POST", f"/v1/tasks/{task_id}/message", json={"text": text, "requester": _user(request)})


async def check_session(request: web.Request) -> web.Response:
    """Have the API look at the session now, for output or a request it hasn't seen yet."""
    task_id = request.match_info["id"]
    return await _act(request, "POST", f"/v1/tasks/{task_id}/check")


async def stop_session(request: web.Request) -> web.Response:
    task_id = request.match_info["id"]
    # The API answers once the session has exited and its end is recorded, which can take a minute.
    timeout = aiohttp.ClientTimeout(total=request.app[CONFIG]["launch_timeout"])
    return await _act(request, "POST", f"/v1/tasks/{task_id}/stop", json={"requester": _user(request)}, timeout=timeout)


async def decide_domains(request: web.Request) -> web.Response:
    task_id, number, verb = request.match_info["id"], request.match_info["n"], request.match_info["verb"]
    return await _act(request, "POST", f"/v1/tasks/{task_id}/domains/{number}/{verb}", json={"requester": _user(request)})


async def clear_session(request: web.Request) -> web.Response:
    """Have the API delete an ended session: its output, and the ~/shared the dejima host kept for it."""
    task_id = request.match_info["id"]
    return await _act(request, "POST", f"/v1/tasks/{task_id}/clear", location="/", json={"requester": _user(request)})


class _TooLarge(Exception):
    pass


async def upload_file(request: web.Request) -> web.Response:
    """Stream a file into the session's ~/shared/uploads, capped at max_upload."""
    config = request.app[CONFIG]
    task_id, name = request.match_info["id"], request.query.get("name", "")
    if not name or len(name) > 255:
        return web.json_response({"error": "The file needs a name of up to 255 characters."}, status=400)
    limit = config["max_upload"]
    if request.content_length is not None and request.content_length > limit:
        return web.json_response({"error": f"That file is over this darner-web's max_upload of {limit} bytes."}, status=413)

    async def body():
        sent = 0
        async for chunk in request.content.iter_chunked(64 * 1024):
            sent += len(chunk)
            if sent > limit:
                raise _TooLarge()
            yield chunk

    url = f"{config['api_url'].rstrip('/')}/v1/tasks/{task_id}/files"
    try:
        async with request.app[API].post(
            url, params={"name": name, "requester": _user(request)}, data=body(),
            headers={"Authorization": f"Bearer {config['api_token']}", "Content-Type": "application/octet-stream"},
            # The API answers only once it has passed the file to the dejima host, over ssh.
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=config["launch_timeout"]),
        ) as resp:
            data = await _json(resp)
            if resp.status >= 400:
                error = data.get("error") if isinstance(data, dict) else None
                return web.json_response({"error": error or f"darner-api answered {resp.status}"}, status=min(resp.status, 502))
    except _TooLarge:
        return web.json_response({"error": f"That file is over this darner-web's max_upload of {limit} bytes."}, status=413)
    except TimeoutError:  # first, as in api()
        return web.json_response({
            "error": "darner-api didn't answer in time. The file may still have reached the session: check before sending it again.",
        }, status=504)
    except aiohttp.ClientError as e:
        if isinstance(e.__cause__, _TooLarge) or isinstance(e.__context__, _TooLarge):
            return web.json_response({"error": f"That file is over this darner-web's max_upload of {limit} bytes."}, status=413)
        return web.json_response({"error": f"couldn't reach darner-api: {type(e).__name__}"}, status=502)
    return web.json_response({"location": None, "result": data})


def _words(value: object) -> list[str]:
    """A list from a form field, as chat keywords take them: separated by commas or spaces."""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v]
    if not isinstance(value, str):
        return []
    return [v for v in value.replace(",", " ").split() if v]


EFFORTS = ("none", "low", "medium", "high", "xhigh")


async def new_task_page(request: web.Request) -> web.Response:
    catalog = await api(request, "GET", "/v1/catalog")
    blurbs = await api(request, "GET", "/v1/blurbs")
    configs = await api(request, "GET", "/v1/configs")
    # Every level a harness's manifest lists; one that lists none takes any of dejima's.
    efforts = []
    for options in catalog.get("options", {}).values():
        for level in EFFORTS if options.get("efforts") is None else options["efforts"]:
            if level not in efforts:
                efforts.append(level)
    # A fresh source per page, so sending the form twice finds the task the first send started.
    return page(
        request, "new.html", catalog=catalog, blurbs=blurbs, configs=configs, efforts=efforts, ref=secrets.token_hex(16)
    )


def _task_request(request: web.Request, body: dict, ref: str) -> dict:
    """The form as the API's task request, the way chat would send it."""
    request_body = {
        "task": body.get("task") if isinstance(body.get("task"), str) else "",
        "blurbs": _words(body.get("blurbs")),
        "configs": _words(body.get("configs")),
        "grants": {
            "domains": _words(body.get("domains")),
            "internal": _words(body.get("internal")),
            "dependencies": _words(body.get("dependencies")),
        },
        "requester": _user(request),
        "source": {"kind": "web", "ref": ref},
    }
    for key in ("harness", "backend", "model", "effort", "context_window", "network", "name"):
        if isinstance(body.get(key), str) and body[key].strip():
            request_body[key] = body[key].strip()
    return request_body


async def preview_task(request: web.Request) -> web.Response:
    body = await _body(request)
    try:
        result = await api(request, "POST", "/v1/tasks/preview", json=_task_request(request, body, "0" * 32))
    except ApiError as e:
        return web.json_response({"error": str(e)}, status=_status(e))
    return web.json_response(result)


async def start_task(request: web.Request) -> web.Response:
    body = await _body(request)
    task, ref = body.get("task"), body.get("ref")
    if not isinstance(task, str) or not task.strip():
        return web.json_response({"error": "Say what the task is."}, status=400)
    if not isinstance(ref, str) or not re.fullmatch(r"[0-9a-f]{32}", ref):
        return web.json_response({"error": "Reload the page and try again."}, status=400)
    request_body = _task_request(request, body, ref)
    timeout = aiohttp.ClientTimeout(total=request.app[CONFIG]["launch_timeout"])
    try:
        status, result = await api(request, "POST", "/v1/tasks", with_status=True, json=request_body, timeout=timeout)
    except ApiError as e:
        if not e.answered:
            # Unknown whether the launch went ahead, and resending this form would only find it.
            return web.json_response({
                "error": f"{e}. The task may still be starting: check Sessions before starting it again.",
            }, status=e.status)
        return web.json_response({"error": str(e)}, status=e.status)
    if status == 200:
        # This page's ref already started a task, so the form as sent now didn't start anything.
        return web.json_response({
            "error": f"This form already started {result['id']}. Reload the page to start another task.",
        }, status=409)
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
        return page(request, "error.html", status=_status(e), message=str(e))


def _age(started) -> str:
    minutes = max(0, int(time.time() - started) // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours < 24 else f"{hours // 24}d{hours % 24}h"


def make_app(config: dict) -> web.Application:
    config = check_config(config)
    app = web.Application(middlewares=[guard, api_errors], client_max_size=MAX_REQUEST)
    app[CONFIG] = config
    templates = jinja2.Environment(
        loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True, undefined=jinja2.StrictUndefined,
    )
    templates.filters["agent"] = render
    templates.filters["age"] = _age
    templates.globals["SETTING_LABELS"] = SETTING_LABELS
    app[TEMPLATES] = templates

    async def client(app: web.Application):
        app[API] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=API_TIMEOUT))
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
    app.router.add_get("/new", new_task_page)
    app.router.add_post("/tasks", start_task)
    app.router.add_post("/tasks/preview", preview_task)
    app.router.add_post(f"/sessions/{TASK_ID}/message", send_message)
    app.router.add_post(f"/sessions/{TASK_ID}/stop", stop_session)
    app.router.add_post(f"/sessions/{TASK_ID}/check", check_session)
    app.router.add_post(f"/sessions/{TASK_ID}/clear", clear_session)
    app.router.add_post(f"/sessions/{TASK_ID}/files", upload_file)
    app.router.add_post(f"/sessions/{TASK_ID}/domains/{{n:\\d{{1,9}}}}/{{verb:allow|deny}}", decide_domains)
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
