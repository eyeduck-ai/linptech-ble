import pytest

from linptech_ble.es5_radar import EmptyRoomScan, bitmap_data, duration_reporting, encode_series, hex_series, radar_bins
from linptech_ble.standard_auth import DirectError


def test_real_es5_hex_strings_and_unsigned_masks():
    wire = "5A,50,46,3C,32,28,1E,1E,21,25,25,25,28,27,26,26,2B,2E,30,31,32,31,2E,2C,2C,2C,28,25,1E,1E,1E,20"
    series = hex_series(wire)
    assert len(series) == 32 and series[:8] == [90, 80, 70, 60, 50, 40, 30, 30]
    assert hex_series(encode_series(series)) == series
    assert radar_bins(0x80000001)[0] and radar_bins(0x80000001)[31]
    assert sum(radar_bins(262143)) == 18  # 4.5 m configured on the real device.
    for value in ("1,2,3", ",".join(["100"] * 32), ",".join(["-1"] * 32)):
        assert hex_series(value) is None
    with pytest.raises(DirectError):
        encode_series([True] * 32)


def test_bitmap_keeps_overall_mask_distinct_from_energy_hits():
    parsed = bitmap_data("80000000,2,60,0,0,0,0,1,4")
    assert parsed == {"motion": 2**31, "presence": 2, "occupied": 4, "floor_noise": 96, "noise_health": "正常", "pir": 1}
    assert bitmap_data("0,0,5f,0")["noise_health"] == "輕微異常"
    assert bitmap_data("0,0,5b,0")["noise_health"] == "嚴重異常"
    assert bitmap_data("100000000,0,60,0") is None


def scan():
    return EmptyRoomScan({"motion_threshold": encode_series([30] * 32),
        "presence_threshold": encode_series([40] * 32)}, 0)


def feed(s, t, occupancy=0):
    s.accept("occupancy_status", occupancy, t)
    s.accept("motion_energy", encode_series([50] * 32), t)
    s.accept("presence_energy", encode_series([20] * 32), t)


def test_calibration_requires_continuous_vacancy_and_both_fresh_streams():
    s = scan()
    for t in range(31):
        feed(s, t)
        progress = s.progress(t)
    assert progress["ready"]
    proposal = s.proposals()
    assert hex_series(proposal["motion_threshold"]) == [56] * 32
    assert hex_series(proposal["presence_threshold"]) == [40] * 32
    feed(s, 31, 1)
    assert not s.progress(31)["ready"] and s.since is None
    feed(s, 32)
    assert not s.progress(40)["ready"] and s.since is None


def test_a_missing_energy_channel_cannot_complete_calibration():
    s = scan()
    for t in range(40):
        s.accept("occupancy_status", 0, t)
        s.accept("motion_energy", encode_series([20] * 32), t)
    assert not s.progress(40)["ready"]


def test_duration_reporting_decodes_each_bit_and_keeps_unknown_bits_distinct():
    assert duration_reporting(0)["enabled_minutes"] == []
    assert duration_reporting(0xFFF)["enabled_minutes"] == [1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120]
    assert duration_reporting(0x8801)["enabled_minutes"] == [1, 120]
    assert duration_reporting(0x8801)["unknown_bits"] == 0x8000
    assert duration_reporting(True) is None and duration_reporting(-1) is None
    assert duration_reporting(65536) is None
