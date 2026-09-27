"""A Grok adapter can use the same validated capture and MCP contracts."""

from musubi_harness.core import TurnEnvelope
from musubi_harness.plugin_mcp import PluginMcpFacade
from musubi_harness.plugin_runtime import PluginRuntime


def test_grok_source_is_valid_for_capture_and_explicit_remember(tmp_path):
    envelope = TurnEnvelope.from_mapping(
        {
            "event_id": "grok:session:turn",
            "actor": "yua",
            "presence": "yua/command-chair",
            "plane": "episodic",
            "context": "primary",
            "source": "grok",
            "zone": "home",
            "user_text": "Remember the decision",
            "assistant_text": "The decision was recorded",
            "captured_at": "2026-09-27T12:00:00Z",
            "metadata": {},
        }
    )
    assert envelope.source == "grok"
    facade = PluginMcpFacade(
        PluginRuntime("musubi-grok", default_data_root=tmp_path),
        source="grok",
        event_prefix="grok",
        owner_label="grok-mcp",
        server_name="musubi-grok",
    )
    assert facade.source == "grok"
