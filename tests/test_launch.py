# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from darner.launch import FETCH_REJECTED, launch, main, parse_args, status

FAKE_TMUX = """#!/bin/sh
python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@" >> "$FAKE_TMUX_LOG"
case "$1" in
    has-session) exit "${FAKE_TMUX_HAS:-1}" ;;
    load-buffer) cat > "$FAKE_TMUX_LOG.buffer" ;;
    capture-pane) cat "$FAKE_TMUX_LOG.screen" 2>/dev/null ;;
    paste-buffer) cut -c "$(( ${FAKE_TMUX_DROP:-0} + 1 ))-" "$FAKE_TMUX_LOG.buffer" | head -c 4000 >> "$FAKE_TMUX_LOG.screen" ;;
    send-keys) if [ "$2" = -l ]; then printf '%s' "$6" >> "$FAKE_TMUX_LOG.screen"; elif [ "$4" = Enter ] && [ -z "$FAKE_TMUX_EAT_ENTER" ]; then echo "[submitted]" >> "$FAKE_TMUX_LOG.screen"; fi ;;
    new-window)
        # Every window gets its own ids, as tmux gives them: @7 %7 for the first.
        n=$(( $(cat "$FAKE_TMUX_LOG.n" 2>/dev/null || echo 6) + 1 )); echo "$n" > "$FAKE_TMUX_LOG.n"
        while [ "$1" != -n ]; do shift; done; echo "darner:$2 @$n %$n" ;;
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
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "dejima").write_text(FAKE_DEJIMA)
    (bin_dir / "dejima").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_TMUX_LOG", str(tmp_path / "tmux.log"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    return tmp_path


def args(tmp_path: Path, *extra: str):
    return parse_args(["--tmux", str(tmp_path / "tmux"), "--tasks-dir", str(tmp_path / "tasks"), *extra])


def tmux_calls(tmp_path: Path) -> list[list[str]]:
    return [json.loads(line) for line in (tmp_path / "tmux.log").read_text().splitlines()]


def written_argv(tmp_path: Path, task_id: str) -> list[str]:
    return json.loads((tmp_path / "tasks" / task_id / "argv.json").read_text())


def test_launch_creates_session_and_window(env):
    job = {"id": "dn-abc23", "prompt": PROMPT, "domains": ["pypi.org"], "dependencies": ["pip:pytest"]}
    result = launch(args(env), job)
    assert result == {"id": "dn-abc23", "target": "darner:dn-abc23"}

    calls = tmux_calls(env)
    assert [c[0] for c in calls] == ["has-session", "new-session", "new-window"]
    window = calls[2]
    assert window[window.index("-n") + 1] == "dn-abc23"
    assert window[window.index("--") + 1 :] == [
        sys.executable, "-m", "darner.launch", "--share-root", str(Path.home() / ".local/share/dejima/shared/isolated"),
        "--exec", str(env / "tasks" / "dn-abc23"),
    ]

    task = env / "tasks" / "dn-abc23"
    assert task.stat().st_mode & 0o777 == 0o700
    assert (task / "domains").read_text() == "pypi.org\n"
    assert (task / "dependencies").read_text() == "pip:pytest\n"
    assert written_argv(env, "dn-abc23") == [
        str(env / "bin" / "dejima"), "--isolated", "--no-pause", "--shared-path-file", str(task / "shared-path"),
        "--domains", str(task / "domains"), "--dependencies", str(task / "dependencies"),
        "--", PROMPT,
    ]


def test_existing_session_is_reused(env, monkeypatch):
    monkeypatch.setenv("FAKE_TMUX_HAS", "0")
    launch(args(env), {"id": "dn-abc23", "prompt": "p"})
    assert [c[0] for c in tmux_calls(env)] == ["has-session", "new-window"]


def test_duplicate_id_rejected(env):
    launch(args(env), {"id": "dn-abc23", "prompt": "p"})
    with pytest.raises(RuntimeError, match="dn-abc23 is already in use$"):
        launch(args(env), {"id": "dn-abc23", "prompt": "p"})


def test_no_grants_or_names_leaves_flags_out(env):
    launch(args(env), {"id": "dn-abc23", "prompt": "p"})
    shared = str(env / "tasks" / "dn-abc23" / "shared-path")
    assert written_argv(env, "dn-abc23") == [
        str(env / "bin" / "dejima"), "--isolated", "--no-pause", "--shared-path-file", shared, "--", "p",
    ]


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
        env, "--identity", "darner", "--harness", "claude-code",
        "--backend", "claude-code=anthropic", "--backend", "cline=self-hosted",
    )
    launch(a, {"id": "dn-abc23", "prompt": "p", **job})
    assert written_argv(env, "dn-abc23")[5:-2] == ["--identity", "darner", *expected]


def test_main_reports_invalid_job(env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"id": "dn-abc23", "prompt": "p", "identity": "x"}'))
    assert main(["--tmux", str(env / "tmux"), "--tasks-dir", str(env / "tasks")]) == 1
    assert "unknown job keys: identity" in capsys.readouterr().err


def test_exec_runs_dejima_with_prompt_as_one_argument(env):
    dejima = env / "dejima"
    dejima.write_text(FAKE_DEJIMA)
    dejima.chmod(0o755)
    launch(args(env, "--dejima", str(dejima)), {"id": "dn-abc23", "prompt": PROMPT})
    subprocess.run(
        [sys.executable, "-m", "darner.launch", "--exec", str(env / "tasks" / "dn-abc23")],
        env={"FAKE_DEJIMA_LOG": str(env / "dejima.log"), "PATH": "/usr/bin:/bin"},
        cwd=Path(__file__).parent.parent,
        check=True,
    )
    shared = str(env / "tasks" / "dn-abc23" / "shared-path")
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
    launch(args(env, "--dejima", str(dejima)), {"id": "dn-abc23", "prompt": "p"})
    task = env / "tasks" / "dn-abc23"
    result = subprocess.run(
        [sys.executable, "-m", "darner.launch", "--exec", str(task)], cwd=Path(__file__).parent.parent
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
    launch(a, {"id": "dn-abc23", "prompt": "p"})
    (env / "tasks" / "dn-abc23" / "shared-path").write_text(f"{shared}\n")
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
    launch(a, {"id": "dn-starx", "prompt": "p"})
    (env / "tasks" / "not-a-task").mkdir()
    assert {t["id"]: (t["state"], t["ready"]) for t in status(a)} == {
        "dn-abc23": ("running", False),
        "dn-starx": ("starting", False),
    }
    (shared / ".done").touch()
    (env / "tasks" / "dn-abc23" / "exit").write_text("0\n")
    code, out, _ = run(a, "status")
    assert code == 0
    assert json.loads(out) == [
        {"id": "dn-abc23", "state": "exited", "ready": True, "unsignalled": False, "help": False, "domains": False, "answerable": False, "run_id": None, "error": None, "awaiting_answer": False, "result_pending": False, "quiet": None, "screen": "", "domain_request": None},
        {"id": "dn-starx", "state": "starting", "ready": False, "unsignalled": False, "help": False, "domains": False, "answerable": False, "run_id": None, "error": None, "awaiting_answer": False, "result_pending": False, "quiet": None, "screen": "", "domain_request": None},
    ]


def test_fetch_returns_zip_and_consumes_done(share):
    a, shared = share
    (shared / "RESULT.md").write_text("found it")
    (shared / ".done").touch()
    code, out, _ = run(a, "fetch", "dn-abc23")
    assert code == 0
    assert zipfile.ZipFile(io.BytesIO(out)).namelist() == ["RESULT.md", "MANIFEST.txt"]
    assert not (shared / ".done").exists()


def test_fetch_without_done_fails(share):
    a, _ = share
    code, out, err = run(a, "fetch", "dn-abc23")
    assert code == 1 and out == b"" and "no output ready" in err


def test_fetch_over_limit_is_rejected_and_consumed(share):
    a, shared = share
    (shared / "big").write_bytes(b"x" * 2000)
    (shared / ".done").touch()
    code, out, err = run(a, "--max-output", "1000", "fetch", "dn-abc23")
    assert code == 3 and out == b"" and "more than 1000 bytes" in err
    assert not (shared / ".done").exists()


def test_done_symlink_removes_only_the_link(env, share):
    a, shared = share
    target = env / "precious"
    target.write_text("keep")
    (shared / ".done").symlink_to(target)
    code, _, _ = run(a, "fetch", "dn-abc23")
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
    (env / "tasks" / "dn-abc23" / "shared-path").write_text(resolved + "\n")
    code, out, err = run(a, "fetch", "dn-abc23")
    assert code == 1 and out == b""


def test_fetch_refuses_symlinked_share(env, share):
    a, shared = share
    outside = env / "elsewhere"
    outside.mkdir()
    (outside / "secret").write_text("x")
    (outside / ".done").touch()
    link = a.share_root / "20261025-120000-Link01"
    link.symlink_to(outside)
    (env / "tasks" / "dn-abc23" / "shared-path").write_text(f"{link}\n")
    code, out, _ = run(a, "fetch", "dn-abc23")
    assert code == 1 and out == b""
    assert (outside / ".done").exists()


@pytest.mark.parametrize("command", ["fetch ../x", "fetch", "rm -rf /", "status extra", "fetch dn-abc23 more"])
def test_unknown_commands_are_refused(share, monkeypatch, command):
    a, _ = share
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", command)
    code, out, err = run(a)
    assert code == 2 and "unknown command" in err


def test_ssh_original_command_overrides_positional(share, monkeypatch):
    a, _ = share
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "status")
    code, out, _ = run(a)
    assert code == 0 and json.loads(out)[0]["id"] == "dn-abc23"


def test_done_directory_is_not_a_signal(share):
    a, shared = share
    (shared / ".done").mkdir()
    assert status(a)[0]["ready"] is False
    code, out, err = run(a, "fetch", "dn-abc23")
    assert code == 1 and "no output ready" in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_undeletable_done_is_consumed_once(share):
    a, shared = share
    (shared / "RESULT.md").write_text("x")
    (shared / ".done").touch()
    shared.chmod(0o555)
    try:
        assert status(a)[0]["ready"] is True
        assert run(a, "fetch", "dn-abc23")[0] == 0
        assert (shared / ".done").exists()
        assert status(a)[0]["ready"] is False
        assert run(a, "fetch", "dn-abc23")[0] == 1
    finally:
        shared.chmod(0o755)

    (shared / ".done").unlink()
    (shared / ".done").touch()
    assert status(a)[0]["ready"] is True


def test_slow_fetch_is_rejected_and_consumed(share, monkeypatch):
    import time

    from darner import launch as launch_module

    a, shared = share
    (shared / ".done").touch()
    monkeypatch.setattr(launch_module.archive, "build", lambda *_: time.sleep(5))
    code, out, err = run(a, "--fetch-timeout", "1", "fetch", "dn-abc23")
    assert code == 3 and out == b"" and "took longer than 1s" in err
    assert status(a)[0]["ready"] is False


def test_help_request_is_read_safely_and_consumed_once(env, share):
    a, shared = share
    (shared / "HELP.md").write_text("Which branch?")
    (shared / ".help").touch()
    assert status(a)[0]["help"] is True
    code, out, _ = run(a, "help", "dn-abc23")
    assert code == 0 and out == b"Which branch?"
    assert (env / "tasks" / "dn-abc23" / "help-pending").exists()
    assert status(a)[0]["help"] is False
    assert run(a, "help", "dn-abc23")[0] == 1


@pytest.mark.parametrize("kind", ["symlink", "fifo", "missing", "dir"])
def test_unsafe_help_text_is_empty(env, share, kind):
    a, shared = share
    target = env / "secret"
    target.write_text("host secret")
    if kind == "symlink":
        (shared / "HELP.md").symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(shared / "HELP.md")
    elif kind == "dir":
        (shared / "HELP.md").mkdir()
    (shared / ".help").touch()
    code, out, _ = run(a, "help", "dn-abc23")
    assert code == 0 and out == b""


def test_help_text_is_capped(share):
    a, shared = share
    (shared / "HELP.md").write_bytes(b"x" * 100000)
    (shared / ".help").touch()
    code, out, _ = run(a, "help", "dn-abc23")
    assert code == 0 and len(out) == 16 * 1024


def answer_run(a, data, *extra):
    import unittest.mock

    with unittest.mock.patch("sys.stdin", io.StringIO(json.dumps(data))):
        return run(a, *extra, "answer", "dn-abc23")


@pytest.fixture
def asked(env, share):
    a, shared = share
    (shared / ".help").touch()
    run(a, "help", "dn-abc23")
    (env / "tmux.log").write_text("")
    return a


def test_answer_is_pasted_into_the_session(env, asked):
    code, _, err = answer_run(asked, {"requester": "@alice:example.org", "text": "Use main.\nThanks"}, "--answers", "--paste-delay", "0")
    assert code == 0, err
    assert tmux_calls(env) == [
        ["capture-pane", "-p", "-J", "-t", "@7"],
        ["load-buffer", "-b", f"darner-dn-abc23-{os.getpid()}", "-"],
        ["paste-buffer", "-p", "-d", "-b", f"darner-dn-abc23-{os.getpid()}", "-t", "@7"],
        ["capture-pane", "-p", "-J", "-t", "@7"],
        ["send-keys", "-t", "@7", "Enter"],
        ["capture-pane", "-p", "-J", "-t", "@7"],
    ]
    assert (env / "tmux.log.buffer").read_text() == "Use main.\nThanks"
    assert not (env / "tasks" / "dn-abc23" / "help-pending").exists()
    assert answer_run(asked, {"requester": "@alice:example.org", "text": "again"}, "--answers")[0] == 1


def test_answer_to_a_cline_question_is_typed(env, asked):
    (env / "tmux.log.screen").write_text(" Cline is asking a question\n Which branch?\n  > main\n    Type a response...\n")
    text = "2 of them:\n\n" + "x" * 1500
    code, _, err = answer_run(asked, {"requester": "@alice:example.org", "text": text}, "--answers", "--paste-delay", "0")
    assert code == 0, err
    typed = [call[5] for call in tmux_calls(env) if call[:2] == ["send-keys", "-l"]]
    assert all(call[4] == "--" for call in tmux_calls(env) if call[:2] == ["send-keys", "-l"])
    assert "".join(typed) == " 2 of them: " + "x" * 1500 and len(typed) == 2
    assert not any(call[0] in ("load-buffer", "paste-buffer") for call in tmux_calls(env))


def test_answer_is_sanitised(env, asked):
    text = "ok\x1b[201~\x03rm -rf ~\r\n‮done\x00"
    code, _, _ = answer_run(asked, {"requester": "@alice\n:example.org\x1b", "text": text}, "--answers", "--paste-delay", "0")
    assert code == 0
    pasted = (env / "tmux.log.buffer").read_text()
    assert pasted == "ok[201~rm -rf ~\ndone"
    assert "\x1b" not in pasted and "\x03" not in pasted


def test_answer_refused_without_flag(env, asked):
    code, _, err = answer_run(asked, {"requester": "@a:example.org", "text": "x"})
    assert code == 1 and "not enabled" in err
    assert tmux_calls(env) == []


def test_answer_refused_without_pending_help(env, share):
    a, _ = share
    code, _, err = answer_run(a, {"requester": "@a:example.org", "text": "x"}, "--answers")
    assert code == 1 and "no help request" in err


def test_answer_refused_after_exit(env, asked):
    (env / "tasks" / "dn-abc23" / "exit").write_text("0\n")
    code, _, err = answer_run(asked, {"requester": "@a:example.org", "text": "x"}, "--answers")
    assert code == 1 and "not running" in err


@pytest.mark.parametrize("data", [{"requester": "@a:example.org", "text": "\x1b\x07 "}, {"text": "x"}, ["x"]])
def test_answer_refuses_bad_input(env, asked, data):
    assert answer_run(asked, data, "--answers")[0] == 1
    assert tmux_calls(env) == []


def test_status_reports_answerable(share):
    a, _ = share
    assert status(a)[0]["answerable"] is False
    assert status(parse_args(["--tasks-dir", str(a.tasks_dir), "--share-root", str(a.share_root), "--answers"]))[0]["answerable"] is True


def test_window_id_is_recorded(env):
    launch(args(env), {"id": "dn-abc23", "prompt": "p"})
    assert (env / "tasks" / "dn-abc23" / "window").read_text() == "@7"


def test_help_requests_are_rate_limited(env, share):
    a, shared = share
    (shared / ".help").touch()
    assert run(a, "help", "dn-abc23")[0] == 0
    # A new signal is a new inode or ctime; wait out the kernel's coarse clock
    # so a reused inode can't carry the same ctime.
    __import__("time").sleep(0.05)
    (shared / ".help").touch()
    assert status(a)[0]["help"] is False
    assert run(a, "help", "dn-abc23")[0] == 1
    consumed = env / "tasks" / "dn-abc23" / "consumed-help"
    os.utime(consumed, (0, 0))
    assert status(a)[0]["help"] is True
    assert run(a, "help", "dn-abc23")[0] == 0


def test_concurrent_answer_loses_the_claim(env, asked):
    os.rename(env / "tasks" / "dn-abc23" / "help-pending", env / "tasks" / "dn-abc23" / "help-answering")
    code, _, err = answer_run(asked, {"requester": "@a:example.org", "text": "x"}, "--answers")
    assert code == 1 and "no help request awaiting" in err
    assert tmux_calls(env) == []


def test_failed_paste_keeps_request_and_hides_tmux_detail(env, asked, monkeypatch):
    broken = env / "broken-tmux"
    broken.write_text("#!/bin/sh\necho 'no server running on /tmp/tmux-1000/default' >&2\nexit 1\n")
    broken.chmod(0o755)
    code, _, err = answer_run(asked, {"requester": "@a:example.org", "text": "x"}, "--answers", "--tmux", str(broken))
    assert code == 1 and "couldn't type into the session" in err and "/tmp" not in err
    assert (env / "tasks" / "dn-abc23" / "help-pending").exists()


def test_long_answer_is_refused_not_truncated(env, asked):
    code, _, err = answer_run(asked, {"requester": "@a:example.org", "text": "x" * 20000}, "--answers")
    assert code == 1 and "over 16 KiB" in err


def test_missing_dejima_fails_the_launch(env, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    with pytest.raises(RuntimeError, match="^dejima not found on PATH; pass --dejima"):
        launch(args(env), {"id": "dn-abc23", "prompt": "p"})
    assert not (env / "tasks" / "dn-abc23").exists()
    assert not (env / "tmux.log").exists()


def test_exec_records_a_dejima_that_cannot_start(env):
    launch(args(env), {"id": "dn-abc23", "prompt": "p"})
    task = env / "tasks" / "dn-abc23"
    argv = json.loads((task / "argv.json").read_text())
    (task / "argv.json").write_text(json.dumps([str(env / "gone"), *argv[1:]]))
    result = subprocess.run(
        [sys.executable, "-m", "darner.launch", "--exec", str(task)],
        cwd=Path(__file__).parent.parent, input="\n", capture_output=True, text=True,
    )
    assert result.returncode == 127
    assert (task / "exit").read_text() == "127\n"
    assert "couldn't start" in result.stderr and "Press Enter" in result.stdout


def register_session(env, pane="%7", run_id="0123456789abcdef"):
    sessions = env / "run" / "dejima" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / run_id).write_text(f"workspace=isolated\npane={pane}\nlabel=darner:1\n")


def test_pane_is_recorded(env):
    launch(args(env), {"id": "dn-abc23", "prompt": "p"})
    assert (env / "tasks" / "dn-abc23" / "pane").read_text() == "%7"


def test_launch_waits_for_the_session_run_id(env):
    register_session(env, pane="%9", run_id="ffffffffffffffff")
    register_session(env)
    (env / "run" / "dejima" / "sessions" / "not-a-run-id").write_text("pane=%3\n")
    result = launch(args(env), {"id": "dn-abc23", "prompt": "p", "session_wait": 5})
    assert result["run_id"] == "0123456789abcdef"


def test_launch_gives_up_waiting(env):
    import time

    start = time.monotonic()
    result = launch(args(env), {"id": "dn-abc23", "prompt": "p", "session_wait": 1})
    assert "run_id" not in result and time.monotonic() - start < 3


def test_launch_without_session_wait_does_not_look(env):
    register_session(env)
    assert "run_id" not in launch(args(env), {"id": "dn-abc23", "prompt": "p"})


def test_status_reports_run_id_of_running_task(env, share):
    a, _ = share
    register_session(env)
    assert status(a)[0]["run_id"] == "0123456789abcdef"


def age(path, seconds):
    t = __import__("time").time() - seconds
    os.utime(path, (t, t), follow_symlinks=False)


def quiet(shared, seconds=700):
    for entry in [shared, *shared.iterdir()]:
        age(entry, seconds)


def test_unsignalled_result_waits_for_quiet(share):
    a, shared = share
    (shared / "RESULT.md").write_text("done, forgot .done")
    (shared / "notes.md").write_text("x")
    assert status(a)[0]["unsignalled"] is False
    quiet(shared)
    assert status(a)[0]["unsignalled"] is True


def test_recent_change_anywhere_at_the_top_keeps_waiting(share):
    a, shared = share
    (shared / "RESULT.md").write_text("draft")
    quiet(shared)
    (shared / "still-writing.md").write_text("x")
    assert status(a)[0]["unsignalled"] is False


def test_unsignalled_result_is_collected_once(share):
    a, shared = share
    (shared / "RESULT.md").write_text("forgot .done")
    quiet(shared)
    code, out, _ = run(a, "fetch", "dn-abc23")
    assert code == 0 and "RESULT.md" in zipfile.ZipFile(io.BytesIO(out)).namelist()
    assert status(a)[0]["unsignalled"] is False
    assert run(a, "fetch", "dn-abc23")[0] == 1

    (shared / "RESULT.md").write_text("did more, forgot again")
    quiet(shared)
    assert status(a)[0]["unsignalled"] is True


def test_session_end_collects_without_waiting(env, share):
    a, shared = share
    (shared / "RESULT.md").write_text("forgot .done")
    (env / "tasks" / "dn-abc23" / "exit").write_text("0\n")
    assert status(parse_args(["--tasks-dir", str(a.tasks_dir), "--share-root", str(a.share_root), "--quiet-period", "0"]))[0]["unsignalled"] is True


def test_quiet_period_zero_waits_for_session_end(share):
    a, shared = share
    (shared / "RESULT.md").write_text("forgot .done")
    quiet(shared, 10**6)
    a0 = parse_args(["--tasks-dir", str(a.tasks_dir), "--share-root", str(a.share_root), "--quiet-period", "0"])
    assert status(a0)[0]["unsignalled"] is False


def test_signalled_delivery_records_its_result(share):
    a, shared = share
    (shared / "RESULT.md").write_text("properly done")
    (shared / ".done").touch()
    quiet(shared)
    assert run(a, "fetch", "dn-abc23")[0] == 0
    age(shared, 700)
    entry = status(a)[0]
    assert entry["ready"] is False and entry["unsignalled"] is False


def test_result_symlink_is_not_output(env, share):
    a, shared = share
    (env / "elsewhere.md").write_text("x")
    (shared / "RESULT.md").symlink_to(env / "elsewhere.md")
    quiet(shared)
    assert status(a)[0]["unsignalled"] is False


def test_concurrent_sessions_each_get_their_own_answer(env, share):
    a, first = share
    second = a.share_root / "20261025-120500-XyZ789"
    second.mkdir()
    launch(a, {"id": "dn-defgh", "prompt": "p"})
    (env / "tasks" / "dn-defgh" / "shared-path").write_text(f"{second}\n")
    for shared, task in ((first, "dn-abc23"), (second, "dn-defgh")):
        (shared / ".help").touch()
        assert run(a, "help", task)[0] == 0
    windows = {t: (env / "tasks" / t / "window").read_text() for t in ("dn-abc23", "dn-defgh")}
    assert windows["dn-abc23"] != windows["dn-defgh"]

    import unittest.mock

    for task, text in (("dn-defgh", "answer for the second"), ("dn-abc23", "answer for the first")):
        (env / "tmux.log").write_text("")
        with unittest.mock.patch("sys.stdin", io.StringIO(json.dumps({"requester": "@a:example.org", "text": text}))):
            assert run(a, "--answers", "--paste-delay", "0", "answer", task)[0] == 0
        paste = next(c for c in tmux_calls(env) if c[0] == "paste-buffer")
        assert paste[paste.index("-t") + 1] == windows[task]
        assert (env / "tmux.log.buffer").read_text().endswith(text)
    assert not (env / "tasks" / "dn-abc23" / "help-pending").exists()
    assert not (env / "tasks" / "dn-defgh" / "help-pending").exists()


def test_status_says_why_a_share_cant_be_read(env, share):
    a, shared = share
    elsewhere = env / "shared-elsewhere" / "isolated" / "20261025-120000-AbC123"
    elsewhere.mkdir(parents=True)
    (elsewhere / ".done").touch()
    (env / "tasks" / "dn-abc23" / "shared-path").write_text(f"{elsewhere}\n")
    entry = status(a)[0]
    assert entry["ready"] is False
    assert "is not under" in entry["error"] and str(a.share_root) in entry["error"]


def test_starting_task_has_no_error(env, share):
    a, _ = share
    (env / "tasks" / "dn-abc23" / "shared-path").unlink()
    assert status(a)[0]["error"] is None


FAKE_DOCKER = """#!/bin/sh
python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@" >> "$FAKE_DOCKER_LOG"
case "$1" in
    ps) [ -n "$FAKE_DOCKER_PS" ] && echo "$FAKE_DOCKER_PS" ;;
    stop) [ -n "$FAKE_DOCKER_EXIT" ] && echo 137 > "$FAKE_DOCKER_EXIT" ;;
