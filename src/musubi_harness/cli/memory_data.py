"""``musubi-memory-data``: a direct HTTP client for the Musubi operations the harness uses.

The harness drives every Musubi call through a ``memory-data`` subprocess
(``<bin> --json --timeout N musubi <command> ...``) and parses its JSON. Until
1.1.0 the only implementation was a private operator tool, so a plugin installed
anywhere else could not capture or recall. This module is a public, stdlib-only
implementation of exactly the subset the harness calls, with the same argv and
the same JSON on stdout, so the capture -> outbox -> drainer -> receipt ->
readback contract is unchanged:

    status | recent | search | get | capture-durable | receipt-lookup

It also carries the operator's seat-scoped correction verbs, ported from the
operator tool with the same argv, request bodies and JSON so they can be used
without it:

    remember [--verify] | patch | retract | archive (alias: delete)

These are owner actions on the owner's own rows, authorized by the seat's own
token. Nothing here needs or accepts operator scope: ``delete --hard`` and
lifecycle transitions stay with operator tooling, and are refused locally.

Endpoint and credential come from the process environment the harness passes
to its child (``MUSUBI_API_URL``, ``MUSUBI_TOKEN``). Plugins fill those from
their own settings; users are not asked to export them.

``MUSUBI_TOKEN`` must be a JWT carrying ``iss``, ``sub``, ``presence`` and
``scope``. Every command sends it as the bearer, but ``receipt-lookup`` also
decodes (without verifying) those claims to self-attest who observed the
receipt, and exits 2 on an opaque token. Musubi issues JWTs, so this is only a
constraint on hand-made tokens.

Differences from the operator tool, all deliberately stricter:
- every write takes an explicit ``--namespace``; the operator tool's
  identity/cwd resolution is not carried over;
- output is always JSON, including ``remember`` without ``--json``;
- a ``retract`` POST answered with 5xx is reported as ambiguous with the
  local replay values, the same as a dropped connection;
- redirects are refused, so the bearer token is never sent to another URL;
- responses are capped at ``MAX_RESPONSE_BYTES``;
- only ``http``/``https`` URLs without credentials, query or fragment are used.

- stderr carries only locally-generated text: HTTP status, method, path, a
  well-formed server error code, and the OS's own socket errors. Response
  bodies and server-supplied exception text are never printed, because a
  server or proxy can echo the bearer token in them in any encoding.

Errors print ``error: <message>`` on stderr and exit 2, like the operator tool.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
RECALL_STATES = ["provisional", "matured", "promoted"]
SETTLED_STATES = ["matured", "promoted"]
GET_PLANES = {"episodic", "curated", "concepts", "artifacts"}
RECEIPT_OPERATION = "capture_episodic.bucket=capture"
RETRACT_TAGS = ("retracted", "false", "do-not-act-on")
REMEMBER_SOURCE_TAG = "src:memory-data-remember"

# Every key the server models on an episodic row. MusubiObject sets
# extra="forbid", so a write carrying any other key makes every later GET of
# that row return 500, and nothing exposes a payload-key delete: the row is
# unreadable for good. Bodies here are built from known arguments; this guard
# keeps a future edit from being one typo away from that.
MODEL_PAYLOAD_KEYS = frozenset(
    {
        "access_count",
        "content",
        "contradicts",
        "created_at",
        "created_epoch",
        "derived_from",
        "event_at",
        "identity_family",
        "importance",
        "importance_last_scored_at",
        "ingested_at",
        "last_accessed_at",
        "linked_to_topics",
        "merged_from",
        "modality",
        "namespace",
        "object_id",
        "participants",
        "reinforcement_count",
        "schema_version",
        "source_context",
        "state",
        "summary",
        "superseded_by",
        "supersedes",
        "supported_by",
        "tags",
        "topics",
        "updated_at",
        "updated_epoch",
        "valid_from",
        "valid_from_epoch",
        "valid_until",
        "valid_until_epoch",
        "version",
    }
)
# The server refuses these on PATCH (writes_episodic._FORBIDDEN_PATCH_FIELDS).
PATCH_REFUSED_KEYS = frozenset({"state", "version", "object_id", "namespace"})


class CliError(RuntimeError):
    """Expected failure with a user-facing message."""


_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class MusubiHTTPError(CliError):
    """An HTTP error whose message carries only locally-generated text.

    The response body is untrusted: a server or proxy can echo the request's
    Authorization header in it, in any encoding (raw, JSON \\u escapes, percent
    encoding). So the body is never printed. It is kept in ``payload`` for the
    one strict parse that needs it (CONTENT_TOO_LARGE), and the message adds
    the server's error code only when it has the shape of a code.
    """

    def __init__(self, status_code: int, method: str, path: str, body: str) -> None:
        self.status_code = status_code
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError:
            decoded = None
        self.payload = decoded if isinstance(decoded, dict) else None
        error = self.payload.get("error") if self.payload is not None else None
        code = error.get("code") if isinstance(error, dict) else None
        suffix = f" ({code})" if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else ""
        super().__init__(f"Musubi HTTP {status_code} {method.upper()} {path}{suffix}")


def _local_reason(exc: BaseException) -> str:
    """Describe a transport failure using only text this machine derives.

    Exception messages can carry server-controlled text (http.client quotes a
    malformed status line verbatim), and even ``OSError.strerror`` is just a
    constructor argument, so no text held by the exception is printed. Only
    the class name and the numeric errno are used; the message is looked up
    locally from that number (Tama's review).
    """
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if not isinstance(reason, BaseException):
        return type(exc).__name__
    name = type(reason).__name__
    number = reason.errno if isinstance(reason, OSError) else None
    # errno is caller-supplied too: os.strerror(10**100) raises OverflowError.
    # Real errno and EAI codes are small; anything else is reported by name.
    if not isinstance(number, int) or isinstance(number, bool) or not 0 < abs(number) < 4096:
        return name
    if isinstance(reason, socket.gaierror):
        # getaddrinfo codes (EAI_*) are not errno values; os.strerror would lie.
        return f"{name} {number}: name resolution failed"
    try:
        message = os.strerror(number)
    except (ValueError, OverflowError):
        return f"{name} errno {number}"
    return f"{name} errno {number}: {message}"


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise CliError(
            f"Musubi answered {code} with a redirect; refusing to follow it so the token "
            "is not sent anywhere else. Set the Musubi URL to the final address."
        )


_OPENER = urllib.request.build_opener(_RefuseRedirects)


def base_url() -> str:
    raw = os.environ.get("MUSUBI_API_URL", "").strip().rstrip("/")
    if not raw:
        raise CliError("MUSUBI_API_URL is not configured")
    invalid = "MUSUBI_API_URL must be an http(s) URL without credentials, query or fragment"
    try:
        # urlsplit, .hostname and .port each raise ValueError on malformed input
        # (an unclosed IPv6 bracket, a non-IP inside brackets, a bad port).
        # That is bad config, so it exits 2 before any network like the rest.
        parts = urllib.parse.urlsplit(raw)
        hostname, _port = parts.hostname, parts.port
    except ValueError as exc:
        raise CliError(f"{invalid}: {exc}") from exc
    if (
        parts.scheme not in ("http", "https")
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise CliError(invalid)
    return raw if raw.endswith("/v1") else f"{raw}/v1"


# RFC 6750 b64token. A JWT always fits; anything else (a newline, a space,
# non-ASCII) would reach http.client, whose error echoes the header value.
_BEARER = re.compile(r"[A-Za-z0-9\-._~+/]+=*")


def token() -> str:
    value = os.environ.get("MUSUBI_TOKEN", "").strip()
    if not value:
        raise CliError("MUSUBI_TOKEN is not configured")
    if not _BEARER.fullmatch(value):
        # Never include the value: this message goes to stderr and into logs.
        # (Also keeps a malformed token from reaching http.client at all.)
        raise CliError("MUSUBI_TOKEN contains characters a bearer token cannot have")
    return value


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def self_attested_token_claims(bearer: str) -> dict[str, object]:
    """Decode (without verifying) the claims of the token the server just authorized."""
    parts = bearer.split(".")
    if len(parts) != 3:
        raise CliError("Musubi token is not a JWT for receipt self-attestation")
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError("Musubi token claims cannot be decoded for receipt self-attestation") from exc
    if not isinstance(payload, dict):
        raise CliError("Musubi token claims are not an object")
    issuer, subject, presence = payload.get("iss"), payload.get("sub"), payload.get("presence")
    scopes = payload.get("scope")
    if isinstance(scopes, str):
        effective = [part for part in scopes.split() if part]
    elif isinstance(scopes, list) and all(isinstance(item, str) for item in scopes):
        effective = scopes
    else:
        effective = []
    if not all(isinstance(value, str) and value for value in (issuer, subject, presence)):
        raise CliError("Musubi token lacks receipt observer identity claims")
    if not effective:
        raise CliError("Musubi token lacks receipt observer scopes")
    return {
        "attestation": "self_attested",
        "issuer": issuer,
        "subject": subject,
        "presence": presence,
        "effective_scopes": list(dict.fromkeys(effective)),
    }


def _send(
    method: str,
    path: str,
    *,
    body: bytes | None,
    content_type: str | None,
    query: dict[str, str] | None,
    extra_headers: dict[str, str] | None,
    timeout: float,
    bearer: str | None = None,
) -> dict[str, Any]:
    url = f"{base_url()}/{path.lstrip('/')}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"Accept": "application/json", "Authorization": f"Bearer {bearer or token()}"}
    if content_type:
        headers["Content-Type"] = content_type
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        body_text = exc.read(64 * 1024).decode("utf-8", errors="replace")
        raise MusubiHTTPError(exc.code, method, path, body_text) from exc
    except CliError:
        raise
    except Exception as exc:  # noqa: BLE001 - network failures become one clear message
        raise CliError(f"Musubi request failed {method.upper()} {path}: {_local_reason(exc)}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CliError(f"Musubi response for {method.upper()} {path} exceeds {MAX_RESPONSE_BYTES} bytes")
    if not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CliError(f"Musubi returned non-JSON for {method.upper()} {path}") from exc
    if not isinstance(decoded, dict):
        raise CliError(f"Musubi returned non-object JSON for {method.upper()} {path}")
    return decoded


def request_json(
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    bearer: str | None = None,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    return _send(
        method,
        path,
        body=data,
        content_type="application/json" if data is not None else None,
        query=query,
        extra_headers=extra_headers,
        timeout=timeout,
        bearer=bearer,
    )


def print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def parse_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _namespace(args: argparse.Namespace) -> str:
    # The harness always passes an explicit, owned namespace with --exact.
    if not args.namespace:
        raise CliError("--namespace is required")
    return str(args.namespace)


def cmd_status(args: argparse.Namespace) -> int:
    print_json(request_json("GET", "/ops/status", timeout=args.timeout))
    return 0


def cmd_recent(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {"namespace": _namespace(args), "mode": "recent", "limit": args.limit}
    tags = parse_csv(args.tags)
    if tags:
        body["tags"] = tags
    print_json(request_json("POST", "/retrieve", body=body, timeout=args.timeout))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "namespace": _namespace(args),
        "query_text": args.query,
        "mode": args.mode,
        "limit": args.limit,
        "state_filter": SETTLED_STATES if args.settled_only else RECALL_STATES,
    }
    planes = parse_csv(args.planes)
    if planes:
        body["planes"] = planes
    print_json(request_json("POST", "/retrieve", body=body, timeout=args.timeout))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    if args.plane not in GET_PLANES:
        raise CliError(f"unsupported plane for get: {args.plane}")
    payload = request_json(
        "GET",
        f"/{args.plane}/{urllib.parse.quote(args.object_id, safe='')}",
        query={"namespace": args.namespace},
        timeout=args.timeout,
    )
    print_json(payload)
    return 0


def cmd_capture_durable(args: argparse.Namespace) -> int:
    if bool(args.request_file) == bool(args.stdin):
        raise CliError("choose exactly one of --request-file or --stdin")
    if not 1 <= len(args.idempotency_key) <= 256:
        raise CliError("idempotency key must contain 1 to 256 characters")
    try:
        if args.request_file:
            with open(args.request_file, "rb") as handle:
                body = handle.read()
        else:
            body = sys.stdin.buffer.read()
    except OSError as exc:
        raise CliError(f"cannot read durable capture body: {exc}") from exc
    try:
        decoded = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CliError("durable capture body must be a UTF-8 JSON object") from exc
    if not isinstance(decoded, dict) or not isinstance(decoded.get("namespace"), str):
        raise CliError("durable capture body must include namespace")
    if not body:
        raise CliError("durable capture body is empty")
    try:
        # Exact caller bytes: the receipt digest binds Content-Type and body.
        payload = _send(
            "POST",
            "/episodic",
            body=body,
            content_type="application/json",
            query=None,
            extra_headers={"Idempotency-Key": args.idempotency_key, "Idempotency-Receipt": "durable"},
            timeout=args.timeout,
        )
    except MusubiHTTPError as exc:
        error = exc.payload.get("error") if exc.payload is not None else None
        if (
            exc.status_code != 422
            or not isinstance(error, dict)
            or error.get("code") != "CONTENT_TOO_LARGE"
            or not isinstance(error.get("detail"), str)
        ):
            raise
        match = re.fullmatch(r"episodic content is ([1-9][0-9]*) UTF-8 bytes; the limit is ([1-9][0-9]*)", error["detail"])
        if match is None:
            raise CliError("CONTENT_TOO_LARGE response detail is not canonical") from exc
        content_bytes, limit_bytes = int(match.group(1)), int(match.group(2))
        if content_bytes <= limit_bytes:
            raise CliError("CONTENT_TOO_LARGE response byte counts are inconsistent") from exc
        print_json(
            {
                "terminal_rejection": {
                    "response_status": exc.status_code,
                    "error_code": error["code"],
                    "response_detail": error["detail"],
                    "content_bytes_server": content_bytes,
                    "limit_bytes": limit_bytes,
                    "observed_at": utc_timestamp(),
                }
            }
        )
        return 0
    object_id = payload.get("object_id")
    if not isinstance(object_id, str) or not object_id:
        raise CliError("Musubi durable capture response did not include object_id")
    print_json(payload)
    return 0


def cmd_receipt_lookup(args: argparse.Namespace) -> int:
    if not args.namespace.strip():
        raise CliError("namespace is required")
    if not 1 <= len(args.idempotency_key) <= 256:
        raise CliError("idempotency key must contain 1 to 256 characters")
    if re.fullmatch(r"[0-9a-fA-F]{64}", args.request_digest) is None:
        raise CliError("request digest must be exactly 64 ASCII hexadecimal characters")
    bearer = token()
    payload = request_json(
        "POST",
        "/idempotency/receipts/lookup",
        body={
            "namespace": args.namespace,
            "method": "POST",
            "operation_id": args.operation_id,
            "idempotency_key": args.idempotency_key,
            "request_digest": args.request_digest.lower(),
        },
        timeout=args.timeout,
        bearer=bearer,
    )
    payload["receipt_observation"] = {
        "status": payload.get("status"),
        **self_attested_token_claims(bearer),
        "observed_at": utc_timestamp(),
        "namespace": args.namespace,
        "operation_id": args.operation_id,
        "request_digest": args.request_digest.lower(),
    }
    print_json(payload)
    return 0


def _write_namespace(args: argparse.Namespace) -> str:
    if not args.namespace or not str(args.namespace).strip():
        raise CliError("--namespace is required for writes (this client does not resolve identity)")
    return str(args.namespace)


def _object_path(object_id: str) -> str:
    return f"/episodic/{urllib.parse.quote(object_id, safe='')}"


def _read_text(args: argparse.Namespace, *, strip: bool = False) -> str:
    """Exactly one of --content, --content-file or --stdin. A file keeps its bytes."""
    if sum(1 for item in (bool(args.content), bool(args.content_file), bool(args.stdin)) if item) > 1:
        raise CliError("choose only one of --content, --content-file, or --stdin")
    if args.content_file:
        try:
            with open(args.content_file, "rb") as handle:
                content = handle.read().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CliError(f"cannot read content file {args.content_file}: {type(exc).__name__}") from exc
        strip = False  # file content is exact, including leading/trailing whitespace and CRLF
    elif args.stdin:
        content = sys.stdin.read()
    else:
        content = args.content or ""
    if strip:
        content = content.strip()
    if not content:
        raise CliError("content is required")
    return content


def assert_writable_payload(body: dict[str, Any]) -> None:
    """Refuse before the wire: an unmodeled key makes the row unreadable for good."""
    unmodeled = set(body) - MODEL_PAYLOAD_KEYS
    if unmodeled:
        raise CliError(
            f"refusing to write unmodeled payload key(s): {sorted(unmodeled)}; "
            "Musubi forbids extra keys and the row would fail every later GET"
        )
    refused = set(body) & PATCH_REFUSED_KEYS
    if refused:
        raise CliError(
            f"the server refuses these on PATCH: {sorted(refused)}; state changes are lifecycle transitions and need operator tooling"
        )


def cmd_remember(args: argparse.Namespace) -> int:
    """Direct, synchronous episodic write, optionally read back by id.

    ``--verify`` means one GET of the returned object id succeeded and its body
    is included as ``readback``. It does not compare namespace or content; it
    carries the operator tool's meaning exactly. This is not the harness's
    queued ``remember`` (outbox, drain, receipt), which is a different contract.
    """
    namespace = _write_namespace(args)
    content = _read_text(args, strip=True)
    tags = parse_csv(args.tags) or []
    if REMEMBER_SOURCE_TAG not in tags:
        tags.append(REMEMBER_SOURCE_TAG)
    body: dict[str, Any] = {"namespace": namespace, "content": content, "tags": tags, "importance": args.importance}
    if args.summary:
        body["summary"] = args.summary
    if args.dry_run:
        print_json({"dry_run": True, "method": "POST", "path": "/episodic", "body": body})
        return 0
    headers = {"Idempotency-Key": args.idempotency_key} if args.idempotency_key else None
    payload = request_json("POST", "/episodic", body=body, extra_headers=headers, timeout=args.timeout)
    object_id = str(payload.get("object_id") or "")
    if not object_id:
        raise CliError("Musubi capture response did not include object_id")
    out: dict[str, Any] = {
        "plane": "episodic",
        "namespace": namespace,
        "object_id": object_id,
        "state": payload.get("state"),
        "dedup": payload.get("dedup"),
        "tags": tags,
        "verified": False,
    }
    if args.verify:
        out["readback"] = request_json("GET", _object_path(object_id), query={"namespace": namespace}, timeout=args.timeout)
        out["verified"] = True
    print_json(out)
    return 0


def cmd_patch(args: argparse.Namespace) -> int:
    namespace = _write_namespace(args)
    body: dict[str, Any] = {}
    if args.content or args.content_file or args.stdin:
        body["content"] = _read_text(args, strip=True)
    if args.summary is not None:
        body["summary"] = args.summary
    if args.tags is not None:
        body["tags"] = parse_csv(args.tags) or []
    if args.importance is not None:
        body["importance"] = args.importance
    if not body:
        raise CliError("nothing to patch: pass --content/--content-file/--stdin, --summary, --tags, or --importance")
    assert_writable_payload(body)
    if args.dry_run:
        print_json({"PATCH": f"/episodic/{args.object_id}", "namespace": namespace, "body": body})
        return 0
    print_json(request_json("PATCH", _object_path(args.object_id), query={"namespace": namespace}, body=body, timeout=args.timeout))
    return 0


def retraction_idempotency_key(object_id: str, body: dict[str, Any]) -> str:
    """Bind a stable retry identity to the exact intended retraction."""
    canonical = json.dumps({"object_id": object_id, "body": body}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"memory-data-retract-{hashlib.sha256(canonical).hexdigest()}"


def cmd_retract(args: argparse.Namespace) -> int:
    """Escrow a false memory and replace it with a bounded server tombstone.

    Musubi owns exact-byte escrow, evidence and the non-reembedding mutation;
    this client supplies the caller's truth and a canonical observed version,
    fenced so a retraction never applies to a row that changed under it.
    """
    namespace = _write_namespace(args)
    current = request_json("GET", _object_path(args.object_id), query={"namespace": namespace}, timeout=args.timeout)
    expected_version = args.expected_version if args.expected_version is not None else current.get("version")
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
        raise CliError("Musubi GET did not return a canonical integer version; refusing an unfenced retraction")
    truth = _read_text(args, strip=True)
    tags = list(RETRACT_TAGS)
    if args.superseded_by:
        tags.append(f"superseded-by:{args.superseded_by}")
    for extra in parse_csv(args.tags) or []:
        if extra not in tags:
            tags.append(extra)
    body = {
        "namespace": namespace,
        "expected_version": expected_version,
        "on": args.on,
        "because": args.because,
        "truth": truth,
        "summary": args.summary,
        "tags": tags,
    }
    key = args.idempotency_key or retraction_idempotency_key(args.object_id, body)
    if not 1 <= len(key) <= 256:
        raise CliError("--idempotency-key must contain 1 to 256 characters")
    path = f"{_object_path(args.object_id)}/retract"
    if args.dry_run:
        print_json(
            {
                "dry_run": True,
                "status": "proposed_request_only",
                "note": "No escrow or mutation was attempted.",
                "method": "POST",
                "path": path,
                "headers": {"Idempotency-Key": key},
                "body": body,
            }
        )
        return 0
    try:
        payload = request_json("POST", path, body=body, extra_headers={"Idempotency-Key": key}, timeout=args.timeout)
    except MusubiHTTPError as exc:
        # A 5xx can come from a proxy after Musubi committed, so it is as
        # ambiguous as a dropped connection (Yua's review). 4xx is a refusal.
        if exc.status_code < 500:
            raise
        raise CliError(
            f"{exc}; retraction outcome may be ambiguous, do not blind-retry. Replay the exact "
            f"server-owned operation with --expected-version {expected_version} --idempotency-key {key}"
        ) from exc
    except CliError as exc:
        # Transport failed after the request may have landed. Local values only.
        raise CliError(
            f"{exc}; retraction outcome may be ambiguous, do not blind-retry. Replay the exact "
            f"server-owned operation with --expected-version {expected_version} --idempotency-key {key}"
        ) from exc
    print_json(payload)
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    """Soft delete: state -> archived through the server's lifecycle transition."""
    if getattr(args, "hard", False):
        raise CliError(
            "hard delete drops the point permanently and needs operator scope; it is not "
            "part of the seat client. Use operator tooling, or retract to correct a false row."
        )
    namespace = _write_namespace(args)
    try:
        payload = request_json("DELETE", _object_path(args.object_id), query={"namespace": namespace}, timeout=args.timeout)
    except MusubiHTTPError as exc:
        error = exc.payload.get("error") if exc.payload is not None else None
        detail = error.get("detail") if isinstance(error, dict) else None
        if exc.status_code == 400 and isinstance(detail, str) and detail.startswith("delete transition rejected:"):
            raise CliError(
                f"{exc}: the lifecycle refuses archiving from this row's current state. "
                "Other moves are operator-only lifecycle transitions; to correct a false row, use retract."
            ) from exc
        raise
    print_json(payload or {"status": "deleted", "object_id": args.object_id})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="musubi-memory-data", description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="accepted for compatibility; output is always JSON")
    parser.add_argument("--timeout", type=float, default=10.0, help="Musubi HTTP timeout in seconds")
    areas = parser.add_subparsers(dest="area", required=True)
    musubi = areas.add_parser("musubi", help="Musubi operations")
    sub = musubi.add_subparsers(dest="command", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)

    recent = sub.add_parser("recent")
    recent.add_argument("--namespace")
    recent.add_argument("--exact", action="store_true")
    recent.add_argument("--limit", type=int, default=5)
    recent.add_argument("--tags")
    recent.set_defaults(func=cmd_recent)

    search = sub.add_parser("search")
    search.add_argument("--namespace")
    search.add_argument("--exact", action="store_true")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--mode", default="deep", choices=["fast", "deep", "blended"])
    search.add_argument("--planes")
    search.add_argument("--settled-only", action="store_true")
    search.set_defaults(func=cmd_search)

    get = sub.add_parser("get")
    get.add_argument("--plane", required=True)
    get.add_argument("--namespace", required=True)
    get.add_argument("--object-id", required=True)
    get.set_defaults(func=cmd_get)

    capture = sub.add_parser("capture-durable")
    capture.add_argument("--idempotency-key", required=True)
    capture.add_argument("--request-file")
    capture.add_argument("--stdin", action="store_true")
    capture.set_defaults(func=cmd_capture_durable)

    lookup = sub.add_parser("receipt-lookup")
    lookup.add_argument("--namespace", required=True)
    lookup.add_argument("--idempotency-key", required=True)
    lookup.add_argument("--request-digest", required=True)
    lookup.add_argument("--operation-id", default=RECEIPT_OPERATION, choices=[RECEIPT_OPERATION])
    lookup.set_defaults(func=cmd_receipt_lookup)

    def text_source(cmd: argparse.ArgumentParser, what: str) -> None:
        cmd.add_argument("--content", help=what)
        cmd.add_argument("--content-file", help="read it from a UTF-8 file (exact bytes, no stripping)")
        cmd.add_argument("--stdin", action="store_true", help="read it from stdin")

    remember = sub.add_parser("remember", help="write an episodic memory directly; --verify reads it back by id")
    remember.add_argument("--namespace", required=True)
    text_source(remember, "memory content")
    remember.add_argument("--summary")
    remember.add_argument("--tags", help="comma-separated tags")
    remember.add_argument("--importance", type=int, default=7, choices=range(1, 11))
    remember.add_argument("--idempotency-key")
    remember.add_argument("--verify", action="store_true", help="GET the written object by id and include it")
    remember.add_argument("--dry-run", action="store_true", help="print the request without writing")
    remember.set_defaults(func=cmd_remember)

    patch_cmd = sub.add_parser("patch", help="edit content/summary/tags/importance on an episodic memory")
    patch_cmd.add_argument("--namespace", required=True)
    patch_cmd.add_argument("--object-id", required=True)
    text_source(patch_cmd, "replacement content")
    patch_cmd.add_argument("--summary")
    patch_cmd.add_argument("--tags", help="comma-separated tags (replaces the existing set)")
    patch_cmd.add_argument("--importance", type=int, choices=range(1, 11))
    patch_cmd.add_argument("--dry-run", action="store_true")
    patch_cmd.set_defaults(func=cmd_patch)

    retract = sub.add_parser("retract", help="escrow and retract a FALSE memory through Musubi's retraction saga")
    retract.add_argument("--namespace", required=True)
    retract.add_argument("--object-id", required=True)
    retract.add_argument("--on", required=True, metavar="YYYY-MM-DD", help="date of the retraction")
    text_source(retract, "the truth that replaces the falsehood")
    retract.add_argument("--because", required=True, help="decision and scope proving this row is false")
    retract.add_argument("--superseded-by", help="replacement object id, if one exists")
    retract.add_argument("--summary", help="replacement summary")
    retract.add_argument("--tags", help="extra comma-separated tags")
    retract.add_argument("--expected-version", type=int, help="exact version from a prior GET; only to replay an ambiguous request")
    retract.add_argument("--idempotency-key", help="stable retry key; default derives from object id and exact body")
    retract.add_argument("--dry-run", action="store_true", help="GET the version and print the request; no escrow or mutation")
    retract.set_defaults(func=cmd_retract)

    for name in ("archive", "delete"):
        archive = sub.add_parser(name, help="soft delete: state -> archived" + (" (alias of archive)" if name == "delete" else ""))
        archive.add_argument("--namespace", required=True)
        archive.add_argument("--object-id", required=True)
        archive.add_argument("--hard", action="store_true", help=argparse.SUPPRESS)
        archive.add_argument("--i-have-operator-scope", action="store_true", help=argparse.SUPPRESS)
        archive.set_defaults(func=cmd_archive)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - the backstop: never a traceback
        # A traceback prints every chained exception's message, and some of
        # that text is caller- or server-supplied (Tama's review). Anything
        # unexpected is reported by class name only, still exit 2.
        print(f"error: unexpected {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
