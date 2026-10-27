# Changelog

What each release changed, newest first, for one question: should I update, and what will I be getting. The pull requests carry the detail.

Each release names the dejima release it was released against: the one it was tested with, which has everything it needs from dejima. It's N/A until that dejima release exists, and a release can't be published while it is. See [docs/release.md](docs/release.md).

## 0.1.18

Released against dejima: N/A

- **Waiting notices:** the bot says when a session's screen has stopped changing, with the end of its screen, so a question asked at the agent's own prompt isn't missed. ([#36](https://gitbox.branderson.io/dejima/meeseeks/pulls/36))
- **Settings defaults:** every optional API setting has a default, and the API logs the settings it uses and warns about unknown keys. `configs_dir` defaults to `configs` beside `blurbs_dir`. ([#36](https://gitbox.branderson.io/dejima/meeseeks/pulls/36))
- **Config sets:** `config: <name>` applies a named set of keyword lines, which can also give each session files such as an API token. ([#34](https://gitbox.branderson.io/dejima/meeseeks/pulls/34), [#35](https://gitbox.branderson.io/dejima/meeseeks/pulls/35))
- **Internal domains at launch and ports:** `internal:` grants internal domains, domains can carry a port, and help lists the harnesses and backends. ([#33](https://gitbox.branderson.io/dejima/meeseeks/pulls/33))
- **Pre-releases:** every merge to `development` publishes a pre-release of the plugin. ([#32](https://gitbox.branderson.io/dejima/meeseeks/pulls/32))
- **Licence:** GPL-3.0-or-later. ([#37](https://gitbox.branderson.io/dejima/meeseeks/pulls/37))
