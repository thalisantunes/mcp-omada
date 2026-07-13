"""Central write-guard: allowlist of write operations + read-only gate + confirm mechanics.

Mirrors mcp-mikrotik's `guard.py` exactly in spirit and shape (see that
project's `guard.py` module docstring - this is the same model, applied to
Omada's HTTP write primitive instead of RouterOS's binary API): this module
is the ONLY place in mcp-omada allowed to call `OmadaClient._patch_v2`.
`server.py` never calls it directly - a write tool in server.py always
calls a dedicated function here (`set_radio_channel` below), so there is no
code path through which an LLM (or any tool caller) can reach an arbitrary
API path. Every writable operation is represented by exactly one
`WriteOperation` entry in `ALLOWLIST`, naming the single endpoint it is
allowed to touch.

Two independent controls apply to every write:
  1. Read-only gate: `OMADA_ALLOW_WRITE` must be true (`Settings.allow_write`),
     checked before anything is read or written, regardless of `confirm`.
  2. Confirm/preview: with `confirm=False`, the operation reads the
     device's CURRENT state and returns a before/after preview without
     calling `_patch_v2` at all. Only `confirm=True` applies the change.

To add a new write tool in a future iteration (v0.3 - AP reboot, LED
control, ...):
  1. Add a `WriteOperation` entry to `ALLOWLIST` below.
  2. Add a function here (following `set_radio_channel`'s shape) that
     builds the before/after preview and, when `confirm=True`, applies it
     via `OmadaClient._patch_v2` (or a future write primitive of the same
     "one fixed path, never a caller-supplied one" shape).
  3. Register a corresponding `@mcp.tool()` in server.py that calls it and
     passes `confirm` straight through.
Never add a generic "PATCH this path with this body" tool - each write
operation must stay individually named and reviewable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .channels import channel_to_freq
from .client import _AP_DEVICE_TYPES, OmadaClient
from .config import AuthMode, Settings
from .exceptions import FeatureUnavailableError, GuardViolationError, RadioUnavailableError, WriteDisabledError
from .validation import validate_band


@dataclass(frozen=True)
class WriteOperation:
    name: str
    endpoint: str  # human-readable: the exact fixed HTTP path this operation touches
    method: str  # HTTP method, e.g. "PATCH"
    description: str


ALLOWLIST: dict[str, WriteOperation] = {
    "set_radio_channel": WriteOperation(
        name="set_radio_channel",
        endpoint="/{omadacId}/api/v2/sites/{siteId}/eaps/{MAC}",
        method="PATCH",
        description=(
            "Set an AP's 2.4GHz or 5GHz radio channel. Always derives BOTH `channel` (string) and "
            "`freq` (MHz) from the same operator-facing channel number (channels.channel_to_freq) "
            "and resends the COMPLETE current radioSetting2g/radioSetting5g object with only "
            "channel/freq replaced - never a partial payload, never an int channel, never a missing "
            "freq. See docs/api-notes.md for why the controller silently discards the change "
            "otherwise (errorCode 0, 'Success.', no actual effect)."
        ),
    ),
    # --- v0.3 adds entries here (AP reboot, LED control), each with its own
    # WriteOperation + dedicated function. See module docstring above for
    # the steps. Deliberately NOT added yet:
    #   * AP reboot: no before/after preview is meaningful for a reboot, and
    #     a bad batch reboot across a site has no dry-run or rollback -
    #     mirrors mcp-mikrotik's own reasoning for excluding `system/reboot`
    #     from ITS allowlist (see that project's guard.py).
    #   * Any write to a field OTHER than radioSetting2g/radioSetting5g's
    #     channel/freq (SSID, security/passphrase, LAN port settings, LED,
    #     ...): each needs its own reviewed before/after shape and its own
    #     WriteOperation entry - never bundled into a generic "PATCH this
    #     device" tool.
}


@dataclass(frozen=True)
class WritePreview:
    """Result of a guarded write call: the change it would make (or made).

    `device` is the AP's MAC address - the closest analog here to
    mcp-mikrotik's `WritePreview.device` (a configured fleet device NAME);
    mcp-omada v0.2 has no device registry of its own, so the MAC address a
    caller already used to address the write is what identifies it.
    """

    operation: str
    device: str
    before: dict[str, Any]
    after: dict[str, Any]
    applied: bool
    # Optional risk/caveat callout surfaced alongside before/after - e.g.
    # set_radio_channel sets this for a 5GHz write, so a caller reading only
    # `applied`/`after` still can't miss the confirmed real-hardware
    # channel-persists-as-internal-index behavior (see channels.py). None
    # for a write that carries no such caveat.
    warning: str | None = None


def _require_allowed(settings: Settings, operation_name: str) -> WriteOperation:
    op = ALLOWLIST.get(operation_name)
    if op is None:
        # Defensive only - see module docstring. Every write tool references
        # a fixed ALLOWLIST key, so this should be unreachable in normal use.
        raise GuardViolationError(operation_name)
    if not settings.allow_write:
        raise WriteDisabledError(operation_name)
    return op


_RADIO_FIELD = {"2g": "radioSetting2g", "5g": "radioSetting5g"}

# Confirmed real-hardware caveat (docs/api-notes.md): on 5GHz, `channel` may
# be persisted/echoed back as an internal index (not the requested channel
# number) on a subsequent read - `freq` is the reliable value to verify a
# write actually took effect.
_FIVE_GHZ_CHANNEL_WARNING = (
    "Confirmed real-hardware behavior: on 5GHz, the controller may persist/echo back 'channel' as "
    "an internal index (not this channel number) on subsequent reads - 'freq' ({freq} MHz) is the "
    "reliable value to verify this change took effect."
)


def set_radio_channel(
    client: OmadaClient,
    settings: Settings,
    mac_address: str,
    band: str,
    channel: int,
    confirm: bool,
    site_id: str | None = None,
) -> WritePreview:
    """Set an AP's 2.4GHz ("2g") or 5GHz ("5g") radio channel
    (`PATCH /api/v2/.../eaps/{MAC}`).

    Confirmed real-hardware gotcha (docs/api-notes.md): the controller
    silently discards this write (`errorCode 0`, "Success.", no actual
    change) unless BOTH `channel` is sent as a STRING and `freq` is filled
    in with the matching MHz value, AND the COMPLETE radioSetting object is
    resent - not a partial one. This function always derives channel+freq
    together from `channels.channel_to_freq` (never accepts a raw `freq`
    from a caller) and always reads-then-resends the full CURRENT
    radioSetting object with only channel/freq replaced, so neither gotcha
    can be hit by construction.

    Legacy auth only - this write endpoint is `/api/v2`, not verified under
    the Open API (see README's auth x endpoint matrix). AP/EAP devices
    only; raises `RadioUnavailableError` if the matched device isn't one, or
    is a single-band AP with no `radioSetting<band>` for the requested band
    at all.
    """
    op = _require_allowed(settings, "set_radio_channel")

    if settings.auth_mode is not AuthMode.LEGACY:
        raise FeatureUnavailableError(
            "set_radio_channel",
            "requires legacy login (OMADA_USER/OMADA_PASS) - PATCH /api/v2/.../eaps/{MAC} is a "
            "legacy-only endpoint (not verified under the Open API).",
        )

    validated_band = validate_band(band)
    target_freq = channel_to_freq(validated_band, channel)
    radio_field = _RADIO_FIELD[validated_band]

    row, resolved_site = client.find_device_row(mac_address, site_id)
    device_type = str(row.get("type") or "").lower()
    mac = str(row.get("mac") or mac_address)
    if device_type not in _AP_DEVICE_TYPES:
        raise RadioUnavailableError(mac, validated_band, device_type)

    detail = client._get_v2(f"/sites/{resolved_site}/eaps/{mac}")
    current_radio = detail.get(radio_field)
    if not isinstance(current_radio, dict):
        raise RadioUnavailableError(mac, validated_band, device_type)

    before = dict(current_radio)
    after = dict(current_radio)
    after["channel"] = str(channel)
    after["freq"] = target_freq

    warning = _FIVE_GHZ_CHANNEL_WARNING.format(freq=target_freq) if validated_band == "5g" else None

    if not confirm:
        return WritePreview(operation=op.name, device=mac, before=before, after=after, applied=False, warning=warning)

    client._patch_v2(f"/sites/{resolved_site}/eaps/{mac}", {radio_field: after})
    return WritePreview(operation=op.name, device=mac, before=before, after=after, applied=True, warning=warning)
