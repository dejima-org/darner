import argparse
import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path

from aiohttp import web

from meeseeks import archive
from meeseeks.job import OUTPUT_INSTRUCTIONS, JobError, compose_prompt, new_task_id, valid_domain, valid_name, validate_job
from meeseeks.launch import FETCH_REJECTED
from meeseeks.store import Store

log = logging.getLogger("meeseeks.api")

RUN_ID = re.compile(r"[0-9a-f]{16}")


def session_link(config: dict, run_id: str | None) -> str | None:
    """A link to the session in dejima's web remote access, if one is configured."""
    base = config.get("remote_access_url")
    return f"{base.rstrip('/')}/?arg={run_id}" if base and run_id else None

REQUEST_KEYS = {"task", "context", "blurbs", "grants", "harness", "backend", "model", "effort", "requester", "source"}
MAX_TEXT = 32 * 1024

CONFIG = web.AppKey("config", dict)
STORE = web.AppKey("store", Store)
SHARE_ERRORS = web.AppKey("share_errors", dict)


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _text(body: dict, key: str, required: bool = True) -> str:
    value = body.get(key, "" if not required else None)
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > MAX_TEXT:
        raise RequestError(400, f"{key} must be {'non-empty ' if required else ''}text of at most {MAX_TEXT} characters")
    return value


def _list(d: dict, key: str) -> list:
    value = d.get(key, [])
    if not isinstance(value, list):
        raise RequestError(400, f"{key} must be a list")
    return value


def _source(body: dict) -> dict:
    source = body.get("source")
    if not isinstance(source, dict) or not {"kind", "ref"} <= set(source) <= {"kind", "ref", "channel"}:
        raise RequestError(400, "source must be an object with kind, ref and optionally channel")
    kind = source["kind"]
    if not isinstance(kind, str) or not valid_name(kind):
        raise RequestError(400, "invalid source kind")
    for key in ("ref", "channel"):
        value = source.get(key, "x")
        if not isinstance(value, str) or not 0 < len(value) <= 255:
            raise RequestError(400, f"invalid source {key}")
    return source


def available_blurbs(blurbs_dir: Path) -> list[str]:
    return sorted(p.stem for p in blurbs_dir.glob("*.md")) if blurbs_dir.is_dir() else []


def load_blurbs(blurbs_dir: Path, names: list) -> list[tuple[str, str]]:
    available = available_blurbs(blurbs_dir)
    blurbs = []
    for name in names:
        if not isinstance(name, str) or name not in available:
            listed = ", ".join(available) or "none"
            raise RequestError(400, f"unknown context blurb {name!r} (available: {listed})")
        blurbs.append((name, (blurbs_dir / f"{name}.md").read_text()))
    return blurbs


def denied_domain(policy: dict, domain: str) -> bool:
    # A grant covers its subdomains, so a parent of a denied domain is denied too.
    return any(
        domain == denied or domain.endswith("." + denied) or denied.endswith("." + domain)
        for denied in (d.lower() for d in policy.get("deny_domains", []))
    )


def check_policy(policy: dict, domains: list[str]) -> None:
    max_domains = policy.get("max_domains")
    if max_domains is not None and len(domains) > max_domains:
        raise RequestError(403, f"at most {max_domains} domains may be granted")
    for domain in domains:
        if denied_domain(policy, domain):
            raise RequestError(403, f"domain not permitted: {domain}")


def _without_port(domain: object) -> object:
    """Lowercase a granted domain and drop any :port, since a launch grant covers every port."""
    if not isinstance(domain, str):
        return domain
    host, sep, port = domain.lower().rpartition(":")
    return host if sep and port.isascii() and port.isdigit() and 0 < int(port) < 65536 else domain.lower()


