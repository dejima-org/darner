# What a task can ask for

[← darner README](../README.md)

A task is what the agent is told to do, plus anything it needs to do it. Every frontend takes the same settings: the [Matrix bot](matrix.md) as keyword lines in a message, [darner-web](web.md) as fields of its form, which shows before Start what the task will launch.

## Settings

Keyword lines take comma or space separated values and can repeat. A domain can carry a `:port`, which is ignored, since a launch grant covers every port.

| keyword | meaning |
|---|---|
| `config:` | config sets to use, by name: saved keyword lines for a recurring task. Their lists combine. Two that give a single value differently, such as `harness:`, are refused, whatever their order, unless the message gives that value itself. Anything else in the message overrides them: a single value replaces theirs, a list adds to theirs |
| `blurb:` | context blurbs to add, by name. `ctx:` is an older spelling, still read |
| `allow:` | domains the session may reach |
| `internal:` | domains inside your network the session may reach, passed to dejima as `--internal-domain` as well as allowed |
| `dependency:` | dependencies to install, in `dejima-dep` syntax: `apt:`, `pip:`, `cargo:`, `npm:` or `tarball:`. `deps:` is an older spelling, still read |
| `harness:` | harness for this task, overriding the default |
| `backend:` | backend for this task, overriding the harness's default |
| `model:` | model for this task, overriding the harness's default |
| `effort:` | effort level for this task, one its harness's dejima manifest lists, which frontends offer |
| `context_window:` | context window for this task, in tokens, for a harness that has the setting (cline). Set it a little under what the model's server allows, so cline compacts before the server refuses |
| `network:` | `named`: the session reaches only what this task names and its model, not dejima's own allowlist (dejima's `--drop-domains`). `sealed`: that, and the session doesn't start if it could look up any name, so only addresses are reachable (`--require-sealed`). A sealed session needs its model reached by address, such as a self-hosted one, and a domain request can't grant it a name later |
| `name:` | a name for the session, so commands can call it `dn-<name>` in place of its id. Lowercase letters, digits and hyphens, up to 32. A session holds its name until it's cleared, even once it has ended |

darner refuses a task asking for a backend, effort level or context window its harness can't take, as dejima's own manifests declare them: the backends speaking one of the harness's `api_styles`, the levels of its `effort` key, and whether it has a `context_window` key. A task naming no harness is held to the one dejima will pick, with its defaults. The API checks against the launcher's catalog, fetched at most every five minutes, and the launcher checks again, with its and dejima's defaults filled in.

The API's `[policy]` applies to every task's grants: denied domains, an allowlist, limits, and which dependency kinds are allowed. See [API config](reference.md#api-config).

## Config sets and blurbs

Both live on the API host and are read per request, so an edit needs no restart.

- **A context blurb** is a markdown file in `blurbs_dir`, added to the prompt by name. `default_blurbs` adds some to every task.
- **A config set** is a file of keyword lines in `configs_dir`, for a recurring task. Comment lines at its top are its description, shown wherever it's described. See [`examples/configs`](../examples/configs).

A config set's `file:` lines give every session started with it those files, such as an API token the task needs. The API sends them with the job and keeps only their names in its database. The launcher holds them owner-only in the task directory until the session's `~/shared` exists, writes them into `~/shared/uploads` as `0600` before the agent starts, deletes its copies, and removes them from `~/shared` when the session ends. The prompt tells the agent where they are. A file given to a session is readable by the agent and by anything it can reach, and anyone who may start tasks and names the config set gives it to their session. Give it only what the task needs, scoped as narrowly as the service allows.

## The prompt

```
<task>

## Context: <blurb name>
<blurb>

## Context from requester
<extra context>

## Delivering your output
<~/shared is in the home directory, so give file tools its full path>
<put output in ~/shared, a summary in ~/shared/RESULT.md, then create ~/shared/.done>
<to ask for something, write ~/shared/HELP.md, create ~/shared/.help and end the turn>
<to ask for domains, list them in ~/shared/DOMAINS, create ~/shared/.domains and end the turn>
```

The last section can be replaced with `output_instructions` in the API config. What happens to what the agent writes is in [sessions.md](sessions.md).
