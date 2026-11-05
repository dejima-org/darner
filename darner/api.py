# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
import argparse
import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import unicodedata
import tomllib
from pathlib import Path

from aiohttp import web

from darner import archive, configsets, selftest
from darner.job import (
    OUTPUT_INSTRUCTIONS, JobError, check_harness, compose_prompt, new_task_id, valid_context_window, valid_domain, valid_model,
    valid_name, valid_task_id, validate_job,
)
from darner.launch import FETCH_REJECTED
from darner.store import NameTaken, Store

log = logging.getLogger("darner.api")
audit = logging.getLogger("darner.audit")


def _audit(action: str, client: str, task_id: str, requester: str, **details) -> None:
    """One line per action someone took on a session, so who did what can be answered later.

    Never what was typed or uploaded: the log would outlive the session's
    data, which is deleted when it ends. Values are repr'd, so a requester
    can't forge a line.
    """
    extra = "".join(f" {key}={value!r}" for key, value in details.items())
    audit.info("%s %s by %r via %s%s", action, task_id, requester, client, extra)

RUN_ID = re.compile(r"[0-9a-f]{16}")

EXAMPLE_TOKEN = "replace-with-a-long-random-token"
MIN_TOKEN = 32
REQUIRED = ("db_path", "blurbs_dir", "results_dir", "launch_command", "tokens")
# Every optional setting, with what it is when api.toml leaves it out, so a
# setting added by an update works on a config file written before it.
DEFAULTS = {
    "listen": "127.0.0.1:8080",
    "configs_dir": None,  # configs beside blurbs_dir
    "default_blurbs": [],
    "output_instructions": OUTPUT_INSTRUCTIONS,
    "poll_interval": 30,
    "launch_timeout": 120,
    "remote_access_url": None,
    "session_wait": 20,
    "max_upload": 50 * 1024 * 1024,
    "waiting_after": 300,
    "waiting_screen": True,
    "max_session_age": 86400,
    "output_retention": 604800,
    "policy": {},
    "limits": {},
    "catalog": {},
}


def with_defaults(config: dict) -> tuple[dict, list[str], list[str]]:
    """The config with every optional setting filled in, a line describing
    each setting for the log, and any keys this version doesn't know."""
    for key in REQUIRED:
        if key not in config:
            raise KeyError(key)
    full = {**DEFAULTS, **config}
    if full["configs_dir"] is None:
        full["configs_dir"] = str(Path(config["blurbs_dir"]).parent / "configs")
    lines = []
    for key in sorted(full):
        value = full[key]
        if key == "tokens":
            value = f"for {', '.join(sorted(value))}"
        elif key == "output_instructions":
            value = "custom" if value != OUTPUT_INSTRUCTIONS else "built in"
        lines.append(f"{key} = {value!r}" + ("" if key in config else " (default)"))
    unknown = sorted(set(config) - set(DEFAULTS) - set(REQUIRED))
    return full, lines, unknown


def session_link(config: dict, run_id: str | None) -> str | None:
    """A link to the session in dejima's web remote access, if one is configured."""
    base = config.get("remote_access_url")
    return f"{base.rstrip('/')}/?arg={run_id}" if base and run_id else None

REQUEST_KEYS = {
    "task", "context", "blurbs", "configs", "grants", "harness", "backend", "model", "effort", "context_window", "network",
    "requester", "source", "name",
}
# A session's name, which stands in for its id as dn-<name>. One shaped like an id can't be one.
SESSION_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
NAME_RULE = "a session name is up to 32 lowercase letters, digits and hyphens, not shaped like a session id"
MAX_TEXT = 32 * 1024

CONFIG = web.AppKey("config", dict)
STORE = web.AppKey("store", Store)
SHARE_ERRORS = web.AppKey("share_errors", dict)
# Polls an exited task's output went uncollected, by task.
UNCOLLECTED = web.AppKey("uncollected", dict)
FORGOTTEN = web.AppKey("forgotten", set)
# Polls an exited task past max_session_age gets to deliver its output before it's ended without it.
COLLECT_TRIES = 3
# Output zips kept for each task.
KEPT_ZIPS = 5
EXPIRE_FAILED = web.AppKey("expire_failed", set)
STATUS_CACHE = web.AppKey("status_cache", dict)
CATALOG_CACHE = web.AppKey("catalog_cache", dict)
POLL_LOCK = web.AppKey("poll_lock", asyncio.Lock)
LAST_CHECK = web.AppKey("last_check", dict)
# Seconds between one client's checks, each a poll of every session.
CHECK_INTERVAL = 15
# What dejima's harnesses take changes only when dejima is upgraded.
CATALOG_TTL = 300
# How old a launcher status, or a failure to get one, a session listing may
# reuse, so tabs refreshing don't each open an ssh connection to the dejima host.
STATUS_MAX_AGE = 5


async def launcher_status(app: web.Application, max_age: float) -> tuple[int, str, str]:
    """The launcher's status, or its failure, reused if younger than max_age."""
    return await _shared(app[STATUS_CACHE], max_age, max_age, lambda: run_remote(app[CONFIG], ["status"]))


async def _shared(cache: dict, max_age: float, error_age: float, fetch):
    """fetch()'s result reused if younger than max_age, or its failure if younger than error_age.

    Everyone asking while a fetch is in flight shares it, so a dejima host that
    hangs costs one ssh attempt at a time rather than a queue of them.
    """
    if "at" in cache and time.monotonic() - cache["at"] < (error_age if "error" in cache else max_age):
        if "error" in cache:
            # A fresh exception each time, so one cached failure doesn't grow a traceback per caller.
            raise RuntimeError(cache["error"])
        return cache["result"]
    if cache.get("inflight") is None:
        cache["inflight"] = asyncio.ensure_future(_record(cache, fetch))
    # Shielded: a caller whose client went away mustn't cancel it for the others.
    return await asyncio.shield(cache["inflight"])


async def _record(cache: dict, fetch):
    try:
        result = await fetch()
    except Exception as e:
        cache.update(at=time.monotonic(), error=str(e))
        cache.pop("result", None)
        raise
    finally:
        # Whatever ends it, cancellation included, the next caller starts afresh.
        cache["inflight"] = None
    cache.update(at=time.monotonic(), result=result)
    cache.pop("error", None)
    return result


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _requester(value: object) -> str:
    """Who acted, as a frontend names them: a label, never trusted further, so only its shape is checked."""
    if (
        not isinstance(value, str) or not value.strip() or len(value) > 255
        # Line and paragraph separators too, which would break a label across lines.
        or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in value)
    ):
        raise RequestError(400, "requester must be up to 255 characters, with no control or invisible characters")
    return value


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


