import re
from dataclasses import dataclass, field

KEYWORD = re.compile(r"^\s*(ctx|allow|deps|harness|backend)\s*:(.*)$", re.IGNORECASE)
FIELDS = {"ctx": "blurbs", "allow": "domains", "deps": "dependencies"}


class ParseError(ValueError):
    pass


@dataclass
class Request:
    context: str = ""
    blurbs: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    harness: str | None = None
    backend: str | None = None


def _mention(names: list[str]) -> str:
    alts = "|".join(re.escape(n.lstrip("@")) for n in sorted(names, key=len, reverse=True) if n)
    return rf"(?<!\w)@?(?:{alts})(?!\w):?[ \t]*"


def strip_mention(text: str, bot_names: list[str]) -> str:
    if not any(bot_names):
        return text
    return re.sub(rf"^\s*(?:{_mention(bot_names)})+", "", text, flags=re.IGNORECASE)


def parse_reply(body: str, bot_names: list[str]) -> Request:
    lines = body.splitlines()
    while lines and lines[0].startswith(">"):
        lines.pop(0)
    mention = _mention(bot_names) if any(bot_names) else None
    values = {"ctx": [], "allow": [], "deps": [], "harness": [], "backend": []}
    context = []
    for line in lines:
        line = strip_mention(line, bot_names)
        if m := KEYWORD.match(line):
            seen = values[m[1].lower()]
            seen += [v for v in dict.fromkeys(re.split(r"[,\s]+", m[2])) if v and v not in seen]
        else:
            context.append(re.sub(mention, "", line, flags=re.IGNORECASE) if mention else line)
    for key in ("harness", "backend"):
        if len(values[key]) > 1:
            raise ParseError(f"{key} takes one value")
    return Request(
        context="\n".join(context).strip(),
        harness=next(iter(values["harness"]), None),
        backend=next(iter(values["backend"]), None),
        **{FIELDS[k]: values[k] for k in FIELDS},
    )
