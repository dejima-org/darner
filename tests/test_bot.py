# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
import asyncio
import sys
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from maubot.matrix import MaubotMessageEvent
from maubot import testing
from urllib.parse import unquote

from mautrix.types import EncryptedFile, Event, EventType, JSONWebKey, MediaRepoConfig, MessageEvent
from mautrix.types.event.base import BaseUnsigned

from dragonlet import Config, Dragonlet

BOT = "@dragonlet:example.org"
ALICE = "@alice:example.org"
NOTE_ID = "$note"
ROOM = "!room:example.org"
ZIP = b"PK zip bytes"
MXC = "mxc://example.org/zip"


def message(body, sender=ALICE, event_id="$trigger", reply_to=NOTE_ID, mention=True, **content):
    content = {"msgtype": "m.text", "body": body, **content}
    if reply_to:
        content.setdefault("m.relates_to", {"m.in_reply_to": {"event_id": reply_to}})
    if mention:
        content["m.mentions"] = {"user_ids": [BOT]}
    return MessageEvent.deserialize(
        {
            "type": "m.room.message",
            "room_id": ROOM,
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": 1,
            "content": content,
        }
    )


def note(body="fix the tests", unsigned=None, sender=ALICE):
    evt = message(body, sender=sender, event_id=NOTE_ID, reply_to=None, mention=False)
    if unsigned:
        evt = MessageEvent.deserialize({**evt.serialize(), "unsigned": {"age": 1, **unsigned}})
    return evt


class Api:
    def __init__(self):
        self.requests = []
        self.status = 201
        self.response = {"id": "dn-7f3k2", "target": "darner:dn-7f3k2"}
        self.events = []
        self.zips = {}
        self.acked = []
        self.answers = []
        self.answer_status = 204
        self.blurbs = ["loki", "myapp"]
        self.stops = []
        self.stop_status = 204
        self.selftests = []
        self.configs = {"recipes": {"ctx": ["myapp"], "file": ["recipes-token"], "harness": "cline", "context_window": "30000"}}

    async def selftest(self, request):
        self.selftests.append(await request.json())
        return web.json_response({"id": "dn-5e1f7"}, status=201)

    async def list_configs(self, request):
        return web.json_response(sorted(self.configs))

    async def blurb(self, request):
        text = {"recipes": "Recipes live in Mealie.", "loki": "Logs <b>are</b> in Loki.", "long": "x" * 9000, "tall": "".join(f"line {i}\n" for i in range(30))}
        if request.match_info["name"] not in text:
            raise web.HTTPNotFound()
        return web.json_response(text[request.match_info["name"]])

    async def describe(self, request):
        if request.match_info["name"] not in self.configs:
            raise web.HTTPNotFound()
        return web.json_response(self.configs[request.match_info["name"]])

    async def stop(self, request):
        self.stops.append(request.match_info["id"])
        if self.stop_status == 204:
            return web.Response(status=204)
        return web.json_response({"error": "dn-7f3k2 is not running"}, status=self.stop_status)

    async def list_blurbs(self, request):
        if self.blurbs is None:
            return web.json_response({"error": "down"}, status=500)
        return web.json_response(self.blurbs)

    async def handle(self, request):
        self.requests.append((request.headers.get("Authorization"), await request.json()))
        return web.json_response(self.response, status=self.status)

    async def answer(self, request):
        assert request.headers["Authorization"] == "Bearer s3cret"
        self.answers.append((request.match_info["id"], await request.json()))
        if self.answer_status == 204:
            return web.Response(status=204)
        return web.json_response({"error": "no help request is pending"}, status=self.answer_status)

    async def list_events(self, request):
        assert request.headers["Authorization"] == "Bearer s3cret"
        return web.json_response([e for e in self.events if e["seq"] not in self.acked])

    async def output(self, request):
        assert request.headers["Authorization"] == "Bearer s3cret"
        key = (request.match_info["id"], int(request.match_info["seq"]))
        if key not in self.zips:
            raise web.HTTPNotFound()
        return web.Response(body=self.zips[key], content_type="application/zip")

    async def ack(self, request):
        assert request.headers["Authorization"] == "Bearer s3cret"
        self.acked.append(int(request.match_info["seq"]))
        return web.Response(status=204)

    def add(self, seq, type="output", id="dn-7f3k2", summary="did the thing", data=ZIP, **extra):
        self.events.append(
            {
                "seq": seq,
                "id": id,
                "type": type,
                "summary": summary,
                "size": len(data) if data else 0,
                "source": {"kind": "matrix", "ref": "$trigger", "channel": ROOM},
                "target": f"darner:{id}",
                "answerable": False,
                "link": None,
                **extra,
            }
        )
        if data:
            self.zips[(id, seq)] = data


@pytest.fixture
async def api(aiohttp_server):
    api = Api()
    app = web.Application()
    app.router.add_post("/v1/tasks", api.handle)
    app.router.add_get("/v1/events", api.list_events)
    app.router.add_get("/v1/tasks/{id}/outputs/{seq}.zip", api.output)
    app.router.add_post("/v1/events/{seq}/ack", api.ack)
    app.router.add_post("/v1/tasks/{id}/answer", api.answer)
    app.router.add_get("/v1/blurbs", api.list_blurbs)
    app.router.add_post("/v1/tasks/{id}/stop", api.stop)
    app.router.add_get("/v1/configs/{name}", api.describe)
    app.router.add_get("/v1/blurbs/{name}", api.blurb)
    app.router.add_get("/v1/configs", api.list_configs)
    app.router.add_post("/v1/selftest", api.selftest)
    api.server = await aiohttp_server(app)
    return api


@pytest.fixture
def maubot_plugin_path():
    return Path(__file__).parent.parent / "plugin"


@pytest.fixture
def maubot_plugin_db():
    # maubot's own fixture is an async generator that never yields without a database
    return None


@pytest.fixture
def maubot_plugin_class():
    return Dragonlet


@pytest.fixture
def maubot_plugin_config_class():
    return Config


@pytest.fixture
def maubot_plugin_config_overrides(api):
    return {"allowlist": [ALICE], "api_url": str(api.server.make_url("/")), "api_token": "s3cret"}


@pytest.fixture
async def maubot_test_bot():
    bot = testing.TestBot(mxid=BOT)
    bot.events = {NOTE_ID: note()}
    bot.redacted = []
    bot.uploads = []
    bot.encrypted = False
    bot.media_config = MediaRepoConfig(upload_size=1024)
    # Raw m.replace events per event id, newest first, as the relations endpoint returns them.
    bot.relations = {}
    send = bot.client.send_message_event

    async def send_message_event(room_id, event_type, content, **kwargs):
        await send(room_id, event_type, content, **kwargs)
        return f"$sent{len(bot.responded)}"

    async def get_event(room_id, event_id):
        return bot.events[event_id]

    async def redact(room_id, event_id, **kwargs):
        bot.redacted.append(event_id)

    async def upload_media(data, mime_type=None, filename=None, **kwargs):
        bot.uploads.append((data, mime_type, filename))
        return MXC

    async def get_media_repo_config():
        if isinstance(bot.media_config, Exception):
            raise bot.media_config
        return bot.media_config

    async def is_encrypted(room_id):
        assert room_id == ROOM
        return bot.encrypted

    bot.client.send_message_event = send_message_event
    bot.client.upload_media = upload_media
    bot.client.get_media_repo_config = get_media_repo_config
    bot.client.state_store = SimpleNamespace(is_encrypted=is_encrypted)
    async def api_request(method, path, query_params=None, **kwargs):
        parts = str(path).split("/")
        assert parts[-3] == "relations" and parts[-1] == "m.replace" and query_params["dir"] == "b"
        if isinstance(bot.relations, Exception):
            raise bot.relations
        return {"chunk": bot.relations.get(unquote(parts[-2]), [])}

    bot.client.api.request = api_request
    bot.client.get_event = get_event
    bot.client.redact = redact
    yield bot
    await bot.client.api.session.close()