DEPENDENCY_KINDS = ("apt", "cargo", "npm", "pip", "tarball")
# tarball: has the dejima host fetch any URL, on its own network, so it's off unless listed.
DEFAULT_DEPENDENCY_KINDS = ("apt", "cargo", "npm", "pip")
POLICY_KEYS = {
    "deny_domains": list, "allow_domains": list, "max_domains": int, "max_internal_domains": int,
    "allow_internal": bool, "allow_dependency_kinds": list,
}


def check_policy_config(policy: object) -> None:
    """Refuse a [policy] that wouldn't do what it says, such as a misspelled key."""
    if not isinstance(policy, dict):
        raise ValueError("policy must be a table")
    for key, value in policy.items():
        if key not in POLICY_KEYS:
            raise ValueError(f"unknown policy setting {key!r}; known: {', '.join(POLICY_KEYS)}")
        if type(value) is not POLICY_KEYS[key] or (isinstance(value, list) and not all(isinstance(v, str) for v in value)):
            raise ValueError(f"policy setting {key} has the wrong type")
    unknown = set(policy.get("allow_dependency_kinds", ())) - set(DEPENDENCY_KINDS)
    if unknown:
        raise ValueError(f"unknown dependency kinds in allow_dependency_kinds: {', '.join(sorted(unknown))}")


def check_catalog_config(catalog: object) -> dict:
    """[catalog.HARNESS.BACKEND] models, each an id or an id and its context window,
    as {harness: {backend: [{"id": ..., "context_window": ...}]}}."""
    if not isinstance(catalog, dict):
        raise ValueError("catalog must be a table")
    found = {}
    for harness, backends in catalog.items():
        if not valid_name(harness) or not isinstance(backends, dict):
            raise ValueError(f"catalog.{harness} must be a table of backends")
        for backend, table in backends.items():
            where = f"catalog.{harness}.{backend}"
            if not valid_name(backend) or not isinstance(table, dict) or set(table) - {"models"}:
                raise ValueError(f"{where} must be a table with models")
            models = []
            for entry in table.get("models", []):
                model = {"id": entry} if isinstance(entry, str) else entry
                if (
                    not isinstance(model, dict) or set(model) - {"id", "context_window"} or not valid_model(model.get("id"))
                    or ("context_window" in model and not valid_context_window(model["context_window"]))
                ):
                    raise ValueError(f"{where}: not a model, or an id and context_window: {entry!r}")
                models.append(model)
            found.setdefault(harness, {})[backend] = models
    return found


def _covers(parent: str, domain: str) -> bool:
    return domain == parent or domain.endswith("." + parent)


def denied_domain(policy: dict, domain: str, internal: bool = False) -> bool:
    # A grant covers its subdomains, so a parent of a denied domain is denied too.
    deny = [d.lower() for d in policy.get("deny_domains", [])]
    if any(_covers(denied, domain) or _covers(domain, denied) for denied in deny):
        return True
    allow = policy.get("allow_domains")
    if allow is not None and not any(_covers(allowed.lower(), domain) for allowed in allow):
        return True
    return internal and not policy.get("allow_internal", True)


def check_policy(policy: dict, domains: list[str], internal: list[str] = (), dependencies: list[str] = ()) -> None:
    max_domains = policy.get("max_domains")
    if max_domains is not None and len(domains) > max_domains:
        raise RequestError(403, f"at most {max_domains} domains may be granted")
    max_internal = policy.get("max_internal_domains")
    if max_internal is not None and len(internal) > max_internal:
        raise RequestError(403, f"at most {max_internal} internal domains may be granted")
    for domain in domains:
        if denied_domain(policy, domain, domain in internal):
            raise RequestError(403, f"domain not permitted: {domain}" + (" (internal)" if domain in internal else ""))
    kinds = policy.get("allow_dependency_kinds", DEFAULT_DEPENDENCY_KINDS)
    for dependency in dependencies:
        if dependency.split(":", 1)[0] not in kinds:
            raise RequestError(403, f"dependency kind not permitted: {dependency.split(':', 1)[0]} (allow_dependency_kinds)")


def _unique(items) -> list:
    # By a hashable key, since a request's lists can be long, and hold anything until validated.
    found = {}
    for item in items:
        found.setdefault(item if isinstance(item, str) else (type(item).__name__, repr(item)), item)
    return list(found.values())


def _without_port(domain: object) -> object:
    """Lowercase a granted domain and drop any :port, since a launch grant covers every port."""
    if not isinstance(domain, str):
        return domain
    host, sep, port = domain.lower().rpartition(":")
    return host if sep and port.isascii() and port.isdigit() and len(port) <= 5 and 0 < int(port) < 65536 else domain.lower()


def _build(config: dict, body: object, read_files: bool = True) -> tuple[dict, dict, str, dict, list[tuple[str, str]]]:
    """The job a request makes, its source and requester, and the combined config sets and blurbs it was made from.
    Unless read_files, its config sets' files are checked but not read, and left empty."""
    if not isinstance(body, dict):
        raise RequestError(400, "body must be a JSON object")
    unknown = set(body) - REQUEST_KEYS
    if unknown:
        raise RequestError(400, f"unknown keys: {', '.join(sorted(unknown))}")
    source = _source(body)
    requester = _requester(body.get("requester"))
    grants = body.get("grants", {})
    if not isinstance(grants, dict) or set(grants) - {"domains", "internal", "dependencies"}:
        raise RequestError(400, "grants must be an object with domains, internal and dependencies")
    try:
        overridden = {key for key in configsets.SINGLES if body.get(key) is not None}
        sets = configsets.load(Path(config.get("configs_dir", "")), _list(body, "configs"), overridden)
        files = configsets.load_files(Path(config.get("configs_dir", "")), sets["files"], read_files)
    except configsets.ConfigSetError as e:
        raise RequestError(400, str(e))
    except OSError:
        # Its reason names a host path.
        log.exception("reading config sets failed")
        raise RequestError(400, "couldn't read the config sets")
    internal = _unique(_without_port(d) for d in sets["internal"] + _list(grants, "internal"))
    domains = _unique(_without_port(d) for d in sets["domains"] + _list(grants, "domains") + internal)
    names = _unique(config.get("default_blurbs", []) + sets["blurbs"] + _list(body, "blurbs"))
    blurbs = load_blurbs(Path(config["blurbs_dir"]), names)
    job = {
        "id": new_task_id(),
        "prompt": compose_prompt(
            _text(body, "task"),
            blurbs,
            _text(body, "context", required=False),
            config.get("output_instructions", OUTPUT_INSTRUCTIONS),
            [name for name, _ in files],
        ),
        "domains": domains,
        "internal_domains": internal,
        "dependencies": _unique(sets["dependencies"] + _list(grants, "dependencies")),
        "files": [{"name": name, "data": base64.b64encode(data).decode()} for name, data in files],
    }
    # The request's own values override its config sets'.
    for key in configsets.SINGLES:
        value = body.get(key) if body.get(key) is not None else sets[key]
        # Chat and config sets give it as text.
        if key == "context_window" and isinstance(value, str) and value.isascii() and value.isdecimal():
            value = int(value)
        if value is not None:
            job[key] = value
    if config.get("remote_access_url"):
        job["session_wait"] = config.get("session_wait", 20)
    try:
        validate_job(job)
    except JobError as e:
        raise RequestError(400, str(e))
    check_policy(config.get("policy", {}), job["domains"], job["internal_domains"], job["dependencies"])
    return job, source, requester, sets, blurbs


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> bytes:
    data = b""
    while chunk := await stream.read(64 * 1024):
        data += chunk
        if len(data) > cap:
            raise RuntimeError(f"launcher sent more than {cap} bytes")
    return data


