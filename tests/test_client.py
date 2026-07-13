from __future__ import annotations

import httpx
import pytest

from mcp_omada.client import OmadaClient, _looks_like_auth_failure, _unwrap, get_client
from mcp_omada.config import AuthMode, Settings
from mcp_omada.exceptions import (
    AuthenticationError,
    ConfigError,
    ControllerCommandError,
    ControllerConnectionError,
    DeviceNotFoundError,
    FeatureUnavailableError,
    SiteAmbiguousError,
)

from .fakes import FakeOmadaController, RaisingTransport

# --- get_controller_info / omadacId discovery -----------------------------


def test_get_controller_info_is_unauthenticated(legacy_client: OmadaClient, fake_controller: FakeOmadaController):
    info = legacy_client.get_controller_info()
    assert info == {"controller_version": "5.13.30.20", "omadac_id": fake_controller.omadac_id, "configured": True}
    assert fake_controller.legacy_login_calls == 0


def test_omadac_id_auto_discovered_and_cached(fake_controller: FakeOmadaController, transport: httpx.MockTransport):
    settings = Settings(
        base_url="https://omada.example.test",
        auth_mode=AuthMode.LEGACY,
        omadac_id=None,
        username=fake_controller.legacy_username,
        password=fake_controller.legacy_password,
    )
    client = OmadaClient(settings, transport=transport)
    client.list_sites()
    assert fake_controller.info_calls == 1
    client.list_sites()
    assert fake_controller.info_calls == 1  # cached, not re-fetched


def test_omadac_id_discovery_failure_raises_connection_error(transport_returning_bad_info):
    settings = Settings(base_url="https://omada.example.test", auth_mode=AuthMode.LEGACY, username="a", password="b")
    client = OmadaClient(settings, transport=transport_returning_bad_info)
    with pytest.raises(ControllerConnectionError):
        client.list_sites()


# --- legacy login -----------------------------------------------------


def test_legacy_login_success_sets_csrf_and_cookie(legacy_client: OmadaClient, fake_controller: FakeOmadaController):
    sites = legacy_client.list_sites()
    assert sites == [{"id": fake_controller.site_id, "name": fake_controller.site_name}]
    assert fake_controller.legacy_login_calls == 1


def test_legacy_login_wrong_credentials_raises(fake_controller: FakeOmadaController, transport: httpx.MockTransport):
    fake_controller.reject_legacy_credentials = True
    settings = Settings(
        base_url="https://omada.example.test",
        auth_mode=AuthMode.LEGACY,
        omadac_id=fake_controller.omadac_id,
        username="admin",
        password="wrong",
    )
    client = OmadaClient(settings, transport=transport)
    with pytest.raises(AuthenticationError):
        client.list_sites()


def test_legacy_login_missing_credentials_raises_without_network_call():
    settings = Settings(base_url="https://omada.example.test", auth_mode=AuthMode.LEGACY)
    client = OmadaClient(settings, transport=RaisingTransport())
    with pytest.raises(FeatureUnavailableError):
        client._ensure_legacy_login()


def test_legacy_session_expiry_triggers_one_automatic_relogin(
    legacy_client: OmadaClient, fake_controller: FakeOmadaController
):
    legacy_client.list_sites()  # establish session
    assert fake_controller.legacy_login_calls == 1

    fake_controller.expire_next_legacy_call = True
    devices = legacy_client.list_devices(fake_controller.site_id)

    assert len(devices) == len(fake_controller.devices)
    assert fake_controller.legacy_login_calls == 2  # re-authenticated exactly once


def test_legacy_generic_error_is_not_retried_as_auth_failure(
    legacy_client: OmadaClient, fake_controller: FakeOmadaController
):
    with pytest.raises(ControllerCommandError) as exc_info:
        legacy_client._get_v2(f"/sites/{fake_controller.site_id}/eaps/AA-BB-CC-DD-EE-FF")
    assert exc_info.value.error_code == -404
    assert fake_controller.legacy_login_calls == 1  # no extra re-login attempted


