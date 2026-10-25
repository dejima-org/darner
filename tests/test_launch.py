import contextlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from meeseeks.launch import launch, main, parse_args, status

FAKE_TMUX = """#!/bin/sh
python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@" >> "$FAKE_TMUX_LOG"
case "$1" in
    has-session) exit "${FAKE_TMUX_HAS:-1}" ;;
    new-window) while [ "$1" != -n ]; do shift; done; echo "meeseeks:$2" ;;
esac
"""

FAKE_DEJIMA = """#!/usr/bin/env python3
import json, os, sys
open(os.environ["FAKE_DEJIMA_LOG"], "w").write(json.dumps(sys.argv[1:]))
"""

PROMPT = "Fix it\n\n## Context from requester\nuse \"$(id)\" and `ls`; 'quotes'\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    tmux = tmp_path / "tmux"
    tmux.write_text(FAKE_TMUX)
    tmux.chmod(0o755)
    monkeypatch.setenv("FAKE_TMUX_LOG", str(tmp_path / "tmux.log"))
    return tmp_path


def args(tmp_path: Path, *extra: str):
    return parse_args(["--tmux", str(tmp_path / "tmux"), "--tasks-dir", str(tmp_path / "tasks"), *extra])


def tmux_calls(tmp_path: Path) -> list[list[str]]:
    return [json.loads(line) for line in (tmp_path / "tmux.log").read_text().splitlines()]


def written_argv(tmp_path: Path, task_id: str) -> list[str]:
    return json.loads((tmp_path / "tasks" / task_id / "argv.json").read_text())


def test_launch_creates_session_and_window(env):
    job = {"id": "ms-abc23", "prompt": PROMPT, "domains": ["pypi.org"], "dependencies": ["pip:pytest"]}
    result = launch(args(env), job)
    assert result == {"id": "ms-abc23", "target": "meeseeks:ms-abc23"}

    calls = tmux_calls(env)
    assert [c[0] for c in calls] == ["has-session", "new-session", "new-window"]
    window = calls[2]
    assert window[window.index("-n") + 1] == "ms-abc23"
    assert window[window.index("--") + 1 :] == [
        sys.executable, "-m", "meeseeks.launch", "--exec", str(env / "tasks" / "ms-abc23"),
    ]

    task = env / "tasks" / "ms-abc23"
    assert task.stat().st_mode & 0o777 == 0o700
    assert (task / "domains").read_text() == "pypi.org\n"
    assert (task / "dependencies").read_text() == "pip:pytest\n"
    assert written_argv(env, "ms-abc23") == [
        "dejima", "--isolated", "--no-pause", "--shared-path-file", str(task / "shared-path"),
        "--domains", str(task / "domains"), "--dependencies", str(task / "dependencies"),
        "--", PROMPT,
    ]


def test_existing_session_is_reused(env, monkeypatch):
    monkeypatch.setenv("FAKE_TMUX_HAS", "0")
    launch(args(env), {"id": "ms-abc23", "prompt": "p"})
    assert [c[0] for c in tmux_calls(env)] == ["has-session", "new-window"]


def test_duplicate_id_rejected(env):
    launch(args(env), {"id": "ms-abc23", "prompt": "p"})
    with pytest.raises(FileExistsError):
        launch(args(env), {"id": "ms-abc23", "prompt": "p"})


def test_no_grants_or_names_leaves_flags_out(env):
    launch(args(env), {"id": "ms-abc23", "prompt": "p"})
    shared = str(env / "tasks" / "ms-abc23" / "shared-path")
    assert written_argv(env, "ms-abc23") == ["dejima", "--isolated", "--no-pause", "--shared-path-file", shared, "--", "p"]


@pytest.mark.parametrize(
    "job,expected",
    [
        ({}, ["--harness", "claude-code", "--backend", "anthropic"]),
        ({"backend": "openrouter"}, ["--harness", "claude-code", "--backend", "openrouter"]),
        ({"harness": "cline"}, ["--harness", "cline", "--backend", "self-hosted"]),
        ({"harness": "other"}, ["--harness", "other"]),
    ],
)
def test_harness_and_backend_resolution(env, job, expected):
    a = args(
        env, "--identity", "meeseeks", "--harness", "claude-code",
        "--backend", "claude-code=anthropic", "--backend", "cline=self-hosted",
    )
    launch(a, {"id": "ms-abc23", "prompt": "p", **job})
    assert written_argv(env, "ms-abc23")[5:-2] == ["--identity", "meeseeks", *expected]


