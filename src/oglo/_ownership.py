"""Process ownership that survives USB port renumbering and updater restarts."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional


def state_directory() -> Path:
    override = os.environ.get("OGLO_STATE_DIR")
    if override:
        root = Path(override).expanduser()
    elif os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "oglo"
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "oglo"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt" and root.stat().st_uid != os.getuid():
        raise PermissionError(f"OGLO state directory is owned by another user: {root}")
    return root


def usb_identity(vid: int, pid: int, serial: str) -> str:
    return f"usb:{vid:04x}:{pid:04x}:{serial.casefold()}"


def identity_for_port(device: str) -> str:
    from serial.tools import list_ports

    real = os.path.realpath(device).replace("/dev/cu.", "/dev/tty.")
    for port in list_ports.comports():
        if os.path.realpath(port.device).replace("/dev/cu.", "/dev/tty.") == real and port.serial_number and port.vid is not None and port.pid is not None:
            return usb_identity(port.vid, port.pid, port.serial_number)
    # macOS callout/dial-in nodes name the same device.
    return "port:" + real.replace("/dev/cu.", "/dev/tty.")


class DeviceLease:
    """Keep this lock through close/re-enumeration; never unlink lock files.

    On POSIX, updater subprocesses inherit the descriptor. Closing the parent's
    descriptor does not unlock a still-running child's copy. This is essential
    when a driver call cannot be killed promptly: a later SDK must remain blocked.
    OS tty exclusion additionally protects against non-SDK serial clients.
    """

    def __init__(self, identity: str) -> None:
        self.identity = identity
        self.fd: Optional[int] = None

    def acquire(self) -> "DeviceLease":
        if self.fd is not None:
            raise RuntimeError("lease already acquired")
        root = state_directory() / "locks"
        root.mkdir(mode=0o700, exist_ok=True)
        path = root / (hashlib.sha256(self.identity.encode()).hexdigest() + ".lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as exc:
            os.close(fd)
            if isinstance(exc, OSError):
                from ._usb import PortBusyError
                raise PortBusyError(f"another OGLO session owns {self.identity}; close it before retrying") from exc
            raise
        self.fd = fd
        return self

    def close(self) -> None:
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)  # Do not LOCK_UN: an inherited child may still own it.

    def __enter__(self) -> "DeviceLease":
        return self.acquire()

    def __exit__(self, *args) -> None:
        self.close()
