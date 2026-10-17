"""Adversarial checks across the Python, CLI, and real stdio MCP surfaces.

The expected values in this file are derived from the hand-written XML and the
normative default table in docs/design.md, never from a saved implementation
response.  Run with PYTHONPATH=src so the checkout under test is explicit.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest
from jsonschema import Draft202012Validator

from rimworld_save_inspector.errors import InspectorError
from rimworld_save_inspector.models import QueryRequest
from rimworld_save_inspector.service import QueryService
from rimworld_save_inspector.source import load_snapshot
from rimworld_save_inspector.schemas import SUCCESS_SCHEMAS


ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable)
SYNTHETIC = b"""<savegame><meta><gameVersion>1.6.4871 rev591</gameVersion>
<modIds><li>core</li></modIds><modNames><li>Core</li></modNames></meta><game>
<tickManager><ticksGame>42</ticksGame></tickManager>
<maps><li><uniqueID>0</uniqueID><things>
<thing Class='Pawn'><id>Same</id><def>Human</def><kindDef>Colonist</kindDef>
<name><first>Aria</first><nick>Ria</nick><last>Vale</last></name>
<skills><skills><li><def>Shooting</def><level>4</level><passion> minor </passion><xpSinceLastLevel>1.5</xpSinceLastLevel><xpSinceMidnight>2.5</xpSinceMidnight></li></skills></skills>
<story><traits><allTraits><li><def>Tough</def><degree>1</degree></li></allTraits></traits></story>
<healthTracker><healthState>Mobile</healthState><hediffSet><hediffs><li><def>Cut</def><loadID>1</loadID><severity>0.25</severity></li></hediffs></hediffSet></healthTracker>
<genes><xenotype>Baseliner</xenotype><endogenes><li><def>GeneA</def><loadID>GA</loadID></li><li><def>GeneB</def><loadID>GB</loadID></li></endogenes><xenogenes><li><def>GeneC</def><loadID>GC</loadID></li></xenogenes></genes>
<equipment><equipment><innerList><li><id>Eq1</id><def>Knife</def><stackCount>1</stackCount></li><li><id>Stack</id><def>Medicine</def><stackCount>3</stackCount></li><li><id>EqMissing</id><def>Tool</def></li></innerList></equipment></equipment>
<apparel><wornApparel><innerList><li><id>App1</id><def>Jacket</def><stackCount>1</stackCount></li></innerList></wornApparel></apparel>
<inventory><innerContainer><innerList>
<li><id>Box</id><def>Container</def><innerContainer><innerList><li><id>Nested</id><def>Medicine</def><stackCount>2</stackCount></li></innerList></innerContainer></li>
<li>Thing_Stack</li><li>Thing_Stack</li><li>Thing_Missing</li>
</innerList></innerContainer></inventory></thing>
<thing Class='Pawn'><id>Same</id><def>Human</def><kindDef>Colonist</kindDef><name><first>Bryn</first></name></thing>
<thing Class='Pawn'><id>Third</id><def>Human</def><kindDef>Colonist</kindDef><name><first>Cy</first></name></thing>
<thing Class='ThingWithComps'><id>Stack</id><def>Medicine</def><stackCount>3</stackCount></thing>
</things></li></maps>
<researchManager><currentProj>ProjectA</currentProj><progress><keys><li>ProjectA</li><li>ProjectB</li></keys><values><li>0.5</li><li>1.25</li></values></progress><techprints><keys><li>ProjectA</li></keys><values><li>2</li></values></techprints></researchManager>
</game></savegame>"""


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


@pytest.fixture
def surface(tmp_path: Path):
    source = tmp_path / "synthetic-interface.rws"
    source.write_bytes(SYNTHETIC)
    snapshot = load_snapshot(source)
    service = QueryService(snapshot)
    pawn_id = next(r.record_id for r in snapshot.records if r.type_name == "pawn" and r.source_path.endswith("thing[1]"))
    return source, service, pawn_id


def _cli(source: Path, query: str, *args: str) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [str(PYTHON), "-m", "rimworld_save_inspector.cli", "inspect", str(source), query, *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=20, check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert completed.stderr == ""
    assert completed.stdout.endswith("\n") and completed.stdout.count("\n") == 1
    return json.loads(completed.stdout)


@asynccontextmanager
async def _mcp_session(source: Path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=str(PYTHON),
        args=["-c", "import sys; from rimworld_save_inspector.mcp_server import main; sys.argv=['rim-save-mcp', '--save', sys.argv[1]]; raise SystemExit(main())", str(source)],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        cwd=str(ROOT),
    )
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as server_stderr:
        async with stdio_client(params, errlog=server_stderr) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session, server_stderr


def _mcp_value(result):
    assert not result.is_error, result.content
    value = result.structured_content
    assert isinstance(value, dict), result
    return value


def test_handwritten_oracle_pins_effective_defaults_and_domains(surface):
    source, service, pawn_id = surface
    cases = [
        ("inspect", {}, "metadata", ()),
        ("summary", {}, "summary", ()),
        ("pawns", {}, "pawns", ()),
        ("pawn", {"record_id": pawn_id}, "pawn", (pawn_id,)),
        ("health", {"record_id": pawn_id}, "health", (pawn_id,)),
        ("genes", {"record_id": pawn_id}, "genes", (pawn_id,)),
        ("genes", {"record_id": pawn_id, "group": "xenogenes"}, "genes", (pawn_id, "--group", "xenogenes")),
        ("equipment", {"record_id": pawn_id}, "equipment", (pawn_id,)),
        ("equipment", {"record_id": pawn_id, "holder": "apparel"}, "equipment", (pawn_id, "--holder", "apparel")),
        ("inventory", {"record_id": pawn_id}, "inventory", (pawn_id,)),
        ("research", {}, "research", ()),
        ("research", {"section": "techprints"}, "research", ("--section", "techprints")),
        ("mods", {}, "mods", ()),
    ]
    # These defaults are manually pinned from the normative selector table.
    # Every paged query defaults to 50; omitted selectors normalize as listed.
    expected_defaults = {
        "inspect": ({}, None), "summary": ({}, None),
        "pawns": ({"scope": "map"}, 50), "pawn": ({"record_id": pawn_id, "section": "core"}, None),
        "health": ({"record_id": pawn_id}, 50), "genes": ({"record_id": pawn_id, "group": "endogenes"}, 50),
        "equipment": ({"record_id": pawn_id, "holder": "equipment", "estimate_missing_as_one": False}, 50),
        "inventory": ({"record_id": pawn_id, "holder_id": None, "estimate_missing_as_one": False}, 50),
        "research": ({"section": "progress"}, 50), "mods": ({}, 50),
    }
    default_mismatches = []
    for name, filters, cli_name, cli_args in cases:
        response = service.execute(QueryRequest(name, filters)).as_dict()
        normalized_filters, effective_limit = expected_defaults[name]
        if name == "genes" and filters.get("group") == "xenogenes":
            normalized_filters = {"record_id": pawn_id, "group": "xenogenes"}
        if name == "equipment" and filters.get("holder") == "apparel":
            normalized_filters = {"record_id": pawn_id, "holder": "apparel", "estimate_missing_as_one": False}
        if name == "research" and filters.get("section") == "techprints":
            normalized_filters = {"section": "techprints"}
        expected_query = {"name": name, "filters": normalized_filters, "limit": effective_limit}
        if response["query"] != expected_query:
            default_mismatches.append((name, response["query"], expected_query))
        cli = _cli(source, cli_name, *cli_args)
        assert _canonical(cli) == _canonical(response), name

    # A second, independent request spelling must be semantically identical.
    omitted = service.execute(QueryRequest("genes", {"record_id": pawn_id})).as_dict()
    explicit = service.execute(QueryRequest("genes", {"record_id": pawn_id, "group": "endogenes"})).as_dict()
    assert _canonical(omitted) == _canonical(explicit)
    assert omitted["page"]["total"] == 2
    estimate_default = service.execute(QueryRequest("equipment", {"record_id": pawn_id})).as_dict()
    estimate_explicit = service.execute(QueryRequest("equipment", {"record_id": pawn_id, "estimate_missing_as_one": True})).as_dict()
    estimate_false = service.execute(QueryRequest("equipment", {"record_id": pawn_id, "estimate_missing_as_one": False})).as_dict()
    assert _canonical(estimate_default) == _canonical(estimate_false)
    inventory_default = service.execute(QueryRequest("inventory", {"record_id": pawn_id})).as_dict()
    inventory_false = service.execute(QueryRequest("inventory", {"record_id": pawn_id, "estimate_missing_as_one": False})).as_dict()
    assert _canonical(inventory_default) == _canonical(inventory_false)
    assert estimate_default["data"]["quantity"]["estimated_stack_units"] is None
    assert estimate_explicit["data"]["quantity"]["estimated_stack_units"] == 5
    assert estimate_default["query"]["filters"].get("estimate_missing_as_one", False) is False
    assert estimate_explicit["query"]["filters"].get("estimate_missing_as_one", False) is True
    assert not default_mismatches, f"normalized default selectors disagree with section 6/7: {default_mismatches}"
    apparel = service.execute(QueryRequest("equipment", {"record_id": pawn_id, "holder": "apparel"})).as_dict()
    assert apparel["page"]["total"] == 1
    assert apparel["data"]["entries"][0]["item"]["item_def"] == "Jacket"


def test_real_stdio_mcp_matches_python_and_cli_for_all_domains(surface):
    source, service, pawn_id = surface

    async def run():
        async with _mcp_session(source) as (session, _server_stderr):
            inspected = _mcp_value(await session.call_tool("inspect_save", {}))
            snapshot_id = inspected["snapshot_id"]
            assert inspected["data"]["snapshot_lifetime"] == "process"
            expected_inspect = service.execute(QueryRequest("inspect")).as_dict()
            expected_inspect["data"]["snapshot_lifetime"] = "process"
            assert _canonical(inspected) == _canonical(expected_inspect)
            specs = [
                ("summary", {}, "get_colony_summary", {}),
                ("pawns", {}, "list_pawns", {}),
                ("pawn", {"record_id": pawn_id}, "get_pawn", {"record_id": pawn_id}),
                ("pawn", {"record_id": pawn_id, "section": "skills"}, "get_pawn", {"record_id": pawn_id, "section": "skills"}),
                ("pawn", {"record_id": pawn_id, "section": "traits"}, "get_pawn", {"record_id": pawn_id, "section": "traits"}),
                ("health", {"record_id": pawn_id}, "get_health", {"record_id": pawn_id}),
                ("genes", {"record_id": pawn_id}, "get_genes", {"record_id": pawn_id}),
                ("genes", {"record_id": pawn_id, "group": "xenogenes"}, "get_genes", {"record_id": pawn_id, "group": "xenogenes"}),
                ("equipment", {"record_id": pawn_id}, "get_equipment", {"record_id": pawn_id}),
                ("equipment", {"record_id": pawn_id, "estimate_missing_as_one": True}, "get_equipment", {"record_id": pawn_id, "estimate_missing_as_one": True}),
                ("equipment", {"record_id": pawn_id, "holder": "apparel"}, "get_equipment", {"record_id": pawn_id, "holder": "apparel"}),
                ("inventory", {"record_id": pawn_id}, "get_inventory", {"record_id": pawn_id}),
                ("research", {}, "get_research", {}),
                ("research", {"section": "techprints"}, "get_research", {"section": "techprints"}),
                ("mods", {}, "get_mods", {}),
            ]
            holder_query = service.execute(QueryRequest("inventory", {"record_id": pawn_id})).as_dict()
            holder_id = holder_query["data"]["entries"][0]["item"]["nested_holders"][0]["holder_id"]
            specs.append(("inventory", {"record_id": pawn_id, "holder_id": holder_id}, "get_inventory", {"record_id": pawn_id, "holder_id": holder_id}))
            for name, filters, tool, args in specs:
                expected = service.execute(QueryRequest(name, filters)).as_dict()
                arguments = {"snapshot_id": snapshot_id, **args}
                actual = _mcp_value(await session.call_tool(tool, arguments))
                assert _canonical(actual) == _canonical(expected), (name, filters)

                cli_query = {"inspect": "metadata", "pawn": "pawn"}.get(name, name)
                cli_args = []
                if "record_id" in filters:
                    cli_args.append(filters["record_id"])
                for key, flag in (("section", "--section"), ("group", "--group"), ("holder", "--holder"), ("holder_id", "--holder-id")):
                    if key in filters:
                        cli_args.extend((flag, str(filters[key])))
                if filters.get("estimate_missing_as_one"):
                    cli_args.append("--estimate-missing-as-one")
                cli = _cli(source, cli_query, *cli_args)
                assert _canonical(cli) == _canonical(expected), ("CLI", name, filters)

            # Every paged roster page is the same serialized page on all three surfaces.
            cursor = None
            all_mcp_entries = []
            while True:
                arguments = {"snapshot_id": snapshot_id, "limit": 1}
                request = QueryRequest("pawns", limit=1)
                if cursor is not None:
                    arguments["cursor"] = cursor
                    request = QueryRequest("pawns", limit=1, cursor=cursor)
                expected = service.execute(request).as_dict()
                actual = _mcp_value(await session.call_tool("list_pawns", arguments))
                assert _canonical(actual) == _canonical(expected)
                cli_args = ["--limit", "1"]
                if cursor is not None:
                    cli_args.extend(("--cursor", cursor))
                cli = _cli(source, "pawns", *cli_args)
                assert _canonical(cli) == _canonical(expected)
                all_mcp_entries.extend(actual["data"]["entries"])
                cursor = actual["page"]["next_cursor"]
                if cursor is None:
                    break
            assert len(all_mcp_entries) == 3
            assert len({entry["record_id"] for entry in all_mcp_entries}) == 3
            assert [entry["saved_id"] for entry in all_mcp_entries].count("Same") == 2

            other_pawn_id = next(r.record_id for r in service.snapshot.records
                                 if r.type_name == "pawn" and r.record_id != pawn_id)
            foreign = await session.call_tool("get_inventory", {
                "snapshot_id": snapshot_id, "record_id": other_pawn_id, "holder_id": holder_id,
            })
            assert foreign.is_error
            assert json.loads(foreign.content[0].text)["error"]["code"] == "HOLDER_NOT_OWNED"

    asyncio.run(run())


def test_duplicate_saved_ids_paginate_as_distinct_records_once(surface):
    _, service, _ = surface
    first = service.execute(QueryRequest("pawns", limit=1)).as_dict()
    pages = [first]
    while pages[-1]["page"]["next_cursor"] is not None:
        pages.append(service.execute(QueryRequest("pawns", limit=1, cursor=pages[-1]["page"]["next_cursor"])).as_dict())
    entries = [entry for page in pages for entry in page["data"]["entries"]]
    assert len(entries) == 3
    assert len({entry["record_id"] for entry in entries}) == 3
    assert [entry["saved_id"] for entry in entries].count("Same") == 2
    assert len({entry["record_id"] for entry in entries}) == len(entries)


def test_repeated_item_references_do_not_multiply_unique_stack_units(surface):
    _, service, pawn_id = surface
    result = service.execute(QueryRequest("inventory", {"record_id": pawn_id})).as_dict()
    assert result["data"]["entry_count"] == 4
    assert result["data"]["quantity"]["explicit_stack_units"] == 3
    assert result["data"]["quantity"]["unresolved_item_entries"] == 1
    refs = [entry for entry in result["data"]["entries"] if entry["entry_kind"] == "reference"]
    assert len(refs) == 3
    assert refs[0]["reference"]["target_record_ids"] == refs[1]["reference"]["target_record_ids"]
    assert refs[2]["reference"]["status"] == "unresolved"


def test_ambiguous_item_candidates_cannot_add_units_across_python_cli_mcp(tmp_path):
    """Only full records owned by the selected holder plus uniquely resolved refs count."""
    xml = b"""<savegame><meta><gameVersion>1.6.4871 rev591</gameVersion></meta><game>
