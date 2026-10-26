import pytest

from meeseeks.job import (
    OUTPUT_INSTRUCTIONS,
    JobError,
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
            "id": "ms-abc23",
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
        {"id": "ms-abc23", "prompt": " "},
        {"id": "ms-abc23", "prompt": "p\0"},
        {"id": "ms-abc23", "prompt": "x" * 70000},
        {"id": "ms-abc23", "prompt": "p", "identity": "me"},
        {"id": "ms-abc23", "prompt": "p", "domains": "pypi.org"},
        {"id": "ms-abc23", "prompt": "p", "domains": ["localhost"]},
        {"id": "ms-abc23", "prompt": "p", "dependencies": ["x"]},
        {"id": "ms-abc23", "prompt": "p", "harness": "Bad Name"},
        {"id": "ms-abc23", "prompt": "p", "session_wait": 61},
        {"id": "ms-abc23", "prompt": "p", "session_wait": True},
        {"id": "ms-abc23", "prompt": "p", "session_wait": "5"},
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
