#!/usr/bin/env python3
"""Copy the Windows-written inventory snapshot into the agent's project-data.

Runs as root from inventory-mirror.service. Windows only writes latest.json to a
folder it owns; it never calls wsl.exe and never touches \\\\wsl.localhost. This
script mounts that single folder read-only inside the service's private mount
namespace, so the agent's sessions and the boundary check never see a Windows
mount, then publishes the file as /home/agent/project-data/inventory/latest.json
(owner agent, mode 0600) with an atomic rename. A symlink, hardlink, FIFO or other
non-regular file is refused at the source and at the destination. A missing
snapshot, or one whose createdAtISO is older than MAX_AGE, fails the unit so
`systemctl --failed` shows that Windows has stopped writing it.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import pwd
import stat
import subprocess
import sys
import uuid

WINDOWS_SOURCE = "C:\\ProgramData\\machine-bootstrap\\export"
MOUNT_POINT = "/run/inventory-mirror"
AGENT = "agent"
AGENT_HOME = "/home/agent"
NAME = "latest.json"
MAX_BYTES = 4 * 1024 * 1024
MAX_AGE = timedelta(hours=36)
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Refused(Exception):
    """The source, destination or environment is not safe to copy through."""


def open_agent_dir(parent_fd, name, uid, gid):
    """Open or create an agent-owned 0700 directory without following symlinks."""
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    try:
        fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise Refused(f"{name} is not a real directory") from error
    try:
        if created:
            os.fchown(fd, uid, gid)
        elif os.fstat(fd).st_uid != uid:
            raise Refused(f"{name} is not owned by {AGENT}")
        os.fchmod(fd, 0o700)
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_source(path):
    """Return the file's bytes; refuse when Windows has not written it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise Refused(f"Windows has not written {NAME}") from None
    except OSError as error:
        raise Refused("source is a symlink or unreadable") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Refused("source is not a plain single-link file")
        with os.fdopen(fd, "rb") as source:
            fd = -1
            data = source.read(MAX_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > MAX_BYTES:
        raise Refused("source is too large")
    return data


def validate(data, now):
    try:
        document = json.loads(data.decode("utf-8"))
    except ValueError as error:
        raise Refused("source is not UTF-8 JSON") from error
    if (not isinstance(document, dict) or type(document.get("schemaVersion")) is not int
            or document["schemaVersion"] not in (1, 2) or not isinstance(document.get("distros"), list)):
        raise Refused("source is not an inventory snapshot")
    try:
        created = datetime.fromisoformat(document.get("createdAtISO"))
    except (TypeError, ValueError):
        raise Refused(f"{NAME} has no valid createdAtISO") from None
    if created.tzinfo is None:
        raise Refused(f"{NAME} createdAtISO has no time zone")
    age = now - created
    if age > MAX_AGE:
        raise Refused(f"{NAME} is stale: created {document['createdAtISO']}, "
                      f"{age.total_seconds() / 3600:.1f} hours old (limit {MAX_AGE.total_seconds() / 3600:.0f})")


def publish(directory_fd, data, uid, gid):
    try:
        info = os.stat(NAME, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Refused(f"existing {NAME} is not a plain single-link file")
    temp = f".inventory-{uuid.uuid4().hex}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(fd, "wb") as staged:
            os.fchown(staged.fileno(), uid, gid)
            os.fchmod(staged.fileno(), 0o600)
            staged.write(data)
            staged.flush()
            os.fsync(staged.fileno())
        os.rename(temp, NAME, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except BaseException:
        try:
            os.unlink(temp, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise


def mirror(source_file, home=AGENT_HOME, user=AGENT, now=None):
    """Publish source_file; refuse a missing or stale snapshot."""
    account = pwd.getpwnam(user)
    data = read_source(source_file)
    validate(data, now or datetime.now(timezone.utc))
    home_fd = os.open(home, DIR_FLAGS)
    try:
        if os.fstat(home_fd).st_uid != account.pw_uid:
            raise Refused(f"{home} is not owned by {user}")
        base_fd = open_agent_dir(home_fd, "project-data", account.pw_uid, account.pw_gid)
        try:
            directory_fd = open_agent_dir(base_fd, "inventory", account.pw_uid, account.pw_gid)
            try:
                publish(directory_fd, data, account.pw_uid, account.pw_gid)
            finally:
                os.close(directory_fd)
        finally:
            os.close(base_fd)
    finally:
        os.close(home_fd)


def mount_source():
    # Never expose Windows files to the shared namespace the agent's sessions use.
    if os.readlink("/proc/self/ns/mnt") == os.readlink("/proc/1/ns/mnt"):
        raise Refused("refusing to mount Windows files in the shared mount namespace")
    info = os.lstat(MOUNT_POINT)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise Refused("mount point must be a root-owned 0700 directory")
    subprocess.run(["/usr/bin/mount", "-t", "drvfs", "-o", "ro,uid=0,gid=0,umask=077",
                    WINDOWS_SOURCE, MOUNT_POINT],
                   check=True, timeout=60, stdin=subprocess.DEVNULL)


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-file", help="read this file instead of mounting the Windows folder")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("inventory-mirror: must run as root", file=sys.stderr)
        return 1
    mounted = False
    try:
        if args.source_file is None:
            mount_source()
            mounted = True
        source = args.source_file or os.path.join(MOUNT_POINT, NAME)
        mirror(source)
    except (Refused, OSError, subprocess.SubprocessError) as error:
        print(f"inventory-mirror: FAILED: {error}", file=sys.stderr)
        return 1
    finally:
        if mounted:
            subprocess.run(["/usr/bin/umount", MOUNT_POINT], check=False, timeout=60,
                           stdin=subprocess.DEVNULL)
    print("inventory-mirror: copied")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
