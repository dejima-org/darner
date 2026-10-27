"""Zip a session's ~/shared without trusting anything in it.

The session may be hostile and may change the tree while it is read, so the
walk goes by descriptor and never by path. Each entry is first opened with
O_PATH, which takes a handle without opening the file itself (opening a
device node can have side effects), and its type is checked on that handle.
Only a regular file is then reopened for reading, through /proc/self/fd, which
reaches the same inode that was checked.
"""

import io
import os
import re
import stat
import unicodedata
import zipfile
import zlib
from collections import Counter
from dataclasses import dataclass, field

MANIFEST = "MANIFEST.txt"
# Where requesters' uploads go; they aren't sent back to them.
UPLOADS = "uploads"
SUMMARY = "RESULT.md"
MAX_SUMMARY = 16 * 1024
MANIFEST_SLACK = 2 * 1024 * 1024
_CHUNK = 64 * 1024
_DATE = (1980, 1, 1, 0, 0, 0)

_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ +-]{0,254}")
_RESERVED = re.compile(r"(con|prn|aux|nul|com[1-9]|lpt[1-9]) *(\..*)?", re.IGNORECASE)


class ArchiveError(Exception):
    pass


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 50 * 1024 * 1024
    max_files: int = 1000
    max_entries: int = 10000
    max_depth: int = 16
    max_path: int = 1024


