# mcp-omada

A [Model Context Protocol](https://modelcontextprotocol.io) server for
[TP-Link Omada](https://www.tp-link.com/en/omada-sdn/) SDN controllers -
read controller/site/device/WiFi state from an MCP client such as Claude
Code.

This is a from-scratch implementation, sibling to
[mcp-mikrotik](https://github.com/thalisantunes/mcp-mikrotik): same
philosophy (structured API calls only, no generic "run any command" tool,
tests against an in-memory fake instead of a real device, 100% test
coverage), applied to a very different transport (HTTP + JSON instead of
RouterOS's binary API) and a controller with two separate, non-interchangeable
authentication mechanisms - see "Verified against real hardware" below.

## Status

**v0.1: read-only.** Five read tools covering controller identity, sites,
devices, device detail, and per-AP WiFi summary. There is no write tool at
all yet - see "Roadmap" below for what v0.2 needs to get right before adding
one, and `docs/api-notes.md` for a verified write endpoint's gotchas,
documented in advance so v0.2 doesn't have to rediscover them.

## Installation

Requires Python >= 3.11.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Configuration

Configuration comes entirely from environment variables (v0.1 targets a
single controller - there is no multi-controller fleet file, unlike
mcp-mikrotik's `devices.yaml`).

1. Copy the example:
   ```bash
   cp .env.example .env
   ```
2. Edit `.env` (or export the variables another way):

   | Variable | Default | Meaning |
   |---|---|---|
   | `OMADA_BASE_URL` | *(required)* | Controller base URL, e.g. `https://192.168.1.2:8043` |
   | `OMADA_OMADAC_ID` | *(auto)* | Controller ID; auto-discovered via `GET /api/info` if unset |
   | `OMADA_SITE_ID` | *(auto)* | Site to operate on; auto-selected if the controller manages exactly one site (**legacy auth only** - see below) |
   | `OMADA_USER` / `OMADA_PASS` | - | Legacy local-user login (**preferred** - richer field set) |
   | `OMADA_CLIENT_ID` / `OMADA_CLIENT_SECRET` | - | Open API `client_credentials` (reduced field set) |
   | `OMADA_VERIFY_TLS` | `false` | Verify the controller's TLS certificate |
   | `OMADA_TIMEOUT` | `15` | HTTP request timeout, in seconds |
   | `OMADA_LOG_LEVEL` | `INFO` | Log level for the server process (stderr) |

   Set **either** `OMADA_USER`+`OMADA_PASS` **or**
   `OMADA_CLIENT_ID`+`OMADA_CLIENT_SECRET` - not partially, and if both
   pairs happen to be set, legacy wins (see "Verified against real
   hardware" for why it's the richer path). The Open API app itself is
   created in the controller UI: **Global View > Settings > Platform
   Integration > Open API**, mode **Client**, role **Viewer**.

   `OMADA_VERIFY_TLS` defaults to `false` (with a startup warning) because
   an OC200 commonly serves a self-signed certificate on its LAN management
   port - strict verification would refuse to connect out of the box. Set
   it to `true` once the controller has a certificate you can actually
   validate.

## Running

The server speaks MCP over **stdio** - it is meant to be launched by an MCP
client (e.g. configured as a command in Claude Code), not run as a network
service:

```bash
mcp-omada
# or, without installing the console script:
python -m mcp_omada.server
```

There is no HTTP transport in v0.1. If one is added later, it must default
to binding `127.0.0.1` (never `0.0.0.0`) and require a bearer token from an
environment variable - see the `TODO(http-transport)` note at the top of
`src/mcp_omada/server.py`.

## Tools

All read-only in v0.1.

| Tool | Description |
|---|---|
| `get_controller_info` | Controller identity: version, `omadac_id`, `configured`. Unauthenticated - works regardless of auth mode. |
| `list_sites` | Sites managed by this controller (id + name). **Requires legacy auth.** |
| `list_devices` | Devices on a site, normalized to one consistent shape regardless of auth mode - see below. |
| `get_device_detail` | Richest available detail for one device, by MAC (any common format accepted). |
| `get_wifi_summary` | Per-AP WiFi summary: parsed 2.4GHz/5GHz channel, client counts per band, radio utilization. **Requires legacy auth.** |

### Normalization

`list_devices`/`get_device_detail` return the same field names regardless
of which auth mode is active - a field unavailable in the current mode is
`null` rather than omitted, so a caller never has to branch on auth mode.
The normalization itself encodes three confirmed real-hardware gotchas
(full detail in `docs/api-notes.md`):

- **`connected`**: `statusCategory == 1` (primary) with fallback
  `status == 14` on the legacy path; `status == 1` on the Open API path -
  the *same field name* (`status`) means something different on each path.
- **`uptime_seconds`**: prefers `uptimeLong` (legacy-only, already
  seconds); falls back to parsing the `uptime` display string (e.g.
  `"1h 43m"`) when `uptimeLong` is absent - notably, always, on the Open
  API path.
- **WiFi channel**: `actualChannel` is a string like `"11  / 2462MHz"`
  (irregular whitespace) - parsed into `{"channel": 11, "freq_mhz": 2462}`.
  On the 5GHz radio, `channel` is an internal index, not the
  operator-recognizable channel number - `freq_mhz` is the reliable value.

## Verified against real hardware (OC200 v5.13.30.20)

Everything below was confirmed against a real OC200 running firmware
v5.13.30.20 on 2026-07-12 - not assumed from public docs (which are thin,
and in places silent about exactly these details). Full write-up,
including the verified-but-not-yet-exposed write endpoint and its
gotchas, in [`docs/api-notes.md`](docs/api-notes.md).

| Capability | Legacy (`/api/v2`) | Open API (`/openapi/v1`) |
|---|---|---|
| Controller identity (`/api/info`) | Yes (unauthenticated either way) | Yes (unauthenticated either way) |
| List sites | Yes | **Not verified** - no Open API sites-list endpoint was exercised; set `OMADA_SITE_ID` explicitly in this mode |
| List devices | Yes, rich fields | Yes, **reduced** fields |
| Per-device detail | Yes for AP/EAP devices (`/eaps/{MAC}`) | No separate endpoint verified - returns the list row |
| Per-radio WiFi detail (`wp2g`/`wp5g`) | Yes | **Absent entirely** |
| `connected` semantics | `statusCategory==1`, fallback `status==14` | `status==1` (different meaning, same field name) |

The two auth mechanisms (legacy session + CSRF token vs. Open API access
token) are **not interchangeable** - a session from one is rejected (empty
response) by the other's endpoints. See `src/mcp_omada/client.py`'s module
docstring and `docs/api-notes.md` for the full login flows.

## Security model

- **Read-only by construction, not by a runtime flag.** Unlike
  mcp-mikrotik's `MIKROTIK_ALLOW_WRITE` gate, v0.1 has **no write tool
  registered at all** - there is no code path to a write, guarded or
  otherwise, yet.
- **Structured HTTP, not shell commands.** All controller communication
  goes through [`httpx`](https://www.python-httpx.org/) with structured URL
  path segments, query parameters, and JSON bodies. Nothing in this
  codebase builds a request by concatenating strings from caller-supplied
  input, so injection through a MAC address or site ID is ruled out by
  construction rather than by input filtering.
- **Input validation on top, for its own sake.** `get_device_detail`/
  `get_wifi_summary`'s `mac` argument is still validated and normalized
  before use (`src/mcp_omada/validation.py`), purely to reject garbage
  input early with a clear error - not as an injection defense (see
  previous point).
- **No secrets in output or logs.** Password, client secret, CSRF token,
  session cookie, and Open API access token are never included in a log
  message or an exception's own text - exceptions carry only what the
  controller told us (an `errorCode`/`msg`), never the request that was
  sent. `Settings`' credential fields are all `repr=False`.
- **TLS verification is explicit, not silently bypassed.** `OMADA_VERIFY_TLS`
  defaults to `false` with a loud startup warning (not a silent
  downgrade) - see "Configuration" above for why an OC200 in LAN needs
  this by default.
- **Structured errors.** All errors raised inside the package derive from
  `OmadaMCPError` (`src/mcp_omada/exceptions.py`) and are caught at the
  tool boundary in `server.py`, which returns a clean, structured result.
  Unexpected exceptions are logged server-side and returned to the caller
  as a generic internal-error message, never as a raw traceback.

## Development

```bash
pip install -e ".[dev]"
pytest --cov=mcp_omada --cov-report=term-missing --cov-fail-under=100
ruff check .
ruff format --check .
mypy src/mcp_omada
```

The test suite never talks to a real controller: `tests/fakes.py` provides
an `httpx.MockTransport`-backed fake that reproduces both auth flows and
the exact JSON shapes (including the documented gotchas) confirmed against
real hardware, injected via a `client_factory` parameter on
`build_server()` - the same dependency-injection shape mcp-mikrotik's
`tests/fakes.py` uses for its RouterOS connection.

## Roadmap

- **v0.2 - guarded writes.** Following mcp-mikrotik's `guard.py`
  `ALLOWLIST` pattern exactly (a named, reviewable write operation per
  entry; a read-only gate checked before anything is touched; explicit
  `confirm`/before-after preview): `set_radio_channel` (must apply the
  `channel`-as-string + `freq`-filled-in write shape documented in
  `docs/api-notes.md`, or the controller silently discards the change),
  AP reboot, LED control.
- **v0.3 - clients/alerts/logs.** Read tools for connected client lists,
  controller alerts, and device/system logs.
- **v3-controller compatibility.** The pre-v5 controller UI uses a
  different login call and session cookie name entirely - recorded as a
  historical note (not independently verified) in `docs/api-notes.md`, for
  whoever picks this up.

## License

Apache-2.0 - see [LICENSE](LICENSE).
