from __future__ import annotations

import pytest

from mcp_omada import guard
from mcp_omada.client import OmadaClient
from mcp_omada.config import AuthMode, Settings
from mcp_omada.exceptions import (
    DeviceNotFoundError,
    FeatureUnavailableError,
    GuardViolationError,
    RadioUnavailableError,
    ValidationError,
    WriteDisabledError,
)

from .fakes import FakeOmadaController, RaisingTransport

AP_MAC = "50-D4-F7-66-0D-9C"  # AP-Backstage-01: dual-band, radioSetting2g+5g
SINGLE_BAND_AP_MAC = "50-D4-F7-66-0D-9D"  # AP-Studio-02: radioSetting2g only
SWITCH_MAC = "AC-15-A2-11-22-33"  # Switch-Core: not an AP at all


# --- read-only gate ---------------------------------------------------


def test_set_radio_channel_blocked_when_write_disabled(legacy_client: OmadaClient, settings_legacy: Settings):
    assert settings_legacy.allow_write is False
    with pytest.raises(WriteDisabledError):
        guard.set_radio_channel(legacy_client, settings_legacy, mac_address=AP_MAC, band="2g", channel=6, confirm=True)


def test_set_radio_channel_read_only_gate_applies_before_touching_device(settings_legacy: Settings):
    """Read-only gate must block *before* touching the device at all, regardless of confirm."""
    guarded_client = OmadaClient(settings_legacy, transport=RaisingTransport())
    with pytest.raises(WriteDisabledError):
        guard.set_radio_channel(guarded_client, settings_legacy, mac_address=AP_MAC, band="2g", channel=6, confirm=True)


# --- preview vs confirm -------------------------------------------------


def test_set_radio_channel_preview_does_not_apply(
    legacy_client_write_enabled: OmadaClient,
    settings_legacy_write_enabled: Settings,
    fake_controller: FakeOmadaController,
):
    preview = guard.set_radio_channel(
        legacy_client_write_enabled,
        settings_legacy_write_enabled,
        mac_address=AP_MAC,
        band="2g",
        channel=6,
        confirm=False,
    )
    assert preview.applied is False
    assert preview.before["channel"] == "11"
    assert preview.after["channel"] == "6"
    assert preview.after["freq"] == 2437
    # Nothing was written to the fake device.
    device = next(d for d in fake_controller.devices if d.mac == AP_MAC)
    assert device.radio_setting_2g is not None
    assert device.radio_setting_2g["channel"] == "11"
    assert fake_controller.patch_calls == []


def test_set_radio_channel_confirm_true_applies_2g(
    legacy_client_write_enabled: OmadaClient,
    settings_legacy_write_enabled: Settings,
    fake_controller: FakeOmadaController,
):
    preview = guard.set_radio_channel(
        legacy_client_write_enabled,
        settings_legacy_write_enabled,
        mac_address=AP_MAC,
        band="2g",
        channel=6,
        confirm=True,
    )
    assert preview.applied is True
    assert preview.device == AP_MAC
    assert preview.warning is None  # 2.4GHz carries no special caveat

    device = next(d for d in fake_controller.devices if d.mac == AP_MAC)
    assert device.radio_setting_2g is not None
    assert device.radio_setting_2g["channel"] == "6"
    assert device.radio_setting_2g["freq"] == 2437
    # Untouched fields (channelWidth, txPower, ...) survive the full-object resend.
    assert device.radio_setting_2g["txPower"] == 21


def test_set_radio_channel_confirm_true_applies_5g_and_warns(
    legacy_client_write_enabled: OmadaClient,
    settings_legacy_write_enabled: Settings,
    fake_controller: FakeOmadaController,
):
    preview = guard.set_radio_channel(
        legacy_client_write_enabled,
        settings_legacy_write_enabled,
        mac_address=AP_MAC,
        band="5g",
        channel=149,
        confirm=True,
    )
    assert preview.applied is True
    assert preview.after["channel"] == "149"
    assert preview.after["freq"] == 5745
    assert preview.warning is not None
    assert "5745" in preview.warning

    # Confirmed real-hardware behavior: a subsequent read shows channel
    # persisted as the internal index ("17"), not "149" - freq is reliable.
    device = next(d for d in fake_controller.devices if d.mac == AP_MAC)
    assert device.radio_setting_5g is not None
    assert device.radio_setting_5g["channel"] == "17"
    assert device.radio_setting_5g["freq"] == 5745


# --- validation happens before any device I/O ---------------------------


