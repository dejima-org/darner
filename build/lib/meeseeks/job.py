# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
"""Job schema shared by the API and the launcher.

Domain and dependency rules mirror dejima's own validators
(bin/dejima-config-lib.sh), so a job that passes here is one dejima accepts.
"""

import base64
import binascii
import re
import secrets
from dataclasses import dataclass, field

MAX_PROMPT = 64 * 1024
MAX_SESSION_WAIT = 60
MAX_FILES = 16
MAX_FILE_BYTES = 1024 * 1024
MAX_GRANTS = 64

_LABEL = r"[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
_DOMAIN = re.compile(rf"{_LABEL}(\.{_LABEL})+")
_DEP = re.compile(
    r"(apt|pip|cargo):[A-Za-z0-9][A-Za-z0-9._+-]*"
    r"|npm:(@[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*(@[A-Za-z0-9][A-Za-z0-9._-]*)?"
)
_TARBALL = re.compile(r"tarball:([A-Za-z0-9][A-Za-z0-9._-]*)@[a-f0-9]{64}:(https://[A-Za-z0-9._~/?=+%-]{1,504})")
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
# dejima's modelid rule: vendor/name, a :variant and a [context] bracket are all real.
_MODEL = re.compile(r"[A-Za-z0-9][][A-Za-z0-9._:/-]{0,199}")
EFFORTS = ("low", "medium", "high", "xhigh")
_ID = re.compile(r"ms-[a-z2-7]{5}")

OUTPUT_INSTRUCTIONS = (
    "When you finish:\n"
    "1. Put anything the requester should receive in ~/shared, but not in ~/shared/uploads, which holds "
    "files they sent you and isn't sent back.\n"
    "2. Write a short summary of what you did and found to ~/shared/RESULT.md.\n"
    "3. Last, run `touch ~/shared/.done`. That file is how the requester learns you have finished, "
    "so the task is not done until it exists.\n"
    "If you are asked to do more afterwards, repeat these steps when that is finished.\n\n"
    "If you need something from the requester to continue, write what you need to ~/shared/HELP.md, "
    "run `touch ~/shared/.help`, and end your turn. Their response will arrive as your next message.\n\n"
    "If you need to reach domains the sandbox blocks, write them to ~/shared/DOMAINS, one per line, with "
    "`internal ` in front of internal ones and `:port` after any you need on a particular port, "
    "then run `touch ~/shared/.domains` and end your turn. A domain is "
    "internal if it's only reachable inside the requester's network, such as a name under a private domain; if one "
    "you were allowed still can't be reached, request it again as internal. The requester is shown the list to "
    "approve, and you will be told what was allowed."
)


class JobError(ValueError):
    pass


@dataclass(frozen=True)
class Job:
    id: str
    prompt: str
    domains: list[str] = field(default_factory=list)
    internal_domains: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    harness: str | None = None
    backend: str | None = None
    model: str | None = None
    effort: str | None = None
    session_wait: int = 0
    files: list[tuple[str, bytes]] = field(default_factory=list)


def new_task_id() -> str:
    return "ms-" + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz234567") for _ in range(5))


def valid_domain(s: str) -> bool:
    return isinstance(s, str) and _DOMAIN.fullmatch(s) is not None


def valid_dependency(s: str) -> bool:
    if not isinstance(s, str):
        return False
    if _DEP.fullmatch(s):
        return True
    m = _TARBALL.fullmatch(s)
    return m is not None and m[1] not in (".", "..")


def valid_model(s: str) -> bool:
    return isinstance(s, str) and _MODEL.fullmatch(s) is not None


def valid_effort(s: str) -> bool:
    return s in EFFORTS


def valid_name(s: str) -> bool:
    return isinstance(s, str) and _NAME.fullmatch(s) is not None


def _str_list(d: dict, key: str, check, what: str) -> list[str]:
    items = d.get(key, [])
    if not isinstance(items, list) or len(items) > MAX_GRANTS:
        raise JobError(f"{key} must be a list of at most {MAX_GRANTS} entries")
    for item in items:
        if not check(item):
            raise JobError(f"not a valid {what}: {item!r}")
    return items


