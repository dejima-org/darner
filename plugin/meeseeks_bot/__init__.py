from typing import Type

from maubot import MessageEvent, Plugin
from maubot.handlers import event
from mautrix.errors import DecryptionError
from mautrix.types import (
    EncryptedEvent,
    Event,
    EventType,
    MessageEvent as RawMessageEvent,
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


class MeeseeksBot(Plugin):
    async def start(self) -> None:
        self.config.load_and_update()
        self.names = [self.client.mxid]
        try:
            if name := await self.client.get_displayname(self.client.mxid):
                self.names.append(name)
        except Exception:
            self.log.warning("Couldn't fetch own display name", exc_info=True)

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

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
            "source": {"kind": "matrix", "ref": evt.event_id},
        }
        if req.harness:
            payload["harness"] = req.harness
        if req.backend:
            payload["backend"] = req.backend
        async with self.http.post(
            f"{self.config['api_url'].rstrip('/')}/v1/tasks",
            json=payload,
            headers={"Authorization": f"Bearer {self.config['api_token']}"},
        ) as resp:
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
