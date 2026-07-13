# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org).

## [0.2.0] - 2026-07-13 - First guarded write + Insight clients/alerts

Everything in this release was verified against a real TP-Link OC200,
firmware v5.13.30.20 (`omadacId a5df88fd23ca87e694ceabac309add4b`, site
`6a539b2958964c12f8ed5cd1`), on 2026-07-13, while correcting the channels of
a real EAP fleet - see `docs/api-notes.md` for the full write-up.

### Added

- **`set_radio_channel`** - the first write tool this project exposes, and
  the reason the write-guard model exists as of this release. Sets an AP's
  2.4GHz or 5GHz radio channel via `PATCH /{oid}/api/v2/sites/{sid}/eaps/{MAC}`,
  always deriving both `channel` (string) and `freq` (int MHz) together from
  a single operator-facing channel number (`src/mcp_omada/channels.py`) and
  resending the COMPLETE current `radioSetting2g`/`radioSetting5g` object -
  the confirmed real-hardware silent-discard gotcha (`channel` as int, or a
  missing `freq` -> `errorCode 0`, "Success.", no actual effect) can't be
  hit by construction.
- **Empirical re-read verification.** `errorCode 0` from the controller is
  NOT trusted on its own: after a confirmed write, `set_radio_channel`
  re-reads the device and compares `freq` - the one field confirmed
  reliable on both bands - against what was requested. `applied=True` is
  returned ONLY when that comparison matches; a write the controller
  accepted but didn't actually apply (an uncharacterized cause beyond the
  two known ones - a DFS channel the firmware silently refuses, say) comes
  back as `applied=False` with a clear `WritePreview.message`, a new
  "rejected" outcome distinct from both "preview" and "error" - see
  `docs/api-notes.md`.
- **`src/mcp_omada/audit.py` + `src/mcp_omada/correlation.py`** - a
  structured, JSON-lines audit journal for every guarded write, following
  the model mcp-mikrotik's `audit.py`/`correlation.py`/`guard.py`
  `_audited` decorator established (studied first): one event per call
  (`preview`/`applied`/`rejected`/`error`), a per-call correlation id
  (`OMADA_AUDIT_LOG`, defaults to a stderr INFO line), before/after,
  `warning`, `message`, and aggressive key-based secret redaction - never a
  controller credential, in any outcome. The "rejected" outcome and the
  `endpoint`/`method` (HTTP) vocabulary are deliberate departures from
  mcp-mikrotik's own three-outcome, `path`/`action` (RouterOS) model - see
  `audit.py`'s module docstring for why Omada's controller needs the
  fourth outcome and RouterOS's doesn't.
- **`WritePreview.warning`** now covers every `set_radio_channel` write, not
  only 5GHz: changing a channel restarts the radio, momentarily
  disconnecting clients associated on that band (reassociation) - alongside
  the existing 5GHz-specific channel-persists-as-internal-index note (e.g.
  requesting channel 149 is followed by a re-read showing `channel: "17"`,
  with `freq` the only reliable round-trip value).
- **`src/mcp_omada/guard.py`** - the central write-guard, following the
  security model mcp-mikrotik's `guard.py` established: a named
  `ALLOWLIST` (one entry so far, `set_radio_channel`) mapping each write
  operation to exactly one fixed endpoint, a read-only gate
  (`OMADA_ALLOW_WRITE`, default `false`) checked before anything is read or
  written regardless of `confirm`, explicit `confirm`/before-after preview
  (`WritePreview`), re-read verification, and the audit journal above. No
  generic "call this endpoint with this body" tool exists anywhere in this
  package.
