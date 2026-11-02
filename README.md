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

&nbsp;<img src="assets/dejima-wordmark-darkmode.png" alt="dejima" height="72">

---

&nbsp;

<!-- darner's own logo goes here, from assets/, once it's drawn: see assets/README.md. -->
# darner

Starts a dejima session for a task sent from chat. A task request carries the task, extra context, preconfigured context blurbs, network and dependency grants, and an optional harness and backend. darner starts `dejima --isolated` for it in a tmux window on a dejima host and hands the agent the composed prompt. When the agent signals it is done, darner delivers what it left in `~/shared` back to the requester. Reaching the session itself is dejima's remote access.

```
maubot plugin ──► darner-api ──ssh──► darner-launch ──► tmux new-window ── dejima --isolated
```

[![Developed in dejima](assets/dejima-badge.svg)](https://gitbox.branderson.io/dejima/dejima)

## Setup

darner has three parts, which can share a machine or not:

- **Dejima host:** runs dejima and `darner-launch`. The API reaches it over ssh with a key that can run nothing but the launcher.
- **API host:** runs `darner-api`, which the bot talks to.
- **Maubot:** runs the bot plugin, logged in as a Matrix account for the bot.

Optionally, **darner-web** gives a browser everything the bot does, and more: its sessions at a glance, each one's events so far, and the full text of blurbs and config sets. It runs as its own container with its own token, behind a proxy you configure to authenticate and authorize. [docs/web.md](docs/web.md) sets it up and says what reaching it means.

You need:

- dejima, working on the dejima host, at a release with every flag and tool the launcher uses; [docs/release.md](docs/release.md) lists them, and `CHANGELOG.md` names the release each version was released against
- tmux 3.2 or newer, pipx, git and Python 3.11 or newer on the dejima host
- git on the API host, plus either Python 3.11 or newer (to run the API under systemd) or Docker with Compose
- a maubot server, and a Matrix account for the bot

### Values you'll need

The commands below take your values from shell variables, so they can be pasted as they are. Each step starts by setting the variables it uses, on the machine it runs on. Replace the example values there with yours:

| variable | what it is | where the value comes from |
|---|---|---|
| `DARNER_REPO` | this repository's clone URL | your forge's clone button |
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
| `LAUNCHER_SRC` | `~/src/darner` | the launcher's clone, on the dejima host |
| `DARNER_DIR` | `/opt/darner` | on the API host: the clone and virtualenv under systemd, or the whole deployment directory under Docker Compose |
| `DARNER_CONFIG` | `/etc/darner` | systemd only: `api.toml` and the blurbs |
| `DARNER_STATE` | `/var/lib/darner` | systemd only: the database, results and ssh key; also the service user's home |

Under systemd, the service user `darner` must be able to read `DARNER_DIR` and `DARNER_CONFIG` and write `DARNER_STATE`. A directory inside a private home directory won't work.

Values you type somewhere other than a shell, such as maubot's web UI, are called out where they come up.

### 1. Install the launcher on the dejima host

On the dejima host, as `DEJIMA_USER`:

```sh
DARNER_REPO=https://git.example.com/example/darner.git
LAUNCHER_SRC=~/src/darner

git clone "$DARNER_REPO" "$LAUNCHER_SRC"
pipx install --editable "$LAUNCHER_SRC"
```

This installs `~/.local/bin/darner-launch`. Make sure dejima can start a session in a detached tmux window without stopping to ask anything:

- Acknowledge CRITICAL audit findings with `dejima-audit ack`.
- Pre-record decisions for any credential-shaped values dejima's staging scan flags.
- Run `sudo loginctl enable-linger "$USER"` (or set `KillUserProcesses=no`), so the tmux server outlives the ssh connection.
- Pre-answer Claude Code's fullscreen renderer offer. Each isolated session gets a throwaway copy of your `~/.claude.json`, so Claude Code keeps offering the renderer, which it does until it has shown the offer three times. With Claude Code closed on this host, record that it has:

  ```sh
  cp ~/.claude.json ~/.claude.json.bak
  jq '.fullscreenUpsellSeenCount = 3' ~/.claude.json.bak > ~/.claude.json
  ```

  dejima marks an isolated session's working directory, `/dejima/work`, as trusted (dejima #428, after release 2026.10.14). With an older dejima, Claude Code also asks whether to trust that folder. Then add `| .projects["/dejima/work"].hasTrustDialogAccepted = true` to the `jq` filter. The directory is empty and container-local, so trusting it trusts no files of yours.

Optionally, add a `[identity.darner]` section to dejima's config, for the git name and keys every darner session should act as (see dejima's configuration docs). Then add `--identity darner` to the launcher options in step 4.