def test_main_reports_invalid_job(env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"id": "ms-abc23", "prompt": "p", "identity": "x"}'))
    assert main(["--tmux", str(env / "tmux"), "--tasks-dir", str(env / "tasks")]) == 1
    assert "unknown job keys: identity" in capsys.readouterr().err


def test_exec_runs_dejima_with_prompt_as_one_argument(env):
    dejima = env / "dejima"
    dejima.write_text(FAKE_DEJIMA)
    dejima.chmod(0o755)
    launch(args(env, "--dejima", str(dejima)), {"id": "ms-abc23", "prompt": PROMPT})
    subprocess.run(
        [sys.executable, "-m", "meeseeks.launch", "--exec", str(env / "tasks" / "ms-abc23")],
        env={"FAKE_DEJIMA_LOG": str(env / "dejima.log"), "PATH": "/usr/bin:/bin"},
        cwd=Path(__file__).parent.parent,
        check=True,
    )
    shared = str(env / "tasks" / "ms-abc23" / "shared-path")
    assert json.loads((env / "dejima.log").read_text()) == [
        "--isolated", "--no-pause", "--shared-path-file", shared, "--", PROMPT,
    ]


EXITING_DEJIMA = """#!/bin/sh
exit 3
"""


def test_exec_records_exit_status(env):
    dejima = env / "dejima"
    dejima.write_text(EXITING_DEJIMA)
    dejima.chmod(0o755)
    launch(args(env, "--dejima", str(dejima)), {"id": "ms-abc23", "prompt": "p"})
    task = env / "tasks" / "ms-abc23"
    result = subprocess.run(
        [sys.executable, "-m", "meeseeks.launch", "--exec", str(task)], cwd=Path(__file__).parent.parent
    )
    assert result.returncode == 3
    assert (task / "exit").read_text() == "3\n"


@pytest.fixture
def share(env):
    """A launched task whose session has a ~/shared under the share root."""
    root = env / "share-root"
    shared = root / "20261025-120000-AbC123"
    shared.mkdir(parents=True)
    a = args(env, "--share-root", str(root))
    launch(a, {"id": "ms-abc23", "prompt": "p"})
    (env / "tasks" / "ms-abc23" / "shared-path").write_text(f"{shared}\n")
    return a, shared


def run(a, *command):
    out, err = io.BytesIO(), io.StringIO()
    stdout = type("Stdout", (), {"buffer": out, "write": lambda self, s: out.write(s.encode())})()
    with contextlib.redirect_stderr(err):
        with contextlib.redirect_stdout(stdout):
            code = main(
                ["--tmux", str(a.tmux), "--tasks-dir", str(a.tasks_dir), "--share-root", str(a.share_root), *command]
            )
    return code, out.getvalue(), err.getvalue()


def test_status_states(env, share):
    a, shared = share
    launch(a, {"id": "ms-starx", "prompt": "p"})
    (env / "tasks" / "not-a-task").mkdir()
    assert {t["id"]: (t["state"], t["ready"]) for t in status(a)} == {
        "ms-abc23": ("running", False),
        "ms-starx": ("starting", False),
    }
    (shared / ".done").touch()
    (env / "tasks" / "ms-abc23" / "exit").write_text("0\n")
    code, out, _ = run(a, "status")
    assert code == 0
    assert json.loads(out) == [
        {"id": "ms-abc23", "state": "exited", "ready": True},
        {"id": "ms-starx", "state": "starting", "ready": False},
    ]


def test_fetch_returns_zip_and_consumes_done(share):
    a, shared = share
    (shared / "RESULT.md").write_text("found it")
    (shared / ".done").touch()
    code, out, _ = run(a, "fetch", "ms-abc23")
    assert code == 0
    assert zipfile.ZipFile(io.BytesIO(out)).namelist() == ["RESULT.md", "MANIFEST.txt"]
    assert not (shared / ".done").exists()


