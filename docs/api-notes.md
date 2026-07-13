# Omada controller API notes

Everything in this document was verified against a **real TP-Link OC200,
firmware v5.13.30.20** (`omadacId a5df88fd23ca87e694ceabac309add4b`, site
`6a539b2958964c12f8ed5cd1`), across two passes: **2026-07-12** (v0.1 - reads)
and **2026-07-13** (v0.2 - `set_radio_channel`, `get_clients`, `get_alerts`,
while correcting the channels of a real EAP fleet). It is the differentiator
of this project: the public TP-Link Omada API/SDK documentation is thin and,
in places, wrong or silent about exactly the details below. Where this
document extends beyond what was directly observed (e.g. an inferred retry
heuristic, an assumption about an unverified endpoint's shape), it says so
explicitly - treat those parts as best-effort, not verified fact.

## Authentication: two worlds that never mix

Omada controllers (v5.x) expose two completely separate, non-interoperable
authentication mechanisms. A session/token from one is **rejected (empty
response)** by the other's endpoints.

### 1. Legacy login (local controller user)

```
POST /{omadacId}/api/v2/login
Content-Type: application/json

{"username": "...", "password": "..."}
```

Response:

```json
{"errorCode": 0, "msg": "Success.", "result": {"token": "<csrf-token>"}}
```

The response also sets a session cookie (`TPOMADA_SESSIONID`) via
`Set-Cookie`. **Every subsequent request to `/api/v2/*` must carry BOTH**:

- The session cookie, via a persistent cookie jar (this client reuses one
  `httpx.Client` instance for its whole lifetime - a fresh client per
  request would silently drop the cookie and every call would 401/403).
- A `Csrf-Token: <token>` header, set from the login response's
  `result.token`.

This is the path this package calls **legacy** (`AuthMode.LEGACY`). It is
the richer, preferred path: `list_sites`, `get_wifi_summary`, and full
per-device detail (for APs) only work through it in v0.1 - see the matrix
below.

### 2. Open API (`client_credentials`)

```
POST /openapi/authorize/token?grant_type=client_credentials
Content-Type: application/json

{"omadacId": "...", "client_id": "...", "client_secret": "..."}
```

Response:

```json
{"errorCode": 0, "result": {"accessToken": "...", "expiresIn": 7200}}
```

Every subsequent request to `/openapi/v1/*` must carry:

```
Authorization: AccessToken=<accessToken>
```

The token is valid ~7200s (2 hours). This client refreshes it proactively
60s before expiry (`TOKEN_REFRESH_MARGIN_SECONDS` in `client.py`), and
reactively (one retry) if a request still comes back looking like an
auth failure.

The Open API app itself must be created in the controller UI first:
**Global View > Settings > Platform Integration > Open API**, mode
**Client**, role **Viewer** (sufficient for every v0.1 read tool).

### Auth x endpoint matrix (what actually works where)

| Capability | Legacy (`/api/v2`) | Open API (`/openapi/v1`) |
|---|---|---|
| Controller identity (`/api/info`) | Yes (unauthenticated either way) | Yes (unauthenticated either way) |
| List sites | Yes - `GET /{oid}/api/v2/sites` | **Not verified.** No Open API sites-list endpoint was exercised against real hardware in this pass. `list_sites`/site auto-selection raise `FeatureUnavailableError`/`ConfigError` in Open API-only mode - set `OMADA_SITE_ID` explicitly. |
| List devices | Yes, rich fields (see below) | Yes, **reduced** fields (see below) |
| Per-device detail | Yes for AP/EAP devices, via `/eaps/{MAC}` (richer than the list row) | No separate endpoint verified - `get_device_detail` returns the matching list row |
| WiFi per-radio detail (`wp2g`/`wp5g`, channel, per-band client counts) | Yes (present directly on the device list rows) | **Absent entirely** - `get_wifi_summary` raises `FeatureUnavailableError` |
| `connected` semantics | `statusCategory==1` (primary), fallback `status==14` | `status==1` (a *different* meaning of the same field name) |
| Insight/known clients (`get_clients`) | Yes - `GET /{oid}/api/v2/sites/{sid}/insight/clients` | **Not verified.** No Open API equivalent was exercised - `get_clients` raises `FeatureUnavailableError`. |
| Alerts (`get_alerts`) | Yes (envelope only - see below) | **Not verified.** `get_alerts` raises `FeatureUnavailableError`. |
| Set AP radio channel (`set_radio_channel`, v0.2 write) | Yes - `PATCH /{oid}/api/v2/sites/{sid}/eaps/{MAC}` | **Not verified.** `set_radio_channel` raises `FeatureUnavailableError` in Open API-only mode. |
| Device/system logs (`get_logs`) | **Not found.** Every path tried (`log`, `logs`, `logs/queryLog` GET+POST, `setting/logs/logs`, `insight/logs`) returned `errorCode -1600` - see "Endpoints tried and NOT found" below. Not implemented in v0.2 - pending v0.3 (see README Roadmap). | Not attempted. |

## Verified endpoints

### `GET /api/info` (no auth)

```json
{"result": {"controllerVer": "5.13.30.20", "omadacId": "<uuid>", "configured": true}}
```

Used to auto-discover `omadacId` when `OMADA_OMADAC_ID` is not set. Works
identically before login/token and regardless of which auth mode is
configured - this is the one call this client never gates behind
authentication.

### `GET /{oid}/api/v2/sites?currentPage=1&currentPageSize=10`

`result.data[]` items: `{"id": "...", "name": "..."}`. The site identifier
field is **`id`, not `siteId`** - easy to get wrong by analogy with other
Omada API examples online.

### `GET /{oid}/api/v2/sites/{sid}/grid/devices?currentPage=1&currentPageSize=100`

`result.data[]` items carry (non-exhaustive, only what this package reads):

| Field | Notes |
|---|---|
| `type` | e.g. `"ap"` |
| `mac` | Hyphenated, uppercase: `"50-D4-F7-66-0D-9C"` |
| `name`, `ip`, `model`, `compoundModel`, `firmwareVersion`, `needUpgrade` | |
| `status` | Int. **14 == connected** on this path only. |
| `statusCategory` | Int. **1 == connected** - prefer this over `status` when present (more stable across firmware than the legacy status codes). |
| `uptime` | String, e.g. `"1h 43m"` |
| `uptimeLong` | Int, same uptime in **seconds** - prefer this; `uptime` is a display string, not a reliable parse target on its own. |
| `cpuUtil`, `memUtil` | |
| `clientNum`, `clientNum2g`, `clientNum5g` | |
| `txRate`, `rxRate`, `upload`, `download` | |
| `wp2g`, `wp5g` | Nested per-radio objects - see below. Absent when a radio doesn't exist (e.g. a single-band AP has no `wp5g`) or the device isn't an AP at all. |

`wp2g`/`wp5g` nested fields:

| Field | Notes |
|---|---|
| `actualChannel` | **String**, not a number: `"11  / 2462MHz"` - note the irregular double space before the slash. Format is `"<channel>  / <freq>MHz"`. Parsed by `formatting.parse_channel`, tolerant of any whitespace amount around the slash (the exact double-space spacing observed is very unlikely to be a stable contract). |
| `maxTxRate`, `txPower`, `bandWidth`, `rdMode` | |
| `txUtil`, `rxUtil`, `interUtil` | |

**5GHz gotcha:** on the 5GHz radio, the `channel` half of `actualChannel`
is an **internal index**, not the channel number an operator/RF planner
would recognize - e.g. `"17  / 5745MHz"` is channel **149** in the 5GHz
plan, not channel 17. `freq_mhz` (5745) is the reliable, unambiguous value.
This is exactly why `set_radio_channel` (v0.2 - see below) always derives
and writes `freq` (MHz) itself rather than relying on `channel` alone on
the 5GHz radio.

### `GET /{oid}/api/v2/sites/{sid}/eaps/{MAC}`

Device detail for **AP/EAP devices only** - not verified for switches,
gateways, or other device types in this pass. Returns every field the
grid/devices row has, plus additional configuration fields: `ssidOverrides`,
`lanPortSettings`, `ledSetting`, and more. `get_device_detail` in this
package calls this endpoint only when the matched device's `type` is
`"ap"`/`"eap"`; any other type falls back to its grid/devices summary row
rather than guessing at an unverified detail path.

### `GET /{oid}/api/v2/sites/{sid}/eaps/{MAC}` - `radioSetting2g`/`radioSetting5g` (v0.2)

Confirmed on 2026-07-13 while correcting a real EAP fleet's channels: the
`/eaps/{MAC}` detail response (see above) also carries `radioSetting2g` and
`radioSetting5g` - CONFIG objects, distinct from the `wp2g`/`wp5g` RUNTIME
summary objects the grid/devices list already exposes. Confirmed shape:

```json
{
  "radioEnable": true,
  "channelWidth": "4",
  "channel": "11",
  "txPower": 21,
  "txPowerLevel": 4,
  "freq": 2462,
  "wirelessMode": -2
}
```

(`radioSetting5g` has the same shape, with `channelWidth` typically `"6"`
for an 80MHz-capable radio.) These are the objects `set_radio_channel`
reads before writing, and the write target described next.

### `PATCH /{oid}/api/v2/sites/{sid}/eaps/{MAC}` - `set_radio_channel` (v0.2 write, GUARDED)

The first (and, as of v0.2, only) write this package exposes - via
`guard.set_radio_channel`, gated by `OMADA_ALLOW_WRITE` and `confirm` (see
README "Security model"). Verified flow (this is exactly how the channels
of a real EAP fleet were corrected on 2026-07-13):

1. `GET /{oid}/api/v2/sites/{sid}/eaps/{MAC-with-hyphens}` to read the
   device's current `radioSetting2g`/`radioSetting5g`.
2. Modify the ONE field pair that changed (`channel`+`freq`) and resend the
   **COMPLETE** object (not a partial one) via
   `PATCH /{oid}/api/v2/sites/{sid}/eaps/{MAC}`, body
   `{"radioSetting2g": {...}}` or `{"radioSetting5g": {...}}`.

**CRITICAL confirmed gotcha - silent discard:** `channel` MUST be sent as a
**STRING**, and `freq` MUST be filled in with the matching frequency in
MHz. If `channel` is sent as an int, or `freq` is left out/zero, the
controller responds `{"errorCode": 0, "msg": "Success."}` (looks like
success) but **silently discards the change** - no error, no effect, and no
way to distinguish this from a real success except by re-reading the
device. `set_radio_channel` never lets a caller hit this: it always derives
both `channel` (string) and `freq` (int MHz) together from the same
operator-facing channel number, via `channels.channel_to_freq` - a caller
can never supply `freq` directly, and `channel` is always
`str(channel_number)` before it reaches the request body. `tests/fakes.py`
reproduces this discard behavior exactly (`_handle_patch_eap`), so a
regression that ever sent an int `channel` or omitted `freq` would show up
as a failing "before/after" assertion, not a silently-wrong success.

**Confirmed gotcha - 5GHz channel persists as an internal index:** on the
5GHz radio, `channel` is persisted/echoed back on a subsequent read as an
**internal index**, not the channel number that was requested - e.g.
requesting channel **149** (freq 5745) is followed by a re-read showing
`channel: "17"` (with `freq: 5745` unchanged and correct). `freq` is the
only reliable value to verify a 5GHz write actually took effect;
`set_radio_channel`'s `WritePreview.warning` field says so explicitly on
every 5GHz write, so a caller reading only `applied`/`after` still can't
miss it. On 2.4GHz, `channel` and `freq` stay in lockstep with the operator
channel number (e.g. channel 11 = freq 2462) - no such gotcha there.

**Every channel write restarts the radio.** Regardless of band, applying a
channel change disconnects clients currently associated on that radio - they
reassociate once it comes back up. Not something the API response itself
signals in any way (no field marks this) - it follows from what a channel
change *is* on any 802.11 radio, confirmed by observing the fleet's clients
drop and reconnect during the live channel corrections this feature was
verified against. `set_radio_channel`'s `WritePreview.warning` states this
on EVERY write (both bands), not only the 5GHz-index caveat above.

**Full-object resend requirement:** the complete `radioSetting<band>`
object must be resent on every write, not just the changed field(s) - a
partial/sparse payload does not merge with the existing configuration the
way a REST `PATCH` implies it should. `set_radio_channel` always reads the
current object first and only overwrites `channel`/`freq` in a copy of it.

**Assumption - PATCH targets only the band being changed, never both.**
`set_radio_channel`'s request body always contains exactly ONE of
`radioSetting2g`/`radioSetting5g` (the band being changed), never both,
relying on the controller to leave the untouched sibling band's
configuration exactly as it was. This was **verified by live operation**
(per-band channel corrections against the real EAP fleet on 2026-07-13 -
changing 2.4GHz never altered the AP's 5GHz configuration, and vice versa)
- it was **not** independently confirmed by an isolated unit test until
this write-up (see `tests/test_guard.py`'s
`test_set_radio_channel_leaves_sibling_band_untouched`, added afterward as
a regression lock, not as the original verification).

**Empirical re-read verification - `errorCode 0` is NOT trusted on its
own.** Both gotchas above are the two KNOWN causes of a silent discard, but
a controller answering `errorCode 0` is not proof a write actually applied
for any OTHER, uncharacterized reason (a DFS channel the firmware silently
refuses, for instance - not independently confirmed, but plausible and not
ruled out either). So after every confirmed write, `set_radio_channel`
performs a SECOND `GET /eaps/{MAC}` and compares the resulting `freq` -
never `channel`, given the 5GHz index gotcha above - against the requested
value:
- Match -> `applied=True`, `WritePreview.after` is the re-read's actual
  radioSetting object (empirical, not merely the intended write).
- Mismatch -> `applied=False` even though the PATCH itself reported
  success, with `WritePreview.message` explaining exactly what didn't
  match (e.g. `"Controller accepted the write (errorCode 0) but the change
  was NOT confirmed on re-read: freq is still 2462 (expected 2437)."`).
  This is a distinct outcome from a `confirm=False` preview (which never
  attempts a write at all) - the audit journal below records it as
  `"rejected"`, not `"preview"`.

This closes the "trust the envelope" gap the two known gotchas alone don't
fully cover - `set_radio_channel` never reports success without positive,
independent confirmation from the device itself.

**Channel/frequency table** (`channels.py`): 2.4GHz channels 1-13
(`freq = 2412 + 5*(n-1)`, confirmed for channel 11 = 2462MHz); 5GHz's
common UNII-1/2/2e/3 channel list (`freq = 5000 + 5*channel`, confirmed for
channel 149 = 5745MHz - the standard IEEE 802.11 relationship, applied to
the rest of each band's channel list, not independently confirmed
channel-by-channel). A channel/band combination not in this table is
rejected with `ValidationError` before the device is ever touched.

**Legacy auth only** - not verified under the Open API in this pass;
`set_radio_channel` raises `FeatureUnavailableError` in Open API-only mode.
**AP/EAP devices only** - raises `RadioUnavailableError` for any other
device type, or for a single-band AP asked for the band it doesn't have.

### Audit journal (v0.2) - `src/mcp_omada/audit.py` + `correlation.py`

Every `set_radio_channel` call - however it ends - is recorded as one
JSON-lines event via `audit.record()` (see `guard.py`'s `_audited`
decorator, the only caller). Destination: `OMADA_AUDIT_LOG` (a file path),
appended to as one JSON line per event; a plain INFO-level stderr line if
unset. Each event carries a `correlation_id` (a 12-hex-char id, one per MCP
tool call - see `correlation.py`), `device` (the AP's MAC), `tool`,
`operation`, `action` (the write's HTTP method, `"PATCH"`), `confirm`, an
`outcome`, and a `summary` (`before`/`after`/`warning`/`message`).

Four outcomes, one more than mcp-mikrotik's own three
(`"preview"`/`"applied"`/`"error"`) - the extra one, `"rejected"`, exists
because Omada's controller can answer `errorCode 0` without actually
applying a write (see the re-read verification above), something
RouterOS's own API doesn't do:

| Outcome | When |
|---|---|
| `preview` | `confirm=False` - nothing was sent to the device. |
| `applied` | `confirm=True`, PATCH sent, post-write re-read confirmed the change. |
| `rejected` | `confirm=True`, PATCH sent, controller said `errorCode 0`, but the post-write re-read did NOT confirm the change. |
| `error` | Any exception, at any point - even a read-only-gate refusal before the device is ever touched. |

No controller credential (password, client secret, CSRF token, session
cookie, Open API access token) can ever reach a journal entry -
`audit._sanitize()` recursively drops any dict key matching a broad
password/secret/token/credential/passphrase/psk/pre-shared/private/cookie
pattern, on top of the fact that a `radioSetting` object never legitimately
carries one in the first place. Writing the journal is best-effort and
never raises - a bad `OMADA_AUDIT_LOG` path or an unserializable value is
caught, logged as a warning, and never blocks or fails the write it
describes.

### `GET /openapi/v1/{oid}/sites/{sid}/devices?page=1&pageSize=100` (Open API)

`result.data[]` items - **reduced field set**, confirmed missing relative to
the legacy grid/devices row: no `clientNum`/`clientNum2g`/`clientNum5g`, no
`wp2g`/`wp5g` at all. Present: `name`, `type`, `mac`, `ip`, `status`
(**1 == connected here - a different meaning than the legacy path's
`status`**), `cpuUtil`, `memUtil`, `uptime` (string only - **no
`uptimeLong`**, so this package's `uptime_seconds` falls back to parsing
the string on this path), `model`, `firmwareVersion`, `sn`, `lastSeen`.

**Not independently verified in this pass** (used defensively, degrading
gracefully if wrong - see `client._paginate_openapi`): the exact field
names of the Open API list envelope beyond `result.data[]` (this client
assumes `result.totalRows`, mirroring the legacy envelope, but only uses it
as an early-stop optimization - pagination still terminates correctly via
"a page came back shorter than requested" even if that assumption is
wrong).

### `GET /{oid}/api/v2/sites/{sid}/insight/clients?currentPage=1&currentPageSize=N` (v0.2)

The controller's "Insight" view: historical + currently-known clients, not
just currently-associated ones. Confirmed envelope:
`result.{currentPage,currentSize,totalRows,data:[...]}` (same shape as
every other paginated `/api/v2` list this package reads). Confirmed row
fields (`get_clients`/`formatting.normalize_client`):

| Field | Notes |
|---|---|
| `mac` | Hyphenated, uppercase - same format as a device `mac`. |
| `name` | |
| `download`, `upload` | Bytes, int. |
| `duration` | Seconds, int. |
| `lastSeen` | Epoch **milliseconds**, int. |
| `guest`, `wireless` | Bool. |
| `vid` | VLAN id, int. |
| `block`, `blockDisable`, `lockToAp`, `manager` | Bool. |

Legacy auth only - no Open API equivalent has been verified against real
hardware; `get_clients` raises `FeatureUnavailableError` in Open API-only
mode.

### `GET /{oid}/api/v2/sites/{sid}/alerts?currentPage=1&currentPageSize=N` (v0.2)

**Envelope confirmed, ROW SHAPE NOT VERIFIED.** The pagination envelope
(`result.{currentPage,currentSize,totalRows,data:[...]}`) is confirmed
against real hardware - `totalRows` was `0` at verification time (a
healthy network, no active alerts), so the envelope was observed but no
actual alert row ever came back through the API. `formatting.normalize_alert`
reads `module`/`level`/`content`/`time` - keys taken from the controller's
own alerts UI, **not** a captured API response - and treats them as an
honest best-effort guess, not a fact, until corrected against a real
alert. Every normalized alert also carries the untouched `raw` row, so a
caller sees whatever the controller actually sends regardless of whether
the guess above is right. `get_alerts` is legacy-auth only (no Open API
equivalent verified).

### Endpoints tried and NOT found - `get_logs` (device/system logs)

Every path tried for a logs-equivalent endpoint returned `errorCode -1600`
(a generic "not found"-shaped error) against this controller: `log`,
`logs`, `logs/queryLog` (both GET and POST), `setting/logs/logs`,
`insight/logs` - all under `/{oid}/api/v2/sites/{sid}/...`. No working
device/system log endpoint was identified in this pass. **Not implemented
in v0.2** - a `get_logs` tool is deferred to v0.3 pending either a
successful endpoint discovery or public documentation pointing at the
right path (see README Roadmap).

### Legacy v3 UI (pre-v5 controllers) - historical note only

Older Omada controller UIs (pre-v5, "v3" web UI) used a different login
call entirely: `GET/POST /api/user/login?ajax` with a JSON body shaped
`{"method": "login", "params": {...}}`, and a **different** session cookie
name, `TPEAP_SESSIONID` (vs v5's `TPOMADA_SESSIONID`). **Not verified
against real v3 hardware in this pass** - recorded here only so a future
contributor adding compatibility for older controllers (see README
Roadmap's "v0.3 ... compat v3 legado") knows where to start, not as a
confirmed working integration.

## Design decisions not directly dictated by the verified API

These are choices this package made to fill gaps the verified knowledge
above doesn't cover, documented here so they're easy to revisit:

- **Session-expiry detection is a heuristic, not a verified error code.**
  The exact `errorCode` an OC200 returns for "your CSRF token/session just
  expired" was not captured during this verification pass (nothing expired
  during the ~1-session window tested). `client._looks_like_auth_failure`
  treats HTTP 401/403, or a `msg` containing "token"/"login"/"session"/
  "csrf", as reason enough to attempt exactly one automatic re-login/
  re-token and retry. A false negative here just means a genuinely expired
  session surfaces as a normal `ControllerCommandError` instead of being
  silently retried - the safe direction to be wrong in.
- **Open API site auto-discovery is unsupported, by design, not oversight.**
  Since no Open API sites-list endpoint was verified, a server configured
  with Open API credentials only requires `OMADA_SITE_ID` to be set
  explicitly; `resolve_site_id` raises a clear `ConfigError` naming exactly
  that requirement rather than guessing at an unverified endpoint shape.
- **Pagination is capped at `MAX_PAGES = 20`** (`client.py`) as a hard
  backstop against a runaway controller or a bookkeeping bug turning one
  tool call into unbounded HTTP requests - mirrors mcp-mikrotik's `logs`
  tool capping `limit` at `MAX_LOG_LIMIT`.
- **`set_radio_channel` is registered unconditionally, gated at call time
  (v0.2).** Mirrors mcp-mikrotik's `set_identity`: the tool is always
  present in `list_tools()`; `OMADA_ALLOW_WRITE=false` (the default) makes
  every call raise a clean `WriteDisabledError` instead of the tool being
  silently absent. A caller (human or LLM) gets an explicit, diagnosable
  "read-only" error either way, never a confusing "unknown tool".
- **The write primitive (`OmadaClient._patch_v2`) is architecturally, not
  mechanically, restricted to guard.py.** Exactly like mcp-mikrotik's
  `MikrotikClient.update()`: `_patch_v2(path, json_body)` still technically
  accepts any path/body a Python caller passes it - the guarantee that only
  `guard.set_radio_channel` ever calls it with a fixed, reviewed path comes
  from the "one named function per ALLOWLIST entry, never a generic tool"
  convention (see `guard.py`'s module docstring), not from a runtime
  restriction on the primitive itself. Same trade-off, same justification,
  as the sibling project.
- **5GHz's internal-channel-index quirk is documented, not "fixed".**
  `set_radio_channel` always WRITES the operator-facing channel number
  (e.g. `"149"`) as `channel`, alongside the correct `freq` - it does not
  attempt to pre-translate to whatever internal index the controller might
  echo back later (only one such mapping, 149->"17", was ever observed, not
  enough to build a reliable translation table from). The `WritePreview.warning`
  field surfaces this honestly instead of silently masking it.
- **`get_clients`/`get_alerts` are legacy-auth-only, matching the pattern
  already established by `list_sites`/`get_wifi_summary` in v0.1** - no
  Open API equivalent was exercised for either, so both raise
  `FeatureUnavailableError` rather than guessing at an unverified path.
- **`get_logs` was deliberately left out of v0.2, not forgotten.** Every
  endpoint path tried returned `errorCode -1600` (see "Endpoints tried and
  NOT found" above) - rather than ship a tool against a guessed, unverified
  path, it stays out until a working endpoint is found (v0.3).
- **"Mirrors mcp-mikrotik's guard.py" is a starting point, not a claim of
  identity - corrected after an adversarial review flagged the earlier
  wording as overstated.** Before this write-up's second pass, `guard.py`
  described itself as mirroring mcp-mikrotik "exactly," which was true of
  the read-only gate/allowlist/confirm-preview shape but not of the
  journal (which didn't exist yet) or of two real, deliberate departures
  that remain true today: the `"rejected"` outcome (mcp-mikrotik has no
  equivalent - see "Audit journal" above) and the empirical re-read
  verification itself (RouterOS's API doesn't need one). The module
  docstrings and README now say "follows the same security model" and
  name the differences explicitly, instead of claiming an exact mirror.
- **Re-read verification checks `freq`, never `channel` - by the same logic
  as everything else in this document.** The 5GHz internal-index gotcha
  already establishes that `channel` is not a reliable round-trip value;
  building the NEW verification step on top of `channel` instead of `freq`
  would have reintroduced the same class of bug this document exists to
  prevent. `freq` is the only field checked, on both bands, for
  consistency (2.4GHz's `channel`/`freq` do stay in lockstep, but nothing
  is gained by treating the two bands differently here).
- **The re-read is a second, separate `GET /eaps/{MAC}` call, not a reuse of
  the PATCH response.** Only `errorCode`/`msg` were confirmed on the
  `PATCH` response during verification - not whether its `result` ever
  echoes the new state on a real controller. `set_radio_channel` never
  assumes either way: it always re-reads with a fresh `GET` rather than
  trying to parse a verification result out of the `PATCH` response body,
  which costs this feature a second round-trip but avoids depending on an
  unverified assumption about that response's shape. `tests/fakes.py`
  models the `PATCH` response as an empty `result: {}` - a plausible but
  **not independently verified** choice, made only because the fake has to
  return something.

## RBAC on writes (verified live 2026-07-13)

`set_radio_channel` (PATCH /eaps/{mac}) requires an **Administrator-role**
Omada user. A Viewer-role local user (e.g. one created for the read tools)
is rejected by the controller's own RBAC with `errorCode -1007` ("The
current user does not have permissions to access this page.") — even when
`OMADA_ALLOW_WRITE=true`. This is a defense-in-depth layer independent of
this server's write guard: the audit journal records such a rejection with
`outcome=error` (verified: correlation id present, no secret leaked). Read
tools (get_clients, get_alerts, list_devices, etc.) work fine under Viewer.
