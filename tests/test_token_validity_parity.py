"""The local verdict agrees with Musubi's whole claim path, not one stage of it.

Musubi runs PyJWT's ``_validate_claims`` (``jwt.decode(..., audience="musubi",
issuer=<configured>)``, no leeway) and then its own ``_context_from_payload``.
Checking only the PyJWT stage missed that the second stage refuses an ``aud``
list PyJWT accepts (Tama's review of #18). This runs both real stages: PyJWT
from the environment, ``_context_from_payload`` from a Musubi checkout.

    MUSUBI_SOURCE_DIR=~/Projects/musubi pytest tests/test_token_validity_parity.py

CI clones Musubi and installs PyJWT, so this runs on every PR.
"""

from __future__ import annotations

import itertools
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from musubi_harness.tokens import identity_refusal, validity_refusal

jwt = pytest.importorskip("jwt")
SOURCE = Path(os.environ.get("MUSUBI_SOURCE_DIR", "")).expanduser() / "src" / "musubi" / "auth" / "tokens.py"
pytestmark = pytest.mark.skipif(not SOURCE.is_file(), reason="set MUSUBI_SOURCE_DIR to a Musubi checkout to run claim parity")
ISSUER = "https://oauth.example"  # the deployment's configured issuer; locally only presence is knowable


@dataclass
class _Ok:
    value: Any


@dataclass
class _Err:
    error: Any


class _InvalidTokenError(Exception):
    def __init__(self, detail: str = "") -> None:
        self.detail = detail


def server_context() -> Any:
    text = SOURCE.read_text(encoding="utf-8")
    namespace: dict[str, Any] = {
        "Ok": _Ok,
        "Err": _Err,
        "InvalidTokenError": _InvalidTokenError,
        "AuthContext": lambda **kwargs: kwargs,
        "Any": Any,
        "Result": Any,
        "cast": lambda _t, v: v,
    }
    for name in ("_parse_scopes", "_identity_consistency_error", "_concrete_scope_tenant", "_context_from_payload"):
        match = re.search(rf"^def {name}\(.*?(?=^def |^class |\Z)", text, re.S | re.M)
        assert match, f"{name} not found in {SOURCE}"
        exec(match.group(0), namespace)
    return namespace["_context_from_payload"]


def server_refuses(claims: dict[str, Any], context: Any) -> bool:
    try:
        jwt.api_jwt.PyJWT()._validate_claims(
            dict(claims), jwt.api_jwt.PyJWT()._merge_options(None), audience="musubi", issuer=ISSUER, leeway=0
        )
    except (jwt.PyJWTError, TypeError):
        return True
    return isinstance(context(dict(claims)), _Err)


def local_refuses(claims: dict[str, Any]) -> bool:
    return validity_refusal(claims) is not None or identity_refusal(claims) is not None


SEAT = "aoi/command-chair"
NOW = int(time.time())
VARIANTS: dict[str, list[Any]] = {
    "aud": ["musubi", ["musubi"], ["other", "musubi"], "other", "", [], None, 7],
    "exp": [NOW + 3600, NOW - 3600, str(NOW + 3600), "soon", None, "absent"],
    "iat": [NOW - 3600, NOW + 3600, "then", "absent"],
    "nbf": [NOW - 3600, NOW + 3600, "absent"],
    "jti": ["id-1", 7, "absent"],
    "sub": [SEAT, "aoi/voice", 7, "", "absent"],
    "presence": [SEAT, "aoi/voice", "aoi/*", "absent"],
}
BASE = {"iss": ISSUER, "aud": "musubi", "sub": SEAT, "presence": SEAT, "scope": "aoi/command-chair/*:rw"}


def cases() -> list[dict[str, Any]]:
    out = []
    for key, values in VARIANTS.items():  # every single-claim variation
        for value in values:
            claims = {k: v for k, v in BASE.items() if k != key}
            if value != "absent":
                claims[key] = value
            out.append(claims)
    for aud, exp, sub in itertools.product(VARIANTS["aud"], VARIANTS["exp"], VARIANTS["sub"]):  # and combinations
        claims = {k: v for k, v in BASE.items() if k not in ("aud", "exp", "sub")}
        for key, value in (("aud", aud), ("exp", exp), ("sub", sub)):
            if value != "absent":
                claims[key] = value
        out.append(claims)
    for iss in (ISSUER, "", ["x"], "absent"):
        claims = {k: v for k, v in BASE.items() if k != "iss"}
        if iss != "absent":
            claims["iss"] = iss
        out.append(claims)
    return out


def test_local_refusal_matches_the_servers_whole_claim_path() -> None:
    context = server_context()
    mismatches = [
        (claims, server_refuses(claims, context), local_refuses(claims))
        for claims in cases()
        if server_refuses(claims, context) != local_refuses(claims)
    ]
    assert mismatches == []


def test_tamas_list_audience_is_refused_by_both() -> None:
    claims = {**BASE, "aud": ["musubi"]}
    assert server_refuses(claims, server_context()) and local_refuses(claims)