# --- Open API token -----------------------------------------------------


def test_openapi_token_success(openapi_client: OmadaClient, fake_controller: FakeOmadaController):
    rows = openapi_client.list_devices(fake_controller.site_id)
    assert len(rows) == len(fake_controller.devices)
    assert fake_controller.openapi_token_calls == 1


def test_openapi_wrong_credentials_raises(fake_controller: FakeOmadaController, transport: httpx.MockTransport):
    fake_controller.reject_openapi_credentials = True
    settings = Settings(
        base_url="https://omada.example.test",
        auth_mode=AuthMode.OPENAPI,
        omadac_id=fake_controller.omadac_id,
        site_id=fake_controller.site_id,
        client_id="cid",
        client_secret="wrong",
    )
    client = OmadaClient(settings, transport=transport)
    with pytest.raises(AuthenticationError):
        client.list_devices()


def test_openapi_missing_credentials_raises_without_network_call():
    settings = Settings(base_url="https://omada.example.test", auth_mode=AuthMode.OPENAPI, omadac_id="oid")
    client = OmadaClient(settings, transport=RaisingTransport())
    with pytest.raises(FeatureUnavailableError):
        client._ensure_openapi_token()


def test_openapi_token_expiry_triggers_one_automatic_refresh(
    openapi_client: OmadaClient, fake_controller: FakeOmadaController
):
    openapi_client.list_devices(fake_controller.site_id)
    assert fake_controller.openapi_token_calls == 1

    fake_controller.expire_next_openapi_call = True
    rows = openapi_client.list_devices(fake_controller.site_id)

    assert len(rows) == len(fake_controller.devices)
    assert fake_controller.openapi_token_calls == 2


# --- list_sites / resolve_site_id -----------------------------------------


def test_list_sites_requires_legacy_auth(openapi_client: OmadaClient):
    with pytest.raises(FeatureUnavailableError):
        openapi_client.list_sites()


def test_list_sites_paginates(fake_controller: FakeOmadaController, transport: httpx.MockTransport):
    fake_controller.extra_sites = [{"id": f"site-{i}", "name": f"Site {i}"} for i in range(15)]
    settings = Settings(
        base_url="https://omada.example.test",
        auth_mode=AuthMode.LEGACY,
        omadac_id=fake_controller.omadac_id,
        username=fake_controller.legacy_username,
        password=fake_controller.legacy_password,
    )
    client = OmadaClient(settings, transport=transport)
    sites = client.list_sites()
    assert len(sites) == 16
    assert {s["id"] for s in sites} == {fake_controller.site_id, *[f"site-{i}" for i in range(15)]}


def test_resolve_site_id_explicit_wins(legacy_client: OmadaClient):
    assert legacy_client.resolve_site_id("explicit-site") == "explicit-site"


def test_resolve_site_id_from_settings():
    settings = Settings(
        base_url="https://omada.example.test",
        auth_mode=AuthMode.LEGACY,
        site_id="configured-site",
        username="a",
        password="b",
    )
    client = OmadaClient(settings, transport=RaisingTransport())
    assert client.resolve_site_id() == "configured-site"


def test_resolve_site_id_auto_selects_single_site(legacy_client: OmadaClient, fake_controller: FakeOmadaController):
    assert legacy_client.resolve_site_id() == fake_controller.site_id


def test_resolve_site_id_ambiguous_raises(legacy_client: OmadaClient, fake_controller: FakeOmadaController):
    fake_controller.extra_sites = [{"id": "site-2", "name": "Site 2"}]
    with pytest.raises(SiteAmbiguousError):
        legacy_client.resolve_site_id()


def test_resolve_site_id_openapi_without_site_id_raises_config_error():
    settings = Settings(
        base_url="https://omada.example.test",
        auth_mode=AuthMode.OPENAPI,
        omadac_id="oid",
        client_id="cid",
        client_secret="secret",
    )
    client = OmadaClient(settings, transport=RaisingTransport())
    with pytest.raises(ConfigError):
        client.resolve_site_id()