@dataclass
class Manifest:
    included: list[tuple[str, int]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def render(self) -> str:
        lines = ["Included:"] + [f"  {path} ({size} bytes)" for path, size in self.included]
        if self.skipped:
            lines += ["", "Skipped:"] + [f"  {path}: {reason}" for path, reason in self.skipped]
        return "\n".join(lines) + "\n"


def safe_component(name: str) -> bool:
    return (
        _COMPONENT.fullmatch(name) is not None
        and not name.endswith((".", " "))
        and _RESERVED.fullmatch(name) is None
    )


def upload_name(name: str) -> str:
    """A safe file name for a file given to a session, from whatever name it came with."""
    name = name.rsplit("/", 1)[-1].replace(" ", "-")
    name = "".join(c for c in name if c.isascii() and (c.isalnum() or c in "._+-")).lstrip("._+-")[:200]
    return name if safe_component(name) else "upload"


def _entry(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=_DATE)
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _skip_reason(name: str, path: str, collisions: set[str], limits: Limits) -> str | None:
    try:
        name.encode()
    except UnicodeEncodeError:
        return "name is not valid UTF-8"
    if not safe_component(name):
        return "unsafe name"
    if name.casefold() in collisions:
        return "name differs only in case from another"
    if len(path.encode()) > limits.max_path:
        return "path too long"
    return None


class _Builder:
    def __init__(self, zf: zipfile.ZipFile, limits: Limits):
        self.zf = zf
        self.limits = limits
        self.manifest = Manifest()
        self.files = 0
        self.entries = 0
        self.bytes = 0

    def walk(self, dir_fd: int, prefix: str, depth: int) -> None:
        if depth > self.limits.max_depth:
            raise ArchiveError(f"deeper than {self.limits.max_depth} directories")
        names = []
        with os.scandir(dir_fd) as entries:
            for entry in entries:
                self.entries += 1
                if self.entries > self.limits.max_entries:
                    raise ArchiveError(f"more than {self.limits.max_entries} entries")
                if prefix or entry.name != ".done":
                    names.append(entry.name)
        names.sort()
        collisions = {f for f, n in Counter(n.casefold() for n in names).items() if n > 1}
        for name in names:
            path = prefix + name
            if not prefix and name.casefold() == MANIFEST.casefold():
                self.manifest.skipped.append((repr(path), "reserved name"))
                continue
            if not prefix and name == UPLOADS:
                self.manifest.skipped.append((repr(path), "uploaded by the requester, not sent back"))
                continue
            if reason := _skip_reason(name, path, collisions, self.limits):
                self.manifest.skipped.append((repr(path), reason))
                continue
            try:
                handle = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
            except OSError as e:
                self.manifest.skipped.append((repr(path), f"unreadable ({e.strerror})"))
                continue
            try:
                self._add(handle, path, depth)
            finally:
                os.close(handle)

    def _add(self, handle: int, path: str, depth: int) -> None:
        mode = os.fstat(handle).st_mode
        if stat.S_ISDIR(mode):
            fd = self._reopen(handle, path, os.O_DIRECTORY)
            if fd is None:
                return
            try:
                self.walk(fd, path + "/", depth + 1)
            finally:
                os.close(fd)
        elif stat.S_ISREG(mode):
            self.files += 1
            if self.files > self.limits.max_files:
                raise ArchiveError(f"more than {self.limits.max_files} files")
            fd = self._reopen(handle, path, os.O_NOCTTY)
            if fd is None:
                return
            try:
                size = self._copy(fd, path)
            finally:
                os.close(fd)
            self.manifest.included.append((path, size))
        else:
            self.manifest.skipped.append((repr(path), "symlink" if stat.S_ISLNK(mode) else "not a regular file"))

    def _reopen(self, handle: int, path: str, flags: int) -> int | None:
        try:
            return os.open(f"/proc/self/fd/{handle}", os.O_RDONLY | os.O_CLOEXEC | flags)
        except OSError as e:
            self.manifest.skipped.append((repr(path), f"unreadable ({e.strerror})"))
            return None

    def _copy(self, fd: int, path: str) -> int:
        size = 0
        with self.zf.open(_entry(path), "w") as out:
            while chunk := os.read(fd, _CHUNK):
                size += len(chunk)
                self.bytes += len(chunk)
                if self.bytes > self.limits.max_bytes:
                    raise ArchiveError(f"more than {self.limits.max_bytes} bytes")
                out.write(chunk)
        return size


def read_file(dir_fd: int, name: str, limit: int) -> bytes | None:
    """Read up to limit bytes of a regular file in dir_fd, or None if it isn't one."""
    try:
        handle = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            return None
        try:
            fd = os.open(f"/proc/self/fd/{handle}", os.O_RDONLY | os.O_NOCTTY | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            data = b""
            while len(data) < limit and (chunk := os.read(fd, min(_CHUNK, limit - len(data)))):
                data += chunk
            return data
        finally:
            os.close(fd)
    finally:
        os.close(handle)


def build(root_fd: int, limits: Limits = Limits()) -> tuple[bytes, Manifest]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        builder = _Builder(zf, limits)
        builder.walk(root_fd, "", 0)
        zf.writestr(_entry(MANIFEST), builder.manifest.render())
    return buf.getvalue(), builder.manifest


def clean_text(raw: bytes, limit: int = MAX_SUMMARY) -> str:
    text = raw[:limit].decode(errors="replace")
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf")).strip()


def repack(data: bytes, limits: Limits = Limits()) -> tuple[bytes, str]:
    """Verify an archive from the launcher and rebuild it with our own writer.

    The stored archive is then exactly what was verified, whatever quirks the
    original had for unzip tools to disagree about. Returns it and the summary.
    """
    if len(data) > limits.max_bytes + MANIFEST_SLACK:
        raise ArchiveError("archive too large")
    # Checked before parsing, since ZipFile builds an object per central directory entry.
    if data.count(b"PK\x01\x02") > limits.max_files + 1:
        raise ArchiveError(f"more than {limits.max_files} files")
    try:
        source = zipfile.ZipFile(io.BytesIO(data))
        infos = source.infolist()
    except (zipfile.BadZipFile, ValueError) as e:
        raise ArchiveError(f"not a valid zip: {e}")
    if len(infos) > limits.max_files + 1:
        raise ArchiveError(f"more than {limits.max_files} files")
    seen = set()
    for info in infos:
        name = info.filename
        if (
            name.endswith("/")
            or len(name.encode()) > limits.max_path
            or not all(safe_component(c) for c in name.split("/"))
        ):
            raise ArchiveError(f"unsafe entry name {name!r}")
        if info.external_attr >> 16 not in (0, 0o100644):
            raise ArchiveError(f"entry {name!r} is not a plain file")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ArchiveError(f"entry {name!r} uses an unsupported compression method")
        if name.casefold() in seen:
            raise ArchiveError(f"duplicate entry {name!r}")
        seen.add(name.casefold())

    buf = io.BytesIO()
    summary = b""
    total = 0
    try:
        with zipfile.ZipFile(buf, "w") as out:
            for info in infos:
                with source.open(info) as src, out.open(_entry(info.filename), "w") as dst:
                    while chunk := src.read(_CHUNK):
                        total += len(chunk)
                        if total > limits.max_bytes + MANIFEST_SLACK:
                            raise ArchiveError("archive contents too large")
                        if info.filename == SUMMARY and len(summary) < MAX_SUMMARY:
                            summary += chunk
                        dst.write(chunk)
    except (zipfile.BadZipFile, EOFError, OSError, ValueError, RuntimeError, NotImplementedError, zlib.error) as e:
        raise ArchiveError(f"corrupt archive: {e}")
    return buf.getvalue(), clean_text(summary)
