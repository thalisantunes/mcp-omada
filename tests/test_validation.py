from __future__ import annotations

import pytest

from mcp_omada.exceptions import ValidationError
from mcp_omada.validation import validate_mac_address


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("50-D4-F7-10-20-30", "50-D4-F7-10-20-30"),
        ("50:d4:f7:10:20:30", "50-D4-F7-10-20-30"),
        ("50d4.f710.2030", "50-D4-F7-10-20-30"),
        ("50D4F7102030", "50-D4-F7-10-20-30"),
        ("  50-D4-F7-10-20-30  ", "50-D4-F7-10-20-30"),
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
        "50-D4-F7-10-20-30-FF",  # too long
        "50-D4-F7-66-0D-ZZ",  # invalid hex
        "50-D4-F7;10-20-30 && reboot",
    ],
)
def test_validate_mac_address_rejects_invalid(raw: str):
    with pytest.raises(ValidationError):
        validate_mac_address(raw)


def test_validate_mac_address_rejects_non_string():
    with pytest.raises(ValidationError):
        validate_mac_address(None)  # type: ignore[arg-type]
