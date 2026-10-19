"""Lossless, provenance-linked semantic extraction for materialized pawns."""

from __future__ import annotations

from typing import Any, Callable

from .models import XmlNode, canonical_json_bytes, evidence_id_for_node, source_ref_for

DOMAIN_NAMES = (
    "identity", "status_and_interaction", "location", "age", "backstory", "traits", "skills",
    "health", "genes", "equipment", "apparel", "inventory", "relationships", "abilities",
    "mechanitor", "ideology", "royalty", "roles", "ownership", "work", "needs", "memories",
    "records", "events", "unknown_fields",
)

GROUP_TO_DOMAIN = {
    "def": "identity", "kindDef": "identity", "id": "identity", "ID": "identity",
    "name": "identity", "first": "identity", "nick": "identity", "last": "identity",
    "gender": "identity", "sex": "identity", "faction": "identity", "race": "identity",
    "royalty": "royalty",
    "guest": "status_and_interaction", "guestStatus": "status_and_interaction",
    "hostFaction": "status_and_interaction", "slaveFaction": "status_and_interaction",
    "prisoner": "status_and_interaction", "slave": "status_and_interaction",
    "ageTracker": "age", "ageBiologicalTicks": "age", "biologicalAgeTicks": "age",
    "birthAbsTicks": "age", "birthAbs": "age", "ageChronologicalTicks": "age",
    "story": "backstory", "childhood": "backstory", "adulthood": "backstory",
    "childhoodBackstory": "backstory", "adulthoodBackstory": "backstory",
    "traits": "traits", "allTraits": "traits", "skills": "skills",
    "healthTracker": "health", "hediffSet": "health", "hediffs": "health", "healthState": "health",
    "genes": "genes", "endogenes": "genes", "xenogenes": "genes", "xenotype": "genes",
    "xenotypeName": "genes", "overriddenByGene": "genes", "suppressedBy": "genes",
    "equipment": "equipment", "apparel": "apparel", "wornApparel": "apparel",
    "inventory": "inventory", "relations": "relationships", "directRelationShips": "relationships",
    "social": "relationships", "directRelations": "relationships",
    "abilities": "abilities", "abilityTracker": "abilities", "mechanitor": "mechanitor",
    "mechanitorSettings": "mechanitor", "ideology": "ideology", "ideo": "ideology",
    "ideoligion": "ideology",
    "ownership": "ownership", "bed": "ownership", "owner": "ownership", "grave": "ownership", "casket": "ownership",
    "workSettings": "work", "workPriorities": "work", "priorities": "work",
    "needs": "needs", "memories": "memories", "records": "records", "recordTracker": "records",
    "tales": "events", "eventRefs": "events",
}
INTEGER_FIELDS = {
    "birthAbsTicks", "birthAbs", "ageBiologicalTicks", "biologicalAgeTicks", "ageChronologicalTicks",
    "level", "degree", "tickAdded", "startTicks", "ticksAbs", "ageTicks", "stackCount", "priority", "ticksGame", "gameStartAbsTick",
}
DECIMAL_FIELDS = {"xpSinceLastLevel", "xpSinceMidnight"}
FIELD_UNITS = {
    "birthAbsTicks": "ticks", "birthAbs": "ticks", "ageBiologicalTicks": "ticks",
    "biologicalAgeTicks": "ticks", "ageChronologicalTicks": "ticks", "tickAdded": "ticks",
    "startTicks": "ticks", "ticksAbs": "ticks", "ageTicks": "ticks", "ticksGame": "ticks", "gameStartAbsTick": "ticks",
    "level": "level", "degree": "degree", "stackCount": "count", "priority": "priority",
    "xpSinceLastLevel": "experience_points", "xpSinceMidnight": "experience_points",
}
COLLECTION_TAGS = {
    "traits", "allTraits", "skills", "hediffs", "genes", "endogenes", "xenogenes",
    "equipment", "apparel", "wornApparel", "inventory", "relations", "directRelationShips",
    "abilities", "titles", "roles", "workPriorities", "priorities", "memories", "records",
}
REQUIRED_ABSENCE_FIELDS = {
    "identity": ("faction", "gender", "sex", "kindDef", "def"),
    "status_and_interaction": ("guest", "guestStatus", "hostFaction", "slaveFaction", "prisoner", "slave"),
    "roles": ("role", "roles", "leaderRole"),
    "ideology": ("ideology", "ideo", "ideoligion"),
}
NESTED_DOMAIN_TAGS = {
    "traits", "allTraits", "skills", "healthTracker", "hediffSet", "hediffs",
    "genes", "endogenes", "xenogenes", "xenotype", "equipment", "apparel", "wornApparel",
    "inventory", "relations", "directRelationShips", "abilities", "abilityTracker",
    "mechanitor", "mechanitorSettings", "ideology", "ideo", "ideoligion", "titles",
    "royalty", "workSettings", "workPriorities", "priorities",
    "needs", "memories", "records", "recordTracker", "tales", "eventRefs",
}