async def _drain(stream: asyncio.StreamReader) -> None:
    while await stream.read(64 * 1024):
        pass


async def run_remote(config: dict, args: list[str], stdin: bytes = b"") -> tuple[int, bytes, str]:
    """Run the launcher with a command; over ssh, args become SSH_ORIGINAL_COMMAND."""
    timeout = config.get("launch_timeout", 120)
    cap = _limits(config).max_bytes + archive.MANIFEST_SLACK
    try:
        # In a session of its own, so a failure ends anything it started, which could hold the pipes open.
        proc = await asyncio.create_subprocess_exec(
            *config["launch_command"], *args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as e:
        # The reason names launch_command's path, and errors reach chat.
        log.error("couldn't run launch_command: %s", e)
        raise RuntimeError("couldn't run the launcher; the API's log says why")

    async def feed():
        proc.stdin.write(stdin)
        try:
            await proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        proc.stdin.close()

    readers = [asyncio.ensure_future(_read_capped(proc.stdout, cap)), asyncio.ensure_future(_read_capped(proc.stderr, 64 * 1024))]

    async def communicate():
        _, out, err = await asyncio.gather(feed(), *readers)
        await proc.wait()
        return out, err

    try:
        out, err = await asyncio.wait_for(communicate(), timeout)
    except (asyncio.TimeoutError, RuntimeError) as e:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        # wait() returns only once the pipes close, and a reader stopped at its
        # cap leaves its pipe full, so what's left is read and dropped first.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.gather(_drain(proc.stdout), _drain(proc.stderr), proc.wait()), 10)
        raise RuntimeError(f"launcher timed out after {timeout}s" if isinstance(e, asyncio.TimeoutError) else str(e))
    return proc.returncode, out, archive.clean_text(err, 2048).removeprefix("darner-launch: ")


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
    # The hash of the zip as the launcher sent it, which the launcher recorded too:
    # forget-delivered names it, so a share is only pruned for a zip stored here.
    sent = hashlib.sha256(out).hexdigest()
    seq = store.add_event(task_id, "output", summary, len(packed), signalled=signalled, data={"zip_sha256": sent})
    try:
        os.replace(tmp, task_dir / f"{seq}.zip")
    except OSError:
        store.remove_event(seq)
        raise
    # A frontend may not collect until the session ends, so a session repeating
    # .done can't fill the disk: only its latest zips are kept.
    stored = sorted(int(p.stem) for p in task_dir.glob("*.zip") if p.stem.isdigit())
    for old in stored[:-KEPT_ZIPS]:
        (task_dir / f"{old}.zip").unlink(missing_ok=True)
        store.drop_zip(old)
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
            "denied": denied_domain(policy, d["domain"], bool(d["internal"])),
        }
        for d in request["domains"]
        if valid_domain(d.get("domain"))
    ]
    data = {"request": int(request["request"]), "domains": domains, "rejected": int(request.get("rejected", 0))}
    store.add_event(task["id"], "domains", data=data)


def check_waiting(config: dict, store: Store, task: dict) -> None:
    """Record a waiting event for a running session whose screen has gone quiet.

    Not for one waiting on its requester through a help or domain request,
    one that has a result to deliver, one that has delivered output and been
    sent nothing since, or one that has asked or been reported since its
    screen last changed: those are done or already said so. Each quiet spell
    is reported once.
    """
    quiet = task.get("quiet")
    # Starting too: dejima can stop at a prompt of its own before the session runs.
    if task.get("state") not in ("running", "starting") or not isinstance(quiet, (int, float)) or quiet < config["waiting_after"]:
        return
    if any(task.get(key) for key in ("awaiting_answer", "domain_request", "ready", "help", "domains", "unsignalled", "result_pending")):
        return
    output = store.latest_event_time(task["id"], ("output",))
    if output is not None and output >= (store.typed(task["id"]) or 0):
        return
    latest = store.latest_event_time(task["id"], ("output", "help", "domains", "waiting"))
    if latest is not None and latest >= time.time() - quiet:
        return
    screen = archive.clean_text(str(task.get("screen", "")).encode(), 4000) if config["waiting_screen"] else ""
    store.add_event(task["id"], "waiting", screen, data={"quiet": int(quiet)})


async def expire(app: web.Application, task: dict) -> None:
    """Stop a running session once it's older than max_session_age, so none is forgotten."""
    config, store = app[CONFIG], app[STORE]
    age = time.time() - store.created(task["id"])
    if not config["max_session_age"] or age < config["max_session_age"] or task["state"] == "exited":
        return
    if store.latest_event_time(task["id"], ("expired",)) is not None:
        return
    code, _, err = await run_remote(config, ["stop", task["id"]])
    if code:
        # Retried every poll, so logged once.
        if task["id"] not in app[EXPIRE_FAILED]:
            app[EXPIRE_FAILED].add(task["id"])
            log.warning("couldn't stop %s after max_session_age: %s", task["id"], err or f"exit {code}")
        return
    store.add_event(task["id"], "expired", data={"age": int(age)})
    audit.info("stop %s after max_session_age, at %ds", task["id"], int(age))


