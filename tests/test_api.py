# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
import asyncio
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

from darner.api import COLLECT_TRIES, KEPT_ZIPS, POLL_LOCK, STATUS_CACHE, STORE, make_app, poll_once
from darner.archive import MANIFEST_SLACK
from darner.store import Store
from tests.test_launch import FAKE_TMUX

STUB = """import json, os, sys
command = sys.argv[1:]
if command == ["status"]:
    if os.environ.get("STUB_STATUS_COUNT"):
        open(os.environ["STUB_STATUS_COUNT"], "a").write("status\\n")
    sys.stdout.write(open(os.environ["STUB_STATUS"]).read())
elif command[:1] == ["catalog"]:
    open(os.environ["STUB_LOG"], "a").write(json.dumps(command) + "\\n")
    sys.stdout.write(os.environ["STUB_CATALOG"])
elif command[:1] == ["help"]:
    sys.stdout.buffer.write(os.environ.get("STUB_HELP", "").encode())
elif command[:1] == ["domains"]:
    sys.stdout.write(os.environ["STUB_DOMAINS"])
elif command[:1] in (["allow"], ["deny"]):
    open(os.environ["STUB_LOG"], "a").write(json.dumps([*command, json.load(sys.stdin)]) + "\\n")
    print(json.dumps({"granted": ["pypi.org"], "failed": [], "told": True}))
elif command[:1] == ["upload"]:
    header = json.loads(sys.stdin.buffer.readline())
    data = sys.stdin.buffer.read()
    open(os.environ["STUB_LOG"], "a").write(json.dumps([*command, header, len(data)]) + "\\n")
    print(json.dumps({"path": "~/shared/uploads/" + header["name"], "size": len(data), "told": False}))
elif command[:1] in (["forget"], ["forget-delivered"]):
    open(os.environ["STUB_LOG"], "a").write(json.dumps(command) + "\\n")
    if os.environ.get("STUB_FORGET_ERROR"):
        sys.exit("darner-launch: " + os.environ["STUB_FORGET_ERROR"])
elif command[:1] == ["stop"]:
    open(os.environ["STUB_LOG"], "a").write(json.dumps(command) + "\\n")
    if os.environ.get("STUB_STOP_ERROR"):
        sys.exit("darner-launch: " + os.environ["STUB_STOP_ERROR"])
elif command[:1] in (["answer"], ["message"]):
    open(os.environ["STUB_LOG"], "a").write(json.dumps([*command, json.load(sys.stdin)]) + "\\n")
    if os.environ.get("STUB_ANSWER_ERROR"):
        sys.exit("darner-launch: " + os.environ["STUB_ANSWER_ERROR"])
elif command[:1] == ["fetch"]:
    open(os.environ["STUB_LOG"], "a").write(json.dumps(command) + "\\n")
    code = int(os.environ.get("STUB_FETCH_CODE", "0"))
    if code:
        sys.stderr.write("darner-launch: boom\\n" if code == 1 else "darner-launch: more than 10 bytes\\n")
        sys.exit(code)
    sys.stdout.buffer.write(open(os.environ["STUB_FETCH"], "rb").read())
else:
    job = json.load(sys.stdin)
    open(os.environ["STUB_LOG"], "a").write(json.dumps(job) + "\\n")
    if os.environ.get("STUB_FAIL"):
        sys.exit("no such harness")
    result = {"id": job["id"], "target": "darner:" + job["id"]}
    if os.environ.get("STUB_RUN_ID"):
        result["run_id"] = os.environ["STUB_RUN_ID"]
    print(json.dumps(result))
"""

AUTH = {"Authorization": "Bearer s3cret-0123456789abcdef0123456789abcdef"}
STOP_BODY = {"requester": "@alice:example.org"}


def request(**overrides):
    body = {
        "task": "Investigate these logs",
        "context": "started after the deploy",
        "blurbs": [],
        "grants": {"domains": ["pypi.org"], "dependencies": ["pip:pytest"]},
        "requester": "@u:example.org",
        "source": {"kind": "matrix", "ref": "$event1", "channel": "!room:example.org"},
    }
    body.update(overrides)
    return body


# As the launcher derives it from dejima's manifests.
OPTIONS = {
    "claude-code": {"backends": ["anthropic", "anthropic-api-key"], "efforts": ["low", "medium", "high", "xhigh"], "context_window": False},
    "cline": {
        "backends": ["anthropic", "anthropic-api-key", "openrouter", "self-hosted"],
        "efforts": ["none", "low", "medium", "high", "xhigh"], "context_window": True,
    },
}
CATALOG = {
    "harnesses": ["claude-code", "cline"], "backends": ["anthropic", "anthropic-api-key", "openrouter", "self-hosted"],
    "default_harness": "claude-code", "defaults": {}, "options": OPTIONS,
}


@pytest.fixture
def config(tmp_path, monkeypatch):
    stub = tmp_path / "stub.py"
    stub.write_text(STUB)
    blurbs = tmp_path / "blurbs"
    blurbs.mkdir()
    (blurbs / "myapp.md").write_text("Repo: example/myapp\n")
    monkeypatch.setenv("STUB_LOG", str(tmp_path / "stub.log"))
    monkeypatch.setenv("STUB_CATALOG", json.dumps(CATALOG))
    return {
        "db_path": str(tmp_path / "db"),
        "results_dir": str(tmp_path / "results"),
        "output_instructions": "Leave it in ~/shared.",
        "blurbs_dir": str(blurbs),
        "tokens": {"maubot": "s3cret-0123456789abcdef0123456789abcdef"},
        "launch_command": [sys.executable, str(stub)],
        "policy": {"deny_domains": ["example.net"], "max_domains": 3},
    }


def launched(tmp_path):
    log = tmp_path / "stub.log"
    jobs = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [j for j in jobs if isinstance(j, dict)]


def fetches(tmp_path):
    log = tmp_path / "stub.log"
    return [j for j in map(json.loads, log.read_text().splitlines()) if isinstance(j, list)] if log.exists() else []


@pytest.fixture
async def client(aiohttp_client, config):
    return await aiohttp_client(make_app(config, poll=False))


async def test_launches_task(client, tmp_path):
    resp = await client.post("/v1/tasks", json=request(blurbs=["myapp"], harness="cline"), headers=AUTH)
    assert resp.status == 201
    body = await resp.json()
    assert body["target"] == "darner:" + body["id"]

    [job] = launched(tmp_path)
    assert job["id"] == body["id"]
    assert job["prompt"] == (
        "Investigate these logs\n\n## Context: myapp\nRepo: example/myapp\n\n"
        "## Context from requester\nstarted after the deploy\n\n"
        "## Delivering your output\nLeave it in ~/shared.\n"
    )
    assert job["domains"] == ["pypi.org"] and job["dependencies"] == ["pip:pytest"]
    assert job["harness"] == "cline" and "backend" not in job


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "s3cret-0123456789abcdef0123456789abcdef"}])
async def test_rejects_bad_auth(client, headers):
    resp = await client.post("/v1/tasks", json=request(), headers=headers)
    assert resp.status == 401


@pytest.mark.parametrize(
    "overrides,status,message",
    [
        ({"identity": "me"}, 400, "unknown keys: identity"),
        ({"task": " "}, 400, "task must be"),
        ({"source": {"kind": "matrix"}}, 400, "source must be"),
        ({"grants": {"domains": ["localhost"]}}, 400, "not a valid domain"),
        ({"grants": {"dependencies": ["apt:$(id)"]}}, 400, "not a valid dependency"),
        ({"harness": "Not Valid"}, 400, "not a valid harness"),
        ({"blurbs": ["nope"]}, 400, "unknown context blurb 'nope' (available: myapp)"),
        ({"grants": {"domains": ["api.example.net"]}}, 403, "domain not permitted"),
        ({"grants": {"domains": ["API.Example.NET"]}}, 403, "domain not permitted"),
        ({"grants": {"domains": ["net"]}}, 400, "not a valid domain"),
        ({"grants": {"domains": ["a.example.com", "b.example.com", "c.example.com", "d.example.com"]}}, 403, "at most 3"),
    ],
)
async def test_rejects_invalid(client, tmp_path, overrides, status, message):
    resp = await client.post("/v1/tasks", json=request(**overrides), headers=AUTH)
    assert resp.status == status
    assert message in (await resp.json())["error"]
    assert launched(tmp_path) == []


async def test_parent_of_denied_domain_is_denied(aiohttp_client, config, tmp_path):
    config["policy"]["deny_domains"] = ["internal.example.org"]
    client = await aiohttp_client(make_app(config, poll=False))
    resp = await client.post("/v1/tasks", json=request(grants={"domains": ["example.org"]}), headers=AUTH)
    assert resp.status == 403


async def test_domains_are_lowercased(client, tmp_path):
    resp = await client.post("/v1/tasks", json=request(grants={"domains": ["PyPI.org"]}), headers=AUTH)
    assert resp.status == 201
    assert launched(tmp_path)[0]["domains"] == ["pypi.org"]


async def test_non_utf8_body_is_rejected(client):
    resp = await client.post("/v1/tasks", data=b"\xff\xfe", headers=AUTH)
    assert resp.status == 400


async def test_pending_task_from_previous_run_is_retried(aiohttp_client, config, tmp_path):
    from darner.store import Store

    Store(config["db_path"]).claim("dn-aaaaa", "maubot", request()["source"], "@u:example.org", "{}")
    client = await aiohttp_client(make_app(config, poll=False))
    resp = await client.post("/v1/tasks", json=request(), headers=AUTH)
    assert resp.status == 201


async def test_repeat_returns_existing_task(client, tmp_path):
    first = await (await client.post("/v1/tasks", json=request(), headers=AUTH)).json()
    resp = await client.post("/v1/tasks", json=request(), headers=AUTH)
    assert resp.status == 200
    assert await resp.json() == first
    assert len(launched(tmp_path)) == 1


