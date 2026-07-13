"""Structured audit journal for every guarded write (guard.py) call.

Mirrors mcp-mikrotik's `audit.py` closely - same shape, same
defense-in-depth secret redaction, same best-effort-never-raises contract -
adapted to mcp-omada's specifics:

- There is no RouterOS "action" verb here; `action` in a journal entry is
  the write operation's HTTP method (e.g. `"PATCH"` - see
  `guard.WriteOperation.method`).
- v0.2 has no fleet device registry (unlike mcp-mikrotik's `devices.yaml`):
  `device` is the AP's MAC address, the identifier a caller already used to
  address the write.
- A FOURTH outcome, `"rejected"`, exists alongside mcp-mikrotik's three
  (`"preview"`/`"applied"`/`"error"`) - mcp-mikrotik has no equivalent
  because RouterOS's own API doesn't answer "success" for a write it didn't
  actually apply. Omada's controller can (see `docs/api-notes.md`'s
  confirmed silent-discard gotcha), so `guard.set_radio_channel` re-reads
  the device after every confirmed write and only reports `"applied"` when
  that re-read actually confirms the change; `"rejected"` records a
  confirmed write attempt (`errorCode 0`) whose effect could NOT be
  verified - a real, audit-worthy event, distinct from both a never-applied
  `"preview"` and a genuine `"error"`.

Every `ALLOWLIST`'d write operation in guard.py is wrapped (see guard.py's
`_audited` decorator - the only thing in this package that calls `record()`)
so it emits exactly one JSON-lines audit event per call, regardless of
outcome - even a `WriteDisabledError` raised before the device is ever
touched.

Destination: `OMADA_AUDIT_LOG` (a file path), appended to as one JSON line
per event, if set; otherwise a plain INFO-level line via the standard
`logging` module (stderr, like every other log line this package emits).
The env var is read fresh on every call rather than cached, mirroring
mcp-mikrotik's own `MIKROTIK_AUDIT_LOG` handling - mainly so tests can point
it at a temp file per test without needing to rebuild any settings object.

NEVER writes a controller password, client secret, CSRF token, session
cookie, or Open API access token - see `_sanitize()` below. Writing the
journal is always best-effort: any failure (a bad `OMADA_AUDIT_LOG` path, a
permissions error, an unserializable value) is caught, logged as a warning,
and never propagates - an audit-logging problem must not be able to block
or fail the write operation it is describing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger("mcp_omada.audit")

# Any dict key matching this (case-insensitive) is dropped from a journal
# entry's summary, however it got there - defense in depth on top of the
# fact that no current write tool's before/after legitimately contains one
# of these (an AP's radioSetting carries no credential - see
# docs/api-notes.md's confirmed shape). Kept broad (mirrors mcp-mikrotik's
# own list, plus "cookie" - a concept mcp-mikrotik's RouterOS API doesn't
# have but mcp-omada's legacy session does) so the journal stays safe even
# if a future ALLOWLIST entry ever touches an endpoint whose response does
# carry a sensitive field.
_SENSITIVE_KEY = re.compile(
    r"password|secret|token|credential|passphrase|psk|pre.?shared|private|cookie",
    re.IGNORECASE,
)

_VALID_OUTCOMES = {"preview", "applied", "rejected", "error"}


def _sanitize(value: Any) -> Any:
    """Recursively drop any dict key that looks sensitive (see
    `_SENSITIVE_KEY`). Lists/tuples are sanitized element-wise; anything
    else is returned unchanged."""
    if isinstance(value, dict):
        return {key: _sanitize(val) for key, val in value.items() if not _SENSITIVE_KEY.search(str(key))}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return value


def record(
    *,
    correlation_id: str,
    device: str,
    tool: str,
    operation: str,
    action: str,
    confirm: bool,
    outcome: str,
    summary: dict[str, Any],
) -> None:
    """Emit one audit journal entry. Best-effort: never raises.

    `summary` is typically `{"before": ..., "after": ..., "warning": ...,
    "message": ...}` (preview/applied/rejected) or `{"error": ...}` (error) -
    always passed through `_sanitize()` before being written anywhere, and
    never includes a controller credential.
    """
    if outcome not in _VALID_OUTCOMES:
        outcome = "error"  # defensive; guard.py should never pass anything else

    event = {
        "timestamp": time.time(),
        "correlation_id": correlation_id,
        "device": device,
        "tool": tool,
        "operation": operation,
        "action": action,
        "confirm": confirm,
        "outcome": outcome,
        "summary": _sanitize(summary),
    }

    try:
        line = json.dumps(event, default=str, sort_keys=True)
    except (TypeError, ValueError):
        logger.warning("Audit journal: failed to serialize event for tool=%s operation=%s", tool, operation)
        return

    path = os.environ.get("OMADA_AUDIT_LOG")
    try:
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        else:
            logger.info(line)
    except OSError as exc:
        logger.warning("Audit journal: failed to write event to %r: %s", path, exc)
