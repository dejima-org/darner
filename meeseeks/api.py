import argparse
import asyncio
import contextlib
import hmac
import json
import logging
import os
import tempfile
import tomllib
from pathlib import Path

from aiohttp import web

from meeseeks import archive
from meeseeks.job import OUTPUT_INSTRUCTIONS, JobError, compose_prompt, new_task_id, valid_name, validate_job
from meeseeks.launch import FETCH_REJECTED
from meeseeks.store import Store

log = logging.getLogger("meeseeks.api")

REQUEST_KEYS = {"task", "context", "blurbs", "grants", "harness", "backend", "requester", "source"}
MAX_TEXT = 32 * 1024

CONFIG = web.AppKey("config", dict)
STORE = web.AppKey("store", Store)


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


def load_blurbs(blurbs_dir: Path, names: list) -> list[tuple[str, str]]:
    available = sorted(p.stem for p in blurbs_dir.glob("*.md")) if blurbs_dir.is_dir() else []
    blurbs = []
    for name in names:
        if not isinstance(name, str) or name not in available:
            listed = ", ".join(available) or "none"
            raise RequestError(400, f"unknown context blurb {name!r} (available: {listed})")
        blurbs.append((name, (blurbs_dir / f"{name}.md").read_text()))
    return blurbs


def check_policy(policy: dict, domains: list[str]) -> None:
    max_domains = policy.get("max_domains")
    if max_domains is not None and len(domains) > max_domains:
        raise RequestError(403, f"at most {max_domains} domains may be granted")
    # A grant covers its subdomains, so a parent of a denied domain is denied too.
    for domain in domains:
        for denied in (d.lower() for d in policy.get("deny_domains", [])):
            if domain == denied or domain.endswith("." + denied) or denied.endswith("." + domain):
                raise RequestError(403, f"domain not permitted: {domain}")


def build_job(config: dict, body: object) -> tuple[dict, dict, str]:
    if not isinstance(body, dict):
        raise RequestError(400, "body must be a JSON object")
    unknown = set(body) - REQUEST_KEYS
    if unknown:
        raise RequestError(400, f"unknown keys: {', '.join(sorted(unknown))}")
    source = _source(body)
    requester = _text(body, "requester")
    grants = body.get("grants", {})
    if not isinstance(grants, dict) or set(grants) - {"domains", "dependencies"}:
        raise RequestError(400, "grants must be an object with domains and dependencies")
    blurbs = load_blurbs(Path(config["blurbs_dir"]), _list(body, "blurbs"))
    job = {
        "id": new_task_id(),
        "prompt": compose_prompt(
            _text(body, "task"),
            blurbs,
            _text(body, "context", required=False),
            config.get("output_instructions", OUTPUT_INSTRUCTIONS),
        ),
        "domains": [d.lower() if isinstance(d, str) else d for d in _list(grants, "domains")],
        "dependencies": _list(grants, "dependencies"),
    }
    for key in ("harness", "backend"):
        if body.get(key) is not None:
            job[key] = body[key]
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

    async def communicate():
        proc.stdin.write(stdin)
        proc.stdin.close()
        out, err = await asyncio.gather(_read_capped(proc.stdout, cap), _read_capped(proc.stderr, 64 * 1024))
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


async def collect(app: web.Application, task_id: str) -> bool:
    """Fetch a task's output into an event. False means try again later."""
    config, store = app[CONFIG], app[STORE]
    code, out, err = await run_remote(config, ["fetch", task_id])
    if code == FETCH_REJECTED:
        store.add_event(task_id, "output", f"output rejected: {err}")
        return True
    if code:
        log.warning("fetch %s failed: %s", task_id, err or f"exit {code}")
        return False
    try:
        packed, summary = archive.repack(out, _limits(config))
    except archive.ArchiveError as e:
        store.add_event(task_id, "output", f"output rejected: {e}")
        return True
    task_dir = Path(config["results_dir"]) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=task_dir, suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(packed)
    seq = store.add_event(task_id, "output", summary, len(packed))
    try:
        os.replace(tmp, task_dir / f"{seq}.zip")
    except OSError:
        store.remove_event(seq)
        raise
    return True


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
        # One task's failure must not hold up the others.
        try:
            collected = await collect(app, task["id"]) if task["ready"] else True
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
        return web.json_response({"id": task.id, "target": task.target})

    try:
        result = await run_launcher(config, job)
        target = result["target"]
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as e:
        store.mark(job["id"], "failed", error=str(e))
        return web.json_response({"error": f"launch failed: {e}"}, status=502)
    store.mark(job["id"], "launched", target=target)
    return web.json_response({"id": job["id"], "target": target}, status=201)


async def list_events(request: web.Request) -> web.Response:
    events = request.app[STORE].events(_client(request))
    return web.json_response(
        [{"seq": e.seq, "id": e.id, "type": e.type, "summary": e.summary, "size": e.size, "source": e.source} for e in events]
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


async def ack_event(request: web.Request) -> web.Response:
    if not request.app[STORE].ack(_client(request), int(request.match_info["seq"])):
        raise web.HTTPNotFound()
    return web.Response(status=204)


def make_app(config: dict, poll: bool = True) -> web.Application:
    app = web.Application(client_max_size=256 * 1024)
    app[CONFIG] = config
    app[STORE] = Store(config["db_path"])
    app[STORE].fail_pending()
    app.router.add_post("/v1/tasks", create_task)
    app.router.add_get("/v1/events", list_events)
    app.router.add_get(r"/v1/tasks/{id:ms-[a-z2-7]{5}}/outputs/{seq:\d+}.zip", get_output)
    app.router.add_post(r"/v1/events/{seq:\d+}/ack", ack_event)
    if poll:
        app.cleanup_ctx.append(poller)
    return app


def main() -> None:
    p = argparse.ArgumentParser(prog="meeseeks-api")
    p.add_argument("--config", default="/etc/meeseeks/api.toml", type=Path)
    args = p.parse_args()
    config = tomllib.loads(args.config.read_text())
    logging.basicConfig(level=logging.INFO)
    host, _, port = config.get("listen", "127.0.0.1:8080").rpartition(":")
    web.run_app(make_app(config), host=host, port=int(port))