# --- list_devices / find_device_row / get_device_detail -------------------


def test_list_devices_legacy_raw_shape(legacy_client: OmadaClient, fake_controller: FakeOmadaController):
    rows = legacy_client.list_devices(fake_controller.site_id)
    assert len(rows) == 3
    ap_row = next(r for r in rows if r["mac"] == "50-D4-F7-66-0D-9C")
    assert ap_row["statusCategory"] == 1
    assert ap_row["uptimeLong"] == 6180
    assert ap_row["wp2g"]["actualChannel"] == "11  / 2462MHz"


def test_list_devices_openapi_raw_shape_has_no_wifi_fields(
    openapi_client: OmadaClient, fake_controller: FakeOmadaController
):
    rows = openapi_client.list_devices(fake_controller.site_id)
    ap_row = next(r for r in rows if r["mac"] == "50-D4-F7-66-0D-9C")
    assert ap_row["status"] == 1
    assert "wp2g" not in ap_row
    assert "clientNum" not in ap_row


def test_find_device_row_not_found_raises(legacy_client: OmadaClient, fake_controller: FakeOmadaController):
    with pytest.raises(DeviceNotFoundError):
        legacy_client.find_device_row("AA-BB-CC-DD-EE-FF", fake_controller.site_id)


def test_find_device_row_case_insensitive(legacy_client: OmadaClient, fake_controller: FakeOmadaController):
    row, site = legacy_client.find_device_row("50-d4-f7-66-0d-9c", fake_controller.site_id)
    assert row["mac"] == "50-D4-F7-66-0D-9C"
    assert site == fake_controller.site_id


def test_get_device_detail_legacy_ap_fetches_eaps(legacy_client: OmadaClient, fake_controller: FakeOmadaController):
    detail = legacy_client.get_device_detail("50-D4-F7-66-0D-9C", fake_controller.site_id)
    assert detail["ledSetting"] == {"enable": True}
    assert any(
        call.endswith(f"/sites/{fake_controller.site_id}/eaps/50-D4-F7-66-0D-9C")
        for call in fake_controller.legacy_calls
    )


def test_get_device_detail_legacy_non_ap_falls_back_to_grid_row(
    legacy_client: OmadaClient, fake_controller: FakeOmadaController
):
    detail = legacy_client.get_device_detail("AC-15-A2-11-22-33", fake_controller.site_id)
    assert detail["type"] == "switch"
    assert "ledSetting" not in detail
    assert not any("eaps" in call for call in fake_controller.legacy_calls)


def test_get_device_detail_openapi_returns_list_row(openapi_client: OmadaClient, fake_controller: FakeOmadaController):
    detail = openapi_client.get_device_detail("50-D4-F7-66-0D-9C", fake_controller.site_id)
    assert detail["mac"] == "50-D4-F7-66-0D-9C"
    assert "wp2g" not in detail


# --- get_wifi_summary_rows -----------------------------------------------


def test_get_wifi_summary_rows_requires_legacy_auth(openapi_client: OmadaClient):
    with pytest.raises(FeatureUnavailableError):
        openapi_client.get_wifi_summary_rows()


def test_get_wifi_summary_rows_filters_to_aps(legacy_client: OmadaClient, fake_controller: FakeOmadaController):
    rows = legacy_client.get_wifi_summary_rows(fake_controller.site_id)
    assert {r["mac"] for r in rows} == {"50-D4-F7-66-0D-9C", "50-D4-F7-66-0D-9D"}


def test_get_wifi_summary_rows_filters_by_mac(legacy_client: OmadaClient, fake_controller: FakeOmadaController):
    rows = legacy_client.get_wifi_summary_rows(fake_controller.site_id, "50-D4-F7-66-0D-9C")
    assert len(rows) == 1
    assert rows[0]["mac"] == "50-D4-F7-66-0D-9C"


