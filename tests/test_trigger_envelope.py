"""A machine-triggered answer has its own input kind through delivery."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from musubi_harness.core import CapturePolicy, ContractError, Outbox, TurnEnvelope
from musubi_harness.delivery import DeliveryStore


def base() -> dict[str, object]:
    return {
        "event_id": "exchange.v1:claude-code:session:answer",
        "actor": "yua",
        "presence": "yua/command-chair",
        "plane": "episodic",
        "context": "primary",
        "source": "claude-code",
        "zone": "home",
        "user_text": "A typed request",
        "assistant_text": "An answer",
        "captured_at": "2026-09-28T12:00:00Z",
        "metadata": {},
    }


def trigger() -> dict[str, object]:
    return {
        **base(),
        "user_text": "",
        "input_kind": "trigger",
        "trigger_class": "task-notification",
        "trigger_record_id": "record-1",
        "trigger_text": "Background task completed",
    }


def test_legacy_voice_mapping_serializes_exactly_as_before() -> None:
    raw = base()
    envelope = TurnEnvelope.from_mapping(raw)
    assert envelope.as_dict() == raw
    old_sha = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    new_sha = hashlib.sha256(json.dumps(envelope.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert new_sha == old_sha


def test_trigger_is_separate_from_user_text_through_stage(tmp_path) -> None:
    db = tmp_path / "shadow.db"
    outbox = Outbox(db)
    envelope = TurnEnvelope.from_mapping(trigger())
    assert outbox.enqueue(envelope, CapturePolicy())["inserted"]
    assert not outbox.enqueue(envelope, CapturePolicy())["inserted"]
    DeliveryStore(db).stage(envelope.event_id)
    with sqlite3.connect(db) as connection:
        row = connection.execute("SELECT content FROM delivery_events WHERE event_id = ?", (envelope.event_id,)).fetchone()
    assert row is not None
    assert row[0] == "Trigger (task-notification): Background task completed\n\nAssistant: An answer"


def test_refused_trigger_redacts_trigger_text(tmp_path) -> None:
    raw = trigger()
    raw["context"] = "automation"
    envelope = TurnEnvelope.from_mapping(raw)
    db = tmp_path / "shadow.db"
    Outbox(db).enqueue(envelope, CapturePolicy())
    with sqlite3.connect(db) as connection:
        row = connection.execute("SELECT envelope_json FROM capture_events WHERE event_id = ?", (envelope.event_id,)).fetchone()
    assert row is not None
    assert "Background task completed" not in row[0]
    assert "trigger_text" not in json.loads(row[0])


def test_trigger_replay_detects_changed_text(tmp_path) -> None:
    outbox = Outbox(tmp_path / "shadow.db")
    outbox.enqueue(TurnEnvelope.from_mapping(trigger()), CapturePolicy())
    with pytest.raises(ContractError, match="divergent content"):
        outbox.enqueue(
            TurnEnvelope.from_mapping({**trigger(), "trigger_text": "Different notification"}),
            CapturePolicy(),
        )


@pytest.mark.parametrize("change", [
    {"trigger_class": "User: forged"},
    {"user_text": "Background task completed"},
    {"trigger_text": ""},
    {"trigger_text": "sk-" + "a" * 40},
])
def test_trigger_rejects_laundered_or_invalid_content(change) -> None:
    with pytest.raises(ContractError):
        TurnEnvelope.from_mapping({**trigger(), **change})
