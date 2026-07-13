"""MCP server entrypoint.

Registers the v0.1 read-only tool set plus, as of v0.2, the FIRST guarded
write tool (`set_radio_channel`) - see `guard.py`'s module docstring for
the full write-guard model (central allowlist, read-only gate via
`OMADA_ALLOW_WRITE`, confirm/preview), which mirrors mcp-mikrotik's
`guard.py` exactly.

`set_radio_channel` is registered UNCONDITIONALLY here, exactly like
mcp-mikrotik's `set_identity` tool - the read-only gate lives entirely in
`guard._require_allowed`, checked on every call regardless of
`OMADA_ALLOW_WRITE`, not in whether the tool is registered at all. This
mirrors the sibling project's actual (tested) behavior: a write tool is
always callable, and always cleanly refuses with `WriteDisabledError` when
writes are disabled - never silently absent, which would be harder for a
caller (or an LLM) to diagnose than a clear "read-only" error.

Transport is stdio only, exactly like mcp-mikrotik v0 - this process is
meant to run on the operator's own machine, launched by an MCP client (e.g.
Claude Code) over stdio, with no network exposure at all.

TODO(http-transport): if a streamable-http transport is added later, it
MUST default to binding 127.0.0.1 (never 0.0.0.0) and MUST require a bearer
token supplied via an environment variable, checked on every request. Do not
add HTTP transport without both of those in place.
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import guard
from .client import OmadaClient, get_client
from .config import Settings, load_settings
from .exceptions import OmadaMCPError
from .formatting import is_connected, normalize_alert, normalize_client, normalize_device, normalize_radio
from .validation import validate_mac_address

logger = logging.getLogger("mcp_omada")

ClientFactory = Callable[[Settings], OmadaClient]


def build_server(settings: Settings | None = None, client_factory: ClientFactory = get_client) -> FastMCP:
    """Build the FastMCP server and register every tool.

    `settings` and `client_factory` are injectable so tests can run the
    exact tool functions registered here against a fake controller (see
    tests/fakes.py), without touching environment variables or a real
    controller. A single OmadaClient is built lazily on first use and reused
    across calls within one server instance, so the legacy session/Open API
    token is only established once - not per tool call.
    """
    settings = settings or load_settings()
    mcp = FastMCP("omada")

    _client_holder: dict[str, OmadaClient] = {}

    def _client() -> OmadaClient:
        client = _client_holder.get("client")
        if client is None:
            client = client_factory(settings)
            _client_holder["client"] = client
        return client

    def _safe(fn):
        """Make sure nothing unexpected leaks a raw traceback or a secret.

        Deliberately re-raises rather than returning an error dict - see
        mcp-mikrotik's server.py for the identical rationale (FastMCP's own
        error path turns a clean exception into a proper isError tool
        result carrying just its message; OmadaMCPError subclasses in
        exceptions.py are already safe to show a caller).
        """

        @functools.wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except OmadaMCPError:
                raise
            except Exception:
                logger.exception("Unhandled error in tool %s", fn.__name__)
                raise RuntimeError("Internal error handling this tool call; see server logs.") from None

        return inner

    # --- Read tools ---------------------------------------------------

    @mcp.tool()
    @_safe
    def get_controller_info() -> dict[str, Any]:
        """Get the Omada controller's own identity: controller_version,
        omadac_id, configured. Unauthenticated (GET /api/info) - works
        regardless of which auth mode this server is configured with."""
        return _client().get_controller_info()

    @mcp.tool()
    @_safe
    def list_sites() -> list[dict[str, Any]]:
        """List sites managed by this controller: id + name.

        Requires legacy login (OMADA_USER/OMADA_PASS) - see README's auth x
        endpoint matrix for why the Open API path can't serve this in
        v0.1.
        """
        return _client().list_sites()

    @mcp.tool()
    @_safe
    def list_devices(site_id: str | None = None) -> list[dict[str, Any]]:
        """List devices on a site, normalized to one consistent shape
        regardless of auth mode (see formatting.normalize_device):
        connected, uptime_seconds, per-radio wifi_2g/wifi_5g channel info,
        and every raw field this package knows how to normalize - fields
        unavailable in the active auth mode are present as null rather than
        omitted, so a caller never has to branch on auth mode.

        `site_id` defaults to OMADA_SITE_ID, or auto-selects if the
        controller manages exactly one site (legacy auth only).
        """
        client = _client()
        rows = client.list_devices(site_id)
        return [normalize_device(row, client.settings.auth_mode) for row in rows]

    @mcp.tool()
    @_safe
    def get_device_detail(mac: str, site_id: str | None = None) -> dict[str, Any]:
        """Get the richest available detail for one device by MAC address
        (any common separator - colon, hyphen, Cisco-dotted, or bare - is
        accepted and normalized).

        Legacy auth: full detail (ssidOverrides, lanPortSettings,
        ledSetting, ...) for AP/EAP devices via /eaps/{MAC}; other device
        types fall back to their grid/devices summary row (no richer
        verified endpoint yet - see README). Open API auth: the matching
        (reduced-field) row from the device list.

        The normalized fields (see list_devices) are included, plus the
        complete raw response under `raw` so nothing the controller
        returned is lost.
        """
        validated_mac = validate_mac_address(mac)
        client = _client()
        detail_row = client.get_device_detail(validated_mac, site_id)
        normalized = normalize_device(detail_row, client.settings.auth_mode)
        normalized["raw"] = detail_row
        return normalized

    @mcp.tool()
    @_safe
    def get_wifi_summary(site_id: str | None = None, mac: str | None = None) -> list[dict[str, Any]]:
        """Per-AP WiFi summary: 2.4GHz/5GHz channel (parsed from the
        controller's irregularly-formatted actualChannel string), client
        counts per band, and radio utilization. One entry per AP on the
        site, or just the one matching `mac` if given.

        Requires legacy login (OMADA_USER/OMADA_PASS) - the Open API device
        list has no per-radio fields at all (confirmed against real
        hardware; see README's auth x endpoint matrix).
        """
        validated_mac = validate_mac_address(mac) if mac else None
        client = _client()
        rows = client.get_wifi_summary_rows(site_id, validated_mac)
        return [
            {
                "mac": row.get("mac"),
                "name": row.get("name"),
                "connected": is_connected(row, client.settings.auth_mode),
                "client_num": row.get("clientNum"),
                "client_num_2g": row.get("clientNum2g"),
                "client_num_5g": row.get("clientNum5g"),
                "wifi_2g": normalize_radio(row.get("wp2g")),
                "wifi_5g": normalize_radio(row.get("wp5g")),
            }
            for row in rows
        ]

    @mcp.tool()
    @_safe
    def get_clients(site_id: str | None = None) -> list[dict[str, Any]]:
        """List Insight/known clients on a site: mac, name, download/upload
        (bytes), duration_seconds, last_seen_ms, guest/wireless flags, vid
        (VLAN), and block/manager flags. This is the controller's "Insight"
        view (historical + known clients), not just currently-associated
        ones.

        Requires legacy login (OMADA_USER/OMADA_PASS) - no Open API
        equivalent has been verified against real hardware yet.
        """
        client = _client()
        rows = client.get_clients(site_id)
        return [normalize_client(row) for row in rows]

    @mcp.tool()
    @_safe
    def get_alerts(site_id: str | None = None) -> list[dict[str, Any]]:
        """List active alerts on a site.

        The pagination envelope is confirmed against real hardware; the
        shape of an individual alert row is NOT (no alert was active during
        verification) - each entry includes a best-effort module/level/
        content/time guess AND the untouched `raw` row, so nothing is lost
        if the guess is wrong. See docs/api-notes.md.

        Requires legacy login (OMADA_USER/OMADA_PASS) - no Open API
        equivalent has been verified against real hardware yet.
        """
        client = _client()
        rows = client.get_alerts(site_id)
        return [normalize_alert(row) for row in rows]

    # --- Write tools (v0.2+) --------------------------------------------
    # Every write tool must call a dedicated function in guard.py - never
    # OmadaClient._patch_v2 directly. See guard.py's module docstring for
    # how to add the next one.

    @mcp.tool()
    @_safe
    def set_radio_channel(
        mac: str, band: str, channel: int, confirm: bool = False, site_id: str | None = None
    ) -> dict[str, Any]:
        """Set an AP's 2.4GHz ("2g") or 5GHz ("5g") radio channel.

        WRITE tool, guarded: blocked entirely unless the server is running
        with OMADA_ALLOW_WRITE=true. Call with confirm=False (the default)
        to get a before/after preview (channel + freq) without changing
        anything; call again with confirm=True to actually apply it.

        Legacy auth only. `channel` is the operator-facing channel number
        (e.g. 11 on 2.4GHz, 149 on 5GHz) - this tool always derives the
        matching frequency itself (channels.py) and resends the complete
        current radio configuration with only channel/freq changed, so the
        confirmed real-hardware silent-discard gotcha (int channel, or a
        missing freq) can't be hit by construction - see docs/api-notes.md.
        """
        validated_mac = validate_mac_address(mac)
        client = _client()
        preview = guard.set_radio_channel(
            client, settings, mac_address=validated_mac, band=band, channel=channel, confirm=confirm, site_id=site_id
        )
        return asdict(preview)

    return mcp


def main() -> None:  # pragma: no cover - process entrypoint; server.run()
    # blocks on stdio for the life of the process, so this is exercised by
    # actually running `mcp-omada`/`python -m mcp_omada.server`, not by the
    # test suite. build_server() (everything up to the blocking run() call)
    # is exactly what tests/test_server.py exercises instead.
    logging.basicConfig(level=os.environ.get("OMADA_LOG_LEVEL", "INFO"))
    server = build_server()
    server.run()


if __name__ == "__main__":  # pragma: no cover - see main() above
    main()
