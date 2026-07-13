# mcp-omada

A [Model Context Protocol](https://modelcontextprotocol.io) server for
[TP-Link Omada](https://www.tp-link.com/en/omada-sdn/) SDN controllers -
read controller/site/device/WiFi state and, for one guarded write, change
it, from an MCP client such as Claude Code.

This is a from-scratch implementation, sibling to
[mcp-mikrotik](https://github.com/thalisantunes/mcp-mikrotik): same
philosophy (structured API calls only, no generic "run any command" tool,
tests against an in-memory fake instead of a real device, 100% test
coverage), applied to a very different transport (HTTP + JSON instead of
RouterOS's binary API) and a controller with two separate, non-interchangeable
authentication mechanisms - see "Verified against real hardware" below.

## Status

**v0.2: read tools + the first guarded write.** Seven read tools (controller
identity, sites, devices, device detail, per-AP WiFi summary, Insight
clients, alerts) plus one write tool, `set_radio_channel`, gated by the same
read-only-by-default + central allowlist + confirm/preview model
[mcp-mikrotik](https://github.com/thalisantunes/mcp-mikrotik) established -
see `src/mcp_omada/guard.py` and "Security model" below.

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
   | `OMADA_ALLOW_WRITE` | `false` | Enable write tools (`set_radio_channel`) - see "Security model" |

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

### Read-only

| Tool | Description |
|---|---|
| `get_controller_info` | Controller identity: version, `omadac_id`, `configured`. Unauthenticated - works regardless of auth mode. |
| `list_sites` | Sites managed by this controller (id + name). **Requires legacy auth.** |
| `list_devices` | Devices on a site, normalized to one consistent shape regardless of auth mode - see below. |
| `get_device_detail` | Richest available detail for one device, by MAC (any common format accepted). |
| `get_wifi_summary` | Per-AP WiFi summary: parsed 2.4GHz/5GHz channel, client counts per band, radio utilization. **Requires legacy auth.** |
| `get_clients` | Insight/known clients on a site: mac, name, download/upload bytes, duration, last_seen, guest/wireless flags, VLAN, block/manager flags. **Requires legacy auth.** |
| `get_alerts` | Active alerts on a site. Pagination envelope confirmed against real hardware; individual alert row shape is a documented best-effort guess (`raw` always included) - see `docs/api-notes.md`. **Requires legacy auth.** |

### Write (guarded)

| Tool | Description |
|---|---|
| `set_radio_channel` | Set an AP's 2.4GHz or 5GHz radio channel. Requires `OMADA_ALLOW_WRITE=true` and `confirm=true`; see "Security model" below. **Requires legacy auth.** |

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
v5.13.30.20, across two verification passes (2026-07-12 reads, 2026-07-13
`set_radio_channel`/`get_clients`/`get_alerts` - the latter while correcting
the channels of a real EAP fleet) - not assumed from public docs (which are
thin, and in places silent about exactly these details). Full write-up,
including the write endpoint's silent-discard gotcha, in
[`docs/api-notes.md`](docs/api-notes.md).

| Capability | Legacy (`/api/v2`) | Open API (`/openapi/v1`) |
|---|---|---|
| Controller identity (`/api/info`) | Yes (unauthenticated either way) | Yes (unauthenticated either way) |
| List sites | Yes | **Not verified** - no Open API sites-list endpoint was exercised; set `OMADA_SITE_ID` explicitly in this mode |
| List devices | Yes, rich fields | Yes, **reduced** fields |
| Per-device detail | Yes for AP/EAP devices (`/eaps/{MAC}`) | No separate endpoint verified - returns the list row |
| Per-radio WiFi detail (`wp2g`/`wp5g`) | Yes | **Absent entirely** |
| `connected` semantics | `statusCategory==1`, fallback `status==14` | `status==1` (different meaning, same field name) |
| Insight/known clients | Yes | **Not verified** |
| Alerts | Yes (envelope only - row shape unverified) | **Not verified** |
| Set AP radio channel (write) | Yes (`PATCH /eaps/{MAC}`) | **Not verified** |
| Device/system logs | **Not found** - every path tried returned `errorCode -1600`; deferred to v0.3 | Not attempted |

The two auth mechanisms (legacy session + CSRF token vs. Open API access
token) are **not interchangeable** - a session from one is rejected (empty
response) by the other's endpoints. See `src/mcp_omada/client.py`'s module
docstring and `docs/api-notes.md` for the full login flows.

## Security model

As of v0.2, this section mirrors mcp-mikrotik's own "Security model"
almost word for word - same three independent controls, centralized in
`src/mcp_omada/guard.py`:

1. **Read-only by default.** `OMADA_ALLOW_WRITE` defaults to `false`. With
   writes disabled, `set_radio_channel` returns a clear `WriteDisabledError`
   and never touches the device - the gate is checked before any read or
   write call is made, regardless of `confirm`.
2. **Central allowlist, no generic command tool.** There is no tool that
   accepts an arbitrary API path or request body. The one write operation
   this package exposes is a dedicated, named function
   (`guard.set_radio_channel`) mapped to exactly one fixed endpoint in
   `guard.ALLOWLIST`. There is no code path by which a caller can reach an
   API path outside that table - `OmadaClient._patch_v2` (the underlying
   write primitive) is never called anywhere except that one function.
3. **Explicit confirm with before/after preview.** `set_radio_channel` takes
   a `confirm: bool` parameter. With `confirm=False` (the default), it reads
   the device's current radio configuration and returns what would change -
   a `before`/`after` structure (channel + freq) - without applying
   anything. Only `confirm=True` applies the change. A 5GHz write also
   returns a `warning` explaining the confirmed channel-persists-as-
   internal-index behavior (see `docs/api-notes.md`), so a caller can't miss
   it by only checking `applied`.

`set_radio_channel` is registered unconditionally (like mcp-mikrotik's
`set_identity`) - the tool is always callable; `OMADA_ALLOW_WRITE=false`
makes every call cleanly refuse rather than making the tool disappear,
which would be harder to diagnose.

On top of the write guard:

- **Structured HTTP, not shell commands.** All controller communication
  goes through [`httpx`](https://www.python-httpx.org/) with structured URL
  path segments, query parameters, and JSON bodies. Nothing in this
  codebase builds a request by concatenating strings from caller-supplied
  input, so injection through a MAC address or site ID is ruled out by
  construction rather than by input filtering.
- **Input validation on top, for its own sake.** `get_device_detail`/
  `get_wifi_summary`/`set_radio_channel`'s `mac` argument is still validated
  and normalized before use (`src/mcp_omada/validation.py`), and
  `set_radio_channel`'s `band`/`channel` are validated against a fixed
  channel/frequency table (`src/mcp_omada/channels.py`) before any device is
  touched - purely to reject garbage input early with a clear error, not as
  an injection defense (see
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

- **v0.2 - delivered.** `set_radio_channel`, the first guarded write,
  following mcp-mikrotik's `guard.py` `ALLOWLIST` pattern exactly (a named,
  reviewable write operation; a read-only gate checked before anything is
  touched; explicit `confirm`/before-after preview) - plus `get_clients`
  (Insight/known clients) and `get_alerts` (envelope verified, row shape
  honestly flagged as unverified). See `docs/api-notes.md`.
- **v0.3 - `get_logs` + more guarded writes.** `get_logs` is NOT in v0.2:
  every device/system log endpoint path tried (`log`, `logs`,
  `logs/queryLog`, `setting/logs/logs`, `insight/logs`) returned
  `errorCode -1600` against real hardware - deferred until a working
  endpoint is found (see `docs/api-notes.md`). Additional guarded writes
  under consideration: AP reboot (needs its own confirmation/cooldown
  policy - no meaningful before/after preview for a reboot, mirroring
  mcp-mikrotik's own reasoning for excluding it from *its* v0 allowlist),
  LED control.
- **v3-controller compatibility.** The pre-v5 controller UI uses a
  different login call and session cookie name entirely - recorded as a
  historical note (not independently verified) in `docs/api-notes.md`, for
  whoever picks this up.

## License

Apache-2.0 - see [LICENSE](LICENSE).

## Related projects

No official TP-Link MCP server exists as of 2026-07; TP-Link's official
offering is the [Omada Open API](https://omada-northbound-docs.tplinkcloud.com/)
(OAuth, `/openapi/v1`, reduced field set). Community MCP servers we know of:

- [MiguelTVMS/tplink-omada-mcp](https://github.com/MiguelTVMS/tplink-omada-mcp) — TypeScript; includes a generic "invoke arbitrary endpoint" tool.
- [realtydev/omada-mcp](https://github.com/realtydev/omada-mcp) — fork with full CRUD (60+ read/write tools).
- [gaspareduard/Omada-mcp](https://github.com/gaspareduard/Omada-mcp) — Open-API-based, capability-gated.

How this project differs: **read-only by default with no generic endpoint
escape hatch** (every write lands behind an explicit, reviewable allowlist -
`OMADA_ALLOW_WRITE` + `guard.py`, mirroring
[mcp-mikrotik](https://github.com/thalisantunes/mcp-mikrotik)), and the
**legacy `/api/v2` path** — which Open-API-only clients cannot reach (the
Open API token is rejected there; verified against real hardware) — for the
rich per-radio/per-client data, with every field-shape gotcha documented in
[docs/api-notes.md](docs/api-notes.md).