def build_job(config: dict, body: object) -> tuple[dict, dict, str]:
    if not isinstance(body, dict):
        raise RequestError(400, "body must be a JSON object")
    unknown = set(body) - REQUEST_KEYS
    if unknown:
        raise RequestError(400, f"unknown keys: {', '.join(sorted(unknown))}")
    source = _source(body)
    requester = _text(body, "requester")
    grants = body.get("grants", {})
    if not isinstance(grants, dict) or set(grants) - {"domains", "internal", "dependencies"}:
        raise RequestError(400, "grants must be an object with domains, internal and dependencies")
    internal = [_without_port(d) for d in _list(grants, "internal")]
    domains = [_without_port(d) for d in _list(grants, "domains")]
    domains += [d for d in internal if d not in domains]
    defaults = config.get("default_blurbs", [])
    names = defaults + [name for name in _list(body, "blurbs") if name not in defaults]
    blurbs = load_blurbs(Path(config["blurbs_dir"]), names)
    job = {
        "id": new_task_id(),
        "prompt": compose_prompt(
            _text(body, "task"),
            blurbs,
            _text(body, "context", required=False),
            config.get("output_instructions", OUTPUT_INSTRUCTIONS),
        ),
        "domains": domains,
        "internal_domains": internal,
        "dependencies": _list(grants, "dependencies"),
    }
    for key in ("harness", "backend", "model", "effort"):
        if body.get(key) is not None:
            job[key] = body[key]
    if config.get("remote_access_url"):
        job["session_wait"] = config.get("session_wait", 20)
    try:
        validate_job(job)
    except JobError as e:
        raise RequestError(400, str(e))
    check_policy(config.get("policy", {}), job["domains"])
    return job, source, requester


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> bytes:
    data = b""
    while chunk := await stream.read(64 * 1024):
        data += chunk
        if len(data) > cap:
            raise RuntimeError(f"launcher sent more than {cap} bytes")
    return data


async def run_remote(config: dict, args: list[str], stdin: bytes = b"") -> tuple[int, bytes, str]:
    """Run the launcher with a command; over ssh, args become SSH_ORIGINAL_COMMAND."""
    timeout = config.get("launch_timeout", 120)
    cap = _limits(config).max_bytes + archive.MANIFEST_SLACK
    proc = await asyncio.create_subprocess_exec(
        *config["launch_command"], *args,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    async def feed():
        proc.stdin.write(stdin)
        try:
            await proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        proc.stdin.close()

    async def communicate():
        _, out, err = await asyncio.gather(feed(), _read_capped(proc.stdout, cap), _read_capped(proc.stderr, 64 * 1024))
        await proc.wait()
        return out, err

    try:
        out, err = await asyncio.wait_for(communicate(), timeout)
    except (asyncio.TimeoutError, RuntimeError) as e:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"launcher timed out after {timeout}s" if isinstance(e, asyncio.TimeoutError) else str(e))
    return proc.returncode, out, archive.clean_text(err, 2048).removeprefix("meeseeks-launch: ")


async def run_launcher(config: dict, job: dict) -> dict:
    code, out, err = await run_remote(config, [], json.dumps(job).encode())
    if code:
        raise RuntimeError(err or f"launcher exited {code}")
    return json.loads(out)


def _limits(config: dict) -> archive.Limits:
    return archive.Limits(**config.get("limits", {}))


async def collect(app: web.Application, task_id: str, signalled: bool = True) -> bool:
    """Fetch a task's output into an event. False means try again later."""
    config, store = app[CONFIG], app[STORE]
    code, out, err = await run_remote(config, ["fetch", task_id])
    if code == FETCH_REJECTED:
        store.add_event(task_id, "output", f"output rejected: {err}", signalled=signalled)
        return True
    if code:
        log.warning("fetch %s failed: %s", task_id, err or f"exit {code}")
        return False
    try:
        packed, summary = archive.repack(out, _limits(config))
    except archive.ArchiveError as e:
        store.add_event(task_id, "output", f"output rejected: {e}", signalled=signalled)
        return True
    task_dir = Path(config["results_dir"]) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=task_dir, suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(packed)
    seq = store.add_event(task_id, "output", summary, len(packed), signalled=signalled)
    try:
        os.replace(tmp, task_dir / f"{seq}.zip")
    except OSError:
        store.remove_event(seq)
        raise
    return True