class EvidenceCollector:
    """Collect unique exact content/occurrence pairs for one source snapshot."""

    def __init__(self, snapshot_id: str, interpretation_id: str,
                 check_work: Callable[[], None] | None = None) -> None:
        self.snapshot_id = snapshot_id
        self.interpretation_id = interpretation_id
        self._check_work = check_work
        self.rows: list[dict[str, Any]] = []
        self._seen: set[tuple[str, str]] = set()
        self._node_pairs: dict[int, tuple[str, str]] = {}

    def check_work(self) -> None:
        if self._check_work is not None:
            self._check_work()

    def add(self, node: XmlNode, *, path: str | None = None) -> dict[str, str]:
        self.check_work()
        cached = self._node_pairs.get(id(node))
        if cached is not None:
            return {"evidence_id": cached[0], "source_ref": cached[1]}
        source_path = path or node.path
        value = node.as_node()
        evidence_id = evidence_id_for_node(value)
        source_ref = source_ref_for(self.snapshot_id, self.interpretation_id, source_path)
        pair = (evidence_id, source_ref)
        if pair not in self._seen:
            self._seen.add(pair)
            self.rows.append({
                "evidence_id": evidence_id, "source_ref": source_ref,
                "snapshot_id": self.snapshot_id, "interpretation_id": self.interpretation_id,
                "path": source_path, "node": value,
            })
        # Claims repeatedly cite their containing pawn and many pawns share the
        # same holder nodes. Retain this reference key so the exact node body is
        # serialized and hashed once per source-node occurrence.
        self._node_pairs[id(node)] = pair
        return {"evidence_id": evidence_id, "source_ref": source_ref}

    def add_start_tag_attribute(self, node: XmlNode, attribute: str) -> dict[str, str]:
        """Anchor an attribute to the authentic SAX start-tag observation only."""
        self.check_work()
        value = {"tag": node.tag, "attributes": [list(pair) for pair in node.attributes],
                 "text": None, "children": []}
        evidence_id = evidence_id_for_node(value)
        source_ref = source_ref_for(self.snapshot_id, self.interpretation_id, node.path)
        pair = (evidence_id, source_ref)
        if pair not in self._seen:
            self._seen.add(pair)
            self.rows.append({
                "evidence_id": evidence_id, "source_ref": source_ref,
                "snapshot_id": self.snapshot_id, "interpretation_id": self.interpretation_id,
                "path": node.path, "node": value, "evidence_kind": "xml_start_tag",
                "raw_field": "/@{}".format(attribute),
            })
        return {"evidence_id": evidence_id, "source_ref": source_ref}


