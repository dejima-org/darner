# meeseeks

Starts a dejima session for a task sent from chat. A task request carries the task, extra context, preconfigured context blurbs, network and dependency grants, and an optional harness and backend. Meeseeks starts `dejima --isolated` for it in a tmux window on a dejima host and hands the agent the composed prompt. When the agent signals it is done, meeseeks delivers what it left in `~/shared` back to the requester. Reaching the session itself is dejima's remote access.

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

`@meeseeks help` replies with this usage, including the available context blurbs.

The note is the task. The rest of the reply, minus the mention and the keyword lines, is extra context. Keyword lines take comma or space separated values and can repeat:

| keyword | meaning |
|---|---|
| `ctx:` | context blurbs to add, by name |
| `allow:` | domains the session may reach |
| `deps:` | dependencies to install, in `dejima-dep` syntax: `apt:`, `pip:`, `cargo:`, `npm:` or `tarball:` |
| `harness:` | harness for this task, overriding the default |
| `backend:` | backend for this task, overriding the harness's default |

The bot replies with the task id and its tmux target, or with why it was refused. It only acts when both the reply and the note are from users in its `allowlist`, since the note becomes the agent's instructions.

When the agent delivers output, the bot replies again with the agent's summary and the output as a zip. It also says when the session ends.

When the agent needs something from you, the bot posts its request. If the dejima host allows answers from chat, reply to that message and your reply is typed into the session as the agent's next message; no mention is needed. Otherwise the message says which session to attach to.

## Prompt

```
<note>

## Context: <blurb name>
<blurb>

## Context from requester
<reply text>

## Delivering your output
<put output in ~/shared, a summary in ~/shared/RESULT.md, then create ~/shared/.done>
<to ask for something, write ~/shared/HELP.md, create ~/shared/.help and end the turn>
```

The last section can be replaced with `output_instructions` in the API config.

## Output

The session is presumed hostile, and so is everything it writes to `~/shared`. Collecting output never follows a link, never opens anything but a regular file, and copies only file contents into a zip that meeseeks builds itself:

- **Walk:** by file descriptor, never by path. Each entry is opened with `O_PATH`, so no device driver is ever opened, and its type is checked on that handle. Only a regular file is reopened, through `/proc/self/fd`. Swapping in a symlink mid-walk therefore gains nothing.
- **Skipped:** symlinks, FIFOs, devices, and names outside `[A-Za-z0-9][A-Za-z0-9._ +-]*`. That covers dotfiles, `..`, backslashes, control and bidi characters, and invalid UTF-8. Also skipped: Windows reserved names, and names that differ only in case. `MANIFEST.txt` in the zip lists what was included and what was skipped, and why.
- **Limits:** bytes actually read (50 MiB by default), files included, entries visited, depth, path length and time taken. Going over one rejects the output and tells the requester why.
- **Signal:** a `.done` is consumed once. The launcher records which one it consumed, so a session that makes `.done` impossible to delete can't trigger repeat deliveries. A new `.done` signals new output.
- **Entries:** mode `0644`, a fixed timestamp, and no symlink or directory entries.
- **Re-check:** the API verifies the archive again and rebuilds it with its own writer before storing it, so what it serves is exactly what it checked.
- **Delivery:** the bot sends the summary as plain text with no mentions, so the agent can't hide a link behind other text or ping the room.

`HELP.md` is read the same way, as a regular file only, at most 16 KiB. Its text is sent as plain text, like the summary.

## Answering from chat

Off by default. Turn it on with `--answers` on the launcher, so the dejima host decides whether chat can type into its sessions. An answer is accepted only for an outstanding help request on a running session, and only once: concurrent answers race for it and only one wins. Answers go to the window by its tmux id, so renaming the window inside the session doesn't redirect them. If typing into the session fails, the chat sees only "couldn't type into the session", with no tmux detail.

Before pasting, the launcher strips control characters, ESC above all, so the text can't end the paste early and have the rest read as keystrokes. It then pastes the answer as `Reply from <user>:` followed by the text. Because of that prefix, the message never starts with `!` or `/`, which Claude Code would run as a shell or slash command. The paste is bracketed, so newlines don't submit it early, and Enter follows after `--paste-delay`.

The bot only treats a reply as an answer when the replied-to message is its own help request, so a forged tag on someone else's message is ignored.

The files are still agent-written: treat what is in the zip as untrusted. Neither the API's `results_dir` nor dejima's `isolated/` share directories are pruned.

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
| `--tasks-dir DIR` | `~/meeseeks/tasks` | per-task grant files, argv and exit status |
| `--share-root DIR` | `~/.local/share/dejima/shared/isolated` | where dejima puts isolated sessions' `~/shared`; output is only read from directly under it |
| `--max-output BYTES` | 50 MiB | largest output that will be collected |
| `--fetch-timeout SECONDS` | 60 | longest time collecting one output may take |
| `--answers` | off | allow answering help requests from chat |
| `--paste-delay SECONDS` | 0.5 | wait between pasting an answer and pressing Enter |
| `--help-interval SECONDS` | 300 | least time between a task's help requests, so a session can't flood the room |

The API sends one of these commands, which arrive as `SSH_ORIGINAL_COMMAND`: none (launch the job on stdin), `status`, `fetch <id>`, `help <id>` or `answer <id>`. Anything else is refused.

A detached launch has nobody to answer a prompt, so the host must not raise one:

- Acknowledge CRITICAL audit findings with `dejima-audit ack`.
- Pre-record decisions for any credential-shaped values dejima's staging scan flags.
- Run `loginctl enable-linger <user>` (or set `KillUserProcesses=no`) so the tmux server outlives the ssh connection.

Grants use `dejima --domains FILE` and `--dependencies FILE`, and output collection uses `--shared-path-file FILE`. A task without grants passes neither grant flag.

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
  "source": {"kind": "matrix", "ref": "$event-id", "channel": "!room-id"}
}
```

It returns `201 {"id", "target"}`. A repeat of the same `source` returns the existing task with `200`, unless its launch failed, in which case it is retried. Errors are `{"error": "..."}`.

Every `poll_interval` seconds the API asks the dejima host for the status of launched tasks, collects output that is ready, and records events. A client sees only the events of tasks it created:

| endpoint | |
|---|---|
| `GET /v1/events` | unacknowledged events, oldest first: `{seq, id, type, summary, size, answerable, target, source}`, `type` is `output`, `help` or `ended` |
| `GET /v1/tasks/<id>/outputs/<seq>.zip` | an `output` event's archive (`size` 0 means it was rejected, and `summary` says why) |
| `POST /v1/events/<seq>/ack` | mark an event delivered |
| `GET /v1/blurbs` | names of the available context blurbs |
| `POST /v1/tasks/<id>/answer` | `{text, requester}`: answer the task's outstanding help request; 409 with the reason if the host refuses |

`source` is echoed back as given, so a client knows where to reply without keeping state.

### Maubot plugin

Build with `mbc build` in `plugin/`, upload the `.mbp`, and create an instance. In the instance config, set `allowlist` to the Matrix IDs allowed to start tasks, `api_url`/`api_token` to the API, and `poll_interval` to how often it checks for output. Turn off autojoin for the bot's client, and enable its encryption support for encrypted rooms.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```