def end_task(app: web.Application, task_id: str, note: str = "", kept: bool = False) -> None:
    """Delete everything about an ended task but the output its frontend hasn't collected."""
    app[STORE].end(task_id, note, {"share_kept": True} if kept else None)
    kept = app[STORE].kept_outputs(task_id)
    task_dir = Path(app[CONFIG]["results_dir"]) / task_id
    for path in task_dir.glob("*.zip"):
        if not path.stem.isdigit() or int(path.stem) not in kept:
            path.unlink(missing_ok=True)


async def purge(app: web.Application) -> None:
    """Delete ended tasks whose frontend has collected everything, or that waited past output_retention.

    One whose ~/shared the launcher kept waits to be cleared, or for output_retention, and the share goes with it.
    """
    store = app[STORE]
    for task_id in store.purgeable(app[CONFIG]["output_retention"]):
        if store.share_kept(task_id):
            await discard(app, task_id)
        delete_task(app, task_id)


def delete_task(app: web.Application, task_id: str) -> None:
    app[STORE].delete(task_id)
    shutil.rmtree(Path(app[CONFIG]["results_dir"]) / task_id, ignore_errors=True)


async def discard(app: web.Application, task_id: str) -> bool:
    """Have the launcher prune the ~/shared it kept for an ended session. Whether it did."""
    try:
        code, _, err = await run_remote(app[CONFIG], ["discard", task_id])
    except RuntimeError as e:
        code, err = 1, str(e)
    if code:
        log.warning("removing %s's ~/shared failed: %s", task_id, err or f"exit {code}")
        return False
    app[STORE].set_share_kept(task_id, False)
    return True


async def forget(app: web.Application, task_id: str, delivered: str | None = None) -> bool:
    """Have the launcher delete an ended session's task directory. Whether it kept its ~/shared.

    With delivered, the hash of the session's latest zip this API stored, its
    ~/shared too. The launcher prunes it only if that's the zip it last sent
    and the share still holds exactly that. Only the API knows which zip it
    kept, so only it can say. A share it keeps waits for the session to be cleared.
    """
    command = ["forget-delivered", task_id, delivered] if delivered else ["forget", task_id]
    code, out, err = await run_remote(app[CONFIG], command)
    if code:
        log.warning("forget %s failed: %s", task_id, err or f"exit {code}")
        return False
    try:
        kept = json.loads(out).get("kept")
    except (ValueError, AttributeError):
        kept = None
    if not isinstance(kept, str):
        return False
    log.warning("kept %s's ~/shared until it's cleared: %s", task_id, kept)
    app[STORE].set_share_kept(task_id, True)
    return True


async def poll_once(app: web.Application) -> None:
    config, store = app[CONFIG], app[STORE]
    await purge(app)
    # An ended task the launcher has stopped listing needs nothing more from it.
    app[FORGOTTEN].intersection_update(store.ended())
    ours, ended = set(store.launched()), set(store.ended()) - app[FORGOTTEN]
    if not ours and not ended:
        return
    code, out, err = await launcher_status(app, 0)
    if code:
        log.warning("status failed: %s", err or f"exit {code}")
        return
    listed = json.loads(out)
    app[FORGOTTEN].update(ended - {task["id"] for task in listed})
    # A task the launcher no longer lists can never report ending, such as one
    # whose task directory was removed, so it's ended once it's past its age.
    gone = ours - {task["id"] for task in listed}
    for task_id in sorted(gone):
        if config["max_session_age"] and time.time() - store.created(task_id) > config["max_session_age"]:
            end_task(app, task_id)
    for task in listed:
        if task["id"] not in ours:
            # Left behind by a forget that failed, or by a task whose launch was never recorded.
            # One that ended here is retried with its output's hash, so its share is still pruned.
            if task["state"] == "exited" and task["id"] in ended:
                await forget(app, task["id"], store.last_stored_zip(task["id"]))
            elif task["state"] == "exited" and not store.known(task["id"]):
                await forget(app, task["id"])
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
            # Still expired and ended below, or a fetch that always fails would keep it forever.
            log.exception("collecting %s failed", task["id"])
            collected = False
        check_waiting(config, store, task)
        await expire(app, task)
        if task["state"] == "exited" and collected:
            # Delivered and kept: the session's latest output is a zip this API stored.
            # Forgotten first, so its end can say whether its ~/shared waits to be cleared.
            kept = await forget(app, task["id"], store.last_stored_zip(task["id"]))
            end_task(app, task["id"], kept=kept)
        elif task["state"] == "exited" and config["max_session_age"] and (
            time.time() - store.created(task["id"]) > config["max_session_age"]
        ):
            # Its output can't be fetched, so it would otherwise be polled forever. A
            # session expire stopped is always this old, so its last output gets a few polls.
            tries = app[UNCOLLECTED][task["id"]] = app[UNCOLLECTED].get(task["id"], 0) + 1
            if tries >= COLLECT_TRIES:
                app[UNCOLLECTED].pop(task["id"])
                kept = await forget(app, task["id"])
                end_task(app, task["id"], "its output couldn't be collected", kept=kept)


async def _poll_forever(app: web.Application) -> None:
    # Once at startup, so the launcher's warnings, such as a dejima-share it can't find, reach the log.
    try:
        await fetch_catalog(app)
    except Exception as e:
        log.warning("couldn't fetch the launcher's catalog: %s", e)
    while True:
        await asyncio.sleep(app[CONFIG].get("poll_interval", 30))
        try:
            async with app[POLL_LOCK]:
                await poll_once(app)
        except Exception:
            log.exception("poll failed")


async def check_task(request: web.Request) -> web.Response:
    """Poll now rather than at the next poll_interval, and answer the session as it then is.

    Each client may ask once every CHECK_INTERVAL seconds. A poll already
    running is the check, so a slow dejima host never has polls queued up
    behind it.
    """
    client, task_id = _client(request), _task_id(request)
    if request.app[STORE].task_status(client, task_id) != "launched":
        raise _not_found(f"{task_id} isn't running")
    checked = request.app[LAST_CHECK]
    wait = checked.get(client, -CHECK_INTERVAL) + CHECK_INTERVAL - time.monotonic()
    if wait > 0:
        return web.json_response(
            {"error": f"checked in the last {CHECK_INTERVAL}s, perhaps by someone else on this frontend; try again in {int(wait) + 1}s"},
            status=429, headers={"Retry-After": str(int(wait) + 1)},
        )
    checked[client] = time.monotonic()
    lock = request.app[POLL_LOCK]
    if lock.locked():
        async with lock:
            pass
    else:
        async with lock:
            try:
                await poll_once(request.app)
            except Exception as e:
                log.exception("check of %s failed", task_id)
                return web.json_response({"error": f"check failed: {type(e).__name__}"}, status=502)
    found = await _sessions(request, task_id)
    # Gone from the open sessions means the check found it ended.
    return web.json_response(found[0] if found else {"id": task_id, "ended": True})


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


