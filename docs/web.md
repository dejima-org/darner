# darner-web

[← darner README](../README.md)

A browser frontend for darner: everything the Matrix bot does, from a browser, and more. It shows this frontend's sessions at a glance, each one's events so far, the full text of blurbs and config sets, and the self-test. It can start tasks with every keyword the bot takes, showing before Start what the task will launch: its config sets combined, its agent settings and where each comes from, the defaults included, and each blurb in full, send a session a message or answer its help request, upload a file into its `~/shared/uploads`, allow or deny its domain requests, stop it, and clear it once it has ended. It's optional. darner runs without it, and it runs as its own container with its own API token.

**It has no login of its own.** Like dejima's [remote access](https://gitbox.branderson.io/dejima/dejima/src/branch/main/docs/remote-access.md), whatever proxy you put in front of it is the whole gate. Don't open it to the public internet.

## What reaching it means

Anyone who gets past your proxy can do everything this frontend's API token can:

- start tasks, with any config set, and so read any config set's files, such as an API token in `configs_dir/files`: start a task with that set, approve the domain it asks for, and have the agent send the file out
- type into this frontend's sessions, upload files into them, and approve their domain requests
- read their output and download their zips

There's no allowlist in the app. Your proxy's authorization is the allowlist.

## Its own origin

darner-web must have its own scheme, host and port, never a path on the host that serves dejima's remote access. ttyd's origin check only compares `Origin` with `Host`, so any script running on a shared origin could open ttyd's WebSocket and get a tmux client, which is a host shell. One cross-site scripting bug in darner-web would become that. Give it its own hostname, and set `public_origin` to exactly what browsers use. Requests for any other host are refused, which also stops DNS rebinding.

## Binding

- darner-web listens on loopback by default. Its container listens on `0.0.0.0:8081` inside itself, and the compose example publishes that as `127.0.0.1:8081:8081`. A bare `8081:8081` publishes on every interface, the Docker bridges included.
- Your proxy must bind one specific address on the LAN interface: never a bare `listen 443`, never `0.0.0.0`, never a Docker bridge. `ip -br addr show type bridge` lists the addresses to avoid.
- **Check the bind on the host,** which needs no session. `ss -ltnp` should show darner-web on `127.0.0.1:8081` only, and your proxy on the one LAN address you chose, never `*` or `0.0.0.0`. This is the direct check, and the one that matters most.
- **On the dejima host**, a sandbox can route to the host's bridge address, so a front door bound there opens inward. [`tests/containment.sh`](../tests/containment.sh) checks it from inside a session, but only when the session lets the bind decide. dejima's egress clamp refuses every gateway port unless the session is launched with `EGRESS_HOST_PORTS` naming it, so in an ordinary session every probe fails whatever the bind. Run that way, the check proves only the clamp, which dejima's docs call the second layer.

  To check the bind itself, use two terminals on the host. In the first, start something that does answer on the gateway, for the check's control. It's on every interface while it runs, so it serves an empty directory:

  ```sh
  python3 -m http.server 9999 --bind 0.0.0.0 --directory "$(mktemp -d)"
  ```

  In the second, open a shell in a session that can reach the control and the checked ports on the gateway. Run it from a scratch directory holding only a copy of the script, not from the launcher's clone, since that's the code `darner-launch` runs on the host and a session's project is mounted writable:

  ```sh
  d=$(mktemp -d) && cp ~/src/darner/tests/containment.sh "$d"/ && cd "$d"
  EGRESS_HOST_PORTS=9999,8081,443 dejima --cmd bash .
  # Inside the session:
  bash containment.sh --gateway-control 9999 8081 443
  ```

  As soon as the check has run, stop the control with Ctrl-C in the first terminal, so it isn't left serving on the LAN. Then leave the session and delete the scratch directory with `rm -r "$d"`.

  The control answering proves the session reaches the gateway on opened ports, so 8081 and 443 not answering is down to their bind. `BAD` on any line means stop and fix the bind first. `--control <host:port>`, somewhere outside the session may reach, is the fallback when you can't run a listener. It only rules out a dead network, and the script says so. Running darner-web on the API host instead of the dejima host avoids the question.

## Authenticating and authorizing the front door

The proxy must authenticate **every request**, and it must **authorize**: let in only specific users or a group, not everyone it can authenticate. The shapes that work, as for dejima's remote access:

