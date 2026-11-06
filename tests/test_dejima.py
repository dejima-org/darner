"""What the launcher relies on from dejima, checked against a real dejima checkout.

docs/release.md lists the same things. DEJIMA_SRC names the checkout; CI sets
it to the dejima release CHANGELOG.md names, or dejima's development while
that's N/A or not tagged yet. Without it, these skip.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

from darner.job import Job
from darner.launch import _dejima_share, dejima_argv, dejima_defaults, dejima_root, harness_options, parse_args

SRC = Path(os.environ["DEJIMA_SRC"]) if os.environ.get("DEJIMA_SRC") else None
pytestmark = pytest.mark.skipif(SRC is None, reason="DEJIMA_SRC is not set")



@pytest.fixture
def home(tmp_path, monkeypatch):
    """A home with a dejima config, so dejima reads nothing of this machine's."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    config = tmp_path / ".config" / "dejima"
    config.mkdir(parents=True)
    (config / "config").write_text(
        "[core]\nharness = claude-code\n\n[harness.claude-code]\nbackend = anthropic\nmodel = claude-opus-5-5\neffort = high\n"
    )
    return tmp_path


def usage(tool):
    # --help-full, since --help is budgeted and a row may move down from it.
    return subprocess.run([str(SRC / "bin" / tool), "--help-full"], capture_output=True, text=True, timeout=30, check=True).stdout


def documented(flag, text):
    # Whole flags only, so --internal isn't found in --internal-domain.
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", text) is not None


def launcher_flags(tmp_path):
    """Every flag the launcher passes dejima, from a job setting every option."""
    dejima = str(SRC / "bin" / "dejima")
    a = parse_args(["--dejima", dejima, "--identity", "darner", "--keep-records"])
    flags = {"--print-config"}
    for network in ("named", "sealed"):
        job = Job(
            id="dn-abc23", prompt="p", domains=["example.org"], internal_domains=["internal.example.org"],
            dependencies=["apt:jq"], harness="cline", backend="anthropic", model="claude-opus-5-5", effort="high",
            context_window=200000, network=network,
        )
        argv = dejima_argv(a, job, tmp_path, dejima)
        flags |= {arg for arg in argv[: argv.index("--")] if arg.startswith("--")}
    return flags


def test_dejima_takes_every_flag_the_launcher_passes(home, tmp_path):
    text = usage("dejima")
    flags = launcher_flags(tmp_path)
    assert {"--isolated", "--context-window", "--require-sealed", "--drop-domains"} <= flags
    assert sorted(flag for flag in flags if not documented(flag, text)) == []


def test_dejima_allow_takes_what_grants_use():
    text = usage("dejima-allow")
    assert [flag for flag in ("--container", "--internal", "--port") if not documented(flag, text)] == []


def test_dejima_share_prunes_one_share_by_name(home):
    assert _dejima_share(parse_args(["--dejima-share", str(SRC / "bin" / "dejima-share")])) == (
        str(SRC / "bin" / "dejima-share"), None,
    )


def test_print_config_gives_the_defaults_the_launcher_reads(home):
    harness, defaults = dejima_defaults(parse_args([]), str(SRC / "bin" / "dejima"))
    assert harness == "claude-code"
    assert defaults["claude-code"] == {"backend": "anthropic", "model": "claude-opus-5-5", "effort": "high"}


def test_manifests_say_what_each_harness_can_be_set_to():
    options = harness_options(dejima_root(str(SRC / "bin" / "dejima")))
    assert options and all(o["backends"] for o in options.values())
    assert any(o["efforts"] for o in options.values()) and any(o["context_window"] for o in options.values())


def test_sessions_are_registered_and_named_as_the_launcher_finds_them():
    # Read from the source: both are written only by a real launch.
    source = (SRC / "bin" / "dejima").read_text()
    assert "printf 'pane=%s\\n'" in source and "printf 'pid=%s\\n' \"$$\"" in source
    assert 'SANDBOX_ISOLATED_WORK="dejima_isolated-work-$DEJIMA_RUN_ID"' in source
