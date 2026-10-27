# meeseeks

Starts a dejima session for a task sent from chat. A task request carries the task, extra context, preconfigured context blurbs, network and dependency grants, and an optional harness and backend. Meeseeks starts `dejima --isolated` for it in a tmux window on a dejima host and hands the agent the composed prompt. When the agent signals it is done, meeseeks delivers what it left in `~/shared` back to the requester. Reaching the session itself is dejima's remote access.

```
maubot plugin ──► meeseeks-api ──ssh──► meeseeks-launch ──► tmux new-window ── dejima --isolated
```

## Setup

Meeseeks has three parts, which can share a machine or not:

- **Dejima host:** runs dejima and `meeseeks-launch`. The API reaches it over ssh with a key that can run nothing but the launcher.
- **API host:** runs `meeseeks-api`, which the bot talks to.
- **Maubot:** runs the bot plugin, logged in as a Matrix account for the bot.

You need:

- dejima, working on the dejima host, with `--isolated`, `--domains`, `--dependencies` and `--shared-path-file`
- tmux, pipx, git and Python 3.11 or newer on the dejima host
- git on the API host, plus either Python 3.11 or newer (to run the API under systemd) or Docker with Compose
- a maubot server, and a Matrix account for the bot

### Values you'll need

The commands below take your values from shell variables, so they can be pasted as they are. Each step starts by setting the variables it uses, on the machine it runs on. Replace the example values there with yours:

| variable | what it is | where the value comes from |
|---|---|---|
| `MEESEEKS_REPO` | this repository's clone URL | your forge's clone button |
| `DEJIMA_HOST` | the dejima host's hostname or IP, as the API host reaches it over ssh | your network; test with `ssh $DEJIMA_HOST` from the API host |
| `DEJIMA_USER` | the account on the dejima host that runs dejima; the API logs in as it | `whoami` on the dejima host, as the user you run dejima as |
| `API_ADDR` | the address the API host's connections arrive from, as the dejima host sees them. Under Docker, that's the Docker host's address | `ip -brief addr` on the API host. For the address the dejima host actually sees, run `echo $SSH_CLIENT` in an ssh session from the API host, or check the `from=` on a key that machine already uses, such as dejima-guard's |
| `API_PUBKEY` | the API's ssh public key | printed in step 3 |
| `API_TOKEN` | the secret the bot uses to authenticate to the API | generated in step 5 |
| `MAUBOT_URL` | your maubot server's URL | where you open maubot's web UI |
| `MAUBOT_ADMIN` | a maubot admin user | `admins:` in maubot's config |
| `MAUBOT_HOMESERVER` | the bot's homeserver, by its name in maubot's config | the keys under `homeservers:` in maubot's config; `mbc auth --list` shows them |
| `BOT_USER` | the bot's Matrix username, without `@` or `:server` | you choose it when creating the account |

These set where things are installed. The defaults work as they are:

| variable | default | what goes there |
|---|---|---|
| `LAUNCHER_SRC` | `~/src/meeseeks` | the launcher's clone, on the dejima host |
| `MEESEEKS_DIR` | `/opt/meeseeks` | on the API host: the clone and virtualenv under systemd, or the whole deployment directory under Docker Compose |
| `MEESEEKS_CONFIG` | `/etc/meeseeks` | systemd only: `api.toml` and the blurbs |
| `MEESEEKS_STATE` | `/var/lib/meeseeks` | systemd only: the database, results and ssh key; also the service user's home |

Under systemd, the service user `meeseeks` must be able to read `MEESEEKS_DIR` and `MEESEEKS_CONFIG` and write `MEESEEKS_STATE`. A directory inside a private home directory won't work.

Values you type somewhere other than a shell, such as maubot's web UI, are called out where they come up.

### 1. Install the launcher on the dejima host

On the dejima host, as `DEJIMA_USER`:

```sh
MEESEEKS_REPO=https://git.example.com/example/meeseeks.git
LAUNCHER_SRC=~/src/meeseeks

git clone "$MEESEEKS_REPO" "$LAUNCHER_SRC"
pipx install --editable "$LAUNCHER_SRC"
```

This installs `~/.local/bin/meeseeks-launch`. Make sure dejima can start a session in a detached tmux window without stopping to ask anything:

