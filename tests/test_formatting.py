from __future__ import annotations

import pytest

from mcp_omada.config import AuthMode
from mcp_omada.formatting import (
    is_connected,
    normalize_device,
    normalize_radio,
    parse_channel,
    parse_uptime_string,
    uptime_seconds,
)

# --- is_connected -----------------------------------------------------


def test_is_connected_legacy_uses_status_category_primary():
    assert is_connected({"statusCategory": 1, "status": 0}, AuthMode.LEGACY) is True
    assert is_connected({"statusCategory": 0, "status": 14}, AuthMode.LEGACY) is False


def test_is_connected_legacy_falls_back_to_status_14():
    assert is_connected({"status": 14}, AuthMode.LEGACY) is True
    assert is_connected({"status": 5}, AuthMode.LEGACY) is False


def test_is_connected_legacy_neither_field_present():
    assert is_connected({}, AuthMode.LEGACY) is None


def test_is_connected_openapi_uses_status_1():
    assert is_connected({"status": 1}, AuthMode.OPENAPI) is True
    assert is_connected({"status": 0}, AuthMode.OPENAPI) is False
    # openapi status==14 must NOT be treated as connected - different semantics.
    assert is_connected({"status": 14}, AuthMode.OPENAPI) is False


def test_is_connected_openapi_status_absent():
    assert is_connected({}, AuthMode.OPENAPI) is None


def test_is_connected_openapi_ignores_status_category():
    # statusCategory is a legacy-only field; must not leak into openapi logic.
    assert is_connected({"statusCategory": 1, "status": 0}, AuthMode.OPENAPI) is False


# --- uptime -----------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1h 43m", 6180),
        ("3d 5h 20m", 278400),
        ("43m", 2580),
        ("5s", 5),
        ("2d", 172800),
        ("1H43M", 6180),
        (None, None),
        ("", None),
        ("garbage", None),
    ],
)
def test_parse_uptime_string(value, expected):
    assert parse_uptime_string(value) == expected


def test_uptime_seconds_prefers_uptime_long():
    assert uptime_seconds({"uptimeLong": 12345, "uptime": "1h 43m"}) == 12345


def test_uptime_seconds_falls_back_to_uptime_string():
    assert uptime_seconds({"uptime": "1h 43m"}) == 6180


def test_uptime_seconds_neither_present():
    assert uptime_seconds({}) is None


def test_uptime_seconds_uptime_long_non_numeric_falls_back():
    assert uptime_seconds({"uptimeLong": "not-a-number", "uptime": "43m"}) == 2580


# --- channel ------------------------------------------------------------


def test_parse_channel_real_hardware_shape():
    # Confirmed shape: irregular whitespace around the slash.
    assert parse_channel("11  / 2462MHz") == {"channel": 11, "freq_mhz": 2462, "raw": "11  / 2462MHz"}


def test_parse_channel_5ghz_internal_index_shape():
    assert parse_channel("17  / 5745MHz") == {"channel": 17, "freq_mhz": 5745, "raw": "17  / 5745MHz"}


def test_parse_channel_tolerates_regular_whitespace():
    assert parse_channel("6 / 2437MHz") == {"channel": 6, "freq_mhz": 2437, "raw": "6 / 2437MHz"}


def test_parse_channel_none_input():
    assert parse_channel(None) == {"channel": None, "freq_mhz": None, "raw": None}


def test_parse_channel_unparseable_input():
    assert parse_channel("not-a-channel") == {"channel": None, "freq_mhz": None, "raw": "not-a-channel"}


# --- normalize_radio -----------------------------------------------------


def test_normalize_radio_none():
    assert normalize_radio(None) is None


def test_normalize_radio_full_shape():
    radio = {
        "actualChannel": "11  / 2462MHz",
        "txPower": "high",
        "bandWidth": "20MHz",
        "rdMode": "11ax",
        "txUtil": 10,
        "rxUtil": 5,
        "interUtil": 2,
    }
    assert normalize_radio(radio) == {
        "channel": 11,
        "freq_mhz": 2462,
        "raw_channel": "11  / 2462MHz",
        "tx_power": "high",
        "band_width": "20MHz",
        "radio_mode": "11ax",
        "tx_util": 10,
        "rx_util": 5,
        "inter_util": 2,
    }


# --- normalize_device -----------------------------------------------------


def test_normalize_device_legacy_row():
    row = {
        "name": "AP-1",
        "type": "ap",
        "mac": "50-D4-F7-66-0D-9C",
        "ip": "10.1.1.50",
        "model": "EAP670",
        "compoundModel": "EAP670(US)",
        "firmwareVersion": "5.1.5",
        "needUpgrade": False,
        "statusCategory": 1,
        "status": 14,
        "uptime": "1h 43m",
        "uptimeLong": 6180,
        "cpuUtil": 12,
        "memUtil": 45,
        "clientNum": 7,
        "clientNum2g": 2,
        "clientNum5g": 5,
        "txRate": 1200,
        "rxRate": 300,
        "upload": 1000,
        "download": 5000,
        "wp2g": {"actualChannel": "11  / 2462MHz"},
        "wp5g": {"actualChannel": "17  / 5745MHz"},
    }
    normalized = normalize_device(row, AuthMode.LEGACY)
    assert normalized["name"] == "AP-1"
    assert normalized["mac"] == "50-D4-F7-66-0D-9C"
    assert normalized["connected"] is True
    assert normalized["uptime_seconds"] == 6180
    assert normalized["client_num"] == 7
    assert normalized["wifi_2g"]["channel"] == 11
    assert normalized["wifi_5g"]["freq_mhz"] == 5745
    assert normalized["sn"] is None
    assert normalized["last_seen"] is None
    assert normalized["auth_mode"] == "legacy"


def test_normalize_device_openapi_row_has_null_legacy_only_fields():
    row = {
        "name": "AP-1",
        "type": "ap",
        "mac": "50-D4-F7-66-0D-9C",
        "ip": "10.1.1.50",
        "status": 1,
        "cpuUtil": 12,
        "memUtil": 45,
        "uptime": "1h 43m",
        "model": "EAP670",
        "firmwareVersion": "5.1.5",
        "sn": "2131A0123456",
    }
    normalized = normalize_device(row, AuthMode.OPENAPI)
    assert normalized["connected"] is True
    assert normalized["uptime_seconds"] == 6180  # parsed from the string fallback
    assert normalized["client_num"] is None
    assert normalized["wifi_2g"] is None
    assert normalized["wifi_5g"] is None
    assert normalized["sn"] == "2131A0123456"
    assert normalized["auth_mode"] == "openapi"


def test_normalize_device_missing_fields_are_none():
    normalized = normalize_device({}, AuthMode.LEGACY)
    assert normalized["name"] is None
    assert normalized["connected"] is None
    assert normalized["uptime_seconds"] is None
    assert normalized["wifi_2g"] is None
