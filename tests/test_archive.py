import io
import os
import zipfile

import pytest

from meeseeks import archive
from meeseeks.archive import ArchiveError, Limits, build, repack, safe_component

SECRET = b"host secret that must never leave"


@pytest.fixture
def tree(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "id_ed25519").write_bytes(SECRET)
    root = tmp_path / "shared"
    root.mkdir()
    return root, outside


def zip_of(root, limits=Limits()):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        data, manifest = build(fd, limits)
    finally:
        os.close(fd)
    return data, manifest


def names(data):
    return zipfile.ZipFile(io.BytesIO(data)).namelist()


def test_regular_files_are_included(tree):
    root, _ = tree
    (root / "RESULT.md").write_text("done")
    (root / "sub").mkdir()
    (root / "sub" / "report.txt").write_text("x" * 10)
    (root / ".done").touch()
    data, manifest = zip_of(root)
    assert names(data) == ["RESULT.md", "sub/report.txt", "MANIFEST.txt"]
    assert manifest.included == [("RESULT.md", 4), ("sub/report.txt", 10)]
    assert manifest.skipped == []
    for info in zipfile.ZipFile(io.BytesIO(data)).infolist():
        assert info.external_attr >> 16 == 0o100644
        assert info.date_time == (1980, 1, 1, 0, 0, 0)


def test_hostile_entries_are_skipped(tree):
    root, outside = tree
    (root / "link").symlink_to(outside / "id_ed25519")
    (root / "dirlink").symlink_to(outside)
    os.mkfifo(root / "fifo")
    for bad in ["esc\x1bname", "rtl‮txt.exe", "back\\slash", "trailing.", "con.txt", "NUL", ".hidden"]:
        (root / bad).write_text("x")
    os.close(os.open(os.fsencode(root) + b"/bad\xff", os.O_CREAT | os.O_WRONLY))
    (root / "a.txt").write_text("1")
    (root / "A.txt").write_text("2")
    (root / "MANIFEST.txt").write_text("forged")
    (root / "ok.txt").write_text("ok")

    data, manifest = zip_of(root)
    assert names(data) == ["ok.txt", "MANIFEST.txt"]
    assert SECRET not in data
    reasons = dict(manifest.skipped)
    assert reasons["'link'"] == "symlink"
    assert reasons["'dirlink'"] == "symlink"
    assert reasons["'fifo'"] == "not a regular file"
    assert reasons["'a.txt'"] == reasons["'A.txt'"] == "name differs only in case from another"
    assert reasons["'MANIFEST.txt'"] == "reserved name"
    assert reasons[repr("bad" + os.fsdecode(b"\xff"))] == "name is not valid UTF-8"
    for bad in ["esc\x1bname", "rtl‮txt.exe", "back\\slash", "trailing.", "con.txt", "NUL", ".hidden"]:
        assert reasons[repr(bad)] == "unsafe name"
    rendered = zipfile.ZipFile(io.BytesIO(data)).read("MANIFEST.txt").decode()
    assert "\x1b" not in rendered and "‮" not in rendered


def test_symlink_swapped_in_after_listing_is_not_followed(tree, monkeypatch):
    root, outside = tree
    (root / "victim").write_text("harmless")
    real_open = os.open

    def swapping_open(path, flags, *args, dir_fd=None, **kwargs):
        if path == "victim" and dir_fd is not None:
            os.unlink(root / "victim")
            os.symlink(outside / "id_ed25519", root / "victim")
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(archive.os, "open", swapping_open)
    data, manifest = zip_of(root)
    assert SECRET not in data
    assert manifest.skipped == [("'victim'", "symlink")]


def test_sparse_file_over_cap_fails(tree):
    root, _ = tree
    with open(root / "huge", "wb") as f:
        f.truncate(1 << 40)
    with pytest.raises(ArchiveError, match="more than 1024 bytes"):
        zip_of(root, Limits(max_bytes=1024))


def test_file_growing_while_read_stops_at_cap(tree, monkeypatch):
    root, _ = tree
    grower = root / "grows"
    grower.write_bytes(b"x" * 100)
    real_read = os.read

    def read_and_grow(fd, n):
        with open(grower, "ab") as f:
            f.write(b"y" * 100)
        return real_read(fd, n)

    monkeypatch.setattr(archive.os, "read", read_and_grow)
    with pytest.raises(ArchiveError, match="more than 1000 bytes"):
        zip_of(root, Limits(max_bytes=1000))


def test_depth_limit(tree):
    root, _ = tree
    deep = root
    for i in range(5):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    with pytest.raises(ArchiveError, match="deeper than 3"):
        zip_of(root, Limits(max_depth=3))


def test_file_count_limit(tree):
    root, _ = tree
    for i in range(4):
        (root / f"f{i}").write_text("x")
    with pytest.raises(ArchiveError, match="more than 3 files"):
        zip_of(root, Limits(max_files=3))


def test_path_length_limit(tree):
    root, _ = tree
    (root / ("a" * 40)).write_text("x")
    _, manifest = zip_of(root, Limits(max_path=30))
    assert manifest.skipped == [(repr("a" * 40), "path too long")]


def test_safe_component():
    assert safe_component("report-1.final+v2.txt") and safe_component("My File.md")
    for bad in ["", ".", "..", ".x", "-x", "a/b", "a\\b", "a.", "a ", "COM1", "lpt9.log", "con .txt", "é.txt"]:
        assert not safe_component(bad), bad


