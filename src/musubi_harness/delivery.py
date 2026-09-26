"""Capability-gated verified delivery for approved Musubi shadow captures."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .core import ContractError, TurnEnvelope
from .resolution import (
    LIVE_REJECTION_SCHEMA_VERSION,
    LiveReceiptObservation,
    LiveTypedNonMutatingRejection,
    OperatorAbandon,
    ProvenNonMutatingRejection,
    ResolutionEvidence,
    parse_resolution_evidence,
)

CAPTURE_CONTENT_TYPE = "application/json"
CAPTURE_OPERATION_ID = "capture_episodic.bucket=capture"
_DIGEST_DOMAIN = b"musubi-idem-json-v1"


def canonical_request_digest(body: bytes, content_type: str) -> str:
    """Match Musubi's byte-exact, content-type-bound idempotency digest."""
    return hashlib.sha256(_DIGEST_DOMAIN + b"\x00" + content_type.encode("latin-1") + b"\x00" + body).hexdigest()


def _capture_body(*, namespace: str, content: str, tags: tuple[str, ...], importance: int) -> bytes:
    return json.dumps(
        {
            "namespace": namespace,
            "content": content,
            "tags": list(tags),
            "importance": importance,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


class DeliveryTransientError(RuntimeError):
    """A retry may succeed without changing the delivery intent."""


class DeliveryTerminalError(RuntimeError):
    """Retrying this delivery intent would be unsafe or invalid."""


class DeliveryNonMutatingRejection(RuntimeError):
    """The server returned a typed guarantee that the attempted write did not mutate."""

    def __init__(
        self,
        *,
        response_status: int,
        error_code: str,
        response_detail: str,
        content_bytes_server: int,
        limit_bytes: int,
        observed_at: str,
    ) -> None:
        if response_status != 422 or error_code != "CONTENT_TOO_LARGE":
            raise ValueError("typed rejection status or code is invalid")
        if (
            isinstance(content_bytes_server, bool)
            or not isinstance(content_bytes_server, int)
            or isinstance(limit_bytes, bool)
            or not isinstance(limit_bytes, int)
            or content_bytes_server <= limit_bytes
        ):
            raise ValueError("typed rejection byte counts are invalid")
        if not isinstance(response_detail, str) or not response_detail:
            raise ValueError("typed rejection detail is invalid")
        if not isinstance(observed_at, str) or not observed_at:
            raise ValueError("typed rejection observation time is invalid")
        super().__init__(error_code)
        self.response_status = response_status
        self.error_code = error_code
        self.response_detail = response_detail
        self.content_bytes_server = content_bytes_server
        self.limit_bytes = limit_bytes
        self.observed_at = observed_at


@dataclass(frozen=True)
class ReceiptLookup:
    status: str
    object_id: str | None = None
    observation: LiveReceiptObservation | None = None

    def __post_init__(self) -> None:
        if self.status not in {"found", "absent", "conflict", "in_flight", "unknown"}:
            raise ValueError("receipt lookup status is invalid")
        if (self.status == "found") != bool(self.object_id):
            raise ValueError("found receipt requires exactly one object_id")
        if self.observation is not None and self.status != "absent":
            raise ValueError("receipt observation is only valid for absent state")


@dataclass(frozen=True)
class Readback:
    object_id: str
    namespace: str
    content: str


class DeliveryClient(Protocol):
    """Transport capability required by the verified-delivery state machine."""

    def lookup_receipt(
        self,
        *,
        method: str,
        operation_id: str,
        idempotency_key: str,
        namespace: str,
        request_digest: str,
    ) -> ReceiptLookup: ...

    def capture_durable(
        self,
        *,
        idempotency_key: str,
        body: bytes,
        content_type: str,
    ) -> str: ...

    def get(self, *, namespace: str, object_id: str) -> Readback: ...


@dataclass(frozen=True)
class DeliveryJob:
    event_id: str
    idempotency_key: str
    namespace: str
    content: str
    content_sha256: str
    operation_id: str
    request_content_type: str
    request_body: bytes
    request_digest: str
    post_attempted: bool
    tags: tuple[str, ...]
    importance: int
    lease_from: str
    object_id: str | None
    attempt_count: int


class DeliveryStore:
    """Durable delivery state beside the host-neutral capture outbox."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        if not self.path.is_file():
            raise ContractError("delivery requires an initialized capture outbox")
        with self._connection() as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='capture_events'").fetchone() is None:
                raise ContractError("delivery requires capture_events")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_events (
                    event_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    namespace TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    request_content_type TEXT NOT NULL,
                    request_body BLOB NOT NULL,
                    request_digest TEXT NOT NULL,
                    post_attempted INTEGER NOT NULL DEFAULT 0 CHECK (post_attempted IN (0, 1)),
                    tags_json TEXT NOT NULL,
                    importance INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 10),
                    state TEXT NOT NULL CHECK (state IN ('pending', 'leased', 'accepted', 'verified', 'dead')),
                    lease_from TEXT CHECK (lease_from IN ('pending', 'accepted')),
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    object_id TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    accepted_at REAL,
                    verified_at REAL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(event_id) REFERENCES capture_events(event_id)
                )
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(delivery_events)").fetchall()}
            additions = {
                "operation_id": "TEXT",
                "request_content_type": "TEXT",
                "request_body": "BLOB",
                "request_digest": "TEXT",
                "post_attempted": "INTEGER NOT NULL DEFAULT 0",
                # Deliberately frozen to ADR 0040's two values. A third kind
                # requires a new migration and an explicit architecture decision.
                "resolution_kind": (
                    "TEXT CHECK (resolution_kind IS NULL OR resolution_kind IN ('proven_non_mutating_rejection', 'operator_abandon'))"
                ),
                "resolution_evidence_json": "TEXT",
                "resolved_at": "REAL CHECK (resolved_at IS NULL OR resolved_at >= 0)",
            }
            legacy_post_attempted = "post_attempted" not in columns
            for name, kind in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE delivery_events ADD COLUMN {name} {kind}")
            legacy_rows = connection.execute(
                """
                SELECT event_id, namespace, content, tags_json, importance
                  FROM delivery_events
                 WHERE request_body IS NULL OR request_digest IS NULL
                """
            ).fetchall()
            for row in legacy_rows:
                try:
                    request_body = _capture_body(
                        namespace=row["namespace"],
                        content=row["content"],
                        tags=tuple(json.loads(row["tags_json"])),
                        importance=row["importance"],
                    )
                except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                    connection.execute(
                        """
                        UPDATE delivery_events
                           SET state = 'dead', last_error = 'legacy_request_reconstruction_failed',
                               lease_from = NULL, lease_owner = NULL, lease_expires_at = NULL
                         WHERE event_id = ?
                        """,
                        (row["event_id"],),
                    )
                    continue
                connection.execute(
                    """
                    UPDATE delivery_events
                       SET operation_id = ?, request_content_type = ?, request_body = ?,
                           request_digest = ?
                     WHERE event_id = ?
                    """,
                    (
                        CAPTURE_OPERATION_ID,
                        CAPTURE_CONTENT_TYPE,
                        request_body,
                        canonical_request_digest(request_body, CAPTURE_CONTENT_TYPE),
                        row["event_id"],
                    ),
                )
            if legacy_post_attempted:
                connection.execute(
                    """
                    UPDATE delivery_events
                       SET post_attempted = CASE WHEN attempt_count > 0 THEN 1 ELSE 0 END
                    """
                )
            resolution_guard = """
                (
                    NEW.resolution_kind IS NULL
                    AND NEW.resolution_evidence_json IS NULL
                    AND NEW.resolved_at IS NULL
                )
                OR
                (
                    NEW.resolution_kind IN (
                        'proven_non_mutating_rejection', 'operator_abandon'
                    )
                    AND NEW.resolution_evidence_json IS NOT NULL
                    AND json_valid(NEW.resolution_evidence_json) = 1
                    AND json_type(
                        NEW.resolution_evidence_json, '$.schema_version'
                    ) = 'integer'
                    AND json_extract(NEW.resolution_evidence_json, '$.resolution_kind')
                        = NEW.resolution_kind
                    AND (
                        (
                            NEW.resolution_kind = 'operator_abandon'
                            AND json_extract(
                                NEW.resolution_evidence_json, '$.schema_version'
                            ) = 1
                            AND json_extract(
                                NEW.resolution_evidence_json, '$.delivery_state'
                            ) = 'unknown'
                        )
                        OR
                        (
                            NEW.resolution_kind = 'proven_non_mutating_rejection'
                            AND json_extract(
                                NEW.resolution_evidence_json, '$.schema_version'
                            ) IN (1, 2)
                        )
                    )
                    AND NEW.resolved_at IS NOT NULL
                    AND NEW.state = 'dead'
                    AND NEW.post_attempted = 1
                    AND NEW.object_id IS NULL
                )
            """
            resolution_update_guard = f"""
                ({resolution_guard})
                AND
                (
                    OLD.resolution_kind IS NULL
                    OR
                    (
                        NEW.resolution_kind IS OLD.resolution_kind
                        AND NEW.resolution_evidence_json IS OLD.resolution_evidence_json
                        AND NEW.resolved_at IS OLD.resolved_at
                    )
                )
            """
            connection.execute("DROP TRIGGER IF EXISTS delivery_resolution_guard_insert")
            connection.execute("DROP TRIGGER IF EXISTS delivery_resolution_guard_update")
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS delivery_resolution_guard_insert
                BEFORE INSERT ON delivery_events
                WHEN NOT ({resolution_guard})
                BEGIN
                    SELECT RAISE(ABORT, 'invalid delivery resolution evidence');
                END
                """
            )
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS delivery_resolution_guard_update
                BEFORE UPDATE OF resolution_kind, resolution_evidence_json, resolved_at,
                                 state, object_id, post_attempted
                ON delivery_events
                WHEN NOT ({resolution_update_guard})
                BEGIN
                    SELECT RAISE(ABORT, 'invalid delivery resolution evidence');
                END
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS delivery_ready ON delivery_events(state, next_attempt_at, lease_expires_at)")

    @staticmethod
    def _content_sha(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def stage(
        self,
        event_id: str,
        *,
        content: str | None = None,
        tags: tuple[str, ...] | None = None,
        importance: int = 4,
        now: float | None = None,
    ) -> dict[str, object]:
        """Explicitly promote one approved shadow record into pending delivery."""
        if isinstance(importance, bool) or not isinstance(importance, int) or not 1 <= importance <= 10:
            raise ContractError("delivery importance is invalid")
        timestamp = time.time() if now is None else now
        with self._connection() as connection:
            captured = connection.execute(
                "SELECT disposition, envelope_json, idempotency_key FROM capture_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if captured is None or captured["disposition"] != "shadow":
                raise ContractError("only an existing shadow capture can be staged")
            envelope = TurnEnvelope.from_mapping(json.loads(captured["envelope_json"]))
            if content is None:
                content = f"User: {envelope.user_text}\n\nAssistant: {envelope.assistant_text}"
            if tags is None:
                tags = (f"src:{envelope.source}-auto",)
            if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > 131072:
                raise ContractError("delivery content is invalid")
            if len(tags) > 16 or not all(isinstance(tag, str) and tag.strip() and len(tag.encode("utf-8")) <= 128 for tag in tags):
                raise ContractError("delivery tags are invalid")
            normalized_tags = tuple(dict.fromkeys(tag.strip() for tag in tags))
            namespace = f"{envelope.presence}/episodic"
            content_sha256 = self._content_sha(content)
            request_body = _capture_body(
                namespace=namespace,
                content=content,
                tags=normalized_tags,
                importance=importance,
            )
            request_digest = canonical_request_digest(request_body, CAPTURE_CONTENT_TYPE)
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO delivery_events
                (event_id, idempotency_key, namespace, content, content_sha256,
                 operation_id, request_content_type, request_body, request_digest,
                 tags_json, importance, state, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    event_id,
                    captured["idempotency_key"],
                    namespace,
                    content,
                    content_sha256,
                    CAPTURE_OPERATION_ID,
                    CAPTURE_CONTENT_TYPE,
                    request_body,
                    request_digest,
                    json.dumps(normalized_tags, separators=(",", ":")),
                    importance,
                    timestamp,
                ),
            )
            if cursor.rowcount != 1:
                existing = connection.execute(
                    """
                    SELECT content_sha256, request_digest, tags_json, importance,
                           state, object_id
                      FROM delivery_events WHERE event_id = ?
                    """,
                    (event_id,),
                ).fetchone()
                if existing is None or (
                    existing["content_sha256"],
                    existing["request_digest"],
                    existing["tags_json"],
                    existing["importance"],
                ) != (
                    content_sha256,
                    request_digest,
                    json.dumps(normalized_tags, separators=(",", ":")),
                    importance,
                ):
                    raise ContractError("staged event collision has divergent delivery intent")
            current = connection.execute(
                "SELECT state, object_id FROM delivery_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        result: dict[str, object] = {
            "event_id": event_id,
            "state": current["state"],
            "inserted": cursor.rowcount == 1,
        }
        if current["object_id"]:
            result["object_id"] = current["object_id"]
        return result

    def status(self) -> dict[str, object]:
        with self._connection() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS count FROM delivery_events GROUP BY state").fetchall()
        return {"path": str(self.path), "counts": {row["state"]: row["count"] for row in rows}}

    def acquire(self, owner: str, *, now: float | None = None, lease_seconds: float = 30) -> DeliveryJob | None:
        """Lease the next eligible delivery, oldest-first, demoting only ambiguity.

        ORDERING POLICY (explicit, not incidental -- see the regression tests):

        ``WHERE next_attempt_at <= ?`` already gates *eligibility*, so
        ``next_attempt_at`` carries no timing information in the ORDER BY. It
        must not appear there, because the column is overloaded: fresh rows
        carry the schema default ``0`` while :meth:`retry` stores an absolute
        epoch. Ordering by it sorted every fresh row ahead of every retry row
        forever, so one transient failure orphaned a turn for as long as turns
        kept arriving (observed live on four seats, 2026-08-20).

        Among eligible rows there are two classes:

        * ``state='pending' AND post_attempted=1`` -- AMBIGUOUS. The bytes may
          already exist remotely and resolution can take a long time, so this
          class is demoted and must never head-of-line-block untouched work.
        * everything else -- untouched ``pending`` work and ``accepted`` rows
          (``object_id`` known, verification is GET-only and safe to retry).
          These share one FIFO class ordered by ``created_at``.

        A failing ``accepted`` readback backs off, so it consumes at most one
        eligible attempt and cannot permanently block fresh rows.
        """
        if not owner or len(owner) > 128 or lease_seconds <= 0:
            raise ContractError("lease owner and duration are required")
        timestamp = time.time() if now is None else now
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE delivery_events
                   SET state = lease_from, lease_from = NULL, lease_owner = NULL, lease_expires_at = NULL
                 WHERE state = 'leased' AND lease_expires_at <= ?
                """,
                (timestamp,),
            )
            row = connection.execute(
                """
                SELECT * FROM delivery_events
                 WHERE state IN ('pending', 'accepted') AND next_attempt_at <= ?
                 ORDER BY CASE WHEN state = 'pending' AND post_attempted = 1 THEN 1 ELSE 0 END,
                          created_at, event_id LIMIT 1
                """,
                (timestamp,),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """
                UPDATE delivery_events
                   SET state = 'leased', lease_from = ?, lease_owner = ?, lease_expires_at = ?,
                       attempt_count = attempt_count + 1
                 WHERE event_id = ? AND state = ?
                """,
                (row["state"], owner, timestamp + lease_seconds, row["event_id"], row["state"]),
            )
            if updated.rowcount != 1:
                raise ContractError("delivery lease race")
            leased = connection.execute("SELECT * FROM delivery_events WHERE event_id = ?", (row["event_id"],)).fetchone()
        return DeliveryJob(
            event_id=leased["event_id"],
            idempotency_key=leased["idempotency_key"],
            namespace=leased["namespace"],
            content=leased["content"],
            content_sha256=leased["content_sha256"],
            operation_id=leased["operation_id"],
            request_content_type=leased["request_content_type"],
            request_body=leased["request_body"],
            request_digest=leased["request_digest"],
            post_attempted=bool(leased["post_attempted"]),
            tags=tuple(json.loads(leased["tags_json"])),
            importance=leased["importance"],
            lease_from=leased["lease_from"],
            object_id=leased["object_id"],
            attempt_count=leased["attempt_count"],
        )

    def record_acceptance(self, job: DeliveryJob, owner: str, object_id: str, *, now: float) -> DeliveryJob:
        if not isinstance(object_id, str) or not object_id.strip() or len(object_id) > 512:
            raise DeliveryTerminalError("capture response omitted object_id")
        object_id = object_id.strip()
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE delivery_events
                   SET lease_from = 'accepted', object_id = ?, accepted_at = COALESCE(accepted_at, ?)
                 WHERE event_id = ? AND state = 'leased' AND lease_owner = ? AND lease_from = 'pending'
                """,
                (object_id, now, job.event_id, owner),
            )
            if updated.rowcount != 1:
                raise ContractError("acceptance requires the active pending lease")
        return DeliveryJob(**{**job.__dict__, "lease_from": "accepted", "object_id": object_id})

    def mark_post_attempted(self, job: DeliveryJob, owner: str) -> DeliveryJob:
        """Commit the ambiguity boundary before any capture bytes touch the network."""
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE delivery_events
                   SET post_attempted = 1
                 WHERE event_id = ? AND state = 'leased' AND lease_owner = ?
                   AND lease_from = 'pending'
                """,
                (job.event_id, owner),
            )
            if updated.rowcount != 1:
                raise ContractError("post attempt requires the active pending lease")
        return DeliveryJob(**{**job.__dict__, "post_attempted": True})

    def retry(self, job: DeliveryJob, owner: str, reason: str, *, now: float) -> None:
        delay = min(300.0, float(2 ** min(job.attempt_count, 8)))
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE delivery_events
                   SET state = lease_from, lease_from = NULL, lease_owner = NULL, lease_expires_at = NULL,
                       next_attempt_at = ?, last_error = ?
                 WHERE event_id = ? AND state = 'leased' AND lease_owner = ?
                """,
                (now + delay, reason[:500], job.event_id, owner),
            )
            if updated.rowcount != 1:
                raise ContractError("retry requires the active delivery lease")

    def finish(self, job: DeliveryJob, owner: str, state: str, reason: str | None, *, now: float) -> None:
        if state not in {"verified", "dead"}:
            raise ContractError("terminal delivery state is invalid")
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE delivery_events
                   SET state = ?, lease_from = NULL, lease_owner = NULL, lease_expires_at = NULL,
                       last_error = ?, verified_at = CASE WHEN ? = 'verified' THEN ? ELSE verified_at END
                 WHERE event_id = ? AND state = 'leased' AND lease_owner = ?
                """,
                (state, reason[:500] if reason else None, state, now, job.event_id, owner),
            )
            if updated.rowcount != 1:
                raise ContractError("finish requires the active delivery lease")

    def inspect(self, event_id: str) -> dict[str, object]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM delivery_events WHERE event_id = ?", (event_id,)).fetchone()
        if row is None:
            raise ContractError("delivery event not found")
        result = dict(row)
        tags_json = result.pop("tags_json")
        try:
            result["tags"] = json.loads(tags_json)
        except (json.JSONDecodeError, TypeError):
            if result["state"] != "dead" or result["last_error"] != "legacy_request_reconstruction_failed":
                raise ContractError("delivery tags are corrupt") from None
            result["tags"] = []
            result["tags_corrupt"] = True
        result.pop("content")
        result.pop("request_body")
        return result

    @staticmethod
    def _canonical_resolution_evidence(evidence: ResolutionEvidence) -> str:
        return json.dumps(evidence.as_mapping(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def resolve_pending(
        self,
        event_id: str,
        raw_evidence: object,
        *,
        now: float | None = None,
    ) -> dict[str, object]:
        """Terminalize one legacy ambiguity without claiming delivery success or absence."""
        evidence = parse_resolution_evidence(raw_evidence)
        if evidence.event_id != event_id:
            raise ContractError("resolution event_id does not match the requested row")
        timestamp = time.time() if now is None else now
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise ContractError("resolution timestamp is invalid")
        timestamp = float(timestamp)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ContractError("resolution timestamp is invalid")
        evidence_json = self._canonical_resolution_evidence(evidence)

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM delivery_events WHERE event_id = ?", (event_id,)).fetchone()
            if row is None:
                raise ContractError("delivery event not found")

            prior_resolution = (row["resolution_kind"], row["resolution_evidence_json"], row["resolved_at"])
            if any(value is not None for value in prior_resolution):
                if (
                    row["resolution_kind"] == evidence.resolution_kind
                    and row["resolution_evidence_json"] == evidence_json
                    and row["resolved_at"] is not None
                ):
                    return {
                        "event_id": event_id,
                        "state": row["state"],
                        "resolution_kind": row["resolution_kind"],
                        "resolved_at": row["resolved_at"],
                        "idempotent": True,
                    }
                raise ContractError("delivery event has divergent prior resolution evidence")

            if row["object_id"] is not None:
                raise ContractError("resolution refuses a delivery event with object_id")
            if any(row[field] is not None for field in ("lease_from", "lease_owner", "lease_expires_at")):
                raise ContractError("resolution refuses a leased delivery event")
            if row["state"] != "pending":
                raise ContractError("resolution requires a pending delivery event")
            if row["post_attempted"] != 1:
                raise ContractError("resolution requires post_attempted=1")

            if not isinstance(row["request_body"], (bytes, bytearray, memoryview)):
                raise ContractError("stored request body is invalid")
            if not isinstance(row["request_content_type"], str) or not row["request_content_type"]:
                raise ContractError("stored request content type is invalid")
            request_body = bytes(row["request_body"])
            request_content_type = row["request_content_type"]
            recomputed_digest = canonical_request_digest(request_body, request_content_type)
            if row["request_digest"] != recomputed_digest:
                raise ContractError("stored request digest does not match body and content type")
            if evidence.expected_request_digest != recomputed_digest:
                raise ContractError("resolution evidence request digest does not match the row")
            if evidence.namespace != row["namespace"] or evidence.operation_id != row["operation_id"]:
                raise ContractError("resolution evidence identity does not match the row")
            observation = evidence.receipt_observation
            if (
                observation.namespace != row["namespace"]
                or observation.operation_id != row["operation_id"]
                or observation.request_digest != recomputed_digest
            ):
                raise ContractError("receipt observation identity does not match the row")

            try:
                decoded_tags = json.loads(row["tags_json"])
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
                raise ContractError("delivery tags are corrupt") from exc
            if not isinstance(decoded_tags, list) or not all(isinstance(tag, str) for tag in decoded_tags):
                raise ContractError("delivery tags are corrupt")
            tags = tuple(decoded_tags)
            projected_body = _capture_body(
                namespace=row["namespace"],
                content=row["content"],
                tags=tags,
                importance=row["importance"],
            )
            if projected_body != request_body:
                raise ContractError("stored request body does not match the delivery projection")
            if isinstance(evidence, ProvenNonMutatingRejection):
                if len(row["content"].encode("utf-8")) != evidence.content_bytes:
                    raise ContractError("resolution content byte count does not match the row")
            elif not isinstance(evidence, OperatorAbandon):  # pragma: no cover - union exhaustiveness
                raise ContractError("resolution evidence kind is unsupported")

            updated = connection.execute(
                """
                UPDATE delivery_events
                   SET state = 'dead', last_error = ?, resolution_kind = ?,
                       resolution_evidence_json = ?, resolved_at = ?
                 WHERE event_id = ?
                   AND state = 'pending'
                   AND post_attempted = 1
                   AND object_id IS NULL
                   AND lease_from IS NULL
                   AND lease_owner IS NULL
                   AND lease_expires_at IS NULL
                   AND request_digest = ?
                   AND request_content_type = ?
                   AND request_body = ?
                   AND resolution_kind IS NULL
                   AND resolution_evidence_json IS NULL
                   AND resolved_at IS NULL
                """,
                (
                    f"resolved:{evidence.resolution_kind}",
                    evidence.resolution_kind,
                    evidence_json,
                    timestamp,
                    event_id,
                    recomputed_digest,
                    request_content_type,
                    request_body,
                ),
            )
            if updated.rowcount != 1:
                raise ContractError("delivery resolution compare-and-swap failed")

        return {
            "event_id": event_id,
            "state": "dead",
            "resolution_kind": evidence.resolution_kind,
            "resolved_at": timestamp,
            "idempotent": False,
        }

    def finish_live_rejection(
        self,
        job: DeliveryJob,
        owner: str,
        raw_evidence: object,
        *,
        now: float,
    ) -> dict[str, object]:
        """Finish an active POST lease from a typed pre-mutation server response."""
        evidence = parse_resolution_evidence(raw_evidence)
        if not isinstance(evidence, LiveTypedNonMutatingRejection):
            raise ContractError("live rejection requires schema_version=2 evidence")
        if evidence.event_id != job.event_id:
            raise ContractError("live rejection event_id does not match the active lease")
        evidence_json = self._canonical_resolution_evidence(evidence)

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM delivery_events WHERE event_id = ?", (job.event_id,)).fetchone()
            if row is None:
                raise ContractError("delivery event not found")
            if row["state"] != "leased" or row["lease_owner"] != owner:
                raise ContractError("live rejection requires the active delivery lease")
            if row["lease_from"] != "pending" or row["post_attempted"] != 1:
                raise ContractError("live rejection requires an attempted pending lease")
            if row["object_id"] is not None:
                raise ContractError("live rejection refuses a delivery event with object_id")
            if any(row[field] is not None for field in ("resolution_kind", "resolution_evidence_json", "resolved_at")):
                raise ContractError("live rejection refuses prior resolution evidence")
            request_body = bytes(row["request_body"])
            recomputed_digest = canonical_request_digest(request_body, row["request_content_type"])
            if row["request_digest"] != recomputed_digest:
                raise ContractError("stored request digest does not match body and content type")
            if evidence.expected_request_digest != recomputed_digest:
                raise ContractError("live rejection request digest does not match the row")
            if evidence.namespace != row["namespace"] or evidence.operation_id != row["operation_id"]:
                raise ContractError("live rejection identity does not match the row")
            observation = evidence.receipt_observation
            if (
                observation.namespace != row["namespace"]
                or observation.operation_id != row["operation_id"]
                or observation.request_digest != recomputed_digest
            ):
                raise ContractError("receipt observation identity does not match the row")
            if len(row["content"].encode("utf-8")) != evidence.content_bytes_client:
                raise ContractError("live rejection content byte count does not match the row")
            try:
                tags = tuple(json.loads(row["tags_json"]))
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
                raise ContractError("delivery tags are corrupt") from exc
            if (
                _capture_body(
                    namespace=row["namespace"],
                    content=row["content"],
                    tags=tags,
                    importance=row["importance"],
                )
                != request_body
            ):
                raise ContractError("stored request body does not match the delivery projection")

            updated = connection.execute(
                """
                UPDATE delivery_events
                   SET state = 'dead', lease_from = NULL, lease_owner = NULL,
                       lease_expires_at = NULL, last_error = ?, resolution_kind = ?,
                       resolution_evidence_json = ?, resolved_at = ?
                 WHERE event_id = ?
                   AND state = 'leased'
                   AND lease_from = 'pending'
                   AND lease_owner = ?
                   AND post_attempted = 1
                   AND object_id IS NULL
                   AND request_digest = ?
                   AND request_content_type = ?
                   AND request_body = ?
                   AND resolution_kind IS NULL
                   AND resolution_evidence_json IS NULL
                   AND resolved_at IS NULL
                """,
                (
                    "resolved:proven_non_mutating_rejection",
                    evidence.resolution_kind,
                    evidence_json,
                    now,
                    job.event_id,
                    owner,
                    recomputed_digest,
                    row["request_content_type"],
                    request_body,
                ),
            )
            if updated.rowcount != 1:
                raise ContractError("live rejection compare-and-swap failed")

        return {
            "event_id": job.event_id,
            "state": "dead",
            "resolution_kind": evidence.resolution_kind,
        }


class Drainer:
    """One-at-a-time verifier. Production activation requires receipt lookup."""

    def __init__(
        self,
        store: DeliveryStore,
        client: DeliveryClient,
        *,
        owner: str,
        checkpoint: Callable[[str, DeliveryJob], None] | None = None,
    ) -> None:
        self.store = store
        self.client = client
        self.owner = owner
        self.checkpoint = checkpoint or (lambda _name, _job: None)

    def flush_once(self, *, now: float | None = None) -> dict[str, object]:
        timestamp = time.time() if now is None else now
        job = self.store.acquire(self.owner, now=timestamp)
        if job is None:
            return {"state": "idle"}
        try:
            if job.lease_from == "pending":
                receipt = self.client.lookup_receipt(
                    method="POST",
                    operation_id=job.operation_id,
                    idempotency_key=job.idempotency_key,
                    namespace=job.namespace,
                    request_digest=job.request_digest,
                )
                if not isinstance(receipt, ReceiptLookup):
                    raise DeliveryTerminalError("receipt_lookup_shape_invalid")
                if receipt.status == "unknown":
                    raise DeliveryTransientError("receipt_lookup_unknown")
                if receipt.status == "in_flight":
                    raise DeliveryTransientError("receipt_lookup_in_flight")
                if receipt.status == "conflict":
                    raise DeliveryTerminalError("receipt_lookup_conflict")
                if receipt.status == "found":
                    job = self.store.record_acceptance(job, self.owner, receipt.object_id or "", now=timestamp)
                else:
                    if job.post_attempted:
                        raise DeliveryTransientError("receipt_absent_after_post_attempt")
                    job = self.store.mark_post_attempted(job, self.owner)
                    self.checkpoint("after_post_mark_before_post", job)
                    try:
                        object_id = self.client.capture_durable(
                            idempotency_key=job.idempotency_key,
                            body=job.request_body,
                            content_type=job.request_content_type,
                        )
                    except DeliveryNonMutatingRejection as exc:
                        if receipt.observation is None:
                            raise DeliveryTransientError("typed_rejection_receipt_observation_missing") from exc
                        evidence = LiveTypedNonMutatingRejection.from_mapping(
                            {
                                "schema_version": LIVE_REJECTION_SCHEMA_VERSION,
                                "resolution_kind": "proven_non_mutating_rejection",
                                "event_id": job.event_id,
                                "namespace": job.namespace,
                                "operation_id": job.operation_id,
                                "expected_request_digest": job.request_digest,
                                "content_bytes_client": len(job.content.encode("utf-8")),
                                "content_bytes_server": exc.content_bytes_server,
                                "limit_bytes": exc.limit_bytes,
                                "response_status": exc.response_status,
                                "error_code": exc.error_code,
                                "response_detail": exc.response_detail,
                                "observed_at": exc.observed_at,
                                "receipt_observation": receipt.observation.as_mapping(),
                            }
                        )
                        return self.store.finish_live_rejection(job, self.owner, evidence.as_mapping(), now=timestamp)
                    self.checkpoint("after_post_before_acceptance_commit", job)
                    job = self.store.record_acceptance(job, self.owner, object_id, now=timestamp)
                self.checkpoint("after_acceptance_before_get", job)
            if job.lease_from != "accepted" or not job.object_id:
                raise DeliveryTerminalError("accepted delivery receipt is incomplete")
            readback = self.client.get(namespace=job.namespace, object_id=job.object_id)
            if not isinstance(readback, Readback):
                raise DeliveryTerminalError("readback_shape_invalid")
            if readback.object_id != job.object_id:
                raise DeliveryTerminalError("readback_object_id_mismatch")
            if readback.namespace != job.namespace:
                raise DeliveryTerminalError("readback_namespace_mismatch")
            if hashlib.sha256(readback.content.encode("utf-8")).hexdigest() != job.content_sha256:
                # Episodic capture may legitimately dedup-merge our exact request into
                # an existing canonical row whose content is not byte-identical (for
                # example case/Unicode/whitespace normalization).  Reimplementing the
                # server's normalization here would create a second contract that can
                # drift.  Instead, ask the server-owned durable receipt ledger whether
                # this exact method + operation + namespace + idempotency key + request
                # digest produced the object we just read back.
                receipt = self.client.lookup_receipt(
                    method="POST",
                    operation_id=job.operation_id,
                    idempotency_key=job.idempotency_key,
                    namespace=job.namespace,
                    request_digest=job.request_digest,
                )
                if not isinstance(receipt, ReceiptLookup):
                    raise DeliveryTerminalError("receipt_lookup_shape_invalid")
                if receipt.status == "unknown":
                    raise DeliveryTransientError("dedup_readback_receipt_unknown")
                if receipt.status == "in_flight":
                    raise DeliveryTransientError("dedup_readback_receipt_in_flight")
                if receipt.status == "conflict":
                    raise DeliveryTerminalError("dedup_readback_receipt_conflict")
                if receipt.status != "found":
                    raise DeliveryTerminalError("readback_content_sha256_mismatch")
                if receipt.object_id != job.object_id:
                    raise DeliveryTerminalError("dedup_readback_receipt_object_mismatch")
                self.store.finish(job, self.owner, "verified", None, now=timestamp)
                return {
                    "event_id": job.event_id,
                    "state": "verified",
                    "object_id": job.object_id,
                    "verification": "durable_receipt_dedup",
                }
            self.store.finish(job, self.owner, "verified", None, now=timestamp)
            return {"event_id": job.event_id, "state": "verified", "object_id": job.object_id}
        except DeliveryTransientError as exc:
            self.store.retry(job, self.owner, str(exc), now=timestamp)
            return {"event_id": job.event_id, "state": job.lease_from, "reason": str(exc)}
        except DeliveryTerminalError as exc:
            self.store.finish(job, self.owner, "dead", str(exc), now=timestamp)
            return {"event_id": job.event_id, "state": "dead", "reason": str(exc)}
