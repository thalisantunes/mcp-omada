"""Exception hierarchy for mcp-omada.

Every exception here is meant to be caught at the MCP tool boundary (see
server.py) and turned into a clean, structured error for the tool caller.
Messages must never contain a device password, client secret, session
cookie, CSRF token, or Open API access token, and must never be a raw stack
trace from the underlying transport library.
"""

from __future__ import annotations


class OmadaMCPError(Exception):
    """Base class for all errors raised by mcp-omada."""


class ConfigError(OmadaMCPError):
    """Problem loading or validating environment configuration."""


class AuthenticationError(OmadaMCPError):
    """Could not authenticate to the Omada controller, in either auth mode.

    Never includes the credential itself (username, password, client
    secret) - only what the controller told us (or, if nothing at all was
    returned, a generic transport-level detail).
    """

    def __init__(self, detail: str):
        super().__init__(f"Authentication to the Omada controller failed: {detail}")
        self.detail = detail


class ControllerConnectionError(OmadaMCPError):
    """Could not establish or use an HTTP connection to the controller
    (DNS/TCP/TLS failure, timeout, connection refused, ...)."""

    def __init__(self, detail: str):
        super().__init__(f"Could not connect to the Omada controller: {detail}")
        self.detail = detail


class ControllerCommandError(OmadaMCPError):
    """The controller answered with a non-zero errorCode envelope."""

    def __init__(self, path: str, error_code: int | str | None, msg: str | None):
        super().__init__(f"Omada API error at {path!r}: errorCode={error_code!r} msg={msg!r}")
        self.path = path
        self.error_code = error_code
        self.msg = msg


class ValidationError(OmadaMCPError):
    """Input failed validation before ever being sent to the controller (e.g. a MAC address)."""


class SiteAmbiguousError(OmadaMCPError):
    """No OMADA_SITE_ID was configured and the controller manages more (or
    fewer) than exactly one site, so auto-selection is not possible."""

    def __init__(self, site_ids: list[str]):
        detail = f"{len(site_ids)} sites found ({', '.join(site_ids)})" if site_ids else "no sites found"
        super().__init__(f"OMADA_SITE_ID is not set and could not be auto-selected: {detail}.")
        self.site_ids = site_ids


class DeviceNotFoundError(OmadaMCPError):
    """A requested device (by MAC address) does not exist on the site."""

    def __init__(self, mac_address: str, site_id: str):
        super().__init__(f"Device {mac_address!r} not found on site {site_id!r}.")
        self.mac_address = mac_address
        self.site_id = site_id


class FeatureUnavailableError(OmadaMCPError):
    """A tool that only works through the legacy auth path was called while
    the server is configured with Open API (client_credentials) credentials
    only.

    See README's "Verified against real hardware" auth x endpoint matrix for
    exactly which tools/fields need which auth mode.
    """

    def __init__(self, feature: str, reason: str):
        super().__init__(f"{feature!r} is not available in this auth mode: {reason}")
        self.feature = feature
        self.reason = reason


class WriteDisabledError(OmadaMCPError):
    """A write tool was called while the server is running in read-only mode
    (the v0.2 default - see guard.py's module docstring)."""

    def __init__(self, operation: str):
        super().__init__(
            f"Write operation {operation!r} was blocked: server is running read-only "
            "(set OMADA_ALLOW_WRITE=true to enable writes)."
        )
        self.operation = operation


class GuardViolationError(OmadaMCPError):
    """A write operation was requested that is not present in the write
    allowlist (guard.ALLOWLIST).

    This should be unreachable from normal tool use: every write tool
    exposed in server.py calls a dedicated, named function in guard.py that
    references a fixed ALLOWLIST key. It exists as a defensive backstop in
    case a future write tool is wired up incorrectly - mirrors
    mcp-mikrotik's exceptions.GuardViolationError exactly.
    """

    def __init__(self, operation: str):
        super().__init__(f"Write operation {operation!r} is not in the write allowlist.")
        self.operation = operation


class RadioUnavailableError(OmadaMCPError):
    """The matched device has no `radioSetting<band>` to configure - either
    it isn't an AP/EAP device at all, or it's a single-band AP asked for the
    band it doesn't have (e.g. 5GHz on a 2.4GHz-only model)."""

    def __init__(self, mac_address: str, band: str, device_type: str):
        super().__init__(f"Device {mac_address!r} (type {device_type!r}) has no {band} radio to configure.")
        self.mac_address = mac_address
        self.band = band
        self.device_type = device_type
