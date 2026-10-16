"""CLI JSON schema and numeric-lexeme contract checks."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from rimworld_save_inspector.schemas import ERROR_SCHEMA, SUCCESS_SCHEMAS

ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable)


class JsonContractVariantTests(unittest.TestCase):
    def _cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(PYTHON), "-m", "rimworld_save_inspector.cli", *args], cwd=ROOT,
                              text=True, encoding="utf-8", capture_output=True, timeout=30)

    def test_cli_numeric_lexemes_and_error_responses_validate(self):
        xml = b"""<savegame><meta><gameVersion>1.6.1</gameVersion></meta><game><maps><li><uniqueID>0</uniqueID><things>
        <thing Class='Pawn'><id>P1</id><def>Human</def><skills><skills><li><def>Shooting</def><level>5</level><passion>Minor</passion><xpSinceLastLevel>1.2300</xpSinceLastLevel></li></skills></skills></thing>
        </things></li></maps></game></savegame>"""
        with tempfile.TemporaryDirectory() as directory:
            save = Path(directory) / "lexeme.rws"
            save.write_bytes(xml)
            listing = self._cli("inspect", str(save), "pawns")
            self.assertEqual(listing.returncode, 0, listing.stderr)
            pawn_list = json.loads(listing.stdout)
            Draft202012Validator(SUCCESS_SCHEMAS["pawns"]).validate(pawn_list)
            record_id = pawn_list["data"]["entries"][0]["record_id"]
            skills = self._cli("inspect", str(save), "pawn", record_id, "--section", "skills")
            self.assertEqual(skills.returncode, 0, skills.stderr)
            response = json.loads(skills.stdout)
            Draft202012Validator(SUCCESS_SCHEMAS["pawn"]).validate(response)
            self.assertEqual(response["data"]["entries"][0]["raw_values"]["/xp_since_last_level"], "1.2300")

            failures = (
                self._cli("inspect", str(save) + ".missing", "summary"),
                self._cli("inspect", str(save), "pawns", "--not-a-filter", "x"),
                self._cli("inspect", str(save), "unknown-query"),
            )
            for failed in failures:
                self.assertEqual(failed.returncode, 2, (failed.stdout, failed.stderr))
                self.assertEqual(failed.stdout.count("\n"), 1)
                Draft202012Validator(ERROR_SCHEMA).validate(json.loads(failed.stdout))
                self.assertNotIn("Traceback", failed.stderr)


if __name__ == "__main__":
    unittest.main()
