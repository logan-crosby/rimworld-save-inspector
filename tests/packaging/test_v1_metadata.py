from __future__ import annotations

import re
import tomllib
from pathlib import Path

from rimworld_save_inspector import __version__, load_snapshot


ROOT = Path(__file__).resolve().parents[2]


def test_public_version_surfaces_agree():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = (ROOT / "uv.lock").read_text()
    package_entry = re.search(
        r'\[\[package\]\]\nname = "rimworld-save-inspector"\nversion = "([^"]+)"', lock
    )
    assert package_entry is not None
    assert project["project"]["version"] == __version__ == package_entry.group(1) == "1.1.0b1"


def test_parser_provenance_uses_package_version(tmp_path):
    save = tmp_path / "synthetic.rws"
    save.write_text(
        "<savegame><meta><gameVersion>1.6.1</gameVersion></meta>"
        "<game><maps><li><uniqueID>1</uniqueID><things /></li></maps></game></savegame>",
        encoding="utf-8",
    )
    assert load_snapshot(save).parser_version == __version__
