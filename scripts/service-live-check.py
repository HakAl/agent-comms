"""Live check of `agent-comms service` against the real service manager.

Not part of the suite or of `make gate`: it loads a real user service (a
launchd agent in your login session on macOS, a systemd user unit on Linux).
The service belongs to a throwaway runtime root under `local/sl/`
(short on purpose: control sockets under the runtime root must fit the
AF_UNIX path limit; `--scratch DIR` names another place),
so its label carries that root's suffix and cannot collide with an install
you use. Everything it loads or writes is removed at the end, pass or fail.

What it shows, with the checkout's own `.venv`:

1. `service install` from the CLI: the monitor runs under the manager and
   `service status` exits 0 (a fresh heartbeat from the service's own pid).
2. The process got the argv and environment the unit names, read back from
   the operating system, for a runtime root and a PATH entry whose directory
   name holds a space, a percent sign and a dollar sign.
3. A reinstall whose program first starts a detached stand-in for a worker
   supervisor (its own session, one child, no model, no credential) and then
   becomes the monitor. The stand-in is started from inside the service, as a
   real supervisor is.
4. SIGKILL of the monitor: the manager restarts it; the stand-in survives.
5. `service stop`: it stays stopped past the restart delay; the stand-in
   survives.
6. `service start`, then `service install` again: running again each time;
   the stand-in survives both.
7. `service uninstall`: no unit file and no loaded service remain.

Run: make service-live
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HUMAN = "01M36YTJV9XBW95S6ZWV47C4RG"
AWKWARD = "a b%c$d"
HEALTHY_TIMEOUT = 90.0
# Longer than either manager's restart delay (launchd 10 s, systemd 5 s).
STAYS_STOPPED_SECONDS = 14.0

FIXTURE = r'''
import json, os, subprocess, sys, time

marker = sys.argv[1]
if os.fork():
    os._exit(0)
os.setsid()
if os.fork():
    os._exit(0)
devnull = os.open(os.devnull, os.O_RDWR)
for fd in (0, 1, 2):
    os.dup2(devnull, fd)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(900)"])
with open(marker + ".new", "w") as handle:
    json.dump({"supervisor": os.getpid(), "child": child.pid}, handle)
os.replace(marker + ".new", marker)
time.sleep(900)
child.kill()
'''


class CheckFailed(Exception):
    pass


def say(text: str) -> None:
    print(text, flush=True)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform.startswith("linux"):
        try:
            # A zombie still answers signal 0.
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return False
    return True


class Live:
    def __init__(self, scratch: Path) -> None:
        self.scratch = scratch
        self.awkward = scratch / AWKWARD
        self.root = self.awkward / "h"
        self.bin = self.awkward / "bin"
        # Registry paths expand ${VAR}, so the project keeps a plain name.
        self.project = scratch / "project"
        self.marker = self.awkward / "fixture.json"
        self.cli = Path(sys.executable).parent / "agent-comms"
        self.steps = 0
        self.backend = None

    # -- plumbing --

    def ok(self, text: str) -> None:
        self.steps += 1
        say(f"ok {self.steps}: {text}")

    def run_cli(self, *args: str) -> tuple[int, dict]:
        done = subprocess.run([str(self.cli), *args], capture_output=True, text=True, check=False)
        try:
            payload = json.loads(done.stdout)
        except ValueError:
            raise CheckFailed(f"agent-comms {' '.join(args)} printed no JSON (exit {done.returncode}): {done.stdout}{done.stderr}") from None
        return done.returncode, payload

    def must(self, *args: str) -> dict:
        code, payload = self.run_cli(*args)
        if code != 0:
            raise CheckFailed(f"agent-comms {' '.join(args)} exited {code}: {json.dumps(payload, sort_keys=True)}")
        return payload

    def wait_healthy(self, what: str, *, not_pid: int | None = None) -> int:
        deadline = time.monotonic() + HEALTHY_TIMEOUT
        payload: dict = {}
        while time.monotonic() < deadline:
            code, payload = self.run_cli("service", "status")
            if code == 0 and payload.get("pid") != not_pid:
                if not payload["heartbeat"]["from_service"]:
                    raise CheckFailed(f"{what}: status ok without the service's own heartbeat: {payload}")
                return int(payload["pid"])
            time.sleep(1.0)
        raise CheckFailed(f"{what}: not running with a fresh heartbeat after {HEALTHY_TIMEOUT:.0f} s: {json.dumps(payload, sort_keys=True)}")

    def fixture(self) -> dict:
        return json.loads(self.marker.read_text())

    def fixture_survives(self, what: str) -> None:
        pids = self.fixture()
        dead = [name for name, pid in pids.items() if not alive(pid)]
        if dead:
            raise CheckFailed(f"{what}: the stand-in {' and '.join(dead)} did not survive ({pids})")

    # -- what the operating system says the process got --

    def process_view(self, pid: int) -> tuple[list[str] | str, dict[str, str] | str]:
        if sys.platform.startswith("linux"):
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")[:-1]
            pairs = Path(f"/proc/{pid}/environ").read_bytes().decode().split("\0")
            return argv, dict(pair.split("=", 1) for pair in pairs if "=" in pair)
        text = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout
        return text, text

    def check_process(self, pid: int, command: list[str], environment: dict[str, str]) -> None:
        argv, env = self.process_view(pid)
        if isinstance(argv, list):
            # The console script is a Python script, so the kernel's argv is
            # the interpreter followed by the unit's command.
            if argv[-len(command):] != command and argv[1:] != command:
                raise CheckFailed(f"pid {pid} runs {argv}, the unit names {command}")
            for name, value in environment.items():
                if env.get(name, "") != value:
                    raise CheckFailed(f"pid {pid} has {name}={env.get(name)!r}, the unit names {value!r}")
            return
        for word in command[1:]:
            if word not in argv:
                raise CheckFailed(f"pid {pid}: {word!r} is not in its command line: {argv}")
        for name, value in environment.items():
            if value and f"{name}={value}" not in env:
                raise CheckFailed(f"pid {pid}: {name}={value} is not in its environment: {env}")

    def in_service_cgroup(self, pid: int) -> None:
        if not sys.platform.startswith("linux"):
            return
        cgroup = Path(f"/proc/{pid}/cgroup").read_text()
        if self.backend.unit not in cgroup:
            raise CheckFailed(f"the stand-in (pid {pid}) is not in the unit's control group, so it proves nothing: {cgroup.strip()}")

    # -- the check --

    def prepare(self) -> None:
        from agent_comms import paths, service

        if self.scratch.exists():
            shutil.rmtree(self.scratch)
        for directory in (self.root, self.bin, self.project):
            directory.mkdir(parents=True)
        for name in [name for name in os.environ if name.startswith("AGENT_COMMS_")]:
            del os.environ[name]
        os.environ["AGENT_COMMS_HOME"] = str(self.root)
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{os.environ['PATH']}"
        self.backend = service.make_backend(service.suffix_for(paths.runtime_root(), default_root=False))
        say(f"manager: {self.backend.name}; unit: {self.backend.unit_path}")
        say(f"runtime root: {self.root}")
        self.must(
            "setup", "--project-root", str(self.project), "--team", "live", "--runtimes", "fake",
            "--clients", "", "--human", "alice", "--human-id", HUMAN, "--yes",
        )

    def wrapper(self) -> Path:
        fixture = self.bin / "fixture.py"
        fixture.write_text(FIXTURE)
        wrapper = self.bin / "monitor-with-stand-in"
        monitor = Path(sys.executable).parent / "agent-comms-monitor"
        wrapper.write_text(
            "#!/bin/sh\n"
            f"marker={shlex.quote(str(self.marker))}\n"
            f'if [ ! -e "$marker" ]; then {shlex.quote(sys.executable)} {shlex.quote(str(fixture))} "$marker"; fi\n'
            f'exec {shlex.quote(str(monitor))} "$@"\n'
        )
        wrapper.chmod(0o755)
        return wrapper

    def run(self) -> None:
        from agent_comms import paths, service

        self.prepare()

        installed = self.must("service", "install", "--human-actor-id", HUMAN)
        if not self.backend.unit_path.is_file():
            raise CheckFailed(f"install wrote no unit at {self.backend.unit_path}")
        if self.backend.unit_path.stat().st_mode & 0o077:
            raise CheckFailed(f"{self.backend.unit_path} is readable by others")
        pid = self.wait_healthy("install")
        self.ok(f"install: running as pid {pid} with its own fresh heartbeat")

        self.check_process(pid, installed["command"], installed["environment"])
        self.ok("the process has the unit's argv and environment, awkward directory name intact")

        spec = service.spec_from_environment(
            human_actor_id=HUMAN, db_path=paths.db_path(), db_explicit=False, program=self.wrapper()
        )
        service.install(spec, self.backend)
        pid = self.wait_healthy("reinstall with the stand-in", not_pid=pid)
        deadline = time.monotonic() + 15
        while not self.marker.is_file() and time.monotonic() < deadline:
            time.sleep(0.2)
        pids = self.fixture()
        self.fixture_survives("start")
        self.in_service_cgroup(pids["supervisor"])
        self.ok(f"reinstall: running as pid {pid}; stand-in supervisor {pids['supervisor']} and child {pids['child']} started inside the service")

        os.kill(pid, signal.SIGKILL)
        restarted = self.wait_healthy("restart after SIGKILL", not_pid=pid)
        self.fixture_survives("restart after SIGKILL")
        self.ok(f"SIGKILL of pid {pid}: restarted as pid {restarted}; the stand-in survived")

        self.must("service", "stop")
        time.sleep(STAYS_STOPPED_SECONDS)
        code, payload = self.run_cli("service", "status")
        if code != 3 or payload["running"] or alive(restarted):
            raise CheckFailed(f"stop: still running after {STAYS_STOPPED_SECONDS:.0f} s: {json.dumps(payload, sort_keys=True)}")
        if not self.backend.unit_path.is_file():
            raise CheckFailed("stop removed the unit file")
        self.fixture_survives("stop")
        self.ok(f"stop: stayed stopped for {STAYS_STOPPED_SECONDS:.0f} s; the stand-in survived")

        self.must("service", "start")
        started = self.wait_healthy("start", not_pid=restarted)
        self.fixture_survives("start after stop")
        self.ok(f"start: running as pid {started}; the stand-in survived")

        self.must("service", "install", "--human-actor-id", HUMAN)
        again = self.wait_healthy("install over a running service", not_pid=started)
        self.fixture_survives("reinstall")
        self.ok(f"install again: running as pid {again}; the stand-in survived")

        self.must("service", "uninstall")
        state = self.backend.state()
        if self.backend.unit_path.exists() or state.running or alive(again):
            raise CheckFailed(f"uninstall left something behind: file={self.backend.unit_path.exists()} state={state}")
        self.fixture_survives("uninstall")
        self.ok("uninstall: no unit file, nothing running; the stand-in survived")

    def cleanup(self, keep: bool) -> None:
        problems = []
        try:
            if self.backend is not None:
                self.backend.uninstall()
        except Exception as exc:  # report and keep cleaning
            problems.append(f"uninstall of {self.backend.unit_path} failed: {exc}")
        if self.marker.is_file():
            for pid in self.fixture().values():
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        if self.backend is not None:
            deadline = time.monotonic() + 10
            state = self.backend.state()
            while state.loaded and time.monotonic() < deadline:
                time.sleep(0.5)
                state = self.backend.state()
            if self.backend.unit_path.exists() or state.loaded:
                problems.append(f"left behind: unit file {self.backend.unit_path.exists()}, loaded {state.loaded} ({self.backend.unit_path})")
            else:
                say("cleanup: no unit file and no loaded service remain")
        if keep:
            say(f"kept {self.scratch}")
        else:
            shutil.rmtree(self.scratch, ignore_errors=True)
        if problems:
            raise CheckFailed("; ".join(problems))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scratch", default=str(REPO / "local" / "sl"), help="Scratch directory (removed first and at the end)")
    parser.add_argument("--keep", action="store_true", help="Keep the scratch directory")
    args = parser.parse_args()
    sys.path.insert(0, str(REPO))
    live = Live(Path(args.scratch).resolve())
    failure = None
    try:
        live.run()
    except CheckFailed as exc:
        failure = str(exc)
    except Exception as exc:  # a refusal or a bug: still clean up
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            live.cleanup(args.keep or failure is not None)
        except CheckFailed as exc:
            failure = f"{failure}; cleanup: {exc}" if failure else f"cleanup: {exc}"
    if failure:
        say(f"FAIL service-live: {failure}")
        return 1
    say(f"PASS service-live ({live.steps} steps)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
