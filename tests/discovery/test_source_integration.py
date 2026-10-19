"""Integration expectations use the independently frozen history XML fixtures."""
from pathlib import Path

from rimworld_save_inspector.discovery import discover_collection, discover_snapshot
from rimworld_save_inspector.history import extract_history_snapshot
from rimworld_save_inspector.history.identity import campaign_identity

FIXTURES = Path(__file__).parents[1] / "history_oracle" / "fixtures"


def test_actual_source_campaign_claims_join_compatible_saved_pawns():
    snapshots = [extract_history_snapshot(FIXTURES / name).as_dict()
                 for name in ("alpha-early.xml", "alpha-later.xml")]
    expected_campaign = campaign_identity(snapshots[0]["metadata"])
    assert expected_campaign is not None
    result = discover_collection(snapshots)
    founder = [row for row in result["entities"] if row["saved_id"] == "pawn-alpha-01"]
    assert len(founder) == 1
    assert founder[0]["campaign_id"] == expected_campaign
    assert set(founder[0]["snapshot_ids"]) == {row["snapshot_id"] for row in snapshots}


def test_actual_indexed_source_locator_survives_discovery_claim_conversion():
    snapshot = extract_history_snapshot(FIXTURES / "alpha-early.xml").as_dict()
    result = discover_snapshot(snapshot)
    source_claims = [claim for pawn in snapshot["pawns"] for domain in pawn["domains"].values()
                     for claim in domain.get("claims", []) if "raw_field" in claim]
    assert source_claims
    for claim in source_claims:
        matching = [row for row in result["claims"] if row["field"] == claim["field"]
                    and row["evidence_id"] == claim["evidence_id"]
                    and row["source_ref"] == claim["source_ref"]
                    and row["raw_value"] == claim["raw_value"]]
        assert any(row.get("raw_field") == claim["raw_field"] for row in matching)


def test_collection_handles_non_partial_domain_status_mapping():
    snapshot = extract_history_snapshot(FIXTURES / "alpha-early.xml").as_dict()
    snapshot["coverage"]["domains"] = {"identity": {"status": "observed", "observed_count": 1}}
    result = discover_collection([snapshot])
    assert result["coverage"]["incomplete_domains"] == []


def test_unvalidated_campaign_label_cannot_join_saved_pawn_ids():
    snapshots = [extract_history_snapshot(FIXTURES / name).as_dict()
                 for name in ("alpha-early.xml", "alpha-later.xml")]
    for snapshot in snapshots:
        snapshot["metadata"]["campaign_key"] = {"status": "compatible", "value": "same-label"}
    result = discover_collection(snapshots)
    founders = [row for row in result["entities"] if row["saved_id"] == "pawn-alpha-01"]
    assert len(founders) == 2
    assert all(row["join_basis"] == "snapshot_only" for row in founders)
