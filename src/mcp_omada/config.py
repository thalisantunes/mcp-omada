"""Server-wide configuration: environment variables only (v0.1 targets a
single controller - no devices.yaml fleet file, unlike mcp-omada's sibling
mcp-mikrotik).

  OMADA_BASE_URL        - required, e.g. https://192.168.1.2:8043
  OMADA_OMADAC_ID        - optional, auto-discovered via GET /api/info
  OMADA_SITE_ID           - optional, auto-selected if exactly one site exists
                            (legacy auth only - see client.py/README)
  OMADA_USER/OMADA_PASS              - legacy local-user login (preferred)
  OMADA_CLIENT_ID/OMADA_CLIENT_SECRET - Open API client_credentials
  OMADA_VERIFY_TLS        - "true"/"1"/"yes"/"on", default false
  OMADA_TIMEOUT            - HTTP timeout in seconds, default 15
  OMADA_LOG_LEVEL           - default INFO
  OMADA_ALLOW_WRITE        - "true"/"1"/"yes"/"on", default false (v0.2+:
                            enables write tools - see guard.py)

See .env.example for the full list with commentary.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from .exceptions import ConfigError

logger = logging.getLogger("mcp_omada")

DEFAULT_TIMEOUT = 15.0


class AuthMode(StrEnum):
    """Which of the two, mutually exclusive auth worlds this run uses.

    See client.py's module docstring for why these two never mix: a legacy
    session (Csrf-Token + cookie) is only accepted by /api/v2/*, an Open API
    access token (Authorization: AccessToken=...) is only accepted by
    /openapi/v1/*, and each is rejected (empty response) by the other's
    endpoints - confirmed against a real OC200 v5.13.30.20.
    """

    LEGACY = "legacy"
    OPENAPI = "openapi"


@dataclass
class Settings:
    """Resolved server configuration."""

    base_url: str
    auth_mode: AuthMode
    omadac_id: str | None = None
    site_id: str | None = None
    # repr=False on every credential field: a stray `logger.debug(settings)`
    # must never write a password/secret to stderr (mirrors mcp-mikrotik's
    # config.Device.password convention).
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    client_id: str | None = field(default=None, repr=False)
    client_secret: str | None = field(default=None, repr=False)
    verify_tls: bool = False
    timeout: float = DEFAULT_TIMEOUT
    # Read-only by default (v0.2+) - mirrors mcp-mikrotik's
    # MIKROTIK_ALLOW_WRITE gate exactly. Checked by guard.py's
    # _require_allowed before anything is read or written for a guarded
    # write operation, regardless of the tool call's own `confirm` value.
    allow_write: bool = False


def _bool_env(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from environment variables.

    `env` defaults to os.environ; passing an explicit mapping is mainly
    useful for tests so they never depend on the real process environment.
    """
    resolved_env: Mapping[str, str] = env if env is not None else os.environ

    base_url = _clean(resolved_env.get("OMADA_BASE_URL"))
    if not base_url:
        raise ConfigError("OMADA_BASE_URL is required (e.g. https://192.168.1.2:8043).")
    base_url = base_url.rstrip("/")

    omadac_id = _clean(resolved_env.get("OMADA_OMADAC_ID"))
    site_id = _clean(resolved_env.get("OMADA_SITE_ID"))

    username = _clean(resolved_env.get("OMADA_USER"))
    password = _clean(resolved_env.get("OMADA_PASS"))
    client_id = _clean(resolved_env.get("OMADA_CLIENT_ID"))
    client_secret = _clean(resolved_env.get("OMADA_CLIENT_SECRET"))

    has_legacy = bool(username or password)
    has_openapi = bool(client_id or client_secret)

    if has_legacy and not (username and password):
        raise ConfigError("OMADA_USER and OMADA_PASS must both be set to use legacy login.")
    if has_openapi and not (client_id and client_secret):
        raise ConfigError("OMADA_CLIENT_ID and OMADA_CLIENT_SECRET must both be set to use the Open API.")

    if username and password:
        auth_mode = AuthMode.LEGACY
        # Legacy is preferred (richer field set - see README) when both
        # credential pairs happen to be configured at once.
        if client_id and client_secret:
            logger.warning(
                "Both legacy (OMADA_USER/OMADA_PASS) and Open API "
                "(OMADA_CLIENT_ID/OMADA_CLIENT_SECRET) credentials are set; "
                "using legacy login (richer field set)."
            )
    elif client_id and client_secret:
        auth_mode = AuthMode.OPENAPI
    else:
        raise ConfigError(
            "No credentials configured: set either OMADA_USER+OMADA_PASS (legacy login, preferred) "
            "or OMADA_CLIENT_ID+OMADA_CLIENT_SECRET (Open API client_credentials)."
        )

    verify_tls = _bool_env(resolved_env.get("OMADA_VERIFY_TLS"), default=False)
    if not verify_tls:
        logger.warning(
            "OMADA_VERIFY_TLS is false: the controller's TLS certificate will not be verified. "
            "This is the default because an OC200 commonly serves a self-signed certificate on "
            "its LAN management port - set OMADA_VERIFY_TLS=true once you can validate it."
        )

    timeout_raw = _clean(resolved_env.get("OMADA_TIMEOUT"))
    if timeout_raw is None:
        timeout = DEFAULT_TIMEOUT
    else:
        try:
            timeout = float(timeout_raw)
        except ValueError as exc:
            raise ConfigError(f"OMADA_TIMEOUT must be a number, got {timeout_raw!r}.") from exc

    allow_write = _bool_env(resolved_env.get("OMADA_ALLOW_WRITE"), default=False)

    return Settings(
        base_url=base_url,
        auth_mode=auth_mode,
        omadac_id=omadac_id,
        site_id=site_id,
        username=username,
        password=password,
        client_id=client_id,
        client_secret=client_secret,
        verify_tls=verify_tls,
        timeout=timeout,
        allow_write=allow_write,
    )
