from pathlib import Path

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from rimworld_save_inspector.history.mcp import create_server
from rimworld_save_inspector.history.schemas import get_history_schema


def test_actual_schema_tool_returns_contract_without_opening_a_catalog(tmp_path):
    configured = tmp_path / "not-built.sqlite"
    result = anyio.run(create_server(configured).call_tool, "get_history_schema", {})
    assert not result.is_error
    assert result.structured_content == get_history_schema()
    assert not configured.exists()


def test_schema_tool_rejects_caller_supplied_paths(tmp_path):
    with pytest.raises(ToolError, match="Extra inputs are not permitted"):
        anyio.run(create_server(tmp_path / "not-built.sqlite").call_tool,
                  "get_history_schema", {"path": str(Path.home())})