def test_get_wifi_summary_rows_mac_not_found_raises(legacy_client: OmadaClient, fake_controller: FakeOmadaController):
    with pytest.raises(DeviceNotFoundError):
        legacy_client.get_wifi_summary_rows(fake_controller.site_id, "AA-BB-CC-DD-EE-FF")


# --- transport-level error handling -----------------------------------------


def test_connect_error_raises_controller_connection_error():
    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    settings = Settings(base_url="https://omada.example.test", auth_mode=AuthMode.LEGACY, username="a", password="b")
    client = OmadaClient(settings, transport=httpx.MockTransport(_raise))
    with pytest.raises(ControllerConnectionError):
        client.get_controller_info()


def test_timeout_raises_controller_connection_error():
    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    settings = Settings(base_url="https://omada.example.test", auth_mode=AuthMode.LEGACY, username="a", password="b")
    client = OmadaClient(settings, transport=httpx.MockTransport(_raise))
    with pytest.raises(ControllerConnectionError):
        client.get_controller_info()


def test_non_json_response_raises_controller_connection_error():
    def _respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    settings = Settings(base_url="https://omada.example.test", auth_mode=AuthMode.LEGACY, username="a", password="b")
    client = OmadaClient(settings, transport=httpx.MockTransport(_respond))
    with pytest.raises(ControllerConnectionError):
        client.get_controller_info()


def test_unexpected_json_shape_raises_controller_connection_error():
    def _respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[1, 2, 3]")

    settings = Settings(base_url="https://omada.example.test", auth_mode=AuthMode.LEGACY, username="a", password="b")
    client = OmadaClient(settings, transport=httpx.MockTransport(_respond))
    with pytest.raises(ControllerConnectionError):
        client.get_controller_info()


def test_close_is_safe(legacy_client: OmadaClient):
    legacy_client.close()


@pytest.fixture
def transport_returning_bad_info() -> httpx.MockTransport:
    def _respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errorCode": 0, "msg": "Success.", "result": {}})

    return httpx.MockTransport(_respond)


# --- module-level helpers, exercised directly -----------------------------


def test_looks_like_auth_failure_true_on_401_403():
    response_401 = httpx.Response(401, request=httpx.Request("GET", "https://x.test"))
    response_403 = httpx.Response(403, request=httpx.Request("GET", "https://x.test"))
    assert _looks_like_auth_failure(response_401, {}) is True
    assert _looks_like_auth_failure(response_403, {}) is True


def test_looks_like_auth_failure_false_on_zero_or_missing_error_code():
    response_200 = httpx.Response(200, request=httpx.Request("GET", "https://x.test"))
    assert _looks_like_auth_failure(response_200, {"errorCode": 0}) is False
    assert _looks_like_auth_failure(response_200, {}) is False


def test_looks_like_auth_failure_false_on_unrelated_error_message():
    response_200 = httpx.Response(200, request=httpx.Request("GET", "https://x.test"))
    assert _looks_like_auth_failure(response_200, {"errorCode": -404, "msg": "Device not found."}) is False


def test_unwrap_raises_on_nonzero_error_code():
    with pytest.raises(ControllerCommandError):
        _unwrap({"errorCode": -1, "msg": "boom", "result": None}, "/some/path")


def test_unwrap_returns_empty_dict_when_result_is_not_a_dict():
    assert _unwrap({"errorCode": 0, "result": [1, 2, 3]}, "/some/path") == {}


# --- error envelopes surfaced through real requests -------------------


def test_get_controller_info_error_envelope_raises_controller_command_error():
    def _respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errorCode": -1, "msg": "controller busy", "result": None})

    settings = Settings(base_url="https://omada.example.test", auth_mode=AuthMode.LEGACY, username="a", password="b")
    client = OmadaClient(settings, transport=httpx.MockTransport(_respond))
    with pytest.raises(ControllerCommandError):
        client.get_controller_info()


