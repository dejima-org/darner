"""Start dejima sessions and collect their output.

Meant to be the forced command of the API's key in authorized_keys, so the
only command a client can send is SSH_ORIGINAL_COMMAND, and only three forms
of it are accepted: none (launch the job on stdin), `status`, `fetch <id>`,
`help <id>` and `answer <id>`.

The dejima argv is written to the task directory and the tmux window re-invokes
this module with --exec to run it, since tmux limits the size of a command
sent to its server and the prompt is passed as a single argument.
"""

import argparse
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from meeseeks import archive
from meeseeks.job import JobError, valid_name, valid_task_id, validate_job

FETCH_REJECTED = 3
RUN_ID = re.compile(r"[0-9a-f]{16}")
MAX_HELP = 16 * 1024
SIGNALS = {"done": (".done", "consumed"), "help": (".help", "consumed-help")}


def _backend_default(value: str) -> tuple[str, str]:
    harness, sep, backend = value.partition("=")
    if not sep or not valid_name(harness) or not valid_name(backend):
        raise argparse.ArgumentTypeError(f"expected HARNESS=BACKEND, got {value!r}")
    return harness, backend


def _name(value: str) -> str:
    if not valid_name(value):
        raise argparse.ArgumentTypeError(f"not a valid name: {value!r}")
    return value


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="meeseeks-launch")
    p.add_argument("--session", default="meeseeks", type=_name)
    p.add_argument("--tasks-dir", default=Path.home() / "meeseeks" / "tasks", type=Path)
    p.add_argument("--dejima", default="dejima")
    p.add_argument("--tmux", default="tmux")
    p.add_argument("--identity", type=_name)
    p.add_argument("--harness", type=_name)
    p.add_argument("--backend", type=_backend_default, action="append", default=[])
    p.add_argument("--share-root", default=Path.home() / ".local/share/dejima/shared/isolated", type=Path)
    p.add_argument("--max-output", default=archive.Limits.max_bytes, type=int)
    p.add_argument("--fetch-timeout", default=60, type=int)
    p.add_argument("--answers", action="store_true", help="allow answering help requests from chat")
    p.add_argument("--paste-delay", default=0.5, type=float)
    p.add_argument("--help-interval", default=300, type=int, help="least seconds between a task's help requests")
    p.add_argument(
        "--quiet-period", default=600, type=int,
        help="seconds without changes after which an unsignalled RESULT.md is collected; 0 waits for the session to end",
    )
    p.add_argument("--exec", dest="exec_dir", type=Path, help=argparse.SUPPRESS)
    p.add_argument("command", nargs="*", help=argparse.SUPPRESS)
    return p.parse_args(argv)


def dejima_argv(args: argparse.Namespace, job, task_dir: Path, dejima: str) -> list[str]:
    harness = job.harness or args.harness
    backend = job.backend or dict(args.backend).get(harness)
    argv = [dejima, "--isolated", "--no-pause", "--shared-path-file", str(task_dir / "shared-path")]
    for flag, value in (("--identity", args.identity), ("--harness", harness), ("--backend", backend)):
        if value:
            argv += [flag, value]
    if job.domains:
        argv += ["--domains", str(task_dir / "domains")]
    if job.dependencies:
        argv += ["--dependencies", str(task_dir / "dependencies")]
    return argv + ["--", job.prompt]