def context_claim(field: str, node: XmlNode, collector: EvidenceCollector, *,
                 raw_field: str | None = None, value: Any = ...,
                 evidence_node: XmlNode | None = None) -> dict[str, Any]:
    """Build one generic, exact-source claim for a shared context entity.

    Context values stay as serialized text. Domain-specific normalization must
    be added only when a verified profile rule supports it.
    """
    target = evidence_node or node
    pair = collector.add(target)
    raw = node.text if not node.children else None
    claim_value = raw.strip() if value is ... and raw is not None else value
    if value is ... and raw is None:
        claim_value = None
    return {
        "field": field,
        "raw_field": raw_field or node.path,
        "value": claim_value,
        "raw_value": raw,
        "status": "observed" if raw is not None else "explicit-null",
        "unit": None,
        "code": None,
        "display_label": None,
        "normalization_rule": None,
        "time_observation": None,
        **pair,
    }


def context_entity(entity_id: str, kind: str, claims: list[dict[str, Any]], *,
                   links: list[dict[str, Any]] | None = None,
                   evidence_refs: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Return the contract's shared `{id, kind, claims, links, evidence_refs}` shape."""
    refs = []
    seen: set[tuple[str, str]] = set()
    for ref in evidence_refs or ():
        pair = (ref["evidence_id"], ref["source_ref"])
        if pair not in seen:
            seen.add(pair)
            refs.append({"evidence_id": pair[0], "source_ref": pair[1]})
    for claim in claims:
        pair = (claim["evidence_id"], claim["source_ref"])
        if pair not in seen:
            seen.add(pair)
            refs.append({"evidence_id": pair[0], "source_ref": pair[1]})
    for link in links or ():
        if link.get("evidence_id") and link.get("source_ref"):
            pair = (link["evidence_id"], link["source_ref"])
            if pair not in seen:
                seen.add(pair)
                refs.append({"evidence_id": pair[0], "source_ref": pair[1]})
    return {"id": entity_id, "kind": kind, "claims": claims,
            "links": links or [], "evidence_refs": refs}

def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag


def semantic_name(tag: str) -> str:
    """Return a semantic field name only for an unqualified saved QName."""
    return "" if tag.startswith("{") else tag


def children_named(node: XmlNode, name: str) -> list[XmlNode]:
    return [child for child, _ in node.children or [] if semantic_name(child.tag) == name]


def _value(node: XmlNode) -> tuple[Any, str]:
    if node.children:
        return [], "observed"
    raw = node.text
    if raw is None:
        return [], "observed"
    normalized = raw.strip()
    if semantic_name(node.tag) in INTEGER_FIELDS:
        try:
            return int(normalized, 10), "observed"
        except ValueError:
            return None, "invalid"
    if semantic_name(node.tag) in DECIMAL_FIELDS:
        try:
            number = float(normalized)
            if number != number or number in (float("inf"), float("-inf")):
                return None, "invalid"
            return number, "observed"
        except ValueError:
            return None, "invalid"
    return normalized, "observed"


def _time_observation(
    node: XmlNode,
    raw: str | None,
    *,
    game_start_abs: int | None,
    snapshot_ticks: int | None,
    pair: dict[str, str],
    record_path: str,
) -> dict[str, Any] | None:
    tag = semantic_name(node.tag)
    if tag in {"birthAbsTicks", "birthAbs"}:
        clock, rule = "absolute_tick", "absolute_tick_minus_gameStartAbsTick"
    elif tag in {"ageBiologicalTicks", "biologicalAgeTicks"}:
        clock, rule = "biological_duration", None
    elif tag in {"ageChronologicalTicks", "tickAdded", "startTicks", "ticksAbs"}:
        clock, rule = "unknown", None
    else:
        return None
    try:
        number = int(raw.strip(), 10) if raw is not None else None
    except ValueError:
        number = None
    if clock == "absolute_tick" and number is not None and game_start_abs is not None:
        normalized = number - game_start_abs
        status = "pre_arrival" if normalized < 0 else "known"
        if snapshot_ticks is not None and normalized > snapshot_ticks:
            status = "future_contradiction"
    elif clock == "game_tick" and number is not None:
        normalized, status = number, "known"
    else:
        normalized = None
        status = "invalid" if raw is not None and number is None else "unknown"
    return {
        "raw_value": raw, "raw_field": node.path[len(record_path):] or "/", "clock": clock,
        "normalized_game_tick": normalized, "normalization_rule": rule, "status": status, **pair,
    }


def _walk_claims(
    node: XmlNode, *, domain: str, prefix: str, evidence: EvidenceCollector,
    game_start_abs: int | None, snapshot_ticks: int | None, claims: list[dict[str, Any]],
    record_path: str, all_domains: dict[str, dict[str, Any]], claim_anchor: XmlNode,
) -> None:
    if not semantic_name(node.tag):
        raw = node.text if not node.children else None
        pair = evidence.add(node)
        all_domains["unknown_fields"]["claims"].append({
            "field": "unknown_fields." + node.path.rsplit("/", 1)[-1],
            "raw_field": node.path[len(record_path):] or "/",
            "value": None, "raw_value": raw, "status": "unsupported", "unit": None,
            "code": None, "display_label": None, "normalization_rule": None,
            "time_observation": None, "structural_kind": "unknown_pawn_field",
            "serialized_bytes": len(canonical_json_bytes(node.as_node())), **pair,
        })
        all_domains["unknown_fields"]["links"].append({
            "kind": "raw_xml_node", "target_id": pair["evidence_id"],
            "status": "unsupported", **pair,
        })
        all_domains["unknown_fields"]["status"] = "partial"
        return
    segment = node.path.rsplit("/", 1)[-1]
    semantic_path = "{}.{}".format(prefix, segment) if prefix else segment
    first_segment = semantic_path.split(".", 1)[0].split("[", 1)[0]
    field = semantic_path if first_segment == domain else "{}.{}".format(domain, semantic_path)
    if node.children:
        for child, _ in node.children:
            child_name = semantic_name(child.tag)
            child_domain = GROUP_TO_DOMAIN.get(child_name, domain) if child_name in NESTED_DOMAIN_TAGS else domain
            child_claims = all_domains[child_domain]["claims"]
            all_domains[child_domain]["status"] = "complete"
            _walk_claims(child, domain=child_domain, prefix=semantic_path, evidence=evidence,
                         game_start_abs=game_start_abs, snapshot_ticks=snapshot_ticks, claims=child_claims,
                         record_path=record_path, all_domains=all_domains, claim_anchor=claim_anchor)
        return
    raw = node.text
    value, status = _value(node)
    if semantic_name(node.tag) in COLLECTION_TAGS and raw is None:
        value, status = [], "observed"
    pair = evidence.add(claim_anchor)
    claims.append({
        "field": field, "raw_field": node.path[len(record_path):] or "/",
        "value": value, "raw_value": raw, "status": status,
        "unit": FIELD_UNITS.get(semantic_name(node.tag)),
        "code": value if semantic_name(node.tag) == "def" and isinstance(value, str) else None,
        "display_label": None, "normalization_rule": None,
        "time_observation": _time_observation(
            node, raw, game_start_abs=game_start_abs, snapshot_ticks=snapshot_ticks, pair=pair,
            record_path=record_path),
        **pair,
    })


def build_pawn_domains(
    pawn_node: XmlNode, *, scope: str, holder_chain: list[dict[str, Any]],
    evidence: EvidenceCollector, game_start_abs: int | None, snapshot_ticks: int | None,
    pawn_ids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    domains = {name: {"status": "absent", "claims": [], "links": []} for name in DOMAIN_NAMES}
    domains["identity"]["status"] = "partial"
    domains["location"]["status"] = "complete" if scope != "unknown" else "partial"

    root_pair = evidence.add(pawn_node)
    domains["location"]["claims"].append({
        "field": "location.scope", "raw_field": "/", "value": scope, "raw_value": None, "status": "derived",
        "unit": None, "code": None, "display_label": None,
        "normalization_rule": "validated_holder_path_classification", "time_observation": None,
        **root_pair,
    })

    for child, _ in pawn_node.children or []:
        domain = GROUP_TO_DOMAIN.get(semantic_name(child.tag))
        if domain is None:
            pair = evidence.add(child)
            domains["unknown_fields"]["claims"].append({
                "field": "unknown_fields.{}".format(child.path.rsplit("/", 1)[-1]),
                "raw_field": child.path[len(pawn_node.path):] or "/",
                "value": None, "raw_value": None, "status": "unsupported", "unit": None,
                "code": None, "display_label": None, "normalization_rule": None,
                "time_observation": None, "structural_kind": "unknown_pawn_field",
                "serialized_bytes": len(canonical_json_bytes(child.as_node())), **pair,
            })
            domains["unknown_fields"]["links"].append({
                "kind": "raw_xml_node", "target_id": pair["evidence_id"],
                "status": "unsupported", **pair,
            })
            continue
        domains[domain]["status"] = "complete"
        _walk_claims(
            child, domain=domain, prefix="", evidence=evidence,
            game_start_abs=game_start_abs, snapshot_ticks=snapshot_ticks,
            claims=domains[domain]["claims"], record_path=pawn_node.path, all_domains=domains,
            claim_anchor=pawn_node,
        )

    direct_names = {semantic_name(child.tag) for child, _ in pawn_node.children or []}
    for domain_name, names in REQUIRED_ABSENCE_FIELDS.items():
        for name in names:
            if name not in direct_names:
                pair = evidence.add(pawn_node)
                domains[domain_name]["claims"].append({
                    "field": "{}.{}".format(domain_name, name), "raw_field": "/{}".format(name),
                    "value": None, "raw_value": None,
                    "status": "absent", "unit": None, "code": None, "display_label": None,
                    "normalization_rule": None, "time_observation": None, **pair,
                })
        domains[domain_name]["status"] = "complete"

    # The save schema does not establish a pawn-office model. Preserve exact
    # direct-field absences above, but do not interpret them as having no role.
    domains["roles"]["status"] = "unsupported"

    for attr_name, attr_value in pawn_node.attributes:
        pair = evidence.add(pawn_node)
        if attr_name == "Class":
            domains["identity"]["claims"].append({
                "field": "identity.class", "raw_field": "/@Class", "value": attr_value, "raw_value": attr_value,
                "status": "observed", "unit": None, "code": attr_value, "display_label": None,
                "normalization_rule": None, "time_observation": None, **pair,
            })
        elif attr_name not in {"id", "loadID"}:
            domains["unknown_fields"]["claims"].append({
                "field": "unknown_fields.attribute.{}".format(attr_name), "raw_field": "/@{}".format(attr_name),
                "value": attr_value, "raw_value": attr_value, "status": "unsupported",
                "unit": None, "code": None, "display_label": None, "normalization_rule": None,
                "time_observation": None, "structural_kind": "unknown_pawn_attribute",
                "serialized_bytes": len(canonical_json_bytes({"attribute": attr_name, "value": attr_value})), **pair,
            })

    domains["unknown_fields"]["status"] = (
        "partial" if domains["unknown_fields"]["claims"] else "complete"
    )
    relationships = domains["relationships"]
    relation_containers = children_named(pawn_node, "relations") + children_named(pawn_node, "directRelationShips")
    for social in children_named(pawn_node, "social"):
        relation_containers.extend(children_named(social, "directRelations"))
    for relation in relation_containers:
        for relation_node, _ in relation.children or []:
            relation_id = None
            relation_kind = None
            for field_node, _ in relation_node.children or []:
                local = semantic_name(field_node.tag)
                raw = field_node.text.strip() if field_node.text is not None else ""
                if local in {"otherPawn", "otherPawnID", "pawn", "relatedPawn"} and raw:
                    relation_id = raw
                elif local in {"def", "relation"} and raw:
                    relation_kind = raw
            if relation_id:
                pair = evidence.add(relation_node)
                relationships["links"].append({
                    "kind": relation_kind or "relationship_unknown", "target_id": relation_id,
                    "status": "observed" if pawn_ids is None or relation_id in pawn_ids else "unsupported",
                    "raw_target_id": relation_id, "direction": "outgoing", **pair,
                })
    return domains