def _session_name(body: dict) -> str | None:
    """The name a request gives its session, without any dn- it was written with."""
    name = body.get("name")
    if name is None or name == "":
        return None
    if not isinstance(name, str):
        raise RequestError(400, NAME_RULE)
    name = name.strip().lower().removeprefix("dn-")
    if not SESSION_NAME.fullmatch(name) or valid_task_id(f"dn-{name}"):
        raise RequestError(400, NAME_RULE)
    return name


def _name_taken(store: Store, client: str, name: str) -> str | None:
    """Why a name can't be given, if it's held: by an open session, or one ended but not yet cleared."""
    held = store.named(client, name)
    if held is None:
        return None
    if store.task_status(client, held) == "ended":
        return f"dn-{name} is still held by {held}, which has ended; clear it to free the name"
    return f"dn-{name} is already the name of an open session"


def _task_id(request: web.Request) -> str:
    """The task a path names: by its id, or as dn-<name> by the name of one of the
    client's open sessions. Only an id goes any further, to the launcher least of all."""
    given = request.match_info["id"]
    if valid_task_id(given):
        return given
    found = request.app[STORE].named(_client(request), given.removeprefix("dn-"))
    if found is None:
        raise _not_found(f"no session {given}")
    return found


def _not_found(error: str) -> web.HTTPNotFound:
    return web.HTTPNotFound(text=json.dumps({"error": error}), content_type="application/json")


@web.middleware
async def _json_errors(request: web.Request, handler) -> web.StreamResponse:
    """Every error as JSON with an error to relay, aiohttp's own included."""
    try:
        return await handler(request)
    except web.HTTPException as e:
        if e.status < 400 or e.content_type == "application/json":
            raise
        return web.json_response({"error": e.reason}, status=e.status)


def _client(request: web.Request) -> str:
    client = authorized(request.app[CONFIG], request.headers.get("Authorization", ""))
    if client is None:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "unauthorized"}), content_type="application/json")
    return client


async def preview_task(request: web.Request) -> web.Response:
    """What POST /v1/tasks would launch for this body, without launching it: the
    settings it asks for, those its config sets gave, and the blurbs, in full.
    A task isn't needed yet. Refused as POST /v1/tasks would refuse it, defaults included."""
    client = _client(request)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "body must be JSON"}, status=400)
    if isinstance(body, dict) and not (isinstance(body.get("task"), str) and body["task"].strip()):
        body = {**body, "task": "-"}
    try:
        job, _, _, sets, blurbs = _build(request.app[CONFIG], body, read_files=False)
        name = _session_name(body)
        if name is not None and (taken := _name_taken(request.app[STORE], client, name)):
            raise RequestError(409, taken)
        await check_job_harness(request.app, job)
    except RequestError as e:
        return web.json_response({"error": str(e)}, status=e.status)
    return web.json_response({
        "name": name,
        "settings": {key: job[key] for key in configsets.SINGLES if key in job},
        "from_configs": {
            key: int(value) if key == "context_window" and value.isdecimal() else value
            for key, value in sets.items() if key in configsets.SINGLES and value is not None
        },
        "domains": job["domains"],
        "internal_domains": job["internal_domains"],
        "dependencies": job["dependencies"],
        "files": [f["name"] for f in job["files"]],
        "blurbs": [{"name": name, "text": text} for name, text in blurbs],
    })


async def create_task(request: web.Request) -> web.Response:
    config = request.app[CONFIG]
    client = _client(request)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "body must be JSON"}, status=400)
    try:
        job, source, requester, _, blurbs = _build(config, body)
        name = _session_name(body)
        await check_job_harness(request.app, job)
    except RequestError as e:
        return web.json_response({"error": str(e)}, status=e.status)

    store = request.app[STORE]
    # Ids are short enough to collide now and then.
    while store.known(job["id"]):
        job["id"] = new_task_id()
    # File contents may be credentials, so the stored job keeps only their names.
    recorded = {
        **job, "files": [f["name"] for f in job["files"]], "configs": _list(body, "configs"),
        "blurbs": [name for name, _ in blurbs],
    }
    try:
        task, new = store.claim(job["id"], client, source, requester, json.dumps(recorded), name)
    except NameTaken:
        return web.json_response({"error": _name_taken(store, client, name)}, status=409)
    if not new:
        if task.status == "pending":
            return web.json_response({"error": f"{task.id} is already being launched"}, status=409)
        return web.json_response({"id": task.id, "name": task.name, "target": task.target, "link": session_link(config, task.run_id)})

    try:
        result = await run_launcher(config, job)
        target = result["target"]
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as e:
        store.delete(job["id"])
        return web.json_response({"error": f"launch failed: {e}"}, status=502)
    store.mark(job["id"], "launched", target=target)
    _audit(
        "start", client, job["id"], requester, domains=job["domains"], internal=job["internal_domains"],
        dependencies=job["dependencies"], files=[f["name"] for f in job["files"]],
    )
    run_id = result.get("run_id")
    if isinstance(run_id, str) and RUN_ID.fullmatch(run_id):
        store.set_run_id(job["id"], run_id)
    else:
        run_id = None
    return web.json_response({"id": job["id"], "name": name, "target": target, "link": session_link(config, run_id)}, status=201)