def make_zip(entries, mode=0o100644, compress=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            info.compress_type = compress
            zf.writestr(info, content)
    return buf.getvalue()


def test_repack_round_trip_and_summary(tree):
    root, _ = tree
    (root / "RESULT.md").write_text("Found it.\x1b[31m‮\nSee report.txt\t ")
    (root / "report.txt").write_text("details")
    data, _ = zip_of(root)
    packed, summary = repack(data)
    assert summary == "Found it.[31m\nSee report.txt"
    assert names(packed) == names(data)
    assert zipfile.ZipFile(io.BytesIO(packed)).read("report.txt") == b"details"


def test_repack_truncates_summary():
    packed, summary = repack(make_zip([("RESULT.md", "x" * 50000)]))
    assert len(summary) == archive.MAX_SUMMARY


def test_repack_drops_prepended_data():
    data = b"#!/bin/sh\necho polyglot\n" + make_zip([("a.txt", "x")])
    packed, _ = repack(data)
    assert packed.startswith(b"PK")


@pytest.mark.parametrize(
    "data",
    [
        make_zip([("../x", "x")]),
        make_zip([("/etc/x", "x")]),
        make_zip([("a\\b", "x")]),
        make_zip([("dir/", "")]),
        make_zip([("a.txt", "x"), ("A.txt", "y")]),
        make_zip([("link", "/etc/passwd")], mode=0o120777),
        make_zip([("run.sh", "x")], mode=0o100755),
        make_zip([("a.txt", "x")], compress=zipfile.ZIP_BZIP2),
        b"not a zip",
    ],
    ids=["dotdot", "absolute", "backslash", "dir", "case-dup", "symlink", "exec", "bzip2", "garbage"],
)
def test_repack_rejects(data):
    with pytest.raises(ArchiveError):
        repack(data)


def test_repack_rejects_understated_size():
    data = bytearray(make_zip([("a.txt", "x" * 1000)], compress=zipfile.ZIP_STORED))
    # Patch the central directory's uncompressed size down, so the entry's
    # real content is longer than it claims.
    cd = data.rfind(b"PK\x01\x02")
    data[cd + 24 : cd + 28] = (10).to_bytes(4, "little")
    with pytest.raises(ArchiveError):
        repack(bytes(data))


def test_repack_enforces_total_size():
    with pytest.raises(ArchiveError, match="too large"):
        repack(make_zip([("a.txt", "x" * 5000)]), Limits(max_bytes=4000 - archive.MANIFEST_SLACK))


def test_entry_limit_counts_skipped_entries(tree):
    root, _ = tree
    for i in range(6):
        (root / f".skipped{i}").touch()
    with pytest.raises(ArchiveError, match="more than 5 entries"):
        zip_of(root, Limits(max_entries=5))


def test_many_entries_are_fast(tree):
    import time

    root, _ = tree
    for i in range(20000):
        (root / f".x{i}").touch()
    start = time.monotonic()
    zip_of(root, Limits(max_entries=30000))
    assert time.monotonic() - start < 10


@pytest.mark.parametrize("name", ["manifest.txt", "Manifest.TXT"])
def test_manifest_name_is_reserved_in_any_case(tree, name):
    root, _ = tree
    (root / name).write_text("forged")
    data, manifest = zip_of(root)
    assert names(data) == ["MANIFEST.txt"]
    assert manifest.skipped == [(repr(name), "reserved name")]
    repack(data)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_unreadable_entries_are_skipped(tree):
    root, _ = tree
    (root / "locked.txt").write_text("x")
    (root / "locked.txt").chmod(0)
    (root / "lockeddir").mkdir()
    (root / "lockeddir").chmod(0)
    (root / "ok.txt").write_text("ok")
    try:
        data, manifest = zip_of(root)
    finally:
        (root / "lockeddir").chmod(0o700)
    assert names(data) == ["ok.txt", "MANIFEST.txt"]
    assert dict(manifest.skipped) == {
        "'locked.txt'": "unreadable (Permission denied)",
        "'lockeddir'": "unreadable (Permission denied)",
    }


def test_repack_rejects_huge_central_directory_before_parsing():
    data = make_zip([(f"f{i}", "") for i in range(20)])
    with pytest.raises(ArchiveError, match="more than 10 files"):
        repack(data, Limits(max_files=10))


def test_repack_rejects_corrupt_deflate_stream():
    data = bytearray(make_zip([("a.txt", "hello " * 100)]))
    start = data.index(b"a.txt") + len("a.txt")
    data[start : start + 20] = b"\xff" * 20
    with pytest.raises(ArchiveError):
        repack(bytes(data))


def test_repack_rejects_encrypted_entry():
    data = bytearray(make_zip([("a.txt", "x")]))
    for sig, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        at = data.index(sig) + offset
        data[at] |= 0x01
    with pytest.raises(ArchiveError):
        repack(bytes(data))


def test_requesters_uploads_are_not_sent_back(tree):
    root, _ = tree
    (root / "uploads").mkdir()
    (root / "uploads" / "access.log").write_text("theirs")
    (root / "Uploads-analysis.md").write_text("mine")
    (root / "notes").mkdir()
    (root / "notes" / "uploads").mkdir()
    (root / "notes" / "uploads" / "kept.txt").write_text("nested, so the agent's")
    data, manifest = zip_of(root)
    assert names(data) == ["Uploads-analysis.md", "notes/uploads/kept.txt", "MANIFEST.txt"]
    assert ("'uploads'", "uploaded by the requester, not sent back") in manifest.skipped
