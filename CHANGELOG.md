# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org).

## [0.1.0] - 2026-07-12 - Initial release (read-only)

Everything in this release was verified against a real TP-Link OC200,
firmware v5.13.30.20, on 2026-07-12 - see `docs/api-notes.md` for the full
write-up.

### Added

- Read tools: `get_controller_info`, `list_sites`, `list_devices`,
  `get_device_detail`, `get_wifi_summary`.
- Dual auth support, mutually exclusive per server run
  (`src/mcp_omada/client.py`, `config.AuthMode`):
  - **Legacy local-user login** (`OMADA_USER`/`OMADA_PASS`, preferred): CSRF
    token + session cookie against `/api/v2/*`. The only path `list_sites`
    and `get_wifi_summary` work through in v0.1.
  - **Open API `client_credentials`** (`OMADA_CLIENT_ID`/
    `OMADA_CLIENT_SECRET`): bearer-style access token against
    `/openapi/v1/*`, with a reduced field set (no per-radio WiFi data, no
    client counts).
  - Automatic `omadacId` discovery via the unauthenticated `GET /api/info`
    when `OMADA_OMADAC_ID` is unset; automatic site auto-selection when the
    controller manages exactly one site (legacy auth only).
  - One automatic re-login/re-token retry on a session/token that looks
    expired (`client._looks_like_auth_failure`) - a best-effort heuristic,
    not a verified session-expiry error code; see `docs/api-notes.md`.
- Field normalization (`src/mcp_omada/formatting.py`) reconciling three
  confirmed real-hardware quirks so tools return one consistent shape
  regardless of auth mode: `connected` (`statusCategory==1`/fallback
  `status==14` on legacy vs. `status==1` on Open API - the same field name
  meaning something different on each path), `uptime_seconds` (prefers
  `uptimeLong`, falls back to parsing the `uptime` display string), and
  WiFi channel (`actualChannel`, a string like `"11  / 2462MHz"` with
  irregular whitespace - parsed into `channel`/`freq_mhz`, with the 5GHz
  `channel`-is-an-internal-index gotcha documented).
- `src/mcp_omada/validation.py`: MAC address validation/normalization
  (colon, hyphen, Cisco-dotted, or bare input, normalized to Omada's own
  hyphenated-uppercase form) for `get_device_detail`/`get_wifi_summary`.
- Structured HTTP access via `httpx` (no shell/string-built requests -
  injection ruled out by construction), a persistent cookie jar for the
  legacy session, and a hard pagination cap (`MAX_PAGES`) as a backstop
  against unbounded reads.
- `docs/api-notes.md`: the full verified-against-real-hardware write-up,
  including a documented-but-not-yet-exposed write endpoint
  (`PATCH /{oid}/api/v2/sites/{sid}/eaps/{MAC}`) and its silent-discard
  gotcha (`channel` must be a string, `freq` must be filled in) - recorded
  in advance for the v0.2 write layer.
- Full pytest suite (100% coverage) against an in-memory fake controller
  (`tests/fakes.py`, an `httpx.MockTransport`-backed fake reproducing both
  auth flows and the exact confirmed JSON shapes); `ruff`/`mypy` clean;
  GitHub Actions CI (lint, type-check, test matrix on Python 3.11/3.12/3.13).

### Deliberately not included in v0.1

- **No write tool at all.** Unlike mcp-mikrotik's v0 (which shipped one
  guarded write tool, `set_identity`, from day one), this release has no
  write code path whatsoever - see README "Roadmap" for what v0.2 needs
  (following mcp-mikrotik's `guard.py` `ALLOWLIST` pattern) before adding
  one.
- **No Open API site listing.** No Open API sites-list endpoint was
  exercised against real hardware in this pass; a server configured with
  Open API credentials only must set `OMADA_SITE_ID` explicitly.
- **No v3-controller (pre-v5 UI) compatibility.** Recorded as an
  unverified historical note in `docs/api-notes.md` only.
