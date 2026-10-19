using System;
using System.Collections.Generic;
using System.Linq;
using LudeonTK;
using Newtonsoft.Json;
using RimWorld;
using RimWorld.Planet;
using Verse;

namespace Rwsi.NativeAcceptance
{
    public static class AcceptanceHolderBirthActions
    {
        private static Pawn? _heldPawn;
        private static Map? _heldMap;
        private static Caravan? _heldCaravan;
        private static Pawn? _carrier;
        private static Pawn? _carriedPawn;
        private static CompTransporter? _transporter;
        private static Building_CryptosleepCasket? _harnessCasket;
        private static Pawn? _inventoryOwner;
        private static Corpse? _inventoryCorpse;
        private static Building_GrowthVat? _growthVat;
        private static Pawn? _vatPawn;

        [DebugAction("RWSI Native Acceptance", "Transition: map pawn to caravan", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void MovePawnToCaravan()
        {
            var pawn = Find.CurrentMap?.mapPawns.AllPawnsSpawned.Where(candidate => candidate.Faction == Faction.OfPlayer && candidate.RaceProps.Humanlike && !candidate.Dead)
                .OrderBy(candidate => candidate.ThingID, StringComparer.Ordinal).FirstOrDefault();
            if (pawn == null || Find.CurrentMap == null || pawn.Faction == null)
            { Log.Error("[RWSI Native Acceptance] No living spawned player human with faction is available for caravan transition."); return; }
            var map = Find.CurrentMap;
            Run("pawn_holder_changed", "holder.map_to_caravan", "RimWorld.Planet.CaravanMaker.MakeCaravan(IEnumerable<Pawn>, Faction, PlanetTile, bool)", pawn,
                "same pawn leaves the current map and appears in a native Caravan holder chain", () =>
                {
                    _heldCaravan = CaravanMaker.MakeCaravan(new[] { pawn }, pawn.Faction, map.Tile, true);
                    _heldPawn = pawn;
                    _heldMap = map;
                    return _heldCaravan?.ID.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? "caravan_not_returned";
                }, (before, after, _) => before.Spawned && !after.Spawned &&
                    _heldCaravan != null && after.HolderChain.Any(path => path == typeof(Caravan).FullName + ":" + _heldCaravan.ID));
        }

        [DebugAction("RWSI Native Acceptance", "Transition: caravan pawn to map", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void RestorePawnFromCaravan()
        {
            var pawn = _heldPawn;
            var map = _heldMap;
            var caravan = _heldCaravan;
            if (pawn == null || map == null || caravan == null || pawn.Spawned || !caravan.ContainsPawn(pawn))
            { Log.Error("[RWSI Native Acceptance] Move a pawn to a harness caravan first."); return; }
            var cell = CellFinder.RandomClosewalkCellNear(map.Center, map, 12,
                candidate => candidate.Standable(map) && map.thingGrid.ThingsListAt(candidate).Count == 0);
            if (!cell.IsValid) { Log.Error("[RWSI Native Acceptance] No clear map cell exists for caravan restoration."); return; }
            Run("pawn_holder_changed", "holder.caravan_to_map", "Caravan.RemovePawn(Pawn); GenSpawn.Spawn(Pawn, Cell, Map, Rot4, WipeMode)", pawn,
                "same pawn leaves the exact Caravan holder chain and is spawned on the original map", () =>
                {
                    caravan.RemovePawn(pawn);
                    GenSpawn.Spawn(pawn, cell, map, Rot4.Random, WipeMode.Vanish);
                    if (!caravan.PawnsListForReading.Any()) caravan.Destroy();
                    return "void";
                }, (before, after, _) => before.HolderChain.Any(path => path == typeof(Caravan).FullName + ":" + caravan.ID) &&
                    !after.HolderChain.Any(path => path == typeof(Caravan).FullName + ":" + caravan.ID) && after.Spawned && after.MapId == map.uniqueID);
            _heldPawn = null;
            _heldMap = null;
            _heldCaravan = null;
        }

        [DebugAction("RWSI Native Acceptance", "Transition: place pawn in cryptosleep casket", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void PlacePawnInCryptosleepCasket()
        {
            var pawn = Find.CurrentMap?.mapPawns.AllPawnsSpawned.Where(candidate => candidate.Faction == Faction.OfPlayer && candidate.RaceProps.Humanlike && !candidate.Dead)
                .OrderBy(candidate => candidate.ThingID, StringComparer.Ordinal).FirstOrDefault();
            var map = Find.CurrentMap;
            var def = DefDatabase<ThingDef>.AllDefsListForReading.FirstOrDefault(candidate => candidate != null &&
                candidate.thingClass != null && typeof(Building_CryptosleepCasket).IsAssignableFrom(candidate.thingClass));
            if (pawn == null || map == null || def == null)
            { Log.Error("[RWSI Native Acceptance] A living player pawn, current map, and native casket definition are required."); return; }
            var cell = CellFinder.RandomClosewalkCellNear(map.Center, map, 12,
                candidate => candidate.Standable(map) && map.thingGrid.ThingsListAt(candidate).Count == 0);
            if (!cell.IsValid) { Log.Error("[RWSI Native Acceptance] No clear cell exists for a temporary native casket."); return; }
            var casket = ThingMaker.MakeThing(def) as Building_CryptosleepCasket;
            if (casket == null) { Log.Error("[RWSI Native Acceptance] Native casket definition did not create a cryptosleep casket."); return; }
            GenSpawn.Spawn(casket, cell, map, Rot4.North, WipeMode.Vanish);
            Run("pawn_holder_changed", "holder.cryptosleep_casket", "Building_CryptosleepCasket.TryAcceptThing(Pawn, bool); EjectContents()", pawn,
                "same pawn leaves the map and appears under the spawned exact casket holder", () =>
                {
                    var accepted = casket.TryAcceptThing(pawn);
                    if (accepted) { _harnessCasket = casket; _heldPawn = pawn; _heldMap = map; }
                    else casket.Destroy();
                    return accepted ? casket.ThingID : "casket_rejected_pawn";
                }, (before, after, result) => result != null && TransitionOracle.HolderEntry(before.Spawned, after.Spawned,
                    after.HolderChain.FirstOrDefault(path => path == typeof(Building_CryptosleepCasket).FullName + ":" + casket.ThingID),
                    typeof(Building_CryptosleepCasket).FullName + ":" + casket.ThingID));
        }

        [DebugAction("RWSI Native Acceptance", "Transition: eject pawn from cryptosleep casket", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void EjectPawnFromCryptosleepCasket()
        {
            var pawn = _heldPawn;
            var casket = _harnessCasket;
            var map = _heldMap;
            if (pawn == null || casket == null || map == null || casket.GetDirectlyHeldThings()?.Contains(pawn) != true)
            { Log.Error("[RWSI Native Acceptance] Place a pawn in the harness casket first."); return; }
            Run("pawn_holder_changed", "holder.cryptosleep_casket_eject", "Building_CryptosleepCasket.EjectContents()", pawn,
                "same pawn exits the exact casket holder and is spawned on the original map", () => { casket.EjectContents(); return "void"; },
                (before, after, _) => before.HolderChain.Any(path => path == typeof(Building_CryptosleepCasket).FullName + ":" + casket.ThingID) &&
                    !after.HolderChain.Any(path => path == typeof(Building_CryptosleepCasket).FullName + ":" + casket.ThingID) && after.Spawned && after.MapId == map.uniqueID);
            if (casket.Spawned) casket.Destroy();
            _harnessCasket = null;
            _heldPawn = null;
            _heldMap = null;
        }

        [DebugAction("RWSI Native Acceptance", "Transition: store corpse in pawn inventory", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void StoreCorpseInInventory()
        {
            var corpse = Find.CurrentMap?.listerThings.ThingsInGroup(ThingRequestGroup.Corpse).OfType<Corpse>()
                .Where(candidate => candidate.InnerPawn?.Dead == true).OrderBy(candidate => candidate.ThingID, StringComparer.Ordinal).FirstOrDefault();
            var owner = Find.CurrentMap?.mapPawns.AllPawnsSpawned.Where(candidate => candidate.Faction == Faction.OfPlayer && candidate.RaceProps.Humanlike && !candidate.Dead)
                .OrderBy(candidate => candidate.ThingID, StringComparer.Ordinal).FirstOrDefault();
            if (corpse == null || owner?.inventory?.innerContainer == null || corpse.InnerPawn == null)
            { Log.Error("[RWSI Native Acceptance] A current-map dead pawn corpse and living player inventory owner are required."); return; }
            var deadPawn = corpse.InnerPawn;
            Run("pawn_holder_changed", "holder.corpse_into_pawn_inventory", "Pawn_InventoryTracker.innerContainer.TryAdd(Corpse); ThingOwner.Remove(Corpse); GenSpawn.Spawn(Corpse, Cell, Map)", deadPawn,
                "dead pawn remains inside the exact corpse while that corpse moves from map into player inventory", () =>
                {
                    var added = owner.inventory.innerContainer.TryAdd(corpse);
                    if (added) { _inventoryOwner = owner; _inventoryCorpse = corpse; _heldMap = owner.Map; }
                    return added;
                }, (before, after, result) => Equals(result, true) && before.HolderChain.Length > 0 &&
                    !before.HolderChain.Any(path => path == typeof(Pawn).FullName + ":" + owner.ThingID) &&
                    after.HolderChain.Any(path => path == typeof(Pawn).FullName + ":" + owner.ThingID), owner);
        }

        [DebugAction("RWSI Native Acceptance", "Transition: remove corpse from pawn inventory", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void RemoveCorpseFromInventory()
        {
            var owner = _inventoryOwner;
            var corpse = _inventoryCorpse;
            var pawn = corpse?.InnerPawn;
            var map = _heldMap;
            if (owner?.inventory?.innerContainer == null || corpse == null || pawn == null || map == null ||
                !owner.inventory.innerContainer.Contains(corpse))
            { Log.Error("[RWSI Native Acceptance] Store a corpse in pawn inventory first."); return; }
            var cell = CellFinder.RandomClosewalkCellNear(owner.Position, map, 3,
                candidate => candidate.Standable(map) && map.thingGrid.ThingsListAt(candidate).Count == 0);
            if (!cell.IsValid) { Log.Error("[RWSI Native Acceptance] No clear cell is available for corpse inventory removal."); return; }
            Run("pawn_holder_changed", "holder.corpse_from_pawn_inventory", "ThingOwner.Remove(Corpse); GenSpawn.Spawn(Corpse, Cell, Map)", pawn,
                "same corpse leaves the exact player inventory chain and is spawned on its original map", () =>
                {
                    var removed = owner.inventory.innerContainer.Remove(corpse);
                    if (removed) GenSpawn.Spawn(corpse, cell, map, Rot4.Random, WipeMode.Vanish);
                    return removed;
                }, (before, after, result) => Equals(result, true) && before.HolderChain.Any(path => path == typeof(Pawn).FullName + ":" + owner.ThingID) &&
                    !after.HolderChain.Any(path => path == typeof(Pawn).FullName + ":" + owner.ThingID), owner);
            _inventoryOwner = null;
            _inventoryCorpse = null;
            _heldMap = null;
        }

        [DebugAction("RWSI Native Acceptance", "Transition: place child pawn in growth vat", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void PlaceChildPawnInGrowthVat()
        {
            var map = Find.CurrentMap;
            var vat = map?.listerThings.AllThings.OfType<Building_GrowthVat>().OrderBy(candidate => candidate.ThingID, StringComparer.Ordinal).FirstOrDefault();
            var pawn = map?.mapPawns.AllPawnsSpawned.Where(candidate => candidate.Faction == Faction.OfPlayer && candidate.RaceProps.Humanlike &&
                    !candidate.Dead && candidate.ageTracker.AgeBiologicalYearsFloat < 13f && vat?.CanAcceptPawn(candidate).Accepted == true)
                .OrderBy(candidate => candidate.ThingID, StringComparer.Ordinal).FirstOrDefault();
            if (map == null || vat == null || pawn == null)
            { Log.Error("[RWSI Native Acceptance] A spawned native growth vat and eligible living player child are required."); return; }
            Run("pawn_holder_changed", "holder.growth_vat", "Building_GrowthVat.CanAcceptPawn(Pawn); TryAcceptPawn(Pawn); ThingOwner.Remove(Pawn)", pawn,
                "same child pawn leaves map and appears under the exact native growth vat holder", () =>
                { vat.TryAcceptPawn(pawn); _growthVat = vat; _vatPawn = pawn; _heldMap = map; return "void"; },
                (before, after, _) => before.Spawned && !after.Spawned &&
                    after.HolderChain.Any(path => path == typeof(Building_GrowthVat).FullName + ":" + vat.ThingID));
        }

        [DebugAction("RWSI Native Acceptance", "Transition: remove child pawn from growth vat", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void RemoveChildPawnFromGrowthVat()
        {
            var vat = _growthVat;
            var pawn = _vatPawn;
            var map = _heldMap;
            if (vat == null || pawn == null || map == null || vat.GetDirectlyHeldThings()?.Contains(pawn) != true)
            { Log.Error("[RWSI Native Acceptance] Place an eligible child pawn in the growth vat first."); return; }
            var cell = CellFinder.RandomClosewalkCellNear(map.Center, map, 12,
                candidate => candidate.Standable(map) && map.thingGrid.ThingsListAt(candidate).Count == 0);
            if (!cell.IsValid) { Log.Error("[RWSI Native Acceptance] No clear cell is available for growth-vat removal."); return; }
            Run("pawn_holder_changed", "holder.growth_vat_to_map", "ThingOwner.Remove(Pawn); GenSpawn.Spawn(Pawn, Cell, Map)", pawn,
                "same child pawn leaves exact growth-vat holder and is spawned on the original map", () =>
                {
                    var removed = vat.GetDirectlyHeldThings()?.Remove(pawn) == true;
                    if (removed) GenSpawn.Spawn(pawn, cell, map, Rot4.Random, WipeMode.Vanish);
                    return removed;
                }, (before, after, result) => Equals(result, true) && before.HolderChain.Any(path => path == typeof(Building_GrowthVat).FullName + ":" + vat.ThingID) &&
                    !after.HolderChain.Any(path => path == typeof(Building_GrowthVat).FullName + ":" + vat.ThingID) && after.Spawned && after.MapId == map.uniqueID);
            _growthVat = null;
            _vatPawn = null;
            _heldMap = null;
        }

        [DebugAction("RWSI Native Acceptance", "Transition: carry pawn", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void CarryPawn()
        {
            var pawns = Find.CurrentMap?.mapPawns.AllPawnsSpawned
                .Where(candidate => candidate.Faction == Faction.OfPlayer && candidate.RaceProps.Humanlike && !candidate.Dead)
                .OrderBy(candidate => candidate.ThingID, StringComparer.Ordinal).Take(2).ToArray();
            if (pawns == null || pawns.Length < 2) { Log.Error("[RWSI Native Acceptance] Two living spawned player humans are required for carry transition."); return; }
            var carrier = pawns[0];
            var carried = pawns[1];
            var map = carrier.Map;
            if (map == null) { Log.Error("[RWSI Native Acceptance] Carrier is not on a map."); return; }
            Run("pawn_holder_changed", "holder.pawn_carry_tracker", "Pawn_CarryTracker.TryStartCarry(Thing, int, bool); TryDropCarriedThing(Cell, ThingPlaceMode, out Thing)", carried,
                "same pawn is held by the exact carrier and later returns to a map cell", () =>
                {
                    var count = carrier.carryTracker.TryStartCarry(carried, 1, false);
                    if (count > 0) { _carrier = carrier; _carriedPawn = carried; _heldMap = map; }
                    return count;
                }, (before, after, result) => Equals(result, 1) && before.Spawned && !after.Spawned &&
                    after.HolderChain.Any(path => path == typeof(Pawn).FullName + ":" + carrier.ThingID), carrier);
        }

        [DebugAction("RWSI Native Acceptance", "Transition: drop carried pawn", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void DropCarriedPawn()
        {
            var carrier = _carrier;
            var carried = _carriedPawn;
            var map = _heldMap;
            if (carrier?.carryTracker?.CarriedThing != carried || carried == null || map == null)
            { Log.Error("[RWSI Native Acceptance] Carry a pawn first."); return; }
            var cell = CellFinder.RandomClosewalkCellNear(carrier!.Position, map, 3,
                candidate => candidate.Standable(map) && map.thingGrid.ThingsListAt(candidate).Count == 0);
            if (!cell.IsValid) { Log.Error("[RWSI Native Acceptance] No adjacent clear cell is available for carried-pawn drop."); return; }
            Run("pawn_holder_changed", "holder.drop_carried_pawn", "Pawn_CarryTracker.TryDropCarriedThing(Cell, ThingPlaceMode, out Thing)", carried,
                "same pawn exits the carrier chain and is spawned on the original map", () =>
                {
                    var dropped = carrier!.carryTracker.TryDropCarriedThing(cell, ThingPlaceMode.Near, out var result);
                    return dropped && ReferenceEquals(result, carried);
                }, (before, after, result) => Equals(result, true) && !before.Spawned && after.Spawned && after.MapId == map.uniqueID);
            _carrier = null;
            _carriedPawn = null;
            _heldMap = null;
        }

        [DebugAction("RWSI Native Acceptance", "Transition: load pawn to transporter", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void LoadPawnToTransporter()
        {
            var pawn = Find.CurrentMap?.mapPawns.AllPawnsSpawned
                .Where(candidate => candidate.Faction == Faction.OfPlayer && candidate.RaceProps.Humanlike && !candidate.Dead)
                .OrderBy(candidate => candidate.ThingID, StringComparer.Ordinal).FirstOrDefault();
            var map = Find.CurrentMap;
            var comp = pawn == null ? null : map?.listerThings.AllThings.Select(thing => thing.TryGetComp<CompTransporter>()).Where(candidate => candidate != null)
                .FirstOrDefault(candidate => candidate.GetDirectlyHeldThings()?.CanAcceptAnyOf(pawn) == true);
            if (pawn == null || map == null || comp == null)
            { Log.Error("[RWSI Native Acceptance] A living player pawn and compatible native CompTransporter are required."); return; }
            Run("pawn_holder_changed", "holder.transport_comp_container", "CompTransporter.GetDirectlyHeldThings().TryAdd(Pawn); ThingOwner.Remove(Pawn); GenSpawn.Spawn(Pawn, Cell, Map)", pawn,
                "same pawn leaves the map and is in the selected native transporter holder", () =>
                {
                    var added = comp.GetDirectlyHeldThings()?.TryAdd(pawn) == true;
                    if (added) { _transporter = comp; _heldPawn = pawn; _heldMap = map; }
                    return added;
                }, (before, after, result) => Equals(result, true) && before.Spawned && !after.Spawned &&
                    after.HolderChain.Any(path => path.EndsWith(":" + comp.parent.ThingID, StringComparison.Ordinal)));
        }

        [DebugAction("RWSI Native Acceptance", "Transition: unload pawn from transporter", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void UnloadPawnFromTransporter()
        {
            var pawn = _heldPawn;
            var comp = _transporter;
            var map = _heldMap;
            if (pawn == null || comp == null || map == null || comp.GetDirectlyHeldThings()?.Contains(pawn) != true)
            { Log.Error("[RWSI Native Acceptance] Load a pawn to a transporter first."); return; }
            var cell = CellFinder.RandomClosewalkCellNear(map.Center, map, 12,
                candidate => candidate.Standable(map) && map.thingGrid.ThingsListAt(candidate).Count == 0);
            if (!cell.IsValid) { Log.Error("[RWSI Native Acceptance] No clear map cell is available for transporter unload."); return; }
            Run("pawn_holder_changed", "holder.transport_comp_to_map", "ThingOwner.Remove(Pawn); GenSpawn.Spawn(Pawn, Cell, Map)", pawn,
                "same pawn leaves exact transporter holder and is spawned on the original map", () =>
                {
                    var removed = comp.GetDirectlyHeldThings()?.Remove(pawn) == true;
                    if (removed) GenSpawn.Spawn(pawn, cell, map, Rot4.Random, WipeMode.Vanish);
                    return removed;
                }, (before, after, result) => Equals(result, true) && !before.Spawned && after.Spawned && after.MapId == map.uniqueID);
            _heldPawn = null;
            _transporter = null;
            _heldMap = null;
        }

        [DebugAction("RWSI Native Acceptance", "Control: add already-held pawn to transporter", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void AddAlreadyHeldPawnControl()
        {
            var pawn = _heldPawn;
            var comp = _transporter;
            if (pawn == null || comp == null || comp.GetDirectlyHeldThings()?.Contains(pawn) != true)
            { Log.Error("[RWSI Native Acceptance] Load a pawn into a transporter first."); return; }
            Run("pawn_holder_changed", "control.holder.add_duplicate_transporter_pawn", "ThingOwner.TryAdd(Pawn)", pawn,
                "already-held pawn remains in the same transporter; duplicate TryAdd returns false", () => comp.GetDirectlyHeldThings()?.TryAdd(pawn) ?? false,
                (before, after, result) => Equals(result, false) && before.HolderChain.SequenceEqual(after.HolderChain), controlOnly: true);
        }

        [DebugAction("RWSI Native Acceptance", "Transition: map pawn to world pool", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void MovePawnToWorldPool()
        {
            var pawn = Find.CurrentMap?.mapPawns.AllPawnsSpawned.Where(candidate => candidate.Faction == Faction.OfPlayer && candidate.RaceProps.Humanlike && !candidate.Dead)
                .OrderBy(candidate => candidate.ThingID, StringComparer.Ordinal).FirstOrDefault();
            if (pawn == null) { Log.Error("[RWSI Native Acceptance] No living spawned player human is available for holder transition."); return; }
            var map = pawn.Map;
            Run("pawn_holder_changed", "holder.map_to_world_pawn_pool", "Thing.DeSpawn(DestroyMode); WorldPawns.PassToWorld(Pawn, PawnDiscardDecideMode)", pawn,
                "same pawn is despawned and appears in the native world-pawn pool", () =>
                {
                    pawn.DeSpawn(DestroyMode.Vanish);
                    Find.WorldPawns.PassToWorld(pawn, PawnDiscardDecideMode.KeepForever);
                    _heldPawn = pawn; _heldMap = map;
                    return "void";
                }, (before, after, _) => before.Spawned && !after.Spawned && after.WorldPool == "world_alive");
        }

        [DebugAction("RWSI Native Acceptance", "Transition: world pool pawn to map", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void RestorePawnToMap()
        {
            var pawn = _heldPawn; var map = _heldMap;
            if (pawn == null || map == null || pawn.Spawned || pawn.Dead) { Log.Error("[RWSI Native Acceptance] Move a living pawn to the world pool first."); return; }
            var cell = CellFinder.RandomClosewalkCellNear(map.Center, map, 12,
                candidate => candidate.Standable(map) && map.thingGrid.ThingsListAt(candidate).Count == 0);
            if (!cell.IsValid) { Log.Error("[RWSI Native Acceptance] No clear map cell exists for holder restoration."); return; }
            Run("pawn_holder_changed", "holder.world_pawn_pool_to_map", "WorldPawns.RemovePawn(Pawn); GenSpawn.Spawn(Pawn, Cell, Map)", pawn,
                "same pawn leaves the world-pawn pool and is spawned on the original map", () =>
                {
                    Find.WorldPawns.RemovePawn(pawn);
                    GenSpawn.Spawn(pawn, cell, map, Rot4.Random, WipeMode.Vanish);
                    return "void";
                }, (before, after, _) => !before.Spawned && before.WorldPool == "world_alive" && after.Spawned && after.MapId == map.uniqueID);
            _heldPawn = null; _heldMap = null;
        }

        [DebugAction("RWSI Native Acceptance", "Transition: hatch fertilized egg", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void HatchFertilizedEgg()
        {
            var map = Find.CurrentMap;
            var egg = map?.listerThings.AllThings.Where(thing => thing.TryGetComp<CompHatcher>() != null)
                .OrderBy(thing => thing.ThingID, StringComparer.Ordinal).FirstOrDefault();
            var hatcher = egg?.TryGetComp<CompHatcher>();
            if (map == null || egg == null || hatcher == null)
            { Log.Error("[RWSI Native Acceptance] No current-map fertilized egg with CompHatcher exists; no birth API was called."); return; }
            var before = HatchObservation.Capture(map, egg);
            try
            {
                var result = AcceptanceTransitionReceipt.RunExternal("pawn_born", "birth.hatch_fertilized_egg",
                    "RimWorld.CompHatcher.Hatch()", egg.ThingID, egg.GetUniqueLoadID(),
                    "native Hatch materializes a new pawn ThingID on the map", () => HatchObservation.Capture(map, egg),
                    () => { hatcher.Hatch(); return "void"; }, () => HatchObservation.Capture(map, egg),
                    (beforeState, afterState, _) =>
                    {
                        var beforeIds = ((HatchObservation)beforeState!).SpawnedPawnThingIds;
                        var afterIds = ((HatchObservation)afterState!).SpawnedPawnThingIds;
                        return afterIds.Except(beforeIds, StringComparer.Ordinal).Any();
                    }, (beforeState, afterState) =>
                    {
                        var beforeIds = new System.Collections.Generic.HashSet<string>(
                            ((HatchObservation)beforeState!).SpawnedPawnThingIds, StringComparer.Ordinal);
                        return ((HatchObservation)afterState!).SpawnedPawns
                            .Where(pawn => !beforeIds.Contains(pawn.ThingId))
                            .OrderBy(pawn => pawn.ThingId, StringComparer.Ordinal).ToArray();
                    });
                Log.Message("[RWSI Native Acceptance] birth.hatch_fertilized_egg action_id=" + result.ActionId +
                    " completed=" + result.Completed + " expected_events=" + result.ExpectedEventCount +
                    " newborn_ids=" + JsonConvert.SerializeObject(result.AffectedSubjects) +
                    " before=" + result.BeforePath + " action=" + result.ActionPath + " after=" + result.AfterPath);
            }
            catch (Exception exception) { Log.Error("[RWSI Native Acceptance] Native hatch failed: " + exception); }
        }

        private static void Run(string family, string actionKey, string nativeApi, Pawn pawn, string expectation,
            Func<object?> action, Func<NativePawnSnapshot, NativePawnSnapshot, object?, bool> predicate, Pawn? related = null, bool controlOnly = false)
        {
            try
            {
                var result = AcceptanceTransitionReceipt.Run(family, actionKey, nativeApi, pawn, related, expectation, action, predicate, controlOnly);
                Log.Message("[RWSI Native Acceptance] " + actionKey + " action_id=" + result.ActionId + " completed=" + result.Completed +
                    " expected_events=" + result.ExpectedEventCount + " before=" + result.BeforePath + " action=" + result.ActionPath + " after=" + result.AfterPath);
            }
            catch (Exception exception) { Log.Error("[RWSI Native Acceptance] " + actionKey + " failed: " + exception); }
        }

        private sealed class HatchObservation
        {
            public string EggThingId = "";
            public string EggUniqueLoadId = "";
            public int MapId;
            public string[] SpawnedPawnThingIds = Array.Empty<string>();
            public NativeSubjectReference[] SpawnedPawns = Array.Empty<NativeSubjectReference>();

            internal static HatchObservation Capture(Map map, Thing egg) => new HatchObservation
            {
                EggThingId = egg.ThingID, EggUniqueLoadId = egg.GetUniqueLoadID(), MapId = map.uniqueID,
                SpawnedPawns = map.mapPawns.AllPawnsSpawned.Select(pawn => new NativeSubjectReference
                    { ThingId = pawn.ThingID, UniqueLoadId = pawn.GetUniqueLoadID() })
                    .OrderBy(pawn => pawn.ThingId, StringComparer.Ordinal).ToArray(),
                SpawnedPawnThingIds = map.mapPawns.AllPawnsSpawned.Select(pawn => pawn.ThingID).OrderBy(id => id, StringComparer.Ordinal).ToArray()
            };
        }
    }
}
