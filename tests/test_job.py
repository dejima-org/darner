# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
import pytest

from darner.job import (
    OUTPUT_INSTRUCTIONS,
    JobError,
    check_harness,
    compose_prompt,
    new_task_id,
    valid_dependency,
    valid_domain,
    valid_name,
    validate_job,
)

SHA = "a" * 64


def test_new_task_id_is_valid():
    assert validate_job({"id": new_task_id(), "prompt": "x"})


@pytest.mark.parametrize("d", ["pypi.org", "files.pythonhosted.org", "a-b.example.com"])
def test_valid_domains(d):
    assert valid_domain(d)


@pytest.mark.parametrize(
    "d", ["localhost", "*.example.com", "a..b.com", "-a.com", "a.com;rm", "a.com\nb.com", "", 3]
)
def test_invalid_domains(d):
    assert not valid_domain(d)


@pytest.mark.parametrize(
    "d",
    [
        "apt:python3-venv",
        "pip:pytest-xdist",
        "cargo:ripgrep",
        "apt:g++",
        "npm:@scope/pkg@1.2.3",
        "npm:left-pad",
        f"tarball:zig@{SHA}:https://example.com/zig.tar.xz",
    ],
)
def test_valid_dependencies(d):
    assert valid_dependency(d)


@pytest.mark.parametrize(
    "d",
    [
        "python3",
        "pip:pytest@1.0",
        "apt:$(id)",
        "gem:rails",
        f"tarball:..@{SHA}:https://example.com/x",
        f"tarball:zig@{SHA}:http://example.com/x",
        "tarball:zig@abc:https://example.com/x",
        f"tarball:zig@{SHA}:https://example.com/a b",
    ],
)
def test_invalid_dependencies(d):
    assert not valid_dependency(d)


def test_names():
    assert valid_name("claude-code") and valid_name("anthropic-api-key")
    assert not valid_name("Claude") and not valid_name("-x") and not valid_name("a b")


def test_validate_job_full():
    job = validate_job(
        {
            "id": "dn-abc23",
            "prompt": "do it",
            "domains": ["pypi.org"],
            "dependencies": ["pip:pytest"],
            "harness": "cline",
            "backend": "openrouter",
        }
    )
    assert job.domains == ["pypi.org"] and job.harness == "cline"


@pytest.mark.parametrize(
    "d",
    [
        [],
        {"id": "x", "prompt": "p"},
        {"id": "dn-abc23", "prompt": " "},
        {"id": "dn-abc23", "prompt": "p\0"},
        {"id": "dn-abc23", "prompt": "x" * 70000},
        {"id": "dn-abc23", "prompt": "p", "identity": "me"},
        {"id": "dn-abc23", "prompt": "p", "domains": "pypi.org"},
        {"id": "dn-abc23", "prompt": "p", "domains": ["localhost"]},
        {"id": "dn-abc23", "prompt": "p", "dependencies": ["x"]},
        {"id": "dn-abc23", "prompt": "p", "harness": "Bad Name"},
        {"id": "dn-abc23", "prompt": "p", "session_wait": 61},
        {"id": "dn-abc23", "prompt": "p", "session_wait": True},
        {"id": "dn-abc23", "prompt": "p", "session_wait": "5"},
    ],
)
def test_validate_job_rejects(d):
    with pytest.raises(JobError):
        validate_job(d)


def test_compose_prompt():
    assert compose_prompt("Task\n", [], "", "") == "Task\n"
    assert compose_prompt("Task", [("myapp", "repo at X\n"), ("loki", "logs")], " ctx ", "Leave it in ~/shared.") == (
        "Task\n\n## Context: myapp\nrepo at X\n\n## Context: loki\nlogs\n\n"
        "## Context from requester\nctx\n\n## Delivering your output\nLeave it in ~/shared.\n"
    )


def test_compose_prompt_default_output_instructions():
    assert compose_prompt("Task", [], "").endswith(f"## Delivering your output\n{OUTPUT_INSTRUCTIONS}\n")


