import argparse
import asyncio
import hmac
import json
import tomllib
from pathlib import Path

from aiohttp import web

from meeseeks.job import JobError, compose_prompt, new_task_id, valid_name, validate_job
from meeseeks.store import Store

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


def _source(body: dict) -> tuple[str, str]:
    source = body.get("source")
    if not isinstance(source, dict) or set(source) != {"kind", "ref"}:
        raise RequestError(400, "source must be an object with kind and ref")
    kind, ref = source["kind"], source["ref"]
    if not isinstance(kind, str) or not valid_name(kind) or not isinstance(ref, str) or not 0 < len(ref) <= 255:
        raise RequestError(400, "invalid source")
    return kind, ref


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


def build_job(config: dict, body: object) -> tuple[dict, tuple[str, str], str]:
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
        "prompt": compose_prompt(_text(body, "task"), blurbs, _text(body, "context", required=False)),
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


async def run_launcher(command: list[str], job: dict, timeout: float) -> dict:
    proc = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(job).encode()), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"launcher timed out after {timeout}s")
    if proc.returncode:
        raise RuntimeError(err.decode().strip() or f"launcher exited {proc.returncode}")
    return json.loads(out)


def authorized(config: dict, header: str) -> str | None:
    scheme, _, token = header.partition(" ")
    if scheme != "Bearer" or not token:
        return None
    for name, expected in config.get("tokens", {}).items():
        if hmac.compare_digest(token.encode(errors="surrogateescape"), expected.encode()):
            return name
    return None


async def create_task(request: web.Request) -> web.Response:
    config = request.app[CONFIG]
    if authorized(config, request.headers.get("Authorization", "")) is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "body must be JSON"}, status=400)
    try:
        job, (kind, ref), requester = build_job(config, body)
    except RequestError as e:
        return web.json_response({"error": str(e)}, status=e.status)

    store = request.app[STORE]
    task, new = store.claim(job["id"], kind, ref, requester, json.dumps(job))
    if not new:
        if task.status == "pending":
            return web.json_response({"error": f"{task.id} is already being launched"}, status=409)
        return web.json_response({"id": task.id, "target": task.target})

    try:
        result = await run_launcher(config["launch_command"], job, config.get("launch_timeout", 120))
        target = result["target"]
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as e:
        store.mark(job["id"], "failed", error=str(e))
        return web.json_response({"error": f"launch failed: {e}"}, status=502)
    store.mark(job["id"], "launched", target=target)
    return web.json_response({"id": job["id"], "target": target}, status=201)


def make_app(config: dict) -> web.Application:
    app = web.Application(client_max_size=256 * 1024)
    app[CONFIG] = config
    app[STORE] = Store(config["db_path"])
    app[STORE].fail_pending()
    app.router.add_post("/v1/tasks", create_task)
    return app


def main() -> None:
    p = argparse.ArgumentParser(prog="meeseeks-api")
    p.add_argument("--config", default="/etc/meeseeks/api.toml", type=Path)
    args = p.parse_args()
    config = tomllib.loads(args.config.read_text())
    host, _, port = config.get("listen", "127.0.0.1:8080").rpartition(":")
    web.run_app(make_app(config), host=host, port=int(port))
