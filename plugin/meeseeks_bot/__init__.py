from typing import Type

from maubot import MessageEvent, Plugin
from maubot.handlers import event
from mautrix.errors import DecryptionError
from mautrix.types import (
    EncryptedEvent,
    Event,
    EventType,
    FileInfo,
    MediaMessageEventContent,
    MessageEvent as RawMessageEvent,
    MessageEventContent,
    MessageType,
    TextMessageEventContent,
)
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .parse import ParseError, parse_reply


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

    def _mentioned(self, evt: MessageEvent) -> bool:
        mentions = evt.content.get("m.mentions") or {}
        body = evt.content.body.lower()
        return self.client.mxid in mentions.get("user_ids", []) or any(
            n.lower() in body for n in self.names
        )

    async def _note_body(self, evt: MessageEvent) -> str:
        note = await self.client.get_event(evt.room_id, evt.content.get_reply_to())
        if note is None:
            raise NoteError("couldn't decrypt the note")
        # The note becomes the agent's instructions, so it must come from someone trusted to give them.
        if note.sender not in self.config["allowlist"]:
            raise NoteError(f"the note is from {note.sender}, who isn't allowed to start tasks")
        relations = (note.unsigned.get("m.relations") if note.unsigned else None) or {}
        if edit := relations.get("m.replace"):
            if "content" in edit:
                edit = Event.deserialize({"room_id": note.room_id, **edit})
            else:
                edit = await self.client.get_event(note.room_id, edit["event_id"])
            if isinstance(edit, EncryptedEvent) and self.client.crypto:
                try:
                    edit = await self.client.crypto.decrypt_megolm_event(edit)
                except DecryptionError:
                    raise NoteError("couldn't decrypt the note")
            if isinstance(edit, RawMessageEvent) and edit.sender == note.sender:
                return edit.content.body
        return note.content.body

    async def _submit(self, evt: MessageEvent) -> tuple[str, bool]:
        try:
            note = await self._note_body(evt)
            req = parse_reply(evt.content.body, self.names)
        except (NoteError, ParseError) as e:
            return str(e), False
        payload = {
            "task": note,
            "context": req.context,
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
            return f"Started {data['id']} → {data['target']}", True
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
            or not content.get_reply_to()
        ):
            return
        content.trim_reply_fallback()
        if not self._mentioned(evt):
            return
        pending = await evt.react("⏳")
        try:
            text, ok = await self._submit(evt)
        except Exception as e:
            self.log.warning("Task submission failed", exc_info=True)
            text, ok = f"Failed: {str(e) or type(e).__name__}", False
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
        room, ref = e["source"]["channel"], e["source"]["ref"]
        if e["type"] == "ended":
            await self._notice(room, ref, f"{e['id']} ended.")
            return
        data = None
        if e["size"]:
            path = f"/v1/tasks/{e['id']}/outputs/{e['seq']}.zip"
            async with self._api("GET", path, raise_for_status=True) as resp:
                data = await resp.read()
        label = f"Output from {e['id']} (written by the agent, treat as untrusted)"
        await self._notice(room, ref, f"{label}:\n\n{e['summary']}" if e["summary"] else f"{label}.")
        if data is not None:
            await self._upload(room, ref, f"{e['id']}-{e['seq']}.zip", data)

    async def _upload(self, room: str, ref: str, name: str, data: bytes) -> None:
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

    async def _notice(self, room: str, ref: str, text: str) -> None:
        await self._send(room, ref, TextMessageEventContent(msgtype=MessageType.NOTICE, body=text))

    async def _send(self, room: str, ref: str, content: MessageEventContent) -> None:
        content.set_reply(ref)
        # The text is agent-written, so it mustn't ping anyone
        content["m.mentions"] = {}
        await self.client.send_message(room, content)