Try a launch by hand:

```sh
echo '{"id": "dn-hello", "prompt": "Write hello to ~/shared/RESULT.md, then create ~/shared/.done"}' \
  | ~/.local/bin/darner-launch
tmux attach -t darner                      # watch it; detach with Ctrl-b d
~/.local/bin/darner-launch status          # "ready": true once .done exists
~/.local/bin/darner-launch fetch dn-hello > /tmp/hello.zip
```

Quit the session when you're done, then remove `~/darner/tasks/dn-hello`.

### 2. Install the API

The API runs either as a systemd service or in Docker Compose. Steps 2, 3 and 5 give both; pick one and follow it throughout. All of it runs on the API host.

**With systemd:** create a service user and install into a virtualenv:

```sh
DARNER_REPO=https://git.example.com/example/darner.git
DARNER_DIR=/opt/darner
DARNER_STATE=/var/lib/darner

sudo useradd --system --create-home --home-dir "$DARNER_STATE" --shell /usr/sbin/nologin darner
sudo git clone "$DARNER_REPO" "$DARNER_DIR/src"
sudo python3 -m venv "$DARNER_DIR/venv"
sudo "$DARNER_DIR/venv/bin/pip" install --require-hashes -r "$DARNER_DIR/src/requirements/api.txt"
sudo "$DARNER_DIR/venv/bin/pip" install --no-deps --no-build-isolation --editable "$DARNER_DIR/src"
```

**With Docker Compose:** lay out a deployment directory around a clone, and build the image. The container runs as uid 10001, so its data directory belongs to that uid:

```sh
DARNER_REPO=https://git.example.com/example/darner.git
DARNER_DIR=/opt/darner

sudo install -d -o "$USER" "$DARNER_DIR" && cd "$DARNER_DIR"
git clone "$DARNER_REPO" src
cp src/examples/compose.yaml .
sudo install -d -m 700 -o 10001 -g 10001 data
sudo install -d -m 750 -g 10001 config config/blurbs
sudo docker compose build
```

### 3. Give the API an ssh key and the dejima host's host key

The API reaches the dejima host with a key of its own. On the API host:

```sh
DEJIMA_HOST=dejima.example.com
DARNER_DIR=/opt/darner          # Docker Compose
DARNER_STATE=/var/lib/darner    # systemd
```

**With systemd:**

```sh
sudo -u darner install -d -m 700 "$DARNER_STATE/.ssh"
sudo -u darner ssh-keygen -t ed25519 -N '' -C darner-api -f "$DARNER_STATE/.ssh/id_ed25519"
sudo cat "$DARNER_STATE/.ssh/id_ed25519.pub"     # this is API_PUBKEY, for step 4
```

**With Docker Compose:** generate the key inside the container, so it lands in `data/.ssh` with the right owner:

```sh
cd "$DARNER_DIR"
sudo docker compose run --rm --entrypoint sh api -c \
  'install -d -m 700 ~/.ssh && ssh-keygen -t ed25519 -N "" -C darner-api -f ~/.ssh/id_ed25519'
sudo cat data/.ssh/id_ed25519.pub                  # this is API_PUBKEY, for step 4
```

