using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using Newtonsoft.Json;
using RimWorld;
using RimWorld.Planet;
using Verse;

namespace Rwsi.NativeAcceptance
{
    internal static class AcceptanceReceiptWriter
    {
        private static readonly BindingFlags InstanceFields = BindingFlags.Instance | BindingFlags.NonPublic;
        private const int MaxHolderDepth = 64;
        private const int MaxHolderVisits = 100000;

        internal static ReceiptResult Write()
        {
            var map = Find.CurrentMap;
            var world = Find.World;
            if (map == null || world?.info == null) throw new InvalidOperationException("Current map or world identity is unavailable.");

            var census = new PawnCensus(new HolderTraversalBudget<IThingHolder>(MaxHolderDepth, MaxHolderVisits));
            var maps = Find.Maps?.ToArray() ?? new Map[0];
            if (maps.Length == 0) census.Diagnostics.Add("loaded map list unavailable or empty");
            if (!maps.Any(candidate => ReferenceEquals(candidate, map)))
                census.Diagnostics.Add("current map was omitted from loaded map list");
            var mapReceipts = new List<MapReceipt>();
            foreach (var loadedMap in maps) mapReceipts.Add(AddMap(census, loadedMap));
            AddWorldPools(census);
            AddWorldObjects(census);

            var payload = new ReceiptPayload
            {
                Schema = "rwsi-native-state-receipt/1",
                CreatedUtc = DateTime.UtcNow.ToString("O"),
                SavedataRoot = GenFilePaths.SaveDataFolderPath,
                GameTicks = Find.TickManager?.TicksGame,
                AbsoluteTicks = Find.TickManager?.TicksAbs,
                WorldSeed = world.info.seedString,
                PersistentRandomValue = world.info.persistentRandomValue,
                NativeBuild = VersionControl.CurrentVersionStringWithRev,
                Map = MapSummary(map),
                Maps = mapReceipts,
                PawnCount = census.Entries.Count,
                Pawns = census.Entries.Values.OrderBy(entry => entry.ThingId, StringComparer.Ordinal).ToList(),
                Diagnostics = census.Diagnostics,
                CensusComplete = census.Diagnostics.Count == 0
            };
            var payloadJson = JsonConvert.SerializeObject(payload, Formatting.Indented);
            var payloadBytes = Encoding.UTF8.GetBytes(payloadJson);
            string payloadHash;
            using (var sha = SHA256.Create())
                payloadHash = BitConverter.ToString(sha.ComputeHash(payloadBytes)).Replace("-", "").ToLowerInvariant();

            var root = GenFilePaths.SaveDataFolderPath;
            var outputDirectory = Path.Combine(root, "NativeAcceptance");
            Directory.CreateDirectory(outputDirectory);
            var fileName = "state-" + DateTime.UtcNow.ToString("yyyyMMddTHHmmss.fffffffZ") + "-" + Guid.NewGuid().ToString("N") + ".json";
            var path = Path.Combine(outputDirectory, fileName);
            var document = JsonConvert.SerializeObject(new ReceiptEnvelope { PayloadSha256 = payloadHash, Payload = payload }, Formatting.Indented);
            var bytes = Encoding.UTF8.GetBytes(document);
            using (var stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read))
            {
                stream.Write(bytes, 0, bytes.Length);
                stream.Flush(true);
            }
            return new ReceiptResult { Path = path, PayloadSha256 = payloadHash, PawnCount = payload.PawnCount, CensusComplete = payload.CensusComplete };
        }

        private static MapReceipt AddMap(PawnCensus census, Map map)
        {
            var directThings = map.listerThings.AllThings;
            foreach (var thing in map.listerThings.AllThings)
            {
                var rootPath = "map:" + map.uniqueID;
                var thingPath = rootPath + "/" + ThingIdentity(thing);
                if (thing is Pawn pawn) census.Add(pawn, rootPath, thingPath);
                if (thing is IThingHolder holder)
                    WalkHolder(census, holder, rootPath, thingPath, 0);
            }
            return MapSummary(map, directThings.Count);
        }

        private static MapReceipt MapSummary(Map map, int? directThingCount = null) => new MapReceipt
        {
            UniqueId = map.uniqueID,
            Tile = map.Tile,
            Width = map.Size.x,
            Height = map.Size.z,
            Current = ReferenceEquals(map, Find.CurrentMap),
            DirectThingCount = directThingCount ?? map.listerThings.AllThings.Count,
            SpawnedPawnCount = map.mapPawns.AllPawnsSpawned.Count
        };

        private static void AddWorldPools(PawnCensus census)
        {
            var pools = Find.WorldPawns;
            if (pools == null) { census.Diagnostics.Add("world pawn pools unavailable"); return; }
            AddPool(census, pools, "pawnsAlive", "world_alive");
            AddPool(census, pools, "pawnsMothballed", "world_mothballed");
            AddPool(census, pools, "pawnsDead", "world_dead");
        }

