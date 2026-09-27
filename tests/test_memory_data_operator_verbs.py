"""The seat-scoped correction verbs: remember, patch, retract, archive.

Ported from the operator memory-data with the same argv, bodies and JSON
(parity is asserted separately in test_memory_data_parity.py). These tests pin
the behaviour each verb exists for, against the same recording fake Musubi.
"""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path
from typing import Any

from musubi_harness.cli import memory_data
from tests.test_memory_data_http import TOKEN, Fake, run, serve

NS = "alice/laptop/episodic"


def sent(fake: Fake, method: str) -> list[dict[str, Any]]:
    return [r for r in fake.requests if r["method"] == method]


# ---- remember ------------------------------------------------------------------


def test_remember_posts_body_with_source_tag_and_idempotency_key() -> None:
    fake = Fake()
    fake.reply("POST", "/v1/episodic", 202, {"object_id": "o1", "state": "provisional", "dedup": None})
    with serve(fake) as url:
        code, out, err = run(
            url,
            "remember",
            "--namespace",
            NS,
            "--content",
            "  tea at four  ",
            "--tags",
            "a,b",
            "--importance",
            "3",
            "--idempotency-key",
            "k1",
        )
    assert code == 0, err
    (post,) = sent(fake, "POST")
    assert json.loads(post["body"]) == {
        "namespace": NS,
        "content": "tea at four",
        "tags": ["a", "b", "src:memory-data-remember"],
        "importance": 3,
    }
    assert post["headers"]["Idempotency-Key"] == "k1"
    assert out["object_id"] == "o1" and out["verified"] is False and "readback" not in out
    assert sent(fake, "GET") == []


def test_remember_verify_is_a_get_readback_by_id_not_a_content_comparison() -> None:
    # Parity with the operator tool: --verify means the GET by the returned id
    # succeeded. It does not compare content; a mismatching readback still reads
    # verified=True. Hardening that is a separate, deliberate change.
    fake = Fake()
    fake.reply("POST", "/v1/episodic", 202, {"object_id": "o1", "state": "provisional"})
    fake.reply("GET", "/v1/episodic/o1", 200, {"object_id": "o1", "namespace": NS, "content": "something else"})
    with serve(fake) as url:
        code, out, err = run(url, "remember", "--namespace", NS, "--content", "tea", "--verify")
    assert code == 0, err
    (get,) = sent(fake, "GET")
    assert get["path"] == "/v1/episodic/o1?namespace=alice%2Flaptop%2Fepisodic"
    assert out["verified"] is True and out["readback"]["content"] == "something else"


def test_remember_verify_fails_when_the_readback_get_fails() -> None:
    fake = Fake()
    fake.reply("POST", "/v1/episodic", 202, {"object_id": "o1"})
    with serve(fake) as url:  # no GET route: the fake answers 404
        code, out, err = run(url, "remember", "--namespace", NS, "--content", "tea", "--verify")
    assert code == 2 and out is None and "HTTP 404 GET" in err


def test_writes_require_an_explicit_namespace() -> None:
    fake = Fake()
    with serve(fake) as url:
        code, _, err = run(url, "remember", "--namespace", " ", "--content", "tea")
    assert code == 2 and "--namespace is required" in err and fake.requests == []


def test_content_file_is_sent_as_exact_bytes(tmp_path: Path) -> None:
    source = tmp_path / "c.txt"
    source.write_bytes(b"  line one\r\n  line two  \n")
    fake = Fake()
    fake.reply("POST", "/v1/episodic", 202, {"object_id": "o1"})
    with serve(fake) as url:
        code, _, err = run(url, "remember", "--namespace", NS, "--content-file", str(source))
    assert code == 0, err
    assert json.loads(sent(fake, "POST")[0]["body"])["content"] == "  line one\r\n  line two  \n"


# ---- patch ---------------------------------------------------------------------


def test_patch_sends_only_the_given_fields_to_the_owned_row() -> None:
    fake = Fake()
    fake.reply("PATCH", "/v1/episodic/o%2F1", 200, {"object_id": "o/1", "version": 4})
    with serve(fake) as url:
        code, out, err = run(url, "patch", "--namespace", NS, "--object-id", "o/1", "--tags", "x,y", "--importance", "2")
    assert code == 0, err
    (req,) = sent(fake, "PATCH")
    assert req["path"] == "/v1/episodic/o%2F1?namespace=alice%2Flaptop%2Fepisodic"
    assert json.loads(req["body"]) == {"tags": ["x", "y"], "importance": 2}
    assert out == {"object_id": "o/1", "version": 4}