esac
exit 0
"""


@pytest.fixture
def docker(env, monkeypatch):
    path = env / "docker"
    path.write_text(FAKE_DOCKER)
    path.chmod(0o755)
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(env / "docker.log"))
    return path


def docker_calls(env):
    log = env / "docker.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_stop_stops_the_container_then_closes_the_window(env, share, docker, monkeypatch):
    a, _ = share
    register_session(env)
    monkeypatch.setenv("FAKE_DOCKER_PS", "c0ffee")
    monkeypatch.setenv("FAKE_DOCKER_EXIT", str(env / "tasks" / "dn-abc23" / "exit"))
    (env / "tmux.log").write_text("")
    code, _, err = run(a, "--docker", str(docker), "stop", "dn-abc23")
    assert code == 0, err
    assert docker_calls(env) == [
        ["ps", "-q", "--filter", "volume=dejima_isolated-work-0123456789abcdef"],
        ["stop", "c0ffee"],
    ]
    assert tmux_calls(env) == [["kill-window", "-t", "@7"]]


def test_stop_before_the_container_exists_ends_dejima(env, share, docker):
    a, _ = share
    starting = subprocess.Popen(["sleep", "30"])
    (env / "tasks" / "dn-abc23" / "pid").write_text(str(starting.pid))
    (env / "tmux.log").write_text("")
    code, _, err = run(a, "--docker", str(docker), "--stop-timeout", "1", "stop", "dn-abc23")
    assert code == 0, err
    assert starting.wait(timeout=5) == -15
    assert tmux_calls(env) == [["kill-window", "-t", "@7"]]


def test_run_id_comes_from_compose_environment_without_remote_access(env, share, docker, monkeypatch):
    a, _ = share
    dejima = subprocess.Popen(
        ["sh", "-c", "SANDBOX_ISOLATED_WORK=dejima_isolated-work-fedcba9876543210 sleep 30 & wait"],
        start_new_session=True,
    )
    try:
        (env / "tasks" / "dn-abc23" / "pid").write_text(str(dejima.pid))
        monkeypatch.setenv("FAKE_DOCKER_PS", "c0ffee")
        monkeypatch.setenv("FAKE_DOCKER_EXIT", str(env / "tasks" / "dn-abc23" / "exit"))
        __import__("time").sleep(0.2)
        assert run(a, "--docker", str(docker), "stop", "dn-abc23")[0] == 0
        assert docker_calls(env)[0][-1] == "volume=dejima_isolated-work-fedcba9876543210"
    finally:
        os.killpg(dejima.pid, __import__("signal").SIGKILL)
        dejima.wait()


def test_stop_refuses_a_task_that_is_not_running(env, share, docker):
    a, _ = share
    (env / "tasks" / "dn-abc23" / "exit").write_text("0\n")
    code, _, err = run(a, "--docker", str(docker), "stop", "dn-abc23")
    assert code == 1 and "not running" in err
    assert docker_calls(env) == []


def test_exec_records_dejima_pid(env):
    launch(args(env), {"id": "dn-abc23", "prompt": "p"})
    task = env / "tasks" / "dn-abc23"
    subprocess.run([sys.executable, "-m", "darner.launch", "--exec", str(task)], cwd=Path(__file__).parent.parent, check=True,
                   env={**os.environ, "FAKE_DEJIMA_LOG": str(env / "dejima.log")})
    assert int((task / "pid").read_text()) > 0


FAKE_DEJIMA_ALLOW = """#!/bin/sh
python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@" >> "$FAKE_ALLOW_LOG"
case "$*" in
    *blocked.example*) echo "blocked.example: unreachable after allowing" >&2; exit 1 ;;
    *quiet.example*) echo "WARNING: quiet.example is still NOT reachable (curl exit 28)." >&2; exit 2 ;;
    *wordy.example*) python3 -c 'import sys; sys.stderr.write("explained at length " * 40 + "the end")'; exit 2 ;;
