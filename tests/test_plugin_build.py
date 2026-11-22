"""plugin/build.py: the .mbp is a function of the source alone (#88)."""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# Where a link would be rewritten on the way to the mirror, so the mirrored source would build other bytes.
FORGE = "gitbox.branderson.io/dejima/"


def copy(dest: Path) -> Path:
    """The packaged source, and the builder, in a tree of their own."""
    shutil.copytree(ROOT / "plugin", dest / "plugin", ignore=shutil.ignore_patterns("__pycache__", "*.mbp"))
    shutil.copy(ROOT / "LICENSE", dest / "LICENSE")
    return dest


def build(tree: Path, *args: str) -> Path:
    out = tree / "out"
    out.mkdir(exist_ok=True)
    name = subprocess.run(
        [sys.executable, str(tree / "plugin" / "build.py"), "--out", str(out), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return out / name


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_same_source_builds_the_same_bytes_whatever_its_timestamps_or_strays(tmp_path):
    a, b = copy(tmp_path / "a"), copy(tmp_path / "b")
    for path in b.rglob("*"):
        os.utime(path, (1_000_000_000, 1_000_000_000))
    (b / "plugin" / "dragonlet" / "__pycache__").mkdir()
    (b / "plugin" / "dragonlet" / "__pycache__" / "parse.cpython-311.pyc").write_bytes(b"\0")
    (b / "plugin" / "dragonlet" / "parse.py.orig").write_text("an editor's backup")
    assert digest(build(a)) == digest(build(b))


def test_it_holds_what_maubot_loads_and_nothing_else(tmp_path):
    mbp = build(copy(tmp_path))
    version = re.search(r"(?m)^version: *(\S+)$", (ROOT / "plugin" / "maubot.yaml").read_text())[1]
    assert mbp.name == f"page.dejima.darner-v{version}.mbp"
    modules = sorted(p.relative_to(ROOT / "plugin").as_posix() for p in (ROOT / "plugin" / "dragonlet").glob("*.py"))
    with zipfile.ZipFile(mbp) as z:
        assert z.namelist() == sorted(["LICENSE", "base-config.yaml", "maubot.yaml", "dragonlet/", *modules])
        assert {(i.compress_type, i.date_time) for i in z.infolist()} == {(zipfile.ZIP_STORED, (1980, 1, 1, 0, 0, 0))}
        assert z.read("dragonlet/parse.py") == (ROOT / "plugin" / "dragonlet" / "parse.py").read_bytes()


def test_a_pre_release_version_is_given_in_the_archive_only(tmp_path):
    tree = copy(tmp_path)
    mbp = build(tree, "--version", "0.1.20.dev7")
    assert mbp.name == "page.dejima.darner-v0.1.20.dev7.mbp"
    with zipfile.ZipFile(mbp) as z:
        assert "version: 0.1.20.dev7\n" in z.read("maubot.yaml").decode()
    assert (tree / "plugin" / "maubot.yaml").read_bytes() == (ROOT / "plugin" / "maubot.yaml").read_bytes()
    assert digest(build(tree, "--version", "0.1.20.dev7")) == digest(mbp)


def test_no_packaged_file_carries_a_forge_link(tmp_path):
    with zipfile.ZipFile(build(copy(tmp_path))) as z:
        assert [n for n in z.namelist() if FORGE.encode() in z.read(n)] == []


def test_a_symlinked_member_is_refused(tmp_path):
    tree = copy(tmp_path / "tree")
    (tmp_path / "elsewhere.py").write_text("not the plugin's")
    (tree / "plugin" / "dragonlet" / "x.py").symlink_to(tmp_path / "elsewhere.py")
    with pytest.raises(subprocess.CalledProcessError) as e:
        build(tree)
    assert "symlink" in e.value.stderr
