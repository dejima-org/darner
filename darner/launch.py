# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
"""Start dejima sessions and collect their output.

Meant to be the forced command of the API's key in authorized_keys, so the
only command a client can send is SSH_ORIGINAL_COMMAND, and only three forms
of it are accepted: none (launch the job on stdin), `status`, `fetch <id>`,
`catalog`, `help <id>`, `answer <id>`, `message <id>`, `upload <id>`, `stop <id>`, `domains <id>`, and `allow <id> <n>` and
`deny <id> <n>` for domain request n.

The dejima argv is written to the task directory and the tmux window re-invokes
this module with --exec to run it, since tmux limits the size of a command
sent to its server and the prompt is passed as a single argument.
"""

import argparse
import contextlib
import fcntl
import hashlib
import threading
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

from darner import archive
from darner.job import (
    JobError, valid_context_window, valid_domain, valid_effort, valid_model, valid_name, valid_task_id, validate_job,
)

FETCH_REJECTED = 3
RUN_ID = re.compile(r"[0-9a-f]{16}")
MAX_HELP = 16 * 1024
SIGNALS = {
    "done": (".done", "consumed"),
    "help": (".help", "consumed-help"),
    "domains": (".domains", "consumed-domains"),
}
MAX_DOMAIN_REQUEST = 20
DOMAINS_QUIET = 30
CLINE_QUESTION = "Cline is asking a question"


def _per_harness(what: str, check):
    def parse(value: str) -> tuple[str, str]:
        harness, sep, setting = value.partition("=")
        if not sep or not valid_name(harness) or not check(setting):
            raise argparse.ArgumentTypeError(f"expected HARNESS={what.upper()}, got {value!r}")
        return harness, setting

    return parse


_backend_default = _per_harness("backend", valid_name)


def _context_window(s: str) -> bool:
    return s.isascii() and s.isdecimal() and valid_context_window(int(s))


def _name(value: str) -> str:
    if not valid_name(value):
        raise argparse.ArgumentTypeError(f"not a valid name: {value!r}")
    return value


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="darner-launch")
    p.add_argument("--session", default="darner", type=_name)
    p.add_argument("--tasks-dir", default=Path.home() / "darner" / "tasks", type=Path)
    p.add_argument("--dejima", default="dejima")
    p.add_argument("--tmux", default="tmux")
    p.add_argument("--identity", type=_name)
    p.add_argument("--harness", type=_name)
    p.add_argument("--backend", type=_backend_default, action="append", default=[])
    p.add_argument("--model", type=_per_harness("model", valid_model), action="append", default=[])
    p.add_argument("--effort", type=_per_harness("effort", valid_effort), action="append", default=[])
    p.add_argument("--context-window", type=_per_harness("context_window", _context_window), action="append", default=[])
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
    p.add_argument("--keep-records", action="store_true", help="pass --keep-records to every dejima session")
    p.add_argument("--max-upload", default=50 * 1024 * 1024, type=int, help="largest file a requester can upload, in bytes")
    p.add_argument("--docker", default="docker")
    p.add_argument("--dejima-allow", default="dejima-allow")
    p.add_argument("--stop-timeout", default=60, type=int, help="seconds to wait for a stopped session to exit")
    p.add_argument("--exec", dest="exec_dir", type=Path, help=argparse.SUPPRESS)
    p.add_argument("command", nargs="*", help=argparse.SUPPRESS)
    return p.parse_args(argv)


def dejima_argv(args: argparse.Namespace, job, task_dir: Path, dejima: str) -> list[str]:
    harness = job.harness or args.harness
    # Each default is the one for the harness actually used, so overriding
    # the harness never pairs it with another harness's setting.
    backend = job.backend or dict(args.backend).get(harness)
    model = job.model or dict(args.model).get(harness)
    effort = job.effort or dict(args.effort).get(harness)
    context_window = job.context_window or dict(args.context_window).get(harness)
    argv = [dejima, "--isolated", "--no-pause", "--shared-path-file", str(task_dir / "shared-path")]
    if args.keep_records:
        argv.append("--keep-records")
    for flag, value in (
        ("--identity", args.identity), ("--harness", harness), ("--backend", backend),
        ("--model", model), ("--effort", effort), ("--context-window", context_window and str(context_window)),
    ):
        if value:
            argv += [flag, value]
    if job.domains:
        argv += ["--domains", str(task_dir / "domains")]
    for name in job.internal_domains:
        argv += ["--internal-domain", name]
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
    try:
        _hold_files(job, task_dir)
        return _start_window(args, job, task_dir)
    except BaseException:
        # No window means no run_session to deliver or remove them.
        shutil.rmtree(task_dir / "files", ignore_errors=True)
        raise