esac
"""


@pytest.fixture
def allow_env(env, share, docker, monkeypatch):
    a, shared = share
    tool = env / "bin" / "dejima-allow"
    tool.write_text(FAKE_DEJIMA_ALLOW)
    tool.chmod(0o755)
    monkeypatch.setenv("FAKE_ALLOW_LOG", str(env / "allow.log"))
    register_session(env)
    monkeypatch.setenv("FAKE_DOCKER_PS", "c0ffee")
    return a, shared


def request_domains(a, shared, text):
    (shared / "DOMAINS").write_text(text)
    (shared / ".domains").touch()
    code, out, err = run(a, "domains", "dn-abc23")
    assert code == 0, err
    return json.loads(out)


def decide(a, verb, number, data, *extra):
    import unittest.mock

    with unittest.mock.patch("sys.stdin", io.StringIO(json.dumps(data))):
        return run(a, "--docker", str(a.tasks_dir.parent / "docker"), *extra, verb, "dn-abc23", str(number))


def allow_calls(env):
    log = env / "allow.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_domain_request_is_validated_and_saved(env, allow_env):
    a, shared = allow_env
    assert status(a)[0]["domains"] is False
    (shared / ".domains").touch()
    assert status(a)[0]["domains"] is True
    request = request_domains(a, shared, (
        "PyPI.org  # packages\n"
        "internal mirror.internal.example\n"
        "pypi.org\n"
        "not a domain\n"
        "localhost\n"
        "$(id).example.com\n"
        "\n# just a comment\n"
    ))
    assert request == {
        "request": 1,
        "domains": [{"domain": "pypi.org", "internal": False, "port": None}, {"domain": "mirror.internal.example", "internal": True, "port": None}],
        "rejected": 3,
    }
    assert json.loads((env / "tasks" / "dn-abc23" / "domains-pending").read_text()) == request
    assert status(a)[0]["domains"] is False


def test_domain_request_is_capped(allow_env):
    a, shared = allow_env
    request = request_domains(a, shared, "".join(f"d{i}.example.com\n" for i in range(25)))
    assert len(request["domains"]) == 20 and request["rejected"] == 5


def test_symlinked_domain_request_is_empty(env, allow_env):
    a, shared = allow_env
    (env / "secret").write_text("secret.example.com\n")
    (shared / "DOMAINS").symlink_to(env / "secret")
    (shared / ".domains").touch()
    assert json.loads(run(a, "domains", "dn-abc23")[1])["domains"] == []


def test_allow_grants_the_saved_request_within_what_the_api_passes(env, allow_env):
    a, shared = allow_env
    request_domains(a, shared, "pypi.org\ninternal mirror.internal.example\nother.example.com\n")
    # The agent rewriting DOMAINS after the request has no effect on what's granted.
    (shared / "DOMAINS").write_text("evil.example.com\n")
    data = {"requester": "@alice:example.org", "domains": ["pypi.org", "mirror.internal.example", "evil.example.com"]}
    code, out, err = decide(a, "allow", 1, data)
    assert code == 0, err
    assert json.loads(out) == {"granted": ["pypi.org", "mirror.internal.example"], "failed": [], "unverified": [], "told": False}
    dejima_allow = str(env / "bin" / "dejima-allow")
    assert allow_calls(env) == [
        ["--container", "c0ffee", "pypi.org"],
        ["--container", "c0ffee", "--internal", "mirror.internal.example"],
    ]
    assert not (env / "tasks" / "dn-abc23" / "domains-pending").exists()
    assert json.loads((env / "tasks" / "dn-abc23" / "domains-granted").read_text()) == ["pypi.org", "mirror.internal.example"]
    assert decide(a, "allow", 1, data)[0] == 1


def test_allow_reports_failures_and_tells_the_agent(env, allow_env):
    a, shared = allow_env
    request_domains(a, shared, "pypi.org\nblocked.example\n")
    (env / "tmux.log").write_text("")
    code, out, _ = decide(
        a, "allow", 1, {"requester": "@alice:example.org", "domains": ["pypi.org", "blocked.example"]},
        "--answers", "--paste-delay", "0",
    )
    result = json.loads(out)
    assert result["granted"] == ["pypi.org"] and result["told"] is True
    assert result["failed"] == [{"domain": "blocked.example", "error": "blocked.example: unreachable after allowing"}]
    assert (env / "tmux.log.buffer").read_text() == (
        "Domain request from @alice:example.org: allowed pypi.org; couldn't allow blocked.example "
        "(blocked.example: unreachable after allowing). Carry on with your task."
    )


def test_superseded_request_is_refused(allow_env):
    a, shared = allow_env
    request_domains(a, shared, "pypi.org\n")
    import time

    time.sleep(0.05)
    (shared / ".domains").touch()
    for name in ("consumed-domains",):
        os.utime(a.tasks_dir / "dn-abc23" / name, (0, 0))
    assert request_domains(a, shared, "files.pythonhosted.org\n")["request"] == 2
    code, _, err = decide(a, "allow", 1, {"requester": "@a:example.org", "domains": ["pypi.org"]})
    assert code == 1 and "replaced by a newer one" in err
    assert decide(a, "allow", 2, {"requester": "@a:example.org", "domains": ["files.pythonhosted.org"]})[0] == 0


def test_deny_discards_the_request(env, allow_env):
    a, shared = allow_env
    request_domains(a, shared, "pypi.org\n")
    code, out, _ = decide(a, "deny", 1, {"requester": "@alice:example.org"})
    assert code == 0 and json.loads(out) == {"told": False}
    assert decide(a, "allow", 1, {"requester": "@a:example.org", "domains": ["pypi.org"]})[0] == 1
    assert allow_calls(env) == []


def test_allow_needs_the_container(env, allow_env, monkeypatch):
    a, shared = allow_env
    request_domains(a, shared, "pypi.org\n")
    monkeypatch.delenv("FAKE_DOCKER_PS")
    code, _, err = decide(a, "allow", 1, {"requester": "@a:example.org", "domains": ["pypi.org"]})
    assert code == 1 and "couldn't find dn-abc23's container" in err
    assert (a.tasks_dir / "dn-abc23" / "domains-pending").exists()


@pytest.mark.parametrize("command", ["allow dn-abc23", "allow dn-abc23 x", "deny dn-abc23 1 2", "allow dn-abc23 ١"])
def test_bad_decision_commands_are_refused(share, monkeypatch, command):
    a, _ = share
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", command)
    assert run(a)[0] == 2


def test_domain_limit_counts_launch_domains_and_earlier_grants(env, allow_env):
    a, shared = allow_env
    (env / "tasks" / "dn-abc23" / "domains").write_text("one.example.com\n")
    request_domains(a, shared, "two.example.com\n")
    data = {"requester": "@a:example.org", "domains": ["two.example.com"], "max_domains": 3}
    assert json.loads(decide(a, "allow", 1, data)[1])["granted"] == ["two.example.com"]

    import time

    time.sleep(0.05)
    os.utime(a.tasks_dir / "dn-abc23" / "consumed-domains", (0, 0))
    request_domains(a, shared, "three.example.com\nfour.example.com\n")
    data = {"requester": "@a:example.org", "domains": ["three.example.com", "four.example.com"], "max_domains": 3}
    result = json.loads(decide(a, "allow", 2, data)[1])
    assert result["granted"] == ["three.example.com"]
    assert result["failed"] == [{"domain": "four.example.com", "error": "over this session's limit of 3 domains"}]


def message_run(a, data, *extra):
    import unittest.mock

    with unittest.mock.patch("sys.stdin", io.StringIO(json.dumps(data))):
        return run(a, *extra, "message", "dn-abc23")


def test_message_needs_no_help_request(env, share):
    a, _ = share
    (env / "tmux.log").write_text("")
    code, _, err = message_run(a, {"requester": "@alice:example.org", "text": "Also check the logs."}, "--answers", "--paste-delay", "0")
    assert code == 0, err
    assert (env / "tmux.log.buffer").read_text() == "Also check the logs."


def test_message_answers_an_outstanding_help_request(env, asked):
    code, _, _ = message_run(asked, {"requester": "@a:example.org", "text": "use main"}, "--answers", "--paste-delay", "0")
    assert code == 0
    assert not (env / "tasks" / "dn-abc23" / "help-pending").exists()
    assert status(asked)[0]["awaiting_answer"] is False


def test_message_needs_answers_enabled(env, share):
    a, _ = share
    code, _, err = message_run(a, {"requester": "@a:example.org", "text": "x"})
    assert code == 1 and "not enabled" in err


def test_status_reports_what_a_session_waits_on(env, asked, allow_env):
    a, shared = allow_env
    assert status(a)[0]["awaiting_answer"] is True
    request_domains(a, shared, "pypi.org\n")
    assert status(a)[0]["domain_request"] == 1


def test_changed_domains_file_is_a_request_once_settled(env, allow_env):
    a, shared = allow_env
    (shared / "DOMAINS").write_text("pypi.org\n")
    assert status(a)[0]["domains"] is False
    age(shared / "DOMAINS", 60)
    assert status(a)[0]["domains"] is True
    code, out, _ = run(a, "domains", "dn-abc23")
    assert code == 0 and json.loads(out)["domains"] == [{"domain": "pypi.org", "internal": False, "port": None}]
    assert status(a)[0]["domains"] is False


def test_rerequest_after_a_decision_goes_straight_through(env, allow_env):
    a, shared = allow_env
    request_domains(a, shared, "recipes.example.com\n")
    decide(a, "allow", 1, {"requester": "@a:example.org", "domains": ["recipes.example.com"]})
    # The agent edits its request without signalling again.
    (shared / "DOMAINS").write_text("internal recipes.example.com\n")
    age(shared / "DOMAINS", 60)
    assert status(a)[0]["domains"] is True
    request = json.loads(run(a, "domains", "dn-abc23")[1])
    assert request["request"] == 2 and request["domains"] == [{"domain": "recipes.example.com", "internal": True, "port": None}]


def test_new_request_waits_while_one_is_undecided(env, allow_env):
    a, shared = allow_env
    request_domains(a, shared, "pypi.org\n")
    import time

    time.sleep(0.05)
    (shared / ".domains").touch()
    assert status(a)[0]["domains"] is False
    assert run(a, "domains", "dn-abc23")[0] == 1


def test_permitted_but_unverified_domain_counts_as_allowed(env, allow_env):
    a, shared = allow_env
    request_domains(a, shared, "internal quiet.example\n")
    (env / "tmux.log").write_text("")
    code, out, _ = decide(
        a, "allow", 1, {"requester": "@a:example.org", "domains": ["quiet.example"]}, "--answers", "--paste-delay", "0"
    )
    result = json.loads(out)
    assert code == 0 and result["granted"] == ["quiet.example"] and result["failed"] == []
    assert result["unverified"] == [{"domain": "quiet.example", "warning": "WARNING: quiet.example is still NOT reachable (curl exit 28)."}]
    assert "allowed but not reachable on port 443 when checked: quiet.example" in (env / "tmux.log.buffer").read_text()
    assert json.loads((a.tasks_dir / "dn-abc23" / "domains-granted").read_text()) == ["quiet.example"]


def test_dejima_allow_message_reaches_the_requester_whole(env, allow_env):
    a, shared = allow_env
    request_domains(a, shared, "wordy.example\n")
    result = json.loads(decide(a, "allow", 1, {"requester": "@a:example.org", "domains": ["wordy.example"]})[1])
    assert result["unverified"][0]["warning"].endswith("the end")


def test_keep_records_is_passed_to_every_session(env):
    launch(args(env, "--keep-records"), {"id": "dn-abc23", "prompt": "p"})
    assert written_argv(env, "dn-abc23")[1:5] == ["--isolated", "--no-pause", "--shared-path-file", str(env / "tasks" / "dn-abc23" / "shared-path")]
    assert "--keep-records" in written_argv(env, "dn-abc23")[:-2]


def test_records_are_not_kept_by_default(env):
    launch(args(env), {"id": "dn-abc23", "prompt": "p"})
    assert "--keep-records" not in written_argv(env, "dn-abc23")


def upload_run(a, name, data, *extra, requester="@alice:example.org"):
    import unittest.mock

    stdin = type("Stdin", (), {"buffer": io.BytesIO(json.dumps({"name": name, "requester": requester}).encode() + b"\n" + data)})()
    with unittest.mock.patch("sys.stdin", stdin):
        return run(a, *extra, "upload", "dn-abc23")


def test_upload_lands_in_shared_uploads(env, share):
    a, shared = share
    code, out, err = upload_run(a, "access.log", b"line one\n")
    assert code == 0, err
    assert json.loads(out) == {"path": "~/shared/uploads/access.log", "size": 9, "told": False}
    assert (shared / "uploads" / "access.log").read_bytes() == b"line one\n"


@pytest.mark.parametrize(
    "name, saved",
    [("../../etc/passwd", "passwd"), (".bashrc", "bashrc"), ("con.txt", "upload"), ("my report (1).pdf", "my-report-1.pdf"), ("ünïcode.txt", "ncode.txt"), ("", "upload")],
)
def test_upload_names_are_made_safe(share, name, saved):
    a, shared = share
    assert json.loads(upload_run(a, name, b"x")[1])["path"] == f"~/shared/uploads/{saved}"


def test_upload_never_overwrites(share):
    a, shared = share
    upload_run(a, "notes.tar.gz", b"first")
    assert json.loads(upload_run(a, "notes.tar.gz", b"second")[1])["path"] == "~/shared/uploads/notes-2.tar.gz"
    assert (shared / "uploads" / "notes.tar.gz").read_bytes() == b"first"


def test_upload_wont_follow_a_planted_file_link(env, share):
    a, shared = share
    (shared / "uploads").mkdir()
    victim = env / "authorized_keys"
    victim.write_text("keep me")
    (shared / "uploads" / "key.pub").symlink_to(victim)
    assert json.loads(upload_run(a, "key.pub", b"attacker")[1])["path"] == "~/shared/uploads/key-2.pub"
    assert victim.read_text() == "keep me"


def test_upload_wont_follow_a_planted_directory_link(env, share):
    a, shared = share
    outside = env / "outside"
    outside.mkdir()
    (shared / "uploads").symlink_to(outside)
    code, _, err = upload_run(a, "x.txt", b"x")
    assert code == 1 and "isn't a directory" in err
    assert list(outside.iterdir()) == []


def test_upload_size_limit(share):
    a, shared = share
    code, _, err = upload_run(a, "big.bin", b"x" * 11, "--max-upload", "10")
    assert code == 1 and "over the 10-byte upload limit" in err
    assert not (shared / "uploads").exists()


def test_upload_tells_the_agent(env, share):
    a, shared = share
    (env / "tmux.log").write_text("")
    upload_run(a, "access.log", b"abc", "--answers", "--paste-delay", "0")
    assert (env / "tmux.log.buffer").read_text() == "Upload from @alice:example.org: ~/shared/uploads/access.log (3 bytes)."


def test_upload_needs_a_running_session(env, share):
    a, _ = share
    (env / "tasks" / "dn-abc23" / "exit").write_text("0\n")
    code, _, err = upload_run(a, "x.txt", b"x")
    assert code == 1 and "not running" in err


def test_upload_error_names_no_host_path(env, share):
    a, _ = share
    (env / "tasks" / "dn-abc23" / "shared-path").write_text("/somewhere/else\n")
    code, _, err = upload_run(a, "x.txt", b"x")
    assert code == 1 and "couldn't open the session's ~/shared" in err and "/somewhere" not in err


@pytest.mark.parametrize(
    "job, expected",
    [
        ({}, ["--model", "sonnet", "--effort", "medium"]),
        ({"model": "opus", "effort": "high"}, ["--model", "opus", "--effort", "high"]),
        ({"harness": "cline"}, ["--model", "z-ai/glm-5.3", "--context-window", "30000"]),
        ({"harness": "cline", "context_window": 60000}, ["--model", "z-ai/glm-5.3", "--context-window", "60000"]),
        ({"harness": "other"}, []),
    ],
)
def test_model_and_effort_resolution(env, job, expected):
    a = args(
        env, "--harness", "claude-code", "--model", "claude-code=sonnet", "--effort", "claude-code=medium",
        "--model", "cline=z-ai/glm-5.3", "--context-window", "cline=30000",
    )
    launch(a, {"id": "dn-abc23", "prompt": "p", **job})
    argv = written_argv(env, "dn-abc23")
    flags = argv[argv.index("--harness") + 2 : argv.index("--")]
    assert flags == expected


@pytest.mark.parametrize("option, value", [
    ("--model", "claude-code=bad value"), ("--effort", "claude-code=max"),
    ("--context-window", "cline=511"), ("--context-window", "cline=３２７６８"), ("--context-window", "cline=lots"),
])
def test_bad_per_harness_defaults_are_refused(env, option, value):
    with pytest.raises(SystemExit):
        args(env, option, value)


def test_domain_ports_are_checked_on_that_port(env, allow_env):
    a, shared = allow_env
    request = request_domains(a, shared, "internal recipes.example.com:8443\npypi.org:99999\nfiles.example.com:\n")
    assert request["domains"] == [{"domain": "recipes.example.com", "internal": True, "port": 8443}]
    assert request["rejected"] == 2
    decide(a, "allow", 1, {"requester": "@a:example.org", "domains": ["recipes.example.com"]})
    assert allow_calls(env) == [["--container", "c0ffee", "--internal", "--port", "8443", "recipes.example.com"]]


def test_successful_typing_leaves_no_log(env, asked):
    answer_run(asked, {"requester": "@a:example.org", "text": "use main"}, "--answers", "--paste-delay", "0")
    assert not (env / "tasks" / "dn-abc23" / "typing.log").exists()


def test_failed_typing_is_logged_for_the_operator_only(env, asked, monkeypatch):
    monkeypatch.setenv("FAKE_TMUX_DROP", "3")
    answer_run(asked, {"requester": "@a:example.org", "text": "use main"}, "--answers", "--paste-delay", "0")
    log = env / "tasks" / "dn-abc23" / "typing.log"
    assert log.stat().st_mode & 0o777 == 0o600
    text = log.read_text()
    assert "===== before\n" in text and "===== after paste\n main\n" in text


@pytest.mark.parametrize("keep", [False, True])
def test_typing_log_goes_when_the_session_ends_unless_records_are_kept(env, keep):
    dejima = env / "dejima"
    dejima.write_text("#!/bin/sh\n")
    dejima.chmod(0o755)
    launch(args(env, "--dejima", str(dejima), *(["--keep-records"] if keep else [])), {"id": "dn-abc23", "prompt": "p"})
    task = env / "tasks" / "dn-abc23"
    (task / "typing.log").write_text("screens")
    subprocess.run([sys.executable, "-m", "darner.launch", "--exec", str(task)], cwd=Path(__file__).parent.parent, check=True)
    assert (task / "typing.log").exists() is keep


def test_lost_first_keystrokes_are_reported(env, asked, monkeypatch):
    monkeypatch.setenv("FAKE_TMUX_DROP", "3")
    code, _, err = answer_run(asked, {"requester": "@a:example.org", "text": "use main"}, "--answers", "--paste-delay", "0")
    assert code == 1 and "couldn't confirm it arrived and was sent" in err
    assert (env / "tasks" / "dn-abc23" / "help-pending").exists()


def test_an_eaten_enter_is_retried_then_reported(env, asked, monkeypatch):
    monkeypatch.setenv("FAKE_TMUX_EAT_ENTER", "1")
    code, _, err = answer_run(asked, {"requester": "@a:example.org", "text": "use main"}, "--answers", "--paste-delay", "0")
    assert code == 1 and "check the session" in err
    assert [c[-1] for c in tmux_calls(env) if c[0] == "send-keys"] == ["Enter", "Enter"]
    assert "===== after second enter" in (env / "tasks" / "dn-abc23" / "typing.log").read_text()


def test_a_collapsed_paste_counts_as_arrived(env, asked, monkeypatch):
    monkeypatch.setenv("FAKE_TMUX_DROP", "999")
    (env / "tmux.log.screen").write_text("  > [Pasted text #1 +3 lines]\n")
    code, _, err = answer_run(asked, {"requester": "@a:example.org", "text": "use main"}, "--answers", "--paste-delay", "0")
    assert code == 0, err


def test_internal_domains_are_exempted_for_the_launch(env):
    launch(args(env), {
        "id": "dn-abc23", "prompt": "p",
        "domains": ["pypi.org", "recipes.example.com"], "internal_domains": ["recipes.example.com"],
    })
    argv = written_argv(env, "dn-abc23")
    assert argv[argv.index("--internal-domain") + 1] == "recipes.example.com"
    assert argv.count("--internal-domain") == 1
    assert (env / "tasks" / "dn-abc23" / "domains").read_text() == "pypi.org\nrecipes.example.com\n"


def test_catalog_lists_what_dejima_has(env):
    root = env / "dejima-install"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "dejima").write_text("#!/bin/sh\n")
    (root / "bin" / "dejima").chmod(0o755)
    for kind, name in (("harness", "claude-code"), ("harness", "cline"), ("backend", "anthropic"), ("backend", "self-hosted")):
        (root / f"{kind}s" / name).mkdir(parents=True)
        (root / f"{kind}s" / name / f"{kind}.conf").write_text("")
    (root / "backends" / "notes").mkdir()
    code, out, _ = run(args(env), "--dejima", str(root / "bin" / "dejima"), "--harness", "claude-code", "catalog")
    assert code == 0
    assert json.loads(out) == {
        "harnesses": ["claude-code", "cline"], "backends": ["anthropic", "self-hosted"], "default_harness": "claude-code",
    }


def b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode()


def test_files_are_held_owner_only_until_delivery(env):
    launch(args(env), {"id": "dn-abc23", "prompt": "p", "files": [{"name": "token", "data": b64(b"secret")}]})
    held = env / "tasks" / "dn-abc23" / "files"
    assert held.stat().st_mode & 0o777 == 0o700
    assert (held / "token").stat().st_mode & 0o777 == 0o600 and (held / "token").read_bytes() == b"secret"


SESSION_DEJIMA = """#!/bin/sh
sleep 1
ls -l "$SHARE/uploads" > "$SEEN"
cat "$SHARE/uploads/token" >> "$SEEN"
"""


def test_files_are_given_to_the_session_and_taken_back(env, monkeypatch):
    root = env / "share-root"
    shared = root / "20261027-120000-AbC123"
    shared.mkdir(parents=True)
    dejima = env / "session-dejima"
    dejima.write_text(SESSION_DEJIMA)
    dejima.chmod(0o755)
    a = args(env, "--dejima", str(dejima), "--share-root", str(root))
    launch(a, {"id": "dn-abc23", "prompt": "p", "files": [{"name": "token", "data": b64(b"secret")}]})
    task = env / "tasks" / "dn-abc23"
    (task / "shared-path").write_text(f"{shared}\n")
    subprocess.run(
        [sys.executable, "-m", "darner.launch", "--share-root", str(root), "--exec", str(task)],
        cwd=Path(__file__).parent.parent, check=True,
        env={**os.environ, "SHARE": str(shared), "SEEN": str(env / "seen")},
    )
    seen = (env / "seen").read_text()
    assert "-rw-------" in seen and seen.endswith("secret")
    assert not (shared / "uploads" / "token").exists()
    assert not (task / "files").exists() and not (task / "files-delivered").exists()


def test_delivery_wont_follow_a_planted_uploads_link(env):
    from darner.launch import _deliver_files
    import threading

    root = env / "share-root"
    shared = root / "20261027-120000-AbC123"
    shared.mkdir(parents=True)
    outside = env / "outside"
    outside.mkdir()
    (shared / "uploads").symlink_to(outside)
    a = args(env, "--share-root", str(root))
    launch(a, {"id": "dn-abc23", "prompt": "p", "files": [{"name": "token", "data": b64(b"secret")}]})
    task = env / "tasks" / "dn-abc23"
    (task / "shared-path").write_text(f"{shared}\n")
    _deliver_files(a, task, threading.Event())
    assert list(outside.iterdir()) == []


def test_failed_launch_removes_held_files(env, monkeypatch):
    broken = env / "broken-tmux"
    broken.write_text("#!/bin/sh\necho nope >&2\nexit 1\n")
    broken.chmod(0o755)
    with pytest.raises(RuntimeError):
        launch(args(env, "--tmux", str(broken)), {"id": "dn-abc23", "prompt": "p", "files": [{"name": "token", "data": b64(b"secret")}]})
    assert not (env / "tasks" / "dn-abc23" / "files").exists()


def test_cleanup_continues_past_a_name_the_session_replaced(env):
    from darner.launch import _remove_files

    root = env / "share-root"
    shared = root / "20261027-120000-AbC123"
    (shared / "uploads" / "first").mkdir(parents=True)
    (shared / "uploads" / "first" / "kept").write_text("x")
    (shared / "uploads" / "second").write_text("secret")
    a = args(env, "--share-root", str(root))
    launch(a, {"id": "dn-abc23", "prompt": "p"})
    task = env / "tasks" / "dn-abc23"
    (task / "shared-path").write_text(f"{shared}\n")
    (task / "files-delivered").write_text(json.dumps(["first", "second"]))
    _remove_files(a, task)
    assert not (shared / "uploads" / "second").exists()
    assert not (task / "files-delivered").exists()


def test_status_sweeps_held_files_of_an_ended_session(env, share):
    a, _ = share
    task = env / "tasks" / "dn-abc23"
    (task / "files").mkdir()
    (task / "files" / "token").write_text("secret")
    (task / "exit").write_text("0\n")
    status(a)
    assert not (task / "files").exists()


def test_screen_quiet_time_and_tail(env, share):
    a, _ = share
    screen = env / "tmux.log.screen"
    screen.write_text("\n".join(f"line {i}" for i in range(30)) + "\n\x1b[31m> \n")
    entry = status(a)[0]
    assert entry["quiet"] < 5
    assert entry["screen"].splitlines()[-1] == "[31m>" and entry["screen"].splitlines()[0] == "line 11"
    record = env / "tasks" / "dn-abc23" / "screen"
    state = json.loads(record.read_text())
    record.write_text(json.dumps({**state, "since": state["since"] - 600}))
    assert status(a)[0]["quiet"] >= 600
    screen.write_text("something new\n")
    assert status(a)[0]["quiet"] < 5


def test_result_pending_until_delivered(env, share):
    a, shared = share
    (shared / "RESULT.md").write_text("done")
    assert status(a)[0]["result_pending"] is True
    quiet(shared)
    run(a, "fetch", "dn-abc23")
    assert status(a)[0]["result_pending"] is False


FAKE_DEJIMA_SHARE = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_SHARE_LOG"
if [ "$1" = --help ]; then
    if [ -n "$FAKE_SHARE_OLD" ]; then echo "Usage: dejima-share <path|list|adopt|prune> [path]"; echo "  prune --isolated  delete every ended share"
    else echo "Usage: dejima-share <path|list|adopt|prune> [path] [--yes]"; echo "  prune --isolated [share]  delete one"; echo "  --yes  answer y"; fi
    exit 0
fi
[ -n "$FAKE_SHARE_REFUSE" ] && { echo "dejima-share: $FAKE_SHARE_REFUSE" >&2; exit 1; }
[ -n "$FAKE_SHARE_NOOP" ] && { echo "Kept."; exit 0; }
rm -r "$4"
"""