async def collect_help(app: web.Application, task: dict) -> None:
    config, store = app[CONFIG], app[STORE]
    code, out, err = await run_remote(config, ["help", task["id"]])
    if code:
        log.warning("help %s failed: %s", task["id"], err or f"exit {code}")
        return
    store.add_event(task["id"], "help", archive.clean_text(out), answerable=task.get("answerable", False))


async def collect_domains(app: web.Application, task: dict) -> None:
    config, store = app[CONFIG], app[STORE]
    code, out, err = await run_remote(config, ["domains", task["id"]])
    if code:
        log.warning("domains %s failed: %s", task["id"], err or f"exit {code}")
        return
    request = json.loads(out)
    policy = config.get("policy", {})
    domains = [
        {
            "domain": d["domain"],
            "internal": bool(d["internal"]),
            "port": d["port"] if type(d.get("port")) is int and 0 < d["port"] < 65536 else None,
            "denied": denied_domain(policy, d["domain"]),
        }
        for d in request["domains"]
        if valid_domain(d.get("domain"))
    ]
    data = {"request": int(request["request"]), "domains": domains, "rejected": int(request.get("rejected", 0))}
    store.add_event(task["id"], "domains", data=data)


async def poll_once(app: web.Application) -> None:
    config, store = app[CONFIG], app[STORE]
    ours = set(store.launched())
    if not ours:
        return
    code, out, err = await run_remote(config, ["status"])
    if code:
        log.warning("status failed: %s", err or f"exit {code}")
        return
    for task in json.loads(out):
        if task["id"] not in ours:
            continue
        # Logged once per change: it's repeated every poll until fixed.
        if app[SHARE_ERRORS].get(task["id"]) != task.get("error"):
            app[SHARE_ERRORS][task["id"]] = task.get("error")
            if task.get("error"):
                log.warning("can't read %s's ~/shared, so its output can't be collected: %s", task["id"], task["error"])
        if isinstance(task.get("run_id"), str) and RUN_ID.fullmatch(task["run_id"]):
            store.set_run_id(task["id"], task["run_id"])
        # One task's failure must not hold up the others.
        try:
            if task.get("help"):
                await collect_help(app, task)
            if task.get("domains"):
                await collect_domains(app, task)
            # An unsignalled RESULT.md is the agent's output all the same, collected
            # once the launcher judges it finished.
            if task["ready"] or task.get("unsignalled"):
                collected = await collect(app, task["id"], signalled=task["ready"])
            else:
                collected = True
        except Exception:
            log.exception("collecting %s failed", task["id"])
            continue
        if task["state"] == "exited" and collected:
            store.add_event(task["id"], "ended")
            store.mark(task["id"], "ended")


async def _poll_forever(app: web.Application) -> None:
    while True:
        await asyncio.sleep(app[CONFIG].get("poll_interval", 30))
        try:
            await poll_once(app)
        except Exception:
            log.exception("poll failed")


async def poller(app: web.Application):
    task = asyncio.create_task(_poll_forever(app))
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def authorized(config: dict, header: str) -> str | None:
    scheme, _, token = header.partition(" ")
    if scheme != "Bearer" or not token:
        return None
    for name, expected in config.get("tokens", {}).items():
        if hmac.compare_digest(token.encode(errors="surrogateescape"), expected.encode()):
            return name
    return None


def _client(request: web.Request) -> str:
    client = authorized(request.app[CONFIG], request.headers.get("Authorization", ""))
    if client is None:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "unauthorized"}), content_type="application/json")
    return client


