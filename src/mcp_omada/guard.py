"""Central write-guard: allowlist of write operations + read-only gate +
confirm mechanics + empirical re-read verification + audit journal.

Follows the same security model mcp-mikrotik's `guard.py` established -
studied before this module was written - adapted to Omada's HTTP write
primitive and, distinctly, to a controller that can answer `errorCode 0`
("Success.") without actually applying a write (see docs/api-notes.md's
confirmed silent-discard gotcha). This module is the ONLY place in
mcp-omada allowed to call `OmadaClient._patch_v2`. `server.py` never calls
it directly - a write tool in server.py always calls a dedicated function
here (`set_radio_channel` below), so there is no code path through which an
LLM (or any tool caller) can reach an arbitrary API path. Every writable
operation is represented by exactly one `WriteOperation` entry in
`ALLOWLIST`, naming the single endpoint it is allowed to touch.

Four independent controls apply to every write:
  1. Read-only gate: `OMADA_ALLOW_WRITE` must be true (`Settings.allow_write`),
     checked before anything is read or written, regardless of `confirm`.
  2. Confirm/preview: with `confirm=False`, the operation reads the
     device's CURRENT state and returns a before/after preview without
     calling `_patch_v2` at all. Only `confirm=True` applies the change.
  3. Empirical re-read verification (this package's own addition - RouterOS's
     API doesn't need it, since it doesn't answer "success" for a write it
     silently discarded). After a confirmed PATCH, `set_radio_channel`
     re-reads the device and compares `freq` - the one field confirmed
     reliable on both bands (docs/api-notes.md) - against what was
     requested. `applied=True` is returned ONLY when that comparison
     matches. A controller that answers `errorCode 0` but leaves `freq`
     unchanged (e.g. an uncharacterized rejection - a DFS channel the
     firmware refuses, say - beyond the two known causes `channel`-as-
     string/`freq`-filled-in already rule out by construction) is reported
     as `applied=False` with a clear `message`, never a false positive.
  4. Audit journal: every call - preview, applied, rejected, or error - emits
     exactly one structured event via `audit.record()` (see `_audited`
     below and `audit.py`'s own module docstring), carrying a correlation
     id, the target MAC, before/after, and outcome. Never includes a
     credential - `audit.py`'s own redaction is a second, independent line
     of defense on top of the fact that no radioSetting field is ever one.

To add a new write tool in a future iteration (v0.3 - AP reboot, LED
control, ...):
  1. Add a `WriteOperation` entry to `ALLOWLIST` below.
  2. Add a function here (following `set_radio_channel`'s shape, decorated
     with `@_audited(...)`) that builds the before/after preview and, when
     `confirm=True`, applies it via `OmadaClient._patch_v2` (or a future
     write primitive of the same "one fixed path, never a caller-supplied
     one" shape) - and, if the endpoint's success response can't be trusted
     on its own, verifies the result the same empirical way.
  3. Register a corresponding `@mcp.tool()` in server.py that calls it and
     passes `confirm` straight through.
Never add a generic "PATCH this path with this body" tool - each write
operation must stay individually named and reviewable.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import audit
from .channels import channel_to_freq
from .client import _AP_DEVICE_TYPES, OmadaClient
from .config import AuthMode, Settings
from .correlation import current as current_correlation_id
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
            "freq. The result is then verified empirically (re-read + freq comparison), not just "
            "trusted from errorCode 0 - see docs/api-notes.md."
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

    `after` reflects different things depending on the call: for a
    `confirm=False` preview, the INTENDED state (nothing was touched, so
    there is nothing to re-read yet). For a `confirm=True` call, the
    ACTUAL post-write state as re-read from the device - whether or not it
    verified (`applied`) - never merely the intended state, since that is
    exactly the "trust errorCode 0" gap this package's re-read verification
    exists to close.
    """

    operation: str
    device: str
    before: dict[str, Any]
    after: dict[str, Any]
    applied: bool
    # Optional risk/caveat callout surfaced alongside before/after - e.g.
    # every set_radio_channel write carries a client-disruption note, and a
    # 5GHz write additionally carries the confirmed real-hardware
    # channel-persists-as-internal-index behavior (see channels.py). None
    # for a write that carries no such caveat.
    warning: str | None = None
    # Set ONLY when a confirm=True write's post-write re-read did NOT
    # confirm the intended change (applied=False in that case) - a clear,
    # human-readable explanation of what was verified and what didn't
    # match. None on a preview (nothing was attempted) and None on a
    # verified-applied write (nothing to explain).
    message: str | None = None