@pytest.fixture
def dejima_share(env, monkeypatch):
    tool = env / "bin" / "dejima-share"
    tool.parent.mkdir(exist_ok=True)
    tool.write_text(FAKE_DEJIMA_SHARE)
    tool.chmod(0o755)
    monkeypatch.setenv("FAKE_SHARE_LOG", str(env / "share.log"))
    return tool


def prunes(env):
    log = env / "share.log"
    return [line for line in log.read_text().splitlines() if line.startswith("prune")] if log.exists() else []


@pytest.fixture
def delivered(env, share, dejima_share):
    """A session whose output was fetched, and which has since exited."""
    a, shared = share
    (shared / "RESULT.md").write_text("done")
    (shared / ".done").touch()
    code, out, err = run(a, "fetch", "dn-abc23")
    assert code == 0, err
    (env / "tasks" / "dn-abc23" / "exit").write_text("0\n")
    return a, shared, sha(out)


def sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


ANY_ZIP = "0" * 64


def forget_delivered(a, dejima_share, stored, *extra):
    return run(a, "--dejima-share", str(dejima_share), *extra, "forget-delivered", "dn-abc23", stored)


def test_forget_delivered_prunes_a_share_that_still_holds_its_delivery(env, delivered, dejima_share):
    a, shared, stored = delivered
    code, _, err = forget_delivered(a, dejima_share, stored)
    assert code == 0, err
    assert prunes(env) == [f"prune --isolated --yes {shared}"]
    assert not shared.exists() and not (env / "tasks" / "dn-abc23").exists()