Give darner this key of its own even if the API host already reaches the dejima host over ssh, for instance because it runs dejima-guard. Each existing key is locked to its own forced command, and loosening one to also run the launcher would hand that tool's machine the ability to start sessions.

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
sudo -u darner tee -a "$DARNER_STATE/.ssh/known_hosts" < /tmp/dejima.hostkey
# With Docker Compose:
sudo install -m 644 -o 10001 -g 10001 /tmp/dejima.hostkey "$DARNER_DIR/data/.ssh/known_hosts"
```

### 4. Lock the key to the launcher on the dejima host

On the dejima host, as `DEJIMA_USER`, authorise the API's key to run the launcher and nothing else. If dejima-guard watches the dejima host, this edit to `authorized_keys` raises its critical-path alert. Run `dejima-guard expect 30m` on the guard's machine first, then `dejima-guard accept` once this step is done.

```sh
API_ADDR=192.0.2.10
API_PUBKEY='ssh-ed25519 AAAA... darner-api'
SHARE_DIR=$(dejima --print-config | sed -n 's/^core.share_dir=//p'); SHARE_DIR=${SHARE_DIR/#\~/$HOME}
LAUNCHER_OPTIONS="--dejima $(command -v dejima) --dejima-allow $(command -v dejima-allow) --dejima-share $(command -v dejima-share) --share-root $SHARE_DIR/isolated"

install -d -m 700 ~/.ssh
touch ~/.ssh/authorized_keys
[ -z "$(tail -c 1 ~/.ssh/authorized_keys)" ] || echo >> ~/.ssh/authorized_keys   # end the last line first
echo "from=\"$API_ADDR\",command=\"$HOME/.local/bin/darner-launch $LAUNCHER_OPTIONS\",restrict $API_PUBKEY" \
  >> ~/.ssh/authorized_keys
ssh-keygen -lf ~/.ssh/authorized_keys | grep darner-api   # no output means sshd can't read the line
```

- `command=` makes the launcher the only thing this key can run, whatever the client asks for. Its paths are absolute, since a non-interactive ssh login has a minimal `PATH` without `~/.local/bin`. That is also why `--dejima` gives dejima's full path. `--share-root` comes from dejima's own `share_dir`, since output is only read from where dejima puts isolated sessions' `~/shared`.
- `restrict` turns off forwarding and terminals, and `from=` accepts the key only from `API_ADDR`.
- `LAUNCHER_OPTIONS` holds the defaults every session gets. Add `--identity darner` if you set one up in step 1, and `--answers` if replies in chat may be typed into sessions. Sessions use dejima's own default harness and its settings unless a task or an option such as `--harness` says otherwise, and frontends show those defaults. All options are listed under [Launcher options](#launcher-options).
- To change the options later, delete the `darner-api` line and run this step again with new `LAUNCHER_OPTIONS`. Don't edit it in place: an option outside the quotes makes sshd skip the whole line, which shows up as `Permission denied (publickey)`.

### 5. Configure and start the API

On the API host, set the values the config needs, and generate the bot's token:

```sh
DEJIMA_HOST=dejima.example.com
DEJIMA_USER=operator
DARNER_DIR=/opt/darner
DARNER_CONFIG=/etc/darner       # systemd
DARNER_STATE=/var/lib/darner    # systemd
API_TOKEN=$(openssl rand -hex 32)
echo "$API_TOKEN"     # keep this for step 6
```

**With systemd:** install the example config, readable only by root and the service, and fill in your values and directories:

```sh
sudo install -d -m 750 -g darner "$DARNER_CONFIG" "$DARNER_CONFIG/blurbs"
sudo install -m 640 -g darner "$DARNER_DIR/src/examples/api.toml" "$DARNER_CONFIG/api.toml"
sudo sed -i -e "s/operator@dejima.example.com/$DEJIMA_USER@$DEJIMA_HOST/" \
  -e "s/replace-with-a-long-random-token/$API_TOKEN/" \
  -e "s|/var/lib/darner|$DARNER_STATE|g" -e "s|/etc/darner|$DARNER_CONFIG|g" \
  "$DARNER_CONFIG/api.toml"
```

`listen` stays on `127.0.0.1:8080`, which is right if maubot runs on the same machine. Otherwise, edit it to a private address, or put a TLS reverse proxy in front: the token travels in every request.

Start it:

```sh
sed -e "s|/opt/darner|$DARNER_DIR|g" -e "s|/var/lib/darner|$DARNER_STATE|g" \
  -e "s|/etc/darner|$DARNER_CONFIG|g" "$DARNER_DIR/src/examples/darner-api.service" \
  | sudo tee /etc/systemd/system/darner-api.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now darner-api
```

Check both links:

```sh
sudo -u darner ssh -T -o BatchMode=yes "$DEJIMA_USER@$DEJIMA_HOST" status   # prints []
curl -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8080/v1/blurbs   # prints []
```

`journalctl -u darner-api` shows the API's log. Lines from `darner.audit` record each action someone took on a session: starting it with its grants, messages and answers (their length, not their text), uploads (name and size), stops, and domain decisions with what was granted. They say who did it and through which frontend, so who did what can be answered after the session's own data is deleted.

**With Docker Compose:** the config directory is mounted at `/etc/darner` in the container, so the example's paths work unchanged. `listen` becomes `0.0.0.0:8080`, which is inside the container. `compose.yaml` decides what reaches it: by default, only `127.0.0.1:8080` on the host. If maubot runs in Docker too, drop the published port and put both on a shared network.

```sh
cd "$DARNER_DIR"
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

Context blurbs go in the blurbs directory as `<name>.md`: `$DARNER_CONFIG/blurbs`, or `$DARNER_DIR/config/blurbs` under Docker. [`examples/blurbs`](examples/blurbs) has one. Every config key is described under [API config](#api-config).

### 6. Set up the bot in maubot

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
   - `api_token`: `API_TOKEN` from step 5
3. Invite the bot to the room you keep notes in, and accept the invite. The simplest way is to turn on **Autojoin** on the bot's client, invite it, then turn autojoin off again, so only rooms you choose matter.

Encrypted rooms need maubot's encryption support installed on the maubot server; see maubot's documentation.

### 7. Check it end to end

In the room, send a message that mentions the bot and says `help`. The reply lists your context blurbs, which also shows that the bot reaches the API.

Then reply to a note, mentioning the bot, with a line of context. The bot answers with the task id, and `tmux attach -t darner` on the dejima host shows the session. When the agent finishes, the bot replies again with its summary and a zip.

## Updating

Update in this order, because each part may rely on commands or fields added to the one after it: the dejima host, then the API, then the plugin. Look at the commit log first for anything that needs a step of its own.

**Dejima host**, as `DEJIMA_USER`. The install is editable, so pulling is the update:

```sh
LAUNCHER_SRC=~/src/darner
git -C "$LAUNCHER_SRC" pull
```

Sessions already running are unaffected.

**API host.** Settings added by an update have defaults, so an `api.toml` written for an earlier version keeps working. At startup the API logs every setting it uses, marking the defaults, and warns about any key it doesn't recognise, such as a misspelling. Check that log after updating, and compare `api.toml` with [`examples/api.toml`](examples/api.toml) for new settings worth setting. Back up the database while the service is stopped.

With systemd:

```sh
DARNER_DIR=/opt/darner
DARNER_STATE=/var/lib/darner

sudo systemctl stop darner-api
sudo -u darner cp "$DARNER_STATE/tasks.db" "$DARNER_STATE/tasks.db.bak"
sudo git -C "$DARNER_DIR/src" pull
sudo "$DARNER_DIR/venv/bin/pip" install --require-hashes -r "$DARNER_DIR/src/requirements/api.txt"   # pinned dependencies
sudo "$DARNER_DIR/venv/bin/pip" install --no-deps --no-build-isolation --editable "$DARNER_DIR/src"
sudo systemctl start darner-api
```

With Docker Compose:

```sh
DARNER_DIR=/opt/darner

cd "$DARNER_DIR"
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
@dragonlet the import job started failing after the deploy, find out why
ctx: myapp
allow: pypi.org, files.pythonhosted.org
deps: apt:jq pip:pytest-xdist
harness: cline
backend: openrouter
```

Or send the task directly, in a message that mentions the bot and isn't a reply. That message, minus the mention and the keyword lines, is the task, with no extra context.

A mention is a pill for the bot, whatever its text, or the bot's full user ID typed out, such as `@dragonlet:example.org`. The bot's name on its own isn't one, so a room can talk about the bot without starting tasks, and neither is replying to one of its messages without a mention.

`@dragonlet help` replies with this usage, including the available context blurbs.

When the agent needs to reach domains the sandbox blocks, it lists them in `~/shared/DOMAINS` and the bot posts every one for you to check. A line can name a port, as in `internal recipes.example.com:8443`. The grant covers the domain on every port, and the port is the one dejima-allow checks it answers on. Reply `@dragonlet allow` to that message to let the running session reach them, or `@dragonlet deny`. Internal domains are marked as such, and any the API's `deny_domains` policy refuses are marked and never allowed. An agent that edits `DOMAINS` without signalling again is still asking: the changed list becomes a request once it has been left alone for 30 seconds. After you decide a request, a follow-up, such as asking again for a domain as internal, comes straight through. What gets allowed is the list as shown: the launcher keeps its own copy, so the agent can't change it afterwards. If answering from chat is on, the agent is told the outcome. Otherwise, tell it in the session.

If a session's screen stops changing for `waiting_after` seconds (5 minutes by default), the bot posts that it seems to be waiting, with the end of its screen as a code block marked as agent-written. That catches an agent that asked a question at its prompt, or left its answer there without signalling, whatever the harness, and dejima stopping at a prompt of its own before the session starts. It isn't posted for a session waiting on a help or domain request, one that has a result to deliver, one that has delivered output and been told nothing since, or one that has asked since its screen last changed. Each quiet spell is reported once. Reply to the notice with a mention to tell the session something. `waiting_screen = false` leaves out the screen.

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

To end a session, reply `@dragonlet stop` to any of the bot's messages about it, or send `@dragonlet stop dn-xxxxx`. The launcher stops the session's sandbox container, so dejima exits and cleans up as usual, then closes its tmux window.

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
| `effort:` | effort level for this task: `low`, `medium`, `high` or `xhigh`, or `none` for cline, which dejima passes to cline as `--thinking` |
| `context_window:` | context window for this task, in tokens, for a harness that has the setting (cline). Set it a little under what the model's server allows, so cline compacts before the server refuses |
| `network:` | `named`: the session reaches only what this task names and its model, not dejima's own allowlist (dejima's `--drop-domains`). `sealed`: that, and the session doesn't start if it could look up any name, so only addresses are reachable (`--require-sealed`). A sealed session needs its model reached by address, such as a self-hosted one, and a domain request can't grant it a name later |

darner refuses a task asking for a backend, effort level or context window its harness can't take, as dejima's own manifests declare them: the backends speaking one of the harness's `api_styles`, the levels of its `effort` key, and whether it has a `context_window` key. A task naming no harness is held to the one dejima will pick, with its defaults. The API checks against the launcher's catalog, fetched at most every five minutes, and the launcher checks again, with its and dejima's defaults filled in.

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

The session is presumed hostile, and so is everything it writes to `~/shared`. Collecting output never follows a link, never opens anything but a regular file, and copies only file contents into a zip that darner builds itself:

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

Before pasting, the launcher strips control characters, ESC above all, so the text can't end the paste early and have the rest read as keystrokes. It then pastes the text as written, so one starting with `/` or `!` reaches Claude Code as a slash or shell command, run inside the session's container. The paste is bracketed, so newlines don't submit it early, and Enter follows after `--paste-delay`. cline's question prompt drops pastes, so when the screen shows one, the text is typed as keys instead, as one line, which answers in your words rather than picking an option.

The bot only treats a reply as an answer when the replied-to message is its own help request, so a forged tag on someone else's message is ignored.

The files are still agent-written: treat what is in the zip as untrusted.

**Nothing about a session outlives it.** When a session ends, the API deletes everything it kept about it except output its frontend hasn't collected: the prompt and context, who asked, help requests, terminal screens and domain requests. The launcher deletes the session's task directory on the dejima host. Once the frontend acknowledges the output, the bot doing so as soon as it has posted it, the API deletes the rest, zips included. Output nobody collects is deleted after `output_retention`. Your frontend's own copy, such as the room's messages, is yours.

The session's `~/shared` on the dejima host, which dejima keeps if it isn't empty, is pruned with `dejima-share prune --isolated --yes` when the session ends, but only once its output has been delivered whole. All of these must hold:

- the API stored the zip from its latest output, and it's the zip the launcher last sent: the API names it by hash, and the launcher compares
- that delivery left nothing out but `uploads/`
- the share still holds exactly what the delivery carried

Otherwise it's kept: a session that was stopped or ended without output, output that was rejected or couldn't be read, files changed after the delivery, or a delivery that left out symlinks or unreadable files. A kept share holds what darner couldn't deliver, so it's left on the dejima host for you to recover, or remove with `dejima-share prune --isolated`. The API's log says why each was kept. `--keep-shared` on the launcher keeps every share.

## Reference

### Launcher options

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

The API sends one of these commands, which arrive as `SSH_ORIGINAL_COMMAND`: none (launch the job on stdin), `status`, `fetch <id>`, `catalog`, `help <id>`, `answer <id>`, `message <id>`, `upload <id>`, `stop <id>`, `domains <id>`, `allow <id> <n>` or `deny <id> <n>`. Anything else is refused.

Grants use `dejima --domains FILE` and `--dependencies FILE`, and output collection uses `--shared-path-file FILE`. A task without grants passes neither grant flag.

### API config

[`examples/api.toml`](examples/api.toml), with:

| key | meaning |
|---|---|
| `listen` | `host:port` to listen on |
| `db_path` | SQLite database of open tasks and their events; see "Nothing about a session outlives it" |
| `blurbs_dir` | context blurbs, as `<name>.md`; read per request, so an edit needs no restart |
| `max_upload` | largest upload in bytes, default 50 MiB; keep it at or under the launcher's `--max-upload` |
| `waiting_after` | seconds a running session's screen can stay unchanged before the bot says it seems to be waiting (default 300) |
| `output_retention` | seconds an ended session's output is kept for a frontend that hasn't collected it (default 604800, 7 days; 0 deletes it when the session ends) |
| `max_session_age` | seconds a session may run before it's stopped, so none is left running forgotten (default 86400, a day; 0 never stops one). A task the launcher no longer knows about is marked ended at the same age. A session stuck starting counts too, and one whose output can never be fetched is ended at that age, with the reason posted |
| `waiting_screen` | whether that notice includes the end of the session's screen (default true) |
| `configs_dir` | config sets, by default `configs` beside `blurbs_dir`; one file per set named as it's used, holding keyword lines (see [`examples/configs`](examples/configs)); read per request. Comment lines at a set's top are its description, shown wherever the set is described. A set's `file: <name>` lines give each of its sessions `configs_dir/files/<name>` (up to 1 MiB) in `~/shared/uploads` |
| `default_blurbs` | blurb names added to every task's prompt, before any it names with `ctx:`; each must exist |
| `results_dir` | collected output archives, until their frontend collects them |
| `poll_interval` | seconds between checks on the dejima host |
| `launch_command` | how to reach the launcher; the command is appended, and over ssh arrives as `SSH_ORIGINAL_COMMAND` |
| `launch_timeout` | seconds a launcher call may take |
| `output_instructions` | replaces the prompt's last section |
| `remote_access_url` | dejima's web remote access `base_url`, to link each session from chat; unset means no links |
| `session_wait` | with `remote_access_url`, seconds a launch waits for dejima to register the session (default 20) |
| `[tokens]` | one bearer token per frontend, by name. Each must be at least 32 characters, differ from the others and from `examples/api.toml`'s placeholder, or the API won't start; `openssl rand -hex 32` makes one. A token can do everything its frontend can, so see [SECURITY.md](SECURITY.md) before adding one |
| `[policy]` | applied at launch and to every domain request; a misspelled key stops the API. `deny_domains`: refused, with their subdomains and parents. `allow_domains`: if set, only these and their subdomains. `max_domains`: also caps what a session gains through requests. `allow_internal` (default true): false refuses internal grants. `max_internal_domains`. `allow_dependency_kinds` (default `apt`, `cargo`, `npm`, `pip`): `tarball` is left out unless listed, since it has the dejima host fetch any URL on its own network |
| `[limits]` | `max_bytes`, `max_files`: should match the launcher's `--max-output` |
| `[catalog.HARNESS.BACKEND]` | `models`: the models to offer for that harness and backend, each an id or `{ id = "...", context_window = N }`, which fills in the context window when it's picked. Frontends offer them, and still take a model that isn't listed. A backend the harness can't use isn't offered, nor a context window if it takes none |

### API endpoints

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

It returns `201 {"id", "target"}`. A repeat of the same `source` returns the existing task with `200` while it's kept. A failed launch is deleted, so repeating it retries. Errors are `{"error": "..."}`.

Every `poll_interval` seconds the API asks the dejima host for the status of launched tasks, collects output that is ready, and records events. A client sees only the events of tasks it created. Every `requester` is a label for who acted, up to 255 characters with no control or invisible characters; the audit log records it with the frontend's token name. `GET /v1/tasks` reuses a launcher status up to 5 seconds old, so open frontends don't each reach the dejima host:

| endpoint | |
|---|---|
| `GET /v1/events` | unacknowledged events, oldest first: `{seq, id, type, summary, size, answerable, target, source}`, `type` is `output`, `help`, `domains`, `waiting`, `expired` or `ended`; `data` holds a domain request, a waiting session's `quiet` seconds, or a stopped session's `age` |
| `GET /v1/tasks/<id>/outputs/<seq>.zip` | an `output` event's archive (`size` 0 means it was rejected, and `summary` says why) |
| `GET /v1/tasks/<id>/events` | a task's events so far, collected or not, in the same form, without acknowledging any; gone once the task is purged |
| `POST /v1/events/<seq>/ack` | mark an event delivered |
| `POST /v1/tasks/preview` | a `POST /v1/tasks` body, whose `task` may be empty: what it would launch, without launching it. `settings` are the agent settings it asks for, `from_configs` those its config sets gave, then `domains`, `internal_domains`, `dependencies`, `files` (names only) and `blurbs` (`{name, text}`). Refused as the task would be. Defaults are the catalog's |
| `GET /v1/blurbs` | names of the available context blurbs |
| `GET /v1/configs` | names of the available config sets |
| `POST /v1/selftest` | `{requester, source}`: a finished task whose output, help request and waiting screen are hostile text, delivered through `/v1/events` like any task's; see [Checking a frontend](#checking-a-frontend) |
| `GET /v1/blurbs/{name}` | a context blurb's text |
| `GET /v1/configs/{name}` | what a config set applies, by keyword; `file` lists names only, never contents |
| `GET /v1/catalog` | the dejima host's harnesses and backends, the default harness and each harness's defaults (dejima's own, from `dejima --print-config`, with the launcher's options in their place), and under `options`, what each harness can be set to: its backends, effort levels, whether it takes a context window, and `[catalog]`'s models for each backend |
| `POST /v1/tasks/<id>/domains/<n>/allow` | `{requester}`: grant domain request `n` as it was shown, minus what policy refuses |
| `POST /v1/tasks/<id>/domains/<n>/deny` | `{requester}`: refuse domain request `n` |
| `GET /v1/tasks` | the client's open sessions, with what each is waiting on: `{id, target, link, requester, started, state, answerable, waiting: {answer, domains}}` |
| `GET /v1/tasks/<id>` | one open session, in the same form |
| `POST /v1/tasks/<id>/message` | `{text, requester}`: type a message into the session; 409 with the reason if the host refuses |
| `POST /v1/tasks/<id>/files?name=&requester=` | the request body is a file for the session's `~/shared/uploads`; 413 over `max_upload` |
| `POST /v1/tasks/<id>/stop` | `{requester}`: end the task's session and close its tmux window; 409 if it isn't running |
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

What the API image and CI install is pinned, with hashes, in `requirements/api.txt` and `requirements/test.txt`. To change a dependency, edit the matching `.in` file, and `pyproject.toml`'s extra if it's there, then regenerate the `.txt` with the `pip-compile` command at the top of the `.in`.

[`.gitea/workflows/tests.yml`](.gitea/workflows/tests.yml) runs the tests on every pull request on a `dejima-confined` runner, and builds the API's image on a `dejima-privileged` one. What the jobs reach is declared in [`.dejima/allowed-domains`](.dejima/allowed-domains), which the runners' egress allowlist reads from `main`.

### Releasing

Pull requests target `development`, which merges to `main` when it's ready to release. `development` carries the next version: the first change after a release bumps both `plugin/maubot.yaml` and `pyproject.toml`, and opens its entry in [`CHANGELOG.md`](CHANGELOG.md). Each release names the dejima release it was released against, so it trails that dejima release. [docs/release.md](docs/release.md) is the release process.

[`.gitea/workflows/release.yml`](.gitea/workflows/release.yml) publishes the plugin on a `dejima-confined` runner:

- **A push to `development`** creates a pre-release of `<version>.dev<run>`, such as `0.1.18.dev42`, with the plugin attached. Pre-releases aren't the forge's latest release, and maubot orders them before `<version>`, so a stable release supersedes one. To try a development build, download its `.mbp` from the releases page and upload it in maubot's web UI under **Plugins**.
- **A push to `main`** whose version has no `v<version>` tag yet creates the stable release, and with it the tag at that commit. Its notes are the version's `CHANGELOG.md` entry, which must name a dejima release. A push that doesn't change the version releases nothing.

A pushed tag releases nothing; only these two branches do, so the forge must protect both (see [docs/release.md](docs/release.md)). The workflow fails if the two versions disagree. It uses the job's automatic `GITEA_TOKEN`, so Actions must be enabled for the repository, and the token must be allowed to create releases.

## Checking a frontend

The API passes on what the agent wrote with control and invisible characters removed, and nothing more: making it safe to show is each frontend's job. `POST /v1/selftest`, or `@bot selftest` in Matrix, sends text written like a hostile agent's ([`darner/selftest.py`](darner/selftest.py)) as a finished task's output, help request and waiting screen. It arrives through `/v1/events` like any task's, so it goes down the same paths an agent's text does.

Each check says what you see if the frontend is safe (PASS) and what you see if it isn't (FAIL), then gives a test line on its own: raw HTML, scripts and HTML links must show as characters; markdown and Slack links and markdown images must show where they really point, not just "click here", and an address too long for the screen must wrap rather than be cut off; `@room` and the like, in Matrix or Slack syntax, must notify nobody; and the last check must arrive, so a cut-off message shows.

For the Matrix bot, `assert_shown_safely` in `tests/test_bot.py` checks those lines on each message it sends for the self-test, and is shown to fail on unescaped HTML, plain markdown and missing `m.mentions`. A new frontend should get a test like it.

## Security

[SECURITY.md](SECURITY.md) says what darner claims, what it holds that's worth taking, and how to report a vulnerability.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

The dejima wordmark and logo are not code and are not covered by that licence. They are trademarks; who holds them and what may be done with them is stated once for the organisation, at [dejima's ATTRIBUTION.md](https://gitbox.branderson.io/dejima/dejima/src/branch/main/ATTRIBUTION.md).
