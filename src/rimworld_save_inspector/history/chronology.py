"""Integer-only clock conversion and branch-aware observation ordering."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_time(raw_value: Any, clock: str, *, game_start_abs_tick: Any,
                   snapshot_game_tick: Any) -> dict[str, Any]:
    """Normalize a validated clock; unknown and durations never become zero."""
    result = {"raw_value": raw_value, "clock": clock, "normalized_game_tick": None,
              "normalization_rule": None, "status": "unknown"}
    if clock not in {"absolute_tick", "game_tick", "biological_duration", "wall_clock", "unknown"}:
        result["status"] = "invalid"
        return result
    if clock in {"biological_duration", "wall_clock", "unknown"}:
        return result
    tick = _integer(raw_value)
    start = _integer(game_start_abs_tick)
    snapshot = _integer(snapshot_game_tick)
    if tick is None or snapshot is None or (clock == "absolute_tick" and start is None):
        result["status"] = "invalid" if raw_value not in (None, "") else "unknown"
        return result
    normalized = tick - start if clock == "absolute_tick" else tick
    result["normalized_game_tick"] = normalized
    result["normalization_rule"] = (
        "absolute_minus_gameStartAbsTick" if clock == "absolute_tick" else "game_relative_identity"
    )
    result["status"] = (
        "pre_arrival" if normalized < 0 else
        "future_contradiction" if normalized > snapshot else "known"
    )
    return result


def order_observations(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Sort known time first, retaining all same-tick alternatives."""
    ordered = [dict(item) for item in items]
    ordered.sort(key=lambda item: (item.get("tick") is None,
                                   item.get("tick") if item.get("tick") is not None else 0,
                                   str(item.get("snapshot_id", ""))))
    counts = Counter(item.get("tick") for item in ordered if item.get("tick") is not None)
    alternatives = {
        tick: sorted(str(item["snapshot_id"]) for item in ordered if item.get("tick") == tick)
        for tick, count in counts.items() if count > 1
    }
    for item in ordered:
        item["alternative_snapshot_ids"] = alternatives.get(item.get("tick"), [])
    return ordered