def test_plain_forget_never_prunes(env, delivered, dejima_share):
    a, shared, stored = delivered
    assert run(a, "--dejima-share", str(dejima_share), "forget", "dn-abc23")[0] == 0
    assert prunes(env) == [] and shared.is_dir() and not (env / "tasks" / "dn-abc23").exists()


def test_forget_still_refuses_a_running_session(env, share, dejima_share):
    a, shared = share
    code, _, err = forget_delivered(a, dejima_share, ANY_ZIP)
    assert code == 1 and "hasn't ended" in err and (env / "tasks" / "dn-abc23").is_dir()


def kept(env, a, shared, dejima_share, why, stored=ANY_ZIP):
    code, _, err = forget_delivered(a, dejima_share, stored)
    assert code == 1 and "kept its ~/shared for dejima-share prune --isolated by hand" in err and why in err, err
    assert prunes(env) == [] and shared.is_dir() and not (env / "tasks" / "dn-abc23").exists()


def test_no_recorded_delivery_keeps_the_share(env, share, dejima_share):
    a, shared = share
    (shared / "notes.md").write_text("work")
    (env / "tasks" / "dn-abc23" / "exit").write_text("0\n")
    kept(env, a, shared, dejima_share, "no complete delivery")


def test_a_rejected_fetch_keeps_the_share(env, share, dejima_share):
    a, shared = share
    (shared / "RESULT.md").write_text("x" * 100)
    (shared / ".done").touch()
    assert run(a, "--max-output", "10", "fetch", "dn-abc23")[0] == FETCH_REJECTED
    (env / "tasks" / "dn-abc23" / "exit").write_text("0\n")
    kept(env, a, shared, dejima_share, "no complete delivery")


