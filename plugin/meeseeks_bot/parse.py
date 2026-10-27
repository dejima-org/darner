import re
from dataclasses import dataclass, field
from urllib.parse import unquote

MARKDOWN_LINK = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)\)")
KEYWORD = re.compile(r"^\s*(ctx|allow|deps|harness|backend|model|effort)\s*:(.*)$", re.IGNORECASE)
SINGLE = ("harness", "backend", "model", "effort")
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
    model: str | None = None
    effort: str | None = None


def _mention(names: list[str]) -> str:
    alts = "|".join(re.escape(n.lstrip("@")) for n in sorted(names, key=len, reverse=True) if n)
    return rf"(?<!\w)@?(?:{alts})(?!\w):?[ \t]*"


def _unlink_bot(text: str, bot_names: list[str]) -> str:
    """Replace markdown links to the bot with their text.

    Clients write a pill's plain-text body as a markdown link, such as
    `[@bot:example.org](https://matrix.to/#/@bot:example.org)`, so the
    mention is only the link text once the link is gone. Any permalink host
    counts: only the user ID it ends in matters.
    """
    ids = {n for n in bot_names if n.startswith("@")}

    def replace(m: re.Match) -> str:
        target = re.search(r"(@[^/?#\s]+)(?:\?.*)?$", unquote(m[2]))
        return m[1] if target and target[1] in ids else m[0]

    return MARKDOWN_LINK.sub(replace, text) if ids else text


def strip_mention(text: str, bot_names: list[str]) -> str:
    if not any(bot_names):
        return text
    text = _unlink_bot(text, bot_names)
    return re.sub(rf"^\s*(?:{_mention(bot_names)})+", "", text, flags=re.IGNORECASE)


def parse_reply(body: str, bot_names: list[str]) -> Request:
    lines = _unlink_bot(body, bot_names).splitlines()
    while lines and lines[0].startswith(">"):
        lines.pop(0)
    mention = _mention(bot_names) if any(bot_names) else None
    values = {key: [] for key in (*FIELDS, *SINGLE)}
    context = []
    for line in lines:
        line = strip_mention(line, bot_names)
        if m := KEYWORD.match(line):
            seen = values[m[1].lower()]
            seen += [v for v in dict.fromkeys(re.split(r"[,\s]+", m[2])) if v and v not in seen]
        else:
            context.append(re.sub(mention, "", line, flags=re.IGNORECASE) if mention else line)
    for key in SINGLE:
        if len(values[key]) > 1:
            raise ParseError(f"{key} takes one value")
    return Request(
        context="\n".join(context).strip(),
        **{key: next(iter(values[key]), None) for key in SINGLE},
        **{FIELDS[k]: values[k] for k in FIELDS},
    )