async def create_selftest(request: web.Request) -> web.Response:
    """A finished task whose output, help request and waiting screen are hostile text.

    It reaches the frontend through /v1/events like any task's, so how the
    frontend shows it is how it shows an agent's.
    """
    client, store = _client(request), request.app[STORE]
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "body must be JSON"}, status=400)
    try:
        if not isinstance(body, dict) or set(body) != {"source", "requester"}:
            raise RequestError(400, "body must have source and requester")
        source, requester = _source(body), _requester(body.get("requester"))
    except RequestError as e:
        return web.json_response({"error": str(e)}, status=e.status)
    task_id = new_task_id()
    while store.known(task_id):
        task_id = new_task_id()
    task, new = store.claim(task_id, client, source, requester, json.dumps({"selftest": True}))
    if not new:
        return web.json_response({"id": task.id})
    store.mark(task.id, "ended")
    text = archive.clean_text(selftest.PAYLOAD.encode())
    store.add_event(task.id, "output", text)
    store.add_event(task.id, "help", text)
    store.add_event(task.id, "waiting", text, data={"quiet": 300})
    store.add_event(task.id, "ended")
    return web.json_response({"id": task.id}, status=201)


def _event_json(config: dict, e) -> dict:
    return {
        "seq": e.seq, "id": e.id, "type": e.type, "summary": e.summary, "size": e.size,
        "answerable": e.answerable, "signalled": e.signalled, "data": e.data,
        "target": e.target, "link": session_link(config, e.run_id),
        "source": e.source, "created": e.created, "name": e.name,
    }


async def list_events(request: web.Request) -> web.Response:
    config = request.app[CONFIG]
    return web.json_response([_event_json(config, e) for e in request.app[STORE].events(_client(request))])


async def task_events(request: web.Request) -> web.Response:
    """A task's events so far, without acknowledging any. Gone with the task once it's purged."""
    client, store = _client(request), request.app[STORE]
    task_id = _task_id(request)
    if store.task_status(client, task_id) is None:
        raise _not_found(f"no session {task_id}")
    return web.json_response([_event_json(request.app[CONFIG], e) for e in store.task_events(client, task_id)])


async def get_output(request: web.Request) -> web.StreamResponse:
    task_id, seq = _task_id(request), int(request.match_info["seq"])
    event = request.app[STORE].event(_client(request), seq)
    path = Path(request.app[CONFIG]["results_dir"]) / task_id / f"{seq}.zip"
    if event is None or event.id != task_id or event.type != "output" or not event.size or not path.is_file():
        raise _not_found("no such output")
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
    task_id = _task_id(request)
    status = request.app[STORE].task_status(client, task_id)
    if status is None:
        raise _not_found(f"no session {task_id}")
    if status == "ended":
        return web.json_response({"error": f"{task_id} has ended"}, status=409)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "body must be JSON"}, status=400)
    if not isinstance(body, dict) or set(body) != {"text", "requester"}:
        return web.json_response({"error": "body must have text and requester"}, status=400)
    try:
        text, requester = _text(body, "text"), _requester(body.get("requester"))
    except RequestError as e:
        return web.json_response({"error": str(e)}, status=e.status)
    payload = json.dumps({"text": text, "requester": requester}).encode()
    # Before typing, since a failure can still have reached the session.
    request.app[STORE].set_typed(task_id)
    try:
        code, _, err = await run_remote(request.app[CONFIG], [command, task_id], payload)
    except RuntimeError as e:
        return web.json_response({"error": f"{command} failed: {e}"}, status=502)
    if code:
        return web.json_response({"error": err or f"launcher exited {code}"}, status=409)
    _audit(command, client, task_id, requester, length=len(text))
    return web.Response(status=204)


async def list_blurbs(request: web.Request) -> web.Response:
    _client(request)
    return web.json_response(available_blurbs(Path(request.app[CONFIG]["blurbs_dir"])))


# What a session's view says it was started with: what was asked for, never the prompt.
STARTED_WITH = (
    "configs", "blurbs", *configsets.SINGLES, "domains", "internal_domains", "dependencies", "files",
)


async def _sessions(request: web.Request, task_id: str | None = None) -> list[dict]:
    """The client's open sessions, with what each is waiting on, asked of the dejima host now."""
    client, store, config = _client(request), request.app[STORE], request.app[CONFIG]
    tasks = store.open_tasks(client, task_id)
    live = {}
    if tasks:
        try:
            code, out, err = await launcher_status(request.app, STATUS_MAX_AGE)
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
            "name": task["name"],
            "target": task["target"],
            "link": session_link(config, task["run_id"]),
            "requester": task["requester"],
            "started": task["created"],
            "state": state.get("state", "unknown"),
            "answerable": bool(state.get("answerable")),
            "waiting": waiting,
            "started_with": {key: task["job"][key] for key in STARTED_WITH if task["job"].get(key)},
        })
    return sessions


async def list_sessions(request: web.Request) -> web.Response:
    return web.json_response(await _sessions(request))


async def get_session(request: web.Request) -> web.Response:
    found = await _sessions(request, _task_id(request))
    if not found:
        raise _not_found(f"no session {_task_id(request)}")
    return web.json_response(found[0])


async def upload_file(request: web.Request) -> web.Response:
    """Pass a requester's file to the session's ~/shared/uploads."""
    client, store, config = _client(request), request.app[STORE], request.app[CONFIG]
    task_id = _task_id(request)
    status = store.task_status(client, task_id)
    if status is None:
        raise _not_found(f"no session {task_id}")
    if status != "launched":
        return web.json_response({"error": f"{task_id} is not running"}, status=409)
    name = request.query.get("name", "")
    try:
        requester = _requester(request.query.get("requester"))
    except RequestError as e:
        return web.json_response({"error": str(e)}, status=e.status)
    if not name or len(name) > 255:
        return web.json_response({"error": "name must be up to 255 characters"}, status=400)
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
    _audit("upload", client, task_id, requester, name=name, size=len(data))
    return web.json_response(json.loads(out))


async def stop_task(request: web.Request) -> web.Response:
    client = _client(request)
    task_id = _task_id(request)
    status = request.app[STORE].task_status(client, task_id)
    if status is None:
        raise _not_found(f"no session {task_id}")
    if status != "launched":
        return web.json_response({"error": f"{task_id} is not running"}, status=409)
    try:
        body = await request.json() if request.can_read_body else {}
        requester = _requester(body.get("requester"))
    except (ValueError, RequestError, AttributeError):
        return web.json_response({"error": "body must have requester"}, status=400)
    try:
        code, _, err = await run_remote(request.app[CONFIG], ["stop", task_id])
    except RuntimeError as e:
        return web.json_response({"error": f"stop failed: {e}"}, status=502)
    if code:
        return web.json_response({"error": err or f"launcher exited {code}"}, status=409)
    _audit("stop", client, task_id, requester)
    # The launcher answers once the session has exited, so a poll now collects
    # what it left and records its end, rather than leaving it shown running
    # until the next poll_interval.
    async with request.app[POLL_LOCK]:
        try:
            await poll_once(request.app)
        except Exception:
            log.exception("poll after stopping %s failed", task_id)
    return web.Response(status=204)


