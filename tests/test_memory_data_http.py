"""musubi-memory-data: the public HTTP client speaks memory-data's argv and JSON.

A fake Musubi server records every request, so each test asserts both what was
sent (method, path, headers, exact bytes) and what the harness will parse.
"""

from __future__ import annotations

import base64
import http.server
import io
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from musubi_harness.cli import memory_data
from musubi_harness.plugin_runtime import PluginRuntime, RuntimeConfigError


def jwt(claims: dict[str, Any]) -> str:
    def part(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


TOKEN = jwt({"iss": "musubi", "sub": "alice/laptop", "presence": "alice/laptop", "scope": "alice/**:rw"})


class Fake:
    """A tiny Musubi: routes -> (status, headers, body); records requests."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.routes: dict[tuple[str, str], tuple[int, dict[str, str], bytes]] = {}

    def reply(self, method: str, path: str, status: int = 200, body: Any = None, headers: dict[str, str] | None = None):
        raw = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode()
        self.routes[(method, path)] = (status, headers or {}, raw)


@contextmanager
def serve(fake: Fake) -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path = self.path.split("?", 1)[0]
            fake.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
            status, headers, raw = fake.routes.get((self.command, path), (404, {}, b'{"error":"nf"}'))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PATCH = do_DELETE = _handle

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def run(url: str, *argv: str, stdin: bytes = b"", token: str = TOKEN) -> tuple[int, Any, str]:
    out, err = io.StringIO(), io.StringIO()
    env = {"MUSUBI_API_URL": url, "MUSUBI_TOKEN": token}
    stdin_obj = io.TextIOWrapper(io.BytesIO(stdin))
    with patch.dict("os.environ", env, clear=True), patch("sys.stdin", stdin_obj), redirect_stdout(out), redirect_stderr(err):
        code = memory_data.main(["--json", "--timeout", "3", "musubi", *argv])
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None), err.getvalue()


def test_status() -> None:
    fake = Fake()
    fake.reply("GET", "/v1/ops/status", body={"status": "ok", "components": {}})
    with serve(fake) as url:
        code, payload, _ = run(url, "status")
    assert (code, payload["status"]) == (0, "ok")
    assert fake.requests[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_url_ending_in_v1_is_not_doubled() -> None:
    fake = Fake()
    fake.reply("GET", "/v1/ops/status", body={"status": "ok"})
    with serve(fake) as url:
        code, _, _ = run(url + "/v1/", "status")
    assert code == 0 and fake.requests[0]["path"] == "/v1/ops/status"


def test_recent_and_search_bodies_match_memory_data() -> None:
    fake = Fake()
    fake.reply("POST", "/v1/retrieve", body={"results": []})
    with serve(fake) as url:
        run(url, "recent", "--namespace", "alice/laptop", "--exact", "--limit", "7", "--tags", "a, b")
        run(
            url,
            "search",
            "--namespace",
            "alice/laptop",
            "--exact",
            "--query",
            "tea",
            "--limit",
            "3",
            "--mode",
            "fast",
            "--planes",
            "episodic,curated",
        )
    recent, search = (json.loads(r["body"]) for r in fake.requests)
    assert recent == {"namespace": "alice/laptop", "mode": "recent", "limit": 7, "tags": ["a", "b"]}
    assert search == {
        "namespace": "alice/laptop",
        "query_text": "tea",
        "mode": "fast",
        "limit": 3,
        "state_filter": ["provisional", "matured", "promoted"],
        "planes": ["episodic", "curated"],
    }


def test_get_quotes_the_id_and_scopes_the_namespace() -> None:
    fake = Fake()
    fake.reply("GET", "/v1/episodic/a%2Fb", body={"object_id": "a/b"})
    with serve(fake) as url:
        code, payload, _ = run(url, "get", "--plane", "episodic", "--namespace", "alice/laptop/episodic", "--object-id", "a/b")
    assert code == 0 and payload == {"object_id": "a/b"}
    assert fake.requests[0]["path"] == "/v1/episodic/a%2Fb?namespace=alice%2Flaptop%2Fepisodic"


def test_capture_durable_sends_the_exact_bytes_with_receipt_headers() -> None:
    fake = Fake()
    fake.reply("POST", "/v1/episodic", status=202, body={"object_id": "obj1", "state": "provisional"})
    raw = b'{"namespace":"alice/laptop/episodic","content":"  exact\\r\\n bytes "}'
    with serve(fake) as url:
        code, payload, _ = run(url, "capture-durable", "--idempotency-key", "k1", "--stdin", stdin=raw)
    request = fake.requests[0]
    assert code == 0 and payload["object_id"] == "obj1"
    assert request["body"] == raw  # not re-serialised: the receipt digest binds these bytes
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["headers"]["Idempotency-Key"] == "k1"
    assert request["headers"]["Idempotency-Receipt"] == "durable"


def test_capture_durable_turns_content_too_large_into_a_terminal_rejection() -> None:
    fake = Fake()
    detail = "episodic content is 70000 UTF-8 bytes; the limit is 65536"
    fake.reply("POST", "/v1/episodic", status=422, body={"error": {"code": "CONTENT_TOO_LARGE", "detail": detail}})
    with serve(fake) as url:
        code, payload, _ = run(url, "capture-durable", "--idempotency-key", "k1", "--stdin", stdin=b'{"namespace":"n"}')
    rejection = payload["terminal_rejection"]
    assert code == 0
    assert (rejection["content_bytes_server"], rejection["limit_bytes"], rejection["response_status"]) == (70000, 65536, 422)


def test_receipt_lookup_attaches_self_attested_claims() -> None:
    fake = Fake()
    fake.reply("POST", "/v1/idempotency/receipts/lookup", body={"status": "committed", "object_id": "obj1"})
    digest = "AB" * 32
    with serve(fake) as url:
        code, payload, _ = run(
            url, "receipt-lookup", "--namespace", "alice/laptop/episodic", "--idempotency-key", "k1", "--request-digest", digest
        )
    sent = json.loads(fake.requests[0]["body"])
    observation = payload["receipt_observation"]
    assert code == 0 and sent["request_digest"] == digest.lower() and sent["method"] == "POST"
    assert observation["subject"] == "alice/laptop" and observation["attestation"] == "self_attested"
    assert observation["effective_scopes"] == ["alice/**:rw"] and observation["status"] == "committed"


def test_a_redirect_is_refused_and_the_token_never_leaves() -> None:
    other = Fake()
    other.reply("GET", "/v1/ops/status", body={"status": "stolen"})
    with serve(other) as elsewhere:
        first = Fake()
        first.reply("GET", "/v1/ops/status", status=302, headers={"Location": elsewhere + "/v1/ops/status"})
        with serve(first) as url:
            code, payload, err = run(url, "status")
    assert code == 2 and payload is None and "redirect" in err
    assert other.requests == []


def test_http_errors_exit_2_with_the_status_on_stderr() -> None:
    fake = Fake()
    fake.reply("GET", "/v1/ops/status", status=401, body={"error": "unauthorized"})
    with serve(fake) as url:
        code, _, err = run(url, "status")
    assert code == 2 and err.startswith("error: Musubi HTTP 401")


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"MUSUBI_TOKEN": TOKEN}, "MUSUBI_API_URL is not configured"),
        ({"MUSUBI_API_URL": "http://127.0.0.1:9"}, "MUSUBI_TOKEN is not configured"),
        ({"MUSUBI_API_URL": "http://user:pw@host", "MUSUBI_TOKEN": TOKEN}, "without credentials"),
        ({"MUSUBI_API_URL": "ftp://host", "MUSUBI_TOKEN": TOKEN}, "http(s)"),
        # urlsplit/.hostname/.port raise ValueError on these; they must still
        # be bad config (exit 2), never a traceback. Yua's review, 2026-09-26.
        ({"MUSUBI_API_URL": "http://[::1", "MUSUBI_TOKEN": TOKEN}, "Invalid IPv6 URL"),
        ({"MUSUBI_API_URL": "http://host]/", "MUSUBI_TOKEN": TOKEN}, "Invalid IPv6 URL"),
        ({"MUSUBI_API_URL": "http://[zz]/", "MUSUBI_TOKEN": TOKEN}, "does not appear to be an IPv4 or IPv6"),
        ({"MUSUBI_API_URL": "http://host:99999", "MUSUBI_TOKEN": TOKEN}, "Port out of range"),
        ({"MUSUBI_API_URL": "http://host:abc", "MUSUBI_TOKEN": TOKEN}, "Port could not be cast"),
    ],
)
def test_bad_configuration_fails_before_any_network(env: dict[str, str], message: str) -> None:
    err = io.StringIO()
    with patch.dict("os.environ", env, clear=True), redirect_stderr(err), redirect_stdout(io.StringIO()):
        code = memory_data.main(["--json", "musubi", "status"])
    assert code == 2 and message in err.getvalue()


def test_runtime_falls_back_to_the_bundled_client_last(tmp_path: Path) -> None:
    runtime = PluginRuntime("harness-test", default_data_root=tmp_path)
    config = type("C", (), {"memory_data_bin": None, "harness_bin": None})()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    bundled = bin_dir / "musubi-memory-data"
    bundled.write_text("#!/bin/sh\n")
    bundled.chmod(0o755)
    with patch.dict("os.environ", {"PATH": str(bin_dir)}, clear=True):
        assert runtime.memory_data_bin(config) == str(bundled)
    # An operator memory-data on PATH still wins.
    operator = bin_dir / "memory-data"
    operator.write_text("#!/bin/sh\n")
    operator.chmod(0o755)
    with patch.dict("os.environ", {"PATH": str(bin_dir)}, clear=True):
        assert runtime.memory_data_bin(config) == str(operator)


def test_nothing_available_is_still_refused(tmp_path: Path) -> None:
    runtime = PluginRuntime("harness-test", default_data_root=tmp_path)
    config = type("C", (), {"memory_data_bin": None, "harness_bin": None})()
    with (
        patch.dict("os.environ", {"PATH": str(tmp_path)}, clear=True),
        patch("musubi_harness.plugin_runtime.sys.executable", str(tmp_path / "python")),
        pytest.raises(RuntimeConfigError, match="memory_data_unavailable"),
    ):
        runtime.memory_data_bin(config)


def test_an_opaque_token_works_as_a_bearer_but_not_for_receipt_lookup() -> None:
    fake = Fake()
    fake.reply("GET", "/v1/ops/status", body={"status": "ok"})
    fake.reply("POST", "/v1/idempotency/receipts/lookup", body={"status": "committed"})
    with serve(fake) as url:
        status_code, _, _ = run(url, "status", token="opaque-token")
        code, payload, err = run(
            url,
            "receipt-lookup",
            "--namespace",
            "alice/laptop/episodic",
            "--idempotency-key",
            "k1",
            "--request-digest",
            "ab" * 32,
            token="opaque-token",
        )
    assert status_code == 0
    assert code == 2 and payload is None and "not a JWT" in err


SECRET = "s3cr3t-value"


@pytest.mark.parametrize(
    "bad_token",
    [f"{SECRET}\nattack", f"{SECRET}\r\nX-Evil: 1", f"{SECRET} attack", f"{SECRET}é"],
    ids=["newline", "crlf-header-injection", "space", "non-ascii"],
)
def test_a_malformed_token_is_refused_without_echoing_it(bad_token: str) -> None:
    # Yua's review, 2026-09-26: http.client's "Invalid header value" error
    # quoted the whole Authorization header, token included, on stderr.
    fake = Fake()
    fake.reply("GET", "/v1/ops/status", body={"status": "ok"})
    with serve(fake) as url:
        code, payload, err = run(url, "status", token=bad_token)
    assert code == 2 and payload is None
    assert "cannot have" in err and SECRET not in err
    assert fake.requests == []


def test_server_supplied_exception_text_is_never_printed() -> None:
    # http.client quotes a malformed status line verbatim; that line is the
    # server's. Only the class name reaches stderr.
    import http.client

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise http.client.BadStatusLine(f"HTTP/1.1 {TOKEN}")

    out, err = io.StringIO(), io.StringIO()
    env = {"MUSUBI_API_URL": "http://127.0.0.1:9", "MUSUBI_TOKEN": TOKEN}
    with (
        patch.dict("os.environ", env, clear=True),
        patch.object(memory_data._OPENER, "open", explode),
        redirect_stdout(out),
        redirect_stderr(err),
    ):
        code = memory_data.main(["--json", "musubi", "status"])
    assert code == 2 and "BadStatusLine" in err.getvalue()
    assert TOKEN not in err.getvalue() + out.getvalue()


def test_a_local_socket_error_keeps_its_os_message() -> None:
    fake_closed = "http://127.0.0.1:9"  # nothing listens on the discard port
    code, _, err = run(fake_closed, "status")
    assert code == 2 and "Connection refused" in err


def _abc_escaped() -> str:
    return "".join(f"\\u{ord(ch):04x}" for ch in "abc123")


@pytest.mark.parametrize(
    "body",
    [
        f'{{"error": {{"code": "E", "detail": "Bearer {TOKEN}"}}}}',
        '{"detail": "Bearer ' + _abc_escaped() + '"}',
        '{"detail": "Bearer abc%31%32%33"}',
        "<html>Bearer abc123 untrusted-marker</html>",
    ],
    ids=["raw", "json-unicode-escapes", "percent-encoded", "html"],
)
@pytest.mark.parametrize("status", [401, 422, 502])
def test_an_error_body_is_never_printed_in_any_encoding(status: int, body: str) -> None:
    # Tama's review, 2026-09-26: a body with the token as JSON \\u escapes got
    # past spelling-based redaction. The body is no longer printed at all.
    fake = Fake()
    fake.reply("GET", "/v1/ops/status", status=status, body=body.encode())
    fake.reply("POST", "/v1/episodic", status=status, body=body.encode())
    token = TOKEN if "Bearer ey" in body else "abc123"
    with serve(fake) as url:
        status_code, status_out, status_err = run(url, "status", token=token)
        capture_code, capture_out, capture_err = run(
            url, "capture-durable", "--idempotency-key", "k1", "--stdin", stdin=b'{"namespace":"alice/laptop/episodic"}', token=token
        )
    printed = status_err + capture_err + json.dumps(status_out) + json.dumps(capture_out)
    assert status_code == 2 and capture_code == 2
    assert status_err.startswith(f"error: Musubi HTTP {status} GET /ops/status")
    assert "Bearer" not in printed and "untrusted-marker" not in printed
    # Decoding what was printed must not recover the token either.
    assert token not in printed.encode().decode("unicode_escape")


def test_a_well_formed_server_error_code_is_kept() -> None:
    fake = Fake()
    fake.reply("POST", "/v1/retrieve", status=503, body={"error": {"code": "BACKEND_UNAVAILABLE", "detail": "x"}})
    fake.reply("GET", "/v1/ops/status", status=503, body={"error": {"code": "not a code: Bearer x"}})
    with serve(fake) as url:
        _, _, kept = run(url, "recent", "--namespace", "alice/laptop", "--exact")
        _, _, dropped = run(url, "status")
    assert kept.strip() == "error: Musubi HTTP 503 POST /retrieve (BACKEND_UNAVAILABLE)"
    assert dropped.strip() == "error: Musubi HTTP 503 GET /ops/status"


@pytest.mark.parametrize(
    "error",
    [
        OSError(111, f"Bearer {TOKEN}"),
        ConnectionRefusedError(61, f"Bearer {TOKEN}"),
        OSError(f"Bearer {TOKEN}"),
        TimeoutError(f"Bearer {TOKEN}"),
    ],
    ids=["oserror-errno", "subclass-errno", "oserror-no-errno", "timeout"],
)
def test_no_text_held_by_the_exception_is_printed(error: OSError) -> None:
    # Tama's review, 2026-09-26: strerror is a constructor argument, not proof
    # the OS wrote it. Only class, errno and a local os.strerror lookup print.
    import urllib.error

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise urllib.error.URLError(error)

    out, err = io.StringIO(), io.StringIO()
    env = {"MUSUBI_API_URL": "http://127.0.0.1:9", "MUSUBI_TOKEN": TOKEN}
    with (
        patch.dict("os.environ", env, clear=True),
        patch.object(memory_data._OPENER, "open", explode),
        redirect_stdout(out),
        redirect_stderr(err),
    ):
        code = memory_data.main(["--json", "musubi", "status"])
    assert code == 2 and type(error).__name__ in err.getvalue()
    assert "Bearer" not in err.getvalue() and TOKEN not in err.getvalue() + out.getvalue()
    if error.errno is not None:
        import os as _os

        assert f"errno {error.errno}: {_os.strerror(error.errno)}" in err.getvalue()


@pytest.mark.parametrize(
    "raised",
    [
        lambda: urllib_error().URLError(OSError(10**100, f"Bearer {TOKEN}")),
        lambda: urllib_error().URLError(OSError(-(10**100), f"Bearer {TOKEN}")),
        lambda: RuntimeError(f"Bearer {TOKEN}"),
        lambda: KeyError(f"Bearer {TOKEN}"),
    ],
    ids=["huge-errno", "huge-negative-errno", "unexpected-runtime", "unexpected-keyerror"],
)
def test_nothing_escapes_main_and_no_traceback_carries_the_token(raised: Any) -> None:
    # Tama's review, 2026-09-26: os.strerror(10**100) raised OverflowError out
    # of main, and the traceback printed the chained "Bearer ..." message.
    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise raised()

    out, err = io.StringIO(), io.StringIO()
    env = {"MUSUBI_API_URL": "http://127.0.0.1:9", "MUSUBI_TOKEN": TOKEN}
    with (
        patch.dict("os.environ", env, clear=True),
        patch.object(memory_data._OPENER, "open", explode),
        redirect_stdout(out),
        redirect_stderr(err),
    ):
        code = memory_data.main(["--json", "musubi", "status"])  # must return, not raise
    assert code == 2
    assert "Bearer" not in err.getvalue() and TOKEN not in err.getvalue() + out.getvalue()
    if isinstance(raised(), urllib_error().URLError):
        # The errno bound handles these itself; the main() backstop is not needed.
        assert err.getvalue().strip() == "error: Musubi request failed GET /ops/status: OSError"


def test_an_unexpected_error_outside_the_request_is_also_contained() -> None:
    err = io.StringIO()
    env = {"MUSUBI_API_URL": "http://127.0.0.1:9", "MUSUBI_TOKEN": TOKEN}
    with (
        patch.dict("os.environ", env, clear=True),
        patch.object(memory_data, "request_json", side_effect=ZeroDivisionError(f"Bearer {TOKEN}")),
        redirect_stdout(io.StringIO()),
        redirect_stderr(err),
    ):
        code = memory_data.main(["--json", "musubi", "status"])
    assert code == 2 and err.getvalue().strip() == "error: unexpected ZeroDivisionError"


def urllib_error() -> Any:
    import urllib.error

    return urllib.error