- Acknowledge CRITICAL audit findings with `dejima-audit ack`.
- Pre-record decisions for any credential-shaped values dejima's staging scan flags.
- Run `sudo loginctl enable-linger "$USER"` (or set `KillUserProcesses=no`), so the tmux server outlives the ssh connection.
- Pre-answer Claude Code's fullscreen renderer offer. Each isolated session gets a throwaway copy of your `~/.claude.json`, so Claude Code keeps offering the renderer, which it does until it has shown the offer three times. With Claude Code closed on this host, record that it has:

  ```sh
  cp ~/.claude.json ~/.claude.json.bak
  jq '.fullscreenUpsellSeenCount = 3' ~/.claude.json.bak > ~/.claude.json
  ```

  dejima marks an isolated session's working directory, `/dejima/work`, as trusted (dejima #428, after release 2026.10.14). With an older dejima, Claude Code also asks whether to trust that folder. Then add `| .projects["/dejima/work"].hasTrustDialogAccepted = true` to the `jq` filter. The directory is empty and container-local, so trusting it trusts no files of yours.

Optionally, add a `[identity.meeseeks]` section to dejima's config, for the git name and keys every meeseeks session should act as (see dejima's configuration docs). Then add `--identity meeseeks` to the launcher options in step 4.

Try a launch by hand:

```sh
echo '{"id": "ms-hello", "prompt": "Write hello to ~/shared/RESULT.md, then create ~/shared/.done"}' \
  | ~/.local/bin/meeseeks-launch
tmux attach -t meeseeks                      # watch it; detach with Ctrl-b d
~/.local/bin/meeseeks-launch status          # "ready": true once .done exists
~/.local/bin/meeseeks-launch fetch ms-hello > /tmp/hello.zip
```

Quit the session when you're done, then remove `~/meeseeks/tasks/ms-hello`.

### 2. Install the API

The API runs either as a systemd service or in Docker Compose. Steps 2, 3 and 5 give both; pick one and follow it throughout. All of it runs on the API host.

**With systemd:** create a service user and install into a virtualenv:

```sh
MEESEEKS_REPO=https://git.example.com/example/meeseeks.git
MEESEEKS_DIR=/opt/meeseeks
MEESEEKS_STATE=/var/lib/meeseeks

sudo useradd --system --create-home --home-dir "$MEESEEKS_STATE" --shell /usr/sbin/nologin meeseeks
sudo git clone "$MEESEEKS_REPO" "$MEESEEKS_DIR/src"
sudo python3 -m venv "$MEESEEKS_DIR/venv"
sudo "$MEESEEKS_DIR/venv/bin/pip" install --editable "$MEESEEKS_DIR/src[api]"
```

**With Docker Compose:** lay out a deployment directory around a clone, and build the image. The container runs as uid 10001, so its data directory belongs to that uid:

```sh
MEESEEKS_REPO=https://git.example.com/example/meeseeks.git
MEESEEKS_DIR=/opt/meeseeks

sudo install -d -o "$USER" "$MEESEEKS_DIR" && cd "$MEESEEKS_DIR"
git clone "$MEESEEKS_REPO" src
cp src/examples/compose.yaml .
sudo install -d -m 700 -o 10001 -g 10001 data
sudo install -d -m 750 -g 10001 config config/blurbs
sudo docker compose build
```

### 3. Give the API an ssh key and the dejima host's host key

The API reaches the dejima host with a key of its own. On the API host:

```sh
DEJIMA_HOST=dejima.example.com
MEESEEKS_DIR=/opt/meeseeks          # Docker Compose
MEESEEKS_STATE=/var/lib/meeseeks    # systemd
```

**With systemd:**

```sh
sudo -u meeseeks install -d -m 700 "$MEESEEKS_STATE/.ssh"
sudo -u meeseeks ssh-keygen -t ed25519 -N '' -C meeseeks-api -f "$MEESEEKS_STATE/.ssh/id_ed25519"
sudo cat "$MEESEEKS_STATE/.ssh/id_ed25519.pub"     # this is API_PUBKEY, for step 4
```

**With Docker Compose:** generate the key inside the container, so it lands in `data/.ssh` with the right owner:

```sh
cd "$MEESEEKS_DIR"
sudo docker compose run --rm --entrypoint sh api -c \
  'install -d -m 700 ~/.ssh && ssh-keygen -t ed25519 -N "" -C meeseeks-api -f ~/.ssh/id_ed25519'
sudo cat data/.ssh/id_ed25519.pub                  # this is API_PUBKEY, for step 4
```

