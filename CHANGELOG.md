# Changelog

What each release changed, newest first, for one question: should I update, and what will I be getting. The pull requests carry the detail.

Each release names the dejima release it was released against: the one it was tested with, which has everything it needs from dejima. It's N/A until that dejima release exists, and a release can't be published while it is. See [docs/release.md](docs/release.md).

## 0.1.18

Released against dejima: N/A

- **Sessions stop after a day:** a session that's still running after `max_session_age` (a day by default) is stopped, and the bot says so, so none is left running forgotten. A task id that isn't one, such as `stop ab-12345`, gets a reply instead of starting a new session. ([#50](https://gitbox.branderson.io/dejima/darner/pulls/50))
- **Weak API tokens refused:** the API won't start if a token in `[tokens]` is shorter than 32 characters, is `examples/api.toml`'s placeholder, or is shared by two frontends. If yours is refused, generate a new one with `openssl rand -hex 32` and set it in both `api.toml` and the bot's config. ([#46](https://gitbox.branderson.io/dejima/darner/pulls/46))
- **Frontend self-test:** `@bot selftest`, or `POST /v1/selftest`, sends text written like a hostile agent's through the paths an agent's text takes, with each line saying what a safe frontend shows. ([#47](https://gitbox.branderson.io/dejima/darner/pulls/47))
- **Looking things up from chat:** `list configs` and `list blurbs` name the config sets and context blurbs, and `describe <names>` shows what each config set applies and what each blurb says. ([#43](https://gitbox.branderson.io/dejima/darner/pulls/43))
- **Waiting notices:** the bot says when a session's screen has stopped changing, with the end of its screen, so a question asked at the agent's own prompt isn't missed. ([#36](https://gitbox.branderson.io/dejima/darner/pulls/36))
- **Settings defaults:** every optional API setting has a default, and the API logs the settings it uses and warns about unknown keys. `configs_dir` defaults to `configs` beside `blurbs_dir`. ([#36](https://gitbox.branderson.io/dejima/darner/pulls/36))
- **Config sets:** `config: <name>` applies a named set of keyword lines, which can also give each session files such as an API token. ([#34](https://gitbox.branderson.io/dejima/darner/pulls/34), [#35](https://gitbox.branderson.io/dejima/darner/pulls/35))
- **Internal domains at launch and ports:** `internal:` grants internal domains, domains can carry a port, and help lists the harnesses and backends. ([#33](https://gitbox.branderson.io/dejima/darner/pulls/33))
- **Pre-releases:** every merge to `development` publishes a pre-release of the plugin. ([#32](https://gitbox.branderson.io/dejima/darner/pulls/32))
- **Licence:** GPL-3.0-or-later. ([#37](https://gitbox.branderson.io/dejima/darner/pulls/37))
