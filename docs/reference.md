# Reference

[← darner README](../README.md)

## Launcher options

| option | default | meaning |
|---|---|---|
| `--dejima PATH` | `dejima` | the dejima executable; a full path, since a forced command's `PATH` is minimal |
| `--tmux PATH` | `tmux` | the tmux executable |
| `--identity NAME` | dejima's | `[identity.<name>]` every session acts as; a task cannot choose it |
| `--harness NAME` | dejima's | default harness |
| `--backend HARNESS=NAME` | dejima's | default backend for a harness; repeatable |
| `--model HARNESS=MODEL` | dejima's | default model for a harness; repeatable |
| `--effort HARNESS=LEVEL` | dejima's | default effort level for a harness that has one (claude-code, cline); repeatable |
| `--context-window HARNESS=TOKENS` | dejima's | default context window for a harness that has one (cline); repeatable |
| `--session NAME` | `darner` | tmux session the windows go in |
| `--tasks-dir DIR` | `~/darner/tasks` | per-task grant files, argv and exit status |
| `--share-root DIR` | `~/.local/share/dejima/shared/isolated` | where dejima puts isolated sessions' `~/shared`; output is only read from directly under it |
| `--max-output BYTES` | 50 MiB | largest output that will be collected |
| `--fetch-timeout SECONDS` | 60 | longest time collecting one output may take |
| `--answers` | off | allow answering help requests from chat |
| `--paste-delay SECONDS` | 0.5 | wait between pasting an answer and pressing Enter |
| `--quiet-period SECONDS` | 600 | after this long without changes in `~/shared`, collect a `RESULT.md` the agent never signalled with `.done`; 0 waits for the session to end |
| `--keep-records` | off | pass `--keep-records` to every dejima session |
| `--max-upload BYTES` | 50 MiB | largest file a requester can upload into a session |
| `--docker PATH` | `docker` | the docker CLI, used to find and stop a session's container |
| `--dejima-allow PATH` | `dejima-allow` | dejima's `dejima-allow`, used to grant a session the domains it requested |
| `--dejima-share PATH` | `dejima-share` | dejima's `dejima-share`, used to remove an ended session's `~/shared` once its output has been delivered whole; it needs a dejima with `prune --isolated [share]` and `--yes`, and without one, shares are kept |
| `--keep-shared` | off | keep every ended session's `~/shared` on the dejima host, even once its output has been delivered; separate from `--keep-records`, which keeps dejima's records |
| `--stop-timeout SECONDS` | 60 | how long a stopped session may take to exit before dejima itself is ended |
| `--help-interval SECONDS` | 300 | least time between a task's help requests, and between domain requests while one awaits a decision, so a session can't flood the room |
| `--done-interval SECONDS` | 60 | least time between a running task's `.done` deliveries, so a session can't fill the API's disk; an exited session's is collected at once |

The API sends one of these commands, which arrive as `SSH_ORIGINAL_COMMAND`: none (launch the job on stdin), `status`, `fetch <id>`, `catalog`, `help <id>`, `answer <id>`, `message <id>`, `upload <id>`, `stop <id>`, `domains <id>`, `allow <id> <n>`, `deny <id> <n>`, `forget <id>` or `forget-delivered <id> <sha256>`. Anything else is refused.

Grants use `dejima --domains FILE` and `--dependencies FILE`, and output collection uses `--shared-path-file FILE`. A task without grants passes neither grant flag.

## API config

[`examples/api.toml`](../examples/api.toml), with:

