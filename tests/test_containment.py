# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
"""tests/containment.sh, run against listeners on loopback standing in for the gateway."""

import os
import socket
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / "containment.sh"


@pytest.fixture
def listener():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        yield s.getsockname()[1]


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(*args):
    env = {**os.environ, "GATEWAY": "127.0.0.1"}
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)


def test_a_front_door_on_the_gateway_is_caught(listener):
    result = run("--gateway-control", str(listener), str(listener))
    assert result.returncode == 1 and f"BAD: 127.0.0.1:{listener} answers" in result.stdout


def test_nothing_on_the_gateway_passes(listener):
    result = run("--gateway-control", str(listener), str(closed_port()), str(closed_port()))
    assert result.returncode == 0 and "BAD" not in result.stdout and "gateway control" in result.stdout


def test_a_gateway_control_that_doesn_t_answer_proves_nothing():
    result = run("--gateway-control", str(closed_port()), str(closed_port()))
    assert result.returncode == 2 and "EGRESS_HOST_PORTS" in result.stderr and "ok:" not in result.stdout


def test_an_outside_control_says_it_may_only_have_checked_the_clamp(listener):
    result = run("--control", f"127.0.0.1:{listener}", str(closed_port()))
    assert result.returncode == 0 and "may be dejima's egress clamp rather than the bind" in result.stdout


def test_a_failed_outside_control_proves_nothing():
    result = run("--control", f"127.0.0.1:{closed_port()}", str(closed_port()))
    assert result.returncode == 2 and "control failed" in result.stderr


@pytest.mark.parametrize("args", [[], ["8081"], ["--port", "1", "8081"]])
def test_a_control_is_required(args):
    assert run(*args).returncode == 2