- **`OMADA_ALLOW_WRITE`** (`config.Settings.allow_write`, default `false`) -
  same shape and default as `MIKROTIK_ALLOW_WRITE`. `set_radio_channel` is
  registered unconditionally (like mcp-mikrotik's `set_identity`) and
  always cleanly refuses with `WriteDisabledError` when writes are
  disabled, rather than being silently absent from the tool list.
- **`OmadaClient._patch_v2`** - this package's first write primitive (PATCH
  against `/api/v2/*`, legacy auth only), following the exact same
  "NOT exposed as an MCP tool directly, only guard.py may call it"
  convention as every read primitive.
- **`get_clients`** - Insight/known clients on a site
  (`GET /sites/{sid}/insight/clients`): mac, name, download/upload bytes,
  duration, last_seen (epoch ms), guest/wireless flags, VLAN id, block/
  manager flags. Legacy auth only.
- **`get_alerts`** - active alerts on a site
  (`GET /sites/{sid}/alerts`). The pagination envelope is confirmed against
  real hardware; the shape of an individual alert row is honestly flagged
  as **unverified** (`totalRows` was 0 - a healthy network - at
  verification time) - `formatting.normalize_alert` returns a best-effort
  `module`/`level`/`content`/`time` guess plus the untouched `raw` row, so
  nothing is lost if the guess is wrong. Legacy auth only.
- New exceptions (`src/mcp_omada/exceptions.py`): `WriteDisabledError`,
  `GuardViolationError` (defensive backstop, mirrors mcp-mikrotik's),
  `RadioUnavailableError` (a matched device isn't an AP/EAP, or is a
  single-band AP missing the requested radio entirely).
- `docs/api-notes.md`: the sibling-band preservation assumption behind
  `set_radio_channel` (a PATCH body only ever contains ONE of
  `radioSetting2g`/`radioSetting5g` - never both, relying on the controller
  to leave the untouched band's configuration alone) is now documented
  explicitly as **verified by live operation** (a real per-band channel
  correction), **not by an isolated unit test** - now backed by a fake-based
  regression test too (`test_set_radio_channel_leaves_sibling_band_untouched`).
- `tests/fakes.py`'s `FakeOmadaController` now simulates the PATCH write
  path faithfully, including the silent-discard gotcha (an int `channel` or
  missing `freq` is accepted but never actually applied - tracked in
  `silent_discards` so a regression shows up as a failing assertion, not a
  silently-wrong pass), an uncharacterized-rejection mode
  (`reject_next_patch_uncharacterized`, exercising the re-read verification
  above), and the confirmed 5GHz channel-persists-as-internal-index
  behavior (`149` -> `"17"` on re-read).
- Full pytest suite (100% coverage, 239 tests) covering the guard's
  read-only gate, preview-vs-confirm, before/after correctness on both
  bands (now sourced from the post-write re-read, not merely the intended
  write), the expanded warning, re-read verification (both the verified and
  the rejected path), the audit journal across all four outcomes and every
  error type this tool can raise, correlation-id propagation, device/radio
  resolution errors, session-expiry retry on a write, sibling-band
  preservation, and the write primitive's silent-discard behavior directly;
  `ruff`/`mypy` clean.

### Deliberately not included in v0.2

- **`get_logs` (device/system logs).** Every endpoint path tried against
  real hardware (`log`, `logs`, `logs/queryLog` GET+POST,
  `setting/logs/logs`, `insight/logs`) returned `errorCode -1600` - no
  working endpoint was identified in this pass. Deferred to v0.3 - see
  `docs/api-notes.md`/README Roadmap.
- **No Open API equivalent for `get_clients`/`get_alerts`/`set_radio_channel`.**
  None was exercised against real hardware in this pass; all three raise
  `FeatureUnavailableError` in Open API-only mode, matching v0.1's
  `list_sites`/`get_wifi_summary` precedent.
- **AP reboot / LED control.** Considered for this release, deferred to
  v0.3: reboot needs its own confirmation/cooldown policy (no meaningful
  before/after preview for a reboot, no rollback for a bad batch reboot) -
  mirrors mcp-mikrotik's own reasoning for excluding `system/reboot` from
  *its* allowlist.

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
