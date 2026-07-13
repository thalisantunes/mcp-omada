"""Tests for guard.py's `_audited` decorator: every guarded write call must
produce exactly one audit journal entry (audit.py), carrying a correlation
id, and never a controller credential - regardless of whether it previewed,
applied, was rejected (post-write re-read did not confirm the change), or
errored. Mirrors mcp-mikrotik's `tests/test_guard_audit.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_omada import guard
from mcp_omada.client import OmadaClient
from mcp_omada.config import Settings
from mcp_omada.exceptions import (
    DeviceNotFoundError,
    RadioUnavailableError,
    ValidationError,
    WriteDisabledError,
)

from .fakes import FakeOmadaController

AP_MAC = "50-D4-F7-66-0D-9C"
SWITCH_MAC = "AC-15-A2-11-22-33"


def _events(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").strip().splitlines() if line]


@pytest.fixture
def audit_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("OMADA_AUDIT_LOG", str(log_path))
    return log_path


# --- four outcomes -----------------------------------------------------


def test_preview_call_journals_outcome_preview(
    audit_log: Path, legacy_client_write_enabled: OmadaClient, settings_legacy_write_enabled: Settings
):
    guard.set_radio_channel(
        legacy_client_write_enabled,
        settings_legacy_write_enabled,
        mac_address=AP_MAC,
        band="2g",
        channel=6,
        confirm=False,
    )

    events = _events(audit_log)
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "preview"
    assert event["confirm"] is False
    assert event["tool"] == "set_radio_channel"
    assert event["operation"] == "set_radio_channel"
    assert event["action"] == "PATCH"
    assert event["device"] == AP_MAC
    assert event["summary"]["before"]["channel"] == "11"
    assert event["summary"]["after"]["channel"] == "6"
    assert event["summary"]["message"] is None


def test_confirmed_verified_call_journals_outcome_applied(
    audit_log: Path, legacy_client_write_enabled: OmadaClient, settings_legacy_write_enabled: Settings
):
    guard.set_radio_channel(
        legacy_client_write_enabled,
        settings_legacy_write_enabled,
        mac_address=AP_MAC,
        band="2g",
        channel=6,
        confirm=True,
    )

    events = _events(audit_log)
    assert len(events) == 1
    assert events[0]["outcome"] == "applied"
    assert events[0]["confirm"] is True
    assert events[0]["summary"]["message"] is None


def test_confirmed_unverified_call_journals_outcome_rejected(
    audit_log: Path,
    legacy_client_write_enabled: OmadaClient,
    settings_legacy_write_enabled: Settings,
    fake_controller: FakeOmadaController,
):
    """The core finding this test locks in: errorCode 0 alone must never be
    reported as "applied" - a write the post-write re-read couldn't confirm
    is a distinct, audit-worthy "rejected" outcome, not "applied" and not
    silently indistinguishable from a "preview" that never even tried."""
    fake_controller.reject_next_patch_uncharacterized = True

    guard.set_radio_channel(
        legacy_client_write_enabled,
        settings_legacy_write_enabled,
        mac_address=AP_MAC,
        band="2g",
        channel=6,
        confirm=True,
    )

    events = _events(audit_log)
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "rejected"
    assert event["confirm"] is True
    assert event["summary"]["message"] is not None
    assert "not confirmed" in event["summary"]["message"].lower()


def test_write_disabled_error_journals_outcome_error(
    audit_log: Path, legacy_client_write_enabled: OmadaClient, settings_legacy: Settings
):
    """The read-only gate blocks the write before the device is ever
    touched, but that attempted write is still audit-worthy."""
    with pytest.raises(WriteDisabledError):
        guard.set_radio_channel(
            legacy_client_write_enabled, settings_legacy, mac_address=AP_MAC, band="2g", channel=6, confirm=True
        )

    events = _events(audit_log)
    assert len(events) == 1
    assert events[0]["outcome"] == "error"
    assert events[0]["operation"] == "set_radio_channel"
    assert events[0]["device"] == AP_MAC
    assert "read-only" in events[0]["summary"]["error"] or "blocked" in events[0]["summary"]["error"]


def test_device_not_found_journals_outcome_error(
    audit_log: Path, legacy_client_write_enabled: OmadaClient, settings_legacy_write_enabled: Settings
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

    events = _events(audit_log)
    assert len(events) == 1
    assert events[0]["outcome"] == "error"
    assert events[0]["device"] == "AA-BB-CC-DD-EE-FF"


def test_radio_unavailable_error_journals_outcome_error(
    audit_log: Path, legacy_client_write_enabled: OmadaClient, settings_legacy_write_enabled: Settings
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

    events = _events(audit_log)
    assert len(events) == 1
    assert events[0]["outcome"] == "error"


def test_validation_error_journals_outcome_error(
    audit_log: Path, legacy_client_write_enabled: OmadaClient, settings_legacy_write_enabled: Settings
):
    with pytest.raises(ValidationError):
        guard.set_radio_channel(
            legacy_client_write_enabled,
            settings_legacy_write_enabled,
            mac_address=AP_MAC,
            band="6g",
            channel=6,
            confirm=True,
        )

    events = _events(audit_log)
    assert len(events) == 1
    assert events[0]["outcome"] == "error"
    assert events[0]["device"] == AP_MAC  # known immediately from the call args, even though nothing was touched


# --- correlation id ------------------------------------------------------


def test_journal_entry_carries_a_correlation_id(
    audit_log: Path, legacy_client_write_enabled: OmadaClient, settings_legacy_write_enabled: Settings
):
    guard.set_radio_channel(
        legacy_client_write_enabled,
        settings_legacy_write_enabled,
        mac_address=AP_MAC,
        band="2g",
        channel=6,
        confirm=False,
    )

    events = _events(audit_log)
    assert len(events) == 1
    correlation_id = events[0]["correlation_id"]
    assert isinstance(correlation_id, str)
    assert len(correlation_id) == 12


def test_separate_calls_get_different_correlation_ids(
    audit_log: Path, legacy_client_write_enabled: OmadaClient, settings_legacy_write_enabled: Settings
):
    guard.set_radio_channel(
        legacy_client_write_enabled,
        settings_legacy_write_enabled,
        mac_address=AP_MAC,
        band="2g",
        channel=6,
        confirm=False,
    )
    guard.set_radio_channel(
        legacy_client_write_enabled,
        settings_legacy_write_enabled,
        mac_address=AP_MAC,
        band="2g",
        channel=1,
        confirm=False,
    )

    events = _events(audit_log)
    assert len(events) == 2
    assert events[0]["correlation_id"] != events[1]["correlation_id"]


# --- CRITICAL: no controller credential ever appears in the journal --------


def test_journal_never_leaks_controller_password(
    audit_log: Path, legacy_client_write_enabled: OmadaClient, settings_legacy_write_enabled: Settings
):
    assert settings_legacy_write_enabled.password == "s3cret"  # sanity: the fixture's password
    guard.set_radio_channel(
        legacy_client_write_enabled,
        settings_legacy_write_enabled,
        mac_address=AP_MAC,
        band="2g",
        channel=6,
        confirm=True,
    )

    raw = audit_log.read_text(encoding="utf-8")
    assert "s3cret" not in raw


def test_journal_never_leaks_password_across_preview_applied_rejected_and_error(
    audit_log: Path,
    legacy_client_write_enabled: OmadaClient,
    settings_legacy_write_enabled: Settings,
    settings_legacy: Settings,
    fake_controller: FakeOmadaController,
):
    """Broad sweep across every outcome this tool can produce."""
    guard.set_radio_channel(
        legacy_client_write_enabled,
        settings_legacy_write_enabled,
        mac_address=AP_MAC,
        band="2g",
        channel=1,
        confirm=False,
    )
    guard.set_radio_channel(
        legacy_client_write_enabled,
        settings_legacy_write_enabled,
        mac_address=AP_MAC,
        band="2g",
        channel=6,
        confirm=True,
    )
    fake_controller.reject_next_patch_uncharacterized = True
    guard.set_radio_channel(
        legacy_client_write_enabled,
        settings_legacy_write_enabled,
        mac_address=AP_MAC,
        band="2g",
        channel=11,
        confirm=True,
    )
    with pytest.raises(WriteDisabledError):
        guard.set_radio_channel(
            legacy_client_write_enabled, settings_legacy, mac_address=AP_MAC, band="2g", channel=6, confirm=True
        )

    raw = audit_log.read_text(encoding="utf-8")
    assert "s3cret" not in raw
    events = _events(audit_log)
    assert len(events) == 4
    assert [event["outcome"] for event in events] == ["preview", "applied", "rejected", "error"]
    for event in events:
        assert "s3cret" not in json.dumps(event)


# --- warning is always present in the journal's summary --------------------


def test_journal_carries_the_disruption_warning_in_summary(
    audit_log: Path, legacy_client_write_enabled: OmadaClient, settings_legacy_write_enabled: Settings
):
    preview = guard.set_radio_channel(
        legacy_client_write_enabled,
        settings_legacy_write_enabled,
        mac_address=AP_MAC,
        band="5g",
        channel=149,
        confirm=True,
    )
    assert preview.warning is not None

    events = _events(audit_log)
    assert len(events) == 1
    assert events[0]["summary"]["warning"] == preview.warning
    assert "reassociate" in events[0]["summary"]["warning"]
    assert "internal index" in events[0]["summary"]["warning"]
