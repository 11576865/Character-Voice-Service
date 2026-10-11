"""Cooperative cross-process ownership for a managed runtime's GPU slot.

The lock file must remain on disk: deleting a held lock-file inode can
create independent owners. It is not a proof that orphaned GPU workers died.
"""
from __future__ import annotations

import errno
import os
import re
from pathlib import Path
from typing import BinaryIO

_SLOT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class RuntimeSlotLease:
    def __init__(self, file: BinaryIO, group: str, path: Path):
        self._file = file
        self.group = group
        self.path = path

    def release(self) -> None:
        if self._file.closed:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()


def acquire_runtime_slot(group: str, directory: Path) -> RuntimeSlotLease | None:
    """Try a nonblocking, OS-owned lease; return None if another owner holds it.

    The caller MUST retain the lease for the entire managed-engine lifetime.
    Directory and lock file are administratively controlled and shared by
    all participating CVS processes on this host.
    """
    if not _SLOT_ID.fullmatch(group):
        raise ValueError(f"invalid runtime exclusive group: {group!r}")
    root = Path(directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / (group + ".lock")
    file = path.open("a+b")
    try:
        # Windows byte-range locks need a real byte in the lock file.
        if os.name == "nt":
            file.seek(0, os.SEEK_END)
            if file.tell() == 0:
                file.write(b"\\0")
                file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLK", errno.EAGAIN)}:
                file.close()
                return None
            raise
        return RuntimeSlotLease(file, group, path)
    except BaseException:
        file.close()
        raise
