#!/usr/bin/env python3
"""Live check that lane sandboxes cannot reach agent's rootless Docker socket (not in the gate).

A Docker socket carries agent's full power, so it must be reachable from unsandboxed agent
processes only. Three modes, all stdlib, none of them touches the Docker API:

  check --sandbox 'codex sandbox' [--sandbox ...] [--live] [PATH]
      Run from an unsandboxed agent shell. Listens at PATH itself (or, with --live, uses the
      running daemon), shows that an unsandboxed connect reaches it, then runs `probe` under each
      sandbox command and requires that it did not connect. With the listener the proof is on the
      server side: the sandboxed run must leave zero connections, whatever errno it saw.
  serve [PATH]
      Listen at PATH and print every connection. Run it in an unsandboxed shell, then run
      `probe` from a Bash call inside a real Claude lane (and a real Codex lane); no connection
      may be printed.
  probe [--expect reached|denied] [PATH]
      Try to connect to PATH. Exit 0 when the outcome matches --expect (default: denied), 3 if not.

PATH defaults to /run/user/1001/docker.sock. check and serve exit 0 when contained, 1 otherwise.
"""
import argparse
import errno
import os
import shlex
import socket
import subprocess
import sys
import threading
import time

SOCKET = "/run/user/1001/docker.sock"


def connect(path):
    """Return 'reached' or a short reason for not connecting."""
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as error:
        return f"socket() {errno.errorcode.get(error.errno, error.errno)}"
    try:
        client.connect(path)
        return "reached"
    except OSError as error:
        return f"connect() {errno.errorcode.get(error.errno, error.errno)}"
    finally:
        client.close()


class Listener:
    def __init__(self, path):
        if os.path.lexists(path):
            raise SystemExit(f"{path} exists; stop the daemon or use --live")
        self.path = path
        self.connections = 0
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        os.chmod(path, 0o600)
        self.server.listen(8)
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self):
        while True:
            try:
                peer, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            print(f"CONNECTION received at {self.path} (#{self.connections})", flush=True)
            peer.close()

    def close(self):
        self.server.close()
        os.unlink(self.path)


def run_check(path, sandboxes, live):
    listener = None if live else Listener(path)
    try:
        control = connect(path)
        if control != "reached":
            print(f"FAIL: control connect without a sandbox did not reach {path}: {control}")
            return 1
        print("control (no sandbox): reached, as it must")
        failed = False
        for sandbox in sandboxes:
            time.sleep(0.3)  # let the accept thread count the previous connection first
            before = listener.connections if listener else 0
            command = shlex.split(sandbox) + [sys.executable, "-I", os.path.abspath(__file__), "probe", path]
            done = subprocess.run(command, capture_output=True, text=True, check=False)
            time.sleep(0.3)
            print(f"sandbox {sandbox!r}: rc={done.returncode} {(done.stdout + done.stderr).strip()}")
            if done.returncode not in (0, 3):
                print("FAIL: the probe did not run inside this sandbox (rc is neither 0 nor 3).")
                failed = True
            elif done.returncode == 3 or (listener and listener.connections != before):
                print(f"FAIL: {sandbox!r} reached the Docker socket.")
                failed = True
        if not failed:
            print("PASS: every sandbox failed to reach the Docker socket.")
        return int(failed)
    finally:
        if listener:
            listener.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("mode", choices=("check", "serve", "probe"))
    parser.add_argument("path", nargs="?", default=SOCKET)
    parser.add_argument("--sandbox", action="append", default=[])
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--expect", choices=("reached", "denied"), default="denied")
    args = parser.parse_args()
    if args.mode == "probe":
        outcome = connect(args.path)
        print(f"{args.path}: {outcome}")
        return 3 * int((outcome == "reached") != (args.expect == "reached"))
    if args.mode == "serve":
        listener = Listener(args.path)
        print(f"listening at {args.path}; Ctrl-C to stop", flush=True)
        try:
            listener.thread.join()
        except KeyboardInterrupt:
            pass
        finally:
            listener.close()
        print(f"{listener.connections} connection(s) received")
        return int(listener.connections > 0)
    if not args.sandbox:
        parser.error("check needs at least one --sandbox command")
    return run_check(args.path, args.sandbox, args.live)


if __name__ == "__main__":
    sys.exit(main())
