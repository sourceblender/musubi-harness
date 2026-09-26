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
from dataclasses import dataclass
from pathlib import Path


class RuntimeConfigError(ValueError):
    """The plugin cannot prove its deployment identity or canonical tools."""


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
        raise RuntimeConfigError("memory_data_unavailable")

    @staticmethod
    def tool_environment(config: RuntimeConfig) -> dict[str, str]:
        env = os.environ.copy()
        env["FLEET_IDENTITY"] = config.actor
        env["FLEET_PRESENCE"] = config.seat
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
