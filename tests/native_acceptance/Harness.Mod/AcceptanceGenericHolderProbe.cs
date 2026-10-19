using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using HarmonyLib;
using LudeonTK;
using Newtonsoft.Json;
using RimWorld;
using Verse;

namespace Rwsi.NativeAcceptance
{
    /// <summary>Independent native regression for generic owner semantics and callback multiplicity.</summary>
    public static class AcceptanceGenericHolderProbe
    {
        private const string PatchOwner = "rwsi.nativeacceptance.generic-holder-probe";
        private static readonly Dictionary<Thing, int[]> Counts = new Dictionary<Thing, int[]>();
        private static readonly List<object> Observations = new List<object>();

        [DebugAction("RWSI Native Acceptance", "Control: generic holder native behavior", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void Run()
        {
            if (!AcceptanceRuntime.TryAuthorize(out var error))
            { Log.Error("[RWSI Native Acceptance] Generic holder probe rejected: " + error); return; }
            var map = Find.CurrentMap!;
            var wood = ThingMaker.MakeThing(ThingDefOf.WoodLog);
            var pawn = PawnGenerator.GeneratePawn(PawnKindDefOf.Colonist, Faction.OfPlayer);
            var added = typeof(ThingOwner).GetMethod("NotifyAdded", BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public,
                null, new[] { typeof(Thing) }, null)!;
            var removed = typeof(ThingOwner).GetMethod("NotifyRemoved", BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public,
                null, new[] { typeof(Thing) }, null)!;
            var harmony = new Harmony(PatchOwner);
            var outcomes = new Dictionary<string, bool>();
            string? failure = null;
            var cleanupPassed = true;
            Counts.Clear(); Observations.Clear();
            Counts.Add(wood, new int[2]); Counts.Add(pawn, new int[2]);
            try
            {
                harmony.Patch(added, new HarmonyMethod(typeof(AcceptanceGenericHolderProbe), nameof(Added)));
                harmony.Patch(removed, new HarmonyMethod(typeof(AcceptanceGenericHolderProbe), nameof(Removed)));
                var things = new ThingOwner<Thing>(null, false, LookMode.Deep);
                var pawns = new ThingOwner<Pawn>(null, false, LookMode.Deep);
                var withComps = new ThingOwner<ThingWithComps>(null, false, LookMode.Deep);
                outcomes["pawn_owner_rejects_nonpawn"] = !pawns.TryAdd(wood, false) && pawns.Count == 0 && wood.holdingOwner == null;
                outcomes["thing_owner_accepts_nonpawn"] = things.TryAdd(wood, false) && things.Contains(wood) && wood.holdingOwner == things;
                outcomes["duplicate_nonpawn_add_is_noop"] = !things.TryAdd(wood, false) && things.Count == 1;
                outcomes["thing_owner_removes_exact_nonpawn"] = things.Remove(wood) && things.Count == 0 && wood.holdingOwner == null;
                outcomes["pawn_owner_accepts_pawn"] = pawns.TryAdd(pawn, false) && pawns.Contains(pawn) && pawn.holdingOwner == pawns;
                outcomes["duplicate_pawn_add_is_noop"] = !pawns.TryAdd(pawn, false) && pawns.Count == 1;
                outcomes["pawn_owner_removes_exact_pawn"] = pawns.Remove(pawn) && pawns.Count == 0 && pawn.holdingOwner == null;
                outcomes["comps_owner_accepts_pawn"] = withComps.TryAdd(pawn, false) && withComps.Contains(pawn) && pawn.holdingOwner == withComps;
                outcomes["comps_owner_removes_exact_pawn"] = withComps.Remove(pawn) && withComps.Count == 0 && pawn.holdingOwner == null;
                var cell = map.AllCells.First(c => c.Standable(map) && c.GetFirstPawn(map) == null);
                var spawned = GenSpawn.Spawn(wood, cell, map);
                outcomes["real_map_spawn_retains_nonpawn"] = ReferenceEquals(spawned, wood) && wood.Spawned &&
                    wood.Map == map && map.spawnedThings.Contains(wood);
                wood.Destroy(DestroyMode.Vanish);
                outcomes["real_map_remove_retains_semantics"] = !wood.Spawned && !map.spawnedThings.Contains(wood);
                outcomes["wood_callbacks_exactly_two_each"] = Counts[wood][0] == 2 && Counts[wood][1] == 2;
                outcomes["pawn_callbacks_exactly_two_each"] = Counts[pawn][0] == 2 && Counts[pawn][1] == 2;
            }
            catch (Exception exception) { failure = exception.ToString(); }
            finally
            {
                void Cleanup(Action action)
                {
                    try { action(); }
                    catch (Exception exception)
                    { cleanupPassed = false; failure = (failure ?? "") + "\ncleanup: " + exception; }
                }
                Cleanup(() => harmony.Unpatch(added, HarmonyPatchType.Prefix, PatchOwner));
                Cleanup(() => harmony.Unpatch(removed, HarmonyPatchType.Prefix, PatchOwner));
                Cleanup(() =>
                {
                    if (wood.holdingOwner != null && !wood.holdingOwner.Remove(wood))
                        throw new InvalidOperationException("Probe Wood could not be removed from its synthetic owner.");
                    if (!wood.Destroyed) wood.Destroy(DestroyMode.Vanish);
                });
                Cleanup(() =>
                {
                    if (pawn.holdingOwner != null && !pawn.holdingOwner.Remove(pawn))
                        throw new InvalidOperationException("Probe Pawn could not be removed from its synthetic owner.");
                });
            }
            var payload = new { schema = "rwsi-native-generic-holder-probe/1", action_id = Guid.NewGuid().ToString("D"),
                created_utc = DateTime.UtcNow.ToString("O"), game_ticks = Find.TickManager?.TicksGame,
                native_build = VersionControl.CurrentVersionStringWithRev, savedata_root = GenFilePaths.SaveDataFolderPath,
                world_seed = Find.World.info.seedString, world_id = Find.World.info.persistentRandomValue,
                wood_id = wood.GetUniqueLoadID(), pawn_id = pawn.GetUniqueLoadID(), outcomes, exception = failure,
                callbacks = Observations.ToArray(), cleanup_passed = cleanupPassed,
                control_passed = failure == null && cleanupPassed && outcomes.Count == 13 && outcomes.Values.All(x => x),
                acceptance_limit = "native behavior control only; does not certify all holder families, durability or timing" };
            var payloadText = JsonConvert.SerializeObject(payload, Formatting.Indented);
            string hash;
            using (var sha = SHA256.Create()) hash = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(payloadText))).Replace("-", "").ToLowerInvariant();
            var bytes = Encoding.UTF8.GetBytes(JsonConvert.SerializeObject(new { payload_sha256 = hash, payload }, Formatting.Indented));
            var directory = Path.Combine(GenFilePaths.SaveDataFolderPath, "NativeAcceptance", "generic-holder-probes");
            Directory.CreateDirectory(directory);
            var path = Path.Combine(directory, payload.action_id + ".json");
            using (var stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read))
            { stream.Write(bytes, 0, bytes.Length); stream.Flush(true); }
            Counts.Clear(); Observations.Clear();
            Log.Message("[RWSI Native Acceptance] Generic holder probe control_passed=" + payload.control_passed + " sha256=" + hash + " receipt=" + path);
        }

        private static void Added(ThingOwner __instance, Thing __0) => Observe(__instance, __0, 0);
        private static void Removed(ThingOwner __instance, Thing __0) => Observe(__instance, __0, 1);
        private static void Observe(ThingOwner owner, Thing item, int index)
        {
            if (!Counts.TryGetValue(item, out var counts)) return;
            counts[index]++;
            Observations.Add(new { callback = index == 0 ? "added" : "removed", item_id = item.GetUniqueLoadID(),
                owner_type = owner.GetType().FullName, parent_type = owner.Owner?.GetType().FullName,
                holding_owner_matches = ReferenceEquals(item.holdingOwner, owner), spawned = item.Spawned,
                game_ticks = Find.TickManager?.TicksGame });
        }
    }
}
