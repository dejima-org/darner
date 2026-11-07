# Setting up darner

[← darner README](../README.md)

darner has three parts, which can share a machine or not:

- **The dejima host** runs dejima and `darner-launch`. The API reaches it over ssh with a key that can run nothing but the launcher.
- **The API host** runs `darner-api`, the only part frontends talk to.
- **Frontends:** the [Matrix bot](matrix.md), [darner-web](web.md), or both. Each has a token of its own.

This page sets up the first two, then points you to a frontend.

You need:

- dejima, working on the dejima host, at a release with every flag and tool the launcher uses; [release.md](release.md) lists them, and `CHANGELOG.md` names the release each version was released against
- tmux 3.2 or newer, pipx, git and Python 3.11 or newer on the dejima host
- git on the API host, plus either Python 3.11 or newer (to run the API under systemd) or Docker with Compose

## Values you'll need

The commands below take your values from shell variables, so they can be pasted as they are. Each step starts by setting the variables it uses, on the machine it runs on. Replace the example values there with yours:

| variable | what it is | where the value comes from |
|---|---|---|
| `DEJIMA_HOST` | the dejima host's hostname or IP, as the API host reaches it over ssh | your network; test with `ssh $DEJIMA_HOST` from the API host |
| `DEJIMA_USER` | the account on the dejima host that runs dejima; the API logs in as it | `whoami` on the dejima host, as the user you run dejima as |
| `API_ADDR` | the address the API host's connections arrive from, as the dejima host sees them. Under Docker, that's the Docker host's address | `ip -brief addr` on the API host. For the address the dejima host actually sees, run `echo $SSH_CLIENT` in an ssh session from the API host, or check the `from=` on a key that machine already uses, such as dejima-guard's |
| `API_PUBKEY` | the API's ssh public key | printed in step 3 |
| `FRONTEND` | your first frontend's token name: `maubot` or `web` | your choice |
| `API_TOKEN` | the secret that frontend uses to authenticate to the API | generated in step 5 |

These set where darner comes from and where it is installed. The defaults work as they are:

| variable | default | what goes there |
|---|---|---|
| `DARNER_REPO` | `https://github.com/dejima-org/darner.git` | where darner is cloned from |
| `LAUNCHER_SRC` | `~/src/darner` | the launcher's clone, on the dejima host |
| `DARNER_DIR` | `/opt/darner` | on the API host: the clone and virtualenv under systemd, or the whole deployment directory under Docker Compose |
| `DARNER_CONFIG` | `/etc/darner` | systemd only: `api.toml` and the blurbs |
| `DARNER_STATE` | `/var/lib/darner` | systemd only: the database, results and ssh key; also the service user's home |

Under systemd, the service user `darner` must be able to read `DARNER_DIR` and `DARNER_CONFIG` and write `DARNER_STATE`. A directory inside a private home directory won't work.

## 1. Install the launcher on the dejima host

On the dejima host, as `DEJIMA_USER`:

```sh
DARNER_REPO=https://github.com/dejima-org/darner.git
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

## 2. Install the API

The API runs either as a systemd service or in Docker Compose. Steps 2, 3 and 5 give both; pick one and follow it throughout. All of it runs on the API host.

**With systemd:** create a service user and install into a virtualenv:

```sh
DARNER_REPO=https://github.com/dejima-org/darner.git
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
DARNER_REPO=https://github.com/dejima-org/darner.git
DARNER_DIR=/opt/darner

