"""Start a dejima session for a job read from stdin.

Meant to be the forced command of the API's key in authorized_keys. The
dejima argv is written to the task directory and the tmux window re-invokes
this module with --exec to run it, since tmux limits the size of a command
sent to its server and the prompt is passed as a single argument.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from meeseeks.job import JobError, valid_name, validate_job


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
    p.add_argument("--exec", dest="exec_dir", type=Path, help=argparse.SUPPRESS)
    return p.parse_args(argv)


def dejima_argv(args: argparse.Namespace, job, task_dir: Path) -> list[str]:
    harness = job.harness or args.harness
    backend = job.backend or dict(args.backend).get(harness)
    argv = [args.dejima, "--isolated", "--no-pause"]
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
    task_dir = args.tasks_dir / job.id
    args.tasks_dir.mkdir(parents=True, exist_ok=True)
    task_dir.mkdir(mode=0o700)
    (task_dir / "domains").write_text("".join(f"{d}\n" for d in job.domains))
    (task_dir / "dependencies").write_text("".join(f"{d}\n" for d in job.dependencies))
    (task_dir / "argv.json").write_text(json.dumps(dejima_argv(args, job, task_dir)))

    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
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
            *tmux, "new-window", "-d", "-P", "-F", "#{session_name}:#{window_name}",
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
    return {"id": job.id, "target": window.stdout.strip()}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.exec_dir:
        dejima = json.loads((args.exec_dir / "argv.json").read_text())
        os.execvp(dejima[0], dejima)
    try:
        result = launch(args, json.load(sys.stdin))
    except (JobError, json.JSONDecodeError, RuntimeError, OSError) as e:
        print(f"meeseeks-launch: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
