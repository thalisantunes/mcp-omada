"""In-memory fake Omada controller used by tests instead of a real one.

Implements an httpx.MockTransport handler that reproduces the exact JSON
shapes (including the documented gotchas: `actualChannel` irregular
whitespace, hyphenated MAC, `status`/`statusCategory` semantics, `uptime`
string vs `uptimeLong` int) confirmed against a real OC200 v5.13.30.20 - see
docs/api-notes.md. Device rows are built from one canonical `DeviceSpec` per
device and projected into BOTH the legacy (rich) and Open API (reduced)
shapes, so a test can assert the same underlying device is read correctly
through either auth path without the fake's own two shapes silently
drifting apart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

LEGACY_SESSION_COOKIE = "TPOMADA_SESSIONID"
LEGACY_SESSION_COOKIE_VALUE = "sess-abc123"

_AP_TYPES = ("ap", "eap")


def _json_response(status_code: int, payload: dict[str, Any], *, set_cookie: str | None = None) -> httpx.Response:
    headers = {"content-type": "application/json"}
    if set_cookie:
        headers["set-cookie"] = set_cookie
    return httpx.Response(status_code, headers=headers, content=json.dumps(payload).encode())


def _envelope(result: Any, error_code: int = 0, msg: str = "Success.") -> dict[str, Any]:
    return {"errorCode": error_code, "msg": msg, "result": result}


def _uptime_string(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# Confirmed real-hardware mapping (2026-07-13): requesting 5GHz channel 149
# is persisted/echoed back on subsequent reads as internal index "17" - see
# channels.py and docs/api-notes.md. Only this one pairing is confirmed;
# the fake does not invent a mapping for any other 5GHz channel.
CONFIRMED_5G_INTERNAL_INDEX = {"149": "17"}


@dataclass
class DeviceSpec:
    """One canonical device, projected into both auth paths' shapes below."""

    name: str
    type: str
    mac: str
    ip: str
    model: str
    firmware_version: str
    connected: bool
    uptime_seconds: int
    cpu_util: int = 10
    mem_util: int = 30
    compound_model: str | None = None
    need_upgrade: bool = False
    client_num: int | None = None
    client_num_2g: int | None = None
    client_num_5g: int | None = None
    tx_rate: int | None = None
    rx_rate: int | None = None
    upload: int | None = None
    download: int | None = None
    sn: str | None = None
    wp2g: dict[str, Any] | None = None
    wp5g: dict[str, Any] | None = None
    # CONFIG objects (distinct from wp2g/wp5g's RUNTIME summary above) -
    # only present on the /eaps/{MAC} detail response, confirmed shape:
    # {"radioEnable":true,"channelWidth":"4","channel":"11","txPower":21,
    # "txPowerLevel":4,"freq":2462,"wirelessMode":-2}. Mutated in place by
    # _handle_patch_eap on a valid set_radio_channel write.
    radio_setting_2g: dict[str, Any] | None = None
    radio_setting_5g: dict[str, Any] | None = None

    def legacy_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "mac": self.mac,
            "ip": self.ip,
            "model": self.model,
            "firmwareVersion": self.firmware_version,
            # statusCategory is the primary connected signal on this path;
            # status=14 (a value only meaningful on THIS path) is set too so
            # tests can exercise the fallback-to-status branch by dropping
            # statusCategory (see test_formatting.py).
            "status": 14 if self.connected else 0,
            "statusCategory": 1 if self.connected else 0,
            "uptime": _uptime_string(self.uptime_seconds),
            "uptimeLong": self.uptime_seconds,
            "cpuUtil": self.cpu_util,
            "memUtil": self.mem_util,
        }
        if self.compound_model is not None:
            row["compoundModel"] = self.compound_model
        if self.need_upgrade:
            row["needUpgrade"] = self.need_upgrade
        if self.client_num is not None:
            row["clientNum"] = self.client_num
        if self.client_num_2g is not None:
            row["clientNum2g"] = self.client_num_2g
        if self.client_num_5g is not None:
            row["clientNum5g"] = self.client_num_5g
        if self.tx_rate is not None:
            row["txRate"] = self.tx_rate
        if self.rx_rate is not None:
            row["rxRate"] = self.rx_rate
        if self.upload is not None:
            row["upload"] = self.upload
        if self.download is not None:
            row["download"] = self.download
        if self.wp2g is not None:
            row["wp2g"] = self.wp2g
        if self.wp5g is not None:
            row["wp5g"] = self.wp5g
        return row

    def openapi_row(self) -> dict[str, Any]:
        # Deliberately reduced field set - no clientNum*, no wp2g/wp5g, and
        # `status` here means 1==connected (a DIFFERENT semantic from the
        # legacy path's status==14) - see formatting.is_connected.
        row = {
            "name": self.name,
            "type": self.type,
            "mac": self.mac,
            "ip": self.ip,
            "status": 1 if self.connected else 0,
            "cpuUtil": self.cpu_util,
            "memUtil": self.mem_util,
            "uptime": _uptime_string(self.uptime_seconds),
            "model": self.model,
            "firmwareVersion": self.firmware_version,
        }
        if self.sn is not None:
            row["sn"] = self.sn
        return row