def test_patch_with_nothing_to_change_never_reaches_the_server() -> None:
    fake = Fake()
    with serve(fake) as url:
        code, _, err = run(url, "patch", "--namespace", NS, "--object-id", "o1")
    assert code == 2 and "nothing to patch" in err and fake.requests == []


def test_payload_guard_refuses_unmodeled_and_server_refused_keys() -> None:
    for body, needle in (({"content": "x", "retracted_original": "y"}, "unmodeled"), ({"state": "archived"}, "refuses these on PATCH")):
        try:
            memory_data.assert_writable_payload(body)
        except memory_data.CliError as exc:
            assert needle in str(exc)
        else:
            raise AssertionError(f"{body} was allowed")


# ---- retract -------------------------------------------------------------------


def retract_argv(*extra: str) -> list[str]:
    return [
        "retract",
        "--namespace",
        NS,
        "--object-id",
        "o1",
        "--on",
        "2026-09-27",
        "--because",
        "decision 12 says otherwise",
        "--content",
        "the truth",
        *extra,
    ]


def test_retract_fences_on_the_observed_version_with_a_deterministic_key() -> None:
    fake = Fake()
    fake.reply("GET", "/v1/episodic/o1", 200, {"object_id": "o1", "version": 7})
    fake.reply("POST", "/v1/episodic/o1/retract", 200, {"status": "retracted"})
    with serve(fake) as url:
        code, out, err = run(url, *retract_argv("--superseded-by", "o2", "--tags", "false,extra"))
    assert code == 0, err
    (post,) = sent(fake, "POST")
    body = json.loads(post["body"])
    assert body == {
        "namespace": NS,
        "expected_version": 7,
        "on": "2026-09-27",
        "because": "decision 12 says otherwise",
        "truth": "the truth",
        "summary": None,
        "tags": ["retracted", "false", "do-not-act-on", "superseded-by:o2", "extra"],
    }
    assert post["headers"]["Idempotency-Key"] == memory_data.retraction_idempotency_key("o1", body)
    assert out == {"status": "retracted"}


def test_retract_key_changes_with_the_intended_truth() -> None:
    base = {"namespace": NS, "expected_version": 7, "truth": "a"}
    assert memory_data.retraction_idempotency_key("o1", base) == memory_data.retraction_idempotency_key("o1", dict(base))
    assert memory_data.retraction_idempotency_key("o1", base) != memory_data.retraction_idempotency_key("o1", {**base, "truth": "b"})


def test_retract_refuses_an_unfenced_request_when_version_is_not_canonical() -> None:
    for version in (None, "7", True, -1):
        fake = Fake()
        fake.reply("GET", "/v1/episodic/o1", 200, {"object_id": "o1", "version": version})
        with serve(fake) as url:
            code, _, err = run(url, *retract_argv())
        assert code == 2 and "unfenced retraction" in err, version
        assert sent(fake, "POST") == []


def test_retract_expected_version_override_replays_exactly() -> None:
    fake = Fake()
    fake.reply("GET", "/v1/episodic/o1", 200, {"object_id": "o1", "version": 9})
    fake.reply("POST", "/v1/episodic/o1/retract", 200, {"status": "retracted"})
    with serve(fake) as url:
        code, _, err = run(url, *retract_argv("--expected-version", "7", "--idempotency-key", "replay-1"))
    assert code == 0, err
    (post,) = sent(fake, "POST")
    assert json.loads(post["body"])["expected_version"] == 7 and post["headers"]["Idempotency-Key"] == "replay-1"


def test_retract_dry_run_reads_the_version_and_never_posts() -> None:
    fake = Fake()
    fake.reply("GET", "/v1/episodic/o1", 200, {"object_id": "o1", "version": 3})
    with serve(fake) as url:
        code, out, err = run(url, *retract_argv("--dry-run"))
    assert code == 0, err
    assert sent(fake, "POST") == [] and out["status"] == "proposed_request_only" and out["body"]["expected_version"] == 3


