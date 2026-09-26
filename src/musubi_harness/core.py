"""Validated envelopes, capture policy, and the durable local shadow outbox."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SOURCES = frozenset({"codex", "claude-code", "openclaw", "hermes"})
CONTEXTS = frozenset({"primary", "subagent", "automation", "unknown"})
ZONES = frozenset({"home", "work"})
PLANES = frozenset({"episodic", "semantic", "procedural", "affective"})
IDENTITY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
PRESENCE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}/[a-z0-9][a-z0-9_-]{0,31}$")
EVENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
SECRET_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{16,}\b|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}",
    re.IGNORECASE,
)
FORBIDDEN_METADATA = frozenset({"system_prompt", "developer_prompt", "reasoning", "tool_output", "token", "secret"})


class ContractError(ValueError):
    """An adapter violated the host-neutral capture contract."""


def _required_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be non-empty text")
    if len(value.encode("utf-8")) > maximum:
        raise ContractError(f"{name} exceeds {maximum} bytes")
    return value


@dataclass(frozen=True)
class TurnEnvelope:
    event_id: str
    actor: str
    presence: str
    plane: str
    context: str
    source: str
    zone: str
    user_text: str
    assistant_text: str
    captured_at: str
    metadata: Mapping[str, str | int | float | bool | None] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> TurnEnvelope:
        if not isinstance(raw, Mapping):
            raise ContractError("envelope must be an object")
        expected = set(cls.__dataclass_fields__)
        unknown = set(raw) - expected
        missing = expected - set(raw)
        if unknown or missing:
            raise ContractError(f"envelope fields mismatch: missing={sorted(missing)} unknown={sorted(unknown)}")
        envelope = cls(**raw)
        envelope.validate()
        return envelope

    def validate(self) -> None:
        if not isinstance(self.event_id, str) or not EVENT_RE.fullmatch(self.event_id):
            raise ContractError("event_id is invalid")
        if not isinstance(self.actor, str) or not IDENTITY_RE.fullmatch(self.actor):
            raise ContractError("actor must be an explicit lowercase identity")
        if not isinstance(self.presence, str) or not PRESENCE_RE.fullmatch(self.presence):
            raise ContractError("presence must be an explicit actor/seat identity")
        if self.presence.split("/", 1)[0] != self.actor:
            raise ContractError("presence actor prefix must match actor")
        if self.plane not in PLANES:
            raise ContractError("plane is invalid")
        if self.context not in CONTEXTS:
            raise ContractError("context is invalid")
        if self.source not in SOURCES:
            raise ContractError("source is invalid")
        if self.zone not in ZONES:
            raise ContractError("zone is invalid")
        user_text = _required_text(self.user_text, "user_text", 65536)
        assistant_text = _required_text(self.assistant_text, "assistant_text", 65536)
        if SECRET_RE.search(user_text) or SECRET_RE.search(assistant_text):
            raise ContractError("capture contains secret-like material")
        try:
            parsed = datetime.fromisoformat(self.captured_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ContractError("captured_at must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise ContractError("captured_at must include a timezone")
        if not isinstance(self.metadata, Mapping) or len(self.metadata) > 16:
            raise ContractError("metadata must be an object with at most 16 keys")
        for key, value in self.metadata.items():
            if not isinstance(key, str) or len(key) > 48 or key.lower() in FORBIDDEN_METADATA:
                raise ContractError("metadata contains a forbidden key")
            if not isinstance(value, (str, int, float, bool, type(None))):
                raise ContractError("metadata values must be scalar")
            if isinstance(value, str) and (len(value.encode("utf-8")) > 1024 or SECRET_RE.search(value)):
                raise ContractError("metadata value is unsafe")

    def as_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass(frozen=True)
class CaptureDecision:
    disposition: str
    reason: str


class CapturePolicy:
    """Fail-closed Phase 0 policy: primary turns become local shadow records only."""

    def __init__(self, *, mode: str = "shadow") -> None:
        if mode != "shadow":
            raise ContractError("Phase 0 only supports shadow mode")
        self.mode = mode

    def evaluate(self, envelope: TurnEnvelope) -> CaptureDecision:
        envelope.validate()
        if envelope.context != "primary":
            return CaptureDecision("refuse", f"context:{envelope.context}")
        return CaptureDecision("shadow", "phase0-shadow-only")


class Outbox:
    """SQLite/WAL outbox. Phase 0 never performs a remote Musubi write."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS capture_events (
                    event_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    actor TEXT NOT NULL,
                    presence TEXT NOT NULL,
                    zone TEXT NOT NULL,
                    source TEXT NOT NULL,
                    context TEXT NOT NULL,
                    disposition TEXT NOT NULL CHECK (disposition IN ('shadow', 'refuse')),
                    reason TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    envelope_sha256 TEXT NOT NULL,
                    enqueued_at TEXT NOT NULL
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS capture_events_scope ON capture_events(actor, zone, disposition)")

    @staticmethod
    def _key(envelope: TurnEnvelope) -> str:
        raw = f"v1\0{envelope.source}\0{envelope.actor}\0{envelope.zone}\0{envelope.event_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def enqueue(self, envelope: TurnEnvelope, policy: CapturePolicy) -> dict[str, Any]:
        decision = policy.evaluate(envelope)
        envelope_dict = envelope.as_dict()
        if decision.disposition == "refuse":
            envelope_dict = {key: value for key, value in envelope_dict.items() if key not in {"user_text", "assistant_text", "metadata"}}
            envelope_dict["content_redacted"] = True
        payload = json.dumps(
            envelope_dict,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO capture_events
                (event_id, idempotency_key, actor, presence, zone, source, context,
                 disposition, reason, envelope_json, envelope_sha256, enqueued_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.event_id,
                    self._key(envelope),
                    envelope.actor,
                    envelope.presence,
                    envelope.zone,
                    envelope.source,
                    envelope.context,
                    decision.disposition,
                    decision.reason,
                    payload,
                    payload_sha256,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                existing = connection.execute(
                    "SELECT envelope_json, envelope_sha256 FROM capture_events WHERE event_id = ?",
                    (envelope.event_id,),
                ).fetchone()
                equivalent_retry = False
                if existing is not None and existing["envelope_sha256"] != payload_sha256:
                    try:
                        stored_envelope = json.loads(existing["envelope_json"])
                        retry_envelope = json.loads(payload)
                    except (json.JSONDecodeError, TypeError):
                        pass
                    else:
                        stored_envelope.pop("captured_at", None)
                        retry_envelope.pop("captured_at", None)
                        equivalent_retry = stored_envelope == retry_envelope
                if existing is None or (existing["envelope_sha256"] != payload_sha256 and not equivalent_retry):
                    raise ContractError("event_id collision has divergent content")
        return {
            "event_id": envelope.event_id,
            "disposition": decision.disposition,
            "reason": decision.reason,
            "inserted": cursor.rowcount == 1,
        }

    def status(self) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute("SELECT disposition, COUNT(*) AS count FROM capture_events GROUP BY disposition").fetchall()
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
        counts = {row["disposition"]: row["count"] for row in rows}
        return {
            "path": str(self.path),
            "mode": "shadow",
            "remote_writes_enabled": False,
            "counts": counts,
            "journal_mode": journal_mode,
            "synchronous": synchronous,
        }

    def inspect(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ContractError("limit must be between 1 and 200")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT envelope_json, disposition, reason, enqueued_at FROM capture_events ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "envelope": json.loads(row["envelope_json"]),
                "disposition": row["disposition"],
                "reason": row["reason"],
                "enqueued_at": row["enqueued_at"],
            }
            for row in rows
        ]
