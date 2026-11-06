# Writing a frontend

[← darner README](../README.md)

A frontend is darner's last mile: it takes tasks from people where they already are, and brings back what their sessions send. darner ships two, the [Matrix bot](matrix.md) and [darner-web](web.md). For anywhere else, such as Slack or Teams, a frontend is a small program against darner-api's HTTP endpoints, listed under [API endpoints](reference.md#api-endpoints).

It can be thin. The API checks every task against its policy, launches it, polls it, collects its output and keeps its events. A frontend does what only it can:

- decide who may act
- show what the agent wrote without letting it do anything
- know which session a message is about
- deliver output

This page is what has to be true of it. Give it a name of its own, as darner-web has, and build it in its own repository: darner doesn't need to know it exists, only its token.

## Security comes first, because every frontend is a way in

**A frontend is a key to your dejima host.** The API acts on whatever a frontend tells it, so a flaw in any frontend is a flaw in the whole deployment, however carefully the rest of it is set up. Whoever gets past a frontend can:

- start sessions on your accounts, with any domains, dependencies and config sets the API's policy allows
- read the files config sets give sessions, such as tokens
- answer for you, approve what agents ask for, and read what they send back

**If you're writing one,** security is its first requirement, ahead of every feature. Every rule below is a boundary, not a nicety. Meet all of them, test each one, and treat what the agent writes, and every request your frontend receives, as hostile until you've checked it.

**If you're running one that someone else wrote,** read its code thoroughly before giving it a token, as you should anything you let start sessions on your dejima host, and again before each update. Check it against this page. darner can't check a frontend for you. Neither the Matrix bot's allowlist nor darner-web's proxy protects you from a different frontend's mistakes.

## The contract

### 1. It holds its own token, and guards it

Add a line for it under `[tokens]` in `api.toml`, named for the frontend: the audit log records that name beside every action. The API trusts a token completely, so anyone who can read it, or administer the frontend, can do everything the API does within its policy for that token's sessions. Keep it where only the frontend can read it, out of logs and out of the repository. Reach the API over TLS, or on loopback or a private network you control: the token travels in every request.

Each token sees only the sessions it started, so a frontend never sees another frontend's tasks, and revoking it is removing its line.

### 2. It decides who may act, on every action

The API acts for whatever `requester` a frontend names, and can't check it. Deciding who may use darner is the frontend's job, and its access control is the only access control there is.

- **Authenticate** each person through the platform, and **authorize** against an allowlist of stable user ids, never display names, which anyone can change.
- **Verify every inbound request** came from the platform, or from your own origin, before trusting who it says sent it: a platform's request signature and timestamp, or for a web frontend, darner-web's host, origin and custom-header checks.
- **Check it on every action**, not only starting a task. Answering a help request, allowing a domain, typing into a session, uploading a file, stopping and clearing all act on a session.
- **Check it for reading too.** A session's output, zips, help requests and screens are for the people allowed to see them. Delivering to a channel shows them to everyone in it, so a channel's members are its readers.
- **The words must be theirs.** A task, its context and an answer are the agent's instructions, run with the requester's grants and any config set's files. Take them only from what the allowed person wrote: a quoted or threaded message by someone else, or an edit by anyone but its author, is an outsider writing the agent's instructions. The bot refuses a quoted note whose author isn't allowlisted, and reads only that author's own latest edit.
- **Name the person as `requester`**: their stable id, up to 255 characters, with no control or invisible characters. It's a label for the audit log, not a credential.

### 3. A message is about a session only by the frontend's own record

When someone replies to say "allow" or "stop", the frontend must know which session that is, and the answer must come from something only it could have written.

- **Tie replies to the frontend's own messages.** The Matrix bot treats a reply as about a session only when the replied-to message was sent by the bot itself and carries its tag (`page.dejima.darner.task`). A platform that can't attach metadata to a message needs the frontend to keep its own map from the messages it sent to session ids.
- **Record the session id, never its `dn-<name>`**: a name is freed when its session is cleared, and another session can take it. A domain request's message records its request number too, so "allow" answers the request that was shown.
- **Never take your own posts as commands.** They carry agent text, so reading them as commands would act on what the agent wrote. The bot ignores its own messages.
- **An id typed by an allowed person is fine** (`stop dn-k3x7q`), since they're allowed to act on that session anyway.
- **Never act on what the agent wrote.** Agent text can contain anything, ids and commands included. It's shown, never interpreted.

### 4. What the agent wrote arrives inert

The API removes control and invisible characters from agent text and nothing more. Making it safe to show is the frontend's job.

The agent writes the `summary` of every `output`, `help` and `waiting` event, the domains in a `domains` event's `data`, a session's `waiting.answer` and `waiting.domains` in `GET /v1/tasks`, and everything inside an output zip. Each is presumed hostile:

- **Escape the platform's markup**, all of it: HTML, markdown, and the platform's own, such as Slack's `<url|text>` links and `<!here>`.
- **Show where every link goes.** A link's text can claim anything, so its destination is shown beside it. Only `http`, `https` and `mailto` destinations are ever clickable.
- **Load nothing the agent names.** No images, no link previews or unfurls: a request to an agent-chosen address tells it who's reading, and when.
- **Notify no one.** No `@room`, `@here` or `@channel`, no mention of a user, whatever the text says. Turn mentions off at the platform's level where it has one, as the bot does with an empty `m.mentions`.
- **Label it** as written by the agent, every time.
- **Keep it whole.** A summary can be up to 16 KiB. If the platform's messages are shorter, split it, or say plainly that it was cut and that the zip holds the rest. Never cut it silently. The API has already cut `RESULT.md` to 16 KiB, and the zip holds all of it; a waiting screen is cut to 4000 bytes.
- **Show a waiting screen as it is**, in a monospaced block, still escaped.

### 5. It passes the self-test

`POST /v1/selftest` with `{requester, source}` creates a finished session whose output, help request and waiting screen are text written like a hostile agent's. It arrives through `/v1/events` like any session's, so it travels the same paths agent text does. Each check in it says what a safe frontend shows and what an unsafe one shows.

Every check must pass. Then turn it into a test that fails on unescaped markup, a hidden link destination or a mention, as `assert_shown_safely` in `tests/test_bot.py` does for the bot. [Checking a frontend](development.md#checking-a-frontend) has more. Give people a way to run the self-test too, so an operator can check their own deployment, with a fresh `ref` each time: a repeated `source` returns the earlier self-test and delivers nothing new. It has no domain request, its help request isn't answerable, and its output has a zip, so check those paths yourself.

### 6. Each request starts one task

Every `POST /v1/tasks` carries a `source`:

- **`kind`** is the frontend's name, lowercase.
- **`ref`** identifies the person's request, up to 255 characters, and must be unique across everything the frontend serves. A message id unique only within its channel, such as Slack's `ts`, needs the channel in it, or a request in one channel gets another channel's task back.
- **`channel`** is optional: where to answer, such as a room or channel id.

A repeat of the same `kind` and `ref` returns the task it already started, with 200 rather than 201, while that task is kept, or 409 while it's still launching. Retrying after a timeout or a crash therefore can't start a second session. Every event carries its task's `source` back, which is how the frontend knows where to deliver it.

### 7. It delivers output, then acknowledges it

There are two ways to deliver, and a frontend picks one:

- **Push**, as the bot does. Read `GET /v1/events` every so often (the bot uses 30 seconds), deliver each event to its `source`, then `POST /v1/events/<seq>/ack`.
  - **Download first.** Acknowledging an `output` event deletes its zip, so fetch `GET /v1/tasks/<id>/outputs/<seq>.zip` before acknowledging.
  - **Retry a failed delivery**, and give up eventually: the bot tries 5 times, then acknowledges, so one bad event doesn't block the rest.
  - **Tolerate a repeat.** Delivery is at least once: a crash between delivering and acknowledging delivers the event again.
- **Pull**, as darner-web does. Read `GET /v1/tasks` and `GET /v1/tasks/<id>/events` when someone looks, leave events unacknowledged, and let people clear an ended session with `POST /v1/tasks/<id>/clear`. `GET /v1/tasks` lists only running sessions, so find ended ones by their `ended` events in `GET /v1/events`. When a session ends, the API keeps only its unacknowledged output and its `ended` event, so its help, domain and waiting history goes. Uncleared output is kept for `output_retention`, 7 days by default, from when the session ended.

An output with `size` 0 has no zip: it was refused, with its `summary` saying why, or it's an older output whose zip was dropped, since the API keeps a session's latest 5. Either way, show its summary as the agent's: a refusal can quote file names the agent chose. Name a zip yourself, as `<id>-<seq>.zip`. Never unpack or preview it: what's inside, file names included, is the agent's. If the platform refuses a file that large, say so rather than dropping it.

### 8. It shows errors as plain text

Every refusal is `{"error": "..."}`, written for the person who asked, and the API keeps host paths out of it. Show it, escaped like anything else: it can quote what someone typed.

### 9. It shares what it uses

A frontend doesn't hold the dejima host to itself.

- `POST /v1/tasks/<id>/check` polls every session at once, so each token may use it once every 15 seconds (429, with `Retry-After`).
- `GET /v1/tasks` asks the dejima host for live status, reused for up to 5 seconds.
- Poll events no faster than people need them.

## What each event means

| `type` | what to show |
|---|---|
| `output` | the agent's summary, labelled, and the zip if `size` isn't 0. `signalled: false` means it was collected without the agent's done signal |
| `help` | the agent's question, labelled. If `answerable`, let an allowed person answer it, with `POST /v1/tasks/<id>/answer` |
| `domains` | each requested domain in `data.domains`, with its port, and whether it's internal or refused by policy; `data.rejected` weren't valid domains. Allow or deny request `data.request`, as it was shown |
| `waiting` | the session's screen hasn't changed for `data.quiet` seconds; `summary` is the end of its screen, the agent's |
| `expired` | it was stopped for running longer than sessions may, after `data.age` seconds |
| `ended` | the session has ended. `data.share_kept` means its `~/shared` is kept on the dejima host until it's cleared, so offer that |

## What each action calls

| someone wants to | endpoint |
|---|---|
| start a task | `POST /v1/tasks`; `POST /v1/tasks/preview` shows what it would launch first |
| see what can be chosen | `GET /v1/configs`, `GET /v1/blurbs`, `GET /v1/catalog`, and each config set or blurb by name |
| see their sessions | `GET /v1/tasks`, `GET /v1/tasks/<id>` |
| tell a session something | `POST /v1/tasks/<id>/message`, typed into it as written |
| answer a help request | `POST /v1/tasks/<id>/answer` |
| allow or deny domains | `POST /v1/tasks/<id>/domains/<n>/allow` or `/deny` |
| give it a file | `POST /v1/tasks/<id>/files?name=&requester=` |
| stop it, or clear it once ended | `POST /v1/tasks/<id>/stop`, `POST /v1/tasks/<id>/clear` |

The API checks every task and grant against its policy and refuses what it doesn't allow, so a frontend doesn't need to validate them first. Pass on what people asked for, and show the refusal.

## Before you ship

- The self-test passes, and a test fails if it wouldn't.
- Someone outside the allowlist can't start, answer, allow, message, upload, stop or clear, can't read a session's output, and can't write a task by being quoted or by editing a message.
- A request that didn't come from the platform, or from your origin, is refused.
- A reply to a message the frontend didn't send names no session.
- The token appears in no log, error or repository.

[SECURITY.md](../SECURITY.md) has what a frontend holds, and why adding one adds a way in. darner's [changelog](../CHANGELOG.md) names any change to these endpoints, so check it at each release.
