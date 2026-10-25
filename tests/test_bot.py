from pathlib import Path

import pytest
from aiohttp import web
from maubot.matrix import MaubotMessageEvent
from maubot import testing
from mautrix.types import Event, EventType, MessageEvent

from meeseeks_bot import Config, MeeseeksBot

BOT = "@meeseeks:example.org"
ALICE = "@alice:example.org"
NOTE_ID = "$note"


def message(body, sender=ALICE, event_id="$trigger", reply_to=NOTE_ID, mention=True, **content):
    content = {"msgtype": "m.text", "body": body, **content}
    if reply_to:
        content.setdefault("m.relates_to", {"m.in_reply_to": {"event_id": reply_to}})
    if mention:
        content["m.mentions"] = {"user_ids": [BOT]}
    return MessageEvent.deserialize(
        {
            "type": "m.room.message",
            "room_id": "!room:example.org",
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": 1,
            "content": content,
        }
    )


def note(body="fix the tests", unsigned=None):
    evt = message(body, sender="@bob:example.org", event_id=NOTE_ID, reply_to=None, mention=False)
    if unsigned:
        evt = MessageEvent.deserialize({**evt.serialize(), "unsigned": {"age": 1, **unsigned}})
    return evt


class Api:
    def __init__(self):
        self.requests = []
        self.status = 201
        self.response = {"id": "ms-7f3k2", "target": "meeseeks:ms-7f3k2"}

    async def handle(self, request):
        self.requests.append((request.headers.get("Authorization"), await request.json()))
        return web.json_response(self.response, status=self.status)


@pytest.fixture
async def api(aiohttp_server):
    api = Api()
    app = web.Application()
    app.router.add_post("/v1/tasks", api.handle)
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
    return {"whitelist": [ALICE], "api_url": str(api.server.make_url("/")), "api_token": "s3cret"}


@pytest.fixture
async def maubot_test_bot():
    bot = testing.TestBot(mxid=BOT)
    bot.events = {NOTE_ID: note()}
    bot.redacted = []
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

    bot.client.send_message_event = send_message_event
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


async def test_starts_task(maubot_plugin, maubot_test_bot, api):
    body = "> <@bob:example.org> fix the tests\n\n@meeseeks:example.org use xdist\nallow: pypi.org\nharness: claude-code"
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
        "source": {"kind": "matrix", "ref": "$trigger"},
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
        message("@meeseeks:example.org go", reply_to=None),
        message("go", mention=False),
    ],
    ids=["own", "edit", "not-whitelisted", "not-reply", "no-mention"],
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


def edit_event(body, event_id="$edit", sender="@bob:example.org"):
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


async def test_bundled_edit(maubot_plugin, maubot_test_bot, api):
    edit = edit_event("fix the tests properly")
    maubot_test_bot.events[NOTE_ID] = note(unsigned={"m.relations": {"m.replace": edit}})
    await send(maubot_test_bot, message("@meeseeks:example.org go"))
    assert api.requests[0][1]["task"] == "fix the tests properly"


async def test_edit_by_another_sender_is_ignored(maubot_plugin, maubot_test_bot, api):
    edit = edit_event("do something else", sender="@mallory:example.org")
    maubot_test_bot.events[NOTE_ID] = note(unsigned={"m.relations": {"m.replace": edit}})
    await send(maubot_test_bot, message("@meeseeks:example.org go"))
    assert api.requests[0][1]["task"] == "fix the tests"


async def test_edit_reference_only(maubot_plugin, maubot_test_bot, api):
    bot = maubot_test_bot
    bot.events["$edit"] = Event.deserialize({"room_id": "!room:example.org", **edit_event("v2")})
    bot.events[NOTE_ID] = note(unsigned={"m.relations": {"m.replace": {"event_id": "$edit"}}})
    await send(bot, message("@meeseeks:example.org go"))
    assert api.requests[0][1]["task"] == "v2"


async def test_encrypted_edit(maubot_plugin, maubot_test_bot, api):
    edit = {
        "type": "m.room.encrypted",
        "event_id": "$edit",
        "sender": "@bob:example.org",
        "origin_server_ts": 2,
        "content": {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": "x",
            "sender_key": "k",
            "device_id": "D",
            "session_id": "s",
        },
    }
    decrypted = Event.deserialize({"room_id": "!room:example.org", **edit_event("secret v2")})

    class Crypto:
        async def decrypt_megolm_event(self, evt):
            assert evt.event_id == "$edit"
            return decrypted

    maubot_test_bot.client._crypto = Crypto()
    maubot_test_bot.events[NOTE_ID] = note(unsigned={"m.relations": {"m.replace": edit}})
    await send(maubot_test_bot, message("@meeseeks:example.org go"))
    assert api.requests[0][1]["task"] == "secret v2"


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
