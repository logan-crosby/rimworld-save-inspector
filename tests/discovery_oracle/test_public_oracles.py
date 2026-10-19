"""Independent structural and evidence-link checks for the frozen public gold set."""
import json
import hashlib
from pathlib import Path
import unittest

FIXTURE = Path(__file__).parent / "fixtures" / "public-discovery-oracles-v1.json"
REGISTER = Path(__file__).parents[2] / "docs" / "requirements" / "requirement-register.json"
REQUIRED_CLASSES = {
    "routine", "rare_benign", "contradictory", "unknowable", "misleading_name",
    "wrong_pawn_near_match", "incomplete_coverage", "biology_perspective"
}


class PublicOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_case_classes_are_covered_exactly_once(self):
        cases = self.data["cases"]
        self.assertEqual(len(cases), 8)
        self.assertEqual({c["class"] for c in cases}, REQUIRED_CLASSES)
        self.assertEqual(len({c["id"] for c in cases}), len(cases))

    def test_every_gold_claim_resolves_to_same_entity_evidence(self):
        for case in self.data["cases"]:
            evidence = {row["id"]: row for row in case["evidence"]}
            self.assertEqual(len(evidence), len(case["evidence"]), case["id"])
            for claim in case["gold"]["claims"]:
                self.assertTrue(claim["support"], (case["id"], claim["text"]))
                self.assertTrue(set(claim["support"]) <= evidence.keys(), case["id"])
                entities = {evidence[eid]["entity"] for eid in claim["support"]}
                self.assertTrue(entities.isdisjoint({"pawn:nera-71"}) or case["id"] == "PUB-WRONG-PAWN-006")

    def test_unknowns_and_partial_coverage_require_abstention(self):
        cases = {c["class"]: c for c in self.data["cases"]}
        self.assertFalse(cases["unknowable"]["gold"]["claims"])
        self.assertGreaterEqual(len(cases["unknowable"]["gold"]["must_abstain"]), 3)
        self.assertTrue(cases["incomplete_coverage"]["gold"]["must_abstain"])
        self.assertTrue(cases["contradictory"]["gold"]["must_abstain"])

    def test_biology_and_perspectives_remain_distinct_from_causality(self):
        case = next(c for c in self.data["cases"] if c["class"] == "biology_perspective")
        ids = {e["id"] for e in case["evidence"]}
        self.assertTrue({"e-eno-guard", "e-eno-xeno", "e-eno-active", "e-eno-overridden"} <= ids)
        self.assertTrue({"e-eno-memory", "e-iri-memory"} <= ids)
        self.assertIn("whether biology caused the conflict", case["gold"]["must_abstain"])
        self.assertIn("either pawn's motive or consent", case["gold"]["must_abstain"])

    def test_causal_and_identity_negative_controls_are_frozen(self):
        by_id = {c["id"]: c for c in self.data["cases"]}
        self.assertTrue(any("absence proves" in x for x in by_id["PUB-UNKNOWABLE-004"]["gold"]["must_not_claim"]))
        self.assertTrue(any("Nera died" in x for x in by_id["PUB-WRONG-PAWN-006"]["gold"]["must_not_claim"]))
        self.assertIn("resurrection", by_id["PUB-CONTRADICTION-003"]["gold"]["must_not_claim"])

    def test_requirement_register_covers_all_g_h_l_a_and_research_rows(self):
        register = json.loads(REGISTER.read_text(encoding="utf-8"))
        requirements = register["requirements"]
        ids = {row["id"] for row in requirements}
        required = ({f"G-R{i:02}" if i < 10 else f"G-R{i}" for i in range(1, 21)}
                    | {f"HIS-{i:02}" for i in range(1, 15)}
                    | {f"L-{i:02}" for i in range(1, 13)}
                    | {f"A{i}" for i in range(8)}
                    | {f"DISC-{i:02}" for i in range(1, 17)}
                    | {f"REC-P{i}" for i in range(1, 6)}
                    | {f"EXP-{i:02}" for i in range(1, 5)})
        self.assertEqual(ids, required)
        self.assertEqual(len(requirements), 79)
        for row in requirements:
            self.assertTrue(row["source"]["pointer"], row["id"])
            self.assertTrue(row["owner"], row["id"])
            self.assertIsInstance(row["dependencies"], list)
            self.assertTrue(row["acceptance_test"], row["id"])
            self.assertTrue(row["evidence_required"], row["id"])
            self.assertIn(row["status"], {"pending", "verified", "failed", "blocked", "deferred"})
        for row in requirements:
            if row["id"].startswith("L-") or row["id"] in {"DISC-01", "DISC-16"}:
                self.assertEqual(row["disposition"], "mandatory", row["id"])
        self.assertEqual({r["disposition"] for r in requirements}, {"mandatory", "exploratory"})

    def test_register_source_hashes_match_referenced_designs_and_public_files_are_sanitized(self):
        register = json.loads(REGISTER.read_text(encoding="utf-8"))
        for source in register["sources"].values():
            if source.get("visibility") == "private":
                self.assertTrue(source["private_binding_required"])
                self.assertNotIn("path", source)
                continue  # Separate mandatory local receipt verifies private source bytes.
            path = Path(source["path"])
            self.assertTrue(path.is_file(), source["path"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), source["sha256"])
        public = FIXTURE.read_text(encoding="utf-8") + REGISTER.read_text(encoding="utf-8")
        self.assertNotIn("Farstdos", public)
        self.assertNotIn(".private/history-oracle", public)
        self.assertNotIn("/Users/", public)


if __name__ == "__main__":
    unittest.main()