async def send(bot, evt):
    await bot.dispatch(EventType.ROOM_MESSAGE, MaubotMessageEvent(evt, bot.client))


def reactions(bot):
    return [r.content.relates_to.key for r in bot.responded if r.event_type == EventType.REACTION]


def replies(bot):
    return [r.content.body for r in bot.responded if r.event_type == EventType.ROOM_MESSAGE]


def sent(bot):
    assert all(r.room_id == ROOM for r in bot.responded)
    return [r.content.serialize() for r in bot.responded]


def notice(body, task="dn-7f3k2", **content):
    return {
        "msgtype": "m.notice",
        "body": body,
        "m.relates_to": {"m.in_reply_to": {"event_id": "$trigger"}},
        "m.mentions": {},
        "io.branderson.dejima.darner.task": task,
        **content,
    }


def agent_notice(label, text="", footer="", **content):
    """A notice quoting agent-written text, with its HTML rendered by the safe renderer."""
    from html import escape

    from dragonlet.markdown import render

    body, html = (f"{label}:\n\n{text}", f"<p>{escape(label)}:</p>{render(text)}") if text else (f"{label}.", f"<p>{escape(label)}.</p>")
    if footer:
        body, html = f"{body}\n\n{footer}", f"{html}<p>{escape(footer)}</p>"
    return notice(body, format="org.matrix.custom.html", formatted_body=html, **content)


def zip_file(**content):
    return {
        "msgtype": "m.file",
        "body": "dn-7f3k2-1.zip",
        "filename": "dn-7f3k2-1.zip",
        "info": {"mimetype": "application/zip", "size": len(ZIP)},
        "m.relates_to": {"m.in_reply_to": {"event_id": "$trigger"}},
        "m.mentions": {},
        "io.branderson.dejima.darner.task": "dn-7f3k2",
        **content,
    }


UNTRUSTED = "Output from dn-7f3k2 (written by the agent, treat as untrusted)"


async def test_starts_task(maubot_plugin, maubot_test_bot, api):
    body = "> <@alice:example.org> fix the tests\n\n@dragonlet:example.org use xdist\nallow: pypi.org\nharness: claude-code"
    await send(maubot_test_bot, message(body))
    auth, payload = api.requests[0]
    assert auth == "Bearer s3cret"
    assert payload == {
        "task": "fix the tests",
        "context": "use xdist",
        "blurbs": [],
        "configs": [],
        "grants": {"domains": ["pypi.org"], "internal": [], "dependencies": []},
        "harness": "claude-code",
        "requester": ALICE,
        "source": {"kind": "matrix", "ref": "$trigger", "channel": ROOM},
    }
    assert replies(maubot_test_bot) == ["Started dn-7f3k2 → darner:dn-7f3k2"]
    assert reactions(maubot_test_bot) == ["⏳", "✅"]
    assert maubot_test_bot.redacted == ["$sent1"]


@pytest.mark.parametrize(
    "evt",
    [
        message("@dragonlet:example.org go", sender=BOT),
        message(
            "* @dragonlet:example.org go",
            **{"m.relates_to": {"rel_type": "m.replace", "event_id": "$old"}},
        ),
        message("@dragonlet:example.org go", sender="@mallory:example.org"),
        message("go", reply_to=None, mention=False),
        message("go", mention=False),
    ],
    ids=["own", "edit", "not-allowlisted", "not-reply", "no-mention"],
)
async def test_ignored(maubot_plugin, maubot_test_bot, api, evt):
    await send(maubot_test_bot, evt)
    assert maubot_test_bot.responded == [] and api.requests == []