- **A network the internet can't reach:** Tailscale, WireGuard or another VPN. It composes with everything below, and doesn't replace the bind address.
- **HTTP Basic:** one shared or per-user credential in an `htpasswd` file. [`examples/nginx-darner-web.conf`](../examples/nginx-darner-web.conf) shows it. TLS isn't optional, since the password goes with every request.
- **Client certificates:** per-device, revocable, nothing reusable sent over the wire.
- **Forward auth:** Authentik, Authelia or oauth2-proxy, through nginx `auth_request` or the provider's own outpost. **Authentik lets every user in its directory into an application unless a policy is bound to it.** Bind one that allows only the users or group who should use darner.

None of these stops a browser that has already signed in, and the next section is what darner-web does about that.

### Who's using it

Set `user_header` to the header your proxy fills in with the signed-in user, such as `X-authentik-username`, or `X-Remote-User` with the nginx example. darner-web uses it only to label actions in the API's audit log. It never grants anything. The proxy must set it on every request, replacing anything a client sent. Once `user_header` is set, a request without it is refused, which catches a proxy that isn't setting it, and so wouldn't be stripping a forged one either.

The nginx example uses `Host $http_host`, not `$host`, because darner-web checks the port too.

## What it defends against itself

- **Cross-site requests:** every request that changes anything, every action above included, must carry the `X-Darner-Request` header, which a cross-site page can't send without a preflight darner-web refuses. `Origin` must match `public_origin`, and `Sec-Fetch-Site` must be same-origin where the browser sends it. No GET changes anything. The page's forms post, so a form sent without its script is refused for the missing header, rather than putting what was typed in a URL.
- **Hostile agent text:** agent markdown is rendered on the server with raw HTML escaped. Every link shows where it goes, and only `http`, `https` and `mailto` links are clickable, with `rel="noopener noreferrer"`. Images are never loaded: their address is shown. The page's script inserts no HTML at all. Every response sends a CSP with no inline script or style and `require-trusted-types-for 'script'`, plus `Referrer-Policy: no-referrer`, `Cache-Control: no-store` and `nosniff`. Zips are only ever downloaded as attachments.

## Sessions it started

darner-web sees and acts on only the sessions it started, as each frontend does. A session started from Matrix is followed up in Matrix.

A session's page leads with what it's waiting on you for, a domain request or a help request, or else its newest event, with the answer box below it, then what it was started with. Its history follows, newest first, each event folded until you open it.

An ended session stays on its page, showing its output and zips, until someone presses **Clear**. That acknowledges its last events, and the API deletes it. Uncleared output is deleted after the API's `output_retention`, 7 days by default. Files are uploaded from a running session's page, not when starting a task, since a session's `~/shared` doesn't exist until it's running.

## What it keeps

Nothing. It has no database and writes no files: every page is read from the API when it's asked for. The API keeps an ended session's output for this frontend until it's cleared, or for `output_retention` at most, and deletes everything else about a session when it ends.

## Setting it up

1. Generate a token for it on the API host with `openssl rand -hex 32`, and add it to `api.toml`'s `[tokens]` as `web = "..."`. Restart the API.
2. Copy [`examples/web.toml`](../examples/web.toml) beside `api.toml` and fill it in. Under Docker Compose, `api_url` is `http://api:8080`, and `api.toml`'s `listen` must be `0.0.0.0:8080`.
3. Start it: `docker compose --profile web up -d` with [`examples/compose.yaml`](../examples/compose.yaml), or `darner-web --config /etc/darner/web.toml` from an install with the `web` extra.
4. Put your proxy in front, as above, and run the verifying commands if it's on the dejima host.
5. Open **Self-test** and run it. Each check in the session it opens says what you should and shouldn't see.

## Checking it

`tests/test_web.py` covers every page, each refusal and header, autoescaping and the renderer's rules, and runs the self-test's checks over the session page's HTML. `tests/test_web_browser.py` loads that page in Chromium and asserts that the self-test's text opens no dialog, loads nothing from its addresses and breaks no CSP rule, and that a long agent-chosen address or requested domain wraps inside its pane at a phone's width. It also proves the check can fail, against a page that doesn't escape. It needs Node with Playwright and its Chromium, so it's skipped in CI and is on the release checklist. `tests/test_containment.py` checks `tests/containment.sh` itself, against listeners standing in for the gateway: that it catches a port that answers, that a gateway control that doesn't answer fails the check, and that an outside control is reported as proving only the clamp.
