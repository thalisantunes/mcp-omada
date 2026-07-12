"""MCP server entrypoint.

Registers the v0.1 read-only tool set. There is no write tool at all yet -
see README's "Roadmap" and client.py's module docstring for what a future
v0.2 guarded-write layer (following mcp-mikrotik's guard.py ALLOWLIST
pattern) would need to handle first (the channel/freq write gotcha
documented in formatting.parse_channel, in particular).

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
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import OmadaClient, get_client
from .config import Settings, load_settings
from .exceptions import OmadaMCPError
from .formatting import is_connected, normalize_device, normalize_radio
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
