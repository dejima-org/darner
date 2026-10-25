import io
import json
import sys
import zipfile

import pytest

from meeseeks.api import make_app, poll_once
from meeseeks.archive import MANIFEST_SLACK
from meeseeks.store import Store
from tests.test_launch import FAKE_TMUX

STUB = """import json, os, sys
command = sys.argv[1:]
if command == ["status"]:
    sys.stdout.write(open(os.environ["STUB_STATUS"]).read())
elif command[:1] == ["help"]:
    sys.stdout.buffer.write(os.environ.get("STUB_HELP", "").encode())
elif command[:1] == ["answer"]:
    open(os.environ["STUB_LOG"], "a").write(json.dumps([*command, json.load(sys.stdin)]) + "\\n")
    if os.environ.get("STUB_ANSWER_ERROR"):
        sys.exit("meeseeks-launch: " + os.environ["STUB_ANSWER_ERROR"])
elif command[:1] == ["fetch"]:
    open(os.environ["STUB_LOG"], "a").write(json.dumps(command) + "\\n")
    code = int(os.environ.get("STUB_FETCH_CODE", "0"))
    if code:
        sys.stderr.write("meeseeks-launch: boom\\n" if code == 1 else "meeseeks-launch: more than 10 bytes\\n")
        sys.exit(code)
    sys.stdout.buffer.write(open(os.environ["STUB_FETCH"], "rb").read())
else:
    job = json.load(sys.stdin)
    open(os.environ["STUB_LOG"], "a").write(json.dumps(job) + "\\n")
    if os.environ.get("STUB_FAIL"):
        sys.exit("no such harness")
    print(json.dumps({"id": job["id"], "target": "meeseeks:" + job["id"]}))
"""

AUTH = {"Authorization": "Bearer s3cret"}


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


@pytest.fixture
def config(tmp_path, monkeypatch):
    stub = tmp_path / "stub.py"
    stub.write_text(STUB)
    blurbs = tmp_path / "blurbs"
    blurbs.mkdir()
    (blurbs / "myapp.md").write_text("Repo: example/myapp\n")
    monkeypatch.setenv("STUB_LOG", str(tmp_path / "stub.log"))
    return {
        "db_path": str(tmp_path / "db"),
        "results_dir": str(tmp_path / "results"),
        "output_instructions": "Leave it in ~/shared.",
        "blurbs_dir": str(blurbs),
        "tokens": {"maubot": "s3cret"},
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
    assert body["target"] == "meeseeks:" + body["id"]

    [job] = launched(tmp_path)
    assert job["id"] == body["id"]
    assert job["prompt"] == (
        "Investigate these logs\n\n## Context: myapp\nRepo: example/myapp\n\n"
        "## Context from requester\nstarted after the deploy\n\n"
        "## Delivering your output\nLeave it in ~/shared.\n"
    )
    assert job["domains"] == ["pypi.org"] and job["dependencies"] == ["pip:pytest"]
    assert job["harness"] == "cline" and "backend" not in job


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "s3cret"}])
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
    from meeseeks.store import Store

    Store(config["db_path"]).claim("ms-aaaaa", "maubot", request()["source"], "@u:example.org", "{}")
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
    config["launch_command"] = [
        sys.executable, "-m", "meeseeks.launch", "--tmux", str(tmux), "--tasks-dir", str(tmp_path / "tasks"),
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
            {"id": "ms-zzzzz", "state": "exited", "ready": True, "help": True},
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


async def test_ready_output_becomes_event_and_download(started, tmp_path):
    app, client, task_id, status, output = started
    status()
    output(zip_bytes([("RESULT.md", "Found it.\x1b[0m"), ("report.txt", "details"), ("MANIFEST.txt", "x")]))
    await poll_once(app)

    [event] = await events(client)
    assert event["id"] == task_id and event["type"] == "output" and event["summary"] == "Found it.[0m"
    assert event["source"] == request()["source"]
    assert fetches(tmp_path) == [["fetch", task_id]]

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

    config["tokens"]["other"] = "0ther"
    other = {"Authorization": "Bearer 0ther"}
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
    config["tokens"]["other"] = "0ther"
    resp = await client.get(f"/v1/tasks/{task_id}/outputs/{event['seq']}.zip", headers={"Authorization": "Bearer 0ther"})
    assert resp.status == 404
    resp = await client.get(f"/v1/tasks/ms-zzzzz/outputs/{event['seq']}.zip", headers=AUTH)
    assert resp.status == 404


@pytest.mark.parametrize("path", ["/v1/events", "/v1/tasks/ms-aaaaa/outputs/1.zip"])
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
    assert event["target"] == f"meeseeks:{task_id}"


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
    config["tokens"]["other"] = "0ther"
    body = {"text": "x", "requester": "@a:example.org"}
    resp = await client.post(f"/v1/tasks/{task_id}/answer", json=body, headers={"Authorization": "Bearer 0ther"})
    assert resp.status == 404
    assert (await client.post(f"/v1/tasks/{task_id}/answer", json=body)).status == 401
    assert all(f[0] != "answer" for f in fetches(tmp_path))


@pytest.mark.parametrize("body", [{"text": ""}, {"text": "x"}, {"text": "x", "requester": "@a", "extra": 1}, ["x"]])
async def test_answer_rejects_bad_body(started, body):
    app, client, task_id, *_ = started
    assert (await client.post(f"/v1/tasks/{task_id}/answer", json=body, headers=AUTH)).status == 400