async def create_task(request: web.Request) -> web.Response:
    config = request.app[CONFIG]
    client = _client(request)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "body must be JSON"}, status=400)
    try:
        job, source, requester = build_job(config, body)
    except RequestError as e:
        return web.json_response({"error": str(e)}, status=e.status)

    store = request.app[STORE]
    task, new = store.claim(job["id"], client, source, requester, json.dumps(job))
    if not new:
        if task.status == "pending":
            return web.json_response({"error": f"{task.id} is already being launched"}, status=409)
        return web.json_response({"id": task.id, "target": task.target, "link": session_link(config, task.run_id)})

    try:
        result = await run_launcher(config, job)
        target = result["target"]
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as e:
        store.mark(job["id"], "failed", error=str(e))
        return web.json_response({"error": f"launch failed: {e}"}, status=502)
    store.mark(job["id"], "launched", target=target)
    run_id = result.get("run_id")
    if isinstance(run_id, str) and RUN_ID.fullmatch(run_id):
        store.set_run_id(job["id"], run_id)
    else:
        run_id = None
    return web.json_response({"id": job["id"], "target": target, "link": session_link(config, run_id)}, status=201)


async def list_events(request: web.Request) -> web.Response:
    config = request.app[CONFIG]
    events = request.app[STORE].events(_client(request))
    return web.json_response(
        [
            {
                "seq": e.seq, "id": e.id, "type": e.type, "summary": e.summary, "size": e.size,
                "answerable": e.answerable, "signalled": e.signalled, "data": e.data,
                "target": e.target, "link": session_link(config, e.run_id),
                "source": e.source,
            }
            for e in events
        ]
    )


async def get_output(request: web.Request) -> web.StreamResponse:
    task_id, seq = request.match_info["id"], int(request.match_info["seq"])
    event = request.app[STORE].event(_client(request), seq)
    path = Path(request.app[CONFIG]["results_dir"]) / task_id / f"{seq}.zip"
    if event is None or event.id != task_id or event.type != "output" or not event.size or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(
        path,
        headers={
            "Content-Type": "application/zip",
            "Content-Disposition": f'attachment; filename="{task_id}-{seq}.zip"',
            "X-Content-Type-Options": "nosniff",
        },
    )


async def answer_help(request: web.Request) -> web.Response:
    return await _type_into(request, "answer")


async def message_task(request: web.Request) -> web.Response:
    return await _type_into(request, "message")


async def _type_into(request: web.Request, command: str) -> web.Response:
    """Answer a help request, or send a message, by typing it into the session."""
    client = _client(request)
    task_id = request.match_info["id"]
    if request.app[STORE].task_status(client, task_id) is None:
        raise web.HTTPNotFound()
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "body must be JSON"}, status=400)
    if not isinstance(body, dict) or set(body) != {"text", "requester"}:
        return web.json_response({"error": "body must have text and requester"}, status=400)
    try:
        text, requester = _text(body, "text"), _text(body, "requester")
    except RequestError as e:
        return web.json_response({"error": str(e)}, status=e.status)
    payload = json.dumps({"text": text, "requester": requester}).encode()
    try:
        code, _, err = await run_remote(request.app[CONFIG], [command, task_id], payload)
    except RuntimeError as e:
        return web.json_response({"error": f"{command} failed: {e}"}, status=502)
    if code:
        return web.json_response({"error": err or f"launcher exited {code}"}, status=409)
    return web.Response(status=204)


async def list_blurbs(request: web.Request) -> web.Response:
    _client(request)
    return web.json_response(available_blurbs(Path(request.app[CONFIG]["blurbs_dir"])))


async def _sessions(request: web.Request, task_id: str | None = None) -> list[dict]:
    """The client's open sessions, with what each is waiting on, asked of the dejima host now."""
    client, store, config = _client(request), request.app[STORE], request.app[CONFIG]
    tasks = store.open_tasks(client, task_id)
    live = {}
    if tasks:
        try:
            code, out, err = await run_remote(config, ["status"])
            live = {t["id"]: t for t in json.loads(out)} if code == 0 else {}
        except (RuntimeError, ValueError):
            pass
    sessions = []
    for task in tasks:
        state = live.get(task["id"], {})
        waiting = {"answer": None, "domains": None}
        if state.get("awaiting_answer"):
            waiting["answer"] = store.latest_summary(client, task["id"], "help") or ""
        if isinstance(state.get("domain_request"), int):
            waiting["domains"] = store.domain_request(client, task["id"], state["domain_request"])
        sessions.append({
            "id": task["id"],
            "target": task["target"],
            "link": session_link(config, task["run_id"]),
            "requester": task["requester"],
            "started": task["created"],
            "state": state.get("state", "unknown"),
            "answerable": bool(state.get("answerable")),
            "waiting": waiting,
        })
    return sessions


