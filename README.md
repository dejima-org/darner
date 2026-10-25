# meeseeks

Starts a dejima session for a task sent from chat. A task request carries the task, extra context, preconfigured context blurbs, network and dependency grants, and an optional harness and backend. Meeseeks starts `dejima --isolated` for it in a tmux window on a dejima host and hands the agent the composed prompt. Reaching the session afterwards is dejima's remote access.

```
maubot plugin ──► meeseeks-api ──ssh──► meeseeks-launch ──► tmux new-window ── dejima --isolated
```

## Using it from Element

Reply to a note with a message that mentions the bot:

```
@meeseeks the import job started failing after the deploy, find out why
ctx: myapp
allow: pypi.org, files.pythonhosted.org
deps: apt:jq pip:pytest-xdist
harness: claude-code
backend: openrouter
```

The note is the task. The rest of the reply, minus the mention and the keyword lines, is extra context. Keyword lines take comma or space separated values and can repeat:

| keyword | meaning |
|---|---|
| `ctx:` | context blurbs to add, by name |
| `allow:` | domains the session may reach |
| `deps:` | dependencies to install, in `dejima-dep` syntax: `apt:`, `pip:`, `cargo:`, `npm:` or `tarball:` |
| `harness:` | harness for this task, overriding the default |
| `backend:` | backend for this task, overriding the harness's default |

The bot replies with the task id and its tmux target, or with why it was refused. It only acts when both the reply and the note are from users in its `whitelist`, since the note becomes the agent's instructions.

## Prompt

```
<note>

## Context: <blurb name>
<blurb>

## Context from requester
<reply text>
```

## Deployment

### Dejima host

Install the package for the account that runs dejima (`pipx install .`), then lock the API's key to the launcher in that account's `~/.ssh/authorized_keys`:

```
command="meeseeks-launch --identity meeseeks --harness claude-code --backend claude-code=anthropic",restrict ssh-ed25519 AAAA... meeseeks-api
```

| option | default | meaning |
|---|---|---|
| `--identity NAME` | dejima's | `[identity.<name>]` every session acts as; a task cannot choose it |
| `--harness NAME` | dejima's | default harness |
| `--backend HARNESS=NAME` | dejima's | default backend for a harness; repeatable |
| `--session NAME` | `meeseeks` | tmux session the windows go in |
| `--tasks-dir DIR` | `~/meeseeks/tasks` | per-task grant files and argv |

A detached launch has nobody to answer a prompt, so the host must not raise one:

- Acknowledge CRITICAL audit findings with `dejima-audit ack`.
- Pre-record decisions for any credential-shaped values dejima's staging scan flags.
- Run `loginctl enable-linger <user>` (or set `KillUserProcesses=no`) so the tmux server outlives the ssh connection.

Grants use `dejima --domains FILE` and `--dependencies FILE`. A task without grants passes neither flag.

### API

Install with `pipx install '.[api]'`, write a config based on [`examples/api.toml`](examples/api.toml), and run `meeseeks-api --config /etc/meeseeks/api.toml`. Put context blurbs in `blurbs_dir` as `<name>.md`. They are read per request, so an edit needs no restart.

`POST /v1/tasks` takes a bearer token from `[tokens]`:

```json
{
  "task": "...",
  "context": "...",
  "blurbs": ["myapp"],
  "grants": {"domains": ["pypi.org"], "dependencies": ["pip:pytest"]},
  "harness": "claude-code",
  "backend": "openrouter",
  "requester": "@user:example.org",
  "source": {"kind": "matrix", "ref": "$event-id"}
}
```

It returns `201 {"id", "target"}`. A repeat of the same `source` returns the existing task with `200`, unless its launch failed, in which case it is retried. Errors are `{"error": "..."}`.

### Maubot plugin

Build with `mbc build` in `plugin/`, upload the `.mbp`, and create an instance. In the instance config, set `whitelist` to the Matrix IDs allowed to start tasks, and `api_url`/`api_token` to the API. Turn off autojoin for the bot's client, and enable its encryption support for encrypted rooms.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```
