"""Omada controller HTTP client.

TWO AUTH WORLDS THAT NEVER MIX - confirmed against a real OC200 v5.13.30.20
on 2026-07-12 (see docs/api-notes.md for the full write-up):

  1. LEGACY (local-user) login: POST /{omadacId}/api/v2/login with a JSON
     {"username","password"} body returns a CSRF token
     (`result.token`) and sets a session cookie (TPOMADA_SESSIONID) via
     Set-Cookie. Every subsequent /api/v2/* request must carry BOTH the
     session cookie (handled automatically by httpx.Client's cookie jar, as
     long as the same Client instance is reused - never a fresh one per
     request) AND a "Csrf-Token: <token>" header. This is the RICHER path -
     the only one, as of v0.1, that list_sites and get_wifi_summary work
     through - and the preferred one when both credential pairs are set.
  2. OPEN API (client_credentials): POST /openapi/authorize/token with a
     JSON {"omadacId","client_id","client_secret"} body returns
     `result.accessToken` (valid ~7200s). Every subsequent /openapi/v1/*
     request must carry "Authorization: AccessToken=<token>". This path
     returns a REDUCED field set (see formatting.py/README) - notably no
     wp2g/wp5g, no clientNum.

A legacy session is REJECTED (empty response) by /openapi/v1/* endpoints,
and an Open API access token is REJECTED (empty response) by /api/v2/*
endpoints - they are not interchangeable, so this client picks exactly one
mode at construction time (Settings.auth_mode) and never tries the other as
a fallback.

All device I/O goes through httpx with structured URL path segments, query
params, and JSON bodies - nothing in this module builds a request by
concatenating strings from caller-supplied input, so injection through a MAC
address or site ID is ruled out by construction (validation.py exists to
reject garbage early with a clear error, not as an injection defense - same
convention as mcp-mikrotik's client.py/validation.py).

No secret (password, client_secret, CSRF token, session cookie, access
token) is ever included in a log message or an exception's own text -
exceptions carry only what the controller told us (an errorCode/msg), never
the request we sent it.

WRITES (v0.2+): `_patch_v2` is this package's only write primitive so far -
NOT exposed as an MCP tool directly. The only caller allowed to invoke it is
guard.py (see that module's docstring), which maps each write operation to
exactly one fixed path via a central ALLOWLIST. server.py never calls
`_patch_v2` directly - mirrors mcp-mikrotik's client.py "Write primitives"
convention exactly.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .config import AuthMode, Settings
from .exceptions import (
    AuthenticationError,
    ConfigError,
    ControllerCommandError,
    ControllerConnectionError,
    DeviceNotFoundError,
    FeatureUnavailableError,
    SiteAmbiguousError,
)

logger = logging.getLogger("mcp_omada")

DEFAULT_PAGE_SIZE = 100
# Hard cap on auto-pagination, so a misbehaving controller (or a bug in the
# totalRows/page-size bookkeeping below) can't turn one tool call into an
# unbounded number of HTTP requests.
MAX_PAGES = 20
# Refresh the Open API access token this many seconds before its reported
# expiry, rather than waiting for a request to fail against an
# already-expired token.
TOKEN_REFRESH_MARGIN_SECONDS = 60
# Legacy /eaps/{MAC} is only a verified detail endpoint for these device
# types (access points) - see docs/api-notes.md.
_AP_DEVICE_TYPES = ("ap", "eap")


def _parse_envelope(response: httpx.Response, path: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ControllerConnectionError(f"non-JSON response from {path} (HTTP {response.status_code})") from exc
    if not isinstance(payload, dict):
        raise ControllerConnectionError(f"unexpected response shape from {path} (HTTP {response.status_code})")
    return payload


def _looks_like_auth_failure(response: httpx.Response, payload: dict[str, Any]) -> bool:
    """Best-effort signal that a non-zero errorCode means "your session/token
    is no longer valid" rather than some other API error (bad MAC, unknown
    site, ...).

    NOT independently verified against a live session-expiry event on real
    hardware today - the exact errorCode Omada uses for that was not
    captured during the 2026-07-12 verification pass (see
    docs/api-notes.md). This is a conservative heuristic (HTTP 401/403, or
    an error message mentioning token/login/session/csrf), used only to
    decide whether ONE automatic re-login/re-token retry is worth
    attempting - a false negative here just means a genuinely-expired
    session surfaces as a normal ControllerCommandError instead of being
    silently retried, which is the safe direction to be wrong in.
    """
    if response.status_code in (401, 403):
        return True
    error_code = payload.get("errorCode")
    if error_code in (0, None):
        return False
    msg = str(payload.get("msg") or "").lower()
    return any(keyword in msg for keyword in ("token", "login", "session", "csrf"))


def _unwrap(payload: dict[str, Any], path: str) -> dict[str, Any]:
    if payload.get("errorCode") != 0:
        raise ControllerCommandError(path, payload.get("errorCode"), payload.get("msg"))
    result = payload.get("result")
    return result if isinstance(result, dict) else {}


class OmadaClient:
    """Wraps one Omada controller's HTTP API, in whichever auth mode
    `settings.auth_mode` selects.

    The underlying httpx.Client (and therefore its cookie jar, which the
    legacy auth path depends on) is created once and reused for the
    client's lifetime - never recreated per request. A `transport` can be
    injected directly (httpx.MockTransport), which is how tests avoid ever
    making a real network call - see tests/fakes.py.
    """

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._http = httpx.Client(
            base_url=settings.base_url,
            verify=settings.verify_tls,
            timeout=settings.timeout,
            transport=transport,
        )
        self._omadac_id: str | None = settings.omadac_id
        self._site_id: str | None = settings.site_id
        self._csrf_token: str | None = None
        self._legacy_logged_in = False
        self._access_token: str | None = None
        self._access_token_expiry: float = 0.0

    def close(self) -> None:
        self._http.close()

    # --- transport ---------------------------------------------------------

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ControllerConnectionError(f"timed out calling {method} {url}") from exc
        except httpx.HTTPError as exc:
            raise ControllerConnectionError(f"{method} {url} failed: {exc}") from exc

    # --- controller-level (unauthenticated) --------------------------------

    def get_controller_info(self) -> dict[str, Any]:
        """GET /api/info - unauthenticated, works before login/token and
        regardless of which auth mode is configured."""
        response = self._send("GET", "/api/info")
        result = _unwrap(_parse_envelope(response, "/api/info"), "/api/info")
        return {
            "controller_version": result.get("controllerVer"),
            "omadac_id": result.get("omadacId"),
            "configured": result.get("configured"),
        }

    def _omadacid(self) -> str:
        if self._omadac_id:
            return self._omadac_id
        info = self.get_controller_info()
        omadac_id = info.get("omadac_id")
        if not omadac_id:
            raise ControllerConnectionError("could not auto-discover omadacId via GET /api/info")
        self._omadac_id = omadac_id
        return omadac_id

    # --- legacy (/api/v2) --------------------------------------------------

    def _ensure_legacy_login(self) -> None:
        if self._legacy_logged_in:
            return
        if not (self.settings.username and self.settings.password):
            raise FeatureUnavailableError(
                "legacy API access", "OMADA_USER/OMADA_PASS are not configured for this server."
            )
        omadac_id = self._omadacid()
        path = f"/{omadac_id}/api/v2/login"
        response = self._send(
            "POST", path, json={"username": self.settings.username, "password": self.settings.password}
        )
        payload = _parse_envelope(response, path)
        if payload.get("errorCode") != 0:
            raise AuthenticationError(str(payload.get("msg") or f"errorCode={payload.get('errorCode')}"))
        result = payload.get("result") or {}
        token = result.get("token")
        if not token:
            raise AuthenticationError("login response did not include a token")
        self._csrf_token = token
        self._legacy_logged_in = True
        logger.info("Logged in to Omada controller (legacy) as %r", self.settings.username)

    def _get_v2(self, path: str, params: dict[str, Any] | None = None, *, _retried: bool = False) -> dict[str, Any]:
        self._ensure_legacy_login()
        omadac_id = self._omadacid()
        full_path = f"/{omadac_id}/api/v2{path}"
        headers = {"Csrf-Token": self._csrf_token} if self._csrf_token else {}
        response = self._send("GET", full_path, params=params, headers=headers)
        payload = _parse_envelope(response, full_path)
        if payload.get("errorCode") != 0:
            if not _retried and _looks_like_auth_failure(response, payload):
                logger.info("Legacy session appears expired at %s; re-authenticating once.", full_path)
                self._legacy_logged_in = False
                return self._get_v2(path, params, _retried=True)
            raise ControllerCommandError(full_path, payload.get("errorCode"), payload.get("msg"))
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    def _paginate_v2(self, path: str, *, page_size: int = DEFAULT_PAGE_SIZE) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_PAGES:
            result = self._get_v2(path, params={"currentPage": page, "currentPageSize": page_size})
            data = result.get("data") or []
            rows.extend(data)
            total_rows = result.get("totalRows")
            if len(data) < page_size:
                break
            if total_rows is not None and len(rows) >= int(total_rows):
                break
            page += 1
        return rows

    def _patch_v2(self, path: str, json_body: dict[str, Any], *, _retried: bool = False) -> dict[str, Any]:
        """Write primitive - PATCH against /api/v2/*. Legacy auth only (this
        endpoint is not verified under the Open API - see
        guard.set_radio_channel).

        NOT exposed as an MCP tool directly, and never called anywhere in
        this package except guard.py (see that module's docstring): the
        only caller allowed to invoke this is a dedicated, named function in
        guard.py that maps each write operation to exactly one fixed path
        via ALLOWLIST. There is no generic "PATCH this path with this body"
        tool anywhere in this package - server.py never calls this method.
        """
        self._ensure_legacy_login()
        omadac_id = self._omadacid()
        full_path = f"/{omadac_id}/api/v2{path}"
        headers = {"Csrf-Token": self._csrf_token} if self._csrf_token else {}
        response = self._send("PATCH", full_path, json=json_body, headers=headers)
        payload = _parse_envelope(response, full_path)
        if payload.get("errorCode") != 0:
            if not _retried and _looks_like_auth_failure(response, payload):
                logger.info("Legacy session appears expired at %s; re-authenticating once.", full_path)
                self._legacy_logged_in = False
                return self._patch_v2(path, json_body, _retried=True)
            raise ControllerCommandError(full_path, payload.get("errorCode"), payload.get("msg"))
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    # --- Open API (/openapi/v1) --------------------------------------------

    def _ensure_openapi_token(self) -> None:
        if self._access_token and time.monotonic() < self._access_token_expiry - TOKEN_REFRESH_MARGIN_SECONDS:
            return
        if not (self.settings.client_id and self.settings.client_secret):
            raise FeatureUnavailableError(
                "Open API access", "OMADA_CLIENT_ID/OMADA_CLIENT_SECRET are not configured for this server."
            )
        omadac_id = self._omadacid()
        path = "/openapi/authorize/token"
        response = self._send(
            "POST",
            path,
            params={"grant_type": "client_credentials"},
            json={
                "omadacId": omadac_id,
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
            },
        )
        payload = _parse_envelope(response, path)
        if payload.get("errorCode") != 0:
            raise AuthenticationError(str(payload.get("msg") or f"errorCode={payload.get('errorCode')}"))
        result = payload.get("result") or {}
        access_token = result.get("accessToken")
        if not access_token:
            raise AuthenticationError("token response did not include an accessToken")
        expires_in = result.get("expiresIn") or 7200
        self._access_token = access_token
        self._access_token_expiry = time.monotonic() + float(expires_in)
        logger.info("Obtained Omada Open API access token for client_id=%r", self.settings.client_id)

    def _get_openapi(
        self, path: str, params: dict[str, Any] | None = None, *, _retried: bool = False
    ) -> dict[str, Any]:
        self._ensure_openapi_token()
        omadac_id = self._omadacid()
        full_path = f"/openapi/v1/{omadac_id}{path}"
        headers = {"Authorization": f"AccessToken={self._access_token}"}
        response = self._send("GET", full_path, params=params, headers=headers)
        payload = _parse_envelope(response, full_path)
        if payload.get("errorCode") != 0:
            if not _retried and _looks_like_auth_failure(response, payload):
                logger.info("Open API token appears expired/invalid at %s; refreshing once.", full_path)
                self._access_token = None
                self._access_token_expiry = 0.0
                return self._get_openapi(path, params, _retried=True)
            raise ControllerCommandError(full_path, payload.get("errorCode"), payload.get("msg"))
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    def _paginate_openapi(self, path: str, *, page_size: int = DEFAULT_PAGE_SIZE) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_PAGES:
            result = self._get_openapi(path, params={"page": page, "pageSize": page_size})
            data = result.get("data") or []
            rows.extend(data)
            total_rows = result.get("totalRows")
            if len(data) < page_size:
                break
            if total_rows is not None and len(rows) >= int(total_rows):
                break
            page += 1
        return rows

    # --- sites ---------------------------------------------------------

    def list_sites(self) -> list[dict[str, Any]]:
        """List sites: `id`+`name` only (the fields this package normalizes
        on - see docs/api-notes.md for the full raw shape).

        Legacy auth ONLY - no Open API sites-list endpoint has been
        independently verified against real hardware as of v0.1 (see
        FeatureUnavailableError message / README).
        """
        if self.settings.auth_mode is not AuthMode.LEGACY:
            raise FeatureUnavailableError(
                "list_sites",
                "requires legacy login (OMADA_USER/OMADA_PASS) - no Open API sites-list endpoint "
                "has been verified against real hardware yet; set OMADA_SITE_ID explicitly instead.",
            )
        rows = self._paginate_v2("/sites")
        return [{"id": row.get("id"), "name": row.get("name")} for row in rows]

    def resolve_site_id(self, explicit: str | None = None) -> str:
        """Resolve the site to operate on: an explicit argument wins, then
        OMADA_SITE_ID, then (legacy auth only) auto-selection if the
        controller manages exactly one site.
        """
        if explicit:
            return explicit
        if self._site_id:
            return self._site_id
        if self.settings.auth_mode is not AuthMode.LEGACY:
            raise ConfigError(
                "OMADA_SITE_ID must be set explicitly when using Open API credentials only "
                "(site auto-discovery requires legacy login - see README)."
            )
        sites = self.list_sites()
        if len(sites) == 1:
            site_id = sites[0].get("id")
            if site_id:
                self._site_id = site_id
                return site_id
        raise SiteAmbiguousError([str(site.get("id")) for site in sites if site.get("id")])

    # --- devices ---------------------------------------------------------

    def list_devices(self, site_id: str | None = None) -> list[dict[str, Any]]:
        """List devices on a site, in whichever raw shape the active auth
        mode returns (see formatting.normalize_device for the normalized
        view tools actually return)."""
        resolved_site = self.resolve_site_id(site_id)
        if self.settings.auth_mode is AuthMode.LEGACY:
            return self._paginate_v2(f"/sites/{resolved_site}/grid/devices")
        return self._paginate_openapi(f"/sites/{resolved_site}/devices")

    def find_device_row(self, mac_address: str, site_id: str | None = None) -> tuple[dict[str, Any], str]:
        """Find a device's raw row by MAC (already normalized to Omada's
        hyphenated-uppercase form - see validation.validate_mac_address) on
        a site. Raises DeviceNotFoundError if no row matches."""
        resolved_site = self.resolve_site_id(site_id)
        rows = self.list_devices(resolved_site)
        target = mac_address.upper()
        for row in rows:
            if str(row.get("mac") or "").upper() == target:
                return row, resolved_site
        raise DeviceNotFoundError(mac_address, resolved_site)

    def get_device_detail(self, mac_address: str, site_id: str | None = None) -> dict[str, Any]:
        """Fetch the richest available detail for one device.

        Open API mode: no per-device detail endpoint has been verified
        against real hardware - the matching (reduced-field) row from
        list_devices IS the detail.

        Legacy mode: richer detail (ssidOverrides, lanPortSettings,
        ledSetting, ...) is only verified for AP/EAP devices, via
        /sites/{site}/eaps/{MAC}. Any other device type falls back to its
        grid/devices summary row - see docs/api-notes.md.
        """
        row, resolved_site = self.find_device_row(mac_address, site_id)
        if self.settings.auth_mode is AuthMode.OPENAPI:
            return row

        device_type = str(row.get("type") or "").lower()
        if device_type not in _AP_DEVICE_TYPES:
            logger.info(
                "get_device_detail: device type %r has no verified detail endpoint in v0.1; "
                "returning the grid/devices summary row instead.",
                device_type,
            )
            return row

        result = self._get_v2(f"/sites/{resolved_site}/eaps/{row.get('mac')}")
        return result or row

    def get_wifi_summary_rows(self, site_id: str | None = None, mac_address: str | None = None) -> list[dict[str, Any]]:
        """Raw AP rows (wp2g/wp5g present) for get_wifi_summary. Legacy auth
        only - see FeatureUnavailableError message / README."""
        if self.settings.auth_mode is not AuthMode.LEGACY:
            raise FeatureUnavailableError(
                "get_wifi_summary",
                "requires legacy login (OMADA_USER/OMADA_PASS) - per-radio fields (wp2g/wp5g, "
                "clientNum2g/clientNum5g) are absent from the Open API device list (confirmed "
                "against real hardware).",
            )
        resolved_site = self.resolve_site_id(site_id)
        rows = self.list_devices(resolved_site)
        ap_rows = [row for row in rows if str(row.get("type") or "").lower() in _AP_DEVICE_TYPES]
        if mac_address:
            target = mac_address.upper()
            ap_rows = [row for row in ap_rows if str(row.get("mac") or "").upper() == target]
            if not ap_rows:
                raise DeviceNotFoundError(mac_address, resolved_site)
        return ap_rows

    # --- insight/clients + alerts (v0.2, read-only) -------------------------

    def get_clients(self, site_id: str | None = None) -> list[dict[str, Any]]:
        """Insight/known clients on a site (GET /sites/{sid}/insight/clients)
        - the controller's "Insight" view: historical + currently-known
        clients, not just currently-associated ones. Legacy auth ONLY - no
        Open API equivalent has been verified against real hardware yet.
        """
        if self.settings.auth_mode is not AuthMode.LEGACY:
            raise FeatureUnavailableError(
                "get_clients",
                "requires legacy login (OMADA_USER/OMADA_PASS) - no Open API insight/clients "
                "endpoint has been verified against real hardware yet.",
            )
        resolved_site = self.resolve_site_id(site_id)
        return self._paginate_v2(f"/sites/{resolved_site}/insight/clients")

    def get_alerts(self, site_id: str | None = None) -> list[dict[str, Any]]:
        """Site alerts (GET /sites/{sid}/alerts). The pagination ENVELOPE
        (currentPage/currentSize/totalRows/data) is confirmed against real
        hardware; the SHAPE of an individual alert row is NOT - see
        formatting.normalize_alert's docstring and docs/api-notes.md.
        Legacy auth ONLY - no Open API equivalent has been verified.
        """
        if self.settings.auth_mode is not AuthMode.LEGACY:
            raise FeatureUnavailableError(
                "get_alerts",
                "requires legacy login (OMADA_USER/OMADA_PASS) - no Open API alerts endpoint has "
                "been verified against real hardware yet.",
            )
        resolved_site = self.resolve_site_id(site_id)
        return self._paginate_v2(f"/sites/{resolved_site}/alerts")


def get_client(settings: Settings) -> OmadaClient:
    """Default client factory. v0.1 targets a single controller, so unlike
    mcp-mikrotik's get_client(settings, device_name) this takes no device
    name - see config.py's module docstring."""
    return OmadaClient(settings)
