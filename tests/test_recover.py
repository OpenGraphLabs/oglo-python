"""`oglo recover`: every way it could erase a glove or flash the wrong one.

No hardware. The USB ports, the glove's CONFIG answer and esptool are faked, so
each test pins one decision the tool makes before or after it writes.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from oglo import _recover as rc
from oglo._firmware_package import HARDWARE, VERSION


APP_PORT = rc.Port("/dev/ttyACM0", 0x2886, 0x0056, "68EE8F4ED8CC")
ROM_PORT = rc.Port("/dev/ttyACM1", 0x303A, 0x1001, "68:EE:8F:4E:D8:CC")
CFG = {"serial": "OGLO-L-00005", "side": "left", "hw_rev": HARDWARE,
       "fw_rev": "0.9.12", "zero_valid": True}
_REAL_LOAD_IMAGES = rc.load_images
BUNDLED_APP = (Path(rc.__file__).parent / "firmware_bundle" / "application.bin").read_bytes()


def unsigned_images():
    """Real boot images and application bytes, without the signature check.

    The signature path needs `cryptography`, which the minimum-dependency job
    does not install; the decisions tested with this do not depend on it.
    """
    return _REAL_LOAD_IMAGES(bundle=SimpleNamespace(image=BUNDLED_APP, version=VERSION))


def test_boot_images_are_the_pinned_factory_bytes():
    for name, digest in rc.PINNED.items():
        assert hashlib.sha256((rc.RECOVERY_DIR / name).read_bytes()).hexdigest() == digest


def test_signed_images_load():
    pytest.importorskip("cryptography")
    images = rc.load_images()
    assert images.application == BUNDLED_APP


def test_no_write_touches_nvs():
    images = unsigned_images()
    assert images.version == VERSION and images.hardware == HARDWARE
    for offset, _name, data in images.segments():
        end = offset + len(data)
        assert end <= rc.NVS_START or offset >= rc.NVS_END, f"{offset:#x}..{end:#x} overlaps NVS"


def test_partition_table_is_where_the_writes_assume():
    parts = rc.parse_partitions((rc.RECOVERY_DIR / "partitions.bin").read_bytes())
    nvs = [p for p in parts if p.label == "nvs"][0]
    assert (nvs.offset, nvs.size) == (0x9000, 0x5000)
    rc.check_layout(parts, 1_054_592)


def test_a_moved_nvs_is_refused():
    parts = [rc.Partition(1, 2, 0xA000, 0x5000, "nvs"), rc.Partition(1, 0, 0xE000, 0x2000, "otadata"),
             rc.Partition(0, 0x10, 0x10000, 0x330000, "app0")]
    with pytest.raises(rc.RecoveryError, match="NVS"):
        rc.check_layout(parts, 1000)


def test_a_tampered_boot_image_is_refused(tmp_path):
    for name in rc.PINNED:
        (tmp_path / name).write_bytes((rc.RECOVERY_DIR / name).read_bytes())
    (tmp_path / "bootloader.bin").write_bytes(b"\0" * 100)
    with pytest.raises(rc.RecoveryError, match="pinned"):
        rc.load_images(directory=tmp_path,
                       bundle=SimpleNamespace(image=BUNDLED_APP, version=VERSION))


def test_rom_and_firmware_serials_name_the_same_chip():
    assert rc.compact_mac("68:EE:8F:4E:D8:CC") == rc.compact_mac("68EE8F4ED8CC") == "68ee8f4ed8cc"
    assert rc.compact_mac("OGLO-L-00005") is None


def test_port_selection():
    other = rc.Port("/dev/ttyUSB0", 0x0403, 0x6001, "FT1")
    assert rc.select_port([other, ROM_PORT]).mode == "bootloader"
    assert rc.select_port([APP_PORT]).mode == "firmware"
    with pytest.raises(rc.RecoveryError, match="no glove"):
        rc.select_port([other])
    with pytest.raises(rc.RecoveryError, match="more than one"):
        rc.select_port([APP_PORT, ROM_PORT])
    assert rc.select_port([APP_PORT, ROM_PORT], "/dev/ttyACM1") is ROM_PORT
    with pytest.raises(rc.RecoveryError):
        rc.select_port([other], "/dev/ttyUSB0")


def test_esptool_command_never_resets_and_never_writes_nvs():
    cmd = rc.esptool_command("/dev/ttyACM1", [(0x0, "a"), (0x8000, "b"), (0xE000, "c"), (0x10000, "d")])
    assert cmd[cmd.index("--before") + 1] == "no_reset" and cmd[cmd.index("--after") + 1] == "no_reset"
    assert "--no-stub" in cmd and cmd[-8:] == ["0x0", "a", "0x8000", "b", "0xe000", "c", "0x10000", "d"]
    with pytest.raises(rc.RecoveryError, match="NVS"):
        rc.esptool_command("/dev/ttyACM1", [(0x9000, "x")])


@pytest.fixture
def bench(monkeypatch):
    """A fake glove: ports appear and disappear as the real one would."""
    state = SimpleNamespace(ports=[APP_PORT], writes=[], configs=[dict(CFG), dict(CFG, fw_rev=VERSION)],
                            touched=[], answers=[])

    monkeypatch.setattr(rc, "require_esptool", lambda: None)
    monkeypatch.setattr(rc, "load_images", unsigned_images)
    monkeypatch.setattr(rc, "list_ports", lambda: list(state.ports))
    monkeypatch.setattr(rc, "read_config", lambda device: state.configs.pop(0))

    def enter_bootloader(device):
        state.touched.append(device)
        state.ports = [ROM_PORT]
    monkeypatch.setattr(rc, "enter_bootloader", enter_bootloader)

    def write_images(device, images, workdir, run=None):
        state.writes.append((device, [o for o, _n, _d in images.segments()]))
        state.ports = [rc.Port("/dev/ttyACM0", 0x2886, 0x0056, "68EE8F4ED8CC")]
    monkeypatch.setattr(rc, "write_images", write_images)

    def wait_for_port(mode, mac, timeout, ports=None, sleep=None):
        hits = [p for p in state.ports if p.mode == mode and (mac is None or p.mac == mac)]
        return hits[0] if len(hits) == 1 else None
    monkeypatch.setattr(rc, "wait_for_port", wait_for_port)
    return state


def run(state, **kw):
    lines = []
    code = rc.recover(out=lines.append, ask=lambda _prompt: state.answers.pop(0), **kw)
    return code, "\n".join(lines)


def test_running_old_firmware_is_restarted_into_rom_and_written(bench):
    code, log = run(bench)
    assert code == 0, log
    assert bench.touched == ["/dev/ttyACM0"]
    assert bench.writes == [("/dev/ttyACM1", [0x0, 0x8000, 0xE000, 0x10000])]
    assert f"recovered: firmware {VERSION}" in log


def test_wrong_hardware_is_refused_before_anything_happens(bench):
    bench.configs = [dict(CFG, hw_rev="RDR02_REV_C")]
    with pytest.raises(rc.RecoveryError, match="hardware"):
        run(bench)
    assert bench.touched == [] and bench.writes == []


def test_a_glove_already_on_the_release_is_left_alone(bench):
    bench.configs = [dict(CFG, fw_rev=VERSION)]
    code, log = run(bench)
    assert code == 0 and bench.writes == [] and "nothing to do" in log


def test_a_bootloader_glove_needs_typed_confirmation(bench):
    bench.ports = [ROM_PORT]
    bench.configs = [dict(CFG, fw_rev=VERSION)]
    bench.answers = ["y"]
    code, _log = run(bench)
    assert code == 1 and bench.writes == []
    bench.answers = [rc.CONFIRM_ROM]
    code, log = run(bench)
    assert code == 0, log
    assert bench.writes == [("/dev/ttyACM1", [0x0, 0x8000, 0xE000, 0x10000])]


def test_no_return_after_replug_is_reported_not_claimed(bench, monkeypatch):
    def write_images(device, images, workdir, run=None):
        bench.writes.append(device)
        bench.ports = [ROM_PORT]
    monkeypatch.setattr(rc, "write_images", write_images)
    code, log = run(bench)
    assert code == 3 and "did not come back" in log


def test_a_changed_identity_after_the_write_is_reported(bench):
    bench.configs = [dict(CFG), dict(CFG, fw_rev=VERSION, serial="OGLO-L-99999")]
    code, log = run(bench)
    assert code == 3 and "serial changed" in log


def test_failed_esptool_says_so(tmp_path):
    images = unsigned_images()
    with pytest.raises(rc.RecoveryError, match="esptool failed"):
        rc.write_images("/dev/null", images, tmp_path, run=lambda cmd: SimpleNamespace(returncode=2))
    assert sorted(p.name for p in Path(tmp_path).iterdir()) == [
        "application.bin", "boot_app0.bin", "bootloader.bin", "partitions.bin"]


def test_cli_exposes_recover():
    from oglo import cli
    with pytest.raises(SystemExit) as exc:
        cli.main(["recover", "--help"])
    assert exc.value.code == 0
