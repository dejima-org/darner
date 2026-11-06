# Releasing: development to main

Work lands on `development`, and every merge there publishes a pre-release. A release moves `development` to `main`, which publishes the stable plugin that users install. This is what that move consists of.

## 1. It ships with a dejima release

darner runs dejima on the dejima host and relies on these of its flags, tools and files:

- `dejima`: `--isolated`, `--no-pause`, `--shared-path-file`, `--keep-records`, `--identity`, `--harness`, `--backend`, `--model`, `--effort`, `--context-window`, `--domains`, `--internal-domain`, `--dependencies`, `--drop-domains`, `--require-sealed` and `--print-config`
- `--print-config`'s `section.key=value` lines, for `core.harness` and `harness.<name>.{backend,model,effort,context_window}`
- `dejima-allow`: `--container`, `--internal` and `--port`, and exit status 2 meaning granted but not answering on 443
- `dejima-share`: `prune --isolated [share]` and `--yes`, as its `--help` lists them
- the manifests, `harnesses/*/harness.conf` (`api_styles`, and `keys` with `effort:?enum:...` and `context_window`) and `backends/*/backend.conf` (`api_style`), found beside the directory `dejima` resolves to from `bin/`
- `$XDG_RUNTIME_DIR/dejima/sessions/<run id>` with its `pane=` line, and the `SANDBOX_ISOLATED_WORK=dejima_isolated-work-<run id>` volume name

darner releases in lockstep with dejima: its release pull request is prepared alongside a dejima release's, and both merge the same day, as [dejima's release process](https://gitbox.branderson.io/dejima/dejima/src/branch/main/docs/release.md) describes. `CHANGELOG.md` names that dejima release, the one it was tested with, under the version's heading:

```
## 0.1.18

Released against dejima: 2026.10.14
```

dejima's releases are named for the day they merge, so the line can be written before dejima's tag exists. A darner release that needs something not in that dejima release waits for the next one. Between releases the line says `N/A`. Pre-releases from `development` may need dejima's `development`, and say so in their pull requests.

The release workflow refuses a stable release whose `CHANGELOG.md` entry is missing, has no `Released against dejima:` line, or says `N/A`. It doesn't check that the tag exists on dejima's `main`, so check it once dejima's release has merged.

## 2. What the forge must protect

The release workflow publishes from exactly two places: a push to `main` makes a stable release, and a push to `development` a pre-release. Both attach the plugin users install. A pushed tag publishes nothing. So whoever can push to either branch can ship a plugin, and pushing a branch is something a dejima session can do.

- **Protect `main` and `development`** in the repository's branch settings: no direct pushes, changes only by merging a pull request. dejima's push guard blocks `main` from inside a session, but not `development`, and it's no substitute for the forge's own rule.
- **Protect `v*` tags.** Tags don't trigger a release, but `v<version>` is how the workflow decides a version is already released, and a forged one would block the real release.
- **Check the job's token** can create releases but can't push to either branch.

Pre-releases are published on this forge only. Before darner is mirrored to GitHub, stop them reaching it: the mirror publishes `main` and the tags on it, and a pre-release's `v<version>.dev<run>` tag is on `main` once `development` merges. Either exclude `.dev` tags from the mirror, or have pre-releases stop creating tags.

## 3. What is required

The same bar as dejima's, for the same reason: one maintainer, so a gate has to be one that gets met.

1. **An agentic security review of the whole `main...development` diff**, against the boundaries in [`.dejima.md`](../.dejima.md). Brief the reviewer with those boundaries specifically. A review briefed only as "look for security problems" returns generic advice. Also ask whether `.dejima/allowed-domains` gains a name, since CI reads it from `main` as already reviewed.
2. **The maintainer runs the build** against the dejima release it names: a task from each frontend you run, on each configured harness, through to its output arriving there.

## 4. The suites

`pytest` runs everywhere. Its darner-web browser test, `tests/test_web_browser.py`, needs Node with Playwright and its Chromium, so CI skips it: run it from a dejima sandbox, where they're installed, and say in the release pull request that it passed. If darner-web or its proxy runs on a dejima host, also check its bind there with `ss -ltnp` and `tests/containment.sh --gateway-control`, from a session launched with `EGRESS_HOST_PORTS` in a scratch directory holding only the script, as `docs/web.md` describes, and say so. The API's and darner-web's images are built only by CI's privileged job, so read the release pull request's checks: green means it built. Yellow or grey means that job hasn't run, and the release pull request says so.

## 5. The release pull request

Say what was run and what wasn't. Also name:

- **Settings added**, in `api.toml`, the bot's config or the launcher's options. A rollback to the previous release warns about an `api.toml` key it doesn't know rather than refusing it, but an unknown launcher option stops the forced command.
- **What an existing setup stops being able to do**, such as a launcher option or an endpoint the new version refuses.

## 6. The version and the changelog

Before opening the release pull request, on `development`:

- Make sure `CHANGELOG.md` has an entry for the version in `plugin/maubot.yaml` and `pyproject.toml`, and set its `Released against dejima:` line.
- Entries name what a user would notice, not what changed, and link the pull requests.

Merging to `main` publishes the release and creates the `v<version>` tag, with the changelog entry as the release notes. Nothing else needs tagging. The first change on `development` after that bumps the version in both files and opens the next entry, with `Released against dejima: N/A`.