def test_a_share_changed_after_delivery_is_kept(env, delivered, dejima_share):
    a, shared, stored = delivered
    (shared / "later.md").write_text("written after the delivery")
    kept(env, a, shared, dejima_share, "changed after its output was delivered", stored)


def test_a_delivery_that_left_files_out_keeps_the_share(env, share, dejima_share):
    a, shared = share
    (shared / "RESULT.md").write_text("done")
    (shared / "link").symlink_to("/etc/passwd")
    (shared / "uploads").mkdir()
    (shared / ".done").touch()
    code, out, _ = run(a, "fetch", "dn-abc23")
    assert code == 0
    (env / "tasks" / "dn-abc23" / "exit").write_text("0\n")
    kept(env, a, shared, dejima_share, "left out 1 entries", sha(out))


def test_uploads_alone_left_out_still_prunes(env, share, dejima_share):
    a, shared = share
    (shared / "RESULT.md").write_text("done")
    (shared / "uploads").mkdir()
    (shared / "uploads" / "token").write_text("s3cret")
    (shared / ".done").touch()
    code, out, _ = run(a, "fetch", "dn-abc23")
    assert code == 0
    (env / "tasks" / "dn-abc23" / "exit").write_text("0\n")
    assert forget_delivered(a, dejima_share, sha(out))[0] == 0 and not shared.exists()


