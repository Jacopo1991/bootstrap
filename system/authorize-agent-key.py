#!/usr/bin/env python3
"""Append one validated AgentDev public key without following agent-controlled links."""
from __future__ import annotations

import fcntl
import os
import pwd
import re
import stat
import sys

KEY = re.compile(r"ssh-ed25519[ \t]+[A-Za-z0-9+/]+={0,3}(?:[ \t]+[^\r\n]+)?\Z")
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE = os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def main() -> int:
    line = sys.stdin.buffer.readline(16385)
    if not line.endswith(b"\n") or sys.stdin.buffer.read(1):
        raise ValueError("invalid public key input")
    payload = line[:-1]
    if payload.endswith(b"\r"):
        payload = payload[:-1]
    key = payload.decode("ascii")
    if not KEY.fullmatch(key):
        raise ValueError("invalid public key")
    account = pwd.getpwnam("agent")
    if account.pw_uid == 0 or account.pw_dir != "/home/agent":
        raise ValueError("unexpected agent account")
    parent_fd = os.open("/home", DIRECTORY)
    home_fd = os.open("agent", DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        home_stat = os.fstat(home_fd)
        if not stat.S_ISDIR(home_stat.st_mode) or home_stat.st_uid != account.pw_uid:
            raise ValueError("unsafe agent home")
        try:
            os.mkdir(".ssh", 0o700, dir_fd=home_fd)
            created_ssh_directory = True
        except FileExistsError:
            created_ssh_directory = False
        ssh_fd = os.open(".ssh", DIRECTORY, dir_fd=home_fd)
        try:
            ssh_stat = os.fstat(ssh_fd)
            if not stat.S_ISDIR(ssh_stat.st_mode):
                raise ValueError("unsafe SSH directory")
            if created_ssh_directory:
                os.fchown(ssh_fd, account.pw_uid, account.pw_gid)
            elif ssh_stat.st_uid != account.pw_uid:
                raise ValueError("unexpected SSH directory owner")
            os.fchmod(ssh_fd, 0o700)
            try:
                key_fd = os.open("authorized_keys", FILE | os.O_EXCL, 0o600, dir_fd=ssh_fd)
                created_key_file = True
            except FileExistsError:
                key_fd = os.open("authorized_keys", FILE, dir_fd=ssh_fd)
                created_key_file = False
            try:
                key_stat = os.fstat(key_fd)
                if not stat.S_ISREG(key_stat.st_mode) or key_stat.st_nlink != 1:
                    raise ValueError("unsafe authorized_keys file")
                if created_key_file:
                    os.fchown(key_fd, account.pw_uid, account.pw_gid)
                elif key_stat.st_uid != account.pw_uid:
                    raise ValueError("unexpected authorized_keys owner")
                os.fchmod(key_fd, 0o600)
                fcntl.flock(key_fd, fcntl.LOCK_EX)
                os.lseek(key_fd, 0, os.SEEK_SET)
                chunks = []
                while True:
                    chunk = os.read(key_fd, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                existing = b"".join(chunks)
                key_bytes = key.encode("ascii")
                if key_bytes not in existing.splitlines():
                    suffix = (b"" if not existing or existing.endswith(b"\n") else b"\n") + key_bytes + b"\n"
                    os.write(key_fd, suffix)
                    os.fsync(key_fd)
            finally:
                os.close(key_fd)
        finally:
            os.close(ssh_fd)
    finally:
        os.close(home_fd)
        os.close(parent_fd)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError, KeyError):
        print("Could not safely install AgentDev public key.", file=sys.stderr)
        raise SystemExit(1)