async def decide_domains(request: web.Request) -> web.Response:
    """Allow or deny a task's domain request, as it was shown in its event."""
    client = _client(request)
    store, config = request.app[STORE], request.app[CONFIG]
    task_id, number, verb = _task_id(request), int(request.match_info["n"]), request.match_info["verb"]
    if store.task_status(client, task_id) is None:
        raise _not_found(f"no session {task_id}")
    shown = store.domain_request(client, task_id, number)
    if shown is None:
        raise _not_found(f"no domain request {number} for {task_id}")
    try:
        body = await request.json()
        requester = _requester(body.get("requester"))
    except (ValueError, RequestError, AttributeError):
        return web.json_response({"error": "body must have requester"}, status=400)
    payload = {"requester": requester}
    if verb == "allow":
        # Checked again here, in case the policy changed since the request was shown.
        policy = config.get("policy", {})
        payload["domains"] = [
            {"domain": d["domain"], "internal": d["internal"], "port": d["port"]}
            for d in shown["domains"] if not denied_domain(policy, d["domain"], d["internal"])
        ]
        payload["max_domains"] = policy.get("max_domains")
        payload["max_internal_domains"] = policy.get("max_internal_domains")
    try:
        code, out, err = await run_remote(config, [verb, task_id, str(number)], json.dumps(payload).encode())
    except RuntimeError as e:
        return web.json_response({"error": f"{verb} failed: {e}"}, status=502)
    if code:
        return web.json_response({"error": err or f"launcher exited {code}"}, status=409)
    result = json.loads(out)
    if verb == "allow":
        _audit("allow", client, task_id, requester, request=number, granted=result.get("granted"), failed=result.get("failed"))
    else:
        _audit("deny", client, task_id, requester, request=number, domains=[d["domain"] for d in shown["domains"]])
    return web.json_response(result)


async def list_configs(request: web.Request) -> web.Response:
    _client(request)
    return web.json_response(configsets.available(Path(request.app[CONFIG].get("configs_dir", ""))))


async def get_blurb(request: web.Request) -> web.Response:
    _client(request)
    blurbs_dir, name = Path(request.app[CONFIG]["blurbs_dir"]), request.match_info["name"]
    if name not in available_blurbs(blurbs_dir):
        raise _not_found("no such blurb")
    return web.json_response(load_blurbs(blurbs_dir, [name])[0][1])


async def describe_config(request: web.Request) -> web.Response:
    """What a config set applies, by keyword, and its description. Its files are named, never read."""
    _client(request)
    configs_dir, name = Path(request.app[CONFIG].get("configs_dir", "")), request.match_info["name"]
    if name not in configsets.available(configs_dir):
        raise _not_found(f"no config set {name}")
    try:
        found = configsets.load(configs_dir, [name])
    except configsets.ConfigSetError as e:
        return web.json_response({"error": str(e)}, status=400)
    described = {keyword: found[field] for keyword, field in configsets.LISTS.items() if found[field]}
    described |= {key: found[key] for key in configsets.SINGLES if found[key] is not None}
    if summary := configsets.description((configs_dir / name).read_text()):
        described["description"] = summary
    return web.json_response(described)


async def fetch_catalog(app: web.Application) -> dict:
    """The launcher's catalog, or its failure, shared as the launcher's status is."""
    return await _shared(app[CATALOG_CACHE], CATALOG_TTL, STATUS_MAX_AGE, lambda: _fetch_catalog(app[CONFIG]))


async def _fetch_catalog(config: dict) -> dict:
    code, out, err = await run_remote(config, ["catalog"])
    if code:
        raise RuntimeError(err or f"launcher exited {code}")
    if err:
        log.warning("catalog: %s", err)
    try:
        found = json.loads(out)
    except ValueError:
        found = None
    if not isinstance(found, dict) or not isinstance(found.get("options"), dict):
        raise RuntimeError("launcher sent a catalog that isn't one")
    return found


async def check_job_harness(app: web.Application, job: dict) -> None:
    """Refuse what the job's harness can't take, with the launcher's defaults filled in as it
    fills them. Without a catalog this is left to the launcher, which checks again anyway."""
    try:
        found = await fetch_catalog(app)
    except RuntimeError as e:
        log.warning("can't check harness settings: %s", e)
        return
    harness = job.get("harness") or found.get("default_harness")
    defaults = found.get("defaults", {}).get(harness, {})
    settings = {k: job.get(k) or defaults.get(k) for k in ("backend", "effort", "context_window")}
    try:
        check_harness(found.get("options", {}), harness, **settings)
    except JobError as e:
        raise RequestError(400, str(e))


async def get_catalog(request: web.Request) -> web.Response:
    """The harnesses and backends the dejima host offers, for clients to show."""
    _client(request)
    try:
        found = await fetch_catalog(request.app)
    except RuntimeError as e:
        return web.json_response({"error": f"catalog failed: {e}"}, status=502)
    return web.json_response(describe_catalog(found, request.app[CONFIG]["catalog"]))


def describe_catalog(found: dict, catalog: dict) -> dict:
    """The launcher's catalog, with what each installed harness can be set to:
    its backends, effort levels, whether it takes a context window, its defaults,
    and the operator's models for each backend it can use."""
    options = {}
    for harness, known in found.get("options", {}).items():
        models = {b: models for b, models in catalog.get(harness, {}).items() if b in known["backends"]}
        if not known["context_window"]:
            models = {b: [{"id": m["id"]} for m in ms] for b, ms in models.items()}
        options[harness] = {**known, "defaults": found.get("defaults", {}).get(harness, {}), "models": models}
    return {**found, "options": options}


async def ack_event(request: web.Request) -> web.Response:
    client, seq = _client(request), int(request.match_info["seq"])
    event = request.app[STORE].event(client, seq)
    if event is None or not request.app[STORE].ack(client, seq):
        raise _not_found("no such event")
    # Collected, so its zip goes now, not when the session ends.
    if event.type == "output":
        (Path(request.app[CONFIG]["results_dir"]) / event.id / f"{seq}.zip").unlink(missing_ok=True)
    await purge(request.app)
    return web.Response(status=204)


