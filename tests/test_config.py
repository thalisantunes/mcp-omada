from __future__ import annotations

import pytest

from mcp_omada.config import DEFAULT_TIMEOUT, AuthMode, load_settings
from mcp_omada.exceptions import ConfigError


def test_requires_base_url():
    with pytest.raises(ConfigError, match="OMADA_BASE_URL"):
        load_settings(env={})


def test_requires_some_credentials():
    with pytest.raises(ConfigError, match="No credentials configured"):
        load_settings(env={"OMADA_BASE_URL": "https://1.2.3.4:8043"})


def test_legacy_credentials_select_legacy_mode():
    settings = load_settings(
        env={"OMADA_BASE_URL": "https://1.2.3.4:8043", "OMADA_USER": "admin", "OMADA_PASS": "hunter2"}
    )
    assert settings.auth_mode is AuthMode.LEGACY
    assert settings.username == "admin"
    assert settings.password == "hunter2"


def test_openapi_credentials_select_openapi_mode():
    settings = load_settings(
        env={
            "OMADA_BASE_URL": "https://1.2.3.4:8043",
            "OMADA_CLIENT_ID": "cid",
            "OMADA_CLIENT_SECRET": "csecret",
        }
    )
    assert settings.auth_mode is AuthMode.OPENAPI
    assert settings.client_id == "cid"
    assert settings.client_secret == "csecret"


def test_both_credential_pairs_prefers_legacy():
    settings = load_settings(
        env={
            "OMADA_BASE_URL": "https://1.2.3.4:8043",
            "OMADA_USER": "admin",
            "OMADA_PASS": "hunter2",
            "OMADA_CLIENT_ID": "cid",
            "OMADA_CLIENT_SECRET": "csecret",
        }
    )
    assert settings.auth_mode is AuthMode.LEGACY


def test_partial_legacy_credentials_rejected():
    with pytest.raises(ConfigError, match="OMADA_USER and OMADA_PASS"):
        load_settings(env={"OMADA_BASE_URL": "https://1.2.3.4:8043", "OMADA_USER": "admin"})


def test_partial_openapi_credentials_rejected():
    with pytest.raises(ConfigError, match="OMADA_CLIENT_ID and OMADA_CLIENT_SECRET"):
        load_settings(env={"OMADA_BASE_URL": "https://1.2.3.4:8043", "OMADA_CLIENT_ID": "cid"})


def test_base_url_trailing_slash_stripped():
    settings = load_settings(env={"OMADA_BASE_URL": "https://1.2.3.4:8043/", "OMADA_USER": "a", "OMADA_PASS": "b"})
    assert settings.base_url == "https://1.2.3.4:8043"


def test_optional_omadac_id_and_site_id_passed_through():
    settings = load_settings(
        env={
            "OMADA_BASE_URL": "https://1.2.3.4:8043",
            "OMADA_USER": "a",
            "OMADA_PASS": "b",
            "OMADA_OMADAC_ID": "oid-1",
            "OMADA_SITE_ID": "site-1",
        }
    )
    assert settings.omadac_id == "oid-1"
    assert settings.site_id == "site-1"


def test_verify_tls_defaults_false():
    settings = load_settings(env={"OMADA_BASE_URL": "https://1.2.3.4:8043", "OMADA_USER": "a", "OMADA_PASS": "b"})
    assert settings.verify_tls is False


@pytest.mark.parametrize(
    "raw,expected",
    [("true", True), ("1", True), ("yes", True), ("on", True), ("false", False), ("nope", False)],
)
def test_verify_tls_env_parsing(raw: str, expected: bool):
    settings = load_settings(
        env={"OMADA_BASE_URL": "https://1.2.3.4:8043", "OMADA_USER": "a", "OMADA_PASS": "b", "OMADA_VERIFY_TLS": raw}
    )
    assert settings.verify_tls is expected


def test_timeout_defaults():
    settings = load_settings(env={"OMADA_BASE_URL": "https://1.2.3.4:8043", "OMADA_USER": "a", "OMADA_PASS": "b"})
    assert settings.timeout == DEFAULT_TIMEOUT


def test_timeout_custom():
    settings = load_settings(
        env={"OMADA_BASE_URL": "https://1.2.3.4:8043", "OMADA_USER": "a", "OMADA_PASS": "b", "OMADA_TIMEOUT": "30"}
    )
    assert settings.timeout == 30.0


def test_timeout_invalid_raises():
    with pytest.raises(ConfigError, match="OMADA_TIMEOUT"):
        load_settings(
            env={
                "OMADA_BASE_URL": "https://1.2.3.4:8043",
                "OMADA_USER": "a",
                "OMADA_PASS": "b",
                "OMADA_TIMEOUT": "not-a-number",
            }
        )


def test_load_settings_reads_real_environ(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OMADA_BASE_URL", "https://1.2.3.4:8043")
    monkeypatch.setenv("OMADA_USER", "a")
    monkeypatch.setenv("OMADA_PASS", "b")
    settings = load_settings()
    assert settings.base_url == "https://1.2.3.4:8043"


def test_allow_write_defaults_false():
    settings = load_settings(env={"OMADA_BASE_URL": "https://1.2.3.4:8043", "OMADA_USER": "a", "OMADA_PASS": "b"})
    assert settings.allow_write is False


@pytest.mark.parametrize(
    "raw,expected",
    [("true", True), ("1", True), ("yes", True), ("on", True), ("false", False), ("nope", False)],
)
def test_allow_write_env_parsing(raw: str, expected: bool):
    settings = load_settings(
        env={"OMADA_BASE_URL": "https://1.2.3.4:8043", "OMADA_USER": "a", "OMADA_PASS": "b", "OMADA_ALLOW_WRITE": raw}
    )
    assert settings.allow_write is expected
