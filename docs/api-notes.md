# Omada controller API notes

Everything in this document was verified against a **real TP-Link OC200,
firmware v5.13.30.20**, on **2026-07-12**. It is the differentiator of this
project: the public TP-Link Omada API/SDK documentation is thin and, in
places, wrong or silent about exactly the details below. Where this
document extends beyond what was directly observed (e.g. an inferred
retry heuristic, an assumption about an unverified endpoint's shape), it
says so explicitly - treat those parts as best-effort, not verified fact.

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
This matters most for a future write tool (`set_radio_channel`, planned for
v0.2 - see README Roadmap): it must set `freq` (MHz), not rely on `channel`
alone, on the 5GHz radio.

### `GET /{oid}/api/v2/sites/{sid}/eaps/{MAC}`

Device detail for **AP/EAP devices only** - not verified for switches,
gateways, or other device types in this pass. Returns every field the
grid/devices row has, plus additional configuration fields: `ssidOverrides`,
`lanPortSettings`, `ledSetting`, and more. `get_device_detail` in this
package calls this endpoint only when the matched device's `type` is
`"ap"`/`"eap"`; any other type falls back to its grid/devices summary row
rather than guessing at an unverified detail path.

### `PATCH /{oid}/api/v2/sites/{sid}/eaps/{MAC}` (write - NOT exposed in v0.1)

Verified as a real, working endpoint, but **not exposed as a tool in this
release** (v0.1 is read-only end to end - see README). Documented here so
the v0.2 write layer starts from a correct understanding instead of
rediscovering this the hard way:

- `channel` must be sent as a **string**, and `freq` must be filled in with
  the target MHz value. Sending `channel` as an int, or omitting `freq`,
  returns `{"errorCode": 0, ...}` (looks like success) but the change is
  **silently discarded** - no error, no effect.
- On the 5GHz radio, `channel` must be the internal index (see the gotcha
  above), not the operator-facing channel number; `freq` is the actual
  source of truth the controller applies.
- The full `radioSetting` object must be resent on every write, not just
  the changed field(s) - a partial/sparse payload does not merge with the
  existing configuration the way a REST PATCH implies it should.

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