def _hold_files(job, task_dir: Path) -> None:
    """Keep the job's files owner-only until the session's ~/shared exists; see _deliver_files."""
    if not job.files:
        return
    pending = task_dir / "files"
    pending.mkdir(mode=0o700)
    for name, data in job.files:
        with os.fdopen(os.open(pending / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as f:
            f.write(data)


def _start_window(args: argparse.Namespace, job, task_dir: Path) -> dict:
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
            "--", sys.executable, "-m", "darner.launch", "--share-root", str(args.share_root), "--exec", str(task_dir),
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
    the pane darner created is what identifies the session there. Nothing
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


def _deliver_files(args: argparse.Namespace, task_dir: Path, stop: threading.Event) -> None:
    """Write the task's files into ~/shared/uploads as soon as dejima has made it.

    dejima records the share path before the container starts, so they're
    there before the agent is. The task directory's copies are removed as
    each is written.
    """
    pending = task_dir / "files"
    while not (task_dir / "shared-path").exists():
        if stop.wait(0.2):
            return
    try:
        uploads = _open_uploads(args, task_dir)
    except RuntimeError as e:
        print(f"darner: couldn't give the session its files: {e}", file=sys.stderr)
        return
    delivered = []
    try:
        for path in sorted(pending.iterdir()):
            delivered.append(_write_new(uploads, path.name, path.read_bytes(), 0o600))
            _write_atomic(task_dir / "files-delivered", json.dumps(delivered))
            path.unlink()
    finally:
        os.close(uploads)


def _remove_files(args: argparse.Namespace, task_dir: Path) -> None:
    """Take the task's files back out of ~/shared, which dejima keeps after the session if it's not empty."""
    record = task_dir / "files-delivered"
    if record.exists():
        with contextlib.suppress(RuntimeError, OSError, ValueError):
            names = json.loads(record.read_text())
            uploads = _open_uploads(args, task_dir)
            try:
                # Each on its own: a name the session replaced with something
                # unlink refuses mustn't keep the rest in place.
                for name in names:
                    with contextlib.suppress(OSError):
                        os.unlink(name, dir_fd=uploads)
            finally:
                os.close(uploads)
        record.unlink(missing_ok=True)
    shutil.rmtree(task_dir / "files", ignore_errors=True)


def run_session(args: argparse.Namespace, task_dir: Path) -> int:
    """Run dejima and record how it exited, so `status` can report it."""
    # Handlers, unlike SIG_IGN, are reset by exec, so dejima still gets these
    # signals as usual while this process stays to record the exit.
    for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM, signal.SIGQUIT):
        signal.signal(sig, lambda *_: None)
    dejima = json.loads((task_dir / "argv.json").read_text())
    stop = threading.Event()
    delivery = threading.Thread(target=_deliver_files, args=(args, task_dir, stop), daemon=True)
    if (task_dir / "files").is_dir():
        delivery.start()
    try:
        proc = subprocess.Popen(dejima)
        (task_dir / "pid").write_text(str(proc.pid))
        code = proc.wait()
    except OSError as e:
        print(f"darner: couldn't start {dejima[0]}: {e}", file=sys.stderr)
        code = 127
    if code < 0:
        code = 128 - code
    stop.set()
    # Delivery writes only new files, so it can't block; waiting for it
    # means cleanup sees everything it wrote.
    if delivery.is_alive():
        delivery.join()
    _remove_files(args, task_dir)
    tmp = task_dir / "exit.tmp"
    tmp.write_text(f"{code}\n")
    os.replace(tmp, task_dir / "exit")
    # An isolated session keeps no records unless asked to, and the typing
    # log holds screens from it.
    keep_records = "--" in dejima and "--keep-records" in dejima[: dejima.index("--")]
    if not keep_records:
        (task_dir / "typing.log").unlink(missing_ok=True)
    if code:
        # The window closes when this process exits, taking the error with it.
        print(f"\ndarner: dejima exited with status {code}. Press Enter to close this window.")
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


def _request_allowed(args: argparse.Namespace, task_dir: Path, kind: str) -> bool:
    """Rate-limit help and domain requests, so a session can't flood the room with them.

    A domain request is limited only while the last one awaits a decision:
    once it's decided, a new one is how the agent follows up, such as asking
    again for a domain as internal.
    """
    if kind == "domains" and not (task_dir / "domains-pending").exists():
        return True
    last = task_dir / SIGNALS[kind][1]
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


def _signals(args: argparse.Namespace, task_dir: Path) -> tuple[dict[str, bool], str | None]:
    """The task's signals, and why its ~/shared couldn't be read, if it couldn't.

    No shared-path yet just means the session is still starting.
    """
    none = {"done": False, "help": False, "domains": False, "unsignalled": False, "result_pending": False}
    try:
        fd = open_share(args, task_dir)
    except FileNotFoundError as e:
        return none, None if e.filename == str(task_dir / "shared-path") else str(e)
    except (OSError, ValueError) as e:
        return none, str(e)
    try:
        signals = {kind: _signal(fd, task_dir, kind) is not None for kind in SIGNALS}
        signals["domains"] = signals["domains"] or _domains_changed(fd, task_dir)
        signals["unsignalled"] = not signals["done"] and _unsignalled(args, fd, task_dir) is not None
        delivered = task_dir / "delivered-result"
        result = _result_id(fd)
        signals["result_pending"] = result is not None and not (delivered.exists() and delivered.read_text() == result)
    finally:
        os.close(fd)
    signals["help"] = signals["help"] and _request_allowed(args, task_dir, "help")
    signals["domains"] = signals["domains"] and _request_allowed(args, task_dir, "domains")
    return signals, None


def _screen(args: argparse.Namespace, task_dir: Path) -> tuple[float | None, str]:
    """How long the session's screen has gone unchanged, and its last lines.

    Recorded per task, so a session that has stopped at a prompt with no
    one there to answer shows as quiet whatever the harness.
    """
    window = task_dir / "window"
    target = window.read_text() if window.exists() else f"={args.session}:={task_dir.name}"
    try:
        shown = subprocess.run([args.tmux, "capture-pane", "-p", "-t", target], env=_tmux_env(), capture_output=True)
    except OSError:
        return None, ""
    if shown.returncode:
        return None, ""
    digest = hashlib.sha256(shown.stdout).hexdigest()
    record = task_dir / "screen"
    now = time.time()
    try:
        last = json.loads(record.read_text())
    except (OSError, ValueError):
        last = {}
    if last.get("hash") != digest:
        last = {"hash": digest, "since": now}
        _write_atomic(record, json.dumps(last))
    lines = [line.rstrip() for line in archive.clean_text(shown.stdout, 256 * 1024).splitlines() if line.strip()]
    return now - last["since"], "\n".join(lines[-20:])[-3000:]


def _state(task_dir: Path) -> str:
    if (task_dir / "exit").exists():
        return "exited"
    if (task_dir / "shared-path").exists():
        return "running"
    return "starting"


def forget(args: argparse.Namespace, task_id: str) -> None:
    """Delete an ended session's task directory, which holds its prompt in argv.json."""
    task_dir = args.tasks_dir / task_id
    if not task_dir.is_dir():
        return
    if _state(task_dir) != "exited":
        raise RuntimeError(f"{task_id} hasn't ended")
    shutil.rmtree(task_dir)


def status(args: argparse.Namespace) -> list[dict]:
    tasks = []
    if not args.tasks_dir.is_dir():
        return tasks
    for task_dir in sorted(args.tasks_dir.iterdir()):
        if not valid_task_id(task_dir.name):
            continue
        state = _state(task_dir)
        if state == "exited":
            # Held files a session never took, if its launcher was killed before removing them.
            shutil.rmtree(task_dir / "files", ignore_errors=True)
        signals, error = _signals(args, task_dir)
        quiet, screen = _screen(args, task_dir) if state == "running" else (None, "")
        tasks.append({
            "id": task_dir.name,
            "state": state,
            "ready": signals["done"],
            "unsignalled": signals["unsignalled"],
            "help": signals["help"],
            "domains": signals["domains"],
            "awaiting_answer": (task_dir / "help-pending").exists(),
            "result_pending": signals["result_pending"],
            "quiet": quiet,
            "screen": screen,
            "domain_request": _pending_request_number(task_dir),
            "answerable": args.answers and state == "running",
            "run_id": session_run_id(task_dir) if state == "running" else None,
            "error": error,
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
            print(f"darner-launch: {reason}", file=sys.stderr)
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
        if signal_id is None or not _request_allowed(args, task_dir, "help"):
            raise RuntimeError(f"{task_id} has no help request")
        text = archive.read_file(fd, "HELP.md", MAX_HELP) or b""
        sys.stdout.buffer.write(text)
        sys.stdout.buffer.flush()
        (task_dir / "help-pending").touch()
        _consume(fd, task_dir, "help", signal_id)
    finally:
        os.close(fd)


def _pending_request_number(task_dir: Path) -> int | None:
    try:
        return int(json.loads((task_dir / "domains-pending").read_text())["request"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def answer(args: argparse.Namespace, task_id: str, data: object, message: bool = False) -> None:
    """Type a requester's text into the session, as its next message.

    An answer needs an outstanding help request. A message doesn't, but
    answers one if it's outstanding.
    """
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
    if not text:
        raise ValueError("the answer is empty")

    # Claimed by rename, so of two concurrent answers only one goes through.
    pending, answering = task_dir / "help-pending", task_dir / "help-answering"
    try:
        os.rename(pending, answering)
        claimed = True
    except FileNotFoundError:
        if not message:
            raise RuntimeError(f"{task_id} has no help request awaiting an answer")
        claimed = False
    try:
        _paste(args, task_dir, task_id, text)
    except BaseException:
        if claimed:
            os.rename(answering, pending)
        raise
    if claimed:
        answering.unlink()


def _paste(args: argparse.Namespace, task_dir: Path, task_id: str, message: str) -> None:
    env = _tmux_env()
    window = task_dir / "window"
    target = window.read_text() if window.exists() else f"={args.session}:={task_id}"
    buffer = f"darner-{task_id}-{os.getpid()}"
    def screen() -> str:
        result = subprocess.run([args.tmux, "capture-pane", "-p", "-J", "-t", target], env=env, capture_output=True, text=True)
        return result.stdout if result.returncode == 0 else ""

    def enter() -> None:
        if subprocess.run([args.tmux, "send-keys", "-t", target, "Enter"], env=env, capture_output=True).returncode:
            raise RuntimeError("couldn't type into the session")

    shots = {"before": screen()}
    if any(line.strip(" │") == CLINE_QUESTION for line in shots["before"].splitlines()):
        # cline's question drops pastes but takes typed keys. A first printable
        # key switches it to a typed answer, except a digit, which picks that
        # option; the answer is trimmed, so a leading space is safe. Enter
        # would submit, so the answer is one line.
        message = " ".join(message.split())
        keys = " " + message
        steps = [([args.tmux, "send-keys", "-l", "-t", target, keys[i:i + 1024]], None) for i in range(0, len(keys), 1024)]
    else:
        steps = [
            ([args.tmux, "load-buffer", "-b", buffer, "-"], message),
            ([args.tmux, "paste-buffer", "-p", "-d", "-b", buffer, "-t", target], None),
        ]
    for argv, stdin in steps:
        # tmux's own error names sockets and paths, which shouldn't reach the chat.
        if subprocess.run(argv, input=stdin, env=env, capture_output=True, text=True).returncode:
            raise RuntimeError("couldn't type into the session")
    # The TUI needs a moment to take the paste before Enter submits it.
    time.sleep(args.paste_delay)
    shots["after paste"] = screen()
    enter()
    time.sleep(args.paste_delay)
    shots["after enter"] = screen()
    # Submitting redraws the screen. An Enter that changed nothing was eaten
    # by something else, and one more on an empty input box does no harm.
    if shots["after enter"] == shots["after paste"]:
        enter()
        time.sleep(args.paste_delay)
        shots["after second enter"] = screen()
    # Long pastes are shown collapsed, so either the message's start or that
    # marker shows it arrived. A start that's missing is a paste that lost its
    # first keystrokes to whatever had focus.
    arrived = message[:16] in shots["after paste"] or "[Pasted text" in shots["after paste"]
    sent = shots.get("after second enter", shots["after enter"]) != shots["after paste"]
    if not (arrived and sent):
        _write_typing_log(task_dir, shots)
        raise RuntimeError("typed into the session, but couldn't confirm it arrived and was sent; check the session")


def _write_typing_log(task_dir: Path, shots: dict[str, str]) -> None:
    """Keep the screens around a failed typing attempt, to see what had focus.

    They hold whatever the session showed, so the file is the operator's
    alone, and is removed when the session ends unless records are kept.
    """
    tmp = task_dir / "typing.log.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"===== {name}\n{shot}\n" for name, shot in shots.items()))
    os.replace(tmp, task_dir / "typing.log")


def _dejima_pid(task_dir: Path) -> int | None:
    try:
        pid = int((task_dir / "pid").read_text())
    except (OSError, ValueError):
        return None
    return pid if Path(f"/proc/{pid}").exists() else None


def _children(pid: int) -> list[int]:
    found = []
    for stat_file in Path("/proc").glob("[0-9]*/stat"):
        try:
            # The command name is parenthesised and may contain spaces, so
            # the parent pid is counted from the closing parenthesis.
            ppid = int(stat_file.read_text().rpartition(")")[2].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if ppid == pid:
            found.append(int(stat_file.parent.name))
    return found


def _run_id_from_processes(task_dir: Path) -> str | None:
    """dejima's run id, read from the environment it gives its docker compose.

    For when remote access is off and dejima registers nothing: the isolated
    work volume dejima exports for compose is named after the run.
    """
    pending = [pid] if (pid := _dejima_pid(task_dir)) else []
    while pending:
        current = pending.pop()
        pending += _children(current)
        try:
            env = Path(f"/proc/{current}/environ").read_bytes().split(b"\0")
        except OSError:
            continue
        for var in env:
            if m := re.fullmatch(rb"SANDBOX_ISOLATED_WORK=dejima_isolated-work-([0-9a-f]{16})", var):
                return m[1].decode()
    return None


def _wait_for_exit(task_dir: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not (task_dir / "exit").exists():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)
    return True


def _session_containers(args: argparse.Namespace, task_dir: Path) -> list[str]:
    """The task's sandbox container, found by the isolated work volume named after dejima's run id."""
    run_id = session_run_id(task_dir) or _run_id_from_processes(task_dir)
    if not run_id:
        return []
    found = subprocess.run(
        [args.docker, "ps", "-q", "--filter", f"volume=dejima_isolated-work-{run_id}"],
        capture_output=True, text=True,
    )
    return found.stdout.split()


@contextlib.contextmanager
def _domains_lock(task_dir: Path):
    """Serialise writing and deciding domain requests, so a decision sees one request whole."""
    with open(task_dir / "domains.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _domains_file_id(fd: int) -> tuple[str, float] | None:
    try:
        st = os.lstat("DOMAINS", dir_fd=fd)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return f"{st.st_dev} {st.st_ino} {st.st_mtime_ns} {st.st_size}", st.st_mtime


def _domains_changed(fd: int, task_dir: Path) -> bool:
    """Whether DOMAINS changed since the last request and has since been left alone.

    An agent that edits its request without signalling .domains again is
    still asking, so a settled change counts as a request too.
    """
    found = _domains_file_id(fd)
    if found is None:
        return False
    file_id, mtime = found
    recorded = task_dir / "domains-file"
    if recorded.exists() and recorded.read_text() == file_id:
        return False
    return time.time() - mtime >= DOMAINS_QUIET


def domain_request(args: argparse.Namespace, task_id: str) -> dict:
    """Read a task's DOMAINS request, and keep the validated list host-side for approval.

    Approval grants this saved list, never a fresh read of the agent's file,
    so what the requester was shown is exactly what can be granted.
    """
    task_dir = args.tasks_dir / task_id
    fd = open_share(args, task_dir)
    try:
        signal_id = _signal(fd, task_dir, "domains")
        asked = signal_id is not None or _domains_changed(fd, task_dir)
        if not asked or not _request_allowed(args, task_dir, "domains"):
            raise RuntimeError(f"{task_id} has no domain request")
        found = _domains_file_id(fd)
        raw = archive.read_file(fd, "DOMAINS", 8 * 1024) or b""
        domains, rejected = [], 0
        for line in raw.decode(errors="replace").splitlines():
            words = line.split("#", 1)[0].split()
            if not words:
                continue
            internal = len(words) == 2 and words[0].lower() == "internal"
            name, port = _domain_and_port(words[-1].lower())
            entry = {"domain": name, "internal": internal, "port": port}
            if (
                len(words) > (2 if internal else 1) or name is None or not valid_domain(name)
                or len(domains) >= MAX_DOMAIN_REQUEST
            ):
                rejected += 1
            elif entry not in domains:
                domains.append(entry)
        with _domains_lock(task_dir):
            counter = task_dir / "domains-requests"
            number = int(counter.read_text()) + 1 if counter.exists() else 1
            _write_atomic(counter, str(number))
            request = {"request": number, "domains": domains, "rejected": rejected}
            _write_atomic(task_dir / "domains-pending", json.dumps(request))
            if found:
                _write_atomic(task_dir / "domains-file", found[0])
        if signal_id:
            _consume(fd, task_dir, "domains", signal_id)
        return request
    finally:
        os.close(fd)


def _domain_and_port(word: str) -> tuple[str | None, int | None]:
    """Split `domain[:port]`. The port is the one dejima-allow checks the domain answers on."""
    host, sep, port = word.rpartition(":")
    if not sep:
        return word, None
    if port.isascii() and port.isdigit() and 0 < int(port) < 65536:
        return host, int(port)
    return None, None


def _claim_domain_request(task_dir: Path, task_id: str, number: int) -> dict:
    """Take a pending domain request, so it is decided once. Only the latest one counts."""
    pending = task_dir / "domains-pending"
    with _domains_lock(task_dir):
        try:
            request = json.loads(pending.read_text())
        except FileNotFoundError:
            raise RuntimeError(f"{task_id} has no domain request awaiting a decision")
        if request["request"] != number:
            raise RuntimeError(f"domain request {number} was replaced by a newer one")
        pending.unlink()
    return request


def _open_uploads(args: argparse.Namespace, task_dir: Path) -> int:
    """Open the session's ~/shared/uploads, creating it, without following a link."""
    try:
        fd = open_share(args, task_dir)
    except (OSError, ValueError):
        # The reason names host paths, and this error is relayed to the chat.
        raise RuntimeError("couldn't open the session's ~/shared")
    try:
        try:
            os.mkdir(archive.UPLOADS, 0o755, dir_fd=fd)
        except FileExistsError:
            pass
        try:
            return os.open(archive.UPLOADS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        except OSError:
            raise RuntimeError("~/shared/uploads in the session isn't a directory")
    finally:
        os.close(fd)


def _write_new(uploads: int, base: str, data: bytes, mode: int = 0o644) -> str:
    """Create a file in uploads that didn't exist, numbering the name if it's taken. Returns its name."""
    stem, dot, ext = base.partition(".")
    for n in range(1, 101):
        name = base if n == 1 else f"{stem}-{n}{dot}{ext}"
        try:
            out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=uploads)
        except FileExistsError:
            continue
        try:
            with os.fdopen(out, "wb") as f:
                f.write(data)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=uploads)
            raise
        return name
    raise RuntimeError("too many uploads with that name")


def upload(args: argparse.Namespace, task_id: str, stream) -> dict:
    """Write a requester's file into the session's ~/shared/uploads.

    ~/shared is the session's to change, so nothing in it is followed: the
    uploads directory is opened without following a link, and the file is
    created new, never opened if something already has its name.
    """
    task_dir = args.tasks_dir / task_id
    if _state(task_dir) != "running":
        raise RuntimeError(f"{task_id} is not running")
    header = json.loads(stream.readline(4096))
    if not isinstance(header, dict) or not isinstance(header.get("name"), str) or not isinstance(header.get("requester"), str):
        raise ValueError("upload needs a header with name and requester")
    data = stream.read(args.max_upload + 1)
    if len(data) > args.max_upload:
        raise ValueError(f"the file is over the {args.max_upload}-byte upload limit")
    uploads = _open_uploads(args, task_dir)
    try:
        name = _write_new(uploads, archive.upload_name(header["name"]), data)
    finally:
        os.close(uploads)
    path = f"~/shared/uploads/{name}"
    requester = " ".join(archive.clean_text(header["requester"].encode(), 255).split())
    told = _tell_agent(args, task_dir, task_id, f"Upload from {requester}: {path} ({len(data)} bytes).")
    return {"path": path, "size": len(data), "told": told}


def _tell_agent(args: argparse.Namespace, task_dir: Path, task_id: str, message: str) -> bool:
    """Type a note into the session, if answering from chat is enabled. False if it isn't."""
    if not args.answers:
        return False
    try:
        _paste(args, task_dir, task_id, message)
    except RuntimeError:
        return False
    return True


def allow_domains(args: argparse.Namespace, task_id: str, number: int, data: object) -> dict:
    """Grant a domain request to the running session, limited to what the API passes on.

    The API leaves out what its policy refuses, and this grants only domains
    that were in the saved request, so neither side alone can widen it.
    """
    task_dir = args.tasks_dir / task_id
    if _state(task_dir) != "running":
        raise RuntimeError(f"{task_id} is not running")
    if not isinstance(data, dict) or not isinstance(data.get("requester"), str) or not isinstance(data.get("domains"), list):
        raise ValueError("allow needs requester and domains")
    limit = data.get("max_domains")
    if limit is not None and type(limit) is not int:
        raise ValueError("max_domains must be a whole number")
    dejima_allow = shutil.which(args.dejima_allow)
    if dejima_allow is None:
        raise RuntimeError(f"{args.dejima_allow} not found on PATH; pass --dejima-allow with its full path")
    containers = _session_containers(args, task_dir)
    if len(containers) != 1:
        raise RuntimeError(f"couldn't find {task_id}'s container")
    request = _claim_domain_request(task_dir, task_id, number)
    approved = {d for d in data["domains"] if isinstance(d, str)}
    # The session's limit covers every domain it holds: those it launched with
    # and every grant since, so repeated requests can't add up past it.
    held_file = task_dir / "domains-granted"
    held = json.loads(held_file.read_text()) if held_file.exists() else []
    launched_with = sum(1 for line in (task_dir / "domains").read_text().splitlines() if line.strip())
    granted, failed, unverified = [], [], []
    for entry in request["domains"]:
        if entry["domain"] not in approved:
            continue
        if limit is not None and launched_with + len(held) + len(granted) >= limit:
            failed.append({"domain": entry["domain"], "error": f"over this session's limit of {limit} domains"})
            continue
        argv = [
            dejima_allow, "--container", containers[0],
            *(["--internal"] if entry["internal"] else []),
            *(["--port", str(entry["port"])] if entry.get("port") else []),
            entry["domain"],
        ]
        result = subprocess.run(argv, capture_output=True, text=True)
        message = archive.clean_text(result.stderr.encode(), 2000)
        # dejima-allow exits 2 when the permit landed but its check that the
        # domain answers on 443 failed, which a service on another port fails
        # spuriously. The domain is allowed either way.
        if result.returncode in (0, 2):
            granted.append(entry["domain"])
            if result.returncode == 2:
                unverified.append({"domain": entry["domain"], "warning": message})
        else:
            failed.append({"domain": entry["domain"], "error": message})
    _write_atomic(held_file, json.dumps(held + granted))
    requester = " ".join(archive.clean_text(data["requester"].encode(), 255).split())
    note = f"Domain request from {requester}: allowed {', '.join(granted) or 'none'}"
    if failed:
        note += "; couldn't allow " + "; ".join(f"{f['domain']} ({f['error']})" for f in failed)
    if unverified:
        note += "; allowed but not reachable on port 443 when checked: " + "; ".join(
            f"{u['domain']} ({u['warning']})" for u in unverified
        )
    told = _tell_agent(args, task_dir, task_id, note + ". Carry on with your task.")
    return {"granted": granted, "failed": failed, "unverified": unverified, "told": told}


def deny_domains(args: argparse.Namespace, task_id: str, number: int, data: object) -> dict:
    task_dir = args.tasks_dir / task_id
    request = _claim_domain_request(task_dir, task_id, number)
    requester = " ".join(archive.clean_text(str((data or {}).get("requester", "")).encode(), 255).split()) or "the requester"
    names = ", ".join(e["domain"] for e in request["domains"]) or "none"
    told = _state(task_dir) == "running" and _tell_agent(
        args, task_dir, task_id, f"Domain request from {requester}: not allowed ({names}). Carry on without them."
    )
    return {"told": told}


def stop(args: argparse.Namespace, task_id: str) -> None:
    """End a session, then close the tmux window that held it.

    The sandbox container is stopped, not dejima: dejima's cleanup doesn't
    stop the container, so ending dejima first would leave it running. With
    the container gone, dejima exits as usual and cleans up after it.
    """
    task_dir = args.tasks_dir / task_id
    if _state(task_dir) != "running":
        raise RuntimeError(f"{task_id} is not running")
    containers = _session_containers(args, task_dir)
    if containers:
        subprocess.run([args.docker, "stop", *containers], capture_output=True)
    elif pid := _dejima_pid(task_dir):
        # No container yet, so dejima is still starting and has nothing to leave behind.
        os.kill(pid, signal.SIGTERM)
    if not _wait_for_exit(task_dir, args.stop_timeout) and (pid := _dejima_pid(task_dir)):
        os.kill(pid, signal.SIGTERM)
        _wait_for_exit(task_dir, 10)
    window = task_dir / "window"
    target = window.read_text() if window.exists() else f"={args.session}:={task_id}"
    subprocess.run([args.tmux, "kill-window", "-t", target], env=_tmux_env(), capture_output=True)


def catalog(args: argparse.Namespace) -> dict:
    """The harnesses and backends the configured dejima has, and this launcher's defaults.

    dejima keeps each as a directory beside its executable, holding a
    harness.conf or backend.conf.
    """
    dejima = shutil.which(args.dejima)
    if dejima is None:
        raise RuntimeError(f"{args.dejima} not found on PATH; pass --dejima with its full path")
    root = Path(dejima).resolve().parent.parent

    def names(kind: str) -> list[str]:
        directory = root / f"{kind}s"
        if not directory.is_dir():
            return []
        return sorted(p.name for p in directory.iterdir() if valid_name(p.name) and (p / f"{kind}.conf").is_file())

    return {"harnesses": names("harness"), "backends": names("backend"), "default_harness": args.harness}


def command(args: argparse.Namespace) -> list[str]:
    original = os.environ.get("SSH_ORIGINAL_COMMAND")
    words = original.split() if original is not None else args.command
    if words in ([], ["status"], ["catalog"]) or (
        len(words) == 2
        and words[0] in ("fetch", "help", "answer", "message", "upload", "stop", "domains", "forget")
        and valid_task_id(words[1])
    ) or (
        len(words) == 3 and words[0] in ("allow", "deny") and valid_task_id(words[1])
        and words[2].isascii() and words[2].isdigit() and len(words[2]) <= 9
    ):
        return words
    raise ValueError("unknown command")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.exec_dir:
        return run_session(args, args.exec_dir)
    try:
        words = command(args)
    except ValueError as e:
        print(f"darner-launch: {e}", file=sys.stderr)
        return 2
    try:
        if words == ["status"]:
            print(json.dumps(status(args)))
        elif words == ["catalog"]:
            print(json.dumps(catalog(args)))
        elif words and words[0] == "fetch":
            return fetch(args, words[1])
        elif words and words[0] == "help":
            help_request(args, words[1])
        elif words and words[0] == "upload":
            print(json.dumps(upload(args, words[1], sys.stdin.buffer)))
        elif words and words[0] == "message":
            answer(args, words[1], json.load(sys.stdin), message=True)
        elif words and words[0] == "stop":
            stop(args, words[1])
        elif words and words[0] == "forget":
            forget(args, words[1])
        elif words and words[0] == "domains":
            print(json.dumps(domain_request(args, words[1])))
        elif words and words[0] in ("allow", "deny"):
            decide = allow_domains if words[0] == "allow" else deny_domains
            print(json.dumps(decide(args, words[1], int(words[2]), json.load(sys.stdin))))
        elif words and words[0] == "answer":
            answer(args, words[1], json.load(sys.stdin))
        else:
            print(json.dumps(launch(args, json.load(sys.stdin))))
    except (JobError, json.JSONDecodeError, RuntimeError, OSError, ValueError) as e:
        print(f"darner-launch: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