async def list_sessions(request: web.Request) -> web.Response:
    return web.json_response(await _sessions(request))


async def get_session(request: web.Request) -> web.Response:
    found = await _sessions(request, request.match_info["id"])
    if not found:
        raise web.HTTPNotFound()
    return web.json_response(found[0])


async def upload_file(request: web.Request) -> web.Response:
    """Pass a requester's file to the session's ~/shared/uploads."""
    client, store, config = _client(request), request.app[STORE], request.app[CONFIG]
    task_id = request.match_info["id"]
    status = store.task_status(client, task_id)
    if status is None:
        raise web.HTTPNotFound()
    if status != "launched":
        return web.json_response({"error": f"{task_id} is not running"}, status=409)
    name, requester = request.query.get("name", ""), request.query.get("requester", "")
    if not requester or len(name) > 255 or len(requester) > 255:
        return web.json_response({"error": "name and requester are required"}, status=400)
    limit = config.get("max_upload", 50 * 1024 * 1024)
    data = bytearray()
    while chunk := await request.content.read(64 * 1024):
        data += chunk
        if len(data) > limit:
            return web.json_response({"error": f"the file is over the {limit}-byte upload limit"}, status=413)
    header = json.dumps({"name": name, "requester": requester}).encode() + b"\n"
    try:
        code, out, err = await run_remote(config, ["upload", task_id], header + bytes(data))
    except RuntimeError as e:
        return web.json_response({"error": f"upload failed: {e}"}, status=502)
    if code:
        return web.json_response({"error": err or f"launcher exited {code}"}, status=409)
    return web.json_response(json.loads(out))


async def stop_task(request: web.Request) -> web.Response:
    client = _client(request)
    task_id = request.match_info["id"]
    status = request.app[STORE].task_status(client, task_id)
    if status is None:
        raise web.HTTPNotFound()
    if status != "launched":
        return web.json_response({"error": f"{task_id} is not running"}, status=409)
    try:
        code, _, err = await run_remote(request.app[CONFIG], ["stop", task_id])
    except RuntimeError as e:
        return web.json_response({"error": f"stop failed: {e}"}, status=502)
    if code:
        return web.json_response({"error": err or f"launcher exited {code}"}, status=409)
    return web.Response(status=204)


async def decide_domains(request: web.Request) -> web.Response:
    """Allow or deny a task's domain request, as it was shown in its event."""
    client = _client(request)
    store, config = request.app[STORE], request.app[CONFIG]
    task_id, number, verb = request.match_info["id"], int(request.match_info["n"]), request.match_info["verb"]
    if store.task_status(client, task_id) is None:
        raise web.HTTPNotFound()
    shown = store.domain_request(client, task_id, number)
    if shown is None:
        raise web.HTTPNotFound()
    try:
        body = await request.json()
        requester = _text(body, "requester")
    except (ValueError, RequestError, AttributeError):
        return web.json_response({"error": "body must have requester"}, status=400)
    payload = {"requester": requester}
    if verb == "allow":
        # Checked again here, in case the policy changed since the request was shown.
        policy = config.get("policy", {})
        payload["domains"] = [d["domain"] for d in shown["domains"] if not denied_domain(policy, d["domain"])]
        payload["max_domains"] = policy.get("max_domains")
    try:
        code, out, err = await run_remote(config, [verb, task_id, str(number)], json.dumps(payload).encode())
    except RuntimeError as e:
        return web.json_response({"error": f"{verb} failed: {e}"}, status=502)
    if code:
        return web.json_response({"error": err or f"launcher exited {code}"}, status=409)
    return web.json_response(json.loads(out))


