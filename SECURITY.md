# Security policy

## What darner claims

It starts dejima sessions for tasks sent from a frontend, the Matrix bot or darner-web, and brings their output back. It doesn't make an agent safe to run, and it isn't a sandbox: the session's boundary is [dejima](https://gitbox.branderson.io/dejima/dejima)'s.

What it does claim is that it doesn't widen that boundary. The agent is presumed hostile, as dejima presumes it:

- **What the agent leaves in `~/shared` is read by descriptor, never by path.** Every component is opened with `O_NOFOLLOW`, every file is checked to be a regular file before it's read, and the output zip is rebuilt from scratch rather than passed on. A session can rearrange its `~/shared` while it's being read, and can't make the host read or write outside it.
- **What the agent writes reaches every frontend inert.** Each escapes HTML, shows where every link goes, and labels the agent's text as the agent's. The Matrix bot also lets nothing the agent writes mention anyone. darner-web renders on the server, loads no image, and sends a CSP that allows no inline script or style.
- **The agent gets only the domains a person approves.** A domain request is posted for an allowlisted user to allow or deny. What's granted is the launcher's saved copy of the request as shown, minus what the API's policy refuses, so the agent can't change it after it's been shown.
- **Only the people a frontend lets in can act through it.** For the Matrix bot, that's its allowlist, and a reply counts as being about a session only when it replies to one of the bot's own messages. For darner-web, it's the proxy in front of it, which must authenticate and authorize every request. Either way the gate is the frontend's, not the API's: see below.

## Every frontend holds a key to the whole system

The API takes requests from frontends: the Matrix bot, darner-web ([docs/web.md](docs/web.md)), and possibly Slack or others later. Each frontend authenticates with an API token, and the API trusts it completely. It acts on whatever requester the frontend names, so deciding who may use darner is the frontend's job: the Matrix bot does it with its allowlist, and darner-web with the proxy in front of it. The API can't check that itself, because a token holder can name any requester.

So adding a frontend adds a way in. Each one, and anyone who can administer it or read its configuration, can do everything the API does within its policy, for every task started through that token. Before adding one, treat it as you would the ssh key below:

- **Give each frontend its own token**, so one can be revoked by removing its line from `[tokens]`, and its tasks stay separate from the others'.
- **Keep the token where only the frontend can read it.** For the Matrix bot, that's the plugin's config in maubot, so a maubot admin can read it, or just edit the allowlist: maubot admin access is as good as the token. For darner-web, it's `web.toml`, so whoever can read that file, or get into darner-web's container, has the token.
- **Know who the frontend lets in.** Its access control is the only access control there is.
- **Check how it shows what the agent wrote.** The API hands summaries, help requests and screens over as the agent wrote them. Making them inert, so a link can't hide where it goes and markup can't run or ping anyone, is each frontend's job. `POST /v1/selftest` sends text written like a hostile agent's down those paths, each line saying what a safe frontend shows; see [Checking a frontend](docs/development.md#checking-a-frontend).

The API refuses to start with a token that's the example's placeholder or shorter than 32 characters.

## What it holds, which is the new risk it introduces

Starting sessions for other people means holding the means to start them, so darner is a concentration of privilege that didn't exist before you ran it.

| It holds | Taking it gets | It doesn't get |
| --- | --- | --- |
| A user a frontend lets in, such as an allowlisted Matrix account or someone past darner-web's proxy | Everything that frontend does for them: sessions within the API's policy, their output, answers typed into them, domain approvals, and clearing them, which deletes the `~/shared` darner kept for each | Anything past the API's policy |
| A frontend's API token, or admin access to the frontend | The same, for every task started through that token, without its access control | Other tokens' tasks, anything past the API's policy |
| The API's ssh key to the dejima host | The launcher's fixed command grammar, as the dejima host's user: sessions with any domains and dependencies, typing into them if `--answers` is on, their output, stopping them, deleting an ended session's task directory, and pruning its `~/shared`: at once only for the zip hash the API names, which must be the zip the launcher last sent and match a rebuild of the share exactly, as the launcher checks itself, and otherwise once the API says the session was cleared or purged, which discards whatever the share holds | A shell, a file outside the task and share directories, a tunnel |
| `configs_dir/files` | Whatever you put there, such as an API token handed to sessions | |
| The API's log | Who started, typed into, uploaded to, stopped or decided domains for each session, and what was granted; never what was typed or uploaded | The text or files themselves |
| The API's database | Every open session's prompt and context, and who asked; an ended session's output until its frontend collects it, at most `output_retention` | Ended sessions, which are deleted. Output zips, which are kept in `results_dir` for the same time |

The policy (`[policy]`: which domains, whether internal ones, and which dependency kinds) is the API's. The launcher re-validates every job and request, but it doesn't apply the policy itself, so whoever holds the ssh key isn't bound by it. The key is restricted at the dejima host end, with `command=`, `restrict` and `from=` in `authorized_keys`, rather than trusted on the API's side.

## What it deliberately does not do

- **It doesn't check what the agent did.** The summary and the zip are the agent's work, written by something presumed hostile. Read them as that.
- **It doesn't keep secrets from the session.** A file a config set gives a session is in its `~/shared/uploads`, readable by the agent and by anything it can reach, for as long as the session runs. Any user a frontend lets in who names that config set gives the file to their session.
- **It doesn't keep tasks from the frontend's own service.** Prompts, context, summaries and zips pass through the frontend: for the Matrix bot, the homeserver and every client in the room, so use an encrypted room if that matters. For darner-web, the proxy in front of it, which sees every request and response, and the browser, though its pages are sent with `Cache-Control: no-store`.
- **It doesn't stop a user a frontend lets in.** They can grant what the policy allows, and with `--answers`, type anything into a session, including a harness command such as `/compact`, or a shell command where the harness takes one. That's inside the session's container.

## This is not proven software

Early development. Nothing here has been through outside review, an external audit, or adversarial testing by anyone other than the people who wrote it. Every security property described above is a claim awaiting scrutiny rather than one that has survived it. Don't deploy it where being wrong about it would matter.

## Reporting a vulnerability

Email **report@dejima.page**. Please don't open a public issue for a vulnerability; a report there is a disclosure.

The organisation-wide policy, including what you can expect in return and which versions are supported, is [dejima's SECURITY.md](https://gitbox.branderson.io/dejima/dejima/src/branch/main/SECURITY.md). In short: only the tip of `main`, which is the latest release, is supported, and nothing is promised.

## No warranty

GPL-3.0-or-later, sections 15 and 16. This is provided as-is.
