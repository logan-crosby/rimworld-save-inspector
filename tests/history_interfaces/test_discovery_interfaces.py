"""First-pass discovery is usable without knowing a pawn or field selector."""

import json
from pathlib import Path

import anyio
import jsonschema
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from rimworld_save_inspector.cli import main
from rimworld_save_inspector.history import catalog
from rimworld_save_inspector.history.mcp import create_server
from rimworld_save_inspector.history.schemas import get_history_schema


@pytest.fixture
def discovery_catalog(tmp_path):
    saves = tmp_path / "saves"
    saves.mkdir()
    fixture = Path(__file__).parents[1] / "history_oracle" / "fixtures" / "alpha-early.xml"
    (saves / "one.rws").write_bytes(fixture.read_bytes())
    db = tmp_path / "catalog.sqlite"
    catalog.build_catalog(saves, db)
    return db


def test_first_pass_cli_and_actual_tool_share_offline_discovery(discovery_catalog, capsys):
    db = discovery_catalog
    assert main(["discover", str(db), "--freshness", "frozen"]) == 0
    cli = json.loads(capsys.readouterr().out)
    result = anyio.run(create_server(db).call_tool, "get_history_discovery",
                      {"freshness": "frozen"})
    assert not result.is_error
    assert result.structured_content == cli
    assert cli["discovery_schema_version"] == "history-discovery/1.0"
    assert len(cli["entities"]) == 4
    assert cli["coverage"]["snapshot_count"] == 1
    assert cli["coverage"]["event_record_count"] == 1
    assert cli["source_channel"]["preinstallation_recorder_claims"] == "abstain"
    assert cli["annotations"] == []
    assert cli["size"]["semantic_completeness"] == "selected_highlights_only"
    assert cli["evidence_use"]["queried"]
    jsonschema.validate(cli, get_history_schema()["schemas"]["discovery"])
    assert len(result.content) == 1
    assert len(result.model_dump_json(by_alias=True).encode()) < 16 * 1024 * 1024


def test_discovery_tool_rejects_caller_paths_and_remote_adapter(discovery_catalog):
    server = create_server(discovery_catalog)
    tool = next(t for t in anyio.run(server.list_tools) if t.name == "get_history_discovery")
    assert tool.annotations.read_only_hint and not tool.annotations.destructive_hint
    for extra in ({"catalog_path": str(discovery_catalog)}, {"semantic_adapter": "remote"}):
        with pytest.raises(ToolError, match="Extra inputs are not permitted"):
            anyio.run(server.call_tool, "get_history_discovery", extra)


def test_discovery_cli_invalid_followup_bound_returns_structured_error(discovery_catalog, capsys):
    assert main(["discover", str(discovery_catalog), "--skeptic-limit", "21"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["history_schema_version"] == "1.0"
    assert result["error"]["code"] == "INVALID_ARGUMENT"
