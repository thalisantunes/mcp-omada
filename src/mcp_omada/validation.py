"""Input validation applied before any value is sent to the controller.

Currently used for the `get_device_detail`/`get_wifi_summary` tools' `mac`
parameter. Note this is not primarily an injection defense: every request
this package makes goes through httpx as structured URL path segments/JSON
bodies, never string-concatenated shell/SQL, so injection through a bad MAC
is already ruled out by construction (see client.py). This module exists to
(a) reject garbage input early with a clear error instead of forwarding it
to the controller, and (b) normalize whatever separator/case a caller used
into the hyphenated-uppercase form Omada's own API returns (e.g.
"50-D4-F7-66-0D-9C" - confirmed against real hardware), so a caller
comparing/looking up a device by MAC doesn't have to worry about matching
the controller's exact formatting.
"""

from __future__ import annotations

import re

from .exceptions import ValidationError

# Accepts colon-, hyphen-, dot- (Cisco-style, e.g. "50d4.f766.0d9c") or
# bare-separated MAC addresses, case-insensitive.
_MAC_COLON_OR_HYPHEN = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")
_MAC_DOTTED = re.compile(r"^[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}$")
_MAC_BARE = re.compile(r"^[0-9A-Fa-f]{12}$")


def validate_mac_address(mac_address: str) -> str:
    """Validate `mac_address` and normalize it to Omada's own hyphenated,
    uppercase form (e.g. "50-D4-F7-66-0D-9C"). Raises ValidationError if it
    doesn't match any recognized MAC shape."""
    if not isinstance(mac_address, str) or not mac_address.strip():
        raise ValidationError("MAC address must be a non-empty string.")

    candidate = mac_address.strip()

    if _MAC_COLON_OR_HYPHEN.match(candidate):
        hex_digits = re.sub(r"[:-]", "", candidate)
    elif _MAC_DOTTED.match(candidate):
        hex_digits = candidate.replace(".", "")
    elif _MAC_BARE.match(candidate):
        hex_digits = candidate
    else:
        raise ValidationError(f"MAC address {mac_address!r} is not a valid MAC address.")

    hex_digits = hex_digits.upper()
    return "-".join(hex_digits[i : i + 2] for i in range(0, 12, 2))
