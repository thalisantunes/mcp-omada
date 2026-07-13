from __future__ import annotations

import pytest

from mcp_omada.channels import CHANNELS_2G, CHANNELS_5G, channel_to_freq
from mcp_omada.exceptions import ValidationError


@pytest.mark.parametrize(
    "channel,expected_freq",
    [(1, 2412), (6, 2437), (11, 2462), (13, 2472)],
)
def test_channel_to_freq_2g(channel: int, expected_freq: int):
    assert channel_to_freq("2g", channel) == expected_freq


@pytest.mark.parametrize(
    "channel,expected_freq",
    [(36, 5180), (149, 5745), (165, 5825)],
)
def test_channel_to_freq_5g(channel: int, expected_freq: int):
    assert channel_to_freq("5g", channel) == expected_freq


def test_channel_to_freq_2g_confirmed_against_real_hardware():
    # Literal confirmed fact (docs/api-notes.md): channel 11 == freq 2462MHz.
    assert channel_to_freq("2g", 11) == 2462


def test_channel_to_freq_5g_confirmed_against_real_hardware():
    # Literal confirmed fact (docs/api-notes.md): channel 149 == freq 5745MHz.
    assert channel_to_freq("5g", 149) == 5745


def test_channel_to_freq_invalid_channel_for_band():
    with pytest.raises(ValidationError, match="not valid for band"):
        channel_to_freq("2g", 149)  # a valid 5g channel, not a 2g one


def test_channel_to_freq_channel_out_of_range():
    with pytest.raises(ValidationError):
        channel_to_freq("2g", 999)


def test_channel_to_freq_invalid_band():
    with pytest.raises(ValidationError, match="band must be"):
        channel_to_freq("6g", 11)


def test_channels_2g_table_has_thirteen_channels():
    assert set(CHANNELS_2G) == set(range(1, 14))


def test_channels_5g_table_has_no_duplicates_and_matches_formula():
    for channel, freq in CHANNELS_5G.items():
        assert freq == 5000 + 5 * channel