def test_legacy_login_response_missing_token_raises_authentication_error(fake_controller: FakeOmadaController):
    def _respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/info":
            return httpx.Response(
                200, json={"errorCode": 0, "result": {"controllerVer": "5.13", "omadacId": fake_controller.omadac_id}}
            )
        if request.url.path.endswith("/api/v2/login"):
            return httpx.Response(200, json={"errorCode": 0, "msg": "Success.", "result": {}})
        raise AssertionError(f"unexpected request to {request.url.path}")

    settings = Settings(base_url="https://omada.example.test", auth_mode=AuthMode.LEGACY, username="a", password="b")
    client = OmadaClient(settings, transport=httpx.MockTransport(_respond))
    with pytest.raises(AuthenticationError, match="did not include a token"):
        client.list_sites()


def test_openapi_token_response_missing_access_token_raises_authentication_error(fake_controller: FakeOmadaController):
    def _respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/info":
            return httpx.Response(
                200, json={"errorCode": 0, "result": {"controllerVer": "5.13", "omadacId": fake_controller.omadac_id}}
            )
        if request.url.path == "/openapi/authorize/token":
            return httpx.Response(200, json={"errorCode": 0, "msg": "Success.", "result": {}})
        raise AssertionError(f"unexpected request to {request.url.path}")

    settings = Settings(
        base_url="https://omada.example.test", auth_mode=AuthMode.OPENAPI, client_id="cid", client_secret="secret"
    )
    client = OmadaClient(settings, transport=httpx.MockTransport(_respond))
    with pytest.raises(AuthenticationError, match="did not include an accessToken"):
        client.list_devices("site-1")


def test_openapi_generic_error_is_not_retried_as_auth_failure(
    openapi_client: OmadaClient, fake_controller: FakeOmadaController
):
    with pytest.raises(ControllerCommandError):
        openapi_client._get_openapi("/does-not-exist")
    assert fake_controller.openapi_token_calls == 1  # no extra token refresh attempted


def test_resolve_site_id_no_sites_at_all_raises_ambiguous(
    legacy_client: OmadaClient, fake_controller: FakeOmadaController
):
    fake_controller.no_sites = True
    with pytest.raises(SiteAmbiguousError, match="no sites found"):
        legacy_client.resolve_site_id()


# --- pagination stop condition driven by totalRows, not just page size ----


def test_paginate_v2_stops_on_total_rows_even_with_a_full_last_page(
    legacy_client: OmadaClient, fake_controller: FakeOmadaController
):
    fake_controller.extra_sites = [{"id": "site-2", "name": "Site 2"}]
    rows = legacy_client._paginate_v2("/sites", page_size=1)
    assert len(rows) == 2


def test_paginate_openapi_stops_on_total_rows_even_with_a_full_last_page(
    openapi_client: OmadaClient, fake_controller: FakeOmadaController
):
    rows = openapi_client._paginate_openapi(f"/sites/{fake_controller.site_id}/devices", page_size=1)
    assert len(rows) == len(fake_controller.devices)


# --- default client factory -----------------------------------------------


def test_get_client_returns_wired_omada_client(settings_legacy: Settings):
    client = get_client(settings_legacy)
    assert isinstance(client, OmadaClient)
    assert client.settings is settings_legacy


# --- _patch_v2: the write primitive, and the confirmed silent-discard gotcha ---
# (guard.py's set_radio_channel is the only real caller - these tests exercise
# the write primitive and the fake's simulation of real-hardware behavior
# directly, the same way test_guard.py exercises guard.set_radio_channel's
# own logic on top of it.)


