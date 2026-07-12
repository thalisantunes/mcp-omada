"""Shared response-shaping helpers for read tools.

The Omada controller answers the SAME logical question ("is this device
connected?", "how long has it been up?", "what channel is this radio on?")
with DIFFERENT shapes depending on which auth path served the request - see
client.py's module docstring for why legacy (/api/v2) and Open API
(/openapi/v1) never mix. Every quirk normalized here was confirmed against a
real OC200 v5.13.30.20 on 2026-07-12 - see docs/api-notes.md.

Kept in one place so no two tools reimplement the same "status 14 vs status
1 means connected" trap, or the "actualChannel is a string with irregular
whitespace, not a number" parsing.
"""

from __future__ import annotations

import re
from typing import Any

from .config import AuthMode

# --- connected -------------------------------------------------------------


def is_connected(row: dict[str, Any], mode: AuthMode) -> bool | None:
    """Whether a device row represents a currently-connected device.

    GOTCHA (confirmed against real hardware): the SAME integer field means
    different things on each auth path.
      - legacy (/api/v2/.../grid/devices): `statusCategory` is the primary
        signal (1 == connected); if it is absent, fall back to the legacy
        `status` field, where 14 == connected (statusCategory is a coarser,
        more stable classification RouterOS-style `status` codes drift
        across firmware; prefer it when present).
      - openapi (/openapi/v1/.../devices): there is no `statusCategory` at
        all - `status` itself means 1 == connected here, a DIFFERENT
        semantic than legacy `status`==14. Never share one code path that
        treats `status` the same way across both modes.

    Returns None if neither field is present (rather than guessing True/False).
    """
    if mode is AuthMode.OPENAPI:
        status = row.get("status")
        return None if status is None else int(status) == 1

    status_category = row.get("statusCategory")
    if status_category is not None:
        return int(status_category) == 1
    status = row.get("status")
    return None if status is None else int(status) == 14


# --- uptime ------------------------------------------------------------

_UPTIME_PART = re.compile(r"(\d+)\s*([dhms])", re.IGNORECASE)
_UPTIME_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_uptime_string(value: str | None) -> int | None:
    """Best-effort parse of an Omada `uptime` string like "1h 43m" or "3d 5h
    20m" into whole seconds. Returns None if `value` is empty or contains no
    recognizable "<number><d|h|m|s>" part.

    Only used as a FALLBACK when the richer `uptimeLong` field (seconds,
    already an int) is unavailable - notably the Open API device list, which
    only ever returns the string form (confirmed against real hardware -
    see docs/api-notes.md).
    """
    if not value:
        return None
    matches = _UPTIME_PART.findall(value)
    if not matches:
        return None
    total = 0
    for amount, unit in matches:
        total += int(amount) * _UPTIME_UNIT_SECONDS[unit.lower()]
    return total


def uptime_seconds(row: dict[str, Any]) -> int | None:
    """Normalized uptime in seconds: prefers `uptimeLong` (legacy-only, an
    int already in seconds) and falls back to parsing the `uptime` string
    (present on both auth paths) when it is absent."""
    uptime_long = row.get("uptimeLong")
    if uptime_long is not None:
        try:
            return int(uptime_long)
        except (TypeError, ValueError):
            pass
    return parse_uptime_string(row.get("uptime"))


# --- wifi channel ------------------------------------------------------

# Confirmed shape (real hardware): "11  / 2462MHz" - irregular whitespace
# around the slash, no space before "MHz". Deliberately tolerant of any
# amount of whitespace in either gap rather than matching that exact
# spacing, since it is very unlikely to be a documented API contract.
_CHANNEL_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*mhz\s*$", re.IGNORECASE)


def parse_channel(actual_channel: str | None) -> dict[str, Any]:
    """Parse a radio's `actualChannel` field (e.g. "11  / 2462MHz") into
    `{"channel": 11, "freq_mhz": 2462, "raw": "11  / 2462MHz"}`.

    GOTCHA (confirmed against real hardware, see docs/api-notes.md): on the
    5GHz radio the `channel` half of this string is an internal index, not
    the channel number an operator would recognize (e.g. "17" for what the
    UI calls channel 149) - `freq_mhz` is the reliable value on 5GHz, and is
    also what set_radio_channel's future write-tool counterpart (v0.2) will
    need to set instead of `channel` to avoid RouterOS-style silent-discard
    behavior. `channel`/`freq_mhz` are None (not 0) when `actual_channel`
    doesn't match the expected shape, so a caller can tell "unparseable"
    apart from "channel zero".
    """
    if not actual_channel:
        return {"channel": None, "freq_mhz": None, "raw": actual_channel}
    match = _CHANNEL_RE.match(actual_channel)
    if not match:
        return {"channel": None, "freq_mhz": None, "raw": actual_channel}
    return {"channel": int(match.group(1)), "freq_mhz": int(match.group(2)), "raw": actual_channel}


def normalize_radio(radio: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize one of a legacy device row's `wp2g`/`wp5g` sub-objects into
    a flatter, parsed shape. Returns None if the radio object itself is
    absent (e.g. a single-band AP has no `wp5g`, or the Open API list never
    includes either at all - see README's auth x endpoint matrix)."""
    if radio is None:
        return None
    parsed = parse_channel(radio.get("actualChannel"))
    return {
        "channel": parsed["channel"],
        "freq_mhz": parsed["freq_mhz"],
        "raw_channel": parsed["raw"],
        "tx_power": radio.get("txPower"),
        "band_width": radio.get("bandWidth"),
        "radio_mode": radio.get("rdMode"),
        "tx_util": radio.get("txUtil"),
        "rx_util": radio.get("rxUtil"),
        "inter_util": radio.get("interUtil"),
    }


# --- device row normalization -------------------------------------------


def normalize_device(row: dict[str, Any], mode: AuthMode) -> dict[str, Any]:
    """Normalize one device row from either auth path into one consistent
    shape. Fields that only exist on one auth path are present (as None)
    on the other too, so a caller never has to branch on which mode is
    active - see README's auth x endpoint matrix for exactly which raw
    field backs each one.
    """
    return {
        "name": row.get("name"),
        "type": row.get("type"),
        "mac": row.get("mac"),
        "ip": row.get("ip"),
        "model": row.get("model"),
        "compound_model": row.get("compoundModel"),
        "firmware_version": row.get("firmwareVersion"),
        "need_upgrade": row.get("needUpgrade"),
        "connected": is_connected(row, mode),
        "status_raw": row.get("status"),
        "status_category_raw": row.get("statusCategory"),
        "uptime_seconds": uptime_seconds(row),
        "uptime_raw": row.get("uptime"),
        "cpu_util": row.get("cpuUtil"),
        "mem_util": row.get("memUtil"),
        "client_num": row.get("clientNum"),
        "client_num_2g": row.get("clientNum2g"),
        "client_num_5g": row.get("clientNum5g"),
        "tx_rate": row.get("txRate"),
        "rx_rate": row.get("rxRate"),
        "upload": row.get("upload"),
        "download": row.get("download"),
        "sn": row.get("sn"),
        "last_seen": row.get("lastSeen"),
        "wifi_2g": normalize_radio(row.get("wp2g")),
        "wifi_5g": normalize_radio(row.get("wp5g")),
        "auth_mode": mode.value,
    }
