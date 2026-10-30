# darner-web

[← darner README](../README.md)

A browser frontend for darner: this frontend's sessions at a glance, each one's events so far, the full text of blurbs and config sets, and the self-test. It's optional. darner runs without it, and it runs as its own container with its own API token.

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
- **On the dejima host**, a sandbox can route to the host's bridge address, so a front door bound there opens inward. Verify it from inside a running dejima session with [`tests/containment.sh`](../tests/containment.sh). It checks that neither darner-web's port nor your proxy's answers on the gateway. Its first argument is a positive control: somewhere the session may reach, such as its model's API, so "nothing answered" can't come from a dead network.

  ```sh
  bash containment.sh api.anthropic.com:443 8081 443
  ```

  `BAD` on any line means stop and fix the bind first. Running darner-web on the API host instead of the dejima host avoids the question.

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

- **Cross-site requests:** every request that changes anything must carry the `X-Darner-Request` header, which a cross-site page can't send without a preflight darner-web refuses. `Origin` must match `public_origin`, and `Sec-Fetch-Site` must be same-origin where the browser sends it. No GET changes anything.
- **Hostile agent text:** agent markdown is rendered on the server with raw HTML escaped. Every link shows where it goes, and only `http`, `https` and `mailto` links are clickable, with `rel="noopener noreferrer"`. Images are never loaded: their address is shown. The page's script inserts no HTML at all. Every response sends a CSP with no inline script or style and `require-trusted-types-for 'script'`, plus `Referrer-Policy: no-referrer`, `Cache-Control: no-store` and `nosniff`. Zips are only ever downloaded as attachments.

## What it keeps

Nothing. It has no database and writes no files: every page is read from the API when it's asked for. The API keeps an ended session's output for this frontend until it's cleared, or for `output_retention` at most, and deletes everything else about a session when it ends.

## Setting it up

1. Generate a token for it on the API host with `openssl rand -hex 32`, and add it to `api.toml`'s `[tokens]` as `web = "..."`. Restart the API.
2. Copy [`examples/web.toml`](../examples/web.toml) beside `api.toml` and fill it in. Under Docker Compose, `api_url` is `http://api:8080`, and `api.toml`'s `listen` must be `0.0.0.0:8080`.
3. Start it: `docker compose --profile web up -d` with [`examples/compose.yaml`](../examples/compose.yaml), or `darner-web --config /etc/darner/web.toml` from an install with the `web` extra.
4. Put your proxy in front, as above, and run the verifying commands if it's on the dejima host.
5. Open **Self-test** and run it. Each check in the session it opens says what you should and shouldn't see.

## Checking it

`tests/test_web.py` covers every page, each refusal and header, autoescaping and the renderer's rules, and runs the self-test's checks over the session page's HTML. `tests/test_web_browser.py` loads that page in Chromium and asserts that the self-test's text opens no dialog, loads nothing from its addresses and breaks no CSP rule. It also proves the check can fail, against a page that doesn't escape. It needs Node with Playwright and its Chromium, so it's skipped in CI and is on the release checklist. `tests/test_containment.py` checks `tests/containment.sh` itself, against listeners standing in for the gateway, including that a failed positive control fails the check.
