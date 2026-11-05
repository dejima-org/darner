# The Matrix bot

[← darner README](../README.md)

A maubot plugin that starts tasks from a Matrix room, such as one you keep notes in from Element. Reply to a note, mentioning the bot, and the note becomes the task. The bot posts what the session asks for and what it delivers, and takes your replies as answers, messages and commands. It's one frontend among others, with its own API token, and sees only the sessions it started.

## Setting it up

You need a maubot server, a Matrix account for the bot, and darner's API and dejima host [set up](setup.md) with a token for the bot: `FRONTEND=maubot` there, or a token of its own under `api.toml`'s `[tokens]`.

| variable | what it is | where the value comes from |
|---|---|---|
| `MAUBOT_URL` | your maubot server's URL | where you open maubot's web UI |
| `MAUBOT_ADMIN` | a maubot admin user | `admins:` in maubot's config |
| `MAUBOT_HOMESERVER` | the bot's homeserver, by its name in maubot's config | the keys under `homeservers:` in maubot's config; `mbc auth --list` shows them |
| `BOT_USER` | the bot's Matrix username, without `@` or `:server` | you choose it when creating the account |

Create the bot's Matrix account, named `BOT_USER`. Use your homeserver's registration, or `register_new_matrix_user` on Synapse.

On any machine, install maubot's CLI, log in to your maubot server, and log the bot in through it. The bot's homeserver has to be listed under `homeservers:` in maubot's config. `--update-client` creates the bot's client in maubot with a device of its own, which encrypted rooms need.

```sh
MAUBOT_URL=https://maubot.example.org
MAUBOT_ADMIN=admin
MAUBOT_HOMESERVER=example.org
BOT_USER=dragonlet

pipx install maubot
mbc login -s "$MAUBOT_URL" -u "$MAUBOT_ADMIN"
mbc auth -h "$MAUBOT_HOMESERVER" -u "$BOT_USER" --update-client
```

Download the plugin, `io.branderson.dejima.darner-v<version>.mbp`, from the repository's latest release, then upload it in maubot's web UI under **Plugins**. `mbc upload <file>` does the same from the command line.

In maubot's web UI:

1. Under **Instances**, create one with the bot's client as the primary user and `io.branderson.dejima.darner` as the type.
2. Set the instance config:
   - `allowlist`: your Matrix ID, and anyone else's who may start tasks, e.g. `["@you:example.org"]`
   - `api_url`: where the API is reachable from maubot. That's `http://127.0.0.1:8080` with the defaults on the same machine, or `http://api:8080` from a container on the same Docker network
   - `api_token`: the bot's token
3. Invite the bot to the room you keep notes in, and accept the invite. The simplest way is to turn on **Autojoin** on the bot's client, invite it, then turn autojoin off again, so only rooms you choose matter.

Encrypted rooms need maubot's encryption support installed on the maubot server; see maubot's documentation.

To check it, send a message in the room that mentions the bot and says `help`. The reply lists your context blurbs, which also shows that the bot reaches the API. Then reply to a note, mentioning the bot, with a line of context. The bot answers with the task id, and when the agent finishes, it replies again with its summary and a zip.

### Instance config

| key | meaning |
|---|---|
| `allowlist` | Matrix IDs that may start tasks and answer help requests |
| `api_url`, `api_token` | where the API is and the token to use |
| `poll_interval` | seconds between checks for task updates |

## Starting a task

Reply to a note with a message that mentions the bot:

```
@dragonlet the import job started failing after the deploy, find out why
blurb: myapp
allow: pypi.org, files.pythonhosted.org
dependency: apt:jq pip:pytest-xdist
harness: cline
backend: openrouter
```

The note is the task. The rest of the reply, minus the mention and the keyword lines, is extra context. Or send the task directly, in a message that mentions the bot and isn't a reply. That message, minus the mention and the keyword lines, is the task, with no extra context. Every keyword is in [tasks.md](tasks.md).

A mention is a pill for the bot, whatever its text, or the bot's full user ID typed out, such as `@dragonlet:example.org`. The bot's name on its own isn't one, so a room can talk about the bot without starting tasks, and neither is replying to one of its messages without a mention.

The bot replies with the task id and its tmux target, plus a link to the session if the API has `remote_access_url` set, or with why it was refused. It only acts when both the reply and the note are from users in its `allowlist`, since the note becomes the agent's instructions.

`@dragonlet help` replies with this usage, including the available context blurbs and each harness's effort levels.

## While it runs

When the agent delivers output, the bot replies with the agent's summary and the output as a zip. It also says when the session ends.

When the agent needs something from you, the bot posts its request. If the dejima host allows answers, reply to that message and your reply is typed into the session as the agent's next message; no mention is needed. Otherwise the message says which session to attach to. The bot only treats a reply as an answer when the replied-to message is its own help request, so a forged tag on someone else's message is ignored.

When the agent asks for domains, the bot posts every one. Reply `@dragonlet allow` to that message to let the running session reach them, or `@dragonlet deny`.

When a session seems to be waiting, the bot posts the end of its screen as a code block. Reply to the notice with a mention to tell the session something.

To manage open sessions, mention the bot with one of these. Replying to any of the bot's messages about a session works without the id.

| command | |
|---|---|
| `sessions` (or `list`, `list sessions`) | lists the open sessions: what each is waiting on, its age, who started it, and where to reach it |
| `status dn-xxxxx` | what a session is waiting on, quoting its help request or listing its requested domains |
| `tell dn-xxxxx <message>` | types a message into the session, which also answers an outstanding help request; needs `--answers` on the launcher |
| `upload dn-xxxxx` | in reply to a file: puts it in the session's `~/shared/uploads`; a file sent as a reply to one of the bot's messages about a session goes there too |
| `stop dn-xxxxx` | ends the session, including one stuck starting |

Replying to one of the bot's messages about a session with anything else, with a mention, sends it to the session like `tell`.

To see what config sets and context blurbs there are, mention the bot with `list configs` or `list blurbs`. `describe <names>` shows each named config set's description and keyword lines, with the names of any files it gives a session but not their contents, then each named blurb's text. A name that's both a config set and a blurb shows both, config set first. Up to 10 names at once. A blurb is cut at 20 lines or 1000 characters, so a reply doesn't fill the room; the whole text is in the blurbs directory.

`@dragonlet selftest` sends a [frontend check](development.md#checking-a-frontend) to the room.

What all of this does on the dejima host is in [sessions.md](sessions.md).

## Updating

Update the dejima host and the API first, as in [setup.md](setup.md#updating). Then download the `.mbp` from the new release and upload it in maubot's web UI under **Plugins**. Maubot replaces the plugin and restarts its instances. Instance config is kept, and new keys get their defaults.

Each push to `development` publishes a pre-release, `<version>.dev<run>`, with the plugin attached. To try one, upload its `.mbp` the same way. maubot orders it before `<version>`, so the stable release supersedes it.
