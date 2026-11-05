# Developing darner

[← darner README](../README.md)

## Tests

```
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

To build the plugin locally, run `mbc build` in `plugin/`.

What the API image and CI install is pinned, with hashes, in `requirements/api.txt` and `requirements/test.txt`. To change a dependency, edit the matching `.in` file, and `pyproject.toml`'s extra if it's there, then regenerate the `.txt` with the `pip-compile` command at the top of the `.in`.

[`.gitea/workflows/tests.yml`](../.gitea/workflows/tests.yml) runs the tests on every pull request on a `dejima-confined` runner, and builds the API's image on a `dejima-privileged` one. What the jobs reach is declared in [`.dejima/allowed-domains`](../.dejima/allowed-domains), which the runners' egress allowlist reads from `main`.

## Releasing

Pull requests target `development`, which merges to `main` when it's ready to release. `development` carries the next version: the first change after a release bumps both `plugin/maubot.yaml` and `pyproject.toml`, and opens its entry in [`CHANGELOG.md`](../CHANGELOG.md). Each release names the dejima release it was released against, so it trails that dejima release. [release.md](release.md) is the release process.

[`.gitea/workflows/release.yml`](../.gitea/workflows/release.yml) publishes the plugin on a `dejima-confined` runner:

- **A push to `development`** creates a pre-release of `<version>.dev<run>`, such as `0.1.18.dev42`, with the plugin attached. Pre-releases aren't the forge's latest release, and maubot orders them before `<version>`, so a stable release supersedes one.
- **A push to `main`** whose version has no `v<version>` tag yet creates the stable release, and with it the tag at that commit. Its notes are the version's `CHANGELOG.md` entry, which must name a dejima release. A push that doesn't change the version releases nothing.

A pushed tag releases nothing; only these two branches do, so the forge must protect both (see [release.md](release.md)). The workflow fails if the two versions disagree. It uses the job's automatic `GITEA_TOKEN`, so Actions must be enabled for the repository, and the token must be allowed to create releases.

## Checking a frontend

The API passes on what the agent wrote with control and invisible characters removed, and nothing more: making it safe to show is each frontend's job. `POST /v1/selftest`, darner-web's **Self-test**, or `selftest` to the Matrix bot, sends text written like a hostile agent's ([`darner/selftest.py`](../darner/selftest.py)) as a finished task's output, help request and waiting screen. It arrives through `/v1/events` like any task's, so it goes down the same paths an agent's text does.

Each check says what you see if the frontend is safe (PASS) and what you see if it isn't (FAIL), then gives a test line on its own: raw HTML, scripts and HTML links must show as characters; markdown and Slack links and markdown images must show where they really point, not just "click here", and an address too long for the screen must wrap rather than be cut off; `@room` and the like, in Matrix or Slack syntax, must notify nobody; and the last check must arrive, so a cut-off message shows.

For the Matrix bot, `assert_shown_safely` in `tests/test_bot.py` checks those lines on each message it sends for the self-test, and is shown to fail on unescaped HTML, plain markdown and missing `m.mentions`. darner-web's are described in [web.md](web.md#checking-it). A new frontend should get a test like them.