async def get_catalog(request: web.Request) -> web.Response:
    """The harnesses and backends the dejima host offers, for clients to show."""
    _client(request)
    try:
        code, out, err = await run_remote(request.app[CONFIG], ["catalog"])
    except RuntimeError as e:
        return web.json_response({"error": f"catalog failed: {e}"}, status=502)
    if code:
        return web.json_response({"error": err or f"launcher exited {code}"}, status=502)
    return web.json_response(json.loads(out))


async def ack_event(request: web.Request) -> web.Response:
    if not request.app[STORE].ack(_client(request), int(request.match_info["seq"])):
        raise web.HTTPNotFound()
    return web.Response(status=204)


def make_app(config: dict, poll: bool = True) -> web.Application:
    if config.get("remote_access_url") and not re.match(r"https?://", config["remote_access_url"]):
        raise ValueError("remote_access_url must start with http:// or https://")
    app = web.Application(client_max_size=256 * 1024)
    app[CONFIG] = config
    app[STORE] = Store(config["db_path"])
    available = available_blurbs(Path(config["blurbs_dir"]))
    for name in config.get("default_blurbs", []):
        if name not in available:
            raise ValueError(f"default blurb {name!r} has no {name}.md in {config['blurbs_dir']}")
    app[STORE].fail_pending()
    app[SHARE_ERRORS] = {}
    app.router.add_post("/v1/tasks", create_task)
    app.router.add_get("/v1/events", list_events)
    app.router.add_get("/v1/blurbs", list_blurbs)
    app.router.add_get("/v1/catalog", get_catalog)
    app.router.add_get(r"/v1/tasks/{id:ms-[a-z2-7]{5}}/outputs/{seq:\d+}.zip", get_output)
    app.router.add_post(r"/v1/events/{seq:\d+}/ack", ack_event)
    app.router.add_post(r"/v1/tasks/{id:ms-[a-z2-7]{5}}/answer", answer_help)
    app.router.add_post(r"/v1/tasks/{id:ms-[a-z2-7]{5}}/stop", stop_task)
    app.router.add_post(r"/v1/tasks/{id:ms-[a-z2-7]{5}}/message", message_task)
    app.router.add_post(r"/v1/tasks/{id:ms-[a-z2-7]{5}}/files", upload_file)
    app.router.add_get("/v1/tasks", list_sessions)
    app.router.add_get(r"/v1/tasks/{id:ms-[a-z2-7]{5}}", get_session)
    app.router.add_post(r"/v1/tasks/{id:ms-[a-z2-7]{5}}/domains/{n:\d{1,9}}/{verb:allow|deny}", decide_domains)
    if poll:
        app.cleanup_ctx.append(poller)
    return app


def main() -> None:
    p = argparse.ArgumentParser(prog="meeseeks-api")
    p.add_argument("--config", default="/etc/meeseeks/api.toml", type=Path)
    args = p.parse_args()
    # A config mistake is reported in one line rather than a traceback, which
    # a restarting container would otherwise repeat without end.
    try:
        config = tomllib.loads(args.config.read_text())
        host, _, port = config.get("listen", "127.0.0.1:8080").rpartition(":")
        app = make_app(config)
        port = int(port)
    except tomllib.TOMLDecodeError as e:
        hint = "; text values need double quotes" if "Invalid value" in str(e) else ""
        sys.exit(f"meeseeks-api: {args.config}: {e}{hint}")
    except KeyError as e:
        sys.exit(f"meeseeks-api: {args.config}: missing setting {e}")
    except (OSError, ValueError) as e:
        sys.exit(f"meeseeks-api: {args.config}: {e}")
    logging.basicConfig(level=logging.INFO)
    web.run_app(app, host=host, port=port)