| key | meaning |
|---|---|
| `listen` | `host:port` to listen on |
| `db_path` | SQLite database of open tasks and their events; see [What's kept](sessions.md#whats-kept) |
| `blurbs_dir` | context blurbs, as `<name>.md`; read per request, so an edit needs no restart |
| `max_upload` | largest upload in bytes, default 50 MiB; keep it at or under the launcher's `--max-upload` |
| `waiting_after` | seconds a running session's screen can stay unchanged before frontends say it seems to be waiting (default 300) |
| `output_retention` | seconds an ended session's output is kept for a frontend that hasn't collected it (default 604800, 7 days; 0 deletes it when the session ends) |
| `max_session_age` | seconds a session may run before it's stopped, so none is left running forgotten (default 86400, a day; 0 never stops one). A task the launcher no longer knows about is marked ended at the same age. A session stuck starting counts too, and one whose output can never be fetched is ended at that age, with the reason posted |
| `waiting_screen` | whether that notice includes the end of the session's screen (default true) |
| `configs_dir` | config sets, by default `configs` beside `blurbs_dir`; one file per set named as it's used, holding keyword lines (see [`examples/configs`](../examples/configs)); read per request. Comment lines at a set's top are its description, shown wherever the set is described. A set's `file: <name>` lines give each of its sessions `configs_dir/files/<name>` (up to 1 MiB) in `~/shared/uploads` |
| `default_blurbs` | blurb names added to every task's prompt, before any it names with `blurb:`; each must exist |
| `results_dir` | collected output archives, until their frontend collects them |
| `poll_interval` | seconds between checks on the dejima host |
| `launch_command` | how to reach the launcher; the command is appended, and over ssh arrives as `SSH_ORIGINAL_COMMAND` |
| `launch_timeout` | seconds a launcher call may take |
| `output_instructions` | replaces the prompt's last section |
| `remote_access_url` | dejima's web remote access `base_url`, to link each session from frontends; unset means no links |
| `session_wait` | with `remote_access_url`, seconds a launch waits for dejima to register the session (default 20) |
| `[tokens]` | one bearer token per frontend, by name. Each must be at least 32 characters, differ from the others and from `examples/api.toml`'s placeholder, or the API won't start; `openssl rand -hex 32` makes one. A token can do everything its frontend can, so see [SECURITY.md](../SECURITY.md) before adding one |
| `[policy]` | applied at launch and to every domain request; a misspelled key stops the API. `deny_domains`: refused, with their subdomains and parents. `allow_domains`: if set, only these and their subdomains. `max_domains`: also caps what a session gains through requests. `allow_internal` (default true): false refuses internal grants. `max_internal_domains`. `allow_dependency_kinds` (default `apt`, `cargo`, `npm`, `pip`): `tarball` is left out unless listed, since it has the dejima host fetch any URL on its own network |
| `[limits]` | `max_bytes`, `max_files`: `max_bytes` must be at least the launcher's `--max-output`, or larger output is never collected |
| `[catalog.HARNESS.BACKEND]` | `models`: the models to offer for that harness and backend, each an id or `{ id = "...", context_window = N }`, which fills in the context window when it's picked. Frontends offer them, and still take a model that isn't listed. A backend the harness can't use isn't offered, nor a context window if it takes none |

## API endpoints

`POST /v1/tasks` takes a bearer token from `[tokens]`:

```json
{
  "task": "...",
  "context": "...",
  "blurbs": ["myapp"],
  "grants": {"domains": ["pypi.org"], "dependencies": ["pip:pytest"]},
  "harness": "cline",
  "backend": "openrouter",
  "requester": "@user:example.org",
  "source": {"kind": "matrix", "ref": "$event-id", "channel": "!room-id"}
}
```

It returns `201 {"id", "target"}`. An optional `name` names the session: up to 32 lowercase letters, digits and hyphens, not shaped like a session id, and not held by another of the client's sessions (409). An ended session holds its name until it's cleared or purged, so a command repeated later gets "has ended" rather than reaching a new session that took the name. Any `<id>` below can be `dn-<name>` instead. A repeat of the same `source` returns the existing task with `200` while it's kept. A failed launch is deleted, so repeating it retries. Errors are `{"error": "..."}`.

Every `poll_interval` seconds the API asks the dejima host for the status of launched tasks, collects output that is ready, and records events. A client sees only the events of tasks it created. Every `requester` is a label for who acted, up to 255 characters with no control or invisible characters; the audit log records it with the frontend's token name. `GET /v1/tasks` reuses a launcher status up to 5 seconds old, so open frontends don't each reach the dejima host:

| endpoint | |
|---|---|
| `GET /v1/events` | unacknowledged events, oldest first: `{seq, id, name, type, summary, size, answerable, target, source, created}`, `type` is `output`, `help`, `domains`, `waiting`, `expired` or `ended`; `data` holds a domain request, a waiting session's `quiet` seconds, or a stopped session's `age` |
| `GET /v1/tasks/<id>/outputs/<seq>.zip` | an `output` event's archive (`size` 0 means it was rejected, and `summary` says why) |
| `GET /v1/tasks/<id>/events` | a task's events so far, collected or not, in the same form, without acknowledging any; gone once the task is purged |
| `POST /v1/events/<seq>/ack` | mark an event delivered |
| `POST /v1/tasks/preview` | a `POST /v1/tasks` body, whose `task` may be empty: what it would launch, without launching it. `settings` are the agent settings it asks for, `from_configs` those its config sets gave, then `domains`, `internal_domains`, `dependencies`, `files` (names only) and `blurbs` (`{name, text}`). Refused as the task would be. Defaults are the catalog's |
| `GET /v1/blurbs` | names of the available context blurbs |
| `GET /v1/configs` | names of the available config sets |
| `POST /v1/selftest` | `{requester, source}`: a finished task whose output, help request and waiting screen are hostile text, delivered through `/v1/events` like any task's; see [Checking a frontend](development.md#checking-a-frontend) |
| `GET /v1/blurbs/{name}` | a context blurb's text |
| `GET /v1/configs/{name}` | what a config set applies, by keyword; `file` lists names only, never contents |
| `GET /v1/catalog` | the dejima host's harnesses and backends, the default harness and each harness's defaults (dejima's own, from `dejima --print-config`, with the launcher's options in their place), and under `options`, what each harness can be set to: its backends, effort levels, whether it takes a context window, and `[catalog]`'s models for each backend |
| `POST /v1/tasks/<id>/domains/<n>/allow` | `{requester}`: grant domain request `n` as it was shown, minus what policy refuses |
| `POST /v1/tasks/<id>/domains/<n>/deny` | `{requester}`: refuse domain request `n` |
| `GET /v1/tasks` | the client's open sessions, with what each is waiting on: `{id, name, target, link, requester, started, state, answerable, waiting: {answer, domains}, started_with}`. `started_with` is what the request asked for, without the prompt: its `configs`, `blurbs`, agent settings, grants and file names |
| `GET /v1/tasks/<id>` | one open session, in the same form |
| `POST /v1/tasks/<id>/message` | `{text, requester}`: type a message into the session; 409 with the reason if the host refuses |
| `POST /v1/tasks/<id>/files?name=&requester=` | the request body is a file for the session's `~/shared/uploads`; 413 over `max_upload` |
| `POST /v1/tasks/<id>/check` | poll now rather than at the next `poll_interval`, and answer the session as it then is, or `{id, ended: true}`. A poll covers every session, so each client may check once every 15 seconds (429, with `Retry-After`), and a poll already running is the check |
| `POST /v1/tasks/<id>/stop` | `{requester}`: end the task's session and close its tmux window, then poll, so a session that has exited is normally shown ended by the time it answers. A failed poll is logged and doesn't fail the stop; 409 if it isn't running |
| `POST /v1/tasks/<id>/answer` | `{text, requester}`: answer the task's outstanding help request; 409 with the reason if the host refuses |

`source` is echoed back as given, so a client knows where to reply without keeping state.
