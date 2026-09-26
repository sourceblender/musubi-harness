"""Typed, versioned evidence for fail-closed legacy delivery resolution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .core import ContractError

RESOLUTION_SCHEMA_VERSION = 1
LIVE_REJECTION_SCHEMA_VERSION = 2
EPISODIC_CREATE_CONTENT_LIMIT_BYTES = 32_768
RESOLUTION_KINDS = frozenset({"proven_non_mutating_rejection", "operator_abandon"})
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_CONTENT_TOO_LARGE_DETAIL_RE = re.compile(r"episodic content is ([1-9][0-9]*) UTF-8 bytes; the limit is ([1-9][0-9]*)")


def _as_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be an object")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ContractError(f"{name} fields are invalid: missing={missing}, extra={extra}")


def _text(value: object, name: str, *, maximum: int = 2048) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise ContractError(f"{name} is invalid")
    return value.strip()


def _digest(value: object, name: str) -> str:
    digest = _text(value, name, maximum=64)
    if _DIGEST_RE.fullmatch(digest) is None:
        raise ContractError(f"{name} must be 64 lowercase hexadecimal characters")
    return digest


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError(f"{name} must be a positive integer")
    return value


def _timestamp(value: object, name: str) -> str:
    raw = _text(value, name, maximum=64)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name} must include a timezone")
    return raw


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ContractError(f"{name} must be a non-empty list")
    normalized = tuple(_text(item, name, maximum=256) for item in value)
    if len(set(normalized)) != len(normalized):
        raise ContractError(f"{name} must not contain duplicates")
    return normalized


@dataclass(frozen=True)
class ReceiptObservation:
    status: str
    principal: str
    token_scopes: tuple[str, ...]
    observed_at: str
    namespace: str
    operation_id: str
    request_digest: str

    @classmethod
    def from_mapping(cls, raw: object) -> ReceiptObservation:
        value = _as_mapping(raw, "receipt_observation")
        _exact_keys(
            value,
            {
                "status",
                "principal",
                "token_scopes",
                "observed_at",
                "namespace",
                "operation_id",
                "request_digest",
            },
            "receipt_observation",
        )
        if value["status"] != "absent":
            raise ContractError("receipt_observation.status must be absent")
        return cls(
            status="absent",
            principal=_text(value["principal"], "receipt_observation.principal", maximum=256),
            token_scopes=_string_list(value["token_scopes"], "receipt_observation.token_scopes"),
            observed_at=_timestamp(value["observed_at"], "receipt_observation.observed_at"),
            namespace=_text(value["namespace"], "receipt_observation.namespace", maximum=512),
            operation_id=_text(value["operation_id"], "receipt_observation.operation_id", maximum=256),
            request_digest=_digest(value["request_digest"], "receipt_observation.request_digest"),
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "status": self.status,
            "principal": self.principal,
            "token_scopes": list(self.token_scopes),
            "observed_at": self.observed_at,
            "namespace": self.namespace,
            "operation_id": self.operation_id,
            "request_digest": self.request_digest,
        }


@dataclass(frozen=True)
class BoundaryEvidence:
    rejected_content_bytes: int
    limit_content_bytes: int
    exact_limit_tested: bool

    @classmethod
    def from_mapping(cls, raw: object) -> BoundaryEvidence:
        value = _as_mapping(raw, "boundary_evidence")
        _exact_keys(
            value,
            {"rejected_content_bytes", "limit_content_bytes", "exact_limit_tested"},
            "boundary_evidence",
        )
        rejected = _positive_integer(value["rejected_content_bytes"], "boundary_evidence.rejected_content_bytes")
        limit = _positive_integer(value["limit_content_bytes"], "boundary_evidence.limit_content_bytes")
        tested = value["exact_limit_tested"]
        if not isinstance(tested, bool):
            raise ContractError("boundary_evidence.exact_limit_tested must be boolean")
        if rejected <= limit:
            raise ContractError("boundary_evidence must describe a rejection above the limit")
        return cls(rejected, limit, tested)

    def as_mapping(self) -> dict[str, object]:
        return {
            "rejected_content_bytes": self.rejected_content_bytes,
            "limit_content_bytes": self.limit_content_bytes,
            "exact_limit_tested": self.exact_limit_tested,
        }


@dataclass(frozen=True)
class ProvenNonMutatingRejection:
    schema_version: int
    resolution_kind: str
    event_id: str
    namespace: str
    operation_id: str
    expected_request_digest: str
    content_bytes: int
    limit_bytes: int
    source_ordering_evidence: str
    version_coverage: tuple[str, ...]
    boundary_evidence: BoundaryEvidence
    receipt_observation: ReceiptObservation

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ProvenNonMutatingRejection:
        _exact_keys(
            value,
            {
                "schema_version",
                "resolution_kind",
                "event_id",
                "namespace",
                "operation_id",
                "expected_request_digest",
                "content_bytes",
                "limit_bytes",
                "source_ordering_evidence",
                "version_coverage",
                "boundary_evidence",
                "receipt_observation",
            },
            "proven_non_mutating_rejection",
        )
        if isinstance(value["schema_version"], bool) or value["schema_version"] != RESOLUTION_SCHEMA_VERSION:
            raise ContractError("resolution schema_version is unsupported")
        if value["resolution_kind"] != "proven_non_mutating_rejection":
            raise ContractError("resolution_kind is invalid")
        content_bytes = _positive_integer(value["content_bytes"], "content_bytes")
        limit_bytes = _positive_integer(value["limit_bytes"], "limit_bytes")
        boundary = BoundaryEvidence.from_mapping(value["boundary_evidence"])
        if boundary.rejected_content_bytes != content_bytes or boundary.limit_content_bytes != limit_bytes:
            raise ContractError("boundary evidence does not match content and limit bytes")
        if content_bytes <= limit_bytes:
            raise ContractError("proven rejection content must exceed its limit")
        return cls(
            schema_version=RESOLUTION_SCHEMA_VERSION,
            resolution_kind="proven_non_mutating_rejection",
            event_id=_text(value["event_id"], "event_id", maximum=512),
            namespace=_text(value["namespace"], "namespace", maximum=512),
            operation_id=_text(value["operation_id"], "operation_id", maximum=256),
            expected_request_digest=_digest(value["expected_request_digest"], "expected_request_digest"),
            content_bytes=content_bytes,
            limit_bytes=limit_bytes,
            source_ordering_evidence=_text(value["source_ordering_evidence"], "source_ordering_evidence", maximum=4096),
            version_coverage=_string_list(value["version_coverage"], "version_coverage"),
            boundary_evidence=boundary,
            receipt_observation=ReceiptObservation.from_mapping(value["receipt_observation"]),
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "resolution_kind": self.resolution_kind,
            "event_id": self.event_id,
            "namespace": self.namespace,
            "operation_id": self.operation_id,
            "expected_request_digest": self.expected_request_digest,
            "content_bytes": self.content_bytes,
            "limit_bytes": self.limit_bytes,
            "source_ordering_evidence": self.source_ordering_evidence,
            "version_coverage": list(self.version_coverage),
            "boundary_evidence": self.boundary_evidence.as_mapping(),
            "receipt_observation": self.receipt_observation.as_mapping(),
        }


@dataclass(frozen=True)
class LiveReceiptObservation:
    status: str
    attestation: str
    issuer: str
    subject: str
    presence: str
    effective_scopes: tuple[str, ...]
    observed_at: str
    namespace: str
    operation_id: str
    request_digest: str

    @classmethod
    def from_mapping(cls, raw: object) -> LiveReceiptObservation:
        value = _as_mapping(raw, "receipt_observation")
        _exact_keys(
            value,
            {
                "status",
                "attestation",
                "issuer",
                "subject",
                "presence",
                "effective_scopes",
                "observed_at",
                "namespace",
                "operation_id",
                "request_digest",
            },
            "receipt_observation",
        )
        if value["status"] != "absent":
            raise ContractError("receipt_observation.status must be absent")
        if value["attestation"] not in {"self_attested", "server_attested"}:
            raise ContractError("receipt_observation.attestation is invalid")
        return cls(
            status="absent",
            attestation=str(value["attestation"]),
            issuer=_text(value["issuer"], "receipt_observation.issuer", maximum=512),
            subject=_text(value["subject"], "receipt_observation.subject", maximum=256),
            presence=_text(value["presence"], "receipt_observation.presence", maximum=256),
            effective_scopes=_string_list(value["effective_scopes"], "receipt_observation.effective_scopes"),
            observed_at=_timestamp(value["observed_at"], "receipt_observation.observed_at"),
            namespace=_text(value["namespace"], "receipt_observation.namespace", maximum=512),
            operation_id=_text(value["operation_id"], "receipt_observation.operation_id", maximum=256),
            request_digest=_digest(value["request_digest"], "receipt_observation.request_digest"),
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "status": self.status,
            "attestation": self.attestation,
            "issuer": self.issuer,
            "subject": self.subject,
            "presence": self.presence,
            "effective_scopes": list(self.effective_scopes),
            "observed_at": self.observed_at,
            "namespace": self.namespace,
            "operation_id": self.operation_id,
            "request_digest": self.request_digest,
        }


@dataclass(frozen=True)
class LiveTypedNonMutatingRejection:
    schema_version: int
    resolution_kind: str
    event_id: str
    namespace: str
    operation_id: str
    expected_request_digest: str
    content_bytes_client: int
    content_bytes_server: int
    limit_bytes: int
    response_status: int
    error_code: str
    response_detail: str
    observed_at: str
    receipt_observation: LiveReceiptObservation

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> LiveTypedNonMutatingRejection:
        _exact_keys(
            value,
            {
                "schema_version",
                "resolution_kind",
                "event_id",
                "namespace",
                "operation_id",
                "expected_request_digest",
                "content_bytes_client",
                "content_bytes_server",
                "limit_bytes",
                "response_status",
                "error_code",
                "response_detail",
                "observed_at",
                "receipt_observation",
            },
            "live_typed_non_mutating_rejection",
        )
        if isinstance(value["schema_version"], bool) or value["schema_version"] != LIVE_REJECTION_SCHEMA_VERSION:
            raise ContractError("resolution schema_version is unsupported")
        if value["resolution_kind"] != "proven_non_mutating_rejection":
            raise ContractError("resolution_kind is invalid")
        if value["response_status"] != 422:
            raise ContractError("live rejection response_status must be 422")
        if value["error_code"] != "CONTENT_TOO_LARGE":
            raise ContractError("live rejection error_code is invalid")
        client_bytes = _positive_integer(value["content_bytes_client"], "content_bytes_client")
        server_bytes = _positive_integer(value["content_bytes_server"], "content_bytes_server")
        limit_bytes = _positive_integer(value["limit_bytes"], "limit_bytes")
        detail = _text(value["response_detail"], "response_detail", maximum=1024)
        match = _CONTENT_TOO_LARGE_DETAIL_RE.fullmatch(detail)
        if match is None:
            raise ContractError("live rejection response_detail is invalid")
        if int(match.group(1)) != server_bytes or int(match.group(2)) != limit_bytes:
            raise ContractError("live rejection detail does not match server byte fields")
        if client_bytes != server_bytes:
            raise ContractError("client and server content byte counts differ")
        if limit_bytes != EPISODIC_CREATE_CONTENT_LIMIT_BYTES:
            raise ContractError("live rejection limit does not match the create contract")
        if client_bytes <= limit_bytes:
            raise ContractError("live rejection content must exceed its limit")
        return cls(
            schema_version=LIVE_REJECTION_SCHEMA_VERSION,
            resolution_kind="proven_non_mutating_rejection",
            event_id=_text(value["event_id"], "event_id", maximum=512),
            namespace=_text(value["namespace"], "namespace", maximum=512),
            operation_id=_text(value["operation_id"], "operation_id", maximum=256),
            expected_request_digest=_digest(value["expected_request_digest"], "expected_request_digest"),
            content_bytes_client=client_bytes,
            content_bytes_server=server_bytes,
            limit_bytes=limit_bytes,
            response_status=422,
            error_code="CONTENT_TOO_LARGE",
            response_detail=detail,
            observed_at=_timestamp(value["observed_at"], "observed_at"),
            receipt_observation=LiveReceiptObservation.from_mapping(value["receipt_observation"]),
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "resolution_kind": self.resolution_kind,
            "event_id": self.event_id,
            "namespace": self.namespace,
            "operation_id": self.operation_id,
            "expected_request_digest": self.expected_request_digest,
            "content_bytes_client": self.content_bytes_client,
            "content_bytes_server": self.content_bytes_server,
            "limit_bytes": self.limit_bytes,
            "response_status": self.response_status,
            "error_code": self.error_code,
            "response_detail": self.response_detail,
            "observed_at": self.observed_at,
            "receipt_observation": self.receipt_observation.as_mapping(),
        }


@dataclass(frozen=True)
class OperatorAbandon:
    schema_version: int
    resolution_kind: str
    delivery_state: str
    event_id: str
    namespace: str
    operation_id: str
    expected_request_digest: str
    reason: str
    receipt_observation: ReceiptObservation

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OperatorAbandon:
        _exact_keys(
            value,
            {
                "schema_version",
                "resolution_kind",
                "delivery_state",
                "event_id",
                "namespace",
                "operation_id",
                "expected_request_digest",
                "reason",
                "receipt_observation",
            },
            "operator_abandon",
        )
        if isinstance(value["schema_version"], bool) or value["schema_version"] != RESOLUTION_SCHEMA_VERSION:
            raise ContractError("resolution schema_version is unsupported")
        if value["resolution_kind"] != "operator_abandon":
            raise ContractError("resolution_kind is invalid")
        if value["delivery_state"] != "unknown":
            raise ContractError("operator_abandon.delivery_state must be unknown")
        return cls(
            schema_version=RESOLUTION_SCHEMA_VERSION,
            resolution_kind="operator_abandon",
            delivery_state="unknown",
            event_id=_text(value["event_id"], "event_id", maximum=512),
            namespace=_text(value["namespace"], "namespace", maximum=512),
            operation_id=_text(value["operation_id"], "operation_id", maximum=256),
            expected_request_digest=_digest(value["expected_request_digest"], "expected_request_digest"),
            reason=_text(value["reason"], "reason", maximum=4096),
            receipt_observation=ReceiptObservation.from_mapping(value["receipt_observation"]),
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "resolution_kind": self.resolution_kind,
            "delivery_state": self.delivery_state,
            "event_id": self.event_id,
            "namespace": self.namespace,
            "operation_id": self.operation_id,
            "expected_request_digest": self.expected_request_digest,
            "reason": self.reason,
            "receipt_observation": self.receipt_observation.as_mapping(),
        }


ResolutionEvidence = ProvenNonMutatingRejection | LiveTypedNonMutatingRejection | OperatorAbandon


def parse_resolution_evidence(raw: object) -> ResolutionEvidence:
    value = _as_mapping(raw, "resolution evidence")
    kind = value.get("resolution_kind")
    if kind == "proven_non_mutating_rejection":
        if value.get("schema_version") == LIVE_REJECTION_SCHEMA_VERSION:
            return LiveTypedNonMutatingRejection.from_mapping(value)
        return ProvenNonMutatingRejection.from_mapping(value)
    if kind == "operator_abandon":
        return OperatorAbandon.from_mapping(value)
    raise ContractError("resolution_kind must be one of the two frozen ADR 0040 values")
