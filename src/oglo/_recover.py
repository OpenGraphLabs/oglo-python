"""Restore a glove that the automatic updater cannot reach.

Automatic updates only start from an exact, reviewed running image. Two kinds of
glove fall outside that: one on older firmware, and one that is not running its
firmware at all and enumerates as the ESP32-S3 ROM bootloader (`303a:1001`).
`oglo recover` handles both by doing what the factory does, minus the part that
would erase the glove: it writes the bootloader, the partition table, the boot
selector and the current signed application with esptool, and **never writes
the NVS partition** (0x9000-0xDFFF), which holds the glove's serial, hand,
pairing and calibration. Writing those four pieces at those offsets produces
exactly the factory merged image everywhere except NVS.

The application comes from the same signed release the automatic updater uses:
the runtime channel when it offers something newer, otherwise the copy in the
wheel. The three boot images are pinned by SHA-256.
"""

from __future__ import annotations

import hashlib
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from ._usb import UsbError

APP_ID = (0x2886, 0x0056)   # the glove running OGLO firmware (TinyUSB)
ROM_ID = (0x303A, 0x1001)   # the ESP32-S3 ROM bootloader's USB-Serial/JTAG

RECOVERY_DIR = Path(__file__).parent / "recovery_bundle"
# Arduino-ESP32 2.0.17 boot images, byte-identical to the factory golden bundle.
PINNED = {
    "bootloader.bin": "1776e4dd896a69d0a5c2e79957b0e2a88aa4129b1381d6478683515a1f6af343",
    "partitions.bin": "1d9cca96de0fe07ad7fc0648b9878ddecd9ce565e38b589ad20fea698ed4c80c",
    "boot_app0.bin": "f94c5d786a7a8fab06ac5d10e33bf37711a6697636dc037559ea19cc410a17f0",
}
BOOTLOADER_AT, PARTITIONS_AT, OTADATA_AT, APP_AT = 0x0, 0x8000, 0xE000, 0x10000
NVS_START, NVS_END = 0x9000, 0xE000
ESPTOOL_VERSION = "4.5.1"
CONFIRM_ROM = "RECOVER"


class RecoveryError(UsbError):
    pass


@dataclass(frozen=True)
class Partition:
    type: int
    subtype: int
    offset: int
    size: int
    label: str


@dataclass(frozen=True)
class Images:
    bootloader: bytes
    partitions: bytes
    boot_app0: bytes
    application: bytes
    version: str
    hardware: str

    def segments(self) -> List[tuple]:
        return [(BOOTLOADER_AT, "bootloader.bin", self.bootloader),
                (PARTITIONS_AT, "partitions.bin", self.partitions),
                (OTADATA_AT, "boot_app0.bin", self.boot_app0),
                (APP_AT, "application.bin", self.application)]


def compact_mac(value: Optional[str]) -> Optional[str]:
    """`68:EE:8F:4E:D8:CC` (ROM) and `68EE8F4ED8CC` (OGLO firmware) -> one key."""
    if not value:
        return None
    text = value.strip().lower().replace(":", "").replace("-", "")
    if len(text) != 12 or any(c not in "0123456789abcdef" for c in text):
        return None
    return text


def parse_partitions(data: bytes) -> List[Partition]:
    out = []
    for at in range(0, len(data) - 31, 32):
        entry = data[at:at + 32]
        if entry[:2] != b"\xaa\x50":
            break
        offset, size = struct.unpack("<II", entry[4:12])
        label = entry[12:28].split(b"\0", 1)[0].decode("ascii", "replace")
        out.append(Partition(entry[2], entry[3], offset, size, label))
    return out


def check_layout(partitions: Sequence[Partition], app_len: int) -> None:
    """Refuse unless the table puts NVS, otadata and app0 where the writes assume."""
    def find(type_, subtype):
        hits = [p for p in partitions if p.type == type_ and p.subtype == subtype]
        if len(hits) != 1:
            raise RecoveryError(f"partition table has {len(hits)} entries of type {type_}/{subtype:#x}")
        return hits[0]

    nvs, otadata, app0 = find(1, 0x02), find(1, 0x00), find(0, 0x10)
    if (nvs.offset, nvs.offset + nvs.size) != (NVS_START, NVS_END):
        raise RecoveryError(f"NVS is at {nvs.offset:#x}+{nvs.size:#x}, not where this tool preserves it")
    if otadata.offset != OTADATA_AT or app0.offset != APP_AT:
        raise RecoveryError("otadata/app0 are not at the offsets this tool writes")
    if app_len > app0.size:
        raise RecoveryError("application does not fit in app0")


