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
    result = run(f"127.0.0.1:{listener}", str(listener))
    assert result.returncode == 1 and f"BAD: 127.0.0.1:{listener} answers" in result.stdout


def test_nothing_on_the_gateway_passes(listener):
    result = run(f"127.0.0.1:{listener}", str(closed_port()), str(closed_port()))
    assert result.returncode == 0 and "BAD" not in result.stdout


def test_a_failed_positive_control_proves_nothing(listener):
    result = run(f"127.0.0.1:{closed_port()}", str(closed_port()))
    assert result.returncode == 2 and "positive control failed" in result.stderr and "ok:" not in result.stdout