def _optional_name(d: dict, key: str) -> str | None:
    value = d.get(key)
    if value is not None and not valid_name(value):
        raise JobError(f"not a valid {key} name: {value!r}")
    return value


def _files(d: dict) -> list[tuple[str, bytes]]:
    """Files to give the session, as name and contents; names already safe to write as they are."""
    from meeseeks.archive import upload_name

    items = d.get("files", [])
    if not isinstance(items, list) or len(items) > MAX_FILES:
        raise JobError(f"files must be a list of at most {MAX_FILES}")
    files = []
    for item in items:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or upload_name(name) != name or name in [n for n, _ in files]:
            raise JobError(f"not a valid file name: {name!r}")
        try:
            data = base64.b64decode(item.get("data", ""), validate=True)
        except (binascii.Error, TypeError, ValueError):
            raise JobError(f"file {name!r} isn't base64")
        if len(data) > MAX_FILE_BYTES:
            raise JobError(f"file {name!r} is over {MAX_FILE_BYTES} bytes")
        files.append((name, data))
    return files


def _internal_domains(d: dict) -> list[str]:
    internal = _str_list(d, "internal_domains", valid_domain, "internal domain")
    # dejima exempts only a domain the launch also permits.
    missing = [name for name in internal if name not in d.get("domains", [])]
    if missing:
        raise JobError(f"internal domains must also be permitted: {', '.join(missing)}")
    return internal


def validate_job(d: object) -> Job:
    if not isinstance(d, dict):
        raise JobError("job must be an object")
    unknown = set(d) - {
        "id", "prompt", "domains", "internal_domains", "dependencies", "harness", "backend", "model", "effort",
        "session_wait", "files",
    }
    if unknown:
        raise JobError(f"unknown job keys: {', '.join(sorted(unknown))}")
    if not valid_task_id(d.get("id")):
        raise JobError("invalid job id")
    prompt = d.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
        raise JobError(f"prompt must be non-empty text of at most {MAX_PROMPT} characters")
    if "\0" in prompt:
        raise JobError("prompt contains a NUL byte")
    for key, check, what in (("model", valid_model, "model"), ("effort", valid_effort, f"effort ({', '.join(EFFORTS)})")):
        if d.get(key) is not None and not check(d[key]):
            raise JobError(f"not a valid {what}: {d[key]!r}")
    session_wait = d.get("session_wait", 0)
    if type(session_wait) is not int or not 0 <= session_wait <= MAX_SESSION_WAIT:
        raise JobError(f"session_wait must be whole seconds from 0 to {MAX_SESSION_WAIT}")
    return Job(
        id=d["id"],
        prompt=prompt,
        domains=_str_list(d, "domains", valid_domain, "domain"),
        internal_domains=_internal_domains(d),
        dependencies=_str_list(d, "dependencies", valid_dependency, "dependency spec"),
        harness=_optional_name(d, "harness"),
        backend=_optional_name(d, "backend"),
        model=d.get("model"),
        effort=d.get("effort"),
        session_wait=session_wait,
        files=_files(d),
    )


def valid_task_id(s: str) -> bool:
    return isinstance(s, str) and _ID.fullmatch(s) is not None


def compose_prompt(
    task: str,
    blurbs: list[tuple[str, str]],
    context: str,
    output_instructions: str = OUTPUT_INSTRUCTIONS,
    files: list[str] = (),
) -> str:
    parts = [task.strip()]
    parts += [f"## Context: {name}\n{text.strip()}" for name, text in blurbs]
    if context.strip():
        parts.append(f"## Context from requester\n{context.strip()}")
    if files:
        listed = "\n".join(f"- ~/shared/uploads/{name}" for name in files)
        parts.append(f"## Files for this task\nThe requester provided these, and they may hold credentials:\n{listed}")
    if output_instructions.strip():
        parts.append(f"## Delivering your output\n{output_instructions.strip()}")
    return "\n\n".join(parts) + "\n"
