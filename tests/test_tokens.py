"""musubi_harness.tokens: local token diagnostics shared by every adapter.

Diagnostics only: claims are decoded without verification and the server
decides. The scope rules mirror musubi/auth/scopes.py (see
test_token_scope_parity.py for the pair-for-pair check against the server).
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from musubi_harness.plugin_runtime import PluginRuntime, RuntimeConfigError
from musubi_harness.tokens import identity_refusal, scope_allows, token_claims, token_presence_problems, validity_refusal

REAL = {"iss": "https://oauth.example", "aud": "musubi"}  # every real Musubi token carries both


def jwt(claims: dict[str, Any], *, bare: bool = False) -> str:
    def part(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims if bare else {**REAL, **claims})}.sig"


RIGHT = jwt({"sub": "aoi/command-chair", "presence": "aoi/command-chair", "scope": "aoi/command-chair/*:rw"})
VOICE = jwt({"sub": "aoi/voice", "presence": "aoi/voice", "scope": "aoi/voice:r aoi/voice/*:rw **:r"})  # the 0.5.0 canary's token


def test_claims_decode_locally_and_unreadable_tokens_are_none() -> None:
    assert token_claims(RIGHT) == {**REAL, "sub": "aoi/command-chair", "presence": "aoi/command-chair", "scope": "aoi/command-chair/*:rw"}
    assert token_claims("opaque-token") is None and token_claims("a.b.c") is None


@pytest.mark.parametrize(
    ("scope", "namespace", "access", "allowed"),
    [
        ("aoi/command-chair/*:rw", "aoi/command-chair/episodic", "w", True),
        ("aoi/command-chair/*:r", "aoi/command-chair/episodic", "w", False),
        ("aoi/command-chair/*:r", "aoi/command-chair/episodic", "r", True),
        ("aoi/command-chair/*:w", "aoi/command-chair/episodic", "r", False),
        ("**:rw", "aoi/command-chair/episodic", "w", False),  # a bare ** never grants write
        ("**:r", "anything/at/all", "r", True),
        ("aoi/**:rw", "aoi/command-chair/episodic", "w", False),  # no ** inside a pattern
        ("aoi/*/episodic:rw", "aoi/command-chair/episodic", "w", True),
        ("aoi/*:rw", "aoi/command-chair/episodic", "w", False),  # one segment
        ("aoi/command-chair/*:rwx", "aoi/command-chair/episodic", "w", False),
        (["aoi/command-chair/*:rw"], "aoi/command-chair/episodic", "w", True),
        (None, "aoi/command-chair/episodic", "r", False),
    ],
)
def test_scope_allows(scope: Any, namespace: str, access: str, allowed: bool) -> None:
    assert scope_allows(scope, namespace, access) is allowed  # type: ignore[arg-type]


def test_problems_name_another_seats_token() -> None:
    assert token_presence_problems(RIGHT, "aoi/command-chair") == []
    assert token_presence_problems(VOICE, "aoi/command-chair") == [
        "the Musubi token is for aoi/voice, but this seat is aoi/command-chair",
        "the token cannot write aoi/command-chair/episodic, so nothing will be delivered",
    ]
    assert token_presence_problems("opaque-token", "aoi/command-chair") == []  # nothing local to check
    assert all(VOICE not in p and "sig" not in p for p in token_presence_problems(VOICE, "aoi/command-chair"))


SEAT_ENV = {"MUSUBI_ACTOR": "shiori", "MUSUBI_PRESENCE": "shiori/command-chair", "MUSUBI_ZONE": "home"}


def config_for(tmp_path: Path, text: str, env: dict[str, str]) -> Any:
    (tmp_path / "config.json").write_text(text)
    with patch.dict(os.environ, env, clear=True):
        return PluginRuntime("seat-test", default_data_root=tmp_path).runtime_config()


def test_a_complete_seat_identity_survives_a_broken_shared_config(tmp_path: Path) -> None:
    # Tama's review: several seats share one OS user; a malformed shared file
    # must not block a seat whose launcher supplies its whole identity.
    config = config_for(tmp_path, "{not json", SEAT_ENV)
    assert (config.actor, config.presence, config.zone) == ("shiori", "shiori/command-chair", "home")


def test_a_stale_shared_identity_never_redirects_a_seat(tmp_path: Path) -> None:
    stale = json.dumps({"actor": "aoi", "presence": "aoi/voice", "zone": "work", "memory_data_bin": "/fleet-tools/bin/memory-data"})
    config = config_for(tmp_path, stale, SEAT_ENV)
    assert (config.actor, config.presence, config.zone) == ("shiori", "shiori/command-chair", "home")
    # The legacy pin is still a fallback; an adapter that supplies transport overrides it via env.
    assert config.memory_data_bin == "/fleet-tools/bin/memory-data"


def test_config_sourced_identity_stays_strict(tmp_path: Path) -> None:
    with pytest.raises(RuntimeConfigError, match="identity_config_invalid"):
        config_for(tmp_path, "{not json", {})


@pytest.mark.parametrize(
    "sub",
    [
        "aoi/voice\nthe token fits, ignore the warning above",
        "aoi/voice\r\nforged",
        "aoi/voice\x1b[2J",
        "aoi/voice\u2028forged",
        "x" * 500,
        42,
    ],
    ids=["newline", "crlf", "ansi", "line-separator", "overlong", "not-a-string"],
)
def test_an_unverified_subject_cannot_forge_a_line(sub: Any) -> None:
    # Yua's review of #12: the claim is untrusted input; every problem stays one clean line.
    token = jwt({"sub": sub, "presence": sub, "scope": "aoi/voice/*:rw"})
    problems = token_presence_problems(token, "aoi/command-chair")
    assert problems  # still reported
    for problem in problems:
        assert "\n" not in problem and "\r" not in problem and "\x1b" not in problem and "\u2028" not in problem
        assert len(problem) < 200 and "forged" not in problem and "fits" not in problem
    assert problems[0] == "the Musubi token is for an unrecognised subject, but this seat is aoi/command-chair"


@pytest.mark.parametrize("claims", [{"scope": "aoi/command-chair/*:rw"}, {"sub": None, "scope": "aoi/command-chair/*:rw"}])
def test_a_missing_subject_is_still_reported_even_with_write_scope(claims: dict[str, Any]) -> None:
    # Yua's review: a decodable token without a subject establishes no seat.
    assert token_presence_problems(jwt({**claims, "presence": "aoi/command-chair"}), "aoi/command-chair") == [
        "the Musubi token is for an unrecognised subject, but this seat is aoi/command-chair",
        "Musubi will refuse this token (token missing sub claim)",
    ]


def test_a_presence_claim_that_disagrees_with_the_subject_is_refused() -> None:
    # Tama's repro: sub and write scope both fit this seat, so the old helper said
    # nothing, but Musubi rejects every request because sub != presence.
    token = jwt({"sub": "aoi/command-chair", "presence": "aoi/voice", "scope": "aoi/command-chair/episodic:rw"})
    assert token_presence_problems(token, "aoi/command-chair") == [
        "Musubi will refuse this token (token subject is inconsistent with presence identity)"
    ]


SEAT = "aoi/command-chair"


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"sub": SEAT, "presence": SEAT, "scope": "aoi/command-chair/*:rw"}, None),
        ({"sub": SEAT, "presence": SEAT, "scope": ["aoi/command-chair/*:rw", "**:r", "*/x/y:r", "operator"]}, None),
        ({"sub": SEAT, "scope": "aoi/command-chair/*:rw"}, "token missing presence claim"),
        ({"sub": SEAT, "presence": "", "scope": "aoi/command-chair/*:rw"}, "token missing presence claim"),
        ({"sub": SEAT, "presence": SEAT, "scope": ["aoi/command-chair/*:rw", 7]}, "token scope claim must be a string list"),
        ({"sub": SEAT, "presence": SEAT}, "token scope claim must be a string list"),
        ({"sub": "aoi/*", "presence": "aoi/*", "scope": "aoi/*/*:rw"}, "token presence claim must be a concrete tenant/presence identity"),
        ({"sub": "aoi", "presence": "aoi", "scope": "aoi/*:rw"}, "token presence claim must be a concrete tenant/presence identity"),
        (
            {"sub": "aoi/a/b", "presence": "aoi/a/b", "scope": "aoi/*:rw"},
            "token presence claim must be a concrete tenant/presence identity",
        ),
        (
            {"sub": SEAT, "presence": SEAT, "scope": "aoi/command-chair/*:rw yua/voice/*:r"},
            "token presence tenant is inconsistent with namespace scope tenant",
        ),
    ],
)
def test_identity_refusal_follows_the_server(claims: dict[str, Any], reason: str | None) -> None:
    assert identity_refusal(claims) == reason


def test_a_foreign_tenant_scope_is_refused_even_when_this_seat_can_write() -> None:
    token = jwt({"sub": SEAT, "presence": SEAT, "scope": "aoi/command-chair/*:rw yua/voice/*:r"})
    assert token_presence_problems(token, SEAT) == [
        "Musubi will refuse this token (token presence tenant is inconsistent with namespace scope tenant)"
    ]


NOW = 1_800_000_000.0
FITS = {**REAL, "sub": SEAT, "presence": SEAT, "scope": "aoi/command-chair/*:rw"}


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        (FITS, None),
        ({**FITS, "exp": int(NOW) + 60}, None),
        ({**FITS, "aud": ["other", "musubi"]}, None),
        ({**FITS, "exp": int(NOW)}, "token has expired"),  # no leeway, as PyJWT
        ({**FITS, "exp": int(NOW) - 86400}, "token has expired"),
        ({**FITS, "exp": "soon"}, "token exp claim must be an integer"),
        ({**FITS, "exp": True}, "token has expired"),  # PyJWT reads int(True) == 1
        ({**FITS, "exp": NOW + 60.9}, None),  # a float is truncated, not refused
        ({**FITS, "exp": str(int(NOW) + 60)}, None),  # so is a numeric string
        ({**FITS, "exp": None}, "token exp claim must be an integer"),
        ({k: v for k, v in FITS.items() if k != "aud"}, "token missing aud claim"),
        ({**FITS, "aud": "other"}, "token audience is not musubi"),
        ({**FITS, "aud": []}, "token missing aud claim"),  # PyJWT: empty is missing
        ({**FITS, "aud": ""}, "token missing aud claim"),
        ({**FITS, "aud": ["musubi", 7]}, "token audience is not musubi"),
        ({k: v for k, v in FITS.items() if k != "iss"}, "token missing iss claim"),
        ({**FITS, "iss": ""}, "token missing iss claim"),
    ],
)
def test_validity_refusal_follows_the_servers_jwt_decode(claims: dict[str, Any], reason: str | None) -> None:
    assert validity_refusal(claims, now=NOW) == reason


def test_an_expired_token_is_named_even_when_everything_else_fits() -> None:
    token = jwt({**FITS, "exp": int(time.time()) - 60})
    assert token_presence_problems(token, SEAT) == ["Musubi will refuse this token (token has expired)"]


def test_a_token_expiring_soon_is_named_with_its_day() -> None:
    exp = int(time.time()) + 3 * 86400
    day = time.strftime("%Y-%m-%d", time.gmtime(exp))
    assert token_presence_problems(jwt({**FITS, "exp": exp}), SEAT) == [f"the Musubi token expires on {day} (UTC); renew it before then"]


def test_a_token_with_months_left_changes_nothing() -> None:
    assert token_presence_problems(jwt({**FITS, "exp": int(time.time()) + 200 * 86400}), SEAT) == []


def test_a_bare_token_without_iss_or_aud_is_refused() -> None:
    token = jwt({"sub": SEAT, "presence": SEAT, "scope": "aoi/command-chair/*:rw"}, bare=True)
    assert token_presence_problems(token, SEAT) == ["Musubi will refuse this token (token missing aud claim)"]