def test_a_dejima_share_without_the_named_form_isn_t_run(env, delivered, dejima_share, monkeypatch):
    a, shared, stored = delivered
    monkeypatch.setenv("FAKE_SHARE_OLD", "1")
    kept(env, a, shared, dejima_share, "can't prune one share by name", stored)


def test_a_prune_that_exits_0_but_leaves_the_share_is_reported(env, delivered, dejima_share, monkeypatch):
    a, shared, stored = delivered
    monkeypatch.setenv("FAKE_SHARE_NOOP", "1")
    code, _, err = forget_delivered(a, dejima_share, stored)
    assert code == 1 and "exited 0 but the share is still there: Kept." in err and shared.is_dir()


def test_a_share_dejima_refuses_is_kept(env, delivered, dejima_share, monkeypatch):
    a, shared, stored = delivered
    monkeypatch.setenv("FAKE_SHARE_REFUSE", "a container still mounts it")
    code, _, err = forget_delivered(a, dejima_share, stored)
    assert code == 1 and "dejima-share: a container still mounts it" in err and shared.is_dir()


def test_a_missing_dejima_share_keeps_the_share_and_the_task_still_goes(env, delivered):
    a, shared, stored = delivered
    code, _, err = run(a, "--dejima-share", "no-such-dejima-share", "forget-delivered", "dn-abc23", stored)
    assert code == 1 and "pass --dejima-share with its full path" in err and shared.is_dir()


