"""Local diagnostics for a Musubi bearer token: whose it is and what it can write.

Several seats can share one OS user, so a plugin can end up holding another
seat's token. That shows up late and silently: reads work, and every delivery
403s at the drain's receipt lookup, which needs write access. Decoding the
token's claims locally names the mismatch on the first turn instead.

These are **diagnostics, not authorization**: the claims are read without
verifying the signature, and the server decides. Nothing here prints or logs
the token.

The scope rules mirror Musubi's own matcher (``musubi/auth/scopes.py``:
``_parse_namespace_scope``, ``_namespace_matches``, ``_namespace_scope_allows``)
exactly:

- an entry is ``pattern:access``, split on the last colon, with access ``r``,
  ``w`` or ``rw``;
- a bare ``**`` matches every namespace but never grants write;
- any other pattern needs the same number of ``/`` segments, each ``*`` (one
  segment) or literal. There is no ``**`` inside a pattern.

``tests/test_token_scope_parity.py`` checks this against the server's own
functions whenever ``MUSUBI_SOURCE_DIR`` points at a Musubi checkout.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Literal

Access = Literal["r", "w"]


def token_claims(token: str) -> dict[str, Any] | None:
    """The token's JWT claims, decoded locally and unverified, or None."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, UnicodeDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def _entries(scope: Any) -> list[str]:
    if isinstance(scope, str):
        return scope.split()
    if isinstance(scope, list):
        return [entry for entry in scope if isinstance(entry, str)]
    return []


def scope_allows(scope: Any, namespace: str, access: Access) -> bool:
    """True when any scope entry grants ``access`` on ``namespace``, as Musubi decides."""
    wanted = namespace.split("/")
    for entry in _entries(scope):
        if ":" not in entry:
            continue
        pattern, granted = entry.rsplit(":", 1)
        if granted not in ("r", "w", "rw") or (granted != "rw" and granted != access):
            continue
        if pattern == "**":
            if access == "w":
                continue  # a bare ** never grants write
            return True
        parts = pattern.split("/")
        if len(parts) == len(wanted) and all(p == "*" or p == n for p, n in zip(parts, wanted, strict=True)):
            return True
    return False


_PRESENCE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}/[a-z0-9][a-z0-9._-]{0,63}")


def _shown(value: Any) -> str:
    """A claim as it may appear in a message: a presence-shaped value, else a label.

    Claims are unverified input; a subject with a newline or control characters
    must not be able to forge a second line in a warning (Yua's review).
    """
    return value if isinstance(value, str) and _PRESENCE.fullmatch(value) else "an unrecognised subject"


def token_presence_problems(token: str, presence: str) -> list[str]:
    """Plain-language problems with using ``token`` as ``presence``; empty when it fits.

    Checks the subject and write access to ``<presence>/episodic``, where
    capture and remember write. An unreadable (opaque) token yields no problems:
    there is nothing local to check, and the server still decides.
    """
    claims = token_claims(token)
    if claims is None:
        return []
    problems = []
    subject = claims.get("sub")
    seat = _shown(presence)
    # A missing or non-string sub establishes no seat either (Yua's review).
    if subject != presence:
        problems.append(f"the Musubi token is for {_shown(subject)}, but this seat is {seat}")
    if not scope_allows(claims.get("scope"), f"{presence}/episodic", "w"):
        problems.append(f"the token cannot write {seat}/episodic, so nothing will be delivered")
    return problems