def test_ambiguous_retract_transport_prints_the_replay_and_never_the_token() -> None:
    # GET succeeds, then the connection drops mid-POST: the request may have landed.
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            raw = json.dumps({"object_id": "o1", "version": 5}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.close_connection = True  # no status line at all

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        code, out, err = run(f"http://127.0.0.1:{server.server_address[1]}", *retract_argv("--idempotency-key", "k-amb"))
    finally:
        server.shutdown()
        server.server_close()
    assert code == 2 and out is None
    assert "may be ambiguous" in err and "do not blind-retry" in err
    assert "--expected-version 5 --idempotency-key k-amb" in err
    assert TOKEN not in err


# ---- archive -------------------------------------------------------------------


def test_archive_and_its_delete_alias_soft_delete_the_owned_row() -> None:
    for verb in ("archive", "delete"):
        fake = Fake()
        fake.reply("DELETE", "/v1/episodic/o1", 200, {"status": "archived"})
        with serve(fake) as url:
            code, out, err = run(url, verb, "--namespace", NS, "--object-id", "o1")
        assert code == 0, err
        (req,) = sent(fake, "DELETE")
        assert req["path"] == "/v1/episodic/o1?namespace=alice%2Flaptop%2Fepisodic"
        assert out == {"status": "archived"}


def test_hard_delete_is_refused_locally_and_never_sent() -> None:
    fake = Fake()
    with serve(fake) as url:
        code, _, err = run(url, "delete", "--namespace", NS, "--object-id", "o1", "--hard", "--i-have-operator-scope")
    assert code == 2 and "operator scope" in err and fake.requests == []


def test_refused_archive_explains_without_printing_the_server_body() -> None:
    fake = Fake()
    detail = f"delete transition rejected: episodic: matured -> archived not permitted {TOKEN}"
    fake.reply("DELETE", "/v1/episodic/o1", 400, {"error": {"code": "BAD_REQUEST", "detail": detail}})
    with serve(fake) as url:
        code, _, err = run(url, "archive", "--namespace", NS, "--object-id", "o1")
    assert code == 2 and "use retract" in err and "(BAD_REQUEST)" in err
    assert TOKEN not in err and "not permitted" not in err


def test_write_errors_never_echo_the_server_body() -> None:
    fake = Fake()
    fake.reply("PATCH", "/v1/episodic/o1", 500, {"error": {"code": "INTERNAL", "detail": f"Authorization: Bearer {TOKEN}"}})
    with serve(fake) as url:
        code, _, err = run(url, "patch", "--namespace", NS, "--object-id", "o1", "--summary", "s")
    assert code == 2 and TOKEN not in err and "HTTP 500 PATCH" in err


def test_retract_5xx_or_408_after_post_is_ambiguous_and_prints_the_replay() -> None:
    # A proxy can answer 503 after Musubi committed the retraction.
    for status in (408, 500, 502, 503, 504):
        fake = Fake()
        fake.reply("GET", "/v1/episodic/o1", 200, {"object_id": "o1", "version": 5})
        detail = f"upstream said Bearer {TOKEN}"
        fake.reply("POST", "/v1/episodic/o1/retract", status, {"error": {"code": "BACKEND_UNAVAILABLE", "detail": detail}})
        with serve(fake) as url:
            code, out, err = run(url, *retract_argv("--idempotency-key", "k-5xx"))
        assert code == 2 and out is None, status
        assert f"HTTP {status} POST" in err and "may be ambiguous" in err
        assert "--expected-version 5 --idempotency-key k-5xx" in err
        assert TOKEN not in err and "upstream said" not in err


def test_retract_4xx_is_a_refusal_not_an_ambiguous_outcome() -> None:
    fake = Fake()
    fake.reply("GET", "/v1/episodic/o1", 200, {"object_id": "o1", "version": 5})
    fake.reply("POST", "/v1/episodic/o1/retract", 409, {"error": {"code": "CONFLICT", "detail": "version_fence_violation"}})
    with serve(fake) as url:
        code, _, err = run(url, *retract_argv())
    assert code == 2 and "HTTP 409 POST" in err and "(CONFLICT)" in err and "ambiguous" not in err
