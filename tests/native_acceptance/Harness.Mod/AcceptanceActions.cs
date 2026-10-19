using System;
using System.Collections.Generic;
using System.Linq;
using LudeonTK;
using RimWorld;
using Verse;

namespace Rwsi.NativeAcceptance
{
    /// <summary>Explicit, guarded game-thread entry points used only by disposable native acceptance.</summary>
    public static class AcceptanceActions
    {
        private const int HumanTarget = 10;
        private const int AnimalTarget = 10;
        private const int MechTarget = 5;

        [DebugAction("RWSI Native Acceptance", "Prepare fixture", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void PrepareFixture()
        {
            if (!AcceptanceRuntime.TryAuthorize(out var error))
            {
                Log.Error("[RWSI Native Acceptance] Prepare fixture rejected: " + error);
                return;
            }

            var map = Find.CurrentMap;
            if (map == null || Faction.OfPlayer == null)
            {
                Log.Error("[RWSI Native Acceptance] Prepare fixture rejected: current map or player faction is missing.");
                return;
            }

            var outcomes = new List<string>();
            EnsurePopulation(map, "humanlike", HumanTarget,
                pawn => pawn.RaceProps.Humanlike, PawnKindDefOf.Colonist, outcomes);

            var animalKind = FirstKind(kind => kind.race.race.Animal);
            EnsurePopulation(map, "animal", AnimalTarget,
                pawn => pawn.RaceProps.Animal, animalKind, outcomes);

            var mechKind = FirstKind(kind => kind.race.race.IsMechanoid);
            EnsurePopulation(map, "mechanoid", MechTarget,
                pawn => pawn.RaceProps.IsMechanoid, mechKind, outcomes);

            var counts = PopulationCounts(map);
            var complete = counts.Humans >= HumanTarget && counts.Animals >= AnimalTarget && counts.Mechs >= MechTarget;
            Log.Message("[RWSI Native Acceptance] Prepare fixture " + (complete ? "completed" : "incomplete") +
                "; current-map player population humans=" + counts.Humans + "/" + HumanTarget +
                ", animals=" + counts.Animals + "/" + AnimalTarget +
                ", mechs=" + counts.Mechs + "/" + MechTarget +
                ". Actions: " + string.Join(" | ", outcomes));
        }

        [DebugAction("RWSI Native Acceptance", "Write state receipt", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void WriteStateReceipt()
        {
            if (!AcceptanceRuntime.TryAuthorize(out var error))
            {
                Log.Error("[RWSI Native Acceptance] State receipt rejected: " + error);
                return;
            }
            try
            {
                var receipt = AcceptanceReceiptWriter.Write();
                Log.Message("[RWSI Native Acceptance] State receipt written: " + receipt.Path +
                    " payload_sha256=" + receipt.PayloadSha256 + " pawn_count=" + receipt.PawnCount +
                    " census_complete=" + receipt.CensusComplete);
            }
            catch (Exception ex)
            {
                Log.Error("[RWSI Native Acceptance] State receipt failed: " + ex.GetType().Name + ": " + ex.Message);
            }
        }

        private static void EnsurePopulation(Map map, string label, int target,
            Func<Pawn, bool> category, PawnKindDef? kind, List<string> outcomes)
        {
            var existing = map.mapPawns.AllPawnsSpawned.Count(pawn => pawn.Faction == Faction.OfPlayer && category(pawn));
            if (existing >= target)
            {
                outcomes.Add(label + " already at or above target (" + existing + ")");
                return;
            }
            if (kind == null)
            {
                outcomes.Add(label + " incomplete: no matching native PawnKindDef exists; current=" + existing);
                return;
            }

            for (var i = existing; i < target; i++)
            {
                var pawn = PawnGenerator.GeneratePawn(kind, Faction.OfPlayer);
                if (pawn == null)
                {
                    outcomes.Add(label + " incomplete: native PawnGenerator returned null at " + i + "/" + target);
                    return;
                }
                var cell = CellFinder.RandomClosewalkCellNear(map.Center, map, 12,
                    candidate => candidate.Standable(map) && map.thingGrid.ThingsListAt(candidate).Count == 0);
                if (!cell.IsValid)
                {
                    outcomes.Add(label + " incomplete: no clear standable cell at " + i + "/" + target);
                    return;
                }
                try
                {
                    GenSpawn.Spawn(pawn, cell, map, Rot4.Random, WipeMode.Vanish);
                }
                catch (Exception ex)
                {
                    outcomes.Add(label + " incomplete: native GenSpawn failed at " + i + "/" + target + " with " + ex.GetType().Name);
                    return;
                }
                if (!pawn.Spawned || pawn.Map != map || pawn.Faction != Faction.OfPlayer || !category(pawn))
                {
                    outcomes.Add(label + " incomplete: generated pawn did not materialize as the requested player pawn at " + i + "/" + target);
                    return;
                }
            }
            outcomes.Add(label + " spawned " + (target - existing) + " using native PawnGenerator and GenSpawn; kind=" + kind.defName);
        }

        private static PawnKindDef? FirstKind(Func<PawnKindDef, bool> predicate) =>
            DefDatabase<PawnKindDef>.AllDefsListForReading
                .Where(kind => kind?.race?.race != null && predicate(kind))
                .OrderBy(kind => kind.defName, StringComparer.Ordinal)
                .FirstOrDefault();

        private static Population PopulationCounts(Map map)
        {
            var playerPawns = map.mapPawns.AllPawnsSpawned.Where(pawn => pawn.Faction == Faction.OfPlayer).ToArray();
            return new Population
            {
                Humans = playerPawns.Count(pawn => pawn.RaceProps.Humanlike),
                Animals = playerPawns.Count(pawn => pawn.RaceProps.Animal),
                Mechs = playerPawns.Count(pawn => pawn.RaceProps.IsMechanoid)
            };
        }

        private sealed class Population
        {
            internal int Humans;
            internal int Animals;
            internal int Mechs;
        }
    }
}
