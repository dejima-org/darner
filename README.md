<!-- Fixed, not preference-aware, and that is the decision rather than an
     oversight. `prefers-color-scheme` reports the browser's setting, never the
     site's, and this is read on a forge that renders dark whichever way the
     browser is set - so anything conditional shows dark ink on a dark page to
     a reader whose browser prefers light, and does it silently.
     The suffix names the mode a file is FOR, not the colour of its ink:
     -darkmode is the pale wordmark that belongs on a dark page. GitHub does
     follow its own theme setting through <picture>, so the GitHub mirror
     replaces the tag below with one that falls back to
     dejima-wordmark-lightmode.png; see dejima's mirror/github-mirror.sh.
     Every &nbsp; here is spacing, and it has to be a character rather than CSS
     or a presentation attribute. Gitea's sanitizer strips `style` from the tag
     and from any wrapper, `hspace`/`vspace` survive sanitizing only to be
     ignored by browsers that dropped HTML4 presentation attributes, and a bare
     <br> ends a line nothing follows, which browsers collapse to no height.
     &nbsp; survives as a real U+00A0: on its own line it fills a paragraph
     that would otherwise be empty, and immediately before the tag it sits
     inline and indents the image by one space. -->
&nbsp;

&nbsp;<picture><source media="(prefers-color-scheme: dark)" srcset="assets/dejima-wordmark-darkmode.png"><img src="assets/dejima-wordmark-lightmode.png" alt="dejima" height="72"></picture>

---

> **This is a read-only mirror.** Development happens on the forge its
> maintainer runs, which takes no accounts, issues or pull requests;
> [this repository](https://gitbox.branderson.io/dejima/darner) is
> public to read there. Issues and pull requests are off here too. See
> [CONTRIBUTING.md](https://github.com/dejima-org/dejima/blob/main/CONTRIBUTING.md).

&nbsp;

<!-- darner's own logo goes here, from assets/, once it's drawn: see assets/README.md. -->
# darner

**Send a task from a chat room or a browser. darner runs it in a dejima sandbox and brings the work back.**

For each task, darner starts a coding agent in a `dejima --isolated` session, with the context, domains and dependencies the task asks for. While it runs, you answer its questions, approve the domains it asks for, and send it files, all from where you started it. When it's done, its summary and a zip of what it left in `~/shared` come back to you.

**→ [Try darner-web](https://darner-demo.dejima.page/)**, in a demo with no model or sandbox behind it: start a task, watch it run, and get its output, without anything you type leaving your browser.

[![Developed in dejima](assets/dejima-badge.svg)](https://github.com/dejima-org/dejima)

## How it fits together

```
frontend ──► darner-api ──ssh──► darner-launch ──► dejima --isolated, in tmux
```

- **Frontends** are where you work. Two ship with darner, as equals: the [Matrix bot](docs/matrix.md), which takes tasks from a room or a direct chat, and [darner-web](docs/web.md), in a browser. Run either or both. Each has its own token and sees only the sessions it started. For anywhere else, such as Slack, [docs/frontends.md](docs/frontends.md) is what a frontend has to do.
- **darner-api** holds tasks and their events, applies your policy, and collects output. Frontends talk only to it.
- **The dejima host** runs [dejima](https://github.com/dejima-org/dejima) and `darner-launch`. The API reaches it over ssh with a key that can run nothing but the launcher.

They can share a machine or not.

## What bounds it

darner isn't a sandbox; dejima is. darner's job is not to widen dejima's boundary, so the agent is presumed hostile, and so is everything it writes:

- **One command on the dejima host.** The API's key runs only the launcher, which takes a fixed grammar and re-checks everything it's sent.
- **Output is read, never trusted.** By file descriptor, regular files only, into a zip darner builds itself and the API checks again.
- **You approve what's granted.** A domain request is granted exactly as you were shown it, within the API's policy.
- **Agent text arrives inert.** HTML escaped, every link's destination shown, no one notified. A self-test checks a frontend.
- **Frontends decide who gets in.** The bot acts only for its allowlist; darner-web sits behind a proxy you configure to authenticate and authorize.
- **Nothing about a session outlives it.** Its prompt, requests and screens go when it ends, and its output once collected.

**→ [docs/sessions.md](docs/sessions.md)**: what a session can ask of you, and what happens to what it writes.

**→ [SECURITY.md](SECURITY.md)**: what darner holds that's worth taking, and how to report a vulnerability.

## What it doesn't do

- **Give you a terminal.** Watching a live session is dejima's remote access, which frontends link to.
- **Keep what you send.** A message is typed into the session, not stored, so no frontend shows it.
- **Run on any dejima.** Each release names the dejima release it needs.

## Setup

You need dejima working on a Linux host, with tmux, pipx and Python 3.11 there, and somewhere to run the API, with Python or Docker.

1. Install `darner-launch` on the dejima host.
2. Install the API, under systemd or Docker Compose.
3. Lock the API's ssh key to the launcher.
4. Add a frontend: the [Matrix bot](docs/matrix.md#setting-it-up), [darner-web](docs/web.md#setting-it-up), or both.

**→ [docs/setup.md](docs/setup.md)**: every step, and updating.

## Usage

A task is an instruction, plus keyword lines for what it needs. In Matrix, mention the bot with it:

```
@darner the import job started failing after the deploy, find out why
blurb: myapp
allow: pypi.org, files.pythonhosted.org
harness: cline
```

darner-web takes the same settings as a form, and shows what will launch before you start it.

**→ [docs/tasks.md](docs/tasks.md)**: every setting, config sets and blurbs, and the prompt.

## Docs

| Doc | What's in it |
|---|---|
| [setup.md](docs/setup.md) | Installing the launcher and the API, and updating |
| [matrix.md](docs/matrix.md) | The Matrix bot: setting it up, starting tasks, its commands |
| [web.md](docs/web.md) | darner-web: what reaching it means, its proxy, setting it up |
| [frontends.md](docs/frontends.md) | Writing a frontend for another platform: what it must do |
| [tasks.md](docs/tasks.md) | What a task can ask for, and the prompt the agent gets |
| [sessions.md](docs/sessions.md) | Help and domain requests, typing into a session, output, what's kept |
| [reference.md](docs/reference.md) | Launcher options, API config and endpoints |
| [development.md](docs/development.md) | Tests, releases, and checking a frontend |
| [release.md](docs/release.md) | Moving `development` to `main` |
| [CHANGELOG.md](CHANGELOG.md) | What each release changed, and the dejima release it needs |
| [SECURITY.md](SECURITY.md) | What darner claims and holds, and where to report a vulnerability |

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

The dejima wordmark and logo are not code and are not covered by that licence. They are trademarks; who holds them and what may be done with them is stated once for the organisation, at [dejima's ATTRIBUTION.md](https://github.com/dejima-org/dejima/blob/main/ATTRIBUTION.md).
