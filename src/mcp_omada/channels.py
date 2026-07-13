"""2.4GHz / 5GHz channel <-> frequency (MHz) tables for `set_radio_channel`.

Confirmed against real hardware on 2026-07-13 (a real OC200 correcting a
fleet of EAPs' channels): 2.4GHz channel 11 -> freq 2462MHz, 5GHz channel
149 -> freq 5745MHz. Both match the `freq = base + 5*channel` formulas
below, which is also the standard IEEE 802.11 channel/frequency
relationship - not just a fit to these two confirmed data points, so it is
applied here to the rest of each band's common channel list too.

WHY THIS TABLE MATTERS (see docs/api-notes.md "set_radio_channel"): the
Omada controller's own `PATCH /eaps/{MAC}` write silently discards a
radioSetting update (`errorCode 0`, "Success.", no actual change) if
`channel` is sent as anything other than a string, or if `freq` is left out
or zero. On the 5GHz radio specifically, `channel` is ALSO persisted back
as an internal index on subsequent reads (e.g. requesting channel 149
stores/echoes back `channel: "17"`) - `freq` is the only reliable
round-trip value there. `set_radio_channel` (guard.py) always derives BOTH
`channel` (string) and `freq` (int MHz) from this table from a single
operator-facing channel number, so neither gotcha can be hit by
construction, and never accepts `freq` directly from a caller.
"""

from __future__ import annotations

from .exceptions import ValidationError

# 2.4GHz: channels 1-13 (channel 14 is Japan-only/DSSS-only and not offered
# by Omada APs in BR/US regulatory domains - excluded). freq = 2412 + 5*(n-1).
CHANNELS_2G: dict[int, int] = {n: 2412 + 5 * (n - 1) for n in range(1, 14)}

# 5GHz: the standard channel plan across UNII-1/2/2e/3 (common to BR/US
# regulatory domains - not necessarily exhaustive of every domain Omada
# ships firmware for). freq = 5000 + 5*channel holds for every channel here
# (confirmed for channel 149 -> 5745 against real hardware; the formula
# itself is the standard IEEE 802.11 relationship, applied to the rest of
# the list).
_CHANNELS_5G = (
    36,
    40,
    44,
    48,  # UNII-1
    52,
    56,
    60,
    64,  # UNII-2 (DFS)
    100,
    104,
    108,
    112,
    116,
    120,
    124,
    128,
    132,
    136,
    140,
    144,  # UNII-2e (DFS)
    149,
    153,
    157,
    161,
    165,  # UNII-3
)
CHANNELS_5G: dict[int, int] = {n: 5000 + 5 * n for n in _CHANNELS_5G}

_TABLES = {"2g": CHANNELS_2G, "5g": CHANNELS_5G}


def channel_to_freq(band: str, channel: int) -> int:
    """Resolve `channel` (the operator-facing channel number) to its
    frequency in MHz for `band` ("2g" or "5g" - normally already validated
    by validation.validate_band, but checked again here defensively since
    this function is also usable standalone). Raises ValidationError for an
    unknown band, or for a channel not in that band's table (listing the
    valid channels so a caller doesn't have to guess)."""
    table = _TABLES.get(band)
    if table is None:
        raise ValidationError(f"band must be '2g' or '5g', got {band!r}.")
    freq = table.get(channel)
    if freq is None:
        valid = ", ".join(str(c) for c in sorted(table))
        raise ValidationError(f"channel {channel!r} is not valid for band {band!r}. Valid channels: {valid}.")
    return freq
