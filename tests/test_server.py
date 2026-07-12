"""Server-level smoke tests: tool registration, and each tool's happy/error
paths driven through FastMCP's own call_tool (not by calling the plain
Python functions directly), against a fake controller only.
"""

from __future__ import annotations

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_omada.client import OmadaClient
from mcp_omada.config import Settings
from mcp_omada.server import build_server

from .fakes import FakeOmadaController, RaisingTransport

EXPECTED_TOOLS = {
    "get_controller_info",
    "list_sites",
    "list_devices",
    "get_device_detail",
    "get_wifi_summary",
}


def _factory(transport: httpx.MockTransport):
    def factory(settings: Settings) -> OmadaClient:
        return OmadaClient(settings, transport=transport)

    return factory


@pytest.mark.asyncio
async def test_all_expected_tools_are_registered(settings_legacy: Settings):
    mcp = build_server(settings=settings_legacy, client_factory=lambda s: None)
    tools = await mcp.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_get_controller_info_happy_path(settings_legacy: Settings, transport: httpx.MockTransport):
    mcp = build_server(settings=settings_legacy, client_factory=_factory(transport))
    _content, result = await mcp.call_tool("get_controller_info", {})
    assert result["controller_version"] == "5.13.30.20"


@pytest.mark.asyncio
async def test_list_sites_happy_path(
    settings_legacy: Settings, transport: httpx.MockTransport, fake_controller: FakeOmadaController
):
    mcp = build_server(settings=settings_legacy, client_factory=_factory(transport))
    _content, result = await mcp.call_tool("list_sites", {})
    assert result["result"] == [{"id": fake_controller.site_id, "name": fake_controller.site_name}]


@pytest.mark.asyncio
async def test_list_sites_openapi_mode_returns_clean_error(settings_openapi: Settings, transport: httpx.MockTransport):
    mcp = build_server(settings=settings_openapi, client_factory=_factory(transport))
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("list_sites", {})
    assert "legacy" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_list_devices_legacy_is_normalized(
    settings_legacy: Settings, transport: httpx.MockTransport, fake_controller: FakeOmadaController
):
    mcp = build_server(settings=settings_legacy, client_factory=_factory(transport))
    _content, result = await mcp.call_tool("list_devices", {"site_id": fake_controller.site_id})
    rows = result["result"]
    assert len(rows) == 3
    ap = next(r for r in rows if r["mac"] == "50-D4-F7-66-0D-9C")
    assert ap["connected"] is True
    assert ap["uptime_seconds"] == 6180
    assert ap["wifi_2g"]["channel"] == 11
    assert ap["auth_mode"] == "legacy"


@pytest.mark.asyncio
async def test_list_devices_openapi_is_normalized_with_null_legacy_fields(
    settings_openapi: Settings, transport: httpx.MockTransport, fake_controller: FakeOmadaController
):
    mcp = build_server(settings=settings_openapi, client_factory=_factory(transport))
    _content, result = await mcp.call_tool("list_devices", {"site_id": fake_controller.site_id})
    rows = result["result"]
    ap = next(r for r in rows if r["mac"] == "50-D4-F7-66-0D-9C")
    assert ap["connected"] is True
    assert ap["wifi_2g"] is None
    assert ap["client_num"] is None
    assert ap["auth_mode"] == "openapi"


@pytest.mark.asyncio
async def test_get_device_detail_happy_path_includes_raw(
    settings_legacy: Settings, transport: httpx.MockTransport, fake_controller: FakeOmadaController
):
    mcp = build_server(settings=settings_legacy, client_factory=_factory(transport))
    _content, result = await mcp.call_tool(
        "get_device_detail", {"mac": "50:d4:f7:66:0d:9c", "site_id": fake_controller.site_id}
    )
    assert result["mac"] == "50-D4-F7-66-0D-9C"
    assert result["raw"]["ledSetting"] == {"enable": True}


@pytest.mark.asyncio
async def test_get_device_detail_invalid_mac_rejected_before_touching_device(settings_legacy: Settings):
    mcp = build_server(
        settings=settings_legacy, client_factory=_factory(httpx.MockTransport(RaisingTransport().handle_request))
    )
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("get_device_detail", {"mac": "not-a-mac"})
    assert "not a valid" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_device_detail_unknown_device_returns_clean_error(
    settings_legacy: Settings, transport: httpx.MockTransport, fake_controller: FakeOmadaController
):
    mcp = build_server(settings=settings_legacy, client_factory=_factory(transport))
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("get_device_detail", {"mac": "AA-BB-CC-DD-EE-FF", "site_id": fake_controller.site_id})
    assert "not found" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_get_wifi_summary_happy_path(
    settings_legacy: Settings, transport: httpx.MockTransport, fake_controller: FakeOmadaController
):
    mcp = build_server(settings=settings_legacy, client_factory=_factory(transport))
    _content, result = await mcp.call_tool("get_wifi_summary", {"site_id": fake_controller.site_id})
    rows = result["result"]
    assert len(rows) == 2
    ap = next(r for r in rows if r["mac"] == "50-D4-F7-66-0D-9C")
    assert ap["wifi_2g"]["channel"] == 11
    assert ap["wifi_5g"]["freq_mhz"] == 5745
    assert ap["client_num_2g"] == 2


@pytest.mark.asyncio
async def test_get_wifi_summary_filters_by_mac(
    settings_legacy: Settings, transport: httpx.MockTransport, fake_controller: FakeOmadaController
):
    mcp = build_server(settings=settings_legacy, client_factory=_factory(transport))
    _content, result = await mcp.call_tool(
        "get_wifi_summary", {"site_id": fake_controller.site_id, "mac": "50-D4-F7-66-0D-9D"}
    )
    rows = result["result"]
    assert len(rows) == 1
    assert rows[0]["connected"] is False


@pytest.mark.asyncio
async def test_get_wifi_summary_openapi_mode_returns_clean_error(
    settings_openapi: Settings, transport: httpx.MockTransport
):
    mcp = build_server(settings=settings_openapi, client_factory=_factory(transport))
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("get_wifi_summary", {})
    assert "legacy" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_client_is_built_once_and_reused_across_tool_calls(
    settings_legacy: Settings, transport: httpx.MockTransport
):
    calls = {"count": 0}

    def counting_factory(settings: Settings) -> OmadaClient:
        calls["count"] += 1
        return OmadaClient(settings, transport=transport)

    mcp = build_server(settings=settings_legacy, client_factory=counting_factory)
    await mcp.call_tool("get_controller_info", {})
    await mcp.call_tool("get_controller_info", {})
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_unhandled_exception_becomes_generic_internal_error(settings_legacy: Settings):
    def _boom(settings: Settings) -> OmadaClient:
        raise RuntimeError("boom - some unexpected failure")

    mcp = build_server(settings=settings_legacy, client_factory=_boom)
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("get_controller_info", {})
    assert "internal error" in str(exc_info.value).lower()
    assert "boom" not in str(exc_info.value).lower()
