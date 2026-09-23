"""Regressions for the independently reproduced September 23 audit failures."""
import os
import runpy
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from oglo import _replay, _stream, _usb
from oglo._ownership import DeviceLease


@pytest.mark.parametrize('name', ['tactile', 'imu', 'mag'])
@pytest.mark.parametrize('legacy', [False, True])
def test_replay_clock_work_is_linear(monkeypatch, name, legacy):
    n = 256
    d = dict(seq=np.arange(n), t_us=np.arange(n), host_t=np.arange(n) / 1000,
             dropped=np.zeros(n), counts=np.zeros((n, 5, 4, 4)),
             accel=np.zeros((n, 3)), gyro=np.zeros((n, 3)), field=np.zeros((n, 3)))
    if not legacy:
        d.update(device_time_us=np.arange(n) + 2**32,
                 host_t_ns=np.arange(n) + 100, host_received_ns=np.arange(n) + 200)
    episode = object.__new__(_replay.Episode)
    episode._info = SimpleNamespace(stream_clean=False)
    monkeypatch.setattr(episode, '_load', lambda _: d)
    rint = np.rint
    calls = []
    monkeypatch.setattr(np, 'rint', lambda a: (calls.append(a.size), rint(a))[1])
    rows = list(getattr(episode, name)())
    assert calls == ([n] if legacy else [])
    assert rows[-1].device_time_us == (n - 1 if legacy else 2**32 + n - 1)
    assert rows[-1].host_received_ns == (255000000 if legacy else 455)


def test_rate_expires_without_new_samples(monkeypatch):
    clock = [10.002]
    monkeypatch.setattr(_stream.time, 'monotonic', lambda: clock[0])
    meter = _stream.RateMeter()
    meter.tick(10)
    meter.tick(10.002)
    assert meter.hz == pytest.approx(500)
    clock[0] = 100
    assert meter.hz == 0


def test_pair_example_stops_peer_before_waiting_for_executor(monkeypatch):
    import oglo
    started = threading.Event()
    stopped, closed = [], []
    def glove(side):
        return SimpleNamespace(info=SimpleNamespace(side=side, serial=side, channels=[]),
                               stop=lambda: stopped.append(side), close=lambda: closed.append(side))
    def record(path, seconds, *, glove, stop_event):
        if glove.info.side == 'left':
            started.set()
            assert stop_event.wait(2), 'peer failure was hidden behind left future'
            return Path('partial-left')
        assert started.wait(2)
        raise RuntimeError('right failed; partial saved')
    monkeypatch.setattr(oglo, 'connect_pair', lambda: (glove('left'), glove('right')))
    monkeypatch.setattr(oglo, 'record', record)
    with pytest.raises(RuntimeError, match='right failed'):
        runpy.run_path(str(Path(__file__).parents[1] / 'examples/04_two_hands.py'))
    assert sorted(stopped) == sorted(closed) == ['left', 'right']


@pytest.mark.skipif(os.name != 'posix', reason='POSIX descriptor inheritance')
def test_child_keeps_ownership_after_parent_closes(tmp_path, monkeypatch):
    monkeypatch.setenv('OGLO_STATE_DIR', str(tmp_path))
    lease = DeviceLease('usb:test').acquire()
    child = subprocess.Popen([sys.executable, '-c', 'import sys; print("ready", flush=True); sys.stdin.read()'],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, pass_fds=(lease.fd,))
    try:
        assert child.stdout.readline() == b'ready\n'
        lease.close()
        with pytest.raises(_usb.PortBusyError):
            DeviceLease('usb:test').acquire()
    finally:
        child.communicate(timeout=3)
    with DeviceLease('usb:test'):
        pass


@pytest.mark.skipif(sys.platform != 'linux', reason='Darwin PTYs do not enforce TIOCEXCL; USB driver qualification is separate')
def test_kernel_exclusion_rejects_uncooperative_second_opener(tmp_path, monkeypatch):
    import pty
    import serial
    import termios
    import fcntl
    monkeypatch.setenv('OGLO_STATE_DIR', str(tmp_path))
    monkeypatch.setattr(_usb, '_owner_pid', lambda _: None)
    master, slave = pty.openpty()
    port = None
    try:
        port = _usb.open_serial(os.ttyname(slave), settle=0)
        with pytest.raises(serial.SerialException):
            serial.Serial(os.ttyname(slave))
        # PTY slave stays alive until master closes, unlike an unplugged USB tty.
        fcntl.ioctl(port.fileno(), termios.TIOCNXCL)
    finally:
        if port:
            port.close()
        os.close(slave)
        os.close(master)


def test_already_owned_port_is_rejected_before_serial_open(monkeypatch, tmp_path):
    monkeypatch.setenv('OGLO_STATE_DIR', str(tmp_path))
    monkeypatch.setattr(_usb, '_owner_pid', lambda _: 12345)
    import serial
    monkeypatch.setattr(serial, 'Serial', lambda: pytest.fail('must not change another owner line state'))
    if os.name == 'posix':
        with pytest.raises(_usb.PortBusyError, match='12345'):
            _usb.open_serial('/dev/fake-oglo')