        private static void AddPool(PawnCensus census, object owner, string fieldName, string source)
        {
            var field = owner.GetType().GetField(fieldName, InstanceFields);
            var values = field?.GetValue(owner) as System.Collections.IEnumerable;
            if (values == null) { census.Diagnostics.Add("world pawn pool unavailable: " + fieldName); return; }
            foreach (var value in values)
                if (value is Pawn pawn) census.Add(pawn, source, source + "/" + pawn.ThingID);
        }

        private static void AddWorldObjects(PawnCensus census)
        {
            var worldObjects = Find.WorldObjects?.AllWorldObjects;
            if (worldObjects == null) { census.Diagnostics.Add("world objects unavailable"); return; }
            foreach (var worldObject in worldObjects)
                if (worldObject is IThingHolder holder)
                {
                    var source = "world_object:" + worldObject.GetType().Name + ":" + worldObject.ID;
                    WalkHolder(census, holder, source, source + "/" + HolderIdentity(census, holder), 0);
                }
        }

        private static void WalkHolder(PawnCensus census, IThingHolder holder, string source, string holderPath, int depth)
        {
            var problem = census.Traversal.TryEnter(holder, depth);
            if (problem != null)
            {
                census.Diagnostics.Add("holder traversal stopped: " + problem + " at " + holderPath +
                    " (depth=" + depth + ", visits=" + census.Traversal.Visited + ")");
                return;
            }
            try
            {
                var directlyHeld = holder.GetDirectlyHeldThings();
                // The native holder contract uses null for "no direct owner"; child holders are still traversed below.
                if (directlyHeld != null)
                {
                    foreach (var thing in directlyHeld)
                    {
                        var thingPath = holderPath + "/" + ThingIdentity(thing);
                        if (thing is Pawn pawn) census.Add(pawn, source, thingPath);
                        if (thing is IThingHolder nested)
                            WalkHolder(census, nested, source, thingPath, depth + 1);
                    }
                }
                var children = new List<IThingHolder>();
                holder.GetChildHolders(children);
                foreach (var child in children)
                    WalkHolder(census, child, source, holderPath + "/" + HolderIdentity(census, child), depth + 1);
            }
            catch (Exception ex)
            {
                census.Diagnostics.Add("holder traversal failed for " + holder.GetType().FullName + ": " + ex.GetType().Name);
            }
            finally { census.Traversal.Exit(holder); }
        }

        private static string HolderIdentity(PawnCensus census, IThingHolder holder)
        {
            var identity = StableHolderIdentity(holder);
            if (identity != null) return identity;
            census.Diagnostics.Add("holder identity unavailable for " + holder.GetType().FullName + "; unstable object references are not used");
            return holder.GetType().FullName + ":identity-unavailable";
        }

        internal static string? StableHolderIdentity(IThingHolder holder) =>
            ResolveHolderIdentity(holder, new HashSet<IThingHolder>(), 0);