async def test_mention_from_body(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@Dragonlet:example.org: go", mention=False))
    assert api.requests[0][1]["context"] == "go"


@pytest.mark.parametrize("body", ["Dragonlet: go", "darner should get a describe command", "@dragonlet go", "@dragonlet:example.org.uk go", "@dragonlet:example.org:8448 go"])
async def test_talking_about_the_bot_is_not_a_mention(maubot_plugin, maubot_test_bot, api, body):
    # m.mentions is set as a client sets it on any reply to the bot.
    await send(maubot_test_bot, message(body))
    await send(maubot_test_bot, message(body, reply_to=None))
    assert maubot_test_bot.responded == [] and api.requests == []


async def test_pill_with_any_text_is_a_mention(maubot_plugin, maubot_test_bot, api):
    evt = message("Mr M: go", format="org.matrix.custom.html", formatted_body=f"{pill('Mr M', BOT)}: go")
    await send(maubot_test_bot, evt)
    assert api.requests[0][1]["context"] == "go"


async def test_markdown_link_with_any_text_is_a_mention(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message(f"[Mr M](https://matrix.to/#/{BOT}?via=example.org) go", reply_to=None))
    assert api.requests[0][1]["task"] == "go"


async def test_mention_in_fallback_only_is_ignored(maubot_plugin, maubot_test_bot, api):
    body = "> <@dragonlet:example.org> Started dn-1\n\nthanks"
    await send(maubot_test_bot, message(body, mention=False))
    assert maubot_test_bot.responded == []


async def test_undecryptable_note(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.events[NOTE_ID] = None
    await send(maubot_test_bot, message("@dragonlet:example.org go"))
    assert replies(maubot_test_bot) == ["couldn't decrypt the note"]
    assert reactions(maubot_test_bot) == ["⏳", "❌"]
    assert api.requests == []


async def test_note_from_non_allowlisted_sender_is_refused(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.events[NOTE_ID] = note(sender="@mallory:example.org")
    await send(maubot_test_bot, message("@dragonlet:example.org go"))
    assert replies(maubot_test_bot) == ["the note is from @mallory:example.org, who isn't allowed to start tasks"]
    assert reactions(maubot_test_bot) == ["⏳", "❌"]
    assert api.requests == []


def edit_event(body, event_id="$edit", sender=ALICE):
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": 2,
        "content": {
            "msgtype": "m.text",
            "body": f"* {body}",
            "m.new_content": {"msgtype": "m.text", "body": body},
            "m.relates_to": {"rel_type": "m.replace", "event_id": NOTE_ID},
        },
    }


async def test_latest_edit_is_the_task(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.relations[NOTE_ID] = [edit_event("v3", "$e3"), edit_event("v2", "$e2")]
    await send(maubot_test_bot, message("@dragonlet:example.org go"))
    assert api.requests[0][1]["task"] == "v3"


async def test_edit_by_another_sender_is_ignored(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.relations[NOTE_ID] = [
        edit_event("do something else", "$evil", sender="@mallory:example.org"),
        edit_event("fix the tests properly"),
    ]
    await send(maubot_test_bot, message("@dragonlet:example.org go"))
    assert api.requests[0][1]["task"] == "fix the tests properly"


async def test_unedited_note(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@dragonlet:example.org go"))
    assert api.requests[0][1]["task"] == "fix the tests"


async def test_decrypted_note_without_bundled_relations(maubot_plugin, maubot_test_bot, api):
    # A decrypted event's unsigned is a plain BaseUnsigned, with no .get and no relations.
    evt = note()
    evt.unsigned = BaseUnsigned(age=1)
    maubot_test_bot.events[NOTE_ID] = evt
    maubot_test_bot.relations[NOTE_ID] = [edit_event("edited in an encrypted room")]
    await send(maubot_test_bot, message("@dragonlet:example.org go"))
    assert api.requests[0][1]["task"] == "edited in an encrypted room"


def encrypted_edit(sender=ALICE):
    return {
        "type": "m.room.encrypted",
        "event_id": "$edit",
        "sender": sender,
        "origin_server_ts": 2,
        "content": {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": "x",
            "sender_key": "k",
            "device_id": "D",
            "session_id": "s",
            "m.relates_to": {"rel_type": "m.replace", "event_id": NOTE_ID},
        },
    }


class Crypto:
    def __init__(self, result):
        self.result = result
        self.decrypted = []

    async def decrypt_megolm_event(self, evt):
        self.decrypted.append(evt.event_id)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def test_encrypted_edit(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.client._crypto = Crypto(Event.deserialize({"room_id": ROOM, **edit_event("secret v2")}))
    maubot_test_bot.relations[NOTE_ID] = [encrypted_edit()]
    await send(maubot_test_bot, message("@dragonlet:example.org go"))
    assert api.requests[0][1]["task"] == "secret v2"


async def test_encrypted_edit_by_another_sender_is_not_decrypted(maubot_plugin, maubot_test_bot, api):
    crypto = maubot_test_bot.client._crypto = Crypto(Event.deserialize({"room_id": ROOM, **edit_event("evil")}))
    maubot_test_bot.relations[NOTE_ID] = [encrypted_edit(sender="@mallory:example.org")]
    await send(maubot_test_bot, message("@dragonlet:example.org go"))
    assert crypto.decrypted == []
    assert api.requests[0][1]["task"] == "fix the tests"


async def test_undecryptable_edit_is_refused(maubot_plugin, maubot_test_bot, api):
    from mautrix.errors import DecryptionError

    maubot_test_bot.client._crypto = Crypto(DecryptionError("no session"))
    maubot_test_bot.relations[NOTE_ID] = [encrypted_edit()]
    await send(maubot_test_bot, message("@dragonlet:example.org go"))
    assert replies(maubot_test_bot) == ["couldn't decrypt the note's latest edit"]
    assert api.requests == []


async def test_relations_failure_is_refused(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.relations = RuntimeError("M_UNRECOGNIZED")
    await send(maubot_test_bot, message("@dragonlet:example.org go"))
    assert replies(maubot_test_bot) == ["couldn't check the note for edits: M_UNRECOGNIZED"]
    assert api.requests == []


async def test_parse_error(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@dragonlet:example.org go\nbackend: a b"))
    assert replies(maubot_test_bot) == ["backend takes one value"]
    assert reactions(maubot_test_bot) == ["⏳", "❌"]
    assert api.requests == []


async def test_api_error(maubot_plugin, maubot_test_bot, api):
    api.status, api.response = 400, {"error": "unknown blurb nope; available: myapp"}
    await send(maubot_test_bot, message("@dragonlet:example.org go\nctx: nope"))
    assert replies(maubot_test_bot) == ["Failed: unknown blurb nope; available: myapp"]
    assert reactions(maubot_test_bot) == ["⏳", "❌"]
    assert maubot_test_bot.redacted == ["$sent1"]


async def test_network_error(maubot_plugin, maubot_test_bot, api):
    maubot_plugin.config["api_url"] = "http://127.0.0.1:1"
    await send(maubot_test_bot, message("@dragonlet:example.org go"))
    [reply] = replies(maubot_test_bot)
    assert reply.startswith("Failed: ")
    assert reactions(maubot_test_bot) == ["⏳", "❌"]


async def test_output(maubot_plugin, maubot_test_bot, api):
    api.add(1, summary="did the <b>thing</b> @room")
    await maubot_plugin.poll_once()
    assert sent(maubot_test_bot) == [
        agent_notice(UNTRUSTED, "did the <b>thing</b> @room"),
        zip_file(url=MXC),
    ]
    assert maubot_test_bot.uploads == [(ZIP, "application/zip", "dn-7f3k2-1.zip")]
    assert api.acked == [1]


async def test_output_without_summary(maubot_plugin, maubot_test_bot, api):
    api.add(1, summary="")
    await maubot_plugin.poll_once()
    assert sent(maubot_test_bot)[0] == agent_notice(UNTRUSTED)


async def test_rejected_output(maubot_plugin, maubot_test_bot, api):
    api.add(1, summary="output rejected: too big", data=None)
    await maubot_plugin.poll_once()
    assert sent(maubot_test_bot) == [agent_notice(UNTRUSTED, "output rejected: too big")]
    assert maubot_test_bot.uploads == []
    assert api.acked == [1]


async def test_encrypted_output(maubot_plugin, maubot_test_bot, api, monkeypatch):
    info = EncryptedFile(key=JSONWebKey(key="k"), iv="iv", hashes={"sha256": "h"})

    def encrypt_attachment(data):
        assert data == ZIP
        return b"ciphertext", info

    monkeypatch.setitem(
        sys.modules, "mautrix.crypto.attachments", SimpleNamespace(encrypt_attachment=encrypt_attachment)
    )
    maubot_test_bot.encrypted = True
    api.add(1)
    await maubot_plugin.poll_once()
    file = sent(maubot_test_bot)[1]
    assert "url" not in file
    assert file == zip_file(file={**info.serialize(), "url": MXC})
    assert maubot_test_bot.uploads == [(b"ciphertext", "application/octet-stream", "dn-7f3k2-1.zip")]
    assert api.acked == [1]


async def test_output_over_upload_limit(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.media_config = MediaRepoConfig(upload_size=4)
    api.add(1)
    await maubot_plugin.poll_once()
    assert sent(maubot_test_bot)[1] == notice(
        f"dn-7f3k2-1.zip is {len(ZIP)} bytes, over this homeserver's upload limit of 4."
    )
    assert maubot_test_bot.uploads == []
    assert api.acked == [1]


async def test_upload_limit_unknown(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.media_config = RuntimeError("no config")
    api.add(1)
    await maubot_plugin.poll_once()
    assert sent(maubot_test_bot)[1] == zip_file(url=MXC)
    assert api.acked == [1]


async def test_ended(maubot_plugin, maubot_test_bot, api):
    api.add(3, type="ended", summary="", data=None)
    await maubot_plugin.poll_once()
    assert sent(maubot_test_bot) == [notice("dn-7f3k2 ended.")]
    assert api.acked == [3]


async def test_failed_upload_is_retried(maubot_plugin, maubot_test_bot, api):
    bot = maubot_test_bot
    upload = bot.client.upload_media

    async def fail(*args, **kwargs):
        raise RuntimeError("upload failed")

    bot.client.upload_media = fail
    api.add(1)
    await maubot_plugin.poll_once()
    assert api.acked == []
    bot.client.upload_media = upload
    bot.responded.clear()
    await maubot_plugin.poll_once()
    assert sent(bot)[1] == zip_file(url=MXC)
    assert api.acked == [1]


async def test_failure_does_not_block_later_events(maubot_plugin, maubot_test_bot, api):
    api.add(1)
    del api.zips[("dn-7f3k2", 1)]
    api.add(2, type="ended", id="dn-other", summary="", data=None)
    await maubot_plugin.poll_once()
    assert sent(maubot_test_bot) == [notice("dn-other ended.", task="dn-other")]
    assert api.acked == [2]


async def test_poll_loop_stops_with_plugin(maubot_plugin):
    [task] = maubot_plugin.sched.tasks
    await maubot_plugin.internal_stop()
    await asyncio.sleep(0)
    assert task.cancelled()


HELP_ID = "$help"
HELP_TAG = {"io.branderson.dejima.darner.help": {"task": "dn-7f3k2"}}
NEEDS_HELP = "dn-7f3k2 needs help (written by the agent, treat as untrusted)"


@pytest.mark.parametrize(
    "summary, answerable, target, footer",
    [
        ("which db?", True, "darner:dn-7f3k2", "Reply to this message to answer."),
        ("which db?", False, "darner:dn-7f3k2", "Attach to darner:dn-7f3k2 to respond."),
        ("", False, None, "Attach to the dn-7f3k2 session to respond."),
        ("", True, None, "Reply to this message to answer."),
    ],
    ids=["answerable", "attach", "no-target-no-summary", "answerable-no-summary"],
)
async def test_help(maubot_plugin, maubot_test_bot, api, summary, answerable, target, footer):
    api.add(2, type="help", summary=summary, data=None, answerable=answerable, target=target)
    await maubot_plugin.poll_once()
    assert sent(maubot_test_bot) == [agent_notice(NEEDS_HELP, summary, footer, **(HELP_TAG if answerable else {}))]
    assert api.acked == [2]


LINK = "https://dejima.example.com/?arg=0123456789abcdef"


@pytest.mark.parametrize(
    "answerable, footer",
    [
        (True, f"Reply to this message to answer, or open the session: {LINK}"),
        (False, f"Open the session to respond: {LINK}"),
    ],
    ids=["answerable", "link-only"],
)
async def test_help_with_session_link(maubot_plugin, maubot_test_bot, api, answerable, footer):
    api.add(2, type="help", summary="which db?", data=None, answerable=answerable, link=LINK)
    await maubot_plugin.poll_once()
    assert sent(maubot_test_bot) == [agent_notice(NEEDS_HELP, "which db?", footer, **(HELP_TAG if answerable else {}))]
    assert api.acked == [2]


def help_message(sender=BOT, **content):
    return message("dn-7f3k2 needs help", sender=sender, event_id=HELP_ID, reply_to="$trigger", mention=False, **content)


@pytest.mark.parametrize(
    "body, mention",
    [
        ("> <@dragonlet:example.org> dn-7f3k2 needs help\n\nuse postgres\n\nallow: x", False),
        ("> <@dragonlet:example.org> dn-7f3k2 needs help\n\n@dragonlet:example.org use postgres\n\nallow: x", True),
    ],
    ids=["no-mention", "mention"],
)
async def test_answer(maubot_plugin, maubot_test_bot, api, body, mention):
    maubot_test_bot.events[HELP_ID] = help_message(**HELP_TAG)
    await send(maubot_test_bot, message(body, reply_to=HELP_ID, mention=mention))
    assert api.answers == [("dn-7f3k2", {"text": "use postgres\n\nallow: x", "requester": ALICE})]
    assert api.requests == []
    assert replies(maubot_test_bot) == []
    assert reactions(maubot_test_bot) == ["⏳", "✅"]
    assert maubot_test_bot.redacted == ["$sent1"]


@pytest.mark.parametrize(
    "tag",
    [{"task": "../dn-7f3k2"}, {"task": 1}, "dn-7f3k2"],
    ids=["bad-id", "not-str", "not-dict"],
)
async def test_malformed_tag_is_not_an_answer(maubot_plugin, maubot_test_bot, api, tag):
    maubot_test_bot.events[HELP_ID] = help_message(**{"io.branderson.dejima.darner.help": tag})
    await send(maubot_test_bot, message("use postgres", reply_to=HELP_ID, mention=False))
    assert maubot_test_bot.responded == [] and api.answers == []


async def test_tag_from_another_sender_is_not_an_answer(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.events[HELP_ID] = help_message(sender="@mallory:example.org", **HELP_TAG)
    await send(maubot_test_bot, message("use postgres", reply_to=HELP_ID, mention=False))
    assert maubot_test_bot.responded == [] and api.answers == []
    await send(maubot_test_bot, message("@dragonlet:example.org use postgres", reply_to=HELP_ID))
    assert replies(maubot_test_bot) == [
        "the note is from @mallory:example.org, who isn't allowed to start tasks"
    ]
    assert api.answers == [] and api.requests == []


async def test_empty_answer(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.events[HELP_ID] = help_message(**HELP_TAG)
    await send(maubot_test_bot, message("@dragonlet:example.org  ", reply_to=HELP_ID, mention=False))
    assert replies(maubot_test_bot) == ["the answer is empty"]
    assert reactions(maubot_test_bot) == ["⏳", "❌"]
    assert api.answers == []


async def test_answer_api_error(maubot_plugin, maubot_test_bot, api):
    api.answer_status = 409
    maubot_test_bot.events[HELP_ID] = help_message(**HELP_TAG)
    await send(maubot_test_bot, message("use postgres", reply_to=HELP_ID, mention=False))
    assert replies(maubot_test_bot) == ["Failed: no help request is pending"]
    assert reactions(maubot_test_bot) == ["⏳", "❌"]
    assert maubot_test_bot.redacted == ["$sent1"]


async def test_answer_from_non_allowlisted_sender_is_ignored(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.events[HELP_ID] = help_message(**HELP_TAG)
    await send(maubot_test_bot, message("use postgres", sender="@mallory:example.org", reply_to=HELP_ID))
    assert maubot_test_bot.responded == [] and api.answers == []


async def test_undecryptable_reply_without_mention_is_ignored(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.events[NOTE_ID] = None
    await send(maubot_test_bot, message("thanks", mention=False))
    assert maubot_test_bot.responded == []


async def test_fetch_error(maubot_plugin, maubot_test_bot, api):
    async def get_event(room_id, event_id):
        raise RuntimeError("not found")

    maubot_test_bot.client.get_event = get_event
    await send(maubot_test_bot, message("thanks", mention=False))
    assert maubot_test_bot.responded == []
    await send(maubot_test_bot, message("@dragonlet:example.org go"))
    assert replies(maubot_test_bot) == ["Failed: not found"]
    assert reactions(maubot_test_bot) == ["❌"]


@pytest.mark.parametrize("body", ["@dragonlet:example.org help", "@Dragonlet:example.org: Help", "@dragonlet:example.org  HELP "])
async def test_help_command_shows_usage(maubot_plugin, maubot_test_bot, api, body):
    await send(maubot_test_bot, message(body, reply_to=None))
    [text] = replies(maubot_test_bot)
    assert text.startswith("**darner** starts an isolated dejima session")
    assert "ctx: <names>       add context blurbs (available: loki, myapp)" in text
    assert "\n@dragonlet:example.org find out why" in text
    assert api.requests == []
    [msg] = sent(maubot_test_bot)
    assert "formatted_body" in msg


async def test_help_command_without_blurbs_listing(maubot_plugin, maubot_test_bot, api):
    api.blurbs = None
    await send(maubot_test_bot, message("@dragonlet:example.org help", reply_to=None))
    [text] = replies(maubot_test_bot)
    assert "ctx: <names>       add context blurbs\n" in text


async def test_help_command_as_reply_to_note_shows_usage(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@dragonlet:example.org help"))
    assert replies(maubot_test_bot)[0].startswith("**darner**")
    assert api.requests == []


async def test_help_command_ignored_for_non_allowlisted(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@dragonlet:example.org help", sender="@mallory:example.org", reply_to=None))
    assert maubot_test_bot.responded == []


async def test_help_needs_a_mention(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("help", reply_to=None, mention=False))
    assert maubot_test_bot.responded == []


NO_TASK = "Tell me the task after the mention, or reply to a note to make it the task. Send `help` with a mention for usage."


async def test_message_that_is_not_a_reply_is_the_task(maubot_plugin, maubot_test_bot, api):
    body = "@dragonlet:example.org find out why the import job fails\nallow: pypi.org\nctx: myapp"
    await send(maubot_test_bot, message(body, reply_to=None))
    payload = api.requests[0][1]
    assert payload["task"] == "find out why the import job fails"
    assert payload["context"] == ""
    assert payload["blurbs"] == ["myapp"] and payload["grants"]["domains"] == ["pypi.org"]
    assert payload["source"]["ref"] == "$trigger"
    assert replies(maubot_test_bot) == ["Started dn-7f3k2 → darner:dn-7f3k2"]


async def test_mention_with_only_keywords_asks_for_a_task(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@dragonlet:example.org\nallow: pypi.org", reply_to=None))
    assert replies(maubot_test_bot) == [NO_TASK]
    assert api.requests == []


def pill(text, href=BOT):
    return f'<a href="https://matrix.to/#/{href}">{text}</a>'


@pytest.mark.parametrize(
    "text,href",
    [("Dragonlet Bot", BOT), ("dragonlet", "%40dragonlet%3Aexample.org"), ("Task &amp; Co", BOT), ("@dragonlet:example.org", BOT)],
)
async def test_help_with_a_pill_of_any_text(maubot_plugin, maubot_test_bot, api, text, href):
    import html

    shown = html.unescape(text)
    evt = message(f"{shown}: help", reply_to=None, format="org.matrix.custom.html", formatted_body=f"{pill(text, href)}: help")
    await send(maubot_test_bot, evt)
    assert replies(maubot_test_bot)[0].startswith("**darner**")


async def test_pill_to_someone_else_is_not_stripped(maubot_plugin, maubot_test_bot, api):
    evt = message(
        "@dragonlet:example.org Alice: help", reply_to=None, format="org.matrix.custom.html",
        formatted_body=f"@dragonlet:example.org {pill('Alice', ALICE)}: help",
    )
    await send(maubot_test_bot, evt)
    assert api.requests[0][1]["task"] == "Alice: help"


async def test_pill_text_stays_out_of_task_context(maubot_plugin, maubot_test_bot, api):
    evt = message(
        "Dragonlet Bot: use xdist", format="org.matrix.custom.html", formatted_body=f"{pill('Dragonlet Bot')}: use xdist"
    )
    await send(maubot_test_bot, evt)
    assert api.requests[0][1]["context"] == "use xdist"


async def test_started_reply_links_to_the_session(maubot_plugin, maubot_test_bot, api):
    api.response = {"id": "dn-7f3k2", "target": "darner:dn-7f3k2", "link": LINK}
    await send(maubot_test_bot, message("@dragonlet:example.org go"))
    assert replies(maubot_test_bot) == [f"Started dn-7f3k2 → darner:dn-7f3k2\nOpen the session: {LINK}"]


async def test_unsignalled_output_says_so(maubot_plugin, maubot_test_bot, api):
    api.add(1, data=None, summary="forgot .done", signalled=False)
    await maubot_plugin.poll_once()
    [body] = replies(maubot_test_bot)
    assert body == (
        "Output from dn-7f3k2, collected without the agent's done signal "
        "(written by the agent, treat as untrusted):\n\nforgot .done"
    )


def bot_message(sender=BOT, event_id="$started", **content):
    return message("Started dn-7f3k2", sender=sender, event_id=event_id, reply_to="$trigger", mention=False, **content)


async def test_started_reply_is_tagged_with_its_task(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@dragonlet:example.org go"))
    started = [r.content for r in maubot_test_bot.responded if r.event_type == EventType.ROOM_MESSAGE][0]
    assert started["io.branderson.dejima.darner.task"] == "dn-7f3k2"


@pytest.mark.parametrize("tag", [{"io.branderson.dejima.darner.task": "dn-7f3k2"}, {"io.branderson.dejima.darner.help": {"task": "dn-7f3k2"}}])
async def test_stop_by_replying_to_a_task_message(maubot_plugin, maubot_test_bot, api, tag):
    maubot_test_bot.events["$started"] = bot_message(**tag)
    await send(maubot_test_bot, message("@dragonlet:example.org stop", reply_to="$started"))
    assert api.stops == ["dn-7f3k2"]
    assert replies(maubot_test_bot) == ["Stopped dn-7f3k2."]
    assert reactions(maubot_test_bot) == ["⏳", "✅"]
    assert api.answers == []


async def test_stop_by_task_id(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@dragonlet:example.org Stop DN-7F3K2", reply_to=None))
    assert api.stops == ["dn-7f3k2"]


@pytest.mark.parametrize(
    "replied",
    [None, "foreign", "untagged"],
    ids=["not-a-reply", "tag-forged-by-another-sender", "untagged-bot-message"],
)
async def test_stop_needs_a_task(maubot_plugin, maubot_test_bot, api, replied):
    if replied == "foreign":
        maubot_test_bot.events["$started"] = bot_message(sender="@mallory:example.org", **{"io.branderson.dejima.darner.task": "dn-7f3k2"})
    elif replied == "untagged":
        maubot_test_bot.events["$started"] = bot_message()
    reply_to = "$started" if replied else None
    await send(maubot_test_bot, message("@dragonlet:example.org stop", reply_to=reply_to))
    assert api.stops == []
    assert replies(maubot_test_bot)[0].startswith("To stop a session")


async def test_stop_failure_is_relayed(maubot_plugin, maubot_test_bot, api):
    api.stop_status = 409
    await send(maubot_test_bot, message("@dragonlet:example.org stop dn-7f3k2", reply_to=None))
    assert replies(maubot_test_bot) == ["Failed: dn-7f3k2 is not running"]
    assert reactions(maubot_test_bot) == ["⏳", "❌"]


async def test_stop_without_mention_answers_a_help_request(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.events[HELP_ID] = help_message(**HELP_TAG)
    await send(maubot_test_bot, message("stop", reply_to=HELP_ID, mention=False))
    assert api.stops == []
    assert api.answers == [("dn-7f3k2", {"text": "stop", "requester": ALICE})]


REQUEST = {
    "request": 3,
    "domains": [
        {"domain": "pypi.org", "internal": False, "denied": False},
        {"domain": "mirror.internal.example", "internal": True, "denied": False},
        {"domain": "api.example.net", "internal": False, "denied": True},
    ],
    "rejected": 1,
}
DOMAINS_TAG = {"io.branderson.dejima.darner.domains": {"task": "dn-7f3k2", "request": 3}}


async def test_domain_request_lists_every_domain(maubot_plugin, maubot_test_bot, api):
    api.add(4, type="domains", summary="", data=None)
    api.events[-1]["data"] = REQUEST
    await maubot_plugin.poll_once()
    [msg] = sent(maubot_test_bot)
    assert msg["body"] == (
        "dn-7f3k2 asks to reach these domains (requested by the agent, check each one before allowing):\n\n"
        "- pypi.org\n- mirror.internal.example (internal)\n- api.example.net (refused by policy, won't be allowed)\n\n"
        "1 line(s) in the request weren't valid domains and were dropped.\n\n"
        "Reply `allow` with a mention to allow them, or `deny`."
    )
    assert "<li><code>mirror.internal.example</code> (internal)</li>" in msg["formatted_body"]
    assert msg["io.branderson.dejima.darner.domains"] == {"task": "dn-7f3k2", "request": 3}
    assert msg["io.branderson.dejima.darner.task"] == "dn-7f3k2" and msg["m.mentions"] == {}
    assert api.acked == [4]


def domains_message(sender=BOT, **content):
    return message("dn-7f3k2 asks to reach these domains", sender=sender, event_id="$domains", reply_to="$trigger", mention=False, **content)


@pytest.fixture
def decisions(api):
    api.decisions = []
    api.allow_response = {"granted": ["pypi.org"], "failed": [{"domain": "x.example.com", "error": "unreachable"}], "told": False}

    async def decide(request):
        api.decisions.append((request.match_info["verb"], request.match_info["n"], await request.json()))
        if request.match_info["verb"] == "allow":
            return web.json_response(api.allow_response)
        return web.json_response({"told": True})

    api.server.app.router._frozen = False
    api.server.app.router.add_post("/v1/tasks/{id}/domains/{n}/{verb}", decide)
    return api


async def test_allow_reply_grants_the_request(maubot_plugin, maubot_test_bot, decisions):
    maubot_test_bot.events["$domains"] = domains_message(**DOMAINS_TAG)
    await send(maubot_test_bot, message("@dragonlet:example.org allow", reply_to="$domains"))
    assert decisions.decisions == [("allow", "3", {"requester": ALICE})]
    assert replies(maubot_test_bot) == [
        "Allowed for dn-7f3k2: pypi.org.\nCouldn't allow x.example.com: unreachable\n"
        "Tell the agent in its session that it can carry on."
    ]


async def test_deny_reply(maubot_plugin, maubot_test_bot, decisions):
    maubot_test_bot.events["$domains"] = domains_message(**DOMAINS_TAG)
    await send(maubot_test_bot, message("@dragonlet:example.org deny", reply_to="$domains"))
    assert decisions.decisions == [("deny", "3", {"requester": ALICE})]
    assert replies(maubot_test_bot) == ["Denied the domain request from dn-7f3k2."]


@pytest.mark.parametrize("replied", [None, "foreign", "untagged"])
async def test_allow_needs_the_bots_domain_request(maubot_plugin, maubot_test_bot, decisions, replied):
    if replied == "foreign":
        maubot_test_bot.events["$domains"] = domains_message(sender="@mallory:example.org", **DOMAINS_TAG)
    elif replied == "untagged":
        maubot_test_bot.events["$domains"] = domains_message(**{"io.branderson.dejima.darner.task": "dn-7f3k2"})
    await send(maubot_test_bot, message("@dragonlet:example.org allow", reply_to="$domains" if replied else None))
    assert decisions.decisions == []
    assert replies(maubot_test_bot)[0].startswith("To allow or deny domains")


def session(**overrides):
    import time

    s = {
        "id": "dn-7f3k2", "target": "darner:dn-7f3k2", "link": None, "requester": ALICE,
        "started": time.time() - 3720, "state": "running", "answerable": True,
        "waiting": {"answer": None, "domains": None},
    }
    s.update(overrides)
    return s


@pytest.fixture
def sessions(api):
    api.sessions = []
    api.messages = []

    async def list_sessions(request):
        return web.json_response(api.sessions)

    async def get_session(request):
        found = [s for s in api.sessions if s["id"] == request.match_info["id"]]
        return web.json_response(found[0]) if found else web.json_response({}, status=404)

    async def message(request):
        api.messages.append((request.match_info["id"], await request.json()))
        return web.Response(status=204)

    api.server.app.router._frozen = False
    api.server.app.router.add_get("/v1/tasks", list_sessions)
    api.server.app.router.add_get("/v1/tasks/{id}", get_session)
    api.server.app.router.add_post("/v1/tasks/{id}/message", message)
    return api


@pytest.mark.parametrize("command", ["sessions", "list", "list sessions"])
async def test_sessions_when_none_are_open(maubot_plugin, maubot_test_bot, sessions, command):
    await send(maubot_test_bot, message(f"@dragonlet:example.org {command}", reply_to=None))
    assert replies(maubot_test_bot) == ["No open sessions."]


async def test_sessions_list_what_each_waits_on(maubot_plugin, maubot_test_bot, sessions):
    sessions.sessions = [
        session(),
        session(id="dn-aaaaa", link="https://dejima.example.com/?arg=0123456789abcdef", waiting={"answer": "Which db?", "domains": None}),
    ]
    await send(maubot_test_bot, message("@dragonlet:example.org list", reply_to=None))
    assert replies(maubot_test_bot) == [
        "Open sessions:\n\n"
        f"- dn-7f3k2: working, started 1h02m ago by {ALICE} (darner:dn-7f3k2)\n"
        f"- dn-aaaaa: waiting for an answer, started 1h02m ago by {ALICE} (https://dejima.example.com/?arg=0123456789abcdef)"
    ]


async def test_status_quotes_the_help_request(maubot_plugin, maubot_test_bot, sessions):
    sessions.sessions = [session(waiting={"answer": "Which **db**?", "domains": None})]
    await send(maubot_test_bot, message("@dragonlet:example.org status dn-7f3k2", reply_to=None))
    [msg] = [r.content for r in maubot_test_bot.responded if r.event_type == EventType.ROOM_MESSAGE]
    assert "Its help request (written by the agent, treat as untrusted):\n\nWhich **db**?" in msg.body
    assert "<strong>db</strong>" in msg.formatted_body
    assert msg.body.endswith("Reply to its help request, or send `tell dn-7f3k2 <answer>` with a mention.")
    assert msg["io.branderson.dejima.darner.task"] == "dn-7f3k2"


async def test_status_lists_requested_domains(maubot_plugin, maubot_test_bot, sessions):
    sessions.sessions = [session(waiting={"answer": None, "domains": REQUEST})]
    maubot_test_bot.events["$started"] = bot_message(**{"io.branderson.dejima.darner.task": "dn-7f3k2"})
    await send(maubot_test_bot, message("@dragonlet:example.org status", reply_to="$started"))
    [msg] = [r.content for r in maubot_test_bot.responded if r.event_type == EventType.ROOM_MESSAGE]
    assert "- mirror.internal.example (internal)" in msg.body
    assert msg["io.branderson.dejima.darner.domains"] == {"task": "dn-7f3k2", "request": 3}


async def test_status_of_an_idle_or_unknown_session(maubot_plugin, maubot_test_bot, sessions):
    sessions.sessions = [session()]
    await send(maubot_test_bot, message("@dragonlet:example.org status dn-7f3k2", reply_to=None))
    await send(maubot_test_bot, message("@dragonlet:example.org status dn-zzzzz", reply_to=None))
    bodies = replies(maubot_test_bot)
    assert bodies[0].endswith("It isn't waiting on you.")
    assert bodies[1] == "dn-zzzzz isn't an open session."


async def test_tell_by_id_and_by_reply(maubot_plugin, maubot_test_bot, sessions):
    await send(maubot_test_bot, message("@dragonlet:example.org tell dn-7f3k2 Also check\nthe logs.", reply_to=None))
    maubot_test_bot.events["$started"] = bot_message(**{"io.branderson.dejima.darner.task": "dn-7f3k2"})
    await send(maubot_test_bot, message("@dragonlet:example.org tell use main", reply_to="$started"))
    assert sessions.messages == [
        ("dn-7f3k2", {"text": "Also check\nthe logs.", "requester": ALICE}),
        ("dn-7f3k2", {"text": "use main", "requester": ALICE}),
    ]
    assert reactions(maubot_test_bot) == ["⏳", "✅", "⏳", "✅"]


async def test_tell_without_a_session_is_a_task(maubot_plugin, maubot_test_bot, sessions):
    await send(maubot_test_bot, message("@dragonlet:example.org tell me why the build fails", reply_to=None))
    assert sessions.messages == []
    assert sessions.requests[0][1]["task"] == "tell me why the build fails"


async def test_unverified_domain_is_reported_as_allowed(maubot_plugin, maubot_test_bot, decisions):
    decisions.allow_response = {
        "granted": ["recipes.example.com"], "failed": [],
        "unverified": [{"domain": "recipes.example.com", "warning": "curl exit 28"}], "told": True,
    }
    maubot_test_bot.events["$domains"] = domains_message(**DOMAINS_TAG)
    await send(maubot_test_bot, message("@dragonlet:example.org allow", reply_to="$domains"))
    assert replies(maubot_test_bot) == [
        "Allowed for dn-7f3k2: recipes.example.com.\n"
        "recipes.example.com is allowed, but didn't answer on port 443 from the session: curl exit 28"
    ]


async def test_mentioned_reply_to_a_session_message_is_told_to_it(maubot_plugin, maubot_test_bot, sessions):
    maubot_test_bot.events["$started"] = bot_message(**{"io.branderson.dejima.darner.task": "dn-7f3k2"})
    await send(maubot_test_bot, message("@dragonlet:example.org try port 8443 instead", reply_to="$started"))
    assert sessions.messages == [("dn-7f3k2", {"text": "try port 8443 instead", "requester": ALICE})]
    assert sessions.requests == []


async def test_mention_alone_in_reply_to_a_session_message(maubot_plugin, maubot_test_bot, sessions):
    maubot_test_bot.events["$started"] = bot_message(**{"io.branderson.dejima.darner.task": "dn-7f3k2"})
    await send(maubot_test_bot, message("@dragonlet:example.org", reply_to="$started"))
    assert replies(maubot_test_bot) == ["Say what to tell dn-7f3k2 after the mention."]
    assert sessions.messages == [] and sessions.requests == []


def file_message(sender=ALICE, event_id="$file", reply_to=None, **content):
    body = {"msgtype": "m.file", "body": "access.log", **({} if "file" in content else {"url": "mxc://example.org/upload"}), **content}
    if reply_to:
        body["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    return MessageEvent.deserialize(
        {"type": "m.room.message", "room_id": ROOM, "event_id": event_id, "sender": sender, "origin_server_ts": 1, "content": body}
    )


@pytest.fixture
def uploads(api, maubot_test_bot):
    api.files = []

    async def receive(request):
        api.files.append((request.match_info["id"], dict(request.query), await request.read()))
        return web.json_response({"path": f"~/shared/uploads/{request.query['name']}", "size": 3, "told": False})

    api.server.app.router._frozen = False
    api.server.app.router.add_post("/v1/tasks/{id}/files", receive)
    downloads = {"mxc://example.org/upload": b"log", "mxc://example.org/secret": b"ciphertext"}

    async def download_media(url):
        return downloads[url]

    maubot_test_bot.client.download_media = download_media
    maubot_test_bot.events["$started"] = bot_message(**{"io.branderson.dejima.darner.task": "dn-7f3k2"})
    return api


async def test_file_sent_as_a_reply_to_a_session_message_is_uploaded(maubot_plugin, maubot_test_bot, uploads):
    await send(maubot_test_bot, file_message(reply_to="$started", filename="access.log"))
    assert uploads.files == [("dn-7f3k2", {"name": "access.log", "requester": ALICE}, b"log")]
    assert replies(maubot_test_bot) == [
        "Uploaded access.log to dn-7f3k2 as ~/shared/uploads/access.log.\nTell the agent in its session where to find it."
    ]


async def test_upload_command_in_reply_to_a_file(maubot_plugin, maubot_test_bot, uploads):
    maubot_test_bot.events["$file"] = file_message()
    await send(maubot_test_bot, message("@dragonlet:example.org upload dn-7f3k2", reply_to="$file"))
    assert uploads.files == [("dn-7f3k2", {"name": "access.log", "requester": ALICE}, b"log")]


async def test_encrypted_file_is_decrypted_before_upload(maubot_plugin, maubot_test_bot, uploads, monkeypatch):
    def decrypt_attachment(data, key, sha256, iv):
        assert (data, key, sha256, iv) == (b"ciphertext", "k", "h", "iv")
        return b"plaintext"

    monkeypatch.setitem(sys.modules, "mautrix.crypto.attachments", SimpleNamespace(decrypt_attachment=decrypt_attachment))
    secret = {"url": "mxc://example.org/secret", "key": {"k": "k", "kty": "oct", "alg": "A256CTR", "ext": True, "key_ops": ["encrypt", "decrypt"]}, "iv": "iv", "hashes": {"sha256": "h"}, "v": "v2"}
    await send(maubot_test_bot, file_message(reply_to="$started", file=secret))
    assert uploads.files[0][2] == b"plaintext"


async def test_upload_command_needs_a_file(maubot_plugin, maubot_test_bot, uploads):
    await send(maubot_test_bot, message("@dragonlet:example.org upload dn-7f3k2", reply_to="$started"))
    assert uploads.files == []
    assert replies(maubot_test_bot) == ["To upload a file to dn-7f3k2, reply `upload dn-7f3k2` with a mention to the file."]


async def test_files_from_others_or_not_for_a_session_are_ignored(maubot_plugin, maubot_test_bot, uploads):
    await send(maubot_test_bot, file_message(sender="@mallory:example.org", reply_to="$started"))
    await send(maubot_test_bot, file_message(event_id="$other"))
    assert uploads.files == [] and maubot_test_bot.responded == []


async def test_model_and_effort_are_sent(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@dragonlet:example.org go\nmodel: opus\neffort: high\ncontext_window: 32768"))
    payload = api.requests[0][1]
    assert payload["model"] == "opus" and payload["effort"] == "high" and payload["context_window"] == "32768"


async def test_domain_port_is_shown(maubot_plugin, maubot_test_bot, api):
    request = {"request": 1, "domains": [{"domain": "recipes.example.com", "internal": True, "port": 8443, "denied": False}], "rejected": 0}
    api.add(4, type="domains", summary="", data=None)
    api.events[-1]["data"] = request
    await maubot_plugin.poll_once()
    assert "- recipes.example.com (internal) (checked on port 8443)" in sent(maubot_test_bot)[0]["body"]


async def test_help_with_a_markdown_pill_body(maubot_plugin, maubot_test_bot, api):
    body = "[@dragonlet:example.org](https://matrix.to/#/@dragonlet:example.org) help"
    html = '<a href="https://matrix.to/#/@dragonlet:example.org">@dragonlet:example.org</a> help'
    await send(maubot_test_bot, message(body, reply_to=None, format="org.matrix.custom.html", formatted_body=html))
    assert replies(maubot_test_bot)[0].startswith("**darner**")
    assert api.requests == []


async def test_help_lists_harnesses_and_backends(maubot_plugin, maubot_test_bot, api):
    async def catalog(request):
        return web.json_response({"harnesses": ["claude-code", "cline"], "backends": ["anthropic", "self-hosted"], "default_harness": "claude-code"})

    api.server.app.router._frozen = False
    api.server.app.router.add_get("/v1/catalog", catalog)
    await send(maubot_test_bot, message("@dragonlet:example.org help", reply_to=None))
    [text] = replies(maubot_test_bot)
    assert "use this harness instead of the default (claude-code, cline; default claude-code)" in text
    assert "use this backend instead of the harness's default (anthropic, self-hosted)" in text


async def test_help_without_the_catalog(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@dragonlet:example.org help", reply_to=None))
    [text] = replies(maubot_test_bot)
    assert "use this harness instead of the default\n" in text


async def test_config_keyword_is_sent(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@dragonlet:example.org add this recipe\nconfig: mealie\nallow: example.com"))
    payload = api.requests[0][1]
    assert payload["configs"] == ["mealie"] and payload["grants"]["domains"] == ["example.com"]


@pytest.mark.parametrize("summary", ["Do you want to continue? (y/n)\n> ", ""])
async def test_waiting_notice(maubot_plugin, maubot_test_bot, api, summary):
    api.add(5, type="waiting", summary=summary, data=None, link=LINK)
    api.events[-1]["data"] = {"quiet": 360}
    await maubot_plugin.poll_once()
    [msg] = sent(maubot_test_bot)
    assert msg["body"].startswith("dn-7f3k2 seems to be waiting: its screen hasn't changed for 6m.")
    assert msg["body"].endswith(f"Reply with a mention to tell it something, or open the session: {LINK}")
    assert msg["io.branderson.dejima.darner.task"] == "dn-7f3k2" and msg["m.mentions"] == {}
    if summary:
        assert "    Do you want to continue? (y/n)" in msg["body"]
        assert "<pre><code>Do you want to continue? (y/n)\n&gt; </code></pre>" in msg["formatted_body"]
    else:
        assert "<pre>" not in msg["formatted_body"]
    assert api.acked == [5]


async def test_describe_shows_a_config_set(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@dragonlet:example.org describe recipes", reply_to=None))
    [msg] = [r for r in maubot_test_bot.responded if r.event_type == EventType.ROOM_MESSAGE]
    assert "ctx: myapp\n    file: recipes-token\n    harness: cline\n    context_window: 30000" in msg.content.body
    assert "<pre><code>ctx: myapp\nfile: recipes-token" in msg.content.formatted_body
    assert "contents aren't shown" in msg.content.body
    assert api.requests == []


async def test_describe_shows_config_sets_then_blurbs_in_order(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@dragonlet:example.org describe loki, recipes", reply_to=None))
    [body] = replies(maubot_test_bot)
    heads = [line for line in body.splitlines() if line.endswith(")")]
    assert heads == ["loki (blurb)", "recipes (config set)", "recipes (blurb)"]
    [msg] = [r for r in maubot_test_bot.responded if r.event_type == EventType.ROOM_MESSAGE]
    assert "<pre><code>Logs &lt;b&gt;are&lt;/b&gt; in Loki.</code></pre>" in msg.content.formatted_body
    assert reactions(maubot_test_bot) == ["⏳", "✅"]


async def test_describe_names_what_it_did_not_find(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@dragonlet:example.org describe recipes nope", reply_to=None))
    [body] = replies(maubot_test_bot)
    assert body.startswith("recipes (config set)")
    assert body.endswith("No config set or blurb called nope. Send `help` with a mention to list them.")
    assert reactions(maubot_test_bot) == ["⏳", "❌"]


async def test_describe_cuts_a_long_blurb(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@dragonlet:example.org describe long tall recipes", reply_to=None))
    [body] = replies(maubot_test_bot)
    assert "x" * 1000 in body and "x" * 1001 not in body
    assert "line 19\n" in body and "line 20" not in body
    assert body.count("Cut short here") == 2


async def test_describe_is_limited(maubot_plugin, maubot_test_bot, api):
    names = " ".join(f"b{i}" for i in range(11))
    await send(maubot_test_bot, message(f"@dragonlet:example.org describe {names}", reply_to=None))
    assert replies(maubot_test_bot) == ["I can describe up to 10 at once."]


@pytest.mark.parametrize("command, expected", [
    ("list blurbs", "Context blurbs: loki, myapp. Send `describe <names>` with a mention to see them."),
    ("List Configs", "Config sets: recipes. Send `describe <names>` with a mention to see them."),
])
async def test_list_blurbs_and_configs(maubot_plugin, maubot_test_bot, api, command, expected):
    await send(maubot_test_bot, message(f"@dragonlet:example.org {command}", reply_to=None))
    assert replies(maubot_test_bot) == [expected]
    assert api.requests == []


async def test_list_with_none(maubot_plugin, maubot_test_bot, api):
    api.blurbs = []
    await send(maubot_test_bot, message("@dragonlet:example.org list blurbs", reply_to=None))
    assert replies(maubot_test_bot) == ["There are no context blurbs."]


class _Shown(HTMLParser):
    """A formatted body's tags, attributes and visible text."""

    def __init__(self, formatted: str):
        super().__init__()
        self.tags, self.text = [], []
        self.feed(formatted)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def assert_shown_safely(content: dict) -> None:
    """Each PASS in POST /v1/selftest's checks, tested on one message."""
    assert content.get("m.mentions") == {}, "check 6: nobody may be notified"
    shown = _Shown(content["formatted_body"])
    visible = "".join(shown.text)
    safe = {"p", "br", "pre", "code", "strong", "em", "ul", "ol", "li", "blockquote", "hr", "h1", "h2", "h3", "h4", "h5", "h6"}
    for tag, attrs in shown.tags:
        assert tag in safe or tag == "a", f"checks 1-3: a {tag} tag came through as markup"
        assert not any(name.startswith("on") or name == "src" for name in attrs), "check 2: a handler or source came through"
        if tag == "a":
            assert attrs.get("href", "") in visible, "checks 3, 4 and 7: a link hides where it goes"

    def test_line(n: int) -> str:
        return visible.split(f"Test line {n}:", 1)[1].split("Check ", 1)[0].strip()

    assert "<b>" in test_line(1) and "</b>" in test_line(1), "check 1"
    assert "<script>" in test_line(2) and "<img" in test_line(2), "check 2"
    assert '<a href="https://evil.selftest.invalid">' in test_line(3), "check 3"
    assert "evil.selftest.invalid" in test_line(4), "check 4"
    assert "pixel.selftest.invalid" in test_line(5), "check 5"
    assert "evil.selftest.invalid" in test_line(7), "check 7"
    assert test_line(8) == "abc", "check 8"
    assert "Check 10, the end." in visible and "Check 10, the end." in content["body"], "check 10: the message was cut short"


async def test_selftest_is_shown_safely(maubot_plugin, maubot_test_bot, api):
    from darner.archive import clean_text
    from darner.selftest import PAYLOAD

    text = clean_text(PAYLOAD.encode())
    api.add(1, summary=text, data=None)
    api.add(2, type="help", summary=text, data=None)
    api.add(3, type="waiting", summary=text, data={"quiet": 300})
    await maubot_plugin.poll_once()
    messages = sent(maubot_test_bot)
    assert len(messages) == 3
    for content in messages:
        assert_shown_safely(content)


async def test_selftest_command(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@dragonlet:example.org selftest", reply_to=None))
    [reply] = replies(maubot_test_bot)
    assert reply.startswith("Self-test dn-5e1f7: its output, help request and waiting notice follow")
    assert api.selftests == [{"requester": ALICE, "source": {"kind": "matrix", "ref": "$trigger", "channel": ROOM}}]


@pytest.mark.parametrize("unsafe", ["no m.mentions", "raw html", "plain markdown"])
def test_the_selftest_check_catches_an_unsafe_frontend(unsafe):
    from mautrix.util.markdown import render as plain_render

    from darner.archive import clean_text
    from darner.selftest import PAYLOAD
    from dragonlet.markdown import render

    text = clean_text(PAYLOAD.encode())
    content = {"body": text, "formatted_body": render(text), "m.mentions": {}}
    if unsafe == "no m.mentions":
        del content["m.mentions"]
    elif unsafe == "raw html":
        content["formatted_body"] = text
    else:
        content["formatted_body"] = plain_render(text, allow_html=True)
    with pytest.raises(AssertionError):
        assert_shown_safely(content)
    content = {"body": text, "formatted_body": render(text), "m.mentions": {}}
    assert_shown_safely(content)


def test_selftest_checks_hold_no_markup():
    # A check has to read the same in a frontend that fails it.
    from darner.selftest import PAYLOAD

    checks = [line for line in PAYLOAD.splitlines() if line.startswith(("Check ", "Frontend self-test"))]
    assert len(checks) == 11
    for line in checks:
        assert not any(mark in line for mark in ("<", ">", "](", "`", "*", "_", "@")), line



async def test_an_expired_session_is_announced(maubot_plugin, maubot_test_bot, api):
    api.add(1, type="expired", summary="", data=None)
    api.events[-1]["data"] = {"age": 86460}
    await maubot_plugin.poll_once()
    assert replies(maubot_test_bot) == ["dn-7f3k2 has run for 1d0h, longer than sessions may run here, so it was stopped."]


@pytest.mark.parametrize("command", ["stop ms-abc23", "Status MS-ABC23", "tell xx-12345 hello", "upload ab-cdefg"])
async def test_something_like_a_task_id_is_not_taken_as_a_task(maubot_plugin, maubot_test_bot, api, command):
    await send(maubot_test_bot, message(f"@dragonlet:example.org {command}", reply_to=None))
    [reply] = replies(maubot_test_bot)
    assert reply.endswith("isn't one of my task ids, which look like dn-xxxxx.")
    assert api.requests == [] and api.stops == []
