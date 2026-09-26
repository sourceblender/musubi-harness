"""Validate adapter-produced JSONL envelopes against the shared core contract.

This is the package-internal version of ``bin/musubi-harness-conformance``
from the fleet-tools workspace; identical surface, identical semantics.

The console script ``musubi-harness-conformance`` declared in
``pyproject.toml`` points at ``musubi_harness.cli.conformance:main``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from musubi_harness import CapturePolicy, TurnEnvelope
from musubi_harness.core import SOURCES, ContractError


def main() -> int:
    parser = argparse.ArgumentParser(prog="musubi-harness-conformance")
    parser.add_argument("--source", required=True, choices=sorted(SOURCES))
    parser.add_argument("--file", type=Path, help="JSONL input; stdin when omitted")
    args = parser.parse_args()
    try:
        text = args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read()
        records = [line for line in text.splitlines() if line.strip()]
        if not records:
            raise ContractError("at least one envelope is required")
        dispositions: dict[str, int] = {}
        event_ids: set[str] = set()
        for line in records:
            envelope = TurnEnvelope.from_mapping(json.loads(line))
            if envelope.source != args.source:
                raise ContractError("envelope source does not match --source")
            if envelope.event_id in event_ids:
                raise ContractError("sample contains a duplicate event_id")
            event_ids.add(envelope.event_id)
            disposition = CapturePolicy().evaluate(envelope).disposition
            dispositions[disposition] = dispositions.get(disposition, 0) + 1
    except (ContractError, json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:500]}))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "source": args.source,
                "records": len(records),
                "dispositions": dispositions,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