        private static string? ResolveHolderIdentity(IThingHolder holder, HashSet<IThingHolder> visited, int depth)
        {
            if (depth >= MaxHolderDepth || !visited.Add(holder)) return null;
            var typeName = holder.GetType().FullName ?? holder.GetType().Name;
            var worldId = (holder as WorldObject)?.ID.ToString(System.Globalization.CultureInfo.InvariantCulture);
            if (holder is World world && world.info != null && !string.IsNullOrEmpty(world.info.seedString))
            {
                var key = world.info.seedString + "\n" + world.info.persistentRandomValue.ToString(System.Globalization.CultureInfo.InvariantCulture);
                using (var hash = SHA256.Create())
                    worldId = "campaign-" + BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(key))).Replace("-", "").ToLowerInvariant();
            }
            var direct = HolderIdentityOracle.StableIdentity(typeName, (holder as Thing)?.ThingID,
                worldId, (holder as Map)?.uniqueID);
            if (direct != null) return direct;
            var parent = holder.ParentHolder;
            return parent == null ? null : HolderIdentityOracle.StableIdentity(typeName, null, null, null,
                ResolveHolderIdentity(parent, visited, depth + 1));
        }

        private static string ThingIdentity(Thing thing) => thing.GetType().Name + ":" + thing.ThingID;

        private sealed class PawnCensus
        {
            internal readonly HolderTraversalBudget<IThingHolder> Traversal;
            internal readonly Dictionary<string, PawnReceipt> Entries = new Dictionary<string, PawnReceipt>(StringComparer.Ordinal);
            internal readonly List<string> Diagnostics = new List<string>();

            internal PawnCensus(HolderTraversalBudget<IThingHolder> traversal) => Traversal = traversal;

            internal void Add(Pawn pawn, string source, string holderPath)
            {
                var id = pawn.ThingID;
                if (string.IsNullOrWhiteSpace(id))
                {
                    Diagnostics.Add("pawn had no stable ThingID in source " + source);
                    return;
                }
                if (!Entries.TryGetValue(id, out var receipt))
                {
                    receipt = new PawnReceipt
                    {
                        ThingId = id,
                        ThingIdNumber = pawn.thingIDNumber,
                        Label = pawn.LabelShortCap,
                        KindDef = pawn.kindDef?.defName,
                        RaceDef = pawn.def?.defName,
                        FactionDef = pawn.Faction?.def?.defName,
                        FactionUniqueLoadId = pawn.Faction?.GetUniqueLoadID(),
                        FactionName = pawn.Faction?.Name,
                        Dead = pawn.Dead,
                        Downed = pawn.Downed,
                        Spawned = pawn.Spawned,
                        MapId = pawn.Map?.uniqueID,
                        Position = pawn.PositionHeld.IsValid ? pawn.PositionHeld.ToString() : null,
                        WorldPool = WorldPool(pawn),
                        Hediffs = pawn.health?.hediffSet?.hediffs?.Select(hediff => new HediffReceipt
                        {
                            LoadId = hediff.loadID,
                            Def = hediff.def?.defName,
                            BodyPart = hediff.Part?.def?.defName,
                            BodyPartIndex = hediff.Part == null ? (int?)null : pawn.RaceProps.body.AllParts.IndexOf(hediff.Part),
                            Severity = hediff.Severity
                        }).OrderBy(value => value.Def, StringComparer.Ordinal).ThenBy(value => value.LoadId).ToList() ?? new List<HediffReceipt>(),
                        Relations = pawn.relations?.DirectRelations?.Select(relation => new RelationReceipt
                        {
                            Kind = relation.def?.defName,
                            OtherPawn = relation.otherPawn?.GetUniqueLoadID(),
                            OtherPawnThingId = relation.otherPawn?.ThingID
                        }).OrderBy(relation => relation.Kind, StringComparer.Ordinal).ThenBy(relation => relation.OtherPawn, StringComparer.Ordinal).ToList() ?? new List<RelationReceipt>(),
                        Xenotype = pawn.genes?.Xenotype?.defName,
                        Genes = pawn.genes?.GenesListForReading?.Select(gene => new GeneReceipt
                        {
                            LoadId = gene.loadID,
                            Def = gene.def?.defName,
                            Xenogene = pawn.genes.IsXenogene(gene)
                        }).OrderBy(value => value.Def, StringComparer.Ordinal).ThenBy(value => value.Xenogene).ToList() ?? new List<GeneReceipt>(),
                        GuestStatus = pawn.guest?.GuestStatus.ToString(),
                        GuestHostFactionUniqueLoadId = pawn.guest?.HostFaction?.GetUniqueLoadID(),
                        GuestHostFactionDef = pawn.guest?.HostFaction?.def?.defName,
                        GuestIsPrisonerOfColony = pawn.IsPrisonerOfColony,
                        GuestIsSlaveOfColony = pawn.IsSlaveOfColony,
                        GuestReleased = pawn.guest?.Released,
                        IdeologyRoleDef = ReadIdeologyRole(pawn),
                        RoyalTitleDef = ReadRoyalTitle(pawn)
                    };
                    Entries.Add(id, receipt);
                }
                receipt.Sources.Add(source);
                receipt.HolderPaths.Add(holderPath);
            }

            private static string? ReadIdeologyRole(Pawn pawn)
            {
                try { return pawn.ideo?.Ideo?.GetRole(pawn)?.def?.defName; }
                catch { return null; }
            }

            private static string? ReadRoyalTitle(Pawn pawn)
            {
                try { return pawn.royalty?.MostSeniorTitle?.def?.defName; }
                catch { return null; }
            }

            private static string WorldPool(Pawn pawn)
            {
                var pools = Find.WorldPawns;
                if (pools == null) return "unavailable";
                foreach (var tuple in new[] { Tuple.Create("pawnsAlive", "world_alive"), Tuple.Create("pawnsMothballed", "world_mothballed"), Tuple.Create("pawnsDead", "world_dead") })
                {
                    var values = pools.GetType().GetField(tuple.Item1, InstanceFields)?.GetValue(pools) as System.Collections.IEnumerable;
                    if (values == null) continue;
                    foreach (var value in values) if (ReferenceEquals(value, pawn)) return tuple.Item2;
                }
                return "not_in_world_pool";
            }
        }

        private sealed class ReceiptEnvelope
        {
            [JsonProperty("payload_sha256")] public string PayloadSha256 = "";
            [JsonProperty("payload")] public ReceiptPayload Payload = new ReceiptPayload();
        }

        private sealed class ReceiptPayload
        {
            [JsonProperty("schema")] public string Schema = "";
            [JsonProperty("created_utc")] public string CreatedUtc = "";
            [JsonProperty("savedata_root")] public string SavedataRoot = "";
            [JsonProperty("game_ticks")] public int? GameTicks;
            [JsonProperty("absolute_ticks")] public int? AbsoluteTicks;
            [JsonProperty("world_seed")] public string WorldSeed = "";
            [JsonProperty("persistent_random_value")] public int PersistentRandomValue;
            [JsonProperty("native_build")] public string NativeBuild = "";
            [JsonProperty("map")] public MapReceipt Map = new MapReceipt();
            [JsonProperty("maps")] public List<MapReceipt> Maps = new List<MapReceipt>();
            [JsonProperty("pawn_count")] public int PawnCount;
            [JsonProperty("pawns")] public List<PawnReceipt> Pawns = new List<PawnReceipt>();
            [JsonProperty("census_complete")] public bool CensusComplete;
            [JsonProperty("diagnostics")] public List<string> Diagnostics = new List<string>();
        }

        private sealed class MapReceipt
        {
            [JsonProperty("unique_id")] public int UniqueId;
            [JsonProperty("tile")] public int Tile;
            [JsonProperty("width")] public int Width;
            [JsonProperty("height")] public int Height;
            [JsonProperty("current")] public bool Current;
            [JsonProperty("direct_thing_count")] public int DirectThingCount;
            [JsonProperty("spawned_pawn_count")] public int SpawnedPawnCount;
        }

        private sealed class PawnReceipt
        {
            [JsonProperty("thing_id")] public string ThingId = "";
            [JsonProperty("thing_id_number")] public int ThingIdNumber;
            [JsonProperty("label")] public string? Label;
            [JsonProperty("kind_def")] public string? KindDef;
            [JsonProperty("race_def")] public string? RaceDef;
            [JsonProperty("faction_def")] public string? FactionDef;
            [JsonProperty("faction_unique_load_id")] public string? FactionUniqueLoadId;
            [JsonProperty("faction_name")] public string? FactionName;
            [JsonProperty("dead")] public bool Dead;
            [JsonProperty("downed")] public bool Downed;
            [JsonProperty("spawned")] public bool Spawned;
            [JsonProperty("map_id")] public int? MapId;
            [JsonProperty("position")] public string? Position;
            [JsonProperty("world_pool")] public string WorldPool = "";
            [JsonProperty("sources")] public SortedSet<string> Sources = new SortedSet<string>(StringComparer.Ordinal);
            [JsonProperty("holder_paths")] public SortedSet<string> HolderPaths = new SortedSet<string>(StringComparer.Ordinal);
            [JsonProperty("hediffs")] public List<HediffReceipt> Hediffs = new List<HediffReceipt>();
            [JsonProperty("direct_relations")] public List<RelationReceipt> Relations = new List<RelationReceipt>();
            [JsonProperty("xenotype")] public string? Xenotype;
            [JsonProperty("genes")] public List<GeneReceipt> Genes = new List<GeneReceipt>();
            [JsonProperty("guest_status")] public string? GuestStatus;
            [JsonProperty("guest_host_faction_unique_load_id")] public string? GuestHostFactionUniqueLoadId;
            [JsonProperty("guest_host_faction_def")] public string? GuestHostFactionDef;
            [JsonProperty("guest_prisoner_of_colony")] public bool GuestIsPrisonerOfColony;
            [JsonProperty("guest_slave_of_colony")] public bool GuestIsSlaveOfColony;
            [JsonProperty("guest_released")] public bool? GuestReleased;
            [JsonProperty("ideology_role_def")] public string? IdeologyRoleDef;
            [JsonProperty("royal_title_def")] public string? RoyalTitleDef;
        }

        private sealed class HediffReceipt
        {
            [JsonProperty("load_id")] public int LoadId;
            [JsonProperty("def")] public string? Def;
            [JsonProperty("body_part_def")] public string? BodyPart;
            [JsonProperty("body_part_index")] public int? BodyPartIndex;
            [JsonProperty("severity")] public float Severity;
        }

        private sealed class GeneReceipt
        {
            [JsonProperty("load_id")] public int LoadId;
            [JsonProperty("def")] public string? Def;
            [JsonProperty("xenogene")] public bool Xenogene;
        }

        private sealed class RelationReceipt
        {
            [JsonProperty("kind")] public string? Kind;
            [JsonProperty("other_pawn")] public string? OtherPawn;
            [JsonProperty("other_pawn_thing_id")] public string? OtherPawnThingId;
        }
    }

    internal sealed class ReceiptResult
    {
        internal string Path = "";
        internal string PayloadSha256 = "";
        internal int PawnCount;
        internal bool CensusComplete;
    }
}
