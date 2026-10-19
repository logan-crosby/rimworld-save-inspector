"""Independent synthetic expectations for catalog identity and chronology."""

from rimworld_save_inspector.history.chronology import normalize_time, order_observations
from rimworld_save_inspector.history.identity import campaign_identity, classify_compatibility, normalize_pawn_id


def _metadata(**changes):
    fields = {
        "seedString": "coral", "persistentRandomValue": "81234567",
        "initialMapSize": "(250,1,250)", "startingYear": "5500",
        "gameStartAbsTick": "52500", "startingTile": "42,0",
    }
    fields.update(changes)
    return {"campaign_key": {key: {"value": value, "status": "observed" if value is not None else "absent"}
                             for key, value in fields.items()},
            "profile": {"support_status": "verified"}}


def test_campaign_identity_needs_six_validated_values_and_ignores_provenance():
    first = _metadata()
    other = _metadata()
    first["campaign_key"]["seedString"]["source_ref"] = "a"
    other["campaign_key"]["seedString"]["source_ref"] = "b"
    assert campaign_identity(first) == campaign_identity(other)
    assert campaign_identity(_metadata(startingTile=None)) is None
    assert classify_compatibility(first, other) == "compatible"
    assert classify_compatibility(first, _metadata(startingTile="43,0")) == "incompatible"
    assert classify_compatibility(first, _metadata(startingTile=None)) == "review_needed"


def test_campaign_identity_accepts_source_contract_nested_fields_and_review_status():
    direct = _metadata()
    nested = {"campaign_key": {"fields": direct["campaign_key"],
                               "identity": {"status": "compatible", "value": "source-hash"}}}
    assert campaign_identity(nested) == campaign_identity(direct)
    nested["campaign_key"]["identity"]["status"] = "review_needed"
    assert campaign_identity(nested) is None


def test_pawn_ids_do_not_join_by_name_or_path():
    assert normalize_pawn_id(" Human42 ") == "Human42"
    assert normalize_pawn_id("Human42") != normalize_pawn_id("Human420")
    assert normalize_pawn_id(" ") is None


def test_absolute_game_duration_and_unknown_clocks_are_distinct():
    assert normalize_time("52490", "absolute_tick", game_start_abs_tick=52500,
                          snapshot_game_tick=730)["normalized_game_tick"] == -10
    future = normalize_time("53450", "absolute_tick", game_start_abs_tick=52500,
                            snapshot_game_tick=900)
    assert future["status"] == "future_contradiction"
    assert future["normalized_game_tick"] == 950
    assert normalize_time("900", "biological_duration", game_start_abs_tick=52500,
                          snapshot_game_tick=730)["normalized_game_tick"] is None
    assert normalize_time("12345", "unknown", game_start_abs_tick=52500,
                          snapshot_game_tick=730)["status"] == "unknown"


def test_same_tick_alternatives_stay_separate():
    ordered = order_observations([{"snapshot_id": "b", "tick": 900},
                                  {"snapshot_id": "a", "tick": 900},
                                  {"snapshot_id": "c", "tick": None}])
    assert [x["snapshot_id"] for x in ordered] == ["a", "b", "c"]
    assert ordered[0]["alternative_snapshot_ids"] == ["a", "b"]
    assert ordered[1]["alternative_snapshot_ids"] == ["a", "b"]
    assert ordered[2]["tick"] is None