def test_fetch_without_done_fails(share):
    a, _ = share
    code, out, err = run(a, "fetch", "ms-abc23")
    assert code == 1 and out == b"" and "no output ready" in err


def test_fetch_over_limit_is_rejected_and_consumed(share):
    a, shared = share
    (shared / "big").write_bytes(b"x" * 2000)
    (shared / ".done").touch()
    code, out, err = run(a, "--max-output", "1000", "fetch", "ms-abc23")
    assert code == 3 and out == b"" and "more than 1000 bytes" in err
    assert not (shared / ".done").exists()


def test_done_symlink_removes_only_the_link(env, share):
    a, shared = share
    target = env / "precious"
    target.write_text("keep")
    (shared / ".done").symlink_to(target)
    code, _, _ = run(a, "fetch", "ms-abc23")
    assert code == 0
    assert not (shared / ".done").is_symlink() and target.read_text() == "keep"


@pytest.mark.parametrize(
    "path",
    ["{outside}", "{root}/../elsewhere", "{root}/..", "relative/dir", "{root}/sub/deeper", "{root}/.hidden"],
)
def test_fetch_refuses_shared_path_outside_share_root(env, share, path):
    a, _ = share
    outside = env / "elsewhere"
    outside.mkdir(exist_ok=True)
    (outside / ".done").touch()
    resolved = path.format(outside=outside, root=a.share_root)
    (env / "tasks" / "ms-abc23" / "shared-path").write_text(resolved + "\n")
    code, out, err = run(a, "fetch", "ms-abc23")
    assert code == 1 and out == b""


def test_fetch_refuses_symlinked_share(env, share):
    a, shared = share
    outside = env / "elsewhere"
    outside.mkdir()
    (outside / "secret").write_text("x")
    (outside / ".done").touch()
    link = a.share_root / "20261025-120000-Link01"
    link.symlink_to(outside)
    (env / "tasks" / "ms-abc23" / "shared-path").write_text(f"{link}\n")
    code, out, _ = run(a, "fetch", "ms-abc23")
    assert code == 1 and out == b""
    assert (outside / ".done").exists()


@pytest.mark.parametrize("command", ["fetch ../x", "fetch", "rm -rf /", "status extra", "fetch ms-abc23 more"])
def test_unknown_commands_are_refused(share, monkeypatch, command):
    a, _ = share
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", command)
    code, out, err = run(a)
    assert code == 2 and "unknown command" in err


def test_ssh_original_command_overrides_positional(share, monkeypatch):
    a, _ = share
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "status")
    code, out, _ = run(a)
    assert code == 0 and json.loads(out)[0]["id"] == "ms-abc23"


def test_done_directory_is_not_a_signal(share):
    a, shared = share
    (shared / ".done").mkdir()
    assert status(a)[0]["ready"] is False
    code, out, err = run(a, "fetch", "ms-abc23")
    assert code == 1 and "no output ready" in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_undeletable_done_is_consumed_once(share):
    a, shared = share
    (shared / "RESULT.md").write_text("x")
    (shared / ".done").touch()
    shared.chmod(0o555)
    try:
        assert status(a)[0]["ready"] is True
        assert run(a, "fetch", "ms-abc23")[0] == 0
        assert (shared / ".done").exists()
        assert status(a)[0]["ready"] is False
        assert run(a, "fetch", "ms-abc23")[0] == 1
    finally:
        shared.chmod(0o755)

    (shared / ".done").unlink()
    (shared / ".done").touch()
    assert status(a)[0]["ready"] is True


def test_slow_fetch_is_rejected_and_consumed(share, monkeypatch):
    import time

    from meeseeks import launch as launch_module

    a, shared = share
    (shared / ".done").touch()
    monkeypatch.setattr(launch_module.archive, "build", lambda *_: time.sleep(5))
    code, out, err = run(a, "--fetch-timeout", "1", "fetch", "ms-abc23")
    assert code == 3 and out == b"" and "took longer than 1s" in err
    assert status(a)[0]["ready"] is False
