#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
"""Build the maubot plugin, <id>-v<version>.mbp, so the same source always gives the same bytes.

    python3 plugin/build.py [--version VERSION] [--out DIR]

Prints the file's name. A .mbp is a zip of maubot.yaml, the plugin's modules and
its extra files, which is all `mbc build` writes. Entries are sorted, carry a
fixed timestamp and mode, and are stored rather than deflated: deflate's bytes
depend on the zlib that writes them, so a rebuild elsewhere could differ (#88).
--version replaces maubot.yaml's version in the archive only, as a pre-release does.
"""

import argparse
import re
import zipfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent
EPOCH = (1980, 1, 1, 0, 0, 0)


def members() -> list[tuple[str, Path | None]]:
    """(name in the archive, source file), with None for a directory entry."""
    found = [("maubot.yaml", PLUGIN / "maubot.yaml"), ("base-config.yaml", PLUGIN / "base-config.yaml"),
             ("LICENSE", PLUGIN.parent / "LICENSE"), ("dragonlet/", None)]
    # Modules only, so a stray file in a working tree, such as an editor's backup, isn't packaged.
    for path in (PLUGIN / "dragonlet").rglob("*.py"):
        found.append((path.relative_to(PLUGIN).as_posix(), path))
    for _, path in found:
        # A symlinked member would package whatever it points to, and still rebuild the same.
        if path is not None and path.resolve() != path:
            raise SystemExit(f"{path} is or is under a symlink")
    return sorted(found)


def build(out_dir: Path, version: str | None = None) -> Path:
    meta = (PLUGIN / "maubot.yaml").read_bytes().decode("utf-8")
    if version:
        meta = re.sub(r"(?m)^version: .*$", f"version: {version}", meta)
    plugin_id = re.search(r"(?m)^id: *(\S+)$", meta)[1]
    plugin_version = re.search(r"(?m)^version: *(\S+)$", meta)[1]
    out = out_dir / f"{plugin_id}-v{plugin_version}.mbp"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for name, source in members():
            info = zipfile.ZipInfo(name, date_time=EPOCH)
            info.create_system = 3  # Unix, so the mode below is read as one
            info.external_attr = (0o40755 << 16) | 0x10 if source is None else 0o100644 << 16
            data = b"" if source is None else meta.encode("utf-8") if name == "maubot.yaml" else source.read_bytes()
            z.writestr(info, data)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Build the maubot plugin reproducibly.")
    p.add_argument("--version", help="the version to give it, in place of maubot.yaml's")
    p.add_argument("--out", type=Path, default=Path.cwd(), help="the directory to write it to")
    args = p.parse_args()
    print(build(args.out, args.version).name)


if __name__ == "__main__":
    main()
