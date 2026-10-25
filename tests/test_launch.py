import json
import subprocess
import sys
from pathlib import Path

import pytest

from meeseeks.launch import launch, main, parse_args

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
        "dejima", "--isolated", "--no-pause",
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
    assert written_argv(env, "ms-abc23") == ["dejima", "--isolated", "--no-pause", "--", "p"]


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
    assert written_argv(env, "ms-abc23")[3:-2] == ["--identity", "meeseeks", *expected]


def test_main_reports_invalid_job(env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO('{"id": "ms-abc23", "prompt": "p", "identity": "x"}'))
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
    assert json.loads((env / "dejima.log").read_text()) == ["--isolated", "--no-pause", "--", PROMPT]