def test_patch_v2_int_channel_is_silently_discarded_by_controller(
    legacy_client: OmadaClient, fake_controller: FakeOmadaController
):
    # Confirmed real-hardware gotcha: `channel` as an int (not a string) is
    # accepted (errorCode 0, "Success.") but the change never actually
    # applies - this locks the regression: if guard.set_radio_channel ever
    # stops sending channel as str(channel), this test's assertion on the
    # device's UNCHANGED state (not just the response) will catch it.
    result = legacy_client._patch_v2(
        f"/sites/{fake_controller.site_id}/eaps/50-D4-F7-66-0D-9C",
        {"radioSetting2g": {"channel": 6, "freq": 2437, "channelWidth": "4"}},
    )
    assert result == {}
    device = next(d for d in fake_controller.devices if d.mac == "50-D4-F7-66-0D-9C")
    assert device.radio_setting_2g is not None
    assert device.radio_setting_2g["channel"] == "11"  # unchanged
    assert len(fake_controller.silent_discards) == 1
    assert fake_controller.silent_discards[0]["radio_key"] == "radioSetting2g"


def test_patch_v2_missing_freq_is_silently_discarded_by_controller(
    legacy_client: OmadaClient, fake_controller: FakeOmadaController
):
    # Confirmed real-hardware gotcha: `freq` left out (or 0) is also
    # silently discarded, even with `channel` correctly sent as a string.
    result = legacy_client._patch_v2(
        f"/sites/{fake_controller.site_id}/eaps/50-D4-F7-66-0D-9C",
        {"radioSetting2g": {"channel": "6", "channelWidth": "4"}},
    )
    assert result == {}
    device = next(d for d in fake_controller.devices if d.mac == "50-D4-F7-66-0D-9C")
    assert device.radio_setting_2g["channel"] == "11"  # unchanged
    assert len(fake_controller.silent_discards) == 1


def test_patch_v2_correct_shape_applies(legacy_client: OmadaClient, fake_controller: FakeOmadaController):
    legacy_client._patch_v2(
        f"/sites/{fake_controller.site_id}/eaps/50-D4-F7-66-0D-9C",
        {"radioSetting2g": {"channel": "6", "freq": 2437, "channelWidth": "4"}},
    )
    device = next(d for d in fake_controller.devices if d.mac == "50-D4-F7-66-0D-9C")
    assert device.radio_setting_2g["channel"] == "6"
    assert device.radio_setting_2g["freq"] == 2437
    assert fake_controller.silent_discards == []


def test_patch_v2_unknown_device_returns_error_envelope(
    legacy_client: OmadaClient, fake_controller: FakeOmadaController
):
    with pytest.raises(ControllerCommandError):
        legacy_client._patch_v2(f"/sites/{fake_controller.site_id}/eaps/AA-BB-CC-DD-EE-FF", {"radioSetting2g": {}})


def test_patch_v2_session_expiry_triggers_one_automatic_relogin(
    legacy_client: OmadaClient, fake_controller: FakeOmadaController
):
    legacy_client.list_sites()  # establish session
    assert fake_controller.legacy_login_calls == 1

    fake_controller.expire_next_legacy_call = True
    legacy_client._patch_v2(
        f"/sites/{fake_controller.site_id}/eaps/50-D4-F7-66-0D-9C",
        {"radioSetting2g": {"channel": "6", "freq": 2437, "channelWidth": "4"}},
    )
    assert fake_controller.legacy_login_calls == 2


# --- get_clients / get_alerts (v0.2, read-only) ----------------------------


def test_get_clients_legacy_happy_path(legacy_client: OmadaClient, fake_controller: FakeOmadaController):
    rows = legacy_client.get_clients(fake_controller.site_id)
    assert len(rows) == 2
    assert rows[0]["mac"] == "A4-83-E7-11-22-33"


def test_get_clients_requires_legacy_auth(openapi_client: OmadaClient):
    with pytest.raises(FeatureUnavailableError):
        openapi_client.get_clients()


def test_get_alerts_legacy_happy_path_empty_by_default(
    legacy_client: OmadaClient, fake_controller: FakeOmadaController
):
    # Confirmed real-hardware state at verification time: totalRows=0.
    rows = legacy_client.get_alerts(fake_controller.site_id)
    assert rows == []


def test_get_alerts_requires_legacy_auth(openapi_client: OmadaClient):
    with pytest.raises(FeatureUnavailableError):
        openapi_client.get_alerts()