Give meeseeks this key of its own even if the API host already reaches the dejima host over ssh, for instance because it runs dejima-guard. Each existing key is locked to its own forced command, and loosening one to also run the launcher would hand that tool's machine the ability to start sessions.

Then record the dejima host's host key, after checking that it's the right one. If this machine already trusts it, for instance in dejima-guard's `known_hosts`, you can copy that entry instead. Otherwise, fetch it and print its fingerprint:

```sh
ssh-keyscan -t ed25519 "$DEJIMA_HOST" > /tmp/dejima.hostkey
ssh-keygen -lf /tmp/dejima.hostkey
```

On the dejima host, print the fingerprint of its real host key. It must match:

```sh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

If they match, save it on the API host:

```sh
# With systemd:
sudo -u meeseeks tee -a "$MEESEEKS_STATE/.ssh/known_hosts" < /tmp/dejima.hostkey
# With Docker Compose:
sudo install -m 644 -o 10001 -g 10001 /tmp/dejima.hostkey "$MEESEEKS_DIR/data/.ssh/known_hosts"
```

### 4. Lock the key to the launcher on the dejima host

On the dejima host, as `DEJIMA_USER`, authorise the API's key to run the launcher and nothing else. If dejima-guard watches the dejima host, this edit to `authorized_keys` raises its critical-path alert. Run `dejima-guard expect 30m` on the guard's machine first, then `dejima-guard accept` once this step is done.

```sh
API_ADDR=192.0.2.10
API_PUBKEY='ssh-ed25519 AAAA... meeseeks-api'
SHARE_DIR=$(dejima --print-config | sed -n 's/^core.share_dir=//p'); SHARE_DIR=${SHARE_DIR/#\~/$HOME}
LAUNCHER_OPTIONS="--dejima $(command -v dejima) --dejima-allow $(command -v dejima-allow) --share-root $SHARE_DIR/isolated --harness claude-code --backend claude-code=anthropic"

install -d -m 700 ~/.ssh
touch ~/.ssh/authorized_keys
[ -z "$(tail -c 1 ~/.ssh/authorized_keys)" ] || echo >> ~/.ssh/authorized_keys   # end the last line first
echo "from=\"$API_ADDR\",command=\"$HOME/.local/bin/meeseeks-launch $LAUNCHER_OPTIONS\",restrict $API_PUBKEY" \
  >> ~/.ssh/authorized_keys
ssh-keygen -lf ~/.ssh/authorized_keys | grep meeseeks-api   # no output means sshd can't read the line
```

- `command=` makes the launcher the only thing this key can run, whatever the client asks for. Its paths are absolute, since a non-interactive ssh login has a minimal `PATH` without `~/.local/bin`. That is also why `--dejima` gives dejima's full path. `--share-root` comes from dejima's own `share_dir`, since output is only read from where dejima puts isolated sessions' `~/shared`.
- `restrict` turns off forwarding and terminals, and `from=` accepts the key only from `API_ADDR`.
- `LAUNCHER_OPTIONS` holds the defaults every session gets. Add `--identity meeseeks` if you set one up in step 1, and `--answers` if replies in chat may be typed into sessions. All options are listed under [Launcher options](#launcher-options).
- To change the options later, delete the `meeseeks-api` line and run this step again with new `LAUNCHER_OPTIONS`. Don't edit it in place: an option outside the quotes makes sshd skip the whole line, which shows up as `Permission denied (publickey)`.

### 5. Configure and start the API

On the API host, set the values the config needs, and generate the bot's token:

```sh
DEJIMA_HOST=dejima.example.com
DEJIMA_USER=operator
MEESEEKS_DIR=/opt/meeseeks
MEESEEKS_CONFIG=/etc/meeseeks       # systemd
MEESEEKS_STATE=/var/lib/meeseeks    # systemd
API_TOKEN=$(openssl rand -hex 32)
echo "$API_TOKEN"     # keep this for step 6
```

**With systemd:** install the example config, readable only by root and the service, and fill in your values and directories:

```sh
sudo install -d -m 750 -g meeseeks "$MEESEEKS_CONFIG" "$MEESEEKS_CONFIG/blurbs"
sudo install -m 640 -g meeseeks "$MEESEEKS_DIR/src/examples/api.toml" "$MEESEEKS_CONFIG/api.toml"
sudo sed -i -e "s/operator@dejima.example.com/$DEJIMA_USER@$DEJIMA_HOST/" \
  -e "s/replace-with-a-long-random-token/$API_TOKEN/" \
  -e "s|/var/lib/meeseeks|$MEESEEKS_STATE|g" -e "s|/etc/meeseeks|$MEESEEKS_CONFIG|g" \
  "$MEESEEKS_CONFIG/api.toml"
```

`listen` stays on `127.0.0.1:8080`, which is right if maubot runs on the same machine. Otherwise, edit it to a private address, or put a TLS reverse proxy in front: the token travels in every request.

Start it:

```sh
sed -e "s|/opt/meeseeks|$MEESEEKS_DIR|g" -e "s|/var/lib/meeseeks|$MEESEEKS_STATE|g" \
  -e "s|/etc/meeseeks|$MEESEEKS_CONFIG|g" "$MEESEEKS_DIR/src/examples/meeseeks-api.service" \
  | sudo tee /etc/systemd/system/meeseeks-api.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now meeseeks-api
```

Check both links:

```sh
sudo -u meeseeks ssh -T -o BatchMode=yes "$DEJIMA_USER@$DEJIMA_HOST" status   # prints []
curl -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8080/v1/blurbs   # prints []
```

`journalctl -u meeseeks-api` shows the API's log.

**With Docker Compose:** the config directory is mounted at `/etc/meeseeks` in the container, so the example's paths work unchanged. `listen` becomes `0.0.0.0:8080`, which is inside the container. `compose.yaml` decides what reaches it: by default, only `127.0.0.1:8080` on the host. If maubot runs in Docker too, drop the published port and put both on a shared network.

```sh
cd "$MEESEEKS_DIR"
sudo install -m 640 -g 10001 src/examples/api.toml config/api.toml
sudo sed -i -e "s/operator@dejima.example.com/$DEJIMA_USER@$DEJIMA_HOST/" \
  -e "s/replace-with-a-long-random-token/$API_TOKEN/" \
  -e 's/127.0.0.1:8080/0.0.0.0:8080/' config/api.toml
sudo docker compose up -d
```

Check both links:

```sh
sudo docker compose exec api ssh -T -o BatchMode=yes "$DEJIMA_USER@$DEJIMA_HOST" status   # prints []
curl -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8080/v1/blurbs               # prints []
```

`docker compose logs api` shows the API's log.

If the ssh check asks for a password or prints anything but JSON, recheck step 4. A common cause is `API_ADDR` not matching where the connection really comes from.

To link sessions from chat, set `remote_access_url` to the `base_url` of dejima's web remote access, if you use it. The bot then adds a link to the "Started" reply and to help requests. Opening the link is protected however your remote access is, such as by the reverse proxy in front of it. The link only works if dejima registers sessions in `/run/user/<uid>`, which needs `loginctl enable-linger` from step 1.

Context blurbs go in the blurbs directory as `<name>.md`: `$MEESEEKS_CONFIG/blurbs`, or `$MEESEEKS_DIR/config/blurbs` under Docker. [`examples/blurbs`](examples/blurbs) has one. Every config key is described under [API config](#api-config).

### 6. Set up the bot in maubot

Create the bot's Matrix account, named `BOT_USER`. Use your homeserver's registration, or `register_new_matrix_user` on Synapse.

On any machine, install maubot's CLI, log in to your maubot server, and log the bot in through it. The bot's homeserver has to be listed under `homeservers:` in maubot's config. `--update-client` creates the bot's client in maubot with a device of its own, which encrypted rooms need.

```sh
MAUBOT_URL=https://maubot.example.org
MAUBOT_ADMIN=admin
MAUBOT_HOMESERVER=example.org
BOT_USER=meeseeks

pipx install maubot
mbc login -s "$MAUBOT_URL" -u "$MAUBOT_ADMIN"
mbc auth -h "$MAUBOT_HOMESERVER" -u "$BOT_USER" --update-client
```

Download the plugin, `io.example.meeseeks-v<version>.mbp`, from the repository's latest release, then upload it in maubot's web UI under **Plugins**. `mbc upload <file>` does the same from the command line.

In maubot's web UI:

1. Under **Instances**, create one with the bot's client as the primary user and `io.example.meeseeks` as the type.
2. Set the instance config:
   - `allowlist`: your Matrix ID, and anyone else's who may start tasks, e.g. `["@you:example.org"]`
   - `api_url`: where the API is reachable from maubot. That's `http://127.0.0.1:8080` with the defaults on the same machine, or `http://api:8080` from a container on the same Docker network
   - `api_token`: `API_TOKEN` from step 5
3. Invite the bot to the room you keep notes in, and accept the invite. The simplest way is to turn on **Autojoin** on the bot's client, invite it, then turn autojoin off again, so only rooms you choose matter.

Encrypted rooms need maubot's encryption support installed on the maubot server; see maubot's documentation.

### 7. Check it end to end

In the room, send a message that mentions the bot and says `help`. The reply lists your context blurbs, which also shows that the bot reaches the API.

Then reply to a note, mentioning the bot, with a line of context. The bot answers with the task id, and `tmux attach -t meeseeks` on the dejima host shows the session. When the agent finishes, the bot replies again with its summary and a zip.

## Updating

Update in this order, because each part may rely on commands or fields added to the one after it: the dejima host, then the API, then the plugin. Look at the commit log first for anything that needs a step of its own.

**Dejima host**, as `DEJIMA_USER`. The install is editable, so pulling is the update:

```sh
LAUNCHER_SRC=~/src/meeseeks
git -C "$LAUNCHER_SRC" pull
```

Sessions already running are unaffected.

**API host.** Settings added by an update have defaults, so an `api.toml` written for an earlier version keeps working. At startup the API logs every setting it uses, marking the defaults, and warns about any key it doesn't recognise, such as a misspelling. Check that log after updating, and compare `api.toml` with [`examples/api.toml`](examples/api.toml) for new settings worth setting. Back up the database while the service is stopped.

With systemd:

```sh
MEESEEKS_DIR=/opt/meeseeks
MEESEEKS_STATE=/var/lib/meeseeks

sudo systemctl stop meeseeks-api
sudo -u meeseeks cp "$MEESEEKS_STATE/tasks.db" "$MEESEEKS_STATE/tasks.db.bak"
sudo git -C "$MEESEEKS_DIR/src" pull
sudo "$MEESEEKS_DIR/venv/bin/pip" install --editable "$MEESEEKS_DIR/src[api]"   # picks up new dependencies
sudo systemctl start meeseeks-api
```

With Docker Compose:

```sh
MEESEEKS_DIR=/opt/meeseeks

cd "$MEESEEKS_DIR"
sudo docker compose stop
sudo cp data/tasks.db data/tasks.db.bak
git -C src pull
sudo docker compose build --pull
sudo docker compose up -d
```

**Plugin.** Download the `.mbp` from the new release and upload it, as in setup step 6. Maubot replaces the plugin and restarts its instances. Instance config is kept, and new keys get their defaults.

Dejima itself updates independently, but the launcher depends on the flags listed under Setup.

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

Or send the task directly, in a message that mentions the bot and isn't a reply. That message, minus the mention and the keyword lines, is the task, with no extra context.

`@meeseeks help` replies with this usage, including the available context blurbs.

When the agent needs to reach domains the sandbox blocks, it lists them in `~/shared/DOMAINS` and the bot posts every one for you to check. A line can name a port, as in `internal recipes.example.com:8443`. The grant covers the domain on every port, and the port is the one dejima-allow checks it answers on. Reply `@meeseeks allow` to that message to let the running session reach them, or `@meeseeks deny`. Internal domains are marked as such, and any the API's `deny_domains` policy refuses are marked and never allowed. An agent that edits `DOMAINS` without signalling again is still asking: the changed list becomes a request once it has been left alone for 30 seconds. After you decide a request, a follow-up, such as asking again for a domain as internal, comes straight through. What gets allowed is the list as shown: the launcher keeps its own copy, so the agent can't change it afterwards. If answering from chat is on, the agent is told the outcome. Otherwise, tell it in the session.

If a session's screen stops changing for `waiting_after` seconds (5 minutes by default), the bot posts that it seems to be waiting, with the end of its screen as a code block marked as agent-written. That catches an agent that asked a question at its prompt, or left its answer there without signalling, whatever the harness. It isn't posted for a session waiting on a help or domain request, one that has a result to deliver, one that has delivered output and been told nothing since, or one that has asked since its screen last changed. Each quiet spell is reported once. Reply to the notice with a mention to tell the session something. `waiting_screen = false` leaves out the screen.

To manage open sessions, mention the bot with one of these. Replying to any of the bot's messages about a session works without the id.

| command | |
|---|---|
| `sessions` | lists the open sessions: what each is waiting on, its age, who started it, and where to reach it |
| `status ms-xxxxx` | what a session is waiting on, quoting its help request or listing its requested domains |
| `tell ms-xxxxx <message>` | types a message into the session, which also answers an outstanding help request; needs `--answers` on the launcher |
| `upload ms-xxxxx` | in reply to a file: puts it in the session's `~/shared/uploads`; a file sent as a reply to one of the bot's messages about a session goes there too |
| `stop ms-xxxxx` | ends the session |

Replying to one of the bot's messages about a session with anything else, with a mention, sends it to the session like `tell`.

To end a session, reply `@meeseeks stop` to any of the bot's messages about it, or send `@meeseeks stop ms-xxxxx`. The launcher stops the session's sandbox container, so dejima exits and cleans up as usual, then closes its tmux window.

The note is the task. The rest of the reply, minus the mention and the keyword lines, is extra context. Keyword lines take comma or space separated values and can repeat. A domain can carry a `:port`, which is ignored, since a launch grant covers every port:

| keyword | meaning |
|---|---|
| `config:` | config sets to use, by name: saved keyword lines for a recurring task. Anything else in the message overrides them: a single value replaces theirs, a list adds to theirs |
| `ctx:` | context blurbs to add, by name |
| `allow:` | domains the session may reach |
| `internal:` | domains inside your network the session may reach, passed to dejima as `--internal-domain` as well as allowed |
| `deps:` | dependencies to install, in `dejima-dep` syntax: `apt:`, `pip:`, `cargo:`, `npm:` or `tarball:` |
| `harness:` | harness for this task, overriding the default |
| `backend:` | backend for this task, overriding the harness's default |
| `model:` | model for this task, overriding the harness's default |
| `effort:` | Claude Code's effort level for this task: `low`, `medium`, `high` or `xhigh` |

The bot replies with the task id and its tmux target, plus a link to the session if the API has `remote_access_url` set, or with why it was refused. It only acts when both the reply and the note are from users in its `allowlist`, since the note becomes the agent's instructions.

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
<to ask for domains, list them in ~/shared/DOMAINS, create ~/shared/.domains and end the turn>
```

The last section can be replaced with `output_instructions` in the API config.

## Output

The session is presumed hostile, and so is everything it writes to `~/shared`. Collecting output never follows a link, never opens anything but a regular file, and copies only file contents into a zip that meeseeks builds itself:

- **Walk:** by file descriptor, never by path. Each entry is opened with `O_PATH`, so no device driver is ever opened, and its type is checked on that handle. Only a regular file is reopened, through `/proc/self/fd`. Swapping in a symlink mid-walk therefore gains nothing.
- **Skipped:** symlinks, FIFOs, devices, and names outside `[A-Za-z0-9][A-Za-z0-9._ +-]*`. That covers dotfiles, `..`, backslashes, control and bidi characters, and invalid UTF-8. Also skipped: Windows reserved names, and names that differ only in case. `MANIFEST.txt` in the zip lists what was included and what was skipped, and why.
- **Limits:** bytes actually read (50 MiB by default), files included, entries visited, depth, path length and time taken. Going over one rejects the output and tells the requester why.
- **Forgotten signal:** an agent can forget `.done`. A new `RESULT.md` is collected anyway once nothing directly in `~/shared` has changed for `--quiet-period`, or as soon as the session ends. The bot says so in its reply. Each delivery records the `RESULT.md` it carried, so the same one isn't sent twice.
- **Signal:** a `.done` is consumed once. The launcher records which one it consumed, so a session that makes `.done` impossible to delete can't trigger repeat deliveries. A new `.done` signals new output.
- **Entries:** mode `0644`, a fixed timestamp, and no symlink or directory entries.
- **Re-check:** the API verifies the archive again and rebuilds it with its own writer before storing it, so what it serves is exactly what it checked.
- **Delivery:** the bot renders the summary's markdown with raw HTML escaped, and shows every link and image with its destination in plain sight, so the agent can't hide where a link goes. Messages carry no mentions, so the text can't ping the room.

`HELP.md` is read the same way, as a regular file only, at most 16 KiB.

A config set's `file:` lines give every session started with it those files, such as an API token the task needs. The API sends them with the job and keeps only their names in its database. The launcher holds them owner-only in the task directory until the session's `~/shared` exists, writes them into `~/shared/uploads` as `0600` before the agent starts, deletes its copies, and removes them from `~/shared` when the session ends. The prompt tells the agent where they are. A file given to a session is readable by the agent and by anything it can reach, and any allowlisted user who names the config set gives it to their session. Give it only what the task needs, scoped as narrowly as the service allows.

Uploads write into `~/shared` the same careful way. The `uploads` directory is opened without following a link, and each file is created new with `O_EXCL|O_NOFOLLOW`, so a link or file the session planted is never followed or overwritten. Names are reduced to safe characters, and size is capped. The output zip leaves out the top-level `uploads` directory, since the requester already has those files, and its manifest says so. Its text is sent as plain text, like the summary.

## Answering from chat

Off by default. Turn it on with `--answers` on the launcher, so the dejima host decides whether chat can type into its sessions. An answer is accepted only for an outstanding help request on a running session, and only once: concurrent answers race for it and only one wins. Answers go to the window by its tmux id, so renaming the window inside the session doesn't redirect them. If typing into the session fails, the chat sees only "couldn't type into the session", with no tmux detail.

Before pasting, the launcher strips control characters, ESC above all, so the text can't end the paste early and have the rest read as keystrokes. It then pastes the text as written, so one starting with `/` or `!` reaches Claude Code as a slash or shell command, run inside the session's container. The paste is bracketed, so newlines don't submit it early, and Enter follows after `--paste-delay`. cline is the exception: dejima runs it in plain mode, which reads a line at a time and doesn't take bracketed pastes, so its messages are sent as one line.

The bot only treats a reply as an answer when the replied-to message is its own help request, so a forged tag on someone else's message is ignored.

The files are still agent-written: treat what is in the zip as untrusted. Neither the API's `results_dir` nor dejima's `isolated/` share directories are pruned.

## Reference

### Launcher options

| option | default | meaning |
|---|---|---|
| `--identity NAME` | dejima's | `[identity.<name>]` every session acts as; a task cannot choose it |
| `--harness NAME` | dejima's | default harness |
| `--backend HARNESS=NAME` | dejima's | default backend for a harness; repeatable |
| `--model HARNESS=MODEL` | dejima's | default model for a harness; repeatable |
| `--effort HARNESS=LEVEL` | dejima's | default effort level for a harness that has one (claude-code); repeatable |
| `--session NAME` | `meeseeks` | tmux session the windows go in |
| `--tasks-dir DIR` | `~/meeseeks/tasks` | per-task grant files, argv and exit status |
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
| `--stop-timeout SECONDS` | 60 | how long a stopped session may take to exit before dejima itself is ended |
| `--help-interval SECONDS` | 300 | least time between a task's help requests, and between domain requests while one awaits a decision, so a session can't flood the room |

The API sends one of these commands, which arrive as `SSH_ORIGINAL_COMMAND`: none (launch the job on stdin), `status`, `fetch <id>`, `catalog`, `help <id>`, `answer <id>`, `message <id>`, `upload <id>`, `stop <id>`, `domains <id>`, `allow <id> <n>` or `deny <id> <n>`. Anything else is refused.

Grants use `dejima --domains FILE` and `--dependencies FILE`, and output collection uses `--shared-path-file FILE`. A task without grants passes neither grant flag.

### API config

[`examples/api.toml`](examples/api.toml), with:

| key | meaning |
|---|---|
| `listen` | `host:port` to listen on |
| `db_path` | SQLite database of tasks and events |
| `blurbs_dir` | context blurbs, as `<name>.md`; read per request, so an edit needs no restart |
| `max_upload` | largest upload in bytes, default 50 MiB; keep it at or under the launcher's `--max-upload` |
| `waiting_after` | seconds a running session's screen can stay unchanged before the bot says it seems to be waiting (default 300) |
| `waiting_screen` | whether that notice includes the end of the session's screen (default true) |
| `configs_dir` | config sets, by default `configs` beside `blurbs_dir`; one file per set named as it's used, holding keyword lines (see [`examples/configs`](examples/configs)); read per request. A set's `file: <name>` lines give each of its sessions `configs_dir/files/<name>` (up to 1 MiB) in `~/shared/uploads` |
| `default_blurbs` | blurb names added to every task's prompt, before any it names with `ctx:`; each must exist |
| `results_dir` | collected output archives |
| `poll_interval` | seconds between checks on the dejima host |
| `launch_command` | how to reach the launcher; the command is appended, and over ssh arrives as `SSH_ORIGINAL_COMMAND` |
| `launch_timeout` | seconds a launcher call may take |
| `output_instructions` | replaces the prompt's last section |
| `remote_access_url` | dejima's web remote access `base_url`, to link each session from chat; unset means no links |
| `session_wait` | with `remote_access_url`, seconds a launch waits for dejima to register the session (default 20) |
| `[tokens]` | one bearer token per client, by name |
| `[policy]` | `deny_domains` (a denied domain's parents are denied too), `max_domains`, which also caps the domains a session gains through requests |
| `[limits]` | `max_bytes`, `max_files`: should match the launcher's `--max-output` |

### API endpoints

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
| `GET /v1/events` | unacknowledged events, oldest first: `{seq, id, type, summary, size, answerable, target, source}`, `type` is `output`, `help`, `domains` or `ended`; `data` holds a domain request |
| `GET /v1/tasks/<id>/outputs/<seq>.zip` | an `output` event's archive (`size` 0 means it was rejected, and `summary` says why) |
| `POST /v1/events/<seq>/ack` | mark an event delivered |
| `GET /v1/blurbs` | names of the available context blurbs |
| `GET /v1/configs` | names of the available config sets |
| `GET /v1/catalog` | the dejima host's harnesses and backends, and the launcher's default harness |
| `POST /v1/tasks/<id>/domains/<n>/allow` | `{requester}`: grant domain request `n` as it was shown, minus what policy refuses |
| `POST /v1/tasks/<id>/domains/<n>/deny` | `{requester}`: refuse domain request `n` |
| `GET /v1/tasks` | the client's open sessions, with what each is waiting on: `{id, target, link, requester, started, state, answerable, waiting: {answer, domains}}` |
| `GET /v1/tasks/<id>` | one open session, in the same form |
| `POST /v1/tasks/<id>/message` | `{text, requester}`: type a message into the session; 409 with the reason if the host refuses |
| `POST /v1/tasks/<id>/files?name=&requester=` | the request body is a file for the session's `~/shared/uploads`; 413 over `max_upload` |
| `POST /v1/tasks/<id>/stop` | end the task's session and close its tmux window; 409 if it isn't running |
| `POST /v1/tasks/<id>/answer` | `{text, requester}`: answer the task's outstanding help request; 409 with the reason if the host refuses |

`source` is echoed back as given, so a client knows where to reply without keeping state.

### Maubot instance config

| key | meaning |
|---|---|
| `allowlist` | Matrix IDs that may start tasks and answer help requests |
| `api_url`, `api_token` | where the API is and the token to use |
| `poll_interval` | seconds between checks for task updates |

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

To build the plugin locally, run `mbc build` in `plugin/`.

[`.gitea/workflows/tests.yml`](.gitea/workflows/tests.yml) runs the tests on every pull request on a `dejima-confined` runner, and builds the API's image on a `dejima-privileged` one. What the jobs reach is declared in [`.dejima/allowed-domains`](.dejima/allowed-domains), which the runners' egress allowlist reads from `main`.

### Releasing

Pull requests target `development`, which merges to `main` when it's ready to release. `development` carries the next version: the first change after a release bumps both `plugin/maubot.yaml` and `pyproject.toml`.

[`.gitea/workflows/release.yml`](.gitea/workflows/release.yml) publishes the plugin on a `dejima-confined` runner:

- **A push to `development`** creates a pre-release of `<version>.dev<run>`, such as `0.1.18.dev42`, with the plugin attached. Pre-releases aren't the forge's latest release, and maubot orders them before `<version>`, so a stable release supersedes one. To try a development build, download its `.mbp` from the releases page and upload it in maubot's web UI under **Plugins**.
- **A push to `main`** whose version has no `v<version>` tag yet creates the stable release, and with it the tag at that commit. A push that doesn't change the version releases nothing. Pushing a `v<version>` tag by hand releases that tag the same way.

The workflow fails if the two versions disagree, or if a pushed tag doesn't match them. It uses the job's automatic `GITEA_TOKEN`, so Actions must be enabled for the repository, and the token must be allowed to create releases.
