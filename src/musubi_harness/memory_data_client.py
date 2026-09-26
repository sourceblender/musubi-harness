"""Fail-closed subprocess transport from the shared drainer to ``memory-data``."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from typing import Any

from .delivery import (
    DeliveryNonMutatingRejection,
    DeliveryTerminalError,
    DeliveryTransientError,
    Readback,
    ReceiptLookup,
)
from .resolution import LiveReceiptObservation

Runner = Callable[..., subprocess.CompletedProcess[bytes]]


class MemoryDataClient:
    """Use the canonical fleet client without exposing Musubi credentials to adapters."""

    def __init__(
        self,
        binary: str = "memory-data",
        *,
        timeout: float = 5.0,
        environment: Mapping[str, str] | None = None,
        runner: Runner = subprocess.run,
    ) -> None:
        if not binary or timeout <= 0:
            raise ValueError("memory-data binary and positive timeout are required")
        self.binary = binary
        self.timeout = timeout
        self.environment = dict(environment) if environment is not None else os.environ.copy()
        self.runner = runner

    def _invoke(self, arguments: list[str], *, body: bytes | None = None) -> dict[str, Any]:
        command = [
            self.binary,
            "--json",
            "--timeout",
            str(self.timeout),
            "musubi",
            *arguments,
        ]
        try:
            completed = self.runner(
                command,
                input=body,
                capture_output=True,
                check=False,
                timeout=self.timeout + 1.0,
                env=self.environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DeliveryTransientError("memory_data_unavailable") from exc
        if completed.returncode != 0:
            raise DeliveryTransientError("memory_data_request_failed")
        try:
            payload = json.loads(completed.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DeliveryTerminalError("memory_data_response_invalid") from exc
        if not isinstance(payload, dict):
            raise DeliveryTerminalError("memory_data_response_invalid")
        return payload

    def lookup_receipt(
        self,
        *,
        method: str,
        operation_id: str,
        idempotency_key: str,
        namespace: str,
        request_digest: str,
    ) -> ReceiptLookup:
        if method != "POST":
            raise DeliveryTerminalError("receipt_lookup_method_invalid")
        payload = self._invoke(
            [
                "receipt-lookup",
                "--namespace",
                namespace,
                "--idempotency-key",
                idempotency_key,
                "--request-digest",
                request_digest,
                "--operation-id",
                operation_id,
            ]
        )
        status = payload.get("status")
        # `unknown` is a real ledger state: the receipt exists but is not settled.
        # Delivery retries that case. Rejecting it here dead-letters the row
        # before the drainer can see the status.
        if status not in {"found", "absent", "conflict", "in_flight", "unknown"}:
            raise DeliveryTerminalError("receipt_lookup_shape_invalid")
        if status != "found":
            observation = None
            if status == "absent":
                try:
                    observation = LiveReceiptObservation.from_mapping(payload.get("receipt_observation"))
                except Exception as exc:
                    raise DeliveryTerminalError("receipt_lookup_shape_invalid") from exc
                if (
                    observation.namespace != namespace
                    or observation.operation_id != operation_id
                    or observation.request_digest != request_digest
                ):
                    raise DeliveryTerminalError("receipt_lookup_shape_invalid")
            return ReceiptLookup(status, observation=observation)
        object_id = payload.get("object_id")
        if (
            not isinstance(object_id, str)
            or not object_id
            or payload.get("namespace") != namespace
            or payload.get("operation_id") != operation_id
            or not isinstance(payload.get("response_status"), int)
            or not 200 <= payload["response_status"] < 300
            or not isinstance(payload.get("response_sha256"), str)
        ):
            raise DeliveryTerminalError("receipt_lookup_shape_invalid")
        return ReceiptLookup("found", object_id)

    def capture_durable(
        self,
        *,
        idempotency_key: str,
        body: bytes,
        content_type: str,
    ) -> str:
        if content_type != "application/json":
            raise DeliveryTerminalError("capture_content_type_invalid")
        payload = self._invoke(
            ["capture-durable", "--idempotency-key", idempotency_key, "--stdin"],
            body=body,
        )
        terminal = payload.get("terminal_rejection")
        if isinstance(terminal, dict):
            required = {
                "response_status",
                "error_code",
                "response_detail",
                "content_bytes_server",
                "limit_bytes",
                "observed_at",
            }
            if set(terminal) != required:
                raise DeliveryTransientError("capture_terminal_rejection_invalid")
            try:
                raise DeliveryNonMutatingRejection(
                    response_status=terminal["response_status"],
                    error_code=terminal["error_code"],
                    response_detail=terminal["response_detail"],
                    content_bytes_server=terminal["content_bytes_server"],
                    limit_bytes=terminal["limit_bytes"],
                    observed_at=terminal["observed_at"],
                )
            except (TypeError, ValueError) as exc:
                raise DeliveryTransientError("capture_terminal_rejection_invalid") from exc
        object_id = payload.get("object_id")
        if not isinstance(object_id, str) or not object_id:
            raise DeliveryTerminalError("capture_response_invalid")
        return object_id

    def get(self, *, namespace: str, object_id: str) -> Readback:
        payload = self._invoke(
            [
                "get",
                "--plane",
                "episodic",
                "--namespace",
                namespace,
                "--object-id",
                object_id,
            ]
        )
        if not all(isinstance(payload.get(key), str) for key in ("object_id", "namespace", "content")):
            raise DeliveryTerminalError("readback_shape_invalid")
        return Readback(
            object_id=payload["object_id"],
            namespace=payload["namespace"],
            content=payload["content"],
        )


__all__ = ["MemoryDataClient"]
