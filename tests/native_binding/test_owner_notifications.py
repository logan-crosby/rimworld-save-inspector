"""Pinned native-1.7 owner-hook safety regression.

Run with RIMWORLD_NATIVE_ASSEMBLY and ILSPYCMD pointing at the installed
Assembly-CSharp.dll and the pinned ilspycmd executable.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "recorder/Recorder.Mod/RecorderBootstrap.cs"
HOLDER = ROOT / "recorder/Recorder.Mod/HolderCollector.cs"


class NativeOwnerNotificationBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assembly = os.environ["RIMWORLD_NATIVE_ASSEMBLY"]
        ilspy = os.environ["ILSPYCMD"]
        cls.native = {}
        for name in ("Verse.Map", "Verse.Thing", "Verse.ThingOwner", "Verse.ThingOwner`1"):
            cls.native[name] = subprocess.run(
                [ilspy, "-t", name, assembly], check=True, capture_output=True, text=True
            ).stdout

    def test_native_spawn_and_notification_dataflow(self) -> None:
        self.assertIn("spawnedThings = new ThingOwner<Thing>(this)", self.native["Verse.Map"])
        self.assertIn("map.spawnedThings.TryAdd(this, canMergeWithExistingStacks: false)", self.native["Verse.Thing"])
        self.assertIn("protected virtual void NotifyAdded(Thing item)", self.native["Verse.ThingOwner"])
        self.assertIn("protected virtual void NotifyRemoved(Thing item)", self.native["Verse.ThingOwner"])
        self.assertIn("base.NotifyAdded(item)", self.native["Verse.ThingOwner`1"])
        self.assertIn("base.NotifyRemoved(item)", self.native["Verse.ThingOwner`1"])
        generic = self.native["Verse.ThingOwner`1"]
        add = generic.split("public override bool TryAdd(Thing item", 1)[1].split(
            "protected override void NotifyAdded", 1
        )[0]
        remove = generic.split("public override bool Remove(Thing item", 1)[1].split(
            "protected override void NotifyRemoved", 1
        )[0]
        self.assertLess(add.index("Contains(item)"), add.index("item.holdingOwner != null"))
        self.assertLess(add.index("item.holdingOwner != null"), add.index("item.holdingOwner = this"))
        self.assertLess(add.index("item.holdingOwner = this"), add.index("NotifyAdded(item2)"))
        self.assertLess(remove.index("Contains(item)"), remove.index("item.holdingOwner = null"))
        self.assertLess(remove.index("item.holdingOwner = null"), remove.index("NotifyRemoved(item)"))

    def test_recorder_avoids_closed_reference_generic_detours(self) -> None:
        bootstrap = BOOTSTRAP.read_text()
        holder = HOLDER.read_text()
        self.assertFalse(
            "InstallThingOwnerOverrides" in bootstrap + holder,
            "unsafe closed generic owner detour is still registered",
        )
        self.assertIn('typeof(ThingOwner), "Contains"', bootstrap)
        self.assertIn('typeof(ThingOwner), "NotifyAdded"', bootstrap)
        self.assertIn('typeof(ThingOwner), "NotifyRemoved"', bootstrap)
        self.assertIn("HoldingOwner = pawn.holdingOwner", holder)


if __name__ == "__main__":
    unittest.main()
