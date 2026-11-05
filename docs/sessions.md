# A session, from start to end

[← darner README](../README.md)

What a running session can ask of you, what you can do to it, and what happens to what it leaves. Each frontend shows these its own way: see [matrix.md](matrix.md) and [web.md](web.md). A frontend sees and acts on only the sessions it started.

## While it runs

**Help requests.** The agent writes `~/shared/HELP.md`, read as a regular file only, at most 16 KiB, and the frontend shows it. Answer it from the frontend if the launcher has `--answers`; otherwise attach to the session. An answer is accepted only for an outstanding help request on a running session, and only once: concurrent answers race for it and only one wins.

**Domain requests.** When the agent needs domains the sandbox blocks, it lists them in `~/shared/DOMAINS`, and the frontend shows every one for you to allow or deny. A line can name a port, as in `internal recipes.example.com:8443`. The grant covers the domain on every port, and the port is the one dejima-allow checks it answers on. Internal domains are marked as such, and any the API's `deny_domains` policy refuses are marked and never allowed. An agent that edits `DOMAINS` without signalling again is still asking: the changed list becomes a request once it has been left alone for 30 seconds. After you decide a request, a follow-up, such as asking again for a domain as internal, comes straight through. What gets allowed is the list as shown: the launcher keeps its own copy, so the agent can't change it afterwards. If answering is on, the agent is told the outcome. Otherwise, tell it in the session.

**Waiting.** If a session's screen stops changing for `waiting_after` seconds (5 minutes by default), the frontend says it seems to be waiting, with the end of its screen marked as agent-written. That catches an agent that asked a question at its prompt, or left its answer there without signalling, whatever the harness, and dejima stopping at a prompt of its own before the session starts. It isn't reported for a session waiting on a help or domain request, one that has a result to deliver, one that has delivered output and been told nothing since, or one that has asked since its screen last changed. Each quiet spell is reported once. `waiting_screen = false` leaves out the screen.

**Messages and uploads.** With `--answers`, a message is typed into the session as the agent's next one. It isn't kept, by the API or a frontend. A file is written into the session's `~/shared/uploads`.

**Stopping.** The launcher stops the session's sandbox container, so dejima exits and cleans up as usual, then closes its tmux window. A session stuck starting can be stopped too. The API stops any session older than `max_session_age`, a day by default.

## Typing into a session

Off by default. Turn it on with `--answers` on the launcher, so the dejima host decides whether frontends can type into its sessions. Text goes to the window by its tmux id, so renaming the window inside the session doesn't redirect it. If typing fails, the frontend sees only "couldn't type into the session", with no tmux detail.

Before pasting, the launcher strips control characters, ESC above all, so the text can't end the paste early and have the rest read as keystrokes. It then pastes the text as written, so one starting with `/` or `!` reaches Claude Code as a slash or shell command, run inside the session's container. The paste is bracketed, so newlines don't submit it early, and Enter follows after `--paste-delay`. cline's question prompt drops pastes, so when the screen shows one, the text is typed as keys instead, as one line, which answers in your words rather than picking an option.

## Output

The agent puts its output in `~/shared`, a summary in `RESULT.md`, and signals with `.done`. The frontend shows the summary and offers the rest as a zip.

The session is presumed hostile, and so is everything it writes to `~/shared`. Collecting output never follows a link, never opens anything but a regular file, and copies only file contents into a zip that darner builds itself:

- **Walk:** by file descriptor, never by path. Each entry is opened with `O_PATH`, so no device driver is ever opened, and its type is checked on that handle. Only a regular file is reopened, through `/proc/self/fd`. Swapping in a symlink mid-walk therefore gains nothing.
- **Skipped:** symlinks, FIFOs, devices, and names outside `[A-Za-z0-9][A-Za-z0-9._ +-]*`. That covers dotfiles, `..`, backslashes, control and bidi characters, and invalid UTF-8. Also skipped: Windows reserved names, and names that differ only in case. `MANIFEST.txt` in the zip lists what was included and what was skipped, and why.
- **Limits:** bytes actually read (50 MiB by default), files included, entries visited, depth, path length and time taken. Going over one rejects the output and tells the requester why.
- **Forgotten signal:** an agent can forget `.done`. A new `RESULT.md` is collected anyway once nothing directly in `~/shared` has changed for `--quiet-period`, or as soon as the session ends. The frontend says so. Each delivery records the `RESULT.md` it carried, so the same one isn't sent twice.
- **Signal:** a `.done` is consumed once. The launcher records which one it consumed, so a session that makes `.done` impossible to delete can't trigger repeat deliveries. A new `.done` signals new output, at most once every `--done-interval`. The API keeps a session's latest five zips; an older output keeps its summary.
- **Entries:** mode `0644`, a fixed timestamp, and no symlink or directory entries.
- **Re-check:** the API verifies the archive again and rebuilds it with its own writer before storing it, so what it serves is exactly what it checked.
- **Showing it:** the API passes on what the agent wrote with control and invisible characters removed, and nothing more. Each frontend renders it inert: raw HTML escaped, every link and image shown with its destination, and nothing that notifies anyone. [Checking a frontend](development.md#checking-a-frontend) tests that.

Uploads write into `~/shared` the same careful way. The `uploads` directory is opened without following a link, and each file is created new with `O_EXCL|O_NOFOLLOW`, so a link or file the session planted is never followed or overwritten. Names are reduced to safe characters, and size is capped. The output zip leaves out the top-level `uploads` directory, since the requester already has those files, and its manifest says so.

The files are still agent-written: treat what is in the zip as untrusted.

## What's kept

**Nothing about a session outlives it.** When a session ends, the API deletes everything it kept about it except output its frontend hasn't collected: the prompt and context, who asked, help requests, terminal screens and domain requests. The launcher deletes the session's task directory on the dejima host. Once the frontend acknowledges the output, the API deletes the rest, zips included. The bot does so as soon as it has posted it; darner-web when you clear the session. A session whose `~/shared` was kept waits to be cleared, below. Output nobody collects is deleted after `output_retention`. Your frontend's own copy, such as the room's messages, is yours.

The session's `~/shared` on the dejima host, which dejima keeps if it isn't empty, is pruned with `dejima-share prune --isolated --yes` when the session ends, but only once its output has been delivered whole. All of these must hold:

- the API stored the zip from its latest output, and it's the zip the launcher last sent: the API names it by hash, and the launcher compares
- that delivery left nothing out but `uploads/`
- the share still holds exactly what the delivery carried

Otherwise it's kept: a session that was stopped or ended without output, output that was rejected or couldn't be read, files changed after the delivery, or a delivery that left out symlinks or unreadable files. A kept share holds what darner couldn't deliver, so it's left on the dejima host for you to recover until the session is cleared, from its frontend, or `output_retention` passes. Then the launcher prunes it, whatever it holds, and the API deletes the session. The API's log says why each was kept. A frontend says which happened: darner-web on the ended session's page, and the bot when a share was kept. `--keep-shared` on the launcher keeps every share, even past a clear.

darner prunes only the share dejima recorded for a session darner launched, by name, never by sweeping the share root, so a session you start on the dejima host yourself keeps its share for you to remove. A kept share whose session the API no longer knows, which no frontend can clear, is named in the API's log; remove it on the dejima host with `darner-launch discard <id>`.

The API's audit log, `darner.audit`, records who did what to each session, through which frontend, so that can be answered after the session's own data is deleted. It records a message's length, never its text.
