"""Options shared by the opt-in real-hardware integration suite."""

from __future__ import annotations

import math

import pytest


@pytest.fixture(autouse=True)
def _no_real_firmware_channel(monkeypatch, tmp_path_factory):
    """Keep every test off the real firmware channel and off shared state.

    The channel is a network fetch now. A suite that reaches github.com is slow,
    flaky offline, and would quietly start writing a real release into a
    developer's state directory. Tests that mean to exercise the channel point
    it at their own loopback server and reload the module, which overrides this.
    """
    monkeypatch.setenv("OGLO_FIRMWARE_CHANNEL_URL", "http://127.0.0.1:1/unused")
    monkeypatch.setenv("OGLO_FIRMWARE_CHANNEL_TIMEOUT", "1")
    monkeypatch.setenv(
        "OGLO_STATE_DIR", str(tmp_path_factory.mktemp("oglo-state")))
    try:
        from oglo import _firmware_channel
        _firmware_channel.reset_cache()
    except Exception:
        pass


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("oglo hardware")
    group.addoption(
        "--hardware-single", action="store_true", default=False,
        help="test one attached USB glove and skip checks requiring both hands",
    )
    group.addoption(
        "--hardware-seconds",
        action="store",
        type=float,
        default=3.0,
        help="measurement window for each real-hardware stream check (default: 3 s)",
    )
    group.addoption(
        "--hardware-mutations",
        action="store_true",
        default=False,
        help="also run reversible RAW/CLEAN and rate changes on attached gloves",
    )


@pytest.fixture(scope="session")
def hardware_seconds(pytestconfig: pytest.Config) -> float:
    value = float(pytestconfig.getoption("--hardware-seconds"))
    if not math.isfinite(value) or value < 1.0:
        pytest.fail("--hardware-seconds must be a finite value of at least 1 second")
    return value


@pytest.fixture(scope="session")
def hardware_mutations_enabled(pytestconfig: pytest.Config) -> None:
    if not pytestconfig.getoption("--hardware-mutations"):
        pytest.skip("pass --hardware-mutations to allow state-changing hardware checks")
