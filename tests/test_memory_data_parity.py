"""Parity: the bundled musubi-memory-data and an operator memory-data are interchangeable.

Both binaries run as real subprocesses against the same fake Musubi. For every
subcommand the harness calls, the test compares what reached the server
(method, path, body bytes, and the headers the contract depends on), the exit
code, and the JSON on stdout.

The operator tool is private, so this runs only when pointed at one:

    MUSUBI_PARITY_MEMORY_DATA=/path/to/memory-data pytest tests/test_memory_data_parity.py

Deliberate differences are asserted as differences, not skipped: the bundled
client refuses redirects (the token must not follow a Location header).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.test_memory_data_http import TOKEN, Fake, serve

OPERATOR = os.environ.get("MUSUBI_PARITY_MEMORY_DATA", "")
BUNDLED = [sys.executable, "-m", "musubi_harness.cli.memory_data"]
CONTRACT_HEADERS = ("Authorization", "Content-Type", "Idempotency-Key", "Idempotency-Receipt")
# Values that differ per run by construction, not by transport.
VOLATILE_KEYS = {"observed_at", "checked_at", "timestamp"}

pytestmark = pytest.mark.skipif(
    not (OPERATOR and Path(OPERATOR).is_file()),
    # The operator memory-data is private and not installable in CI, so CI skips
    # parity; reviewers run it locally against their own copy.
    reason="operator memory-data is private; set MUSUBI_PARITY_MEMORY_DATA to run parity locally",
)


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def invoke(binary: list[str], url: str, argv: list[str], stdin: bytes) -> dict[str, Any]:
    env = {"MUSUBI_API_URL": url, "MUSUBI_TOKEN": TOKEN, "PATH": os.environ.get("PATH", ""), "HOME": os.environ["HOME"]}
    done = subprocess.run(
        [*binary, "--json", "--timeout", "3", "musubi", *argv],
        input=stdin,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )
    stdout = done.stdout.decode()
    return {"code": done.returncode, "stdout": scrub(json.loads(stdout)) if stdout.strip() else None}


def observe(binary: list[str], routes: list[tuple[str, str, int, Any]], argv: list[str], stdin: bytes = b"") -> dict[str, Any]:
    fake = Fake()
    for method, path, status, body in routes:
        fake.reply(method, path, status=status, body=body)
    with serve(fake) as url:
        result = invoke(binary, url, argv, stdin)
    result["requests"] = [
        {
            "method": r["method"],
            "path": r["path"],
            "body": json.loads(r["body"]) if r["body"] and not argv[0].startswith("capture") else r["body"],
            "headers": {h: r["headers"].get(h) for h in CONTRACT_HEADERS},
        }
        for r in fake.requests
    ]
    return result


DIGEST = "ab" * 32
CASES = {
    "status": ([("GET", "/v1/ops/status", 200, {"status": "ok"})], ["status"], b""),
    "recent": (
        [("POST", "/v1/retrieve", 200, {"results": [{"object_id": "o1"}]})],
        ["recent", "--namespace", "alice/laptop", "--exact", "--limit", "7", "--tags", "a,b"],
        b"",
    ),
    "search": (
        [("POST", "/v1/retrieve", 200, {"results": []})],
        ["search", "--namespace", "alice/laptop", "--exact", "--query", "tea", "--limit", "3", "--mode", "fast"],
        b"",
    ),
    "search-settled": (
        [("POST", "/v1/retrieve", 200, {"results": []})],
        ["search", "--namespace", "alice/laptop", "--exact", "--query", "tea", "--settled-only"],
        b"",
    ),
    "get": (
        [("GET", "/v1/episodic/a%2Fb", 200, {"object_id": "a/b"})],
        ["get", "--plane", "episodic", "--namespace", "alice/laptop/episodic", "--object-id", "a/b"],
        b"",
    ),
    "capture-durable": (
        [("POST", "/v1/episodic", 202, {"object_id": "o1", "state": "provisional"})],
        ["capture-durable", "--idempotency-key", "k1", "--stdin"],
        b'{"namespace":"alice/laptop/episodic","content":"  exact\\r\\n bytes "}',
    ),
    "capture-too-large": (
        [
            (
                "POST",
                "/v1/episodic",
                422,
                {"error": {"code": "CONTENT_TOO_LARGE", "detail": "episodic content is 70000 UTF-8 bytes; the limit is 65536"}},
            )
        ],
        ["capture-durable", "--idempotency-key", "k1", "--stdin"],
        b'{"namespace":"alice/laptop/episodic","content":"x"}',
    ),
    "receipt-lookup": (
        [("POST", "/v1/idempotency/receipts/lookup", 200, {"status": "committed", "object_id": "o1"})],
        ["receipt-lookup", "--namespace", "alice/laptop/episodic", "--idempotency-key", "k1", "--request-digest", DIGEST],
        b"",
    ),
    "remember-verify": (
        [
            ("POST", "/v1/episodic", 202, {"object_id": "o1", "state": "provisional", "dedup": None}),
            ("GET", "/v1/episodic/o1", 200, {"object_id": "o1", "namespace": "alice/laptop/episodic", "content": "tea"}),
        ],
        [
            "remember",
            "--namespace",
            "alice/laptop/episodic",
            "--content",
            " tea ",
            "--tags",
            "a",
            "--importance",
            "4",
            "--idempotency-key",
            "k1",
            "--verify",
        ],
        b"",
    ),
    "patch": (
        [("PATCH", "/v1/episodic/o%2F1", 200, {"object_id": "o/1", "version": 4})],
        ["patch", "--namespace", "alice/laptop/episodic", "--object-id", "o/1", "--summary", "s", "--tags", "x,y", "--importance", "2"],
        b"",
    ),
    "retract": (
        [
            ("GET", "/v1/episodic/o1", 200, {"object_id": "o1", "version": 7}),
            ("POST", "/v1/episodic/o1/retract", 200, {"status": "retracted", "object_id": "o1"}),
        ],
        [
            "retract",
            "--namespace",
            "alice/laptop/episodic",
            "--object-id",
            "o1",
            "--on",
            "2026-09-27",
            "--because",
            "decision 12",
            "--stdin",
            "--superseded-by",
            "o2",
            "--tags",
            "extra",
        ],
        b"  the truth\n",
    ),
    "retract-unfenced": (
        [("GET", "/v1/episodic/o1", 200, {"object_id": "o1"})],
        ["retract", "--namespace", "alice/laptop/episodic", "--object-id", "o1", "--on", "2026-09-27", "--because", "b", "--content", "t"],
        b"",
    ),
    "delete-soft": (
        [("DELETE", "/v1/episodic/o1", 200, {"status": "archived"})],
        ["delete", "--namespace", "alice/laptop/episodic", "--object-id", "o1"],
        b"",
    ),
    "http-401": ([("GET", "/v1/ops/status", 401, {"error": "unauthorized"})], ["status"], b""),
    "http-503": ([("POST", "/v1/retrieve", 503, {"error": "down"})], ["recent", "--namespace", "alice/laptop", "--exact"], b""),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_bundled_matches_operator(case: str) -> None:
    routes, argv, stdin = CASES[case]
    operator = observe([OPERATOR], routes, argv, stdin)
    bundled = observe(BUNDLED, routes, argv, stdin)
    # Both failing identically before the network would also compare equal.
    assert operator["requests"], "operator memory-data never reached the server"
    assert bundled == operator


def test_redirects_are_the_one_deliberate_difference() -> None:
    other = Fake()
    other.reply("GET", "/v1/ops/status", body={"status": "elsewhere"})
    with serve(other) as elsewhere:
        first = Fake()
        first.reply("GET", "/v1/ops/status", status=302, headers={"Location": elsewhere + "/v1/ops/status"})
        with serve(first) as url:
            bundled = invoke(BUNDLED, url, ["status"], b"")
    assert bundled["code"] == 2 and other.requests == []
