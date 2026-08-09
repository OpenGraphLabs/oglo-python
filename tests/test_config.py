"""Config parsing for the single firmware-0.9.9+/schema-6 contract."""

from __future__ import annotations

import json

import pytest

from oglo._config import (
    Capabilities,
    ConfigError,
    Info,
    _fw_at_least,
    finger_index,
    parse_config,
    taxel_index,
)

# Schema shape used by firmware 0.9.9 and newer.
CFG_V6 = json.loads(
    '{"device":"oglo","schema_ver":6,"serial":"OGLO-L-TEST01","side":"left",'
    '"hw_rev":"RDR02_FLEX5_REV_D_TIA","fw_rev":"0.9.9","rate_hz":250,'
    '"samples_per_packet":3,"imu_len":25,"has_mag":true,"values_per_sample":80,'
    '"sample_shape":[5,4,4],"channels":["pinky","ring","middle","index","thumb"],'
    '"device_id":"oglo-test-device-01","batch":"","factory_passed":true,'
    '"stream_clean":true,"stream_thr":80,"zero_valid":true,"cal_lock":false}'
)


def test_a_current_board_parses_to_the_right_capabilities():
    info, caps = parse_config(CFG_V6)
    assert info.serial == "OGLO-L-TEST01" and info.side == "left" and info.is_left
    assert (info.rate_hz, info.has_mag, info.zero_valid, info.stream_clean) == (250, True, True, True)
    assert (caps.values_per_sample, caps.imu_len, caps.has_mag) == (80, 25, True)


def test_the_left_hand_finger_order_comes_from_the_board():
    """A hardcoded list silently mislabels every left-hand dataset: the data looks
    fine and the fingers are wrong."""
    left, _ = parse_config(CFG_V6)
    right, _ = parse_config({
        **CFG_V6,
        "serial": "OGLO-R-TEST01",
        "side": "right",
        "channels": ["thumb", "index", "middle", "ring", "pinky"],
    })
    assert left.channels == ["pinky", "ring", "middle", "index", "thumb"]
    assert right.channels == ["thumb", "index", "middle", "ring", "pinky"]
    assert finger_index(left, "thumb") == 4
    assert finger_index(right, "thumb") == 0


def test_an_unknown_finger_name_says_what_the_board_actually_has():
    info, _ = parse_config(CFG_V6)
    with pytest.raises(KeyError, match="pinky"):
        finger_index(info, "toe")


@pytest.mark.parametrize(
    "cfg,msg",
    [
        ({}, "did not answer"),
        ({"device": "notoglo"}, "not an OGLO"),
        ({**CFG_V6, "fw_rev": "0.9.8"}, "requires firmware 0.9.9"),
        ({**CFG_V6, "schema_ver": 5}, "requires schema 6"),
        ({**CFG_V6, "values_per_sample": 40}, "values_per_sample"),
        ({**CFG_V6, "sample_shape": [80]}, "sample_shape"),
        ({k: v for k, v in CFG_V6.items() if k != "imu_len"}, "missing imu_len"),
        ({**CFG_V6, "channels": []}, "exactly 5"),
        ({**CFG_V6, "side": ""}, "expected 'left' or 'right'"),
        ({**CFG_V6, "channels": ["thumb"] * 5}, "duplicate"),
        ({**CFG_V6, "serial": ""}, "missing device serial"),
        ({**CFG_V6, "stream_clean": True, "zero_valid": False}, "impossible"),
        ({**CFG_V6, "rate_hz": "250"}, "rate_hz must be a JSON integer"),
        ({**CFG_V6, "has_mag": "false"}, "has_mag must be a JSON boolean"),
        ({**CFG_V6, "zero_valid": 1}, "zero_valid must be a JSON boolean"),
        ({**CFG_V6, "samples_per_packet": 4}, "samples_per_packet"),
    ],
)
def test_configs_the_sdk_cannot_work_with_are_rejected_clearly(cfg, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_config(cfg)


def test_unknown_fields_survive_on_raw_so_a_new_one_is_readable_immediately():
    info, _ = parse_config({**CFG_V6, "future_field": 42})
    assert info.raw["future_field"] == 42
    assert info.raw["cal_lock"] is False


def test_device_side_tag_drop_counter_is_not_silently_left_at_zero():
    info, _ = parse_config({**CFG_V6, "tag_dropped": 17})
    assert info.device_dropped == 17


# --- version comparison --------------------------------------------------------


@pytest.mark.parametrize(
    "fw,floor,ok",
    [
        ("0.9.9", (0, 9, 9), True),
        ("0.9.10", (0, 9, 9), True),
        ("0.9.8", (0, 9, 9), False),
        ("1.0.0", (0, 9, 9), True),
        ("", (0, 9, 9), False),
        ("0.9", (0, 9, 9), False),
    ],
)
def test_version_floor_is_numeric_not_lexicographic(fw, floor, ok):
    assert _fw_at_least(fw, floor) is ok


def test_the_lexicographic_trap_specifically():
    """`"0.9.10" < "0.9.9"` as strings. A future build must not read as older."""
    assert "0.9.10" < "0.9.9"  # the trap
    assert _fw_at_least("0.9.10", (0, 9, 9)) is True  # not fallen into


# --- taxel addressing ----------------------------------------------------------


@pytest.mark.parametrize(
    "f,r,c,idx", [(0, 0, 0, 0), (0, 0, 3, 3), (0, 1, 0, 4), (1, 0, 0, 16), (4, 3, 3, 79)]
)
def test_taxel_index_is_finger_row_col(f, r, c, idx):
    assert taxel_index(f, r, c) == idx


@pytest.mark.parametrize("bad", [(5, 0, 0), (0, 4, 0), (0, 0, 4), (-1, 0, 0)])
def test_taxel_index_rejects_out_of_range(bad):
    with pytest.raises(IndexError):
        taxel_index(*bad)