@pytest.mark.parametrize("model", ["opus", "claude-opus-5[1m]", "z-ai/glm-5.3", "deepseek/deepseek-r1:free", "llama3:8b"])
def test_valid_models(model):
    assert validate_job({"id": "dn-abc23", "prompt": "p", "model": model}).model == model


@pytest.mark.parametrize("job", [
    {"model": "-opus"}, {"model": "op us"}, {"model": "opus\n"}, {"model": "a" * 201},
    {"effort": "-high"}, {"effort": "HIGH"}, {"effort": 3}, {"effort": "None"}, {"effort": "a" * 33},
    {"context_window": 511}, {"context_window": 100_000_001}, {"context_window": "32768"}, {"context_window": True},
])
def test_invalid_model_and_effort(job):
    with pytest.raises(JobError):
        validate_job({"id": "dn-abc23", "prompt": "p", **job})


def test_context_window():
    assert validate_job({"id": "dn-abc23", "prompt": "p", "context_window": 32768}).context_window == 32768


@pytest.mark.parametrize("effort", ["none", "low", "xhigh"])
def test_valid_efforts(effort):
    assert validate_job({"id": "dn-abc23", "prompt": "p", "effort": effort}).effort == effort


OPTIONS = {
    "claude-code": {"backends": ["anthropic", "anthropic-api-key"], "efforts": ["low", "medium", "high", "xhigh"], "context_window": False},
    "cline": {"backends": ["anthropic", "openrouter", "self-hosted"], "efforts": ["none", "low", "high"], "context_window": True},
    "free": {"backends": ["self-hosted"], "efforts": None, "context_window": False},
    "plain": {"backends": ["self-hosted"], "efforts": [], "context_window": False},
}


@pytest.mark.parametrize("settings", [
    ("cline", "self-hosted", "none", 120000),
    ("claude-code", "anthropic-api-key", "xhigh", None),
    ("other", "anything", "none", 4096),
    (None, "openrouter", "none", None),
    ("free", None, "max", None),
])
def test_settings_a_harness_can_take(settings):
    check_harness(OPTIONS, *settings)


@pytest.mark.parametrize("settings", [
    ("claude-code", "self-hosted", None, None),
    ("claude-code", None, "none", None),
    ("claude-code", None, None, 4096),
    ("free", "openrouter", None, None),
])
def test_settings_a_harness_can_t_take(settings):
    with pytest.raises(JobError):
        check_harness(OPTIONS, *settings)


def test_a_harness_without_effort_levels_says_so():
    with pytest.raises(JobError, match="^plain takes no effort level$"):
        check_harness(OPTIONS, "plain", None, "low", None)


@pytest.mark.parametrize("network", ["named", "sealed"])
def test_networks(network):
    assert validate_job({"id": "dn-abc23", "prompt": "p", "network": network}).network == network


@pytest.mark.parametrize("network", ["open", "Sealed", "", True])
def test_invalid_networks(network):
    with pytest.raises(JobError, match="network"):
        validate_job({"id": "dn-abc23", "prompt": "p", "network": network})


def test_internal_domains_must_be_permitted():
    with pytest.raises(JobError, match="internal domains must also be permitted: recipes.example.com"):
        validate_job({"id": "dn-abc23", "prompt": "p", "domains": ["pypi.org"], "internal_domains": ["recipes.example.com"]})


def test_files_are_validated():
    import base64

    good = {"name": "token", "data": base64.b64encode(b"s").decode()}
    assert validate_job({"id": "dn-abc23", "prompt": "p", "files": [good]}).files == [("token", b"s")]
    for files in (
        [{"name": "../x", "data": ""}],
        [{"name": "token", "data": "not base64!"}],
        [good, good],
        [{"name": "big", "data": base64.b64encode(b"x" * (1024 * 1024 + 1)).decode()}],
        "token",
    ):
        with pytest.raises(JobError):
            validate_job({"id": "dn-abc23", "prompt": "p", "files": files})


def test_prompt_lists_files():
    assert "## Files for this task\nThe requester provided these, and they may hold credentials:\n- ~/shared/uploads/token" in compose_prompt("T", [], "", "", ["token"])