def default_devices() -> list[DeviceSpec]:
    return [
        DeviceSpec(
            name="AP-Backstage-01",
            type="ap",
            mac="50-D4-F7-66-0D-9C",
            ip="10.1.1.50",
            model="EAP670",
            compound_model="EAP670(US)",
            firmware_version="5.1.5 Build 20230530 Rel.75176",
            connected=True,
            uptime_seconds=6180,  # -> "1h 43m", matches docs/api-notes.md example
            cpu_util=12,
            mem_util=45,
            client_num=7,
            client_num_2g=2,
            client_num_5g=5,
            tx_rate=1200,
            rx_rate=300,
            upload=1000,
            download=5000,
            sn="2131A0123456",
            wp2g={
                "actualChannel": "11  / 2462MHz",
                "txPower": "high",
                "bandWidth": "20MHz",
                "rdMode": "11ax",
                "txUtil": 10,
                "rxUtil": 5,
                "interUtil": 2,
            },
            wp5g={
                # Confirmed real-hardware gotcha: 5GHz `channel` is an
                # internal index (here, "17"), NOT the channel number an
                # operator would recognize (149) - freq_mhz is the reliable
                # value. See formatting.parse_channel's docstring.
                "actualChannel": "17  / 5745MHz",
                "txPower": "high",
                "bandWidth": "80MHz",
                "rdMode": "11ax",
                "txUtil": 20,
                "rxUtil": 8,
                "interUtil": 3,
            },
            radio_setting_2g={
                "radioEnable": True,
                "channelWidth": "4",
                "channel": "11",
                "txPower": 21,
                "txPowerLevel": 4,
                "freq": 2462,
                "wirelessMode": -2,
            },
            radio_setting_5g={
                # Confirmed real-hardware shape: channel already persisted
                # as the internal index ("17") from a previous configuration
                # of channel 149 - freq (5745) is the reliable value. See
                # CONFIRMED_5G_INTERNAL_INDEX above and docs/api-notes.md.
                "radioEnable": True,
                "channelWidth": "6",
                "channel": "17",
                "txPower": 23,
                "txPowerLevel": 4,
                "freq": 5745,
                "wirelessMode": -2,
            },
        ),
        DeviceSpec(
            name="AP-Studio-02",
            type="ap",
            mac="50-D4-F7-66-0D-9D",
            ip="10.1.1.51",
            model="EAP225",
            firmware_version="2.0.3 Build 20220101 Rel.54321",
            connected=False,
            uptime_seconds=0,
            cpu_util=0,
            mem_util=0,
            client_num=0,
            client_num_2g=0,
            client_num_5g=0,
            sn="2131A0123457",
            wp2g={
                "actualChannel": "6  / 2437MHz",
                "txPower": "medium",
                "bandWidth": "20MHz",
                "rdMode": "11n",
                "txUtil": 0,
                "rxUtil": 0,
                "interUtil": 0,
            },
            # No wp5g at all - single-band AP; exercises normalize_radio(None).
            radio_setting_2g={
                "radioEnable": True,
                "channelWidth": "4",
                "channel": "6",
                "txPower": 18,
                "txPowerLevel": 3,
                "freq": 2437,
                "wirelessMode": -2,
            },
            # No radioSetting5g either - exercises RadioUnavailableError when
            # set_radio_channel is asked for band="5g" on this device.
        ),
        DeviceSpec(
            name="Switch-Core",
            type="switch",
            mac="AC-15-A2-11-22-33",
            ip="10.1.1.2",
            model="TL-SG3428",
            firmware_version="1.0.0 Build 20230101 Rel.12345",
            connected=True,
            uptime_seconds=864000,  # 10 days
            cpu_util=5,
            mem_util=20,
            sn="2131B0987654",
            # No clientNum*/wp2g/wp5g at all - exercises the "field absent
            # from this device type" branch of normalize_device.
        ),
    ]


