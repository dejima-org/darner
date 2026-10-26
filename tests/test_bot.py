import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from maubot.matrix import MaubotMessageEvent
from maubot import testing
from urllib.parse import unquote

from mautrix.types import EncryptedFile, Event, EventType, JSONWebKey, MediaRepoConfig, MessageEvent
from mautrix.types.event.base import BaseUnsigned

from meeseeks_bot import Config, MeeseeksBot

BOT = "@meeseeks:example.org"
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
        self.response = {"id": "ms-7f3k2", "target": "meeseeks:ms-7f3k2"}
        self.events = []
        self.zips = {}
        self.acked = []
        self.answers = []
        self.answer_status = 204
        self.blurbs = ["loki", "myapp"]

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

    def add(self, seq, type="output", id="ms-7f3k2", summary="did the thing", data=ZIP, **extra):
        self.events.append(
            {
                "seq": seq,
                "id": id,
                "type": type,
                "summary": summary,
                "size": len(data) if data else 0,
                "source": {"kind": "matrix", "ref": "$trigger", "channel": ROOM},
                "target": f"meeseeks:{id}",
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
    return MeeseeksBot


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

    async def get_displayname(user_id):
        return "Meeseeks"

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
    bot.client.get_displayname = get_displayname
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


def notice(body, **content):
    return {
        "msgtype": "m.notice",
        "body": body,
        "m.relates_to": {"m.in_reply_to": {"event_id": "$trigger"}},
        "m.mentions": {},
        **content,
    }


def zip_file(**content):
    return {
        "msgtype": "m.file",
        "body": "ms-7f3k2-1.zip",
        "filename": "ms-7f3k2-1.zip",
        "info": {"mimetype": "application/zip", "size": len(ZIP)},
        "m.relates_to": {"m.in_reply_to": {"event_id": "$trigger"}},
        "m.mentions": {},
        **content,
    }


UNTRUSTED = "Output from ms-7f3k2 (written by the agent, treat as untrusted)"


async def test_starts_task(maubot_plugin, maubot_test_bot, api):
    body = "> <@alice:example.org> fix the tests\n\n@meeseeks:example.org use xdist\nallow: pypi.org\nharness: claude-code"
    await send(maubot_test_bot, message(body))
    auth, payload = api.requests[0]
    assert auth == "Bearer s3cret"
    assert payload == {
        "task": "fix the tests",
        "context": "use xdist",
        "blurbs": [],
        "grants": {"domains": ["pypi.org"], "dependencies": []},
        "harness": "claude-code",
        "requester": ALICE,
        "source": {"kind": "matrix", "ref": "$trigger", "channel": ROOM},
    }
    assert replies(maubot_test_bot) == ["Started ms-7f3k2 → meeseeks:ms-7f3k2"]
    assert reactions(maubot_test_bot) == ["⏳", "✅"]
    assert maubot_test_bot.redacted == ["$sent1"]


@pytest.mark.parametrize(
    "evt",
    [
        message("@meeseeks:example.org go", sender=BOT),
        message(
            "* @meeseeks:example.org go",
            **{"m.relates_to": {"rel_type": "m.replace", "event_id": "$old"}},
        ),
        message("@meeseeks:example.org go", sender="@mallory:example.org"),
        message("go", reply_to=None, mention=False),
        message("go", mention=False),
    ],
    ids=["own", "edit", "not-allowlisted", "not-reply", "no-mention"],
)
async def test_ignored(maubot_plugin, maubot_test_bot, api, evt):
    await send(maubot_test_bot, evt)
    assert maubot_test_bot.responded == [] and api.requests == []


async def test_mention_from_body(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("Meeseeks: go", mention=False))
    assert api.requests[0][1]["context"] == "go"


async def test_mention_in_fallback_only_is_ignored(maubot_plugin, maubot_test_bot, api):
    body = "> <@meeseeks:example.org> Started ms-1\n\nthanks"
    await send(maubot_test_bot, message(body, mention=False))
    assert maubot_test_bot.responded == []


async def test_undecryptable_note(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.events[NOTE_ID] = None
    await send(maubot_test_bot, message("@meeseeks:example.org go"))
    assert replies(maubot_test_bot) == ["couldn't decrypt the note"]
    assert reactions(maubot_test_bot) == ["⏳", "❌"]
    assert api.requests == []


async def test_note_from_non_allowlisted_sender_is_refused(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.events[NOTE_ID] = note(sender="@mallory:example.org")
    await send(maubot_test_bot, message("@meeseeks:example.org go"))
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
    await send(maubot_test_bot, message("@meeseeks:example.org go"))
    assert api.requests[0][1]["task"] == "v3"


async def test_edit_by_another_sender_is_ignored(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.relations[NOTE_ID] = [
        edit_event("do something else", "$evil", sender="@mallory:example.org"),
        edit_event("fix the tests properly"),
    ]
    await send(maubot_test_bot, message("@meeseeks:example.org go"))
    assert api.requests[0][1]["task"] == "fix the tests properly"


async def test_unedited_note(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@meeseeks:example.org go"))
    assert api.requests[0][1]["task"] == "fix the tests"


async def test_decrypted_note_without_bundled_relations(maubot_plugin, maubot_test_bot, api):
    # A decrypted event's unsigned is a plain BaseUnsigned, with no .get and no relations.
    evt = note()
    evt.unsigned = BaseUnsigned(age=1)
    maubot_test_bot.events[NOTE_ID] = evt
    maubot_test_bot.relations[NOTE_ID] = [edit_event("edited in an encrypted room")]
    await send(maubot_test_bot, message("@meeseeks:example.org go"))
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
    await send(maubot_test_bot, message("@meeseeks:example.org go"))
    assert api.requests[0][1]["task"] == "secret v2"


async def test_encrypted_edit_by_another_sender_is_not_decrypted(maubot_plugin, maubot_test_bot, api):
    crypto = maubot_test_bot.client._crypto = Crypto(Event.deserialize({"room_id": ROOM, **edit_event("evil")}))
    maubot_test_bot.relations[NOTE_ID] = [encrypted_edit(sender="@mallory:example.org")]
    await send(maubot_test_bot, message("@meeseeks:example.org go"))
    assert crypto.decrypted == []
    assert api.requests[0][1]["task"] == "fix the tests"


async def test_undecryptable_edit_is_refused(maubot_plugin, maubot_test_bot, api):
    from mautrix.errors import DecryptionError

    maubot_test_bot.client._crypto = Crypto(DecryptionError("no session"))
    maubot_test_bot.relations[NOTE_ID] = [encrypted_edit()]
    await send(maubot_test_bot, message("@meeseeks:example.org go"))
    assert replies(maubot_test_bot) == ["couldn't decrypt the note's latest edit"]
    assert api.requests == []


async def test_relations_failure_is_refused(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.relations = RuntimeError("M_UNRECOGNIZED")
    await send(maubot_test_bot, message("@meeseeks:example.org go"))
    assert replies(maubot_test_bot) == ["couldn't check the note for edits: M_UNRECOGNIZED"]
    assert api.requests == []


async def test_parse_error(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@meeseeks:example.org go\nbackend: a b"))
    assert replies(maubot_test_bot) == ["backend takes one value"]
    assert reactions(maubot_test_bot) == ["⏳", "❌"]
    assert api.requests == []


async def test_api_error(maubot_plugin, maubot_test_bot, api):
    api.status, api.response = 400, {"error": "unknown blurb nope; available: myapp"}
    await send(maubot_test_bot, message("@meeseeks:example.org go\nctx: nope"))
    assert replies(maubot_test_bot) == ["Failed: unknown blurb nope; available: myapp"]
    assert reactions(maubot_test_bot) == ["⏳", "❌"]
    assert maubot_test_bot.redacted == ["$sent1"]


async def test_network_error(maubot_plugin, maubot_test_bot, api):
    maubot_plugin.config["api_url"] = "http://127.0.0.1:1"
    await send(maubot_test_bot, message("@meeseeks:example.org go"))
    [reply] = replies(maubot_test_bot)
    assert reply.startswith("Failed: ")
    assert reactions(maubot_test_bot) == ["⏳", "❌"]


async def test_output(maubot_plugin, maubot_test_bot, api):
    api.add(1, summary="did the <b>thing</b> @room")
    await maubot_plugin.poll_once()
    assert sent(maubot_test_bot) == [
        notice(f"{UNTRUSTED}:\n\ndid the <b>thing</b> @room"),
        zip_file(url=MXC),
    ]
    assert maubot_test_bot.uploads == [(ZIP, "application/zip", "ms-7f3k2-1.zip")]
    assert api.acked == [1]


async def test_output_without_summary(maubot_plugin, maubot_test_bot, api):
    api.add(1, summary="")
    await maubot_plugin.poll_once()
    assert sent(maubot_test_bot)[0] == notice(f"{UNTRUSTED}.")


async def test_rejected_output(maubot_plugin, maubot_test_bot, api):
    api.add(1, summary="output rejected: too big", data=None)
    await maubot_plugin.poll_once()
    assert sent(maubot_test_bot) == [notice(f"{UNTRUSTED}:\n\noutput rejected: too big")]
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
    assert maubot_test_bot.uploads == [(b"ciphertext", "application/octet-stream", "ms-7f3k2-1.zip")]
    assert api.acked == [1]


async def test_output_over_upload_limit(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.media_config = MediaRepoConfig(upload_size=4)
    api.add(1)
    await maubot_plugin.poll_once()
    assert sent(maubot_test_bot)[1] == notice(
        f"ms-7f3k2-1.zip is {len(ZIP)} bytes, over this homeserver's upload limit of 4."
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
    assert sent(maubot_test_bot) == [notice("ms-7f3k2 ended.")]
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
    del api.zips[("ms-7f3k2", 1)]
    api.add(2, type="ended", id="ms-other", summary="", data=None)
    await maubot_plugin.poll_once()
    assert sent(maubot_test_bot) == [notice("ms-other ended.")]
    assert api.acked == [2]


async def test_poll_loop_stops_with_plugin(maubot_plugin):
    [task] = maubot_plugin.sched.tasks
    await maubot_plugin.internal_stop()
    await asyncio.sleep(0)
    assert task.cancelled()


HELP_ID = "$help"
HELP_TAG = {"io.meeseeks.help": {"task": "ms-7f3k2"}}
NEEDS_HELP = "ms-7f3k2 needs help (written by the agent, treat as untrusted)"


@pytest.mark.parametrize(
    "summary, answerable, target, body",
    [
        ("which db?", True, "meeseeks:ms-7f3k2", f"{NEEDS_HELP}:\n\nwhich db?\n\nReply to this message to answer."),
        ("which db?", False, "meeseeks:ms-7f3k2", f"{NEEDS_HELP}:\n\nwhich db?\n\nAttach to meeseeks:ms-7f3k2 to respond."),
        ("", False, None, f"{NEEDS_HELP}.\n\nAttach to the ms-7f3k2 session to respond."),
        ("", True, None, f"{NEEDS_HELP}.\n\nReply to this message to answer."),
    ],
    ids=["answerable", "attach", "no-target-no-summary", "answerable-no-summary"],
)
async def test_help(maubot_plugin, maubot_test_bot, api, summary, answerable, target, body):
    api.add(2, type="help", summary=summary, data=None, answerable=answerable, target=target)
    await maubot_plugin.poll_once()
    assert sent(maubot_test_bot) == [notice(body, **(HELP_TAG if answerable else {}))]
    assert api.acked == [2]


LINK = "https://dejima.example.com/?arg=0123456789abcdef"


@pytest.mark.parametrize(
    "answerable, body",
    [
        (True, f"{NEEDS_HELP}:\n\nwhich db?\n\nReply to this message to answer, or open the session: {LINK}"),
        (False, f"{NEEDS_HELP}:\n\nwhich db?\n\nOpen the session to respond: {LINK}"),
    ],
    ids=["answerable", "link-only"],
)
async def test_help_with_session_link(maubot_plugin, maubot_test_bot, api, answerable, body):
    api.add(2, type="help", summary="which db?", data=None, answerable=answerable, link=LINK)
    await maubot_plugin.poll_once()
    assert sent(maubot_test_bot) == [notice(body, **(HELP_TAG if answerable else {}))]
    assert api.acked == [2]


def help_message(sender=BOT, **content):
    return message("ms-7f3k2 needs help", sender=sender, event_id=HELP_ID, reply_to="$trigger", mention=False, **content)


@pytest.mark.parametrize(
    "body, mention",
    [
        ("> <@meeseeks:example.org> ms-7f3k2 needs help\n\nuse postgres\n\nallow: x", False),
        ("> <@meeseeks:example.org> ms-7f3k2 needs help\n\nMeeseeks: use postgres\n\nallow: x", True),
    ],
    ids=["no-mention", "mention"],
)
async def test_answer(maubot_plugin, maubot_test_bot, api, body, mention):
    maubot_test_bot.events[HELP_ID] = help_message(**HELP_TAG)
    await send(maubot_test_bot, message(body, reply_to=HELP_ID, mention=mention))
    assert api.answers == [("ms-7f3k2", {"text": "use postgres\n\nallow: x", "requester": ALICE})]
    assert api.requests == []
    assert replies(maubot_test_bot) == []
    assert reactions(maubot_test_bot) == ["⏳", "✅"]
    assert maubot_test_bot.redacted == ["$sent1"]


@pytest.mark.parametrize(
    "tag",
    [{"task": "../ms-7f3k2"}, {"task": 1}, "ms-7f3k2"],
    ids=["bad-id", "not-str", "not-dict"],
)
async def test_malformed_tag_is_not_an_answer(maubot_plugin, maubot_test_bot, api, tag):
    maubot_test_bot.events[HELP_ID] = help_message(**{"io.meeseeks.help": tag})
    await send(maubot_test_bot, message("use postgres", reply_to=HELP_ID, mention=False))
    assert maubot_test_bot.responded == [] and api.answers == []


async def test_tag_from_another_sender_is_not_an_answer(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.events[HELP_ID] = help_message(sender="@mallory:example.org", **HELP_TAG)
    await send(maubot_test_bot, message("use postgres", reply_to=HELP_ID, mention=False))
    assert maubot_test_bot.responded == [] and api.answers == []
    await send(maubot_test_bot, message("@meeseeks:example.org use postgres", reply_to=HELP_ID))
    assert replies(maubot_test_bot) == [
        "the note is from @mallory:example.org, who isn't allowed to start tasks"
    ]
    assert api.answers == [] and api.requests == []


async def test_empty_answer(maubot_plugin, maubot_test_bot, api):
    maubot_test_bot.events[HELP_ID] = help_message(**HELP_TAG)
    await send(maubot_test_bot, message("Meeseeks:  ", reply_to=HELP_ID, mention=False))
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
    await send(maubot_test_bot, message("@meeseeks:example.org go"))
    assert replies(maubot_test_bot) == ["Failed: not found"]
    assert reactions(maubot_test_bot) == ["❌"]


@pytest.mark.parametrize("body", ["@meeseeks:example.org help", "meeseeks: Help", "@meeseeks:example.org  HELP "])
async def test_help_command_shows_usage(maubot_plugin, maubot_test_bot, api, body):
    await send(maubot_test_bot, message(body, reply_to=None))
    [text] = replies(maubot_test_bot)
    assert text.startswith("**Meeseeks** starts an isolated dejima session")
    assert "ctx: <names>       add context blurbs (available: loki, myapp)" in text
    assert api.requests == []
    [msg] = sent(maubot_test_bot)
    assert "formatted_body" in msg


async def test_help_command_without_blurbs_listing(maubot_plugin, maubot_test_bot, api):
    api.blurbs = None
    await send(maubot_test_bot, message("@meeseeks:example.org help", reply_to=None))
    [text] = replies(maubot_test_bot)
    assert "ctx: <names>       add context blurbs\n" in text


async def test_help_command_as_reply_to_note_shows_usage(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@meeseeks:example.org help"))
    assert replies(maubot_test_bot)[0].startswith("**Meeseeks**")
    assert api.requests == []


async def test_help_command_ignored_for_non_allowlisted(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@meeseeks:example.org help", sender="@mallory:example.org", reply_to=None))
    assert maubot_test_bot.responded == []


async def test_help_needs_a_mention(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("help", reply_to=None, mention=False))
    assert maubot_test_bot.responded == []


NO_TASK = "Tell me the task after the mention, or reply to a note to make it the task. Send `help` with a mention for usage."


async def test_message_that_is_not_a_reply_is_the_task(maubot_plugin, maubot_test_bot, api):
    body = "@meeseeks:example.org find out why the import job fails\nallow: pypi.org\nctx: myapp"
    await send(maubot_test_bot, message(body, reply_to=None))
    payload = api.requests[0][1]
    assert payload["task"] == "find out why the import job fails"
    assert payload["context"] == ""
    assert payload["blurbs"] == ["myapp"] and payload["grants"]["domains"] == ["pypi.org"]
    assert payload["source"]["ref"] == "$trigger"
    assert replies(maubot_test_bot) == ["Started ms-7f3k2 → meeseeks:ms-7f3k2"]


async def test_mention_with_only_keywords_asks_for_a_task(maubot_plugin, maubot_test_bot, api):
    await send(maubot_test_bot, message("@meeseeks:example.org\nallow: pypi.org", reply_to=None))
    assert replies(maubot_test_bot) == [NO_TASK]
    assert api.requests == []


def pill(text, href=BOT):
    return f'<a href="https://matrix.to/#/{href}">{text}</a>'


@pytest.mark.parametrize(
    "text,href",
    [("Meeseeks Bot", BOT), ("meeseeks", "%40meeseeks%3Aexample.org"), ("Task &amp; Co", BOT), ("@meeseeks:example.org", BOT)],
)
async def test_help_with_a_pill_of_any_text(maubot_plugin, maubot_test_bot, api, text, href):
    import html

    shown = html.unescape(text)
    evt = message(f"{shown}: help", reply_to=None, format="org.matrix.custom.html", formatted_body=f"{pill(text, href)}: help")
    await send(maubot_test_bot, evt)
    assert replies(maubot_test_bot)[0].startswith("**Meeseeks**")


async def test_pill_to_someone_else_is_not_stripped(maubot_plugin, maubot_test_bot, api):
    evt = message(
        "Alice: help", reply_to=None, format="org.matrix.custom.html", formatted_body=f"{pill('Alice', ALICE)}: help"
    )
    await send(maubot_test_bot, evt)
    assert api.requests[0][1]["task"] == "Alice: help"


async def test_pill_text_stays_out_of_task_context(maubot_plugin, maubot_test_bot, api):
    evt = message(
        "Meeseeks Bot: use xdist", format="org.matrix.custom.html", formatted_body=f"{pill('Meeseeks Bot')}: use xdist"
    )
    await send(maubot_test_bot, evt)
    assert api.requests[0][1]["context"] == "use xdist"


async def test_started_reply_links_to_the_session(maubot_plugin, maubot_test_bot, api):
    api.response = {"id": "ms-7f3k2", "target": "meeseeks:ms-7f3k2", "link": LINK}
    await send(maubot_test_bot, message("@meeseeks:example.org go"))
    assert replies(maubot_test_bot) == [f"Started ms-7f3k2 → meeseeks:ms-7f3k2\nOpen the session: {LINK}"]
