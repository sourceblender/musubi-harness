"""OpenCode retains its own provenance through capture and explicit remember."""

from musubi_harness.core import TurnEnvelope
from musubi_harness.plugin_mcp import PluginMcpFacade
from musubi_harness.plugin_runtime import PluginRuntime, RuntimeConfig


def test_opencode_source_is_valid_for_capture_and_explicit_remember(tmp_path):
    envelope = TurnEnvelope.from_mapping(
        {
            "event_id": "opencode:session:turn",
            "actor": "iris",
            "presence": "iris/agent",
            "plane": "episodic",
            "context": "primary",
            "source": "opencode",
            "zone": "home",
            "user_text": "Remember the decision",
            "assistant_text": "The decision was recorded",
            "captured_at": "2026-09-29T12:00:00Z",
            "metadata": {},
        }
    )
    assert envelope.source == "opencode"
    facade = PluginMcpFacade(
        PluginRuntime("musubi-opencode", default_data_root=tmp_path),
        source="opencode",
        event_prefix="opencode",
        owner_label="opencode-mcp",
        server_name="musubi-opencode",
    )
    command, content, event_id = facade.remember_command(
        RuntimeConfig(actor="iris", presence="iris/agent", zone="home"),
        {"content": "A durable decision", "idempotency_key": "opencode-source-test"},
    )
    assert command[command.index("--source") + 1] == "opencode"
    assert content == "A durable decision"
    assert event_id.startswith("opencode:remember:")
