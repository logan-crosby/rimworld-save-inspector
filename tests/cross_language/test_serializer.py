"""Mandatory real C# serializer interoperability; no fixture fallback or skip."""
import json
import os
from pathlib import Path
import subprocess
import sys

from rimworld_save_inspector.journal import read_journal


def test_actual_csharp_serializer_preserves_control_baseline_and_unknown_event():
    value = os.environ.get('RWSI_CSHARP_SAMPLE_PATH')
    assert value, 'Generate Recorder.Sample output and provide RWSI_CSHARP_SAMPLE_PATH; missing evidence cannot pass.'
    path = Path(value)
    assert path.is_file()
    raw = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row['event_type'] for row in raw] == ['session_started', 'pawn_baseline', 'future_pawn_signal']
    assert [row['sequence'] for row in raw] == [1, 2, 3]
    assert raw[2]['extensions']['example.source_tag'] == 'csharp-production-serializer'
    before = path.read_bytes()
    result = read_journal(path, pawn_id='Pawn_123')
    assert result['coverage']['failed_source_count'] == 0
    assert result['coverage']['record_count'] == 3
    assert [row['event'] for row in result['events']] == raw
    assert path.read_bytes() == before


def test_actual_csharp_output_cli_matches_python_exactly():
    value = os.environ.get('RWSI_CSHARP_SAMPLE_PATH')
    assert value, 'Actual serializer output is mandatory; do not substitute a synthetic JSONL.'
    expected = read_journal(value, pawn_id='Pawn_123')
    command = [sys.executable, '-m', 'rimworld_save_inspector.cli', 'journal', value, '--pawn-id', 'Pawn_123']
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected


def test_actual_csharp_output_readonly_stdio_mcp_matches_python():
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    value = os.environ.get('RWSI_CSHARP_SAMPLE_PATH')
    assert value, 'Real C# output is required for MCP interoperability.'
    expected = read_journal(value, pawn_id='Pawn_123')
    async def check():
        params = StdioServerParameters(command=sys.executable,
            args=['-m', 'rimworld_save_inspector.cli', 'journal-mcp', '--journal', value],
            env={'PYTHONPATH': os.environ['PYTHONPATH']} if os.environ.get('PYTHONPATH') else None)
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=30) as session:
                await session.initialize()
                tools = await session.list_tools()
                tool = next(t for t in tools.tools if t.name == 'get_pawn_journal')
                assert tool.annotations.read_only_hint is True
                assert tool.annotations.destructive_hint is False
                result = await session.call_tool('get_pawn_journal', {'pawn_id': 'Pawn_123'})
                assert not result.is_error
                assert result.structured_content == expected
    anyio.run(check)