def _pinned(name: str, directory: Path) -> bytes:
    data = (directory / name).read_bytes()
    if hashlib.sha256(data).hexdigest() != PINNED[name]:
        raise RecoveryError(f"{name} is not the pinned boot image")
    return data


def load_images(*, directory: Path = RECOVERY_DIR, bundle=None) -> Images:
    """Pinned boot images plus the current signed application, all verified."""
    from ._firmware_package import HARDWARE
    if bundle is None:
        from ._firmware_channel import fetch_current
        from ._firmware_package import (BUNDLE_DIR, FILE_SHA, RUNNING_SHA, VERSION,
                                        load_bundle)
        fetched = fetch_current()
        bundle = (load_bundle(fetched) if fetched is not None else
                  load_bundle(BUNDLE_DIR, expect=(VERSION, FILE_SHA, RUNNING_SHA)))
    images = Images(_pinned("bootloader.bin", directory), _pinned("partitions.bin", directory),
                    _pinned("boot_app0.bin", directory), bundle.image, bundle.version, HARDWARE)
    if len(images.bootloader) > PARTITIONS_AT - BOOTLOADER_AT:
        raise RecoveryError("bootloader overlaps the partition table")
    if PARTITIONS_AT + len(images.partitions) > NVS_START:
        raise RecoveryError("partition table overlaps NVS")
    if OTADATA_AT + len(images.boot_app0) > APP_AT:
        raise RecoveryError("boot selector overlaps app0")
    check_layout(parse_partitions(images.partitions), len(images.application))
    return images


@dataclass(frozen=True)
class Port:
    device: str
    vid: Optional[int]
    pid: Optional[int]
    serial_number: Optional[str]

    @property
    def mode(self) -> Optional[str]:
        ident = (self.vid, self.pid)
        return "firmware" if ident == APP_ID else "bootloader" if ident == ROM_ID else None

    @property
    def mac(self) -> Optional[str]:
        return compact_mac(self.serial_number)


def list_ports() -> List[Port]:
    from serial.tools import list_ports as lp
    return sorted((Port(p.device, p.vid, p.pid, p.serial_number) for p in lp.comports()),
                  key=lambda p: p.device)


def select_port(ports: Sequence[Port], device: Optional[str] = None) -> Port:
    gloves = [p for p in ports if p.mode is not None]
    if device is not None:
        for p in gloves:
            if p.device == device:
                return p
        raise RecoveryError(f"{device} is neither an OGLO glove nor an ESP32-S3 bootloader")
    if not gloves:
        raise RecoveryError("no glove found: nothing is attached as 2886:0056 (OGLO) or "
                            "303a:1001 (ESP32-S3 bootloader). Check the USB data cable.")
    if len(gloves) > 1:
        raise RecoveryError("more than one glove is attached; plug in only the one to recover: "
                            + ", ".join(f"{p.device} ({p.mode})" for p in gloves))
    return gloves[0]


def read_config(device: str, timeout: float = 6.0) -> Dict:
    from ._usb import _HANDSHAKE_STOP, _find_config, _open_serial_locked
    s = _open_serial_locked(device)
    try:
        s.write((_HANDSHAKE_STOP + "\n").encode())
        time.sleep(0.4)
        s.reset_input_buffer()
        text, deadline = b"", time.monotonic() + timeout
        while time.monotonic() < deadline:
            s.write(b"GET CONFIG\n")
            end = time.monotonic() + 0.5
            while time.monotonic() < end:
                text += s.read(8192)
                cfg = _find_config(text)
                if cfg is not None:
                    return cfg
            text = text[-16384:]
    finally:
        s.close()
    raise RecoveryError(f"{device} did not answer GET CONFIG")


def enter_bootloader(device: str) -> None:
    """Opening at 1200 baud makes Arduino-ESP32 USB CDC reboot into the ROM."""
    import serial as pyserial
    try:
        s = pyserial.Serial(device, 1200)
        time.sleep(0.25)
        s.close()
    except (OSError, pyserial.SerialException):
        pass  # the device usually vanishes before close() returns; that is success


def wait_for_port(mode: str, mac: Optional[str], timeout: float,
                  ports: Callable[[], List[Port]] = list_ports,
                  sleep: Callable[[float], None] = time.sleep) -> Optional[Port]:
    deadline = time.monotonic() + timeout
    while True:
        hits = [p for p in ports() if p.mode == mode and (mac is None or p.mac == mac)]
        if len(hits) == 1:
            return hits[0]
        if time.monotonic() >= deadline:
            return None
        sleep(0.5)


