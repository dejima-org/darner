"""Job schema shared by the API and the launcher.

Domain and dependency rules mirror dejima's own validators
(bin/dejima-config-lib.sh), so a job that passes here is one dejima accepts.
"""

import re
import secrets
from dataclasses import dataclass, field

MAX_PROMPT = 64 * 1024
MAX_SESSION_WAIT = 60
MAX_GRANTS = 64

_LABEL = r"[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
_DOMAIN = re.compile(rf"{_LABEL}(\.{_LABEL})+")
_DEP = re.compile(
    r"(apt|pip|cargo):[A-Za-z0-9][A-Za-z0-9._+-]*"
    r"|npm:(@[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*(@[A-Za-z0-9][A-Za-z0-9._-]*)?"
)
_TARBALL = re.compile(r"tarball:([A-Za-z0-9][A-Za-z0-9._-]*)@[a-f0-9]{64}:(https://[A-Za-z0-9._~/?=+%-]{1,504})")
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_ID = re.compile(r"ms-[a-z2-7]{5}")

OUTPUT_INSTRUCTIONS = (
    "When you finish:\n"
    "1. Put anything the requester should receive in ~/shared.\n"
    "2. Write a short summary of what you did and found to ~/shared/RESULT.md.\n"
    "3. Last, run `touch ~/shared/.done`. That file is how the requester learns you have finished, "
    "so the task is not done until it exists.\n"
    "If you are asked to do more afterwards, repeat these steps when that is finished.\n\n"
    "If you need something from the requester to continue, write what you need to ~/shared/HELP.md, "
    "run `touch ~/shared/.help`, and end your turn. Their response will arrive as your next message.\n\n"
    "If you need to reach domains the sandbox blocks, write them to ~/shared/DOMAINS, one per line, with "
    "`internal ` in front of internal ones, then run `touch ~/shared/.domains` and end your turn. A domain is "
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
    dependencies: list[str] = field(default_factory=list)
    harness: str | None = None
    backend: str | None = None
    session_wait: int = 0


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


def validate_job(d: object) -> Job:
    if not isinstance(d, dict):
        raise JobError("job must be an object")
    unknown = set(d) - {"id", "prompt", "domains", "dependencies", "harness", "backend", "session_wait"}
    if unknown:
        raise JobError(f"unknown job keys: {', '.join(sorted(unknown))}")
    if not valid_task_id(d.get("id")):
        raise JobError("invalid job id")
    prompt = d.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
        raise JobError(f"prompt must be non-empty text of at most {MAX_PROMPT} characters")
    if "\0" in prompt:
        raise JobError("prompt contains a NUL byte")
    session_wait = d.get("session_wait", 0)
    if type(session_wait) is not int or not 0 <= session_wait <= MAX_SESSION_WAIT:
        raise JobError(f"session_wait must be whole seconds from 0 to {MAX_SESSION_WAIT}")
    return Job(
        id=d["id"],
        prompt=prompt,
        domains=_str_list(d, "domains", valid_domain, "domain"),
        dependencies=_str_list(d, "dependencies", valid_dependency, "dependency spec"),
        harness=_optional_name(d, "harness"),
        backend=_optional_name(d, "backend"),
        session_wait=session_wait,
    )


def valid_task_id(s: str) -> bool:
    return isinstance(s, str) and _ID.fullmatch(s) is not None


def compose_prompt(
    task: str, blurbs: list[tuple[str, str]], context: str, output_instructions: str = OUTPUT_INSTRUCTIONS
) -> str:
    parts = [task.strip()]
    parts += [f"## Context: {name}\n{text.strip()}" for name, text in blurbs]
    if context.strip():
        parts.append(f"## Context from requester\n{context.strip()}")
    if output_instructions.strip():
        parts.append(f"## Delivering your output\n{output_instructions.strip()}")
    return "\n\n".join(parts) + "\n"