def launch(args: argparse.Namespace, data: object) -> dict:
    job = validate_job(data)
    # Resolved here, where a failure reaches the requester. Under an ssh forced
    # command PATH is minimal, and a dejima that doesn't start would otherwise
    # only close its tmux window.
    dejima = shutil.which(args.dejima)
    if dejima is None:
        raise RuntimeError(f"{args.dejima} not found on PATH ({os.environ.get('PATH', '')}); pass --dejima with its full path")
    task_dir = args.tasks_dir / job.id
    args.tasks_dir.mkdir(parents=True, exist_ok=True)
    task_dir.mkdir(mode=0o700)
    (task_dir / "domains").write_text("".join(f"{d}\n" for d in job.domains))
    (task_dir / "dependencies").write_text("".join(f"{d}\n" for d in job.dependencies))
    (task_dir / "argv.json").write_text(json.dumps(dejima_argv(args, job, task_dir, dejima)))

    env = _tmux_env()
    tmux = [args.tmux]
    if subprocess.run([*tmux, "has-session", "-t", f"={args.session}"], env=env, capture_output=True).returncode:
        created = subprocess.run([*tmux, "new-session", "-d", "-s", args.session], env=env, capture_output=True)
        # Lost a race with a concurrent launch, which created it first.
        if created.returncode and subprocess.run(
            [*tmux, "has-session", "-t", f"={args.session}"], env=env, capture_output=True
        ).returncode:
            raise RuntimeError(created.stderr.decode().strip() or "tmux new-session failed")
    window = subprocess.run(
        [
            *tmux, "new-window", "-d", "-P", "-F", "#{session_name}:#{window_name} #{window_id} #{pane_id}",
            "-t", f"={args.session}:", "-n", job.id, "-c", str(task_dir),
            "-e", f"XDG_RUNTIME_DIR={env['XDG_RUNTIME_DIR']}",
            "--", sys.executable, "-m", "meeseeks.launch", "--exec", str(task_dir),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    if window.returncode:
        raise RuntimeError(window.stderr.strip() or "tmux new-window failed")
    target, window_id, pane_id = window.stdout.strip().rsplit(" ", 2)
    # Answers target the window by id, which a rename inside the session can't change.
    (task_dir / "window").write_text(window_id)
    (task_dir / "pane").write_text(pane_id)
    result = {"id": job.id, "target": target}
    deadline = time.monotonic() + job.session_wait
    while job.session_wait:
        if run_id := session_run_id(task_dir):
            result["run_id"] = run_id
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    return result


def session_run_id(task_dir: Path) -> str | None:
    """dejima's run id for the session in this task's pane, once dejima has registered it.

    dejima records each session's tmux pane in its remote-access registry, so
    the pane meeseeks created is what identifies the session there. Nothing
    is registered unless dejima's remote access is enabled.
    """
    try:
        pane = (task_dir / "pane").read_text()
    except FileNotFoundError:
        return None
    sessions = Path(_tmux_env()["XDG_RUNTIME_DIR"]) / "dejima" / "sessions"
    for entry in sessions.glob("*") if sessions.is_dir() else ():
        if not RUN_ID.fullmatch(entry.name):
            continue
        try:
            if f"pane={pane}" in entry.read_text().splitlines():
                return entry.name
        except OSError:
            continue
    return None


def _tmux_env() -> dict:
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def run_session(task_dir: Path) -> int:
    """Run dejima and record how it exited, so `status` can report it."""
    # Handlers, unlike SIG_IGN, are reset by exec, so dejima still gets these
    # signals as usual while this process stays to record the exit.
    for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM, signal.SIGQUIT):
        signal.signal(sig, lambda *_: None)
    dejima = json.loads((task_dir / "argv.json").read_text())
    try:
        code = subprocess.Popen(dejima).wait()
    except OSError as e:
        print(f"meeseeks: couldn't start {dejima[0]}: {e}", file=sys.stderr)
        code = 127
    if code < 0:
        code = 128 - code
    tmp = task_dir / "exit.tmp"
    tmp.write_text(f"{code}\n")
    os.replace(tmp, task_dir / "exit")
    if code:
        # The window closes when this process exits, taking the error with it.
        print(f"\nmeeseeks: dejima exited with status {code}. Press Enter to close this window.")
        try:
            input()
        except EOFError:
            pass
    return code


def open_share(args: argparse.Namespace, task_dir: Path) -> int:
    """Open a task's ~/shared, which must be a directory directly under the share root."""
    raw = (task_dir / "shared-path").read_text()
    path = raw.removesuffix("\n")
    parent, leaf = os.path.split(path)
    if not os.path.isabs(path) or "\0" in path or not archive.safe_component(leaf):
        raise ValueError(f"invalid shared path {path!r}")
    root = os.path.realpath(args.share_root)
    if os.path.realpath(parent) != root:
        raise ValueError(f"shared path {path!r} is not under {root}")
    parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        return os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _signal(fd: int, task_dir: Path, kind: str) -> str | None:
    """Identify an unconsumed signal file (.done or .help), if there is one.

    Consumption is recorded host-side rather than relying on removing the file,
    which the session can make impossible. A new file is a new inode or ctime.
    """
    name, record = SIGNALS[kind]
    try:
        st = os.lstat(name, dir_fd=fd)
    except OSError:
        return None
    if stat.S_ISDIR(st.st_mode):
        return None
    signal_id = f"{st.st_dev} {st.st_ino} {st.st_ctime_ns}"
    consumed = task_dir / record
    if consumed.exists() and consumed.read_text() == signal_id:
        return None
    return signal_id


def _consume(fd: int, task_dir: Path, kind: str, signal_id: str) -> None:
    name, record = SIGNALS[kind]
    tmp = task_dir / f"{record}.tmp"
    tmp.write_text(signal_id)
    os.replace(tmp, task_dir / record)
    try:
        os.unlink(name, dir_fd=fd)
    except OSError:
        pass


def _help_allowed(args: argparse.Namespace, task_dir: Path) -> bool:
    """Rate-limit help requests, so a session can't flood the room with them."""
    last = task_dir / SIGNALS["help"][1]
    return not last.exists() or time.time() - last.stat().st_mtime >= args.help_interval


def _result_id(fd: int) -> str | None:
    try:
        st = os.lstat("RESULT.md", dir_fd=fd)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return f"{st.st_dev} {st.st_ino} {st.st_mtime_ns} {st.st_size}"


def _quiet_for(fd: int) -> float:
    """Seconds since anything directly in ~/shared last changed."""
    newest = os.fstat(fd).st_mtime
    with os.scandir(fd) as entries:
        for i, entry in enumerate(entries):
            if i >= archive.Limits.max_entries:
                break
            try:
                newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
            except OSError:
                continue
    return time.time() - newest


def _unsignalled(args: argparse.Namespace, fd: int, task_dir: Path) -> str | None:
    """A RESULT.md the agent never signalled with .done, if it's time to collect it anyway.

    That is once nothing in ~/shared has changed for the quiet period, or as
    soon as the session has ended. Returns the RESULT.md's identity.
    """
    result = _result_id(fd)
    delivered = task_dir / "delivered-result"
    if result is None or (delivered.exists() and delivered.read_text() == result):
        return None
    if _state(task_dir) == "exited":
        return result
    if args.quiet_period and _quiet_for(fd) >= args.quiet_period:
        return result
    return None


def _signals(args: argparse.Namespace, task_dir: Path) -> dict[str, bool]:
    try:
        fd = open_share(args, task_dir)
    except (OSError, ValueError):
        return {"done": False, "help": False, "unsignalled": False}
    try:
        signals = {kind: _signal(fd, task_dir, kind) is not None for kind in SIGNALS}
        signals["unsignalled"] = not signals["done"] and _unsignalled(args, fd, task_dir) is not None
    finally:
        os.close(fd)
    signals["help"] = signals["help"] and _help_allowed(args, task_dir)
    return signals


def _state(task_dir: Path) -> str:
    if (task_dir / "exit").exists():
        return "exited"
    if (task_dir / "shared-path").exists():
        return "running"
    return "starting"


def status(args: argparse.Namespace) -> list[dict]:
    tasks = []
    if not args.tasks_dir.is_dir():
        return tasks
    for task_dir in sorted(args.tasks_dir.iterdir()):
        if not valid_task_id(task_dir.name):
            continue
        state = _state(task_dir)
        signals = _signals(args, task_dir)
        tasks.append({
            "id": task_dir.name,
            "state": state,
            "ready": signals["done"],
            "unsignalled": signals["unsignalled"],
            "help": signals["help"],
            "answerable": args.answers and state == "running",
            "run_id": session_run_id(task_dir) if state == "running" else None,
        })
    return tasks


class _Timeout(Exception):
    pass


def _timeout(*_):
    raise _Timeout


def fetch(args: argparse.Namespace, task_id: str) -> int:
    task_dir = args.tasks_dir / task_id
    fd = open_share(args, task_dir)
    try:
        signal_id = _signal(fd, task_dir, "done")
        result = _result_id(fd)
        if signal_id is None and _unsignalled(args, fd, task_dir) is None:
            raise RuntimeError(f"{task_id} has no output ready")
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(args.fetch_timeout)
        try:
            data, _ = archive.build(fd, archive.Limits(max_bytes=args.max_output))
        except (archive.ArchiveError, _Timeout) as e:
            # Retrying would fail the same way, so the output is consumed.
            _delivered(fd, task_dir, signal_id, result)
            reason = e if isinstance(e, archive.ArchiveError) else f"took longer than {args.fetch_timeout}s to read"
            print(f"meeseeks-launch: {reason}", file=sys.stderr)
            return FETCH_REJECTED
        finally:
            signal.alarm(0)
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        _delivered(fd, task_dir, signal_id, result)
    finally:
        os.close(fd)
    return 0


def _delivered(fd: int, task_dir: Path, signal_id: str | None, result: str | None) -> None:
    """Record a delivery: the .done it answered, and the RESULT.md it carried."""
    if signal_id:
        _consume(fd, task_dir, "done", signal_id)
    if result:
        tmp = task_dir / "delivered-result.tmp"
        tmp.write_text(result)
        os.replace(tmp, task_dir / "delivered-result")


def help_request(args: argparse.Namespace, task_id: str) -> None:
    """Print a task's HELP.md and mark its help request as awaiting an answer."""
    task_dir = args.tasks_dir / task_id
    fd = open_share(args, task_dir)
    try:
        signal_id = _signal(fd, task_dir, "help")
        if signal_id is None or not _help_allowed(args, task_dir):
            raise RuntimeError(f"{task_id} has no help request")
        text = archive.read_file(fd, "HELP.md", MAX_HELP) or b""
        sys.stdout.buffer.write(text)
        sys.stdout.buffer.flush()
        (task_dir / "help-pending").touch()
        _consume(fd, task_dir, "help", signal_id)
    finally:
        os.close(fd)


def answer(args: argparse.Namespace, task_id: str, data: object) -> None:
    """Type a requester's answer into the session, as its next message."""
    task_dir = args.tasks_dir / task_id
    if not args.answers:
        raise RuntimeError("answering from chat is not enabled on this host")
    if _state(task_dir) != "running":
        raise RuntimeError(f"{task_id} is not running")
    if not isinstance(data, dict) or not isinstance(data.get("text"), str) or not isinstance(data.get("requester"), str):
        raise ValueError("answer must be an object with text and requester")
    if len(data["text"].encode()) > MAX_HELP:
        raise ValueError(f"the answer is over {MAX_HELP // 1024} KiB")
    # Stripping control characters, ESC above all, keeps the text from ending
    # the bracketed paste early and having the rest read as keystrokes.
    text = archive.clean_text(data["text"].encode(), MAX_HELP)
    requester = " ".join(archive.clean_text(data["requester"].encode(), 255).split())
    if not text:
        raise ValueError("the answer is empty")

    # Claimed by rename, so of two concurrent answers only one goes through.
    pending, answering = task_dir / "help-pending", task_dir / "help-answering"
    try:
        os.rename(pending, answering)
    except FileNotFoundError:
        raise RuntimeError(f"{task_id} has no help request awaiting an answer")
    # The prefix also means the message never starts with a character the
    # harness treats as a command, such as Claude Code's ! (shell) or / (slash).
    try:
        _paste(args, task_dir, task_id, f"Reply from {requester}:\n\n{text}")
    except BaseException:
        os.rename(answering, pending)
        raise
    answering.unlink()


def _paste(args: argparse.Namespace, task_dir: Path, task_id: str, message: str) -> None:
    env = _tmux_env()
    window = task_dir / "window"
    target = window.read_text() if window.exists() else f"={args.session}:={task_id}"
    buffer = f"meeseeks-{task_id}-{os.getpid()}"
    for argv, stdin in (
        ([args.tmux, "load-buffer", "-b", buffer, "-"], message),
        ([args.tmux, "paste-buffer", "-p", "-d", "-b", buffer, "-t", target], None),
    ):
        # tmux's own error names sockets and paths, which shouldn't reach the chat.
        if subprocess.run(argv, input=stdin, env=env, capture_output=True, text=True).returncode:
            raise RuntimeError("couldn't type into the session")
    # The TUI needs a moment to take the paste before Enter submits it.
    time.sleep(args.paste_delay)
    if subprocess.run([args.tmux, "send-keys", "-t", target, "Enter"], env=env, capture_output=True).returncode:
        raise RuntimeError("couldn't type into the session")


def command(args: argparse.Namespace) -> list[str]:
    original = os.environ.get("SSH_ORIGINAL_COMMAND")
    words = original.split() if original is not None else args.command
    if words in ([], ["status"]) or (
        len(words) == 2 and words[0] in ("fetch", "help", "answer") and valid_task_id(words[1])
    ):
        return words
    raise ValueError("unknown command")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.exec_dir:
        return run_session(args.exec_dir)
    try:
        words = command(args)
    except ValueError as e:
        print(f"meeseeks-launch: {e}", file=sys.stderr)
        return 2
    try:
        if words == ["status"]:
            print(json.dumps(status(args)))
        elif words and words[0] == "fetch":
            return fetch(args, words[1])
        elif words and words[0] == "help":
            help_request(args, words[1])
        elif words:
            answer(args, words[1], json.load(sys.stdin))
        else:
            print(json.dumps(launch(args, json.load(sys.stdin))))
    except (JobError, json.JSONDecodeError, RuntimeError, OSError, ValueError) as e:
        print(f"meeseeks-launch: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