async def test_failed_launch_is_reported_and_retryable(client, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_FAIL", "1")
    resp = await client.post("/v1/tasks", json=request(), headers=AUTH)
    assert resp.status == 502
    assert "no such harness" in (await resp.json())["error"]

    monkeypatch.delenv("STUB_FAIL")
    resp = await client.post("/v1/tasks", json=request(), headers=AUTH)
    assert resp.status == 201
    assert len(launched(tmp_path)) == 2


async def test_end_to_end_with_launcher(aiohttp_client, config, tmp_path, monkeypatch):
    tmux = tmp_path / "tmux"
    tmux.write_text(FAKE_TMUX)
    tmux.chmod(0o755)
    monkeypatch.setenv("FAKE_TMUX_LOG", str(tmp_path / "tmux.log"))
    dejima = tmp_path / "dejima"
    dejima.write_text("#!/bin/sh\n")
    dejima.chmod(0o755)
    config["launch_command"] = [
        sys.executable, "-m", "darner.launch", "--tmux", str(tmux), "--tasks-dir", str(tmp_path / "tasks"),
        "--dejima", str(dejima),
        "--harness", "claude-code", "--backend", "claude-code=anthropic", "--share-root", str(tmp_path / "isolated"),
    ]
    app = make_app(config, poll=False)
    client = await aiohttp_client(app)
    resp = await client.post("/v1/tasks", json=request(), headers=AUTH)
    assert resp.status == 201, await resp.text()
    task_id = (await resp.json())["id"]
    argv = json.loads((tmp_path / "tasks" / task_id / "argv.json").read_text())
    assert argv[3:5] == ["--shared-path-file", str(tmp_path / "tasks" / task_id / "shared-path")]
    assert argv[5:9] == ["--harness", "claude-code", "--backend", "anthropic"]
    assert argv[-1].startswith("Investigate these logs")

    # What dejima and the agent would do: record ~/shared, leave output, signal it.
    shared = tmp_path / "isolated" / "20261025-120000-AbC123"
    shared.mkdir(parents=True)
    (tmp_path / "tasks" / task_id / "shared-path").write_text(f"{shared}\n")
    (shared / "RESULT.md").write_text("The import fails on empty rows.")
    (shared / "fix.patch").write_text("diff")
    (shared / "leak").symlink_to(tmp_path / "db")
    (shared / ".done").touch()
    await poll_once(app)

    [event] = await events(client)
    assert event["summary"] == "The import fails on empty rows."
    body = await (await client.get(f"/v1/tasks/{task_id}/outputs/{event['seq']}.zip", headers=AUTH)).read()
    archive = zipfile.ZipFile(io.BytesIO(body))
    assert archive.namelist() == ["RESULT.md", "fix.patch", "MANIFEST.txt"]
    assert "'leak': symlink" in archive.read("MANIFEST.txt").decode()
    assert not (shared / ".done").exists()


def zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o100644 << 16
            zf.writestr(info, content)
    return buf.getvalue()


@pytest.fixture
async def started(aiohttp_client, config, tmp_path, monkeypatch):
    """A launched task, an app that doesn't poll by itself, and helpers to drive the stub."""
    app = make_app(config, poll=False)
    client = await aiohttp_client(app)
    task_id = (await (await client.post("/v1/tasks", json=request(), headers=AUTH)).json())["id"]

    def status(state="running", ready=True, help=False, answerable=False):
        path = tmp_path / "status.json"
        path.write_text(json.dumps([
            {"id": task_id, "state": state, "ready": ready, "help": help, "answerable": answerable},
            {"id": "dn-zzzzz", "state": "exited", "ready": True, "help": True},
        ]))
        monkeypatch.setenv("STUB_STATUS", str(path))

    def output(data):
        path = tmp_path / "out.zip"
        path.write_bytes(data)
        monkeypatch.setenv("STUB_FETCH", str(path))

    return app, client, task_id, status, output


async def events(client, headers=AUTH):
    resp = await client.get("/v1/events", headers=headers)
    assert resp.status == 200
    return await resp.json()


async def test_an_acked_output_s_zip_is_deleted(started, tmp_path):
    app, client, task_id, status, output = started
    status()
    output(zip_bytes([("RESULT.md", "done")]))
    await poll_once(app)
    [event] = await events(client)
    assert (tmp_path / "results" / task_id / f"{event['seq']}.zip").is_file()
    assert (await client.post(f"/v1/events/{event['seq']}/ack", headers=AUTH)).status == 204
    assert not (tmp_path / "results" / task_id / f"{event['seq']}.zip").exists()


async def test_only_a_task_s_latest_zips_are_kept(started, tmp_path):
    app, client, task_id, status, output = started
    status()
    for i in range(KEPT_ZIPS + 2):
        output(zip_bytes([("RESULT.md", f"take {i}")]))
        await poll_once(app)
    seqs = sorted(e["seq"] for e in await events(client) if e["type"] == "output")
    assert len(seqs) == KEPT_ZIPS + 2
    kept = sorted(int(p.stem) for p in (tmp_path / "results" / task_id).glob("*.zip"))
    assert kept == seqs[-KEPT_ZIPS:]


async def test_ready_output_becomes_event_and_download(started, tmp_path):
    app, client, task_id, status, output = started
    status()
    output(zip_bytes([("RESULT.md", "Found it.\x1b[0m"), ("report.txt", "details"), ("MANIFEST.txt", "x")]))
    await poll_once(app)

    [event] = await events(client)
    assert event["id"] == task_id and event["type"] == "output" and event["summary"] == "Found it.[0m"
    assert event["source"] == request()["source"]
    assert [f for f in fetches(tmp_path) if f[0] == "fetch"] == [["fetch", task_id]]

    resp = await client.get(f"/v1/tasks/{task_id}/outputs/{event['seq']}.zip", headers=AUTH)
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "application/zip"
    assert resp.headers["Content-Disposition"] == f'attachment; filename="{task_id}-{event["seq"]}.zip"'
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    body = await resp.read()
    assert len(body) == event["size"]
    assert zipfile.ZipFile(io.BytesIO(body)).read("report.txt") == b"details"


async def test_unsafe_output_is_rejected_with_reason(started):
    app, client, task_id, status, output = started
    status()
    output(zip_bytes([("../escape", "x")]))
    await poll_once(app)
    [event] = await events(client)
    assert event["size"] == 0 and event["summary"].startswith("output rejected: unsafe entry name")
    resp = await client.get(f"/v1/tasks/{task_id}/outputs/{event['seq']}.zip", headers=AUTH)
    assert resp.status == 404


async def test_launcher_rejection_becomes_event(started, monkeypatch):
    app, client, task_id, status, _ = started
    status()
    monkeypatch.setenv("STUB_FETCH_CODE", "3")
    await poll_once(app)
    [event] = await events(client)
    assert event["summary"] == "output rejected: more than 10 bytes"


async def test_failed_fetch_is_retried(started, monkeypatch):
    app, client, task_id, status, output = started
    status(state="exited")
    monkeypatch.setenv("STUB_FETCH_CODE", "1")
    await poll_once(app)
    assert await events(client) == []

    monkeypatch.delenv("STUB_FETCH_CODE")
    output(zip_bytes([("RESULT.md", "ok")]))
    await poll_once(app)
    assert [e["type"] for e in await events(client)] == ["output", "ended"]


async def test_exited_task_ends_once(started):
    app, client, task_id, status, _ = started
    status(state="exited", ready=False)
    await poll_once(app)
    await poll_once(app)
    assert [e["type"] for e in await events(client)] == ["ended"]


async def test_status_failure_changes_nothing(started, monkeypatch, tmp_path):
    app, client, *_ = started
    monkeypatch.setenv("STUB_STATUS", str(tmp_path / "missing"))
    await poll_once(app)
    assert await events(client) == []


async def test_ack_and_client_scoping(started, config):
    app, client, task_id, status, output = started
    status(state="exited", ready=False)
    await poll_once(app)
    [event] = await events(client)

    config["tokens"]["other"] = "0ther-0123456789abcdef0123456789abcdef"
    other = {"Authorization": "Bearer 0ther-0123456789abcdef0123456789abcdef"}
    assert await events(client, other) == []
    assert (await client.post(f"/v1/events/{event['seq']}/ack", headers=other)).status == 404
    assert (await client.post(f"/v1/events/{event['seq']}/ack", headers=AUTH)).status == 204
    assert await events(client) == []
    assert (await client.post("/v1/events/999/ack", headers=AUTH)).status == 404


async def test_other_clients_output_is_not_downloadable(started, config):
    app, client, task_id, status, output = started
    status()
    output(zip_bytes([("RESULT.md", "ok")]))
    await poll_once(app)
    [event] = await events(client)
    config["tokens"]["other"] = "0ther-0123456789abcdef0123456789abcdef"
    resp = await client.get(f"/v1/tasks/{task_id}/outputs/{event['seq']}.zip", headers={"Authorization": "Bearer 0ther-0123456789abcdef0123456789abcdef"})
    assert resp.status == 404
    resp = await client.get(f"/v1/tasks/dn-zzzzz/outputs/{event['seq']}.zip", headers=AUTH)
    assert resp.status == 404


@pytest.mark.parametrize("path", ["/v1/events", "/v1/tasks/dn-aaaaa/outputs/1.zip"])
async def test_events_need_auth(client, path):
    assert (await client.get(path)).status == 401


async def test_invalid_channel_is_rejected(client):
    body = request(source={"kind": "matrix", "ref": "$e", "channel": ""})
    resp = await client.post("/v1/tasks", json=body, headers=AUTH)
    assert resp.status == 400


async def test_oversized_launcher_output_is_cut_off(started, config):
    app, client, task_id, status, output = started
    status()
    config["limits"] = {"max_bytes": 1000 - MANIFEST_SLACK}
    output(b"x" * 5000)
    await poll_once(app)
    assert await events(client) == []


async def test_one_failing_task_does_not_block_others(aiohttp_client, config, tmp_path, monkeypatch):
    app = make_app(config, poll=False)
    client = await aiohttp_client(app)
    first = (await (await client.post("/v1/tasks", json=request(), headers=AUTH)).json())["id"]
    second_source = {"kind": "matrix", "ref": "$event2", "channel": "!room:example.org"}
    second = (await (await client.post("/v1/tasks", json=request(source=second_source), headers=AUTH)).json())["id"]
    path = tmp_path / "status.json"
    path.write_text(json.dumps([
        {"id": first, "state": "running", "ready": True},
        {"id": second, "state": "exited", "ready": False},
    ]))
    monkeypatch.setenv("STUB_STATUS", str(path))
    monkeypatch.setenv("STUB_FETCH", str(tmp_path / "missing.zip"))
    await poll_once(app)
    assert [(e["id"], e["type"]) for e in await events(client)] == [(second, "ended")]


@pytest.mark.parametrize("answerable", [True, False])
async def test_help_request_becomes_event(started, monkeypatch, answerable):
    app, client, task_id, status, _ = started
    status(ready=False, help=True, answerable=answerable)
    monkeypatch.setenv("STUB_HELP", "Which branch?\x1b[2J")
    await poll_once(app)
    [event] = await events(client)
    assert event["type"] == "help" and event["summary"] == "Which branch?[2J"
    assert event["answerable"] is answerable
    assert event["target"] == f"darner:{task_id}"


async def test_answer_reaches_launcher(started, tmp_path):
    app, client, task_id, *_ = started
    body = {"text": "Use main.", "requester": "@alice:example.org"}
    resp = await client.post(f"/v1/tasks/{task_id}/answer", json=body, headers=AUTH)
    assert resp.status == 204
    assert fetches(tmp_path)[-1] == ["answer", task_id, body]


async def test_answer_refusal_is_relayed(started, monkeypatch):
    app, client, task_id, *_ = started
    monkeypatch.setenv("STUB_ANSWER_ERROR", "answering from chat is not enabled on this host")
    resp = await client.post(f"/v1/tasks/{task_id}/answer", json={"text": "x", "requester": "@a:example.org"}, headers=AUTH)
    assert resp.status == 409
    assert (await resp.json())["error"] == "answering from chat is not enabled on this host"


async def test_answer_is_scoped_to_client(started, config, tmp_path):
    app, client, task_id, *_ = started
    config["tokens"]["other"] = "0ther-0123456789abcdef0123456789abcdef"
    body = {"text": "x", "requester": "@a:example.org"}
    resp = await client.post(f"/v1/tasks/{task_id}/answer", json=body, headers={"Authorization": "Bearer 0ther-0123456789abcdef0123456789abcdef"})
    assert resp.status == 404
    assert (await client.post(f"/v1/tasks/{task_id}/answer", json=body)).status == 401
    assert all(f[0] != "answer" for f in fetches(tmp_path))


@pytest.mark.parametrize("body", [{"text": ""}, {"text": "x"}, {"text": "x", "requester": "@a", "extra": 1}, ["x"]])
async def test_answer_rejects_bad_body(started, body):
    app, client, task_id, *_ = started
    assert (await client.post(f"/v1/tasks/{task_id}/answer", json=body, headers=AUTH)).status == 400


async def test_blurbs_are_listed(client):
    resp = await client.get("/v1/blurbs", headers=AUTH)
    assert resp.status == 200 and await resp.json() == ["myapp"]
    assert (await client.get("/v1/blurbs")).status == 401


RUN = "0123456789abcdef"


async def test_no_link_without_remote_access_url(client, tmp_path):
    body = await (await client.post("/v1/tasks", json=request(), headers=AUTH)).json()
    assert body["link"] is None
    assert "session_wait" not in launched(tmp_path)[0]


async def test_started_task_links_to_its_session(aiohttp_client, config, tmp_path, monkeypatch):
    config["remote_access_url"] = "https://dejima.example.com/"
    monkeypatch.setenv("STUB_RUN_ID", RUN)
    client = await aiohttp_client(make_app(config, poll=False))
    resp = await client.post("/v1/tasks", json=request(), headers=AUTH)
    assert (await resp.json())["link"] == f"https://dejima.example.com/?arg={RUN}"
    assert launched(tmp_path)[0]["session_wait"] == 20

    repeat = await (await client.post("/v1/tasks", json=request(), headers=AUTH)).json()
    assert repeat["link"] == f"https://dejima.example.com/?arg={RUN}"


async def test_bad_run_id_from_launcher_is_dropped(aiohttp_client, config, monkeypatch):
    config["remote_access_url"] = "https://dejima.example.com"
    monkeypatch.setenv("STUB_RUN_ID", "../../etc")
    client = await aiohttp_client(make_app(config, poll=False))
    assert (await (await client.post("/v1/tasks", json=request(), headers=AUTH)).json())["link"] is None


async def test_run_id_from_status_links_later_events(started, config, monkeypatch, tmp_path):
    app, client, task_id, status, _ = started
    config["remote_access_url"] = "https://dejima.example.com"
    path = tmp_path / "status.json"
    path.write_text(json.dumps([
        {"id": task_id, "state": "running", "ready": False, "help": True, "answerable": False, "run_id": RUN},
    ]))
    monkeypatch.setenv("STUB_STATUS", str(path))
    monkeypatch.setenv("STUB_HELP", "Which branch?")
    await poll_once(app)
    [event] = await events(client)
    assert event["type"] == "help" and event["link"] == f"https://dejima.example.com/?arg={RUN}"


def test_remote_access_url_must_be_http(config):
    config["remote_access_url"] = "javascript:alert(1)"
    with pytest.raises(ValueError):
        make_app(config, poll=False)


def test_existing_database_gains_run_id_column(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, client TEXT NOT NULL, source_kind TEXT NOT NULL,"
        " source_ref TEXT NOT NULL, source_json TEXT NOT NULL, requester TEXT NOT NULL, job_json TEXT NOT NULL,"
        " status TEXT NOT NULL, target TEXT, error TEXT, created REAL NOT NULL, UNIQUE (client, source_kind, source_ref));"
        "INSERT INTO tasks VALUES ('dn-aaaaa', 'maubot', 'matrix', '$e', '{}', '@u:example.org', '{}', 'launched',"
        " 'darner:dn-aaaaa', NULL, 0);"
    )
    old.commit()
    old.close()
    store = Store(str(db))
    store.set_run_id("dn-aaaaa", RUN)
    assert store.launched() == ["dn-aaaaa"]


async def test_unsignalled_result_is_collected_and_marked(started, tmp_path, monkeypatch):
    app, client, task_id, _, output = started
    path = tmp_path / "status.json"
    path.write_text(json.dumps([{"id": task_id, "state": "running", "ready": False, "unsignalled": True, "help": False}]))
    monkeypatch.setenv("STUB_STATUS", str(path))
    output(zip_bytes([("RESULT.md", "forgot .done")]))
    await poll_once(app)
    [event] = await events(client)
    assert event["type"] == "output" and event["signalled"] is False and event["summary"] == "forgot .done"


async def test_signalled_output_is_marked_signalled(started):
    app, client, task_id, status, output = started
    status()
    output(zip_bytes([("RESULT.md", "ok")]))
    await poll_once(app)
    assert (await events(client))[0]["signalled"] is True


def test_existing_events_table_gains_signalled_column(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    Store(str(db))
    old = sqlite3.connect(db)
    old.executescript(
        "ALTER TABLE events RENAME TO events_new;"
        "CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, type TEXT NOT NULL,"
        " summary TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL DEFAULT 0, answerable INTEGER NOT NULL DEFAULT 0,"
        " acked INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);"
        "DROP TABLE events_new;"
        "INSERT INTO tasks (id, client, source_kind, source_ref, source_json, requester, job_json, status, created)"
        " VALUES ('dn-aaaaa', 'maubot', 'matrix', '$e', '{}', '@u:example.org', '{}', 'launched', 0);"
        "INSERT INTO events (task_id, type, created) VALUES ('dn-aaaaa', 'ended', 0);"
    )
    old.commit()
    old.close()
    store = Store(str(db))
    [event] = store.events("maubot")
    assert event.signalled is True
    store.add_event("dn-aaaaa", "output", signalled=False)
    assert store.events("maubot")[1].signalled is False


@pytest.mark.parametrize(
    "toml, message",
    [
        ('listen = "127.0.0.1:8080"\nremote_access_url = https://x\n', "line 2, column 21); text values need double quotes"),
        ('listen = "127.0.0.1:8080"\n', "missing setting 'db_path'"),
    ],
)
def test_config_errors_are_one_line(tmp_path, toml, message):
    import subprocess

    path = tmp_path / "api.toml"
    path.write_text(toml)
    result = subprocess.run(
        [sys.executable, "-c", "from darner.api import main; main()", "--config", str(path)],
        capture_output=True, text=True, cwd=Path(__file__).parent.parent,
    )
    assert result.returncode == 1
    assert result.stderr.strip().startswith(f"darner-api: {path}: ")
    assert message in result.stderr and "Traceback" not in result.stderr


async def test_share_error_is_logged_once(started, tmp_path, monkeypatch, caplog):
    app, client, task_id, *_ = started
    path = tmp_path / "status.json"
    path.write_text(json.dumps([{"id": task_id, "state": "running", "ready": False, "error": "shared path '/x' is not under /y"}]))
    monkeypatch.setenv("STUB_STATUS", str(path))
    with caplog.at_level("WARNING", logger="darner.api"):
        await poll_once(app)
        await poll_once(app)
    warnings = [r.getMessage() for r in caplog.records if "can't read" in r.getMessage()]
    assert warnings == [f"can't read {task_id}'s ~/shared, so its output can't be collected: shared path '/x' is not under /y"]


async def test_stop_reaches_launcher(started, tmp_path):
    app, client, task_id, *_ = started
    assert (await client.post(f"/v1/tasks/{task_id}/stop", json=STOP_BODY, headers=AUTH)).status == 204
    assert ["stop", task_id] in fetches(tmp_path)


async def test_a_stopped_session_s_end_is_recorded_before_stop_answers(started, tmp_path):
    app, client, task_id, status, *_ = started
    # As the launcher reports it once its stop has returned.
    status(state="exited", ready=False)
    assert (await client.post(f"/v1/tasks/{task_id}/stop", json=STOP_BODY, headers=AUTH)).status == 204
    assert (await client.get(f"/v1/tasks/{task_id}", headers=AUTH)).status == 404
    assert app[STORE].task_status("maubot", task_id) == "ended"


async def test_stop_is_scoped_and_needs_a_running_task(started, config, tmp_path, monkeypatch):
    app, client, task_id, *_ = started
    config["tokens"]["other"] = "0ther-0123456789abcdef0123456789abcdef"
    assert (await client.post(f"/v1/tasks/{task_id}/stop", headers={"Authorization": "Bearer 0ther-0123456789abcdef0123456789abcdef"})).status == 404
    assert (await client.post(f"/v1/tasks/{task_id}/stop")).status == 401
    monkeypatch.setenv("STUB_STOP_ERROR", "dn-x is not running")
    resp = await client.post(f"/v1/tasks/{task_id}/stop", json=STOP_BODY, headers=AUTH)
    assert resp.status == 409 and (await resp.json())["error"] == "dn-x is not running"


async def test_stop_refuses_an_ended_task(started, tmp_path):

    app, client, task_id, *_ = started
    app[STORE].mark(task_id, "ended")
    resp = await client.post(f"/v1/tasks/{task_id}/stop", json=STOP_BODY, headers=AUTH)
    assert resp.status == 409 and "not running" in (await resp.json())["error"]
    assert ["stop", task_id] not in fetches(tmp_path)


DOMAIN_REQUEST = {
    "request": 3,
    "domains": [
        {"domain": "pypi.org", "internal": False},
        {"domain": "mirror.internal.example", "internal": True},
        {"domain": "api.example.net", "internal": False},
    ],
    "rejected": 1,
}


@pytest.fixture
async def requested(started, tmp_path, monkeypatch):
    app, client, task_id, *_ = started
    path = tmp_path / "status.json"
    path.write_text(json.dumps([{"id": task_id, "state": "running", "ready": False, "domains": True}]))
    monkeypatch.setenv("STUB_STATUS", str(path))
    monkeypatch.setenv("STUB_DOMAINS", json.dumps(DOMAIN_REQUEST))
    await poll_once(app)
    return app, client, task_id


async def test_domain_request_becomes_event_with_policy_marked(requested):
    app, client, task_id = requested
    [event] = await events(client)
    assert event["type"] == "domains"
    assert event["data"] == {
        "request": 3,
        "domains": [
            {"domain": "pypi.org", "internal": False, "port": None, "denied": False},
            {"domain": "mirror.internal.example", "internal": True, "port": None, "denied": False},
            {"domain": "api.example.net", "internal": False, "port": None, "denied": True},
        ],
        "rejected": 1,
    }


async def test_allow_passes_only_what_policy_permits(requested, tmp_path):
    app, client, task_id = requested
    resp = await client.post(f"/v1/tasks/{task_id}/domains/3/allow", json={"requester": "@alice:example.org"}, headers=AUTH)
    assert resp.status == 200 and (await resp.json())["granted"] == ["pypi.org"]
    assert fetches(tmp_path)[-1] == [
        "allow", task_id, "3",
        {
            "requester": "@alice:example.org",
            "domains": [
                {"domain": "pypi.org", "internal": False, "port": None},
                {"domain": "mirror.internal.example", "internal": True, "port": None},
            ],
            "max_domains": 3, "max_internal_domains": None,
        },
    ]


async def test_deny_reaches_launcher(requested, tmp_path):
    app, client, task_id = requested
    resp = await client.post(f"/v1/tasks/{task_id}/domains/3/deny", json={"requester": "@alice:example.org"}, headers=AUTH)
    assert resp.status == 200
    assert fetches(tmp_path)[-1] == ["deny", task_id, "3", {"requester": "@alice:example.org"}]


async def test_domain_decisions_are_scoped(requested, config, tmp_path):
    app, client, task_id = requested
    config["tokens"]["other"] = "0ther-0123456789abcdef0123456789abcdef"
    other = {"Authorization": "Bearer 0ther-0123456789abcdef0123456789abcdef"}
    body = {"requester": "@a:example.org"}
    assert (await client.post(f"/v1/tasks/{task_id}/domains/3/allow", json=body, headers=other)).status == 404
    assert (await client.post(f"/v1/tasks/{task_id}/domains/9/allow", json=body, headers=AUTH)).status == 404
    assert (await client.post(f"/v1/tasks/{task_id}/domains/3/allow", json={}, headers=AUTH)).status == 400
    assert not any(f[0] == "allow" for f in fetches(tmp_path))


async def test_sessions_list_what_each_waits_on(started, tmp_path, monkeypatch, config):
    app, client, task_id, *_ = started
    config["remote_access_url"] = "https://dejima.example.com"
    path = tmp_path / "status.json"
    path.write_text(json.dumps([{"id": task_id, "state": "running", "ready": False, "help": True, "answerable": True}]))
    monkeypatch.setenv("STUB_STATUS", str(path))
    monkeypatch.setenv("STUB_HELP", "Which branch?")
    await poll_once(app)
    path.write_text(json.dumps([{
        "id": task_id, "state": "running", "answerable": True, "awaiting_answer": True, "domain_request": None,
        "run_id": RUN,
    }]))
    await poll_once(app)
    resp = await client.get("/v1/tasks", headers=AUTH)
    [session] = await resp.json()
    assert session["id"] == task_id and session["state"] == "running" and session["answerable"] is True
    assert session["requester"] == "@u:example.org" and session["started"] > 0
    assert session["link"] == f"https://dejima.example.com/?arg={RUN}"
    assert session["waiting"] == {"answer": "Which branch?", "domains": None}
    single = await (await client.get(f"/v1/tasks/{task_id}", headers=AUTH)).json()
    assert single == session
    # What the request asked for, never the prompt.
    assert session["started_with"] == {"domains": ["pypi.org"], "dependencies": ["pip:pytest"]}
    [help_event] = await (await client.get(f"/v1/tasks/{task_id}/events", headers=AUTH)).json()
    assert help_event["created"] > 0


async def test_a_session_says_the_config_sets_and_blurbs_it_was_started_with(client, config):
    configs = Path(config["blurbs_dir"]).parent / "configs"
    configs.mkdir()
    (configs / "recipes").write_text("harness: cline\ncontext_window: 30000\nblurb: myapp\n")
    task_id = (await (await client.post("/v1/tasks", json=request(configs=["recipes"]), headers=AUTH)).json())["id"]
    session = await (await client.get(f"/v1/tasks/{task_id}", headers=AUTH)).json()
    assert session["started_with"] == {
        "configs": ["recipes"], "blurbs": ["myapp"], "harness": "cline", "context_window": 30000,
        "domains": ["pypi.org"], "dependencies": ["pip:pytest"],
    }


async def test_session_waiting_on_domains(requested, tmp_path, monkeypatch):
    app, client, task_id = requested
    path = tmp_path / "status.json"
    path.write_text(json.dumps([{"id": task_id, "state": "running", "domain_request": 3}]))
    monkeypatch.setenv("STUB_STATUS", str(path))
    app[STATUS_CACHE].clear()  # the poll's status would otherwise be reused
    [session] = await (await client.get("/v1/tasks", headers=AUTH)).json()
    assert session["waiting"]["answer"] is None
    assert [d["domain"] for d in session["waiting"]["domains"]["domains"]] == ["pypi.org", "mirror.internal.example", "api.example.net"]


async def test_sessions_are_scoped_and_open_only(started, config, tmp_path, monkeypatch):

    app, client, task_id, *_ = started
    config["tokens"]["other"] = "0ther-0123456789abcdef0123456789abcdef"
    assert await (await client.get("/v1/tasks", headers={"Authorization": "Bearer 0ther-0123456789abcdef0123456789abcdef"})).json() == []
    assert (await client.get(f"/v1/tasks/{task_id}", headers={"Authorization": "Bearer 0ther-0123456789abcdef0123456789abcdef"})).status == 404
    app[STORE].mark(task_id, "ended")
    assert await (await client.get("/v1/tasks", headers=AUTH)).json() == []
    assert (await client.get("/v1/tasks")).status == 401


async def test_sessions_survive_an_unreachable_host(started, tmp_path, monkeypatch):
    app, client, task_id, *_ = started
    monkeypatch.setenv("STUB_STATUS", str(tmp_path / "missing"))
    [session] = await (await client.get("/v1/tasks", headers=AUTH)).json()
    assert session["state"] == "unknown"


async def test_message_reaches_launcher(started, tmp_path):
    app, client, task_id, *_ = started
    body = {"text": "Also check the logs.", "requester": "@alice:example.org"}
    assert (await client.post(f"/v1/tasks/{task_id}/message", json=body, headers=AUTH)).status == 204
    assert fetches(tmp_path)[-1] == ["message", task_id, body]
    assert app[STORE].typed(task_id) is not None


async def test_default_blurbs_come_first_in_every_prompt(aiohttp_client, config, tmp_path):
    (Path(config["blurbs_dir"]) / "network.md").write_text("Reach internal services through the proxy on port 8443.\n")
    config["default_blurbs"] = ["network"]
    client = await aiohttp_client(make_app(config, poll=False))
    resp = await client.post("/v1/tasks", json=request(blurbs=["myapp", "network"]), headers=AUTH)
    assert resp.status == 201
    prompt = launched(tmp_path)[0]["prompt"]
    assert prompt.index("## Context: network") < prompt.index("## Context: myapp")
    assert prompt.count("## Context: network") == 1


def test_missing_default_blurb_is_a_config_error(config):
    config["default_blurbs"] = ["nope"]
    with pytest.raises(ValueError, match="default blurb 'nope' has no nope.md"):
        make_app(config, poll=False)


async def test_upload_reaches_launcher(started, tmp_path):
    app, client, task_id, *_ = started
    resp = await client.post(
        f"/v1/tasks/{task_id}/files", params={"name": "access.log", "requester": "@alice:example.org"},
        data=b"x" * 1000, headers=AUTH,
    )
    assert resp.status == 200
    assert (await resp.json())["path"] == "~/shared/uploads/access.log"
    assert fetches(tmp_path)[-1] == ["upload", task_id, {"name": "access.log", "requester": "@alice:example.org"}, 1000]


async def test_upload_limit_and_scoping(started, config, tmp_path):
    app, client, task_id, *_ = started
    config["max_upload"] = 10
    params = {"name": "big.bin", "requester": "@a:example.org"}
    assert (await client.post(f"/v1/tasks/{task_id}/files", params=params, data=b"x" * 11, headers=AUTH)).status == 413
    config["tokens"]["other"] = "0ther-0123456789abcdef0123456789abcdef"
    other = {"Authorization": "Bearer 0ther-0123456789abcdef0123456789abcdef"}
    assert (await client.post(f"/v1/tasks/{task_id}/files", params=params, data=b"x", headers=other)).status == 404
    assert (await client.post(f"/v1/tasks/{task_id}/files", params={"name": "x"}, data=b"x", headers=AUTH)).status == 400
    assert not any(f[0] == "upload" for f in fetches(tmp_path))


async def test_upload_bigger_than_the_default_request_size(started, tmp_path):
    app, client, task_id, *_ = started
    params = {"name": "big.bin", "requester": "@a:example.org"}
    resp = await client.post(f"/v1/tasks/{task_id}/files", params=params, data=b"x" * (1024 * 1024), headers=AUTH)
    assert resp.status == 200


async def test_model_and_effort_reach_the_job(client, tmp_path):
    resp = await client.post("/v1/tasks", json=request(model="opus", effort="xhigh"), headers=AUTH)
    assert resp.status == 201
    job = launched(tmp_path)[0]
    assert job["model"] == "opus" and job["effort"] == "xhigh"


@pytest.mark.parametrize("value", [32768, "32768"])
async def test_context_window_reaches_the_job(client, tmp_path, value):
    resp = await client.post("/v1/tasks", json=request(harness="cline", context_window=value), headers=AUTH)
    assert resp.status == 201
    assert launched(tmp_path)[0]["context_window"] == 32768


@pytest.mark.parametrize("value", ["lots", "１２３４", 100])
async def test_bad_context_window_is_refused(client, value):
    resp = await client.post("/v1/tasks", json=request(harness="cline", context_window=value), headers=AUTH)
    assert resp.status == 400 and "context window" in (await resp.json())["error"]


async def test_network_reaches_the_job_from_a_request_or_a_config_set(client, config, tmp_path):
    configs = Path(config["blurbs_dir"]).parent / "configs"
    configs.mkdir()
    (configs / "airgapped").write_text("network: sealed\n")
    assert (await client.post("/v1/tasks", json=request(network="named"), headers=AUTH)).status == 201
    source = {"kind": "matrix", "ref": "$event2", "channel": "!room:example.org"}
    assert (await client.post("/v1/tasks", json=request(configs=["airgapped"], source=source), headers=AUTH)).status == 201
    assert [job.get("network") for job in launched(tmp_path)] == ["named", "sealed"]
    resp = await client.post("/v1/tasks", json=request(network="open"), headers=AUTH)
    assert resp.status == 400 and "network" in (await resp.json())["error"]


async def test_a_preview_is_what_the_task_would_launch_without_launching_it(client, config, tmp_path):
    configs = Path(config["blurbs_dir"]).parent / "configs"
    (configs / "files").mkdir(parents=True)
    (configs / "files" / "token").write_text("secret")
    (configs / "recipes").write_text("harness: cline\ncontext_window: 30000\nblurb: myapp\nfile: token\nallow: example.org\n")
    body = request(task="", configs=["recipes"], backend="self-hosted", grants={"domains": ["pypi.org"]})
    resp = await client.post("/v1/tasks/preview", json=body, headers=AUTH)
    assert resp.status == 200, await resp.text()
    assert await resp.json() == {
        "name": None,
        "settings": {"harness": "cline", "backend": "self-hosted", "context_window": 30000},
        "from_configs": {"harness": "cline", "context_window": 30000},
        "domains": ["example.org", "pypi.org"], "internal_domains": [], "dependencies": [],
        # A file's name only: its contents may be a credential.
        "files": ["token"],
        "blurbs": [{"name": "myapp", "text": "Repo: example/myapp\n"}],
    }
    assert launched(tmp_path) == []
    assert client.server.app[STORE].open_tasks("maubot") == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
async def test_a_preview_doesn_t_read_config_set_files(client, config):
    configs = Path(config["blurbs_dir"]).parent / "configs"
    (configs / "files").mkdir(parents=True)
    (configs / "files" / "token").write_text("secret")
    (configs / "files" / "token").chmod(0)
    (configs / "tokened").write_text("file: token\n")
    body = request(configs=["tokened"])
    resp = await client.post("/v1/tasks/preview", json=body, headers=AUTH)
    assert resp.status == 200 and (await resp.json())["files"] == ["token"]
    resp = await client.post("/v1/tasks", json=body, headers=AUTH)
    assert resp.status == 400 and "couldn't read config set file 'token'" in (await resp.json())["error"]


async def test_a_preview_refuses_what_the_task_would_be_refused(client, config, tmp_path):
    resp = await client.post("/v1/tasks/preview", json=request(harness="claude-code", backend="openrouter"), headers=AUTH)
    assert resp.status == 400 and "can't use the openrouter backend" in (await resp.json())["error"]
    configs = Path(config["blurbs_dir"]).parent / "configs"
    configs.mkdir()
    (configs / "hosted").write_text("backend: openrouter\n")
    # No harness named, so the default's.
    resp = await client.post("/v1/tasks/preview", json=request(configs=["hosted"]), headers=AUTH)
    assert resp.status == 400 and "claude-code can't use the openrouter backend" in (await resp.json())["error"]
    resp = await client.post("/v1/tasks/preview", json=request(grants={"domains": ["example.net"]}), headers=AUTH)
    assert resp.status == 403
    assert (await client.post("/v1/tasks/preview", json=request())).status == 401


async def test_bad_effort_is_refused(client):
    resp = await client.post("/v1/tasks", json=request(effort="max"), headers=AUTH)
    assert resp.status == 400 and "effort" in (await resp.json())["error"]


@pytest.mark.parametrize("settings,refused", [
    ({"harness": "claude-code", "backend": "openrouter"}, "claude-code can't use the openrouter backend"),
    ({"backend": "self-hosted"}, "claude-code can't use the self-hosted backend"),
    ({"harness": "claude-code", "effort": "none"}, "claude-code has no effort level none"),
    ({"harness": "claude-code", "context_window": 32768}, "claude-code takes no context window"),
])
async def test_settings_the_harness_can_t_take_are_refused(client, tmp_path, settings, refused):
    resp = await client.post("/v1/tasks", json=request(**settings), headers=AUTH)
    assert resp.status == 400 and refused in (await resp.json())["error"]
    assert launched(tmp_path) == []


async def test_config_sets_that_disagree_are_refused_unless_the_request_settles_it(client, config, tmp_path):
    configs = Path(config["blurbs_dir"]).parent / "configs"
    configs.mkdir()
    (configs / "hosted").write_text("harness: cline\nbackend: openrouter\n")
    (configs / "local").write_text("harness: cline\nbackend: self-hosted\n")
    for path in ("/v1/tasks/preview", "/v1/tasks"):
        resp = await client.post(path, json=request(configs=["local", "hosted"]), headers=AUTH)
        assert resp.status == 400
        assert (await resp.json())["error"] == (
            "config sets disagree on backend: local gives self-hosted; hosted gives openrouter. Set backend for the task to choose"
        )
    assert launched(tmp_path) == []
    resp = await client.post("/v1/tasks", json=request(configs=["local", "hosted"], backend="openrouter"), headers=AUTH)
    assert resp.status == 201
    assert (launched(tmp_path)[0]["harness"], launched(tmp_path)[0]["backend"]) == ("cline", "openrouter")


async def test_a_config_set_s_harness_is_held_to_its_backends(client, config, tmp_path):
    configs = Path(config["blurbs_dir"]).parent / "configs"
    configs.mkdir()
    (configs / "hosted").write_text("backend: openrouter\n")
    resp = await client.post("/v1/tasks", json=request(harness="claude-code", configs=["hosted"]), headers=AUTH)
    assert resp.status == 400 and "can't use the openrouter backend" in (await resp.json())["error"]


async def test_the_launcher_s_defaults_are_checked(aiohttp_client, config, monkeypatch, tmp_path):
    monkeypatch.setenv("STUB_CATALOG", json.dumps({**CATALOG, "defaults": {"cline": {"backend": "self-hosted", "effort": "none"}}}))
    client = await aiohttp_client(make_app(config, poll=False))
    resp = await client.post("/v1/tasks", json=request(harness="cline"), headers=AUTH)
    assert resp.status == 201
    source = {"kind": "matrix", "ref": "$event2", "channel": "!room:example.org"}
    resp = await client.post("/v1/tasks", json=request(effort="none", source=source), headers=AUTH)
    assert resp.status == 400 and "claude-code has no effort level none" in (await resp.json())["error"]
    # Fetched once, and cached.
    assert [f for f in fetches(tmp_path) if f == ["catalog"]] == [["catalog"]]


async def test_without_a_catalog_the_launcher_checks(aiohttp_client, config, monkeypatch, tmp_path):
    monkeypatch.delenv("STUB_CATALOG")
    client = await aiohttp_client(make_app(config, poll=False))
    resp = await client.post("/v1/tasks", json=request(harness="claude-code", effort="none"), headers=AUTH)
    assert resp.status == 201 and launched(tmp_path)[0]["effort"] == "none"


async def test_a_failing_catalog_is_asked_for_once_at_a_time(aiohttp_client, config, monkeypatch, tmp_path):
    monkeypatch.delenv("STUB_CATALOG")
    client = await aiohttp_client(make_app(config, poll=False))

    def post(n):
        source = {"kind": "matrix", "ref": f"$event{n}", "channel": "!room:example.org"}
        return client.post("/v1/tasks", json=request(source=source), headers=AUTH)

    # Together they share one fetch, and those after it reuse its failure.
    responses = await asyncio.gather(*(post(n) for n in range(3)))
    responses += [await post(n) for n in range(3, 6)]
    assert [r.status for r in responses] == [201] * 6
    assert [f for f in fetches(tmp_path) if f == ["catalog"]] == [["catalog"]]


async def test_previews_share_a_failing_catalog(aiohttp_client, config, monkeypatch, tmp_path):
    monkeypatch.delenv("STUB_CATALOG")
    client = await aiohttp_client(make_app(config, poll=False))
    for _ in range(5):
        assert (await client.post("/v1/tasks/preview", json=request(), headers=AUTH)).status == 200
    assert [f for f in fetches(tmp_path) if f == ["catalog"]] == [["catalog"]]


async def test_the_catalog_says_what_each_harness_can_take(aiohttp_client, config, monkeypatch):
    monkeypatch.setenv("STUB_CATALOG", json.dumps({**CATALOG, "defaults": {"cline": {"backend": "self-hosted"}}}))
    config["catalog"] = {
        "claude-code": {
            "anthropic": {"models": ["claude-opus-5-5[1m]", {"id": "opus", "context_window": 200000}]},
            "openrouter": {"models": ["z-ai/glm-5.3"]},
        },
        "cline": {"self-hosted": {"models": [{"id": "qwen3.5-4b", "context_window": 120000}, "llama3:8b"]}},
    }
    client = await aiohttp_client(make_app(config, poll=False))
    catalog = await (await client.get("/v1/catalog", headers=AUTH)).json()
    assert catalog["harnesses"] == ["claude-code", "cline"] and catalog["default_harness"] == "claude-code"
    # Only the backends a harness can use, and context windows only for a harness that takes one.
    assert catalog["options"] == {
        "claude-code": {
            **OPTIONS["claude-code"], "defaults": {},
            "models": {"anthropic": [{"id": "claude-opus-5-5[1m]"}, {"id": "opus"}]},
        },
        "cline": {
            **OPTIONS["cline"], "defaults": {"backend": "self-hosted"},
            "models": {"self-hosted": [{"id": "qwen3.5-4b", "context_window": 120000}, {"id": "llama3:8b"}]},
        },
    }


@pytest.mark.parametrize("catalog,refused", [
    ({"cline": {"self-hosted": {"models": [{"id": "qwen", "context_window": 100}]}}}, "not a model"),
    ({"cline": {"self-hosted": {"models": [{"name": "qwen"}]}}}, "not a model"),
    ({"cline": {"self-hosted": {"model": ["qwen"]}}}, "must be a table with models"),
    ({"cline": ["qwen"]}, "must be a table of backends"),
])
def test_a_catalog_that_wouldn_t_work_is_refused(config, catalog, refused):
    config["catalog"] = catalog
    with pytest.raises(ValueError, match=refused):
        make_app(config, poll=False)


async def test_internal_grants_and_ports(client, tmp_path):
    grants = {"domains": ["PyPI.org:443"], "internal": ["recipes.example.com:8443"], "dependencies": []}
    resp = await client.post("/v1/tasks", json=request(grants=grants), headers=AUTH)
    assert resp.status == 201, await resp.text()
    job = launched(tmp_path)[0]
    assert job["domains"] == ["pypi.org", "recipes.example.com"]
    assert job["internal_domains"] == ["recipes.example.com"]


async def test_policy_covers_internal_grants(client):
    grants = {"internal": ["db.example.net"]}
    resp = await client.post("/v1/tasks", json=request(grants=grants), headers=AUTH)
    assert resp.status == 403


async def test_bad_port_is_not_stripped(client):
    resp = await client.post("/v1/tasks", json=request(grants={"domains": ["pypi.org:99999"]}), headers=AUTH)
    assert resp.status == 400 and "not a valid domain" in (await resp.json())["error"]


async def test_config_sets_expand_and_the_request_overrides(aiohttp_client, config, tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "mealie").write_text(
        "ctx: myapp\nharness: cline\nbackend: self-hosted\nmodel: qwen3-14b\ninternal: recipes.example.com:8443\n"
    )
    config["configs_dir"] = str(configs)
    client = await aiohttp_client(make_app(config, poll=False))
    body = request(configs=["mealie"], model="qwen3-32b", grants={"domains": ["allrecipes.example.org"]})
    resp = await client.post("/v1/tasks", json=body, headers=AUTH)
    assert resp.status == 201, await resp.text()
    job = launched(tmp_path)[0]
    assert job["harness"] == "cline" and job["backend"] == "self-hosted" and job["model"] == "qwen3-32b"
    assert job["domains"] == ["allrecipes.example.org", "recipes.example.com"]
    assert job["internal_domains"] == ["recipes.example.com"]
    assert "## Context: myapp" in job["prompt"]
    assert await (await client.get("/v1/configs", headers=AUTH)).json() == ["mealie"]
    (configs / "files").mkdir()
    (configs / "files" / "token").write_text("s3cret-0123456789abcdef0123456789abcdef")
    (configs / "mealie").write_text((configs / "mealie").read_text() + "file: token\ncontext_window: 30000\n")
    described = await (await client.get("/v1/configs/mealie", headers=AUTH)).json()
    assert described == {
        # Written with the earlier ctx:, described with blurb:.
        "blurb": ["myapp"], "internal": ["recipes.example.com:8443"], "file": ["token"],
        "harness": "cline", "backend": "self-hosted", "model": "qwen3-14b", "context_window": "30000",
    }
    (configs / "mealie").write_text("# Recipes, from Mealie\n#   on the home server\n\n# not this\n" + (configs / "mealie").read_text())
    described = await (await client.get("/v1/configs/mealie", headers=AUTH)).json()
    assert described["description"] == "Recipes, from Mealie on the home server" and described["harness"] == "cline"
    assert (await client.get("/v1/configs/nope", headers=AUTH)).status == 404
    (Path(config["blurbs_dir"]) / "mealie.md").write_text("Recipes live in Mealie.\n")
    assert await (await client.get("/v1/blurbs/mealie", headers=AUTH)).json() == "Recipes live in Mealie.\n"
    assert (await client.get("/v1/blurbs/nope", headers=AUTH)).status == 404
    assert (await client.get("/v1/blurbs/..%2Fapi", headers=AUTH)).status == 404
    assert (await client.get("/v1/blurbs/mealie")).status == 401
    assert (await client.get("/v1/configs/mealie")).status == 401
    (configs / "mealie").write_text("bogus line\n")
    resp = await client.get("/v1/configs/mealie", headers=AUTH)
    assert resp.status == 400 and str(configs) not in (await resp.json())["error"]


async def test_unknown_config_set_is_refused(client):
    resp = await client.post("/v1/tasks", json=request(configs=["nope"]), headers=AUTH)
    assert resp.status == 400 and "unknown config set 'nope' (available: none)" in (await resp.json())["error"]


async def test_config_set_grants_go_through_policy(aiohttp_client, config, tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "bad").write_text("allow: api.example.net\n")
    config["configs_dir"] = str(configs)
    client = await aiohttp_client(make_app(config, poll=False))
    assert (await client.post("/v1/tasks", json=request(configs=["bad"]), headers=AUTH)).status == 403


async def test_config_set_files_reach_the_job_but_not_the_database(aiohttp_client, config, tmp_path):
    import base64
    import sqlite3

    configs = tmp_path / "configs"
    (configs / "files").mkdir(parents=True)
    (configs / "files" / "mealie-token").write_text("s3cret-0123456789abcdef0123456789abcdef-token")
    (configs / "mealie").write_text("file: mealie-token\n")
    config["configs_dir"] = str(configs)
    client = await aiohttp_client(make_app(config, poll=False))
    resp = await client.post("/v1/tasks", json=request(configs=["mealie"]), headers=AUTH)
    assert resp.status == 201, await resp.text()
    job = launched(tmp_path)[0]
    assert job["files"] == [{"name": "mealie-token", "data": base64.b64encode(b"s3cret-0123456789abcdef0123456789abcdef-token").decode()}]
    assert "- ~/shared/uploads/mealie-token" in job["prompt"]
    stored = sqlite3.connect(config["db_path"]).execute("SELECT job_json FROM tasks").fetchone()[0]
    assert json.loads(stored)["files"] == ["mealie-token"] and "s3cret-0123456789abcdef0123456789abcdef" not in stored and base64.b64encode(b"s3cret-0123456789abcdef0123456789abcdef-token").decode() not in stored


async def test_missing_config_set_file_is_refused(aiohttp_client, config, tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "mealie").write_text("file: mealie-token\n")
    config["configs_dir"] = str(configs)
    client = await aiohttp_client(make_app(config, poll=False))
    resp = await client.post("/v1/tasks", json=request(configs=["mealie"]), headers=AUTH)
    assert resp.status == 400 and "mealie-token" in (await resp.json())["error"]


@pytest.fixture
def waiting_store(tmp_path):
    from darner.api import DEFAULTS

    store = Store(str(tmp_path / "w.db"))
    store.claim("dn-aaaaa", "maubot", {"kind": "matrix", "ref": "$e"}, "@u:example.org", "{}")
    store.mark("dn-aaaaa", "launched")
    return store, dict(DEFAULTS)


def waiting_events(store):
    return [e for e in store.events("maubot") if e.type == "waiting"]


def quiet_task(**overrides):
    return {"id": "dn-aaaaa", "state": "running", "quiet": 400, "screen": "Do you want to continue? (y/n)", **overrides}


def test_quiet_session_is_reported_once_with_its_screen(waiting_store):
    from darner.api import check_waiting

    store, config = waiting_store
    check_waiting(config, store, quiet_task())
    check_waiting(config, store, quiet_task(quiet=460))
    [event] = waiting_events(store)
    assert event.summary == "Do you want to continue? (y/n)" and event.data == {"quiet": 400}


def test_screen_can_be_left_out(waiting_store):
    from darner.api import check_waiting

    store, config = waiting_store
    config["waiting_screen"] = False
    check_waiting(config, store, quiet_task())
    assert waiting_events(store)[0].summary == ""


@pytest.mark.parametrize("overrides", [
    {"quiet": 100}, {"state": "exited"}, {"quiet": None},
    {"awaiting_answer": True}, {"domain_request": 2}, {"ready": True}, {"unsignalled": True}, {"result_pending": True},
])
def test_not_reported_while_waiting_on_the_requester_or_done(waiting_store, overrides):
    from darner.api import check_waiting

    store, config = waiting_store
    check_waiting(config, store, quiet_task(**overrides))
    assert waiting_events(store) == []


def test_not_reported_after_output_since_the_screen_last_changed(waiting_store):
    from darner.api import check_waiting

    store, config = waiting_store
    store.add_event("dn-aaaaa", "output", "done")
    check_waiting(config, store, quiet_task())
    assert waiting_events(store) == []


def test_finished_session_is_not_reported_however_long_ago(waiting_store):
    import time

    from darner.api import check_waiting

    store, config = waiting_store
    store.add_event("dn-aaaaa", "output", "done")
    store.db.execute("UPDATE events SET created = ?", (time.time() - 1000,))
    check_waiting(config, store, quiet_task())
    assert waiting_events(store) == []


def test_reported_again_after_being_told_something_since_output(waiting_store):
    import time

    from darner.api import check_waiting

    store, config = waiting_store
    store.add_event("dn-aaaaa", "output", "done")
    store.db.execute("UPDATE events SET created = ?", (time.time() - 1000,))
    store.set_typed("dn-aaaaa")
    check_waiting(config, store, quiet_task())
    assert len(waiting_events(store)) == 1


def test_a_new_quiet_spell_is_reported_again(waiting_store):
    import time

    from darner.api import check_waiting

    store, config = waiting_store
    check_waiting(config, store, quiet_task())
    store.db.execute("UPDATE events SET created = ?", (time.time() - 1000,))
    check_waiting(config, store, quiet_task(quiet=320))
    assert len(waiting_events(store)) == 2


@pytest.mark.parametrize("tokens, message", [
    ({"maubot": "replace-with-a-long-random-token"}, "the token for maubot must be at least 32 characters"),
    ({"maubot": "short"}, "openssl rand -hex 32"),
    ({"maubot": 12345678901234567890123456789012345}, "the token for maubot"),
    ({"maubot": "a" * 32, "web": "a" * 32}, "each frontend needs its own token"),
    (["a" * 32], "tokens must be a table"),
])
def test_weak_tokens_are_refused(config, tokens, message):
    config["tokens"] = tokens
    with pytest.raises(ValueError, match=message):
        make_app(config, poll=False)


def test_settings_defaults_log_and_unknown_keys(config):
    from darner.api import with_defaults

    config["configz_dir"] = "/typo"
    full, lines, unknown = with_defaults(config)
    assert full["configs_dir"] == str(Path(config["blurbs_dir"]).parent / "configs")
    assert full["waiting_after"] == 300 and full["waiting_screen"] is True
    assert unknown == ["configz_dir"]
    assert "waiting_after = 300 (default)" in lines and "tokens = 'for maubot'" in lines
    assert not any("s3cret-0123456789abcdef0123456789abcdef" in line for line in lines)
    assert any(line.startswith("blurbs_dir = ") and not line.endswith("(default)") for line in lines)


async def test_selftest_sends_hostile_text_as_a_finished_task(client):
    body = {"requester": "@u:example.org", "source": {"kind": "matrix", "ref": "$st", "channel": "!r:example.org"}}
    resp = await client.post("/v1/selftest", json=body, headers=AUTH)
    assert resp.status == 201
    task_id = (await resp.json())["id"]
    events = [e for e in await (await client.get("/v1/events", headers=AUTH)).json() if e["id"] == task_id]
    assert [e["type"] for e in events] == ["output", "help", "waiting", "ended"]
    for e in events[:3]:
        assert "<script>" in e["summary"] and "Check 11, the end." in e["summary"]
        assert "\u200b" not in e["summary"] and "\u202e" not in e["summary"] and "\x07" not in e["summary"]
    assert events[0]["size"] == 0 and events[1]["answerable"] is False
    assert await (await client.get("/v1/tasks", headers=AUTH)).json() == []
    again = await client.post("/v1/selftest", json=body, headers=AUTH)
    assert again.status == 200 and (await again.json())["id"] == task_id
    assert (await client.post("/v1/selftest", json=body)).status == 401
    assert (await client.post("/v1/selftest", json={"requester": "x"}, headers=AUTH)).status == 400


def age_task(app, task_id, seconds):
    app[STORE].db.execute("UPDATE tasks SET created = ? WHERE id = ?", (time.time() - seconds, task_id))


async def test_old_sessions_are_stopped_once(started, tmp_path):
    app, client, task_id, status, _ = started
    status(ready=False)
    await poll_once(app)
    assert ["stop", task_id] not in fetches(tmp_path)
    age_task(app, task_id, 86401)
    await poll_once(app)
    await poll_once(app)
    assert fetches(tmp_path).count(["stop", task_id]) == 1
    [expired] = [e for e in await events(client) if e["type"] == "expired"]
    assert expired["id"] == task_id and expired["data"]["age"] >= 86400


async def test_session_age_limit_can_be_turned_off(started, config, tmp_path):
    app, client, task_id, status, _ = started
    config["max_session_age"] = 0
    status(ready=False)
    age_task(app, task_id, 10**7)
    await poll_once(app)
    assert ["stop", task_id] not in fetches(tmp_path)


async def test_a_failed_stop_is_retried_and_logged_once(started, tmp_path, monkeypatch, caplog):
    app, client, task_id, status, _ = started
    status(ready=False)
    age_task(app, task_id, 86401)
    monkeypatch.setenv("STUB_STOP_ERROR", "docker isn't answering")
    await poll_once(app)
    await poll_once(app)
    assert fetches(tmp_path).count(["stop", task_id]) == 2
    assert caplog.text.count("couldn't stop") == 1
    assert not [e for e in await events(client) if e["type"] == "expired"]


async def test_a_task_the_launcher_forgot_is_ended_once_old(started, tmp_path, monkeypatch):
    app, client, task_id, status, _ = started
    path = tmp_path / "status.json"
    path.write_text("[]")
    monkeypatch.setenv("STUB_STATUS", str(path))
    await poll_once(app)
    assert app[STORE].launched() == [task_id]
    age_task(app, task_id, 86401)
    await poll_once(app)
    assert app[STORE].launched() == []
    assert [e["type"] for e in await events(client) if e["id"] == task_id] == ["ended"]


async def test_ending_deletes_all_but_uncollected_output_then_the_rest(started, config, tmp_path):
    app, client, task_id, status, output = started
    status(state="exited")
    output(zip_bytes([("RESULT.md", "done")]))
    await poll_once(app)
    assert ["forget-delivered", task_id] in [f[:2] for f in fetches(tmp_path)]
    job, requester = app[STORE].db.execute("SELECT job_json, requester FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert job == "{}" and requester == ""
    results = Path(config["results_dir"]) / task_id
    assert len(list(results.glob("*.zip"))) == 1
    for e in await events(client):
        assert (await client.post(f"/v1/events/{e['seq']}/ack", headers=AUTH)).status == 204
    assert not app[STORE].known(task_id) and not results.exists()


async def test_uncollected_output_is_purged_after_output_retention(started, config):
    app, client, task_id, status, output = started
    status(state="exited")
    output(zip_bytes([("RESULT.md", "done")]))
    await poll_once(app)
    app[STORE].db.execute("UPDATE tasks SET ended = ?", (time.time() - config["output_retention"] - 1,))
    await poll_once(app)
    assert not app[STORE].known(task_id) and await events(client) == []


async def test_a_failed_launch_leaves_nothing(client, config, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_FAIL", "1")
    assert (await client.post("/v1/tasks", json=request(), headers=AUTH)).status == 502
    assert Store(config["db_path"]).db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


async def test_an_ended_session_the_api_never_recorded_is_forgotten(started, tmp_path):
    app, client, task_id, status, _ = started
    status(state="running", ready=False)
    await poll_once(app)
    assert ["forget", "dn-zzzzz"] in fetches(tmp_path) and ["forget", task_id] not in fetches(tmp_path)


def test_a_session_stuck_starting_is_reported_waiting(waiting_store):
    from darner.api import check_waiting

    store, config = waiting_store
    check_waiting(config, store, quiet_task(state="starting", screen="Acknowledge these findings? [y/N]"))
    assert waiting_events(store)[0].summary == "Acknowledge these findings? [y/N]"


async def test_a_session_stuck_starting_is_stopped_after_max_session_age(started, tmp_path):
    app, client, task_id, status, _ = started
    status(state="starting", ready=False)
    age_task(app, task_id, 86401)
    await poll_once(app)
    assert ["stop", task_id] in fetches(tmp_path)


async def test_output_that_can_never_be_fetched_ends_the_task_after_max_session_age(started, tmp_path, monkeypatch):
    app, client, task_id, status, _ = started
    status(state="exited")
    monkeypatch.setenv("STUB_FETCH_CODE", "1")
    await poll_once(app)
    assert app[STORE].launched() == [task_id]
    age_task(app, task_id, 86401)
    # A few polls' grace, for a session expire stopped, whose last output is still to come.
    for _ in range(COLLECT_TRIES - 1):
        await poll_once(app)
    assert app[STORE].launched() == [task_id]
    await poll_once(app)
    assert app[STORE].launched() == []
    [ended] = [e for e in await events(client) if e["id"] == task_id]
    assert ended["type"] == "ended" and ended["summary"] == "its output couldn't be collected"
    assert ["forget", task_id] in fetches(tmp_path)


async def test_a_task_id_in_use_is_not_reused(client, tmp_path, monkeypatch):
    import darner.api

    ids = iter(["dn-aaaaa", "dn-aaaaa", "dn-bbbbb"])
    monkeypatch.setattr(darner.api, "new_task_id", lambda: next(ids))
    first = await client.post("/v1/tasks", json=request(), headers=AUTH)
    second = await client.post("/v1/tasks", json=request(source={"kind": "matrix", "ref": "$other"}), headers=AUTH)
    assert (await first.json())["id"] == "dn-aaaaa" and (await second.json())["id"] == "dn-bbbbb"


async def test_every_action_is_audited_without_its_content(started, monkeypatch, caplog):
    import logging

    app, client, task_id, status, _ = started
    caplog.set_level(logging.INFO, logger="darner.audit")
    status(ready=False)
    body = {"text": "the password is hunter2", "requester": "@alice:example.org"}
    assert (await client.post(f"/v1/tasks/{task_id}/message", json=body, headers=AUTH)).status == 204
    resp = await client.post(f"/v1/tasks/{task_id}/files?name=notes.txt&requester=%40alice%3Aexample.org", data=b"secret", headers=AUTH)
    assert resp.status == 200
    assert (await client.post(f"/v1/tasks/{task_id}/stop", json=STOP_BODY, headers=AUTH)).status == 204
    lines = [r.getMessage() for r in caplog.records if r.name == "darner.audit"]
    assert lines == [
        f"message {task_id} by '@alice:example.org' via maubot length=23",
        f"upload {task_id} by '@alice:example.org' via maubot name='notes.txt' size=6",
        f"stop {task_id} by '@alice:example.org' via maubot",
    ]
    assert "hunter2" not in caplog.text and "secret" not in caplog.text


async def test_stop_needs_a_requester(started):
    app, client, task_id, *_ = started
    assert (await client.post(f"/v1/tasks/{task_id}/stop", headers=AUTH)).status == 400


async def test_a_start_is_audited_with_its_grants(client, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="darner.audit")
    task_id = (await (await client.post("/v1/tasks", json=request(), headers=AUTH)).json())["id"]
    [line] = [r.getMessage() for r in caplog.records if r.name == "darner.audit"]
    assert line == (
        f"start {task_id} by '@u:example.org' via maubot domains=['pypi.org'] internal=[] dependencies=['pip:pytest'] files=[]"
    )


async def test_domain_decisions_are_audited(requested, caplog):
    import logging

    app, client, task_id = requested
    caplog.set_level(logging.INFO, logger="darner.audit")
    await client.post(f"/v1/tasks/{task_id}/domains/3/allow", json={"requester": "@alice:example.org"}, headers=AUTH)
    await client.post(f"/v1/tasks/{task_id}/domains/3/deny", json={"requester": "@bob:example.org"}, headers=AUTH)
    lines = [r.getMessage() for r in caplog.records if r.name == "darner.audit"]
    assert lines[0] == f"allow {task_id} by '@alice:example.org' via maubot request=3 granted=['pypi.org'] failed=[]"
    assert lines[1].startswith(f"deny {task_id} by '@bob:example.org' via maubot request=3 domains=[")


@pytest.mark.parametrize("policy, grants, message", [
    ({"allow_internal": False}, {"domains": [], "internal": ["recipes.example.com"]}, "domain not permitted: recipes.example.com (internal)"),
    ({"max_internal_domains": 1}, {"internal": ["a.example.com", "b.example.com"]}, "at most 1 internal domains"),
    ({"allow_domains": ["example.org"]}, {"domains": ["pypi.org"]}, "domain not permitted: pypi.org"),
    ({}, {"dependencies": ["tarball:tool@" + "0" * 64 + ":https://example.org/t.tgz"]}, "dependency kind not permitted: tarball"),
    ({"allow_dependency_kinds": ["apt"]}, {"dependencies": ["pip:pytest"]}, "dependency kind not permitted: pip"),
])
async def test_policy_covers_internal_grants_dependencies_and_an_allowlist(aiohttp_client, config, policy, grants, message):
    config["policy"] = policy
    client = await aiohttp_client(make_app(config, poll=False))
    resp = await client.post("/v1/tasks", json=request(grants={"domains": [], "dependencies": [], **grants}), headers=AUTH)
    assert resp.status == 403 and message in (await resp.json())["error"]


async def test_an_allowlist_and_listed_dependency_kinds_permit(aiohttp_client, config):
    config["policy"] = {"allow_domains": ["example.org"], "allow_dependency_kinds": ["pip", "tarball"]}
    client = await aiohttp_client(make_app(config, poll=False))
    grants = {"domains": ["docs.example.org"], "dependencies": ["tarball:tool@" + "0" * 64 + ":https://example.org/t.tgz"]}
    assert (await client.post("/v1/tasks", json=request(grants=grants), headers=AUTH)).status == 201


@pytest.mark.parametrize("policy, message", [
    ({"deny_domain": ["x"]}, "unknown policy setting 'deny_domain'"),
    ({"allow_internal": "no"}, "allow_internal has the wrong type"),
    ({"allow_dependency_kinds": ["brew"]}, "unknown dependency kinds"),
])
def test_a_policy_that_wouldn_t_work_stops_the_api(config, policy, message):
    config["policy"] = policy
    with pytest.raises(ValueError, match=message):
        make_app(config, poll=False)


async def test_an_internal_domain_request_is_marked_denied_when_internal_grants_are_off(started, config, tmp_path, monkeypatch):
    app, client, task_id, *_ = started
    config["policy"] = {"allow_internal": False}
    path = tmp_path / "status.json"
    path.write_text(json.dumps([{"id": task_id, "state": "running", "ready": False, "domains": True}]))
    monkeypatch.setenv("STUB_STATUS", str(path))
    monkeypatch.setenv("STUB_DOMAINS", json.dumps({"request": 1, "domains": [
        {"domain": "recipes.example.com", "internal": True}, {"domain": "recipes.example.com", "internal": False},
    ]}))
    await poll_once(app)
    [event] = [e for e in await events(client) if e["type"] == "domains"]
    assert [(d["internal"], d["denied"]) for d in event["data"]["domains"]] == [(True, True), (False, False)]
    await client.post(f"/v1/tasks/{task_id}/domains/1/allow", json={"requester": "@a:example.org"}, headers=AUTH)
    # The launcher matches whole entries, so the plain one passing doesn't pass the internal one.
    assert fetches(tmp_path)[-1][3]["domains"] == [{"domain": "recipes.example.com", "internal": False, "port": None}]


@pytest.mark.parametrize("requester", ["@alice:example.org\nforged line", "@room\u202e", "a\u2028b", "a\u2029b", "x" * 256, "  ", 7])
async def test_a_requester_that_isn_t_a_plain_label_is_refused(started, requester):
    app, client, task_id, *_ = started
    body = {"text": "hi", "requester": requester}
    resp = await client.post(f"/v1/tasks/{task_id}/message", json=body, headers=AUTH)
    assert resp.status == 400 and "requester must be" in (await resp.json())["error"]
    resp = await client.post("/v1/tasks", json=request(requester=requester, source={"kind": "matrix", "ref": "$r"}), headers=AUTH)
    assert resp.status == 400


async def test_a_task_s_events_so_far_are_read_without_acknowledging_them(started, config):
    app, client, task_id, status, output = started
    status(state="running")
    output(zip_bytes([("RESULT.md", "partial")]))
    await poll_once(app)
    first = await (await client.get(f"/v1/tasks/{task_id}/events", headers=AUTH)).json()
    assert [e["type"] for e in first] == ["output"] and first[0]["id"] == task_id
    assert [e["seq"] for e in await events(client)] == [first[0]["seq"]]
    await client.post(f"/v1/events/{first[0]['seq']}/ack", headers=AUTH)
    assert [e["type"] for e in await (await client.get(f"/v1/tasks/{task_id}/events", headers=AUTH)).json()] == ["output"]
    config["tokens"]["other"] = "0ther-0123456789abcdef0123456789abcdef"
    other = {"Authorization": "Bearer 0ther-0123456789abcdef0123456789abcdef"}
    assert (await client.get(f"/v1/tasks/{task_id}/events", headers=other)).status == 404


async def test_session_listings_share_a_recent_launcher_status(started, tmp_path, monkeypatch):
    import asyncio

    app, client, task_id, status, _ = started
    status(ready=False)
    count = tmp_path / "status-count"
    monkeypatch.setenv("STUB_STATUS_COUNT", str(count))
    await asyncio.gather(*(client.get("/v1/tasks", headers=AUTH) for _ in range(3)))
    assert (await client.get(f"/v1/tasks/{task_id}", headers=AUTH)).status == 200
    assert count.read_text().count("status") == 1
    await poll_once(app)
    assert count.read_text().count("status") == 2


async def test_an_outage_costs_one_status_attempt_at_a_time(started, monkeypatch):
    import asyncio

    import darner.api
    from darner.api import STATUS_MAX_AGE, launcher_status

    app, *_ = started
    app[STATUS_CACHE].clear()
    attempts = []

    async def hanging(config, command, stdin=None):
        attempts.append(command)
        await asyncio.sleep(0.2)
        raise RuntimeError("timed out")

    monkeypatch.setattr(darner.api, "run_remote", hanging)
    results = await asyncio.gather(*(launcher_status(app, STATUS_MAX_AGE) for _ in range(5)), return_exceptions=True)
    assert len(attempts) == 1 and all(isinstance(r, RuntimeError) for r in results)
    # A listing in the failure's window gets it again without another attempt.
    with pytest.raises(RuntimeError):
        await launcher_status(app, STATUS_MAX_AGE)
    assert len(attempts) == 1
    # The poller doesn't take a cached failure: it asks again, and a listing
    # arriving meanwhile shares its attempt rather than queueing behind it.
    poller = asyncio.ensure_future(launcher_status(app, 0))
    await asyncio.sleep(0.05)
    app[STATUS_CACHE].pop("at")
    listing = asyncio.ensure_future(launcher_status(app, STATUS_MAX_AGE))
    await asyncio.gather(poller, listing, return_exceptions=True)
    assert len(attempts) == 2


async def test_a_listing_that_goes_away_doesn_t_cancel_the_shared_status(started, monkeypatch):
    import asyncio

    import darner.api
    from darner.api import STATUS_MAX_AGE, launcher_status

    app, *_ = started
    app[STATUS_CACHE].clear()

    async def slow(config, command, stdin=None):
        await asyncio.sleep(0.2)
        return 0, "[]", ""

    monkeypatch.setattr(darner.api, "run_remote", slow)
    first = asyncio.ensure_future(launcher_status(app, STATUS_MAX_AGE))
    second = asyncio.ensure_future(launcher_status(app, STATUS_MAX_AGE))
    await asyncio.sleep(0.05)
    first.cancel()
    assert await second == (0, "[]", "")


async def test_a_cancelled_status_call_doesn_t_stick(started, monkeypatch):
    import asyncio

    import darner.api
    from darner.api import STATUS_MAX_AGE, launcher_status

    app, *_ = started
    app[STATUS_CACHE].clear()

    async def slow(config, command, stdin=None):
        await asyncio.sleep(10)

    monkeypatch.setattr(darner.api, "run_remote", slow)
    waiter = asyncio.ensure_future(launcher_status(app, STATUS_MAX_AGE))
    await asyncio.sleep(0.05)
    app[STATUS_CACHE]["inflight"].cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    async def working(config, command, stdin=None):
        return 0, "[]", ""

    monkeypatch.setattr(darner.api, "run_remote", working)
    assert await launcher_status(app, 0) == (0, "[]", "")
    assert await launcher_status(app, STATUS_MAX_AGE) == (0, "[]", "")


def forgets(tmp_path, task_id):
    return [f[0] for f in fetches(tmp_path) if f[0] in ("forget", "forget-delivered") and f[1] == task_id]


async def test_a_clean_delivery_asks_for_the_share_to_be_pruned(started, tmp_path):
    app, client, task_id, status, output = started
    status(state="exited")
    output(zip_bytes([("RESULT.md", "done")]))
    await poll_once(app)
    assert forgets(tmp_path, task_id) == ["forget-delivered"]


async def test_a_delivery_before_the_session_exited_still_counts(started, tmp_path):
    app, client, task_id, status, output = started
    status(state="running")
    output(zip_bytes([("RESULT.md", "done")]))
    await poll_once(app)
    status(state="exited", ready=False)
    await poll_once(app)
    assert forgets(tmp_path, task_id) == ["forget-delivered"]


async def test_no_output_keeps_the_share(started, tmp_path):
    app, client, task_id, status, _ = started
    status(state="exited", ready=False)
    await poll_once(app)
    assert forgets(tmp_path, task_id) == ["forget"]


async def test_output_the_launcher_rejected_keeps_the_share(started, tmp_path, monkeypatch):
    app, client, task_id, status, _ = started
    status(state="exited")
    monkeypatch.setenv("STUB_FETCH_CODE", "3")
    await poll_once(app)
    assert forgets(tmp_path, task_id) == ["forget"]


async def test_output_the_api_rejected_keeps_the_share(started, tmp_path):
    app, client, task_id, status, output = started
    status(state="exited")
    output(b"not a zip at all")
    await poll_once(app)
    [event] = [e for e in await events(client) if e["type"] == "output"]
    assert event["size"] == 0 and forgets(tmp_path, task_id) == ["forget"]


async def test_an_unreadable_share_keeps_the_share(started, tmp_path, monkeypatch):
    app, client, task_id, *_ = started
    path = tmp_path / "status.json"
    path.write_text(json.dumps([{"id": task_id, "state": "exited", "ready": False, "error": "permission denied"}]))
    monkeypatch.setenv("STUB_STATUS", str(path))
    await poll_once(app)
    assert forgets(tmp_path, task_id) == ["forget"]


async def test_output_that_could_never_be_fetched_keeps_the_share(started, tmp_path, monkeypatch):
    app, client, task_id, status, _ = started
    status(state="exited")
    monkeypatch.setenv("STUB_FETCH_CODE", "1")
    age_task(app, task_id, 86401)
    for _ in range(COLLECT_TRIES):
        await poll_once(app)
    assert forgets(tmp_path, task_id) == ["forget"]


async def test_a_session_this_api_never_recorded_keeps_its_share(started, tmp_path):
    app, client, task_id, status, _ = started
    status(state="running", ready=False)
    await poll_once(app)
    assert forgets(tmp_path, "dn-zzzzz") == ["forget"]


async def test_a_failed_forget_of_a_delivered_share_is_retried_with_its_zip(started, tmp_path, monkeypatch):
    app, client, task_id, status, output = started
    status(state="exited")
    output(zip_bytes([("RESULT.md", "done")]))
    monkeypatch.setenv("STUB_FORGET_ERROR", "host unreachable")
    await poll_once(app)
    monkeypatch.delenv("STUB_FORGET_ERROR")
    status(state="exited", ready=False)
    await poll_once(app)
    assert forgets(tmp_path, task_id) == ["forget-delivered", "forget-delivered"]


async def test_a_collection_that_always_fails_still_expires(started, tmp_path, monkeypatch):
    import darner.api

    app, client, task_id, status, _ = started
    status(state="exited")

    async def broken(*args, **kwargs):
        raise ValueError("malformed reply")

    monkeypatch.setattr(darner.api, "collect", broken)
    age_task(app, task_id, 86401)
    for _ in range(COLLECT_TRIES):
        await poll_once(app)
    assert app[STORE].task_status("maubot", task_id) == "ended"


async def test_forget_delivered_names_the_zip_the_api_stored(started, tmp_path):
    import hashlib

    app, client, task_id, status, output = started
    sent = zip_bytes([("RESULT.md", "done")])
    status(state="exited")
    output(sent)
    await poll_once(app)
    assert ["forget-delivered", task_id, hashlib.sha256(sent).hexdigest()] in fetches(tmp_path)


async def test_a_later_fetch_the_api_lost_doesn_t_change_which_zip_it_names(started, tmp_path, monkeypatch):
    import hashlib

    app, client, task_id, status, output = started
    first = zip_bytes([("RESULT.md", "first")])
    status(state="running")
    output(first)
    await poll_once(app)
    # The launcher sent a second zip, but the API never stored it.
    monkeypatch.setenv("STUB_FETCH_CODE", "1")
    await poll_once(app)
    monkeypatch.delenv("STUB_FETCH_CODE")
    status(state="exited", ready=False)
    await poll_once(app)
    assert ["forget-delivered", task_id, hashlib.sha256(first).hexdigest()] in fetches(tmp_path)


async def test_a_named_session_is_found_by_its_name(client, tmp_path, config):
    resp = await client.post("/v1/tasks", json=request(name="DN-Recipes"), headers=AUTH)
    task_id = (await resp.json())["id"]
    session = await (await client.get("/v1/tasks/dn-recipes", headers=AUTH)).json()
    assert session["id"] == task_id and session["name"] == "recipes"
    assert (await client.get("/v1/tasks/dn-recipes/events", headers=AUTH)).status == 200
    # Only to the client that named it, and nothing but the id reaches the launcher.
    config["tokens"]["other"] = "0ther-0123456789abcdef0123456789abcdef"
    other = {"Authorization": "Bearer 0ther-0123456789abcdef0123456789abcdef"}
    assert (await client.get("/v1/tasks/dn-recipes", headers=other)).status == 404
    assert (await client.post("/v1/tasks/dn-recipes/stop", json={"requester": "@a:example.org"}, headers=AUTH)).status == 204
    assert ["stop", task_id] in fetches(tmp_path)


async def test_an_open_session_s_name_can_t_be_reused_until_it_ends(client, tmp_path):
    first = await (await client.post("/v1/tasks", json=request(name="recipes"), headers=AUTH)).json()
    source = {"kind": "matrix", "ref": "$event2", "channel": "!room:example.org"}
    for path in ("/v1/tasks/preview", "/v1/tasks"):
        resp = await client.post(path, json=request(name="recipes", source=source), headers=AUTH)
        assert resp.status == 409 and (await resp.json())["error"] == "dn-recipes is already the name of an open session"
    assert len(launched(tmp_path)) == 1
    # Ended, it holds its name until it's purged, so a command repeated later can't reach a new session.
    store = client.server.app[STORE]
    store.end(first["id"])
    resp = await client.post("/v1/tasks", json=request(name="recipes", source=source), headers=AUTH)
    assert resp.status == 409 and (await resp.json())["error"] == (
        f"dn-recipes is still held by {first['id']}, which has ended; clear it to free the name"
    )
    body = {"text": "deploy it", "requester": "@a:example.org"}
    resp = await client.post("/v1/tasks/dn-recipes/message", json=body, headers=AUTH)
    assert resp.status == 409 and (await resp.json())["error"] == f"{first['id']} has ended"
    resp = await client.post("/v1/tasks/dn-recipes/files?name=x&requester=a", data=b"secret", headers=AUTH)
    assert resp.status == 409
    assert not any(f[0] in ("message", "upload") for f in fetches(tmp_path))
    # Cleared, the name is free.
    store.delete(first["id"])
    resp = await client.post("/v1/tasks", json=request(name="recipes", source=source), headers=AUTH)
    assert resp.status == 201 and (await resp.json())["name"] == "recipes"


@pytest.mark.parametrize("name", ["abcde", "dn-k3x7q", "Has Space", "-lead", "a" * 33, "dn-", 7])
async def test_a_name_that_isn_t_one_is_refused(client, tmp_path, name):
    resp = await client.post("/v1/tasks", json=request(name=name), headers=AUTH)
    assert resp.status == 400 and "a session name is" in (await resp.json())["error"]
    assert launched(tmp_path) == []


async def test_a_check_polls_now_and_answers_the_session(started, tmp_path, monkeypatch):
    app, client, task_id, status, *_ = started
    status(ready=False)
    count = tmp_path / "status-count"
    monkeypatch.setenv("STUB_STATUS_COUNT", str(count))
    resp = await client.post(f"/v1/tasks/{task_id}/check", headers=AUTH)
    assert resp.status == 200 and (await resp.json())["id"] == task_id
    assert count.read_text().count("status") >= 1
    # Once every CHECK_INTERVAL for each client.
    resp = await client.post(f"/v1/tasks/{task_id}/check", headers=AUTH)
    assert resp.status == 429 and "try again in" in (await resp.json())["error"] and resp.headers["Retry-After"]
    assert (await client.post("/v1/tasks/dn-zzzzz/check", headers=AUTH)).status == 404


async def test_a_poll_already_running_is_the_check(started, monkeypatch):
    app, client, task_id, status, *_ = started
    status(ready=False)
    polled = []
    monkeypatch.setattr("darner.api.poll_once", lambda app: polled.append(1) or asyncio.sleep(0))
    async with app[POLL_LOCK]:
        check = asyncio.ensure_future(client.post(f"/v1/tasks/{task_id}/check", headers=AUTH))
        await asyncio.sleep(0.2)
        assert not check.done()
    # Answered once the running poll is done, without another.
    assert (await check).status == 200 and polled == []


async def test_launcher_over_the_cap_is_given_up_with_what_it_started():
    from darner.api import run_remote

    writer = "(sleep 30; echo late) & head -c 5000000 /dev/zero"
    config = {"launch_command": ["sh", "-c", writer], "limits": {"max_bytes": 1}, "launch_timeout": 30}
    with pytest.raises(RuntimeError, match="more than"):
        await asyncio.wait_for(run_remote(config, []), 5)


async def test_launcher_over_the_cap_is_given_up_not_waited_on():
    from darner.api import run_remote

    writer = "import sys; sys.stdout.buffer.write(b'x' * (5 * 1024 * 1024))"
    config = {"launch_command": [sys.executable, "-c", writer], "limits": {"max_bytes": 1}, "launch_timeout": 30}
    with pytest.raises(RuntimeError, match="more than"):
        await asyncio.wait_for(run_remote(config, []), 15)


async def test_errors_are_json_for_frontends_to_relay(client):
    resp = await client.post("/v1/tasks/dn-recipes/message", json={"text": "hi", "requester": "@a:example.org"}, headers=AUTH)
    assert resp.status == 404 and await resp.json() == {"error": "no session dn-recipes"}
    resp = await client.get("/v1/nothing-here", headers=AUTH)
    assert resp.status == 404 and await resp.json() == {"error": "Not Found"}