async def clear_task(request: web.Request) -> web.Response:
    """Delete an ended session now: its output, its events, and the ~/shared the launcher kept for it."""
    client = _client(request)
    task_id = _task_id(request)
    store = request.app[STORE]
    status = store.task_status(client, task_id)
    if status is None:
        raise _not_found(f"no session {task_id}")
    if status != "ended":
        return web.json_response({"error": f"{task_id} is still running. Stop it first."}, status=409)
    try:
        body = await request.json() if request.can_read_body else {}
        requester = _requester(body.get("requester"))
    except (ValueError, RequestError, AttributeError):
        return web.json_response({"error": "body must have requester"}, status=400)
    # The launcher's reason can name host paths, so it goes to the log, not the frontend.
    if store.share_kept(task_id) and not await discard(request.app, task_id):
        return web.json_response(
            {"error": f"couldn't remove {task_id}'s ~/shared on the dejima host; the API's log says why"}, status=502,
        )
    _audit("clear", client, task_id, requester)
    delete_task(request.app, task_id)
    return web.Response(status=204)


def check_tokens(tokens: object) -> None:
    """Refuse a token anyone could guess: examples/api.toml's, or a short one."""
    if not isinstance(tokens, dict):
        raise ValueError("tokens must be a table of frontend names and their tokens")
    for name, token in tokens.items():
        if not isinstance(token, str) or token == EXAMPLE_TOKEN or len(token) < MIN_TOKEN:
            raise ValueError(
                f"the token for {name} must be at least {MIN_TOKEN} characters and not the example's;"
                " generate one with: openssl rand -hex 32"
            )
    if len(set(tokens.values())) < len(tokens):
        raise ValueError("each frontend needs its own token, so tokens must all differ")


def make_app(config: dict, poll: bool = True) -> web.Application:
    full, _, _ = with_defaults(config)
    for key, value in full.items():
        config.setdefault(key, value)
    check_tokens(config["tokens"])
    check_policy_config(config.get("policy", {}))
    config["catalog"] = check_catalog_config(config.get("catalog", {}))
    if config.get("remote_access_url") and not re.match(r"https?://", config["remote_access_url"]):
        raise ValueError("remote_access_url must start with http:// or https://")
    app = web.Application(client_max_size=256 * 1024, middlewares=[_json_errors])
    app[CONFIG] = config
    app[STORE] = Store(config["db_path"])
    available = available_blurbs(Path(config["blurbs_dir"]))
    for name in config.get("default_blurbs", []):
        if name not in available:
            raise ValueError(f"default blurb {name!r} has no {name}.md in {config['blurbs_dir']}")
    app[STORE].fail_pending()
    app[SHARE_ERRORS] = {}
    app[UNCOLLECTED] = {}
    app[FORGOTTEN] = set()
    app[EXPIRE_FAILED] = set()
    app[STATUS_CACHE] = {}
    app[CATALOG_CACHE] = {}
    app[POLL_LOCK] = asyncio.Lock()
    app[LAST_CHECK] = {}
    app.router.add_post("/v1/tasks", create_task)
    app.router.add_post("/v1/tasks/preview", preview_task)
    app.router.add_post("/v1/selftest", create_selftest)
    app.router.add_get("/v1/events", list_events)
    app.router.add_get("/v1/blurbs", list_blurbs)
    app.router.add_get("/v1/catalog", get_catalog)
    app.router.add_get("/v1/blurbs/{name}", get_blurb)
    app.router.add_get("/v1/configs", list_configs)
    app.router.add_get(r"/v1/configs/{name:[a-z0-9][a-z0-9_-]{0,63}}", describe_config)
    app.router.add_get(r"/v1/tasks/{id:dn-[a-z0-9][a-z0-9-]{0,31}}/outputs/{seq:\d{1,9}}.zip", get_output)
    app.router.add_post(r"/v1/events/{seq:\d{1,9}}/ack", ack_event)
    app.router.add_post(r"/v1/tasks/{id:dn-[a-z0-9][a-z0-9-]{0,31}}/answer", answer_help)
    app.router.add_post(r"/v1/tasks/{id:dn-[a-z0-9][a-z0-9-]{0,31}}/stop", stop_task)
    app.router.add_post(r"/v1/tasks/{id:dn-[a-z0-9][a-z0-9-]{0,31}}/clear", clear_task)
    app.router.add_post(r"/v1/tasks/{id:dn-[a-z0-9][a-z0-9-]{0,31}}/message", message_task)
    app.router.add_post(r"/v1/tasks/{id:dn-[a-z0-9][a-z0-9-]{0,31}}/files", upload_file)
    app.router.add_post(r"/v1/tasks/{id:dn-[a-z0-9][a-z0-9-]{0,31}}/check", check_task)
    app.router.add_get("/v1/tasks", list_sessions)
    app.router.add_get(r"/v1/tasks/{id:dn-[a-z0-9][a-z0-9-]{0,31}}", get_session)
    app.router.add_get(r"/v1/tasks/{id:dn-[a-z0-9][a-z0-9-]{0,31}}/events", task_events)
    app.router.add_post(r"/v1/tasks/{id:dn-[a-z0-9][a-z0-9-]{0,31}}/domains/{n:\d{1,9}}/{verb:allow|deny}", decide_domains)
    if poll:
        app.cleanup_ctx.append(poller)
    return app


def main() -> None:
    p = argparse.ArgumentParser(prog="darner-api")
    p.add_argument("--config", default="/etc/darner/api.toml", type=Path)
    args = p.parse_args()
    # A config mistake is reported in one line rather than a traceback, which
    # a restarting container would otherwise repeat without end.
    try:
        config = tomllib.loads(args.config.read_text())
        full, lines, unknown = with_defaults(config)
        host, _, port = full["listen"].rpartition(":")
        app = make_app(config)
        port = int(port)
    except tomllib.TOMLDecodeError as e:
        hint = "; text values need double quotes" if "Invalid value" in str(e) else ""
        sys.exit(f"darner-api: {args.config}: {e}{hint}")
    except KeyError as e:
        sys.exit(f"darner-api: {args.config}: missing setting {e}")
    except (OSError, ValueError) as e:
        sys.exit(f"darner-api: {args.config}: {e}")
    logging.basicConfig(level=logging.INFO)
    log.info("settings from %s:", args.config)
    for line in lines:
        log.info("  %s", line)
    for key in unknown:
        log.warning("%s: unknown setting %r, which this version ignores; check its spelling", args.config, key)
    web.run_app(app, host=host, port=port)
