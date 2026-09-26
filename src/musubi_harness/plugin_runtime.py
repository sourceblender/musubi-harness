"""Shared deployment runtime for installable Musubi harness adapters.

Adapters own lifecycle parsing and their deployment data-root name.  Identity,
namespace, delivery-mode, and canonical fleet-tool resolution live here so two
plugins cannot silently evolve different memory boundaries.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


class RuntimeConfigError(ValueError):
    """The plugin cannot prove its deployment identity or canonical tools."""


BUNDLED_MEMORY_DATA = "musubi-memory-data"
# The endpoint and credential that memory-data (bundled or operator) reads from
# its environment. Only processes that talk to Musubi should receive them.
TRANSPORT_ENV = ("MUSUBI_API_URL", "MUSUBI_TOKEN")

SEGMENT = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


@dataclass(frozen=True)
class RuntimeConfig:
    actor: str
    presence: str
    zone: str
    harness_bin: str | None = None
    memory_data_bin: str | None = None
    delivery_mode: str = "shadow"

    @property
    def seat(self) -> str:
        return self.presence.split("/", 1)[1]

    @property
    def presence_root(self) -> str:
        return self.presence

    @property
    def episodic_namespace(self) -> str:
        return f"{self.presence}/episodic"


class PluginRuntime:
    """One parameterized runtime contract shared by every harness adapter."""

    def __init__(
        self,
        state_name: str,
        *,
        development_root: Path | None = None,
        default_data_root: Path | None = None,
    ):
        if not SEGMENT.fullmatch(state_name):
            raise ValueError("state_name_invalid")
        self.state_name = state_name
        self.development_root = development_root
        self.default_data_root = default_data_root

    def data_root(self) -> Path:
        raw = os.environ.get("PLUGIN_DATA")
        if raw:
            return Path(raw).expanduser()
        if self.default_data_root is not None:
            return self.default_data_root.expanduser()
        return Path.home() / ".local" / "state" / self.state_name

    def plugin_config(self) -> dict[str, str]:
        path = self.data_root() / "config.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeConfigError("identity_config_invalid") from exc
        allowed = {
            "actor",
            "presence",
            "zone",
            "harness_bin",
            "memory_data_bin",
            "delivery_mode",
        }
        if not isinstance(raw, dict) or not set(raw).issubset(allowed):
            raise RuntimeConfigError("identity_config_invalid")
        if not all(isinstance(value, str) and value for value in raw.values()):
            raise RuntimeConfigError("identity_config_invalid")
        identity_keys = {"actor", "presence", "zone"}
        if set(raw).intersection(identity_keys) and not identity_keys.issubset(raw):
            raise RuntimeConfigError("identity_config_invalid")
        return raw

    def runtime_config(self) -> RuntimeConfig:
        names = ("MUSUBI_ACTOR", "MUSUBI_PRESENCE", "MUSUBI_ZONE")
        values = tuple(os.environ.get(name, "") for name in names)
        if all(values):
            # A seat whose launcher supplies its whole identity must not be blocked
            # by a shared config.json it does not use for identity (several seats
            # can share one OS user). A broken shared file counts as absent here;
            # it stays strict when identity comes from it. (Tama's review.)
            try:
                raw = self.plugin_config()
            except RuntimeConfigError:
                raw = {}
        else:
            raw = self.plugin_config()
        if all(values):
            actor, presence, zone = values
        elif any(values):
            raise RuntimeConfigError("partial_identity_config_refused")
        else:
            try:
                actor, presence, zone = (raw["actor"], raw["presence"], raw["zone"])
            except KeyError as exc:
                raise RuntimeConfigError("explicit_identity_config_missing") from exc
        presence_parts = presence.split("/")
        if len(presence_parts) != 2 or presence_parts[0] != actor or not all(SEGMENT.fullmatch(part) for part in (actor, *presence_parts)):
            raise RuntimeConfigError("identity_config_invalid")
        if zone not in {"home", "work"}:
            raise RuntimeConfigError("identity_config_invalid")
        delivery_mode = os.environ.get("MUSUBI_DELIVERY_MODE") or raw.get("delivery_mode", "shadow")
        if delivery_mode not in {"shadow", "verified"}:
            raise RuntimeConfigError("delivery_mode_invalid")
        return RuntimeConfig(
            actor=actor,
            presence=presence,
            zone=zone,
            harness_bin=raw.get("harness_bin"),
            memory_data_bin=raw.get("memory_data_bin"),
            delivery_mode=delivery_mode,
        )

    def harness_bin(self, config: RuntimeConfig | None = None) -> str:
        config = config or self.runtime_config()
        configured = os.environ.get("MUSUBI_HARNESS_BIN") or config.harness_bin
        if configured:
            return configured
        installed = shutil.which("musubi-harness")
        if installed:
            return installed
        if self.development_root is not None:
            development = self.development_root / "bin" / "musubi-harness"
            if development.is_file():
                return str(development)
        raise RuntimeConfigError("musubi_harness_unavailable")

    def memory_data_bin(self, config: RuntimeConfig | None = None) -> str:
        config = config or self.runtime_config()
        configured = os.environ.get("MUSUBI_MEMORY_DATA_BIN") or config.memory_data_bin
        if configured:
            return configured
        installed = shutil.which("memory-data")
        if installed:
            return installed
        try:
            sibling = Path(self.harness_bin(config)).with_name("memory-data")
        except RuntimeConfigError:
            sibling = Path("/__missing__")
        if sibling.is_file():
            return str(sibling)
        if self.development_root is not None:
            development = self.development_root / "bin" / "memory-data"
            if development.is_file():
                return str(development)
        # Last resort, and the only one available outside an operator install:
        # the public HTTP client this package ships (musubi-memory-data). It
        # speaks the same argv and JSON, so everything above still wins where
        # an operator memory-data exists.
        bundled = shutil.which(BUNDLED_MEMORY_DATA)
        if bundled:
            return bundled
        for directory in (sibling.parent, Path(sys.executable).parent):
            candidate = directory / BUNDLED_MEMORY_DATA
            if candidate.is_file():
                return str(candidate)
        raise RuntimeConfigError("memory_data_unavailable")

    @staticmethod
    def tool_environment(config: RuntimeConfig) -> dict[str, str]:
        env = os.environ.copy()
        env["FLEET_IDENTITY"] = config.actor
        env["FLEET_PRESENCE"] = config.seat
        return env

    def local_tool_environment(self, config: RuntimeConfig) -> dict[str, str]:
        """The child environment for a subprocess that never contacts Musubi.

        Same as ``tool_environment`` minus the transport credentials
        (``TRANSPORT_ENV``). Local outbox commands (``remember``, ``enqueue``,
        ``stage``, ``status``, ``inspect``, ``delivery-status``) write or read
        only the local SQLite outbox, so they have no use for the token, even
        in shadow mode. Only the drain and memory-data reads get it.
        """
        env = self.tool_environment(config)
        for key in TRANSPORT_ENV:
            env.pop(key, None)
        return env

    @staticmethod
    def require_owned_namespace(config: RuntimeConfig, namespace: str, *, plane: str | None = None) -> str:
        if not isinstance(namespace, str) or not namespace.strip():
            raise RuntimeConfigError("namespace_required")
        normalized = namespace.strip().rstrip("/")
        parts = normalized.split("/")
        if len(parts) not in {2, 3} or parts[0] != config.actor or not all(SEGMENT.fullmatch(part) for part in parts):
            raise RuntimeConfigError("namespace_outside_actor_boundary")
        if len(parts) == 3 and parts[2] not in {
            "episodic",
            "curated",
            "concept",
            "artifact",
        }:
            raise RuntimeConfigError("namespace_plane_invalid")
        if plane and len(parts) == 3 and parts[2] != plane:
            raise RuntimeConfigError("namespace_plane_mismatch")
        if plane and len(parts) == 2:
            return f"{normalized}/{plane}"
        return normalized