def _require_allowed(settings: Settings, operation_name: str) -> WriteOperation:
    op = ALLOWLIST.get(operation_name)
    if op is None:
        # Defensive only - see module docstring. Every write tool references
        # a fixed ALLOWLIST key, so this should be unreachable in normal use.
        raise GuardViolationError(operation_name)
    if not settings.allow_write:
        raise WriteDisabledError(operation_name)
    return op


def _audited(anchor_operation: str) -> Callable[[Callable[..., WritePreview]], Callable[..., WritePreview]]:
    """Decorator applied to every public write function below (audit
    journal - see audit.py's module docstring).

    Ensures exactly one `audit.record()` call per invocation, regardless of
    how it ends:
      - Returns a `WritePreview` with `applied=False` and no `message`
        (nothing was attempted, `confirm=False`) -> outcome "preview".
      - Returns a `WritePreview` with `applied=True` (the post-write re-read
        confirmed the change) -> outcome "applied".
      - Returns a `WritePreview` with `applied=False` after a real write
        attempt (`confirm=True`, but the re-read did NOT confirm it) ->
        outcome "rejected" - see module docstring's control #3. mcp-mikrotik
        has no equivalent of this outcome; see audit.py's module docstring
        for why Omada's controller needs it and RouterOS's doesn't.
      - Raises anything -> outcome "error" (`WriteDisabledError` from the
        read-only gate, `ValidationError`, `DeviceNotFoundError`,
        `RadioUnavailableError`, a device-side `ControllerCommandError` -
        all of it, however early it happens).

    `anchor_operation` is the `ALLOWLIST` key to report as `operation`/
    `action` - this package has no dynamic-dispatch write (unlike
    mcp-mikrotik's `set_wifi_ssid`/`set_client_bandwidth`, which resolve
    between two candidate operations at runtime), so unlike mcp-mikrotik's
    `_audited` this one anchor is also always the FINAL operation reported.

    `device` for the journal is read directly from the call's own
    `mac_address` keyword argument (always present - every guard.py write
    function takes it, and every caller, server.py included, passes it by
    keyword - see WritePreview's own docstring for why there is no
    `client.device.name` to fall back on here, unlike mcp-mikrotik).

    This is the ONLY place in the package that calls `audit.record()` -
    keeping every write's audit trail centralized here means a future write
    function only has to follow the existing `@_audited(...)` +
    `_require_allowed` shape to be covered automatically. Writing the
    journal never affects the call's own outcome: `audit.record()` is
    itself best-effort (see audit.py) and never raises.
    """

    def decorator(fn: Callable[..., WritePreview]) -> Callable[..., WritePreview]:
        @functools.wraps(fn)
        def inner(client: OmadaClient, settings: Settings, *args: Any, confirm: bool, **kwargs: Any) -> WritePreview:
            correlation_id = current_correlation_id()
            device = str(kwargs.get("mac_address") or (args[0] if args else "<unknown>"))
            op = ALLOWLIST.get(anchor_operation)
            action = op.method if op is not None else "<unknown>"
            try:
                result = fn(client, settings, *args, confirm=confirm, **kwargs)
            except Exception as exc:
                audit.record(
                    correlation_id=correlation_id,
                    device=device,
                    tool=fn.__name__,
                    operation=anchor_operation,
                    action=action,
                    confirm=confirm,
                    outcome="error",
                    summary={"error": str(exc)},
                )
                raise
            if result.applied:
                outcome = "applied"
            elif confirm:
                outcome = "rejected"
            else:
                outcome = "preview"
            audit.record(
                correlation_id=correlation_id,
                device=result.device,
                tool=fn.__name__,
                operation=result.operation,
                action=action,
                confirm=confirm,
                outcome=outcome,
                summary={
                    "before": result.before,
                    "after": result.after,
                    "warning": result.warning,
                    "message": result.message,
                },
            )
            return result

        return inner

    return decorator


_RADIO_FIELD = {"2g": "radioSetting2g", "5g": "radioSetting5g"}