<maps><li><uniqueID>0</uniqueID><things>
<thing Class='Pawn'><id>A</id><def>Human</def><inventory><innerContainer><innerList>
<li>Thing_SharedItem</li><li>Thing_SharedItem</li>
</innerList></innerContainer></inventory></thing>
<thing Class='Pawn'><id>AOwn</id><def>Human</def><inventory><innerContainer><innerList>
<li><id>SharedItem</id><def>Medicine</def><stackCount>8</stackCount></li><li>Thing_SharedItem</li>
</innerList></innerContainer></inventory></thing>
<thing Class='Pawn'><id>B</id><def>Human</def><inventory><innerContainer><innerList>
<li><id>SharedItem</id><def>Medicine</def><stackCount>4</stackCount></li>
</innerList></innerContainer></inventory></thing>
<thing Class='Pawn'><id>C</id><def>Human</def><inventory><innerContainer><innerList>
<li><id>SharedItem</id><def>Medicine</def><stackCount>6</stackCount></li>
</innerList></innerContainer></inventory></thing>
</things></li></maps></game></savegame>"""
    source = tmp_path / "synthetic-ambiguous-items.rws"
    source.write_bytes(xml)
    snapshot = load_snapshot(source)
    service = QueryService(snapshot)
    pawn_ids = {
        record.saved_id: record.record_id
        for record in snapshot.records if record.type_name == "pawn"
    }
    expected_units = {"A": 0, "AOwn": 8}
    expected_owned_records = {"A": 0, "AOwn": 1}
    expected_unresolved_refs = {"A": 2, "AOwn": 1}
    expected_by_transport = {}
    for saved_id, record_id in (("A", pawn_ids["A"]), ("AOwn", pawn_ids["AOwn"])):
        filters = {"record_id": record_id, "estimate_missing_as_one": True}
        expected = service.execute(QueryRequest("inventory", filters)).as_dict()
        data = expected["data"]
        assert data["quantity"]["explicit_stack_units"] == expected_units[saved_id]
        assert data["resolved_item_count"] == expected_owned_records[saved_id]
        assert data["quantity"]["unresolved_item_entries"] == expected_unresolved_refs[saved_id]
        assert data["quantity"]["estimated_stack_units"] is None
        refs = [entry for entry in data["entries"] if entry["entry_kind"] == "reference"]
        assert len(refs) == expected_unresolved_refs[saved_id]
        assert all(entry["reference"]["status"] == "ambiguous" for entry in refs)
        assert all(len(entry["reference"]["target_record_ids"]) >= 2 for entry in refs)

        cli = _cli(source, "inventory", record_id, "--estimate-missing-as-one")
        expected_by_transport[saved_id] = expected
        assert _canonical(cli) == _canonical(expected), ("CLI", saved_id)
        Draft202012Validator(SUCCESS_SCHEMAS["inventory"]).validate(expected)
        Draft202012Validator(SUCCESS_SCHEMAS["inventory"]).validate(cli)

    async def compare_mcp():
        async with _mcp_session(source) as (session, _server_stderr):
            inspected = _mcp_value(await session.call_tool("inspect_save", {}))
            for saved_id in ("A", "AOwn"):
                response = _mcp_value(await session.call_tool("get_inventory", {
                    "snapshot_id": inspected["snapshot_id"],
                    "record_id": pawn_ids[saved_id],
                    "estimate_missing_as_one": True,
                }))
                assert _canonical(response) == _canonical(expected_by_transport[saved_id]), ("MCP", saved_id)
                Draft202012Validator(SUCCESS_SCHEMAS["inventory"]).validate(response)

    asyncio.run(compare_mcp())


def test_nested_holder_selector_is_owned_and_cross_pawn_holder_is_rejected(surface):
    _, service, pawn_id = surface
    inventory = service.execute(QueryRequest("inventory", {"record_id": pawn_id})).as_dict()
    owned_holder_id = inventory["data"]["entries"][0]["item"]["nested_holders"][0]["holder_id"]
    nested = service.execute(QueryRequest("inventory", {"record_id": pawn_id, "holder_id": owned_holder_id})).as_dict()
    assert nested["data"]["holder"]["holder_id"] == owned_holder_id
    assert nested["page"]["total"] == 1
    other_pawn = next(r.record_id for r in service.snapshot.records if r.type_name == "pawn" and r.record_id != pawn_id)
    with pytest.raises(InspectorError) as foreign:
        service.execute(QueryRequest("inventory", {"record_id": other_pawn, "holder_id": owned_holder_id}))
    assert foreign.value.code == "HOLDER_NOT_OWNED"


def test_cursor_rejects_stale_or_mixed_snapshot_and_mcp_keeps_ready_snapshot(surface, tmp_path):
    source, service, pawn_id = surface
    page = service.execute(QueryRequest("pawns", limit=1)).as_dict()
    with pytest.raises(InspectorError) as stale:
        service.execute(QueryRequest("pawns", limit=2, cursor=page["page"]["next_cursor"]))
    assert stale.value.code == "INVALID_CURSOR"
    changed_source = tmp_path / "other.rws"
    changed_source.write_bytes(SYNTHETIC.replace(b"<ticksGame>42", b"<ticksGame>43"))
    other = QueryService(load_snapshot(changed_source))
    with pytest.raises(InspectorError) as mixed:
        other.execute(QueryRequest("pawns", limit=1, cursor=page["page"]["next_cursor"]))
    assert mixed.value.code == "INVALID_CURSOR"

    equipment_page = service.execute(QueryRequest("equipment", {"record_id": pawn_id}, limit=1)).as_dict()
    with pytest.raises(InspectorError) as cross_estimate:
        service.execute(QueryRequest("equipment", {"record_id": pawn_id, "estimate_missing_as_one": True},
                                     limit=1, cursor=equipment_page["page"]["next_cursor"]))
    assert cross_estimate.value.code == "INVALID_CURSOR"

    async def run():
        async with _mcp_session(source) as (session, _server_stderr):
            ready = _mcp_value(await session.call_tool("inspect_save", {}))
            source.write_bytes(SYNTHETIC.replace(b"<ticksGame>42", b"<ticksGame>999"))
            again = _mcp_value(await session.call_tool("inspect_save", {}))
            assert _canonical(again) == _canonical(ready)
            summary = _mcp_value(await session.call_tool("get_colony_summary", {"snapshot_id": ready["snapshot_id"]}))
            assert summary["data"]["ticks_game"] == 42
            wrong = await session.call_tool("get_colony_summary", {"snapshot_id": "sha256:" + "0" * 64})
            assert wrong.is_error
            error = json.loads(wrong.content[0].text)
            assert error["error"]["code"] == "SNAPSHOT_MISMATCH"
    asyncio.run(run())


def test_default_errors_and_stderr_do_not_leak_path_or_raw_sentinel(surface, tmp_path):
    source, _, _ = surface
    bad = tmp_path / "private-absolute-sentinel.rws"
    bad.write_text("<savegame><SECRET_SENTINEL_7F42>", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.run([str(PYTHON), "-m", "rimworld_save_inspector.cli", "inspect", str(bad), "summary"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=20, check=False)
    assert proc.returncode == 2
    failure = proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["error"]["code"] == "MALFORMED_XML"
    assert str(bad.resolve()) not in failure
    assert "SECRET_SENTINEL_7F42" not in failure
    assert "SECRET_SENTINEL_7F42" not in proc.stderr
    success = _cli(source, "summary")
    serialized_success = json.dumps(success, ensure_ascii=False)
    assert str(source.resolve()) not in serialized_success
    assert "SECRET_SENTINEL_7F42" not in serialized_success

    # Explicit source disclosure is the sole supported CLI path extension.
    disclosed = _cli(source, "summary", "--include-source-path")
    assert disclosed["provenance"]["source_path"] == str(source.resolve())

    async def mcp_error():
        async with _mcp_session(bad) as (session, server_stderr):
            failure = await session.call_tool("inspect_save", {})
            assert failure.is_error
            payload = failure.content[0].text
            assert json.loads(payload)["error"]["code"] == "MALFORMED_XML"
            assert str(bad.resolve()) not in payload
            assert "SECRET_SENTINEL_7F42" not in payload
            await asyncio.sleep(0.05)
            server_stderr.flush()
            server_stderr.seek(0)
            stderr_text = server_stderr.read()
            assert str(bad.resolve()) not in stderr_text
            assert "SECRET_SENTINEL_7F42" not in stderr_text

    asyncio.run(mcp_error())


@pytest.mark.parametrize(("args", "code"), [
    (("unknown",), "INVALID_ARGUMENT"),
    (("summary", "--wat"), "INVALID_ARGUMENT"),
    (("pawn",), "INVALID_ARGUMENT"),
])
def test_cli_argument_failures_have_stable_codes_without_fabricated_success(surface, args, code):
    source, _, _ = surface
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.run([str(PYTHON), "-m", "rimworld_save_inspector.cli", "inspect", str(source), *args],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=20, check=False)
    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["error"]["code"] == code
    assert "data" not in payload
