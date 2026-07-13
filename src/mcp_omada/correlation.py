"""Short, unique per-call correlation IDs, threaded through logs and the
audit journal.

Mirrors mcp-mikrotik's `correlation.py` exactly (see that project's module
docstring for the full rationale - a `ContextVar` rather than a plain
module-level variable specifically because it stays safe under concurrent/
async tool calls, and the id itself is derived from nothing but random
bytes, never from device state or credentials, so it can never leak
anything sensitive by construction).

Generated once per MCP tool call (see server.py's `_safe` wrapper) and
bound so any code running within that call - a guard.py write function, an
exception handler - can retrieve the same id with `current()`, without
threading it through every function signature by hand.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from contextvars import ContextVar

_current: ContextVar[str | None] = ContextVar("mcp_omada_correlation_id", default=None)


def new_id() -> str:
    """A short, unique-enough id for one tool call (12 hex chars)."""
    return uuid.uuid4().hex[:12]


def current() -> str:
    """The correlation id bound for the currently running tool call.

    Falls back to minting a fresh one if nothing is bound (e.g. a guard.py
    write function called directly, outside of server.py's `_safe` wrapper -
    as every guard.py test does) so callers never need to special-case "no
    id bound yet". Only call this once per logical operation and reuse the
    result - calling it twice with nothing bound returns two different ids.
    """
    return _current.get() or new_id()


@contextlib.contextmanager
def bind(correlation_id: str | None = None) -> Iterator[str]:
    """Bind a correlation id for the duration of a `with` block.

    Used once per MCP tool call, in server.py's `_safe` wrapper. Generates a
    fresh id when `correlation_id` is not given.
    """
    value = correlation_id or new_id()
    token = _current.set(value)
    try:
        yield value
    finally:
        _current.reset(token)
