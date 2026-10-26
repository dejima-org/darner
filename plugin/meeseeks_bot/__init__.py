import html
import re
from typing import Awaitable, Type
from urllib.parse import unquote

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
from .parse import ParseError, parse_reply, strip_mention

TASK_ID = re.compile(r"ms-[a-z2-7]{5}")
# Marks each of the bot's messages about a task, so a reply to any of them can name it.
TASK_TAG = "io.meeseeks.task"
PILL = re.compile(r'<a\s[^>]*href="https://matrix\.to/#/([^"?]+)[^"]*"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
STOP = re.compile(r"stop(?:\s+(ms-[a-z2-7]{5}))?", re.IGNORECASE)
STOP_HOW = "To stop a session, reply `stop` with a mention to one of my messages about it, or send `stop ms-xxxxx` with a mention."
NO_TASK = "Tell me the task after the mention, or reply to a note to make it the task. Send `help` with a mention for usage."

USAGE = """\
**Meeseeks** starts an isolated dejima session for a task.

**Start a task:** reply to a note with a message that mentions me. The note is the task, and the rest of your reply is extra context. Or mention me in a message that isn't a reply, and that message is the task. Keyword lines configure the session:

```
ctx: <names>       add context blurbs{blurbs}
allow: <domains>   let the session reach these domains
deps: <specs>      install dependencies (apt:, pip:, cargo:, npm:, tarball:)
harness: <name>    use this harness instead of the default
backend: <name>    use this backend instead of the harness's default
```

Values are separated by commas or spaces, and keywords can repeat. For example:

```
@meeseeks find out why the import job fails since the deploy
ctx: myapp
allow: pypi.org, files.pythonhosted.org
```

**Updates:** I reply when the session starts, when it delivers output (a summary and a zip), when it needs something from you, and when it ends. When a help request says "Reply to this message to answer", reply to it and your answer is typed into the session.

**Stop a session:** reply `stop` with a mention to any of my messages about it, or send `stop ms-xxxxx` with a mention.

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


class MeeseeksBot(Plugin):
    async def start(self) -> None:
        self.config.load_and_update()
        self.names = [self.client.mxid]
        try:
            if name := await self.client.get_displayname(self.client.mxid):
                self.names.append(name)
        except Exception:
            self.log.warning("Couldn't fetch own display name", exc_info=True)
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
        """The bot's names, plus whatever text a client gave a pill linking to it.

        A pill's plain-text body is its visible text, usually the bot's display
        name in that room, which needn't match the names known at startup.
        """
        names = list(self.names)
        for target, text in PILL.findall(evt.content.formatted_body or ""):
            if unquote(target) == self.client.mxid:
                names.append(html.unescape(re.sub(r"<[^>]+>", "", text)).strip())
        return names

    def _mentioned(self, evt: MessageEvent, names: list[str]) -> bool:
        mentions = evt.content.get("m.mentions") or {}
        body = evt.content.body.lower()
        return self.client.mxid in mentions.get("user_ids", []) or any(
            n and n.lower() in body for n in names
        )

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
            "grants": {"domains": req.domains, "dependencies": req.dependencies},
            "requester": evt.sender,
            "source": {"kind": "matrix", "ref": evt.event_id, "channel": evt.room_id},
        }
        if req.harness:
            payload["harness"] = req.harness
        if req.backend:
            payload["backend"] = req.backend
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
        if (
            evt.sender == self.client.mxid
            or not isinstance(content, TextMessageEventContent)
            or content.get_edit()
            or evt.sender not in self.config["allowlist"]
        ):
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
        return USAGE.format(blurbs=listed)

    def _help_task(self, replied: Event | None) -> str | None:
        # Only the bot's own tag counts, so a user can't steer an answer into another task
        if not isinstance(replied, RawMessageEvent) or replied.sender != self.client.mxid:
            return None
        tag = replied.content.get("io.meeseeks.help")
        task = tag.get("task") if isinstance(tag, dict) else None
        return task if isinstance(task, str) and TASK_ID.fullmatch(task) else None

    def _tagged_task(self, replied: Event | None) -> str | None:
        """The task one of the bot's own messages is about."""
        if not isinstance(replied, RawMessageEvent) or replied.sender != self.client.mxid:
            return None
        task = replied.content.get(TASK_TAG)
        return task if isinstance(task, str) and TASK_ID.fullmatch(task) else self._help_task(replied)

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
            content["io.meeseeks.help"] = {"task": e["id"]}
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