sudo install -d -o "$USER" "$DARNER_DIR" && cd "$DARNER_DIR"
git clone "$DARNER_REPO" src
cp src/examples/compose.yaml .
sudo install -d -m 700 -o 10001 -g 10001 data
sudo install -d -m 750 -g 10001 config config/blurbs
sudo docker compose build
```

## 3. Give the API an ssh key and the dejima host's host key

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

## 4. Lock the key to the launcher on the dejima host

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
- One API per `DEJIMA_USER`. An API deletes the task directories of ended sessions it doesn't know, so two APIs sharing a user would delete each other's.
- The launcher asks `dejima --print-config` for dejima's defaults, as this user. Check that `darner-launch catalog` run as this user shows non-empty `defaults`; if it can't read dejima's config, the API's log says so.
- `LAUNCHER_OPTIONS` holds the defaults every session gets. Add `--identity darner` if you set one up in step 1, and `--answers` if frontends may type into sessions. Sessions use dejima's own default harness and its settings unless a task or an option such as `--harness` says otherwise, and frontends show those defaults. All options are listed under [Launcher options](reference.md#launcher-options).
- To change the options later, delete the `darner-api` line and run this step again with new `LAUNCHER_OPTIONS`. Don't edit it in place: an option outside the quotes makes sshd skip the whole line, which shows up as `Permission denied (publickey)`.

## 5. Configure and start the API

On the API host, set the values the config needs, and generate your first frontend's token:

```sh
DEJIMA_HOST=dejima.example.com
DEJIMA_USER=operator
DARNER_DIR=/opt/darner
DARNER_CONFIG=/etc/darner       # systemd
DARNER_STATE=/var/lib/darner    # systemd
FRONTEND=maubot                 # or web
API_TOKEN=$(openssl rand -hex 32)
echo "$API_TOKEN"     # keep this for the frontend
```

**With systemd:** install the example config, readable only by root and the service, and fill in your values and directories:

```sh
sudo install -d -m 750 -g darner "$DARNER_CONFIG" "$DARNER_CONFIG/blurbs"
sudo install -m 640 -g darner "$DARNER_DIR/src/examples/api.toml" "$DARNER_CONFIG/api.toml"
sudo sed -i -e "s/operator@dejima.example.com/$DEJIMA_USER@$DEJIMA_HOST/" \
  -e "s/^maubot = \"replace-with-a-long-random-token\"/$FRONTEND = \"$API_TOKEN\"/" \
  -e "s|/var/lib/darner|$DARNER_STATE|g" -e "s|/etc/darner|$DARNER_CONFIG|g" \
  "$DARNER_CONFIG/api.toml"
```

`listen` stays on `127.0.0.1:8080`, which is right if the frontend runs on the same machine. Otherwise, edit it to a private address, or put a TLS reverse proxy in front: the token travels in every request.

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

**With Docker Compose:** the config directory is mounted at `/etc/darner` in the container, so the example's paths work unchanged. `listen` becomes `0.0.0.0:8080`, which is inside the container. `compose.yaml` decides what reaches it: by default, only `127.0.0.1:8080` on the host. If a frontend runs in Docker too, drop the published port and put both on a shared network.

```sh
cd "$DARNER_DIR"
sudo install -m 640 -g 10001 src/examples/api.toml config/api.toml
sudo sed -i -e "s/operator@dejima.example.com/$DEJIMA_USER@$DEJIMA_HOST/" \
  -e "s/^maubot = \"replace-with-a-long-random-token\"/$FRONTEND = \"$API_TOKEN\"/" \
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

To link sessions from a frontend, set `remote_access_url` to the `base_url` of dejima's web remote access, if you use it. Frontends then link each session. Opening the link is protected however your remote access is, such as by the reverse proxy in front of it. The link only works if dejima registers sessions in `/run/user/<uid>`, which needs `loginctl enable-linger` from step 1.

Context blurbs go in the blurbs directory as `<name>.md`: `$DARNER_CONFIG/blurbs`, or `$DARNER_DIR/config/blurbs` under Docker. [`examples/blurbs`](../examples/blurbs) has one. Every config key is described under [API config](reference.md#api-config).

## 6. Add a frontend

Give it `API_TOKEN` from step 5, and each later frontend a token of its own under `[tokens]`:

- **[The Matrix bot](matrix.md#setting-it-up)**, a maubot plugin: tasks from a room, by replying to a note.
- **[darner-web](web.md#setting-it-up)**, a browser frontend, behind a proxy you configure.

Then start a task from it. `tmux attach -t darner` on the dejima host shows the session, and when the agent finishes, its summary and zip arrive in the frontend.

## Updating

Update in this order, because each part may rely on commands or fields added to the one after it: the dejima host, then the API, then the frontends. Look at `CHANGELOG.md` first for anything that needs a step of its own.

**Dejima host**, as `DEJIMA_USER`. The install is editable, so pulling is the update:

```sh
LAUNCHER_SRC=~/src/darner
git -C "$LAUNCHER_SRC" pull
```

Sessions already running are unaffected.

**API host.** Settings added by an update have defaults, so an `api.toml` written for an earlier version keeps working. At startup the API logs every setting it uses, marking the defaults, and warns about any key it doesn't recognise, such as a misspelling. Check that log after updating, and compare `api.toml` with [`examples/api.toml`](../examples/api.toml) for new settings worth setting. Back up the database while the service is stopped.

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

**Frontends:** [the Matrix bot](matrix.md#updating) and [darner-web](web.md#updating).

Dejima itself updates independently, but the launcher depends on the flags [release.md](release.md) lists.