def default_clients() -> list[dict[str, Any]]:
    """Confirmed real-hardware shape (GET /sites/{sid}/insight/clients) -
    see docs/api-notes.md and formatting.normalize_client."""
    return [
        {
            "mac": "A4-83-E7-11-22-33",
            "name": "thalis-laptop",
            "download": 1_048_576_000,
            "upload": 52_428_800,
            "duration": 3600,
            "lastSeen": 1_752_364_800_000,
            "guest": False,
            "wireless": True,
            "vid": 10,
            "block": False,
            "blockDisable": False,
            "lockToAp": False,
            "manager": True,
        },
        {
            "mac": "B8-27-EB-44-55-66",
            "name": "guest-phone",
            "download": 10_485_760,
            "upload": 1_048_576,
            "duration": 600,
            "lastSeen": 1_752_364_200_000,
            "guest": True,
            "wireless": True,
            "vid": 20,
            "block": False,
            "blockDisable": False,
            "lockToAp": False,
            "manager": False,
        },
    ]


@dataclass
class FakeOmadaController:
    """Handler for httpx.MockTransport reproducing one Omada controller."""

    omadac_id: str = "omadac-abc123"
    controller_version: str = "5.13.30.20"
    legacy_username: str = "admin"
    legacy_password: str = "s3cret"
    legacy_token: str = "csrf-token-abc"
    openapi_client_id: str = "client-id-abc"
    openapi_client_secret: str = "client-secret-xyz"
    openapi_access_token: str = "access-token-xyz"
    openapi_expires_in: int = 7200
    site_id: str = "site-default"
    site_name: str = "Default"
    extra_sites: list[dict[str, str]] = field(default_factory=list)
    # Edge case: a controller with zero sites at all (e.g. freshly
    # provisioned) - exercises SiteAmbiguousError's "no sites found" message.
    no_sites: bool = False
    devices: list[DeviceSpec] = field(default_factory=default_devices)
    reject_legacy_credentials: bool = False
    reject_openapi_credentials: bool = False
    # One-shot "session/token just expired" simulation: the NEXT authenticated
    # v2 (or Open API) request fails with an auth-looking error; the retry
    # after re-login/re-token succeeds normally. Exercises client.py's
    # single-retry re-authentication path.
    expire_next_legacy_call: bool = False
    expire_next_openapi_call: bool = False
    clients: list[dict[str, Any]] = field(default_factory=default_clients)
    # Empty by default - matches the verified real-hardware state (a healthy
    # network, totalRows=0) at the moment /alerts' envelope was confirmed.
    # See formatting.normalize_alert's docstring for why the ROW shape below
    # is a best-effort guess, only used when a test opts into a non-empty list.
    alerts: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.info_calls = 0
        self.legacy_login_calls = 0
        self.openapi_token_calls = 0
        self.legacy_calls: list[str] = []
        self.openapi_calls: list[str] = []
        self.patch_calls: list[dict[str, Any]] = []
        # Records of a write the fake accepted (errorCode 0) but silently
        # discarded, per the confirmed real-hardware gotcha - see
        # _handle_patch_eap. Used to assert this package's own write code
        # NEVER triggers a discard (the regression this fake locks in).
        self.silent_discards: list[dict[str, Any]] = []

    # --- helpers ---------------------------------------------------------

    def _legacy_grid_rows(self) -> list[dict[str, Any]]:
        return [device.legacy_row() for device in self.devices]

    def _openapi_rows(self) -> list[dict[str, Any]]:
        return [device.openapi_row() for device in self.devices]

    def _paginated(
        self, rows: list[dict[str, Any]], params: httpx.QueryParams, page_key: str, size_key: str
    ) -> dict[str, Any]:
        page = int(params.get(page_key, "1"))
        page_size = int(params.get(size_key, "10"))
        start = (page - 1) * page_size
        chunk = rows[start : start + page_size]
        return {"totalRows": len(rows), "currentPage": page, "currentSize": page_size, "data": chunk}

    def _all_sites(self) -> list[dict[str, str]]:
        if self.no_sites:
            return []
        return [{"id": self.site_id, "name": self.site_name}, *self.extra_sites]

    # --- request handler ---------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = request.url.params

        if path == "/api/info" and request.method == "GET":
            self.info_calls += 1
            return _json_response(
                200,
                _envelope({"controllerVer": self.controller_version, "omadacId": self.omadac_id, "configured": True}),
            )

        if path == f"/{self.omadac_id}/api/v2/login" and request.method == "POST":
            self.legacy_login_calls += 1
            body = json.loads(request.content or b"{}")
            if (
                self.reject_legacy_credentials
                or body.get("username") != self.legacy_username
                or body.get("password") != self.legacy_password
            ):
                return _json_response(200, _envelope(None, error_code=-1000, msg="Incorrect username or password."))
            cookie = f"{LEGACY_SESSION_COOKIE}={LEGACY_SESSION_COOKIE_VALUE}; Path=/; HttpOnly"
            return _json_response(200, _envelope({"token": self.legacy_token}), set_cookie=cookie)

        if path == "/openapi/authorize/token" and request.method == "POST":
            self.openapi_token_calls += 1
            body = json.loads(request.content or b"{}")
            if (
                self.reject_openapi_credentials
                or body.get("client_id") != self.openapi_client_id
                or body.get("client_secret") != self.openapi_client_secret
                or body.get("omadacId") != self.omadac_id
            ):
                return _json_response(200, _envelope(None, error_code=-44112, msg="Invalid client credentials."))
            return _json_response(
                200,
                _envelope(
                    {
                        "accessToken": self.openapi_access_token,
                        "tokenType": "bearer",
                        "expiresIn": self.openapi_expires_in,
                    }
                ),
            )

        if path.startswith(f"/{self.omadac_id}/api/v2/"):
            return self._handle_legacy(request, path, params)

        if path.startswith(f"/openapi/v1/{self.omadac_id}/"):
            return self._handle_openapi(request, path, params)

        return _json_response(404, _envelope(None, error_code=-404, msg=f"Not found: {path}"))

    def _legacy_authenticated(self, request: httpx.Request) -> bool:
        token = request.headers.get("csrf-token")
        cookie_header = request.headers.get("cookie", "")
        return token == self.legacy_token and f"{LEGACY_SESSION_COOKIE}={LEGACY_SESSION_COOKIE_VALUE}" in cookie_header

    def _handle_legacy(self, request: httpx.Request, path: str, params: httpx.QueryParams) -> httpx.Response:
        if not self._legacy_authenticated(request):
            return _json_response(401, _envelope(None, error_code=-44112, msg="Token error, please login again."))

        if self.expire_next_legacy_call:
            self.expire_next_legacy_call = False
            return _json_response(401, _envelope(None, error_code=-44112, msg="Token error, please login again."))

        self.legacy_calls.append(path)
        suffix = path[len(f"/{self.omadac_id}/api/v2") :]

        if suffix == "/sites":
            return _json_response(
                200, _envelope(self._paginated(self._all_sites(), params, "currentPage", "currentPageSize"))
            )

        if suffix == f"/sites/{self.site_id}/grid/devices":
            return _json_response(
                200, _envelope(self._paginated(self._legacy_grid_rows(), params, "currentPage", "currentPageSize"))
            )

        if suffix.startswith(f"/sites/{self.site_id}/eaps/") and request.method == "GET":
            mac = suffix.rsplit("/", 1)[-1]
            device = self._find_ap(mac)
            if device is None:
                return _json_response(200, _envelope(None, error_code=-404, msg="Device not found."))
            detail = device.legacy_row()
            detail["ssidOverrides"] = []
            detail["lanPortSettings"] = []
            detail["ledSetting"] = {"enable": True}
            if device.radio_setting_2g is not None:
                detail["radioSetting2g"] = device.radio_setting_2g
            if device.radio_setting_5g is not None:
                detail["radioSetting5g"] = device.radio_setting_5g
            return _json_response(200, _envelope(detail))

        if suffix.startswith(f"/sites/{self.site_id}/eaps/") and request.method == "PATCH":
            mac = suffix.rsplit("/", 1)[-1]
            return self._handle_patch_eap(mac, request)

        if suffix == f"/sites/{self.site_id}/insight/clients":
            return _json_response(
                200, _envelope(self._paginated(list(self.clients), params, "currentPage", "currentPageSize"))
            )

        if suffix == f"/sites/{self.site_id}/alerts":
            return _json_response(
                200, _envelope(self._paginated(list(self.alerts), params, "currentPage", "currentPageSize"))
            )

        return _json_response(404, _envelope(None, error_code=-404, msg=f"Not found: {path}"))

    def _find_ap(self, mac: str) -> DeviceSpec | None:
        for device in self.devices:
            if device.mac == mac and device.type in _AP_TYPES:
                return device
        return None

    def _handle_patch_eap(self, mac: str, request: httpx.Request) -> httpx.Response:
        device = self._find_ap(mac)
        if device is None:
            return _json_response(200, _envelope(None, error_code=-404, msg="Device not found."))

        self.patch_calls.append({"mac": mac, "path": request.url.path})
        body = json.loads(request.content or b"{}")

        for radio_key, attr_name in (("radioSetting2g", "radio_setting_2g"), ("radioSetting5g", "radio_setting_5g")):
            if radio_key not in body:
                continue
            new_setting = body[radio_key]
            channel = new_setting.get("channel")
            freq = new_setting.get("freq")
            # Confirmed real-hardware gotcha (docs/api-notes.md): accepted
            # (errorCode 0, "Success.") but SILENTLY DISCARDED - the stored
            # radioSetting is never actually updated - unless `channel` is a
            # string AND `freq` is truthy (filled in with the MHz value).
            if not isinstance(channel, str) or not freq:
                self.silent_discards.append({"mac": mac, "radio_key": radio_key, "body": dict(new_setting)})
                continue
            stored = dict(new_setting)
            if radio_key == "radioSetting5g" and channel in CONFIRMED_5G_INTERNAL_INDEX:
                # Confirmed real-hardware gotcha: 5GHz channel persists back
                # as an internal index, not the requested channel number.
                stored["channel"] = CONFIRMED_5G_INTERNAL_INDEX[channel]
            setattr(device, attr_name, stored)

        # Real controller behavior: PATCH returns Success with an empty
        # result, not an echo of the new state - callers re-GET to verify.
        return _json_response(200, _envelope({}))

    def _handle_openapi(self, request: httpx.Request, path: str, params: httpx.QueryParams) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if auth != f"AccessToken={self.openapi_access_token}":
            return _json_response(401, _envelope(None, error_code=-44112, msg="Invalid or expired access token."))

        if self.expire_next_openapi_call:
            self.expire_next_openapi_call = False
            return _json_response(401, _envelope(None, error_code=-44112, msg="Invalid or expired access token."))

        self.openapi_calls.append(path)
        suffix = path[len(f"/openapi/v1/{self.omadac_id}") :]

        if suffix == f"/sites/{self.site_id}/devices":
            return _json_response(200, _envelope(self._paginated(self._openapi_rows(), params, "page", "pageSize")))

        return _json_response(404, _envelope(None, error_code=-404, msg=f"Not found: {path}"))


class RaisingTransport(httpx.BaseTransport):
    """Transport that raises on any use - for testing that disabled/blocked
    paths never touch the network at all (mirrors mcp-mikrotik's
    RaisingConnection)."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"HTTP request {request.method} {request.url} should not have been made")
