from __future__ import annotations

import pytest

from mcp_omada.exceptions import ValidationError
from mcp_omada.validation import validate_band, validate_mac_address


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("50-D4-F7-66-0D-9C", "50-D4-F7-66-0D-9C"),
        ("50:d4:f7:66:0d:9c", "50-D4-F7-66-0D-9C"),
        ("50d4.f766.0d9c", "50-D4-F7-66-0D-9C"),
        ("50D4F7660D9C", "50-D4-F7-66-0D-9C"),
        ("  50-D4-F7-66-0D-9C  ", "50-D4-F7-66-0D-9C"),
    ],
)
def test_validate_mac_address_normalizes(raw: str, expected: str):
    assert validate_mac_address(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not-a-mac",
        "50-D4-F7-66-0D",  # too short
        "50-D4-F7-66-0D-9C-FF",  # too long
        "50-D4-F7-66-0D-ZZ",  # invalid hex
        "50-D4-F7;66-0D-9C && reboot",
    ],
)
def test_validate_mac_address_rejects_invalid(raw: str):
    with pytest.raises(ValidationError):
        validate_mac_address(raw)


def test_validate_mac_address_rejects_non_string():
    with pytest.raises(ValidationError):
        validate_mac_address(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "raw,expected",
    [("2g", "2g"), ("5g", "5g"), ("2G", "2g"), ("5G", "5g"), ("  5g  ", "5g")],
)
def test_validate_band_normalizes(raw: str, expected: str):
    assert validate_band(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "6g", "2.4g", "24", "5", "band"])
def test_validate_band_rejects_invalid(raw: str):
    with pytest.raises(ValidationError):
        validate_band(raw)


def test_validate_band_rejects_non_string():
    with pytest.raises(ValidationError):
        validate_band(None)  # type: ignore[arg-type]
