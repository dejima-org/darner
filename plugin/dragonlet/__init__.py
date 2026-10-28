# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
import html
import re
import time
from typing import Awaitable, Type
from urllib.parse import quote, unquote

from maubot import MessageEvent, Plugin
from maubot.handlers import event
from mautrix.api import Method, Path
from mautrix.errors import DecryptionError
from mautrix.types import (
    EncryptedEvent,
    Event,
    EventType,
    FileInfo,
    Format,
    MediaMessageEventContent,
    MessageEvent as RawMessageEvent,
    MessageEventContent,
    MessageType,
    TextMessageEventContent,
)
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .markdown import render
from .parse import MARKDOWN_LINK, ParseError, parse_reply, strip_mention

_ID = r"dn-[a-z2-7]{5}"
TASK_ID = re.compile(_ID)
# Content keys on the bot's messages. TASK_TAG marks each message about a task, so a reply to any of them can name it.
TAG = "io.branderson.dejima.darner"
TASK_TAG, HELP_TAG, DOMAINS_TAG = f"{TAG}.task", f"{TAG}.help", f"{TAG}.domains"
PILL = re.compile(r'<a\s[^>]*href="https://matrix\.to/#/([^"?]+)[^"]*"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
STOP = re.compile(rf"stop(?:\s+({_ID}))?", re.IGNORECASE)
STOP_HOW = "To stop a session, reply `stop` with a mention to one of my messages about it, or send `stop dn-xxxxx` with a mention."
STATUS = re.compile(rf"status(?:\s+({_ID}))?", re.IGNORECASE)
TELL = re.compile(rf"tell(?:\s+({_ID}))?\s+(\S.*)", re.IGNORECASE | re.DOTALL)
UPLOAD = re.compile(rf"upload\s+({_ID})", re.IGNORECASE)
DESCRIBE = re.compile(r"describe\s+(\S.*)", re.IGNORECASE | re.DOTALL)
MAX_DESCRIBED = 10
# Each blurb shown is cut here, so a reply doesn't fill the room. Lines wrap,
# so characters are capped too.
MAX_BLURB_LINES, MAX_BLURB_CHARS = 20, 1000
STATUS_HOW = "Send `status dn-xxxxx` with a mention, or reply `status` with a mention to one of my messages about the session."
DECIDE_HOW = "To allow or deny domains, reply `allow` or `deny` with a mention to my message listing them."
NO_TASK = "Tell me the task after the mention, or reply to a note to make it the task. Send `help` with a mention for usage."

USAGE = """\
**darner** starts an isolated dejima session for a task.

**Start a task:** reply to a note with a message that mentions me. The note is the task, and the rest of your reply is extra context. Or mention me in a message that isn't a reply, and that message is the task. Keyword lines configure the session:

```
config: <names>    use saved sets of these keywords{configs}; anything else you give overrides them
ctx: <names>       add context blurbs{blurbs}
allow: <domains>   let the session reach these domains
internal: <domains>  let it reach these domains inside your network
deps: <specs>      install dependencies (apt:, pip:, cargo:, npm:, tarball:)
harness: <name>    use this harness instead of the default{harnesses}
backend: <name>    use this backend instead of the harness's default{backends}
model: <name>      use this model instead of the harness's default
effort: <level>    effort level: low, medium, high or xhigh, or none for cline
context_window: <tokens>  cline's context window, for a self-hosted model
```

Values are separated by commas or spaces, and keywords can repeat. For example:

```
{bot} find out why the import job fails since the deploy
ctx: myapp
allow: pypi.org, files.pythonhosted.org
```

**Config sets and blurbs:** with a mention, `list configs` and `list blurbs` name them, and `describe <names>` shows what each config set applies and what each blurb says.

**Self-test:** `selftest` with a mention sends hostile text through the paths an agent's text takes, to check I show it safely.

**Updates:** I reply when the session starts, when it delivers output (a summary and a zip), when it needs something from you, and when it ends. When a help request says "Reply to this message to answer", reply to it and your answer is typed into the session.

**Domains:** when the agent asks to reach domains, I list every one. Reply `allow` or `deny` with a mention to that message.

**Sessions:** with a mention, `sessions` lists the open ones, `status dn-xxxxx` says what one is waiting on, `tell dn-xxxxx <message>` types a message into it, `upload dn-xxxxx` in reply to a file puts the file in its `~/shared/uploads`, and `stop dn-xxxxx` ends it. Replying to any of my messages about a session works without the id.

Only allowlisted users can start tasks, and the note must be from one of them too. Anything the agent writes is untrusted.
"""


class NoteError(Exception):
    pass


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("allowlist")
        helper.copy("api_url")
        helper.copy("api_token")
        helper.copy("poll_interval")


class Dragonlet(Plugin):
    async def start(self) -> None:
        self.config.load_and_update()
        self.sched.run_periodically(self.config["poll_interval"], self.poll_once)

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    def _api(self, method: str, path: str, **kwargs):
        return self.http.request(
            method,
            f"{self.config['api_url'].rstrip('/')}{path}",
            headers={"Authorization": f"Bearer {self.config['api_token']}"},
            **kwargs,
        )

    def _names(self, evt: MessageEvent) -> list[str]:
        """The bot's user ID, plus the text of any pill or markdown link to it.

        A pill's plain-text body is its visible text, usually the bot's display
        name in that room, so that's what's stripped from the body.
        """
        names = [self.client.mxid]
        for target, text in PILL.findall(evt.content.formatted_body or ""):
            if unquote(target) == self.client.mxid:
                names.append(html.unescape(re.sub(r"<[^>]+>", "", text)).strip())
        for text, target in MARKDOWN_LINK.findall(evt.content.body):
            linked = re.search(r"(@[^/?#\s]+)(?:\?.*)?$", unquote(target))
            if linked and linked[1] == self.client.mxid:
                names.append(text.strip())
        return names

    def _mentioned(self, evt: MessageEvent, names: list[str]) -> bool:
        """Whether the message mentions the bot directly: a pill, or its user ID written out.

        Not its display name, which is also a word people use about it, and not
        m.mentions alone, which clients fill in for every reply to the bot.
        """
        return len(names) > 1 or re.search(
            rf"(?<![\w@]){re.escape(self.client.mxid)}(?![\w-]|[.:]\w)", evt.content.body, re.IGNORECASE
        ) is not None

    async def _note_body(self, note: Event | None) -> str:
        if note is None:
            raise NoteError("couldn't decrypt the note")
        # The note becomes the agent's instructions, so it must come from someone trusted to give them.
        if note.sender not in self.config["allowlist"]:
            raise NoteError(f"the note is from {note.sender}, who isn't allowed to start tasks")
        edited = await self._latest_edit(note)
        return note.content.body if edited is None else edited

    async def _latest_edit(self, note: Event) -> str | None:
        """The text of the note's latest edit by its author, if it has one.

        Asked of the relations endpoint rather than read from the event's
        bundled relations, which a decrypted event doesn't carry.
        """
        path = Path.v1.rooms[note.room_id].relations[note.event_id]["m.replace"]
        try:
            resp = await self.client.api.request(Method.GET, path, query_params={"dir": "b", "limit": "50"})
        except Exception as e:
            raise NoteError(f"couldn't check the note for edits: {e}")
        for raw in resp.get("chunk", []):
            edit = Event.deserialize({"room_id": note.room_id, **raw})
            if edit.sender != note.sender:
                continue
            if isinstance(edit, EncryptedEvent):
                try:
                    edit = await self.client.crypto.decrypt_megolm_event(edit)
                except (DecryptionError, AttributeError):
                    raise NoteError("couldn't decrypt the note's latest edit")
            if isinstance(edit, RawMessageEvent):
                return edit.content.body
        return None

    async def _submit(self, evt: MessageEvent, replied: Event | None = None) -> tuple[str, bool]:
        """Start a task: a reply's note is the task, otherwise the message itself is."""
        try:
            req = parse_reply(evt.content.body, self._names(evt))
            if evt.content.get_reply_to():
                task, context = await self._note_body(replied), req.context
            elif req.context:
                task, context = req.context, ""
            else:
                return NO_TASK, False
        except (NoteError, ParseError) as e:
            return str(e), False
        payload = {
            "task": task,
            "context": context,
            "blurbs": req.blurbs,
            "configs": req.configs,
            "grants": {"domains": req.domains, "internal": req.internal_domains, "dependencies": req.dependencies},
            "requester": evt.sender,
            "source": {"kind": "matrix", "ref": evt.event_id, "channel": evt.room_id},
        }
        for key in ("harness", "backend", "model", "effort", "context_window"):
            if value := getattr(req, key):
                payload[key] = value
        async with self._api("POST", "/v1/tasks", json=payload) as resp:
            data = await resp.json(content_type=None)
        if resp.status in (200, 201):
            text = f"Started {data['id']} → {data['target']}"
            if data.get("link"):
                text += f"\nOpen the session: {data['link']}"
            content = TextMessageEventContent(msgtype=MessageType.TEXT, body=text)
            content[TASK_TAG] = data["id"]
            return content, True
        error = data.get("error") if isinstance(data, dict) else None
        return f"Failed: {error or f'HTTP {resp.status}'}", False

    @event.on(EventType.ROOM_MESSAGE)
    async def handle(self, evt: MessageEvent) -> None:
        content = evt.content
        if evt.sender == self.client.mxid or content.get_edit() or evt.sender not in self.config["allowlist"]:
            return
        if _is_file(content):
            # A file sent as a reply to the bot about a session is for that session.
            if task := await self._replied_task(evt):
                await self._respond(evt, self._upload_to(evt, task, evt))
            return
        if not isinstance(content, TextMessageEventContent):
            return
        content.trim_reply_fallback()
        names = self._names(evt)
        mentioned = self._mentioned(evt, names)
        command = strip_mention(content.body, names).strip()
        if mentioned and command.lower() == "help":
            await evt.reply(await self._usage())
            return
        if mentioned and (stop := STOP.fullmatch(command)):
            await self._respond(evt, self._stop(evt, stop[1] and stop[1].lower()))
            return
        if mentioned and command.lower() in ("allow", "deny"):
            await self._respond(evt, self._decide_domains(evt, command.lower()))
            return
        if mentioned and (describe := DESCRIBE.fullmatch(command)):
            await self._respond(evt, self._describe(list(dict.fromkeys(re.split(r"[,\s]+", describe[1].strip())))))
            return
        if mentioned and command.lower() == "selftest":
            await self._respond(evt, self._selftest(evt))
            return
        if mentioned and command.lower() in ("list blurbs", "list configs"):
            await self._respond(evt, self._list(command.lower().split()[1]))
            return
        if mentioned and command.lower() in ("sessions", "list", "list sessions"):
            await self._respond(evt, self._sessions())
            return
        if mentioned and (status := STATUS.fullmatch(command)):
            await self._respond(evt, self._status(evt, status[1] and status[1].lower()))
            return
        if mentioned and (upload := UPLOAD.fullmatch(command)):
            await self._respond(evt, self._upload_replied_file(evt, upload[1].lower()))
            return
        if mentioned and (tell := TELL.fullmatch(command)):
            # Only a command when it names a session; otherwise "tell me ..." is a task.
            task = tell[1] and tell[1].lower() or await self._replied_task(evt)
            if task:
                await self._respond(evt, self._tell(evt, task, tell[2].strip()))
                return
        if not content.get_reply_to():
            if mentioned:
                await self._respond(evt, self._submit(evt))
            return
        try:
            replied = await self.client.get_event(evt.room_id, content.get_reply_to())
        except Exception as e:
            self.log.warning("Couldn't fetch the replied-to event", exc_info=True)
            if mentioned:
                await evt.reply(_failure(e), markdown=False)
                await evt.react("❌")
            return
        if task := self._help_task(replied):
            await self._respond(evt, self._answer(evt, task))
        elif mentioned and (task := self._tagged_task(replied)):
            # A reply to the bot about a session is for that session, not a new task.
            if command:
                await self._respond(evt, self._tell(evt, task, command))
            else:
                await evt.reply(f"Say what to tell {task} after the mention.", markdown=False)
        elif mentioned:
            await self._respond(evt, self._submit(evt, replied))

    async def _usage(self) -> str:
        try:
            async with self._api("GET", "/v1/blurbs", raise_for_status=True) as resp:
                blurbs = await resp.json()
        except Exception:
            self.log.warning("Couldn't list context blurbs", exc_info=True)
            blurbs = None
        listed = f" (available: {', '.join(blurbs) or 'none'})" if isinstance(blurbs, list) else ""
        try:
            async with self._api("GET", "/v1/catalog", raise_for_status=True) as resp:
                catalog = await resp.json()
        except Exception:
            self.log.warning("Couldn't list harnesses and backends", exc_info=True)
            catalog = {}
        harnesses = backends = ""
        if catalog.get("harnesses"):
            harnesses = f" ({', '.join(catalog['harnesses'])}"
            harnesses += f"; default {catalog['default_harness']})" if catalog.get("default_harness") else ")"
        if catalog.get("backends"):
            backends = f" ({', '.join(catalog['backends'])})"
        try:
            async with self._api("GET", "/v1/configs", raise_for_status=True) as resp:
                configs = await resp.json()
        except Exception:
            self.log.warning("Couldn't list config sets", exc_info=True)
            configs = None
        configs = f" (available: {', '.join(configs) or 'none'})" if isinstance(configs, list) else ""
        return USAGE.format(blurbs=listed, harnesses=harnesses, backends=backends, configs=configs, bot=self.client.mxid)

    def _help_task(self, replied: Event | None) -> str | None:
        # Only the bot's own tag counts, so a user can't steer an answer into another task
        if not isinstance(replied, RawMessageEvent) or replied.sender != self.client.mxid:
            return None
        tag = replied.content.get(HELP_TAG)
        task = tag.get("task") if isinstance(tag, dict) else None
        return task if isinstance(task, str) and TASK_ID.fullmatch(task) else None

    def _tagged_task(self, replied: Event | None) -> str | None:
        """The task one of the bot's own messages is about."""
        if not isinstance(replied, RawMessageEvent) or replied.sender != self.client.mxid:
            return None
        task = replied.content.get(TASK_TAG)
        return task if isinstance(task, str) and TASK_ID.fullmatch(task) else self._help_task(replied)

    def _domain_request(self, replied: Event | None) -> tuple[str, int] | None:
        """The task and request number of one of the bot's own domain request messages."""
        if not isinstance(replied, RawMessageEvent) or replied.sender != self.client.mxid:
            return None
        tag = replied.content.get(DOMAINS_TAG)
        if not isinstance(tag, dict):
            return None
        task, number = tag.get("task"), tag.get("request")
        if isinstance(task, str) and TASK_ID.fullmatch(task) and type(number) is int:
            return task, number
        return None

    async def _decide_domains(self, evt: MessageEvent, verb: str) -> tuple[str, bool]:
        found = None
        if evt.content.get_reply_to():
            found = self._domain_request(await self.client.get_event(evt.room_id, evt.content.get_reply_to()))
        if found is None:
            return DECIDE_HOW, False
        task, number = found
        path = f"/v1/tasks/{task}/domains/{number}/{verb}"
        async with self._api("POST", path, json={"requester": evt.sender}) as resp:
            data = await resp.json(content_type=None)
        if not resp.ok:
            error = data.get("error") if isinstance(data, dict) else None
            return f"Failed: {error or f'HTTP {resp.status}'}", False
        if verb == "allow":
            text = f"Allowed for {task}: {', '.join(data['granted']) or 'none'}."
            for failure in data.get("failed", []):
                text += f"\nCouldn't allow {failure['domain']}: {failure['error']}"
            for check in data.get("unverified", []):
                text += f"\n{check['domain']} is allowed, but didn't answer on port 443 from the session: {check['warning']}"
        else:
            text = f"Denied the domain request from {task}."
        if not data.get("told"):
            text += "\nTell the agent in its session that it can carry on."
        return text, True

    async def _replied_task(self, evt: MessageEvent) -> str | None:
        """The task of the bot's own message this one replies to, if any."""
        if not evt.content.get_reply_to():
            return None
        try:
            return self._tagged_task(await self.client.get_event(evt.room_id, evt.content.get_reply_to()))
        except Exception:
            return None

    async def _sessions(self) -> tuple[str, bool]:
        async with self._api("GET", "/v1/tasks", raise_for_status=True) as resp:
            sessions = await resp.json()
        if not sessions:
            return "No open sessions.", True
        return "Open sessions:\n\n" + "\n".join(f"- {_session_line(s)}" for s in sessions), True

    async def _status(self, evt: MessageEvent, task: str | None) -> tuple[str | TextMessageEventContent, bool]:
        task = task or await self._replied_task(evt)
        if task is None:
            return STATUS_HOW, False
        async with self._api("GET", f"/v1/tasks/{task}") as resp:
            if resp.status == 404:
                return f"{task} isn't an open session.", False
            resp.raise_for_status()
            session = await resp.json()
        return _status_notice(session), True

    async def _selftest(self, evt: MessageEvent) -> tuple[str, bool]:
        payload = {"requester": evt.sender, "source": {"kind": "matrix", "ref": evt.event_id, "channel": evt.room_id}}
        async with self._api("POST", "/v1/selftest", json=payload) as resp:
            data = await resp.json(content_type=None)
        if not resp.ok:
            return f"Failed: {data.get('error') if isinstance(data, dict) else f'HTTP {resp.status}'}", False
        return (
            f"Self-test {data['id']}: its output, help request and waiting notice follow, written like a hostile"
            " agent's. Each numbered line says what you should see.",
            True,
        )

    async def _list(self, what: str) -> tuple[str, bool]:
        kind = {"blurbs": "context blurbs", "configs": "config sets"}[what]
        async with self._api("GET", f"/v1/{what}", raise_for_status=True) as resp:
            names = await resp.json()
        if not names:
            return f"There are no {kind}.", True
        return f"{kind.capitalize()}: {', '.join(names)}. Send `describe <names>` with a mention to see them.", True

    async def _describe(self, names: list[str]) -> tuple[str | TextMessageEventContent, bool]:
        """Each name's config set, then its blurb, for every name that has either."""
        if len(names) > MAX_DESCRIBED:
            return f"I can describe up to {MAX_DESCRIBED} at once.", False
        sections, missing = [], []
        for name in names:
            found = False
            for kind, path in (("config set", "configs"), ("blurb", "blurbs")):
                async with self._api("GET", f"/v1/{path}/{quote(name, safe='')}") as resp:
                    if resp.status == 404:
                        continue
                    data = await resp.json(content_type=None)
                    if not resp.ok:
                        return f"Failed: {data.get('error') if isinstance(data, dict) else f'HTTP {resp.status}'}", False
                sections.append((name, kind, data))
                found = True
            if not found:
                missing.append(name)
        return _described_notice(sections, missing), bool(sections) and not missing

    async def _upload_replied_file(self, evt: MessageEvent, task: str) -> tuple[str, bool]:
        file_event = None
        if evt.content.get_reply_to():
            file_event = await self.client.get_event(evt.room_id, evt.content.get_reply_to())
        if not isinstance(file_event, RawMessageEvent) or not _is_file(file_event.content):
            return f"To upload a file to {task}, reply `upload {task}` with a mention to the file.", False
        return await self._upload_to(evt, task, file_event)

    async def _upload_to(self, evt: MessageEvent, task: str, file_event: RawMessageEvent) -> tuple[str, bool]:
        content = file_event.content
        if content.file:
            # Imported here because mautrix.crypto needs python-olm
            from mautrix.crypto.attachments import decrypt_attachment

            data = await self.client.download_media(content.file.url)
            data = decrypt_attachment(data, content.file.key.key, content.file.hashes["sha256"], content.file.iv)
        else:
            data = await self.client.download_media(content.url)
        name = content.get("filename") or content.body or "upload"
        params = {"name": name, "requester": evt.sender}
        async with self._api("POST", f"/v1/tasks/{task}/files", params=params, data=data) as resp:
            result = await resp.json(content_type=None)
        if not resp.ok:
            error = result.get("error") if isinstance(result, dict) else None
            return f"Failed: {error or f'HTTP {resp.status}'}", False
        text = f"Uploaded {name} to {task} as {result['path']}."
        if not result.get("told"):
            text += "\nTell the agent in its session where to find it."
        return text, True

    async def _tell(self, evt: MessageEvent, task: str, text: str) -> tuple[str | None, bool]:
        payload = {"text": text, "requester": evt.sender}
        async with self._api("POST", f"/v1/tasks/{task}/message", json=payload) as resp:
            if resp.ok:
                return None, True
            data = await resp.json(content_type=None)
        error = data.get("error") if isinstance(data, dict) else None
        return f"Failed: {error or f'HTTP {resp.status}'}", False

    async def _stop(self, evt: MessageEvent, task: str | None) -> tuple[str, bool]:
        if task is None and evt.content.get_reply_to():
            task = self._tagged_task(await self.client.get_event(evt.room_id, evt.content.get_reply_to()))
        if task is None:
            return STOP_HOW, False
        async with self._api("POST", f"/v1/tasks/{task}/stop") as resp:
            if resp.ok:
                return f"Stopped {task}.", True
            data = await resp.json(content_type=None)
        error = data.get("error") if isinstance(data, dict) else None
        return f"Failed: {error or f'HTTP {resp.status}'}", False

    async def _answer(self, evt: MessageEvent, task: str) -> tuple[str | None, bool]:
        text = strip_mention(evt.content.body, self._names(evt)).strip()
        if not text:
            return "the answer is empty", False
        payload = {"text": text, "requester": evt.sender}
        async with self._api("POST", f"/v1/tasks/{task}/answer", json=payload) as resp:
            if resp.ok:
                return None, True
            data = await resp.json(content_type=None)
        error = data.get("error") if isinstance(data, dict) else None
        return f"Failed: {error or f'HTTP {resp.status}'}", False

    async def _respond(
        self, evt: MessageEvent, action: Awaitable[tuple[str | TextMessageEventContent | None, bool]]
    ) -> None:
        pending = await evt.react("⏳")
        try:
            text, ok = await action
        except Exception as e:
            self.log.warning("Request failed", exc_info=True)
            text, ok = _failure(e), False
        if text:
            await evt.reply(text, markdown=False)
        await evt.react("✅" if ok else "❌")
        await self.client.redact(evt.room_id, pending)

    async def poll_once(self) -> None:
        async with self._api("GET", "/v1/events", raise_for_status=True) as resp:
            events = await resp.json()
        for e in events:
            try:
                await self._deliver(e)
                async with self._api("POST", f"/v1/events/{e['seq']}/ack", raise_for_status=True):
                    pass
            except Exception:
                self.log.warning("Couldn't deliver event %s", e.get("seq"), exc_info=True)

    async def _deliver(self, e: dict) -> None:
        room, ref = e["source"]["channel"], (e["source"]["ref"], e["id"])
        if e["type"] == "ended":
            await self._notice(room, ref, f"{e['id']} ended.")
            return
        if e["type"] == "help":
            await self._help(room, ref, e)
            return
        if e["type"] == "waiting":
            await self._send(room, ref, _waiting_notice(e))
            return
        if e["type"] == "domains":
            await self._send(room, ref, _domains_notice(e["id"], e["data"]))
            return
        data = None
        if e["size"]:
            path = f"/v1/tasks/{e['id']}/outputs/{e['seq']}.zip"
            async with self._api("GET", path, raise_for_status=True) as resp:
                data = await resp.read()
        unsignalled = ", collected without the agent's done signal" if e.get("signalled") is False else ""
        label = f"Output from {e['id']}{unsignalled} (written by the agent, treat as untrusted)"
        await self._send(room, ref, _agent_notice(label, e["summary"]))
        if data is not None:
            await self._upload(room, ref, f"{e['id']}-{e['seq']}.zip", data)

    async def _help(self, room: str, ref: tuple[str, str], e: dict) -> None:
        label = f"{e['id']} needs help (written by the agent, treat as untrusted)"
        link = e.get("link")
        if e["answerable"]:
            footer = "Reply to this message to answer" + (f", or open the session: {link}" if link else ".")
        elif link:
            footer = f"Open the session to respond: {link}"
        else:
            target = e["target"] or f"the {e['id']} session"
            footer = f"Attach to {target} to respond."
        content = _agent_notice(label, e["summary"], footer)
        if e["answerable"]:
            content[HELP_TAG] = {"task": e["id"]}
        await self._send(room, ref, content)

    async def _upload(self, room: str, ref: tuple[str, str], name: str, data: bytes) -> None:
        try:
            limit = (await self.client.get_media_repo_config()).upload_size
        except Exception:
            self.log.warning("Couldn't fetch the media repo config", exc_info=True)
            limit = None
        if limit and len(data) > limit:
            text = f"{name} is {len(data)} bytes, over this homeserver's upload limit of {limit}."
            await self._notice(room, ref, text)
            return
        content = MediaMessageEventContent(
            msgtype=MessageType.FILE,
            body=name,
            filename=name,
            info=FileInfo(mimetype="application/zip", size=len(data)),
        )
        if await self.client.state_store.is_encrypted(room):
            # Imported here because mautrix.crypto needs python-olm
            from mautrix.crypto.attachments import encrypt_attachment

            data, content.file = encrypt_attachment(data)
            content.file.url = await self.client.upload_media(data, "application/octet-stream", name)
        else:
            content.url = await self.client.upload_media(data, "application/zip", name)
        await self._send(room, ref, content)

    async def _notice(self, room: str, ref: tuple[str, str], text: str) -> None:
        await self._send(room, ref, TextMessageEventContent(msgtype=MessageType.NOTICE, body=text))

    async def _send(self, room: str, ref: tuple[str, str], content: MessageEventContent) -> None:
        """Reply to the event a task came from, marking the message as about that task."""
        event_id, task = ref
        content.set_reply(event_id)
        content[TASK_TAG] = task
        # The text is agent-written, so it mustn't ping anyone
        content["m.mentions"] = {}
        await self.client.send_message(room, content)


def _is_file(content) -> bool:
    return isinstance(content, MediaMessageEventContent) and content.msgtype in (
        MessageType.FILE, MessageType.IMAGE, MessageType.VIDEO, MessageType.AUDIO
    )


def _age(started: float) -> str:
    minutes = max(0, int(time.time() - started) // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours < 24 else f"{hours // 24}d{hours % 24}h"


def _waiting_on(session: dict) -> str:
    if session["waiting"]["answer"] is not None:
        return "waiting for an answer"
    if session["waiting"]["domains"] is not None:
        return "waiting on a domain request"
    return {"running": "working", "starting": "starting"}.get(session["state"], "state unknown")


def _session_line(session: dict) -> str:
    where = session["link"] or session["target"]
    return f"{session['id']}: {_waiting_on(session)}, started {_age(session['started'])} ago by {session['requester']} ({where})"


def _described_notice(sections: list[tuple[str, str, object]], missing: list[str]) -> TextMessageEventContent:
    """Config sets as the keyword lines they apply, and blurbs as their text."""
    body, html_body = [], []
    for name, kind, data in sections:
        if kind == "config set":
            text = "\n".join(
                f"{key}: {', '.join(value) if isinstance(value, list) else value}" for key, value in data.items()
            ) or "(nothing)"
            note = "Each file: is put in the session's ~/shared/uploads; its contents aren't shown here." if "file" in data else ""
        else:
            text = "\n".join(data.strip().splitlines()[:MAX_BLURB_LINES])[:MAX_BLURB_CHARS]
            note = "Cut short here; the whole blurb is in the API's blurbs directory." if text != data.strip() else ""
        head = f"{name} ({kind})"
        body.append(f"{head}\n\n" + "\n".join(f"    {line}" for line in text.splitlines()) + (f"\n\n{note}" if note else ""))
        html_body.append(
            f"<p><strong>{html.escape(name)}</strong> ({kind})</p><pre><code>{html.escape(text)}</code></pre>"
            + (f"<p>{html.escape(note)}</p>" if note else "")
        )
    if missing:
        line = f"No config set or blurb called {', '.join(missing)}. Send `help` with a mention to list them."
        body.append(line)
        html_body.append(f"<p>{html.escape(line)}</p>")
    return TextMessageEventContent(
        msgtype=MessageType.NOTICE, body="\n\n".join(body), format=Format.HTML, formatted_body="".join(html_body)
    )


def _status_notice(session: dict) -> TextMessageEventContent:
    """What a session is waiting on, quoting the agent's own request where there is one."""
    task, waiting = session["id"], session["waiting"]
    if waiting["answer"] is not None:
        footer = (
            f"Reply to its help request, or send `tell {task} <answer>` with a mention."
            if session["answerable"]
            else f"Answer it in the session: {session['link'] or session['target']}"
        )
        content = _agent_notice(f"{_session_line(session)}. Its help request (written by the agent, treat as untrusted)", waiting["answer"], footer)
    elif waiting["domains"] is not None:
        content = _domains_notice(task, waiting["domains"])
        content.body = f"{_session_line(session)}.\n\n{content.body}"
        content.formatted_body = f"<p>{html.escape(_session_line(session))}.</p>{content.formatted_body}"
    else:
        line = f"{_session_line(session)}. It isn't waiting on you."
        content = TextMessageEventContent(msgtype=MessageType.NOTICE, body=line, format=Format.HTML, formatted_body=f"<p>{html.escape(line)}</p>")
    content[TASK_TAG] = task
    content["m.mentions"] = {}
    return content


def _waiting_notice(e: dict) -> TextMessageEventContent:
    """A session that has gone quiet, with the end of its screen if the API sent it."""
    quiet = (e.get("data") or {}).get("quiet", 0)
    head = f"{e['id']} seems to be waiting: its screen hasn't changed for {_age(time.time() - quiet)}."
    foot = "Reply with a mention to tell it something"
    foot += f", or open the session: {e['link']}" if e.get("link") else "."
    body, html_body = head, f"<p>{html.escape(head)}</p>"
    if e["summary"]:
        label = "The end of its screen (written by the agent, treat as untrusted):"
        body += f"\n\n{label}\n\n" + "\n".join(f"    {line}" for line in e["summary"].splitlines())
        html_body += f"<p>{html.escape(label)}</p><pre><code>{html.escape(e['summary'])}</code></pre>"
    body += f"\n\n{foot}"
    html_body += f"<p>{html.escape(foot)}</p>"
    return TextMessageEventContent(msgtype=MessageType.NOTICE, body=body, format=Format.HTML, formatted_body=html_body)


def _domains_notice(task: str, request: dict) -> TextMessageEventContent:
    """List every requested domain for the requester to read before allowing any."""
    lines = []
    for d in request["domains"]:
        note = " (internal)" if d["internal"] else ""
        note += f" (checked on port {d['port']})" if d.get("port") else ""
        note += " (refused by policy, won't be allowed)" if d["denied"] else ""
        lines.append((d["domain"], note))
    if lines:
        label = f"{task} asks to reach these domains (requested by the agent, check each one before allowing)"
        body = f"{label}:\n\n" + "\n".join(f"- {name}{note}" for name, note in lines)
        items = "".join(f"<li><code>{html.escape(name)}</code>{html.escape(note)}</li>" for name, note in lines)
        html_body = f"<p>{html.escape(label)}:</p><ul>{items}</ul>"
    else:
        body = f"{task} sent a domain request with no valid domains."
        html_body = f"<p>{html.escape(body)}</p>"
    footer = []
    if request.get("rejected"):
        footer.append(f"{request['rejected']} line(s) in the request weren't valid domains and were dropped.")
    if any(not d["denied"] for d in request["domains"]):
        footer.append("Reply `allow` with a mention to allow them, or `deny`.")
    else:
        footer.append("None of them can be allowed. Reply `deny` with a mention to tell the agent.")
    for line in footer:
        body += f"\n\n{line}"
        html_body += f"<p>{html.escape(line)}</p>"
    content = TextMessageEventContent(msgtype=MessageType.NOTICE, body=body, format=Format.HTML, formatted_body=html_body)
    content[DOMAINS_TAG] = {"task": task, "request": request["request"]}
    return content


def _agent_notice(label: str, text: str, footer: str = "") -> TextMessageEventContent:
    """A notice quoting agent-written markdown, rendered so it can't mislead."""
    if text:
        body, html_body = f"{label}:\n\n{text}", f"<p>{html.escape(label)}:</p>{render(text)}"
    else:
        body, html_body = f"{label}.", f"<p>{html.escape(label)}.</p>"
    if footer:
        body += f"\n\n{footer}"
        html_body += f"<p>{html.escape(footer)}</p>"
    return TextMessageEventContent(msgtype=MessageType.NOTICE, body=body, format=Format.HTML, formatted_body=html_body)


def _failure(e: Exception) -> str:
    return f"Failed: {str(e) or type(e).__name__}"