def test_an_unrunnable_dejima_share_is_reported(env, delivered):
    a, shared, stored = delivered
    broken = env / "bin" / "broken-share"
    broken.parent.mkdir(exist_ok=True)
    broken.write_text("not a program")
    broken.chmod(0o755)
    code, _, err = run(a, "--dejima-share", str(broken), "forget-delivered", "dn-abc23", stored)
    assert code == 1 and "couldn't run" in err and shared.is_dir() and not (env / "tasks" / "dn-abc23").exists()


def test_keep_shared_keeps_the_share(env, delivered, dejima_share):
    a, shared, stored = delivered
    assert forget_delivered(a, dejima_share, stored, "--keep-shared")[0] == 0
    assert prunes(env) == [] and shared.is_dir() and not (env / "tasks" / "dn-abc23").exists()


@pytest.mark.parametrize("recorded", [None, "gone", "/etc"])
def test_a_share_that_isn_t_there_or_isn_t_under_the_root_isn_t_pruned(env, delivered, dejima_share, recorded):
    a, shared, stored = delivered
    task_dir = env / "tasks" / "dn-abc23"
    if recorded is None:
        (task_dir / "shared-path").unlink()
    elif recorded == "gone":
        shutil.rmtree(shared)
    else:
        (task_dir / "shared-path").write_text(f"{recorded}\n")
    code, _, err = forget_delivered(a, dejima_share, stored)
    assert prunes(env) == [] and not task_dir.exists()
    if recorded == "/etc":
        assert code == 1 and "is not under" in err
    else:
        assert code == 0, err



def test_a_later_delivery_the_api_didn_t_store_keeps_the_share(env, share, dejima_share):
    a, shared = share
    (shared / "RESULT.md").write_text("first")
    (shared / ".done").touch()
    code, first, _ = run(a, "fetch", "dn-abc23")
    assert code == 0
    # A second delivery the API never stored, as when its fetch timed out.
    (shared / "RESULT.md").write_text("second")
    (shared / "more.py").write_text("print('the only copy')")
    (shared / ".done").touch()
    assert run(a, "fetch", "dn-abc23")[0] == 0
    (env / "tasks" / "dn-abc23" / "exit").write_text("0\n")
    kept(env, a, shared, dejima_share, "isn't the output the API stored", sha(first))
    assert (shared / "more.py").exists()


def test_an_unreadable_share_is_kept_and_the_task_still_goes(env, delivered, dejima_share):
    if os.geteuid() == 0:
        pytest.skip("root ignores permissions")
    a, shared, stored = delivered
    shared.chmod(0)
    try:
        kept(env, a, shared, dejima_share, "couldn't open it to compare with its delivery: Permission denied", stored)
    finally:
        shared.chmod(0o755)


@pytest.mark.parametrize("third", ["", "ABC" + "0" * 61, "0" * 63, "0" * 65, "g" * 64])
def test_forget_delivered_needs_a_sha256(share, monkeypatch, third):
    a, _ = share
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"forget-delivered dn-abc23 {third}".strip())
    assert run(a)[0] == 2
