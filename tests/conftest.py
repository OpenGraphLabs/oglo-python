"""Options shared by the opt-in real-hardware integration suite."""

from __future__ import annotations

import math

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("oglo hardware")
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