def esptool_command(device: str, files: Sequence[tuple]) -> List[str]:
    cmd = [sys.executable, "-m", "esptool", "--chip", "esp32s3", "--port", device,
           "--baud", "460800", "--no-stub", "--before", "no_reset", "--after", "no_reset",
           "write_flash", "--flash_mode", "dio", "--flash_freq", "80m", "--flash_size", "8MB"]
    for offset, path in files:
        if NVS_START <= offset < NVS_END:
            raise RecoveryError("refusing to write inside NVS")
        cmd += [f"{offset:#x}", str(path)]
    return cmd


def require_esptool() -> None:
    try:
        from importlib.metadata import version
        found = version("esptool")
    except Exception:
        found = None
    if found != ESPTOOL_VERSION:
        raise RecoveryError(
            f"oglo recover needs esptool {ESPTOOL_VERSION} (found {found or 'none'}). "
            f'Install it in this environment with: python -m pip install "esptool=={ESPTOOL_VERSION}"')


def write_images(device: str, images: Images, workdir: Path,
                 run: Callable = subprocess.run) -> None:
    files = []
    for offset, name, data in images.segments():
        path = workdir / name
        path.write_bytes(data)
        files.append((offset, path))
    result = run(esptool_command(device, files))
    if result.returncode != 0:
        raise RecoveryError(f"esptool failed (exit {result.returncode}); nothing was "
                            "confirmed written. Replug the glove and run oglo recover again.")


def recover(device: Optional[str] = None, *, yes: bool = False,
            replug_timeout: float = 120.0, out: Callable[[str], None] = print,
            ask: Callable[[str], str] = input) -> int:
    import tempfile

    require_esptool()
    images = load_images()
    port = select_port(list_ports(), device)
    out(f"glove at {port.device}: {port.mode} ({port.vid:04x}:{port.pid:04x})")
    before: Dict = {}

    if port.mode == "firmware":
        before = read_config(port.device)
        out(f"  serial {before.get('serial')}  hand {before.get('side')}  "
            f"hardware {before.get('hw_rev')}  firmware {before.get('fw_rev')}")
        if before.get("hw_rev") != images.hardware:
            raise RecoveryError(f"this glove reports hardware {before.get('hw_rev')!r}; "
                                f"firmware {images.version} is only for {images.hardware}")
        if before.get("fw_rev") == images.version and not yes:
            out(f"already on {images.version}; nothing to do (pass --yes to rewrite anyway)")
            return 0
        mac = port.mac
        out("restarting the glove into its bootloader ...")
        enter_bootloader(port.device)
        rom = wait_for_port("bootloader", mac, 15.0)
        if rom is None:
            raise RecoveryError("the glove did not reappear as the ESP32-S3 bootloader. Unplug it, "
                                "plug it back in and run oglo recover again; nothing was written.")
        port = rom
    else:
        out("  the glove is in its bootloader, so its hardware cannot be read first.")
        out(f"  this writes firmware {images.version} for {images.hardware} (OGLO RDR-02 Rev D TIA).")
        if not yes and ask(f'type {CONFIRM_ROM} to continue: ').strip() != CONFIRM_ROM:
            out("cancelled; nothing was written")
            return 1

    out(f"writing firmware {images.version}; the glove's serial, hand and calibration are kept")
    with tempfile.TemporaryDirectory(prefix="oglo-recover-") as tmp:
        write_images(port.device, images, Path(tmp))

    out("")
    out("written. Now UNPLUG the glove, wait 3 seconds and plug it back in")
    out("(switch it off as well if it has a power switch). Waiting for it ...")
    back = wait_for_port("firmware", port.mac, replug_timeout)
    if back is None:
        out("the glove did not come back as 2886:0056 in time.")
        out("If it still shows as 303a:1001 after a replug, contact OpenGraph Labs.")
        return 3
    after = read_config(back.device)
    out(f"  serial {after.get('serial')}  hand {after.get('side')}  firmware {after.get('fw_rev')}")
    if after.get("fw_rev") != images.version:
        out(f"the glove reports firmware {after.get('fw_rev')}, expected {images.version}")
        return 3
    for key in ("serial", "side", "zero_valid"):
        if before and before.get(key) != after.get(key):
            out(f"{key} changed from {before.get(key)!r} to {after.get(key)!r}")
            return 3
    out(f"recovered: firmware {images.version}. `oglo doctor` should now find this glove.")
    return 0
