# Releasing: development to main

Work lands on `development`, and every merge there publishes a pre-release. A release moves `development` to `main`, which publishes the stable plugin that users install. This is what that move consists of.

## 1. It trails a dejima release

darner runs dejima on the dejima host and relies on its flags and tools: `--isolated`, `--shared-path-file`, `--internal-domain`, `--model`, `dejima-allow --port` and others. A darner release is released against one dejima release, the tag on dejima's `main` that it was tested with and that has everything it needs. `CHANGELOG.md` names it under the version's heading:

```
## 0.1.18

Released against dejima: 2026.10.14
```

So a darner release trails its dejima release. If it needs a dejima change that isn't in a dejima release yet, it waits for one. Until then the line says `N/A`. Pre-releases from `development` may need dejima's `development`, and say so in their pull requests.

The release workflow refuses a stable release whose `CHANGELOG.md` entry is missing, has no `Released against dejima:` line, or says `N/A`. It doesn't check that the tag exists on dejima's `main`, so check that yourself.

## 2. What is required

The same bar as dejima's, for the same reason: one maintainer, so a gate has to be one that gets met.

1. **An agentic security review of the whole `main...development` diff**, against the boundaries in [`.dejima.md`](../.dejima.md). Brief the reviewer with those boundaries specifically. A review briefed only as "look for security problems" returns generic advice. Also ask whether `.dejima/allowed-domains` gains a name, since CI reads it from `main` as already reviewed.
2. **The maintainer runs the build** against the dejima release it names: a task from chat on each configured harness, through to its output arriving in the room.

## 3. The suites

`pytest` runs everywhere. The API's image is built only by CI's privileged job, so read the release pull request's checks: green means it built. Yellow or grey means that job hasn't run, and the release pull request says so.

## 4. The release pull request

Say what was run and what wasn't. Also name:

- **Settings added**, in `api.toml`, the bot's config or the launcher's options. A rollback to the previous release warns about an `api.toml` key it doesn't know rather than refusing it, but an unknown launcher option stops the forced command.
- **What an existing setup stops being able to do**, such as a launcher option or an endpoint the new version refuses.

## 5. The version and the changelog

Before opening the release pull request, on `development`:

- Make sure `CHANGELOG.md` has an entry for the version in `plugin/maubot.yaml` and `pyproject.toml`, and set its `Released against dejima:` line.
- Entries name what a user would notice, not what changed, and link the pull requests.

Merging to `main` publishes the release and creates the `v<version>` tag, with the changelog entry as the release notes. Nothing else needs tagging. The first change on `development` after that bumps the version in both files and opens the next entry, with `Released against dejima: N/A`.
