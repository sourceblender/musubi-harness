"""CLI for the host-neutral Musubi shadow capture core.

This is the package-internal version of ``bin/musubi-harness`` from the
fleet-tools workspace; it is identical in surface and semantics, only its
imports now resolve through the installed ``musubi_harness`` package.

The console script ``musubi-harness`` declared in ``pyproject.toml`` points
at ``musubi_harness.cli.harness:main``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from musubi_harness import (
    CapturePolicy,
    DeliveryStore,
    Drainer,
    MemoryDataClient,
    Outbox,
    TurnEnvelope,
)
from musubi_harness.core import SOURCES, ContractError
from musubi_harness.delivery import DeliveryTerminalError, DeliveryTransientError


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="musubi-harness")
    root.add_argument("--db", required=True, help="identity-and-zone-specific SQLite outbox")
    sub = root.add_subparsers(dest="command", required=True)
    enqueue = sub.add_parser("enqueue", help="validate and shadow-enqueue one TurnEnvelope")
    enqueue.add_argument("--file", type=Path, help="read the JSON envelope from a file instead of stdin")
    sub.add_parser("status", help="show local durability and queue state")
    inspect = sub.add_parser("inspect", help="show recent local shadow records")
    inspect.add_argument("--limit", type=int, default=20)
    stage = sub.add_parser("stage", help="promote one approved shadow event into pending delivery")
    stage.add_argument("--event-id", required=True)
    remember = sub.add_parser("remember", help="enqueue and stage one explicit episodic memory")
    remember.add_argument("--event-id", required=True)
    remember.add_argument("--actor", required=True)
    remember.add_argument("--presence", required=True)
    remember.add_argument("--zone", required=True)
    remember.add_argument("--source", required=True, choices=sorted(SOURCES))
    remember.add_argument("--importance", type=int, default=7)
    remember.add_argument("--topic", action="append", default=[])
    sub.add_parser("delivery-status", help="show local verified-delivery state")
    resolve = sub.add_parser("resolve", help="terminalize one legacy ambiguity with versioned operator evidence")
    resolve.add_argument("--event-id", required=True)
    resolve.add_argument("--evidence-file", type=Path, required=True)
    drain = sub.add_parser("drain", help="run one bounded verified-delivery attempt")
    drain.add_argument("--once", action="store_true", required=True)
    drain.add_argument("--owner", required=True)
    drain.add_argument("--memory-data-bin", default="memory-data")
    drain.add_argument("--timeout", type=float, default=5.0)
    return root


def main() -> int:
    args = parser().parse_args()
    result: dict[str, Any] | list[dict[str, Any]]
    try:
        outbox = Outbox(args.db)
        if args.command == "enqueue":
            text = args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read()
            result = outbox.enqueue(TurnEnvelope.from_mapping(json.loads(text)), CapturePolicy())
        elif args.command == "status":
            result = outbox.status()
        elif args.command == "inspect":
            result = outbox.inspect(limit=args.limit)
        elif args.command == "stage":
            result = DeliveryStore(args.db).stage(args.event_id)
        elif args.command == "remember":
            content = sys.stdin.read()
            envelope = TurnEnvelope.from_mapping(
                {
                    "event_id": args.event_id,
                    "actor": args.actor,
                    "presence": args.presence,
                    "plane": "episodic",
                    "context": "primary",
                    "source": args.source,
                    "zone": args.zone,
                    "user_text": "Explicit memory selected by the agent.",
                    "assistant_text": content,
                    "captured_at": datetime.now(UTC).isoformat(),
                    "metadata": {"explicit_remember": True},
                }
            )
            outbox.enqueue(envelope, CapturePolicy())
            result = DeliveryStore(args.db).stage(
                args.event_id,
                content=content,
                tags=tuple(args.topic) + (f"src:{args.source}-agent-remember",),
                importance=args.importance,
            )
        elif args.command == "delivery-status":
            result = DeliveryStore(args.db).status()
        elif args.command == "resolve":
            evidence = json.loads(args.evidence_file.read_text(encoding="utf-8"))
            result = DeliveryStore(args.db).resolve_pending(args.event_id, evidence)
        else:
            client = MemoryDataClient(args.memory_data_bin, timeout=args.timeout)
            result = Drainer(DeliveryStore(args.db), client, owner=args.owner).flush_once()
    except (
        ContractError,
        DeliveryTerminalError,
        DeliveryTransientError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:500]}))
        return 2
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