def test_set_radio_channel_invalid_band_raises_before_touching_device(settings_legacy_write_enabled: Settings):
    client = OmadaClient(settings_legacy_write_enabled, transport=RaisingTransport())
    with pytest.raises(ValidationError):
        guard.set_radio_channel(
            client, settings_legacy_write_enabled, mac_address=AP_MAC, band="6g", channel=6, confirm=True
        )


def test_set_radio_channel_invalid_channel_raises_before_touching_device(settings_legacy_write_enabled: Settings):
    client = OmadaClient(settings_legacy_write_enabled, transport=RaisingTransport())
    with pytest.raises(ValidationError):
        guard.set_radio_channel(
            client, settings_legacy_write_enabled, mac_address=AP_MAC, band="2g", channel=999, confirm=True
        )


# --- device/radio resolution --------------------------------------------


def test_set_radio_channel_device_not_found_raises(
    legacy_client_write_enabled: OmadaClient,
    settings_legacy_write_enabled: Settings,
    fake_controller: FakeOmadaController,
):
    with pytest.raises(DeviceNotFoundError):
        guard.set_radio_channel(
            legacy_client_write_enabled,
            settings_legacy_write_enabled,
            mac_address="AA-BB-CC-DD-EE-FF",
            band="2g",
            channel=6,
            confirm=True,
        )


def test_set_radio_channel_non_ap_device_raises_radio_unavailable(
    legacy_client_write_enabled: OmadaClient, settings_legacy_write_enabled: Settings
):
    with pytest.raises(RadioUnavailableError):
        guard.set_radio_channel(
            legacy_client_write_enabled,
            settings_legacy_write_enabled,
            mac_address=SWITCH_MAC,
            band="2g",
            channel=6,
            confirm=True,
        )


def test_set_radio_channel_single_band_ap_missing_5g_raises_radio_unavailable(
    legacy_client_write_enabled: OmadaClient, settings_legacy_write_enabled: Settings
):
    with pytest.raises(RadioUnavailableError):
        guard.set_radio_channel(
            legacy_client_write_enabled,
            settings_legacy_write_enabled,
            mac_address=SINGLE_BAND_AP_MAC,
            band="5g",
            channel=149,
            confirm=True,
        )


def test_set_radio_channel_single_band_ap_2g_still_works(
    legacy_client_write_enabled: OmadaClient, settings_legacy_write_enabled: Settings
):
    preview = guard.set_radio_channel(
        legacy_client_write_enabled,
        settings_legacy_write_enabled,
        mac_address=SINGLE_BAND_AP_MAC,
        band="2g",
        channel=1,
        confirm=True,
    )
    assert preview.applied is True
    assert preview.after["channel"] == "1"


# --- auth mode gate -----------------------------------------------------


def test_set_radio_channel_openapi_mode_raises_feature_unavailable(fake_controller: FakeOmadaController, transport):
    settings = Settings(
        base_url="https://omada.example.test",
        auth_mode=AuthMode.OPENAPI,
        omadac_id=fake_controller.omadac_id,
        site_id=fake_controller.site_id,
        client_id=fake_controller.openapi_client_id,
        client_secret=fake_controller.openapi_client_secret,
        allow_write=True,
    )
    client = OmadaClient(settings, transport=transport)
    with pytest.raises(FeatureUnavailableError):
        guard.set_radio_channel(client, settings, mac_address=AP_MAC, band="2g", channel=6, confirm=True)


# --- session expiry retry ------------------------------------------------


def test_set_radio_channel_session_expiry_triggers_one_automatic_relogin(
    settings_legacy_write_enabled: Settings, fake_controller: FakeOmadaController, transport
):
    client = OmadaClient(settings_legacy_write_enabled, transport=transport)
    fake_controller.expire_next_legacy_call = True

    preview = guard.set_radio_channel(
        client,
        settings_legacy_write_enabled,
        mac_address=AP_MAC,
        band="2g",
        channel=6,
        confirm=True,
        site_id=fake_controller.site_id,
    )

    assert preview.applied is True
    assert fake_controller.legacy_login_calls == 2  # re-authenticated exactly once


# --- allowlist sanity (mirrors mcp-mikrotik's test_guard.py) --------------


def test_allowlist_only_contains_named_operations():
    for name, op in guard.ALLOWLIST.items():
        assert op.name == name
        assert op.endpoint
        assert op.method


def test_require_allowed_rejects_unknown_operation(settings_legacy_write_enabled: Settings):
    with pytest.raises(GuardViolationError):
        guard._require_allowed(settings_legacy_write_enabled, "delete_everything")
