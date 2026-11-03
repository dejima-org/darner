# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
"""Config sets: named groups of task keywords, so a recurring task needs only what changes.

A config set is a file of the keyword lines a chat request takes, one per
line, with # comments. Comment lines at its top are its description. A request names config sets, and its own values
override theirs: a single value replaces theirs, and a list adds to theirs.
Config sets' lists combine. Two giving different single values conflict, which
is refused unless the request gives that value itself, so no order between
them decides it.
"""

import re
from pathlib import Path

from darner.job import valid_name

LISTS = {"ctx": "blurbs", "allow": "domains", "internal": "internal", "deps": "dependencies", "file": "files"}
SINGLES = ("harness", "backend", "model", "effort", "context_window", "network")
_LINE = re.compile(r"\s*([A-Za-z_]+)\s*:(.*)")


class ConfigSetError(ValueError):
    pass


def available(configs_dir: Path) -> list[str]:
    if not configs_dir.is_dir():
        return []
    return sorted(p.name for p in configs_dir.iterdir() if valid_name(p.name) and p.is_file())


def load_files(configs_dir: Path, names: list[str], read: bool = True) -> list[tuple[str, bytes]]:
    """The files config sets give a session, from configs_dir/files. Unless read, each is
    checked as it would be, and its contents left empty."""
    from darner.archive import upload_name
    from darner.job import MAX_FILE_BYTES

    files = []
    for name in names:
        path = configs_dir / "files" / name
        # Messages name the file only: they're relayed to chat, and paths are the host's.
        if upload_name(name) != name or not path.is_file():
            raise ConfigSetError(f"config set file {name!r} isn't available")
        try:
            data = path.read_bytes() if read else b""
            size = len(data) if read else path.stat().st_size
        except OSError:
            raise ConfigSetError(f"couldn't read config set file {name!r}")
        if size > MAX_FILE_BYTES:
            raise ConfigSetError(f"config set file {name!r} is over {MAX_FILE_BYTES} bytes")
        files.append((name, data))
    return files


def parse(name: str, text: str) -> dict:
    found = {field: [] for field in LISTS.values()} | {key: None for key in SINGLES}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0]
        if not line.strip():
            continue
        m = _LINE.fullmatch(line)
        key = m and m[1].lower()
        if key not in LISTS and key not in SINGLES:
            raise ConfigSetError(f"config set {name!r} line {number}: expected one of {', '.join([*LISTS, *SINGLES])}")
        values = [v for v in re.split(r"[,\s]+", m[2]) if v]
        if key in SINGLES:
            if len(values) != 1 or found[key] is not None:
                raise ConfigSetError(f"config set {name!r} line {number}: {key} takes one value")
            found[key] = values[0]
        else:
            field = found[LISTS[key]]
            field += [v for v in values if v not in field]
    return found


def description(text: str) -> str | None:
    """The comment lines a config set starts with, as one line, or None."""
    lines = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        lines.append(line[1:].strip())
    return " ".join(line for line in lines if line) or None


def load(configs_dir: Path, names: list, overridden=()) -> dict:
    """The named config sets combined, their lists joined. A single value two of them
    give differently is refused, unless it's in overridden, when it's left unset."""
    combined = {field: [] for field in LISTS.values()} | {key: None for key in SINGLES}
    given = {}
    choices = available(configs_dir)
    for name in names:
        if not isinstance(name, str) or name not in choices:
            raise ConfigSetError(f"unknown config set {name!r} (available: {', '.join(choices) or 'none'})")
        found = parse(name, (configs_dir / name).read_text())
        for field in LISTS.values():
            combined[field] += [v for v in found[field] if v not in combined[field]]
        for key in SINGLES:
            if found[key] is not None:
                given.setdefault(key, {}).setdefault(found[key], []).append(name)
    for key, values in given.items():
        if len(values) == 1:
            combined[key] = next(iter(values))
        elif key not in overridden:
            sides = "; ".join(f"{', '.join(sets)} {'give' if len(sets) > 1 else 'gives'} {value}" for value, sets in values.items())
            raise ConfigSetError(f"config sets disagree on {key}: {sides}. Set {key} for the task to choose")
    return combined
