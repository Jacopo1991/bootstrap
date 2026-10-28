"""Shared, fail-closed lock for the owner-provisioned bootstrap marker."""
import contextlib
import fcntl
import os
import pwd
import stat
import time


@contextlib.contextmanager
def marker_lock(marker, exclusive=False):
    path = os.path.abspath(str(marker))
    parent, name = os.path.split(path)
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    lock = None
    try:
        for component in parent.split('/'):
            if not component:
                continue
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            if info.st_mode & 0o022 and not (info.st_uid in (0, os.getuid()) and info.st_mode & stat.S_ISVTX):
                raise OSError('unsafe hold parent')
        info = os.fstat(directory)
        if info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
            raise OSError('unsafe hold parent')
        lock = os.open(name + '.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(lock)
        current = os.stat(name + '.lock', dir_fd=directory, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid not in (os.getuid(), pwd.getpwnam('agent').pw_uid)
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise OSError('unsafe hold lock')
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(lock, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError('hold lock unavailable')
                time.sleep(0.05)
        current = os.stat(name + '.lock', dir_fd=directory, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise OSError('hold lock replaced')
        yield
        current = os.stat(name + '.lock', dir_fd=directory, follow_symlinks=False)
        if (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink) != (current.st_dev, current.st_ino, current.st_mode, current.st_uid, current.st_nlink):
            raise OSError('hold lock replaced')
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)


def provision():
    # Called only by the owner installation step. Existing locks are never changed.
    path = '/etc/agent-hold.lock'
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        info = os.lstat(path)
        if info.st_uid != pwd.getpwnam('agent').pw_uid:
            raise OSError('existing hold lock has wrong owner')
    else:
        try:
            os.fchown(fd, pwd.getpwnam('agent').pw_uid, -1)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    with marker_lock('/etc/agent-hold', exclusive=True):
        pass


if __name__ == '__main__':
    provision()
