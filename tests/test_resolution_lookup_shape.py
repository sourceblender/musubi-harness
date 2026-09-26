"""resolve accepts receipt-lookup's own observation, and stores the v1 shape.

Both Claude seats resolved a stuck row on 2026-09-26 by renaming
subject->principal and effective_scopes->token_scopes by hand, because the
evidence schema did not accept what the bundled receipt-lookup prints (Aoi).
The observation here is built by the lookup's own producer, so if that shape
drifts again this fails.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from musubi_harness.cli.memory_data import self_attested_token_claims, utc_timestamp
from musubi_harness.core import ContractError
from musubi_harness.resolution import ReceiptObservation, parse_resolution_evidence

DIGEST = "8eeed14cf056c3b1ca41158c8e3fe946b0e46c90d03b025d07b524d97d5e4369"
NS = "shiori/command-chair/episodic"
OP = "capture_episodic.bucket=capture"


def jwt(claims: dict[str, Any]) -> str:
    def part(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


def lookup_observation() -> dict[str, Any]:
    """Exactly what `musubi-memory-data musubi receipt-lookup` puts in receipt_observation."""
    token = jwt(
        {
            "iss": "https://oauth.example",
            "sub": "shiori/command-chair",
            "presence": "shiori/command-chair",
            "scope": "shiori/command-chair:r shiori/command-chair/*:rw **:r",
        }
    )
    return {
        "status": "absent",
        **self_attested_token_claims(token),
        "observed_at": utc_timestamp(),
        "namespace": NS,
        "operation_id": OP,
        "request_digest": DIGEST,
    }


def abandon(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "resolution_kind": "operator_abandon",
        "delivery_state": "unknown",
        "event_id": "claude-code:s:e",
        "namespace": NS,
        "operation_id": OP,
        "expected_request_digest": DIGEST,
        "reason": "stuck after a POST with no receipt; content re-queued and verified",
        "receipt_observation": observation,
    }


def test_the_lookup_observation_is_accepted_and_stored_as_v1() -> None:
    observation = lookup_observation()
    evidence = parse_resolution_evidence(abandon(observation))
    stored = evidence.as_mapping()["receipt_observation"]
    assert stored == {
        "status": "absent",
        "principal": "shiori/command-chair",
        "token_scopes": ["shiori/command-chair:r", "shiori/command-chair/*:rw", "**:r"],
        "observed_at": observation["observed_at"],
        "namespace": NS,
        "operation_id": OP,
        "request_digest": DIGEST,
    }


def test_the_stored_v1_shape_still_round_trips() -> None:
    v1 = parse_resolution_evidence(abandon(lookup_observation())).as_mapping()
    assert parse_resolution_evidence(v1).as_mapping() == v1


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"attestation": "server_attested"}, "attestation must be self_attested"),
        ({"presence": "aoi/command-chair"}, "presence must equal"),
        ({"status": "present"}, "status must be absent"),
        ({"issuer": ""}, "issuer is invalid"),
    ],
)
def test_a_lookup_observation_is_still_checked(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ContractError, match=message):
        ReceiptObservation.from_mapping({**lookup_observation(), **change})


def test_a_mix_of_the_two_shapes_is_refused() -> None:
    mixed = {**lookup_observation(), "principal": "shiori/command-chair"}
    with pytest.raises(ContractError, match=r"fields are invalid: missing=\['token_scopes'\]"):
        ReceiptObservation.from_mapping(mixed)
