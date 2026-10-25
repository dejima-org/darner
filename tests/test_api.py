import json
import sys

import pytest

from meeseeks.api import make_app
from tests.test_launch import FAKE_TMUX

STUB = """import json, os, sys
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
        "source": {"kind": "matrix", "ref": "$event1"},
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
        "blurbs_dir": str(blurbs),
        "tokens": {"maubot": "s3cret"},
        "launch_command": [sys.executable, str(stub)],
        "policy": {"deny_domains": ["example.net"], "max_domains": 3},
    }


def launched(tmp_path):
    log = tmp_path / "stub.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
async def client(aiohttp_client, config):
    return await aiohttp_client(make_app(config))


async def test_launches_task(client, tmp_path):
    resp = await client.post("/v1/tasks", json=request(blurbs=["myapp"], harness="cline"), headers=AUTH)
    assert resp.status == 201
    body = await resp.json()
    assert body["target"] == "meeseeks:" + body["id"]

    [job] = launched(tmp_path)
    assert job["id"] == body["id"]
    assert job["prompt"] == (
        "Investigate these logs\n\n## Context: myapp\nRepo: example/myapp\n\n"
        "## Context from requester\nstarted after the deploy\n"
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
    client = await aiohttp_client(make_app(config))
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

    Store(config["db_path"]).claim("ms-aaaaa", "matrix", "$event1", "@u:example.org", "{}")
    client = await aiohttp_client(make_app(config))
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
        "--harness", "claude-code", "--backend", "claude-code=anthropic",
    ]
    client = await aiohttp_client(make_app(config))
    resp = await client.post("/v1/tasks", json=request(), headers=AUTH)
    assert resp.status == 201, await resp.text()
    task_id = (await resp.json())["id"]
    argv = json.loads((tmp_path / "tasks" / task_id / "argv.json").read_text())
    assert argv[:7] == ["dejima", "--isolated", "--no-pause", "--harness", "claude-code", "--backend", "anthropic"]
    assert argv[-1].startswith("Investigate these logs")