# Every channel change restarts the radio - clients associated on that band
# momentarily disconnect and have to reassociate. Surfaced on EVERY
# set_radio_channel write (both bands), not just the 5GHz-specific caveat
# below - an agent reading only before/after must not miss that this write
# is disruptive, the same way mcp-mikrotik's disable_route callout makes a
# default-route write's traffic impact impossible to miss from before/after
# alone.
_CLIENT_DISRUPTION_WARNING = (
    "Changing a radio's channel restarts it - clients currently associated on the {band_label} band "
    "will momentarily disconnect and have to reassociate."
)

# Confirmed real-hardware caveat (docs/api-notes.md): on 5GHz, `channel` may
# be persisted/echoed back as an internal index (not the requested channel
# number) on a subsequent read - `freq` is the reliable value to verify a
# write actually took effect. (This package's own re-read verification
# already checks `freq`, not `channel`, for exactly this reason - see
# control #3 above - this warning exists so a caller inspecting `after`
# directly isn't confused by seeing a `channel` value that doesn't match
# what they asked for.)
_FIVE_GHZ_INDEX_WARNING = (
    "Confirmed real-hardware behavior: on 5GHz, the controller may persist/echo back 'channel' as "
    "an internal index (not this channel number) on subsequent reads - 'freq' ({freq} MHz) is the "
    "reliable value that was actually verified."
)

_BAND_LABELS = {"2g": "2.4GHz", "5g": "5GHz"}


def _build_warning(band: str, freq: int) -> str:
    parts = [_CLIENT_DISRUPTION_WARNING.format(band_label=_BAND_LABELS[band])]
    if band == "5g":
        parts.append(_FIVE_GHZ_INDEX_WARNING.format(freq=freq))
    return " ".join(parts)


@_audited("set_radio_channel")
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

    Confirmed real-hardware gotcha (docs/api-notes.md): the controller can
    silently discard this write (`errorCode 0`, "Success.", no actual
    change) unless BOTH `channel` is sent as a STRING and `freq` is filled
    in with the matching MHz value, AND the COMPLETE radioSetting object is
    resent - not a partial one. This function always derives channel+freq
    together from `channels.channel_to_freq` (never accepts a raw `freq`
    from a caller) and always reads-then-resends the full CURRENT
    radioSetting object with only channel/freq replaced, so neither of
    those two known causes can be hit by construction.

    That construction-level defense only covers the causes already known,
    though - `errorCode 0` on its own is not proof the change actually took
    effect (the controller could, for an uncharacterized reason - a DFS
    channel the firmware refuses, say - answer "Success" and still not
    apply it). So after a confirmed write, this function re-reads the
    device and compares the resulting `freq` (the one field confirmed
    reliable on both bands) against what was requested: `applied=True` is
    returned ONLY when they match. Otherwise `applied=False` is returned
    with `message` explaining the mismatch - never a false positive.

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
    intended_after = dict(current_radio)
    intended_after["channel"] = str(channel)
    intended_after["freq"] = target_freq

    warning = _build_warning(validated_band, target_freq)

    if not confirm:
        return WritePreview(
            operation=op.name, device=mac, before=before, after=intended_after, applied=False, warning=warning
        )

    client._patch_v2(f"/sites/{resolved_site}/eaps/{mac}", {radio_field: intended_after})

    # Empirical re-read verification (control #3 above): errorCode 0 alone
    # is NOT trusted. Re-read and compare the one field confirmed reliable
    # on both bands - freq - against what was requested.
    post_detail = client._get_v2(f"/sites/{resolved_site}/eaps/{mac}")
    post_radio = post_detail.get(radio_field)
    actual_after = dict(post_radio) if isinstance(post_radio, dict) else dict(before)
    actual_freq = actual_after.get("freq")

    if actual_freq == target_freq:
        return WritePreview(
            operation=op.name, device=mac, before=before, after=actual_after, applied=True, warning=warning
        )

    message = (
        f"Controller accepted the write (errorCode 0) but the change was NOT confirmed on re-read: "
        f"freq is still {actual_freq!r} (expected {target_freq})."
    )
    return WritePreview(
        operation=op.name,
        device=mac,
        before=before,
        after=actual_after,
        applied=False,
        warning=warning,
        message=message,
    )
