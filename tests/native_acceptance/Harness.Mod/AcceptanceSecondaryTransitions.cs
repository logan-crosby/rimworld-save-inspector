using System;
using System.Linq;
using LudeonTK;
using RimWorld;
using Verse;

namespace Rwsi.NativeAcceptance
{
    public static class AcceptanceSecondaryTransitions
    {
        private static Pawn? _factionPawn;
        private static Faction? _priorFaction;
        private static Faction? _assignedFaction;
        private static Pawn? _guestPawn;
        private static Faction? _guestHost;
        private static Faction? _initialGuestHost;
        private static Faction? _alternateGuestHost;
        private static GuestStatus? _guestStatus;
        private static Pawn? _genePawn;
        private static Gene? _gene;
        private static Pawn? _rolePawn;
        private static Precept_Role? _role;

        [DebugAction("RWSI Native Acceptance", "Transition: change faction", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void ChangeFaction()
        {
            var pawn = LiveHuman();
            var faction = Find.FactionManager?.AllFactions.Where(candidate => candidate != null && candidate != pawn?.Faction)
                .OrderBy(candidate => candidate.GetUniqueLoadID(), StringComparer.Ordinal).FirstOrDefault();
            if (pawn == null || faction == null) { Log.Error("[RWSI Native Acceptance] No live human/distinct faction pair is available."); return; }
            var original = pawn.Faction;
            Run("pawn_faction_changed", "faction.set_distinct_faction", "Verse.Pawn.SetFaction(Faction, Pawn)", pawn,
                "faction unique load ID changes", () => { pawn.SetFaction(faction, null); _factionPawn = pawn; _priorFaction = original; _assignedFaction = faction; return "void"; },
                (before, after, _) => before.FactionId != null && after.FactionId != null && before.FactionId != after.FactionId);
        }

        [DebugAction("RWSI Native Acceptance", "Transition: restore faction", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void RestoreFaction()
        {
            var pawn = _factionPawn; var faction = _priorFaction;
            if (pawn == null || faction == null || pawn.Faction == faction) { Log.Error("[RWSI Native Acceptance] No changed faction to restore."); return; }
            Run("pawn_faction_changed", "faction.restore_original_faction", "Verse.Pawn.SetFaction(Faction, Pawn)", pawn,
                "original faction unique load ID is restored", () => { pawn.SetFaction(faction, null); return "void"; },
                (before, after, _) => before.FactionId != after.FactionId && after.FactionId == faction.GetUniqueLoadID());
            _factionPawn = null; _priorFaction = null; _assignedFaction = null;
        }

        [DebugAction("RWSI Native Acceptance", "Control: set same faction", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void SameFactionControl()
        {
            var pawn = LiveHuman(); var faction = pawn?.Faction;
            if (pawn == null || faction == null) { Log.Error("[RWSI Native Acceptance] No factioned live human is available."); return; }
            Run("pawn_faction_changed", "control.faction.set_same_faction", "Verse.Pawn.SetFaction(Faction, Pawn)", pawn,
                "same faction unique load ID remains unchanged", () => { pawn.SetFaction(faction, null); return "void"; },
                (before, after, _) => before.FactionId == after.FactionId, true);
        }

        [DebugAction("RWSI Native Acceptance", "Transition: set guest status", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void SetGuestStatus()
        {
            var pawn = LiveHuman();
            var host = Find.FactionManager?.AllFactions.Where(candidate => candidate != null && candidate != pawn?.Faction)
                .OrderBy(candidate => candidate.GetUniqueLoadID(), StringComparer.Ordinal).FirstOrDefault();
            if (pawn?.guest == null || host == null || !Enum.GetNames(typeof(GuestStatus)).Contains("Guest"))
            { Log.Error("[RWSI Native Acceptance] No valid guest tracker, distinct host faction, or GuestStatus.Guest value is available."); return; }
            var requested = (GuestStatus)Enum.Parse(typeof(GuestStatus), "Guest");
            _alternateGuestHost = Find.FactionManager?.AllFactions.Where(candidate => candidate != null && candidate != pawn.Faction && candidate != host)
                .OrderBy(candidate => candidate.GetUniqueLoadID(), StringComparer.Ordinal).FirstOrDefault();
            Run("pawn_guest_status_changed", "guest.set_guest_status", "RimWorld.Pawn_GuestTracker.SetGuestStatus(Faction, GuestStatus)", pawn,
                "complete guest status tuple changes to requested host and status", () =>
                { pawn.guest.SetGuestStatus(host, requested); _guestPawn = pawn; _guestHost = host; _initialGuestHost = host; _guestStatus = requested; return "void"; },
                (before, after, _) => !GuestEquals(before.Guest, after.Guest));
        }

        [DebugAction("RWSI Native Acceptance", "Transition: toggle guest host", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void ToggleGuestHost()
        {
            var pawn = _guestPawn; var host = pawn?.guest?.HostFaction == _initialGuestHost ? _alternateGuestHost : _initialGuestHost; var status = _guestStatus;
            if (pawn?.guest == null || host == null || status == null || pawn.guest.HostFaction == host)
            { Log.Error("[RWSI Native Acceptance] Set guest status and ensure two distinct host factions exist first."); return; }
            Run("pawn_guest_status_changed", "guest.toggle_distinct_host", "RimWorld.Pawn_GuestTracker.SetGuestStatus(Faction, GuestStatus)", pawn,
                "host faction unique ID changes in the complete guest tuple", () => { pawn.guest.SetGuestStatus(host, status.Value); _guestHost = host; return "void"; },
                (before, after, _) => !GuestEquals(before.Guest, after.Guest));
        }

        [DebugAction("RWSI Native Acceptance", "Control: repeat guest status", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void RepeatGuestStatusControl()
        {
            var pawn = _guestPawn; var host = _guestHost; var status = _guestStatus;
            if (pawn?.guest == null || host == null || status == null) { Log.Error("[RWSI Native Acceptance] Set guest status first."); return; }
            Run("pawn_guest_status_changed", "control.guest.repeat_identical_status", "RimWorld.Pawn_GuestTracker.SetGuestStatus(Faction, GuestStatus)", pawn,
                "complete guest state tuple remains unchanged", () => { pawn.guest.SetGuestStatus(host, status.Value); return "void"; },
                (before, after, _) => GuestEquals(before.Guest, after.Guest), true);
        }

        [DebugAction("RWSI Native Acceptance", "Transition: add gene", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void AddGene()
        {
            var pawn = LiveHuman();
            var geneDef = pawn?.genes == null ? null : DefDatabase<GeneDef>.AllDefsListForReading
                .Where(candidate => candidate != null && !pawn.genes.GenesListForReading.Any(gene => gene.def == candidate))
                .OrderBy(candidate => candidate.defName, StringComparer.Ordinal).FirstOrDefault();
            if (pawn?.genes == null || geneDef == null) { Log.Error("[RWSI Native Acceptance] No absent native gene definition is compatible with the selected pawn."); return; }
            Gene? created = null;
            Run("pawn_genes_changed", "genes.add_endogene", "RimWorld.Pawn_GeneTracker.AddGene(GeneDef, Boolean)", pawn,
                "the exact newly added gene definition and membership tuple appears", () =>
                { pawn.genes.AddGene(geneDef, false); created = pawn.genes.GenesListForReading.FirstOrDefault(gene => gene.def == geneDef); _genePawn = pawn; _gene = created; return created?.loadID.ToString() ?? "no_gene_returned"; },
                (before, after, _) => created != null && !before.Genes.Any(item => item.Def == geneDef.defName) && after.Genes.Any(item => item.LoadId == created.loadID && item.Def == geneDef.defName));
        }

        [DebugAction("RWSI Native Acceptance", "Transition: remove exact gene", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void RemoveExactGene()
        {
            var pawn = _genePawn; var gene = _gene;
            if (pawn?.genes == null || gene == null || !pawn.genes.GenesListForReading.Contains(gene)) { Log.Error("[RWSI Native Acceptance] Add gene first."); return; }
            Run("pawn_genes_changed", "genes.remove_exact_gene", "RimWorld.Pawn_GeneTracker.RemoveGene(Gene)", pawn,
                "exact gene load ID and endogenous/xenogene tuple is absent", () => { pawn.genes.RemoveGene(gene); return "void"; },
                (before, after, _) => before.Genes.Any(item => item.LoadId == gene.loadID) && !after.Genes.Any(item => item.LoadId == gene.loadID));
            _genePawn = null; _gene = null;
        }

        [DebugAction("RWSI Native Acceptance", "Control: remove absent gene", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void RemoveAbsentGeneControl()
        {
            var pawn = LiveHuman();
            var geneDef = pawn?.genes == null ? null : DefDatabase<GeneDef>.AllDefsListForReading
                .Where(candidate => candidate != null && !pawn.genes.GenesListForReading.Any(gene => gene.def == candidate))
                .OrderBy(candidate => candidate.defName, StringComparer.Ordinal).FirstOrDefault();
            if (pawn?.genes == null || geneDef == null) { Log.Error("[RWSI Native Acceptance] No absent gene definition is available for the no-op control."); return; }
            var absent = GeneMaker.MakeGene(geneDef, pawn);
            Run("pawn_genes_changed", "control.genes.remove_absent_gene", "RimWorld.Pawn_GeneTracker.RemoveGene(Gene)", pawn,
                "absent gene remains absent and native gene membership is unchanged", () => { pawn.genes.RemoveGene(absent); return "void"; },
                (before, after, _) => before.Genes.Length == after.Genes.Length &&
                    !after.Genes.Any(item => item.LoadId == absent.loadID), true);
        }

        [DebugAction("RWSI Native Acceptance", "Transition: assign ideology role", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void AssignIdeologyRole()
        {
            var pawn = LiveHuman();
            var role = pawn?.ideo?.Ideo?.PreceptsListForReading.OfType<Precept_Role>()
                .Where(candidate => !candidate.IsAssigned(pawn)).OrderBy(candidate => candidate.def.defName, StringComparer.Ordinal).FirstOrDefault();
            if (pawn == null || role == null) { Log.Error("[RWSI Native Acceptance] No unassigned formal ideology role is available for the selected pawn."); return; }
            Run("pawn_role_changed", "role.assign_formal_ideology_holder", "RimWorld.Precept_Role.Assign(Pawn, Boolean)", pawn,
                "the selected Precept_Role reports IsAssigned(pawn) after Assign", () => { role.Assign(pawn, false); _rolePawn = pawn; _role = role; return "void"; },
                (_, after, _) => after.Roles.Any(item => (string?)item.GetType().GetProperty("def")?.GetValue(item) == role.def.defName));
        }

        [DebugAction("RWSI Native Acceptance", "Transition: unassign ideology role", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void UnassignIdeologyRole()
        {
            var pawn = _rolePawn; var role = _role;
            if (pawn == null || role == null || !role.IsAssigned(pawn)) { Log.Error("[RWSI Native Acceptance] Assign ideology role first."); return; }
            Run("pawn_role_changed", "role.unassign_formal_ideology_holder", "RimWorld.Precept_Role.Unassign(Pawn, Boolean)", pawn,
                "the selected Precept_Role no longer reports IsAssigned(pawn)", () => { role.Unassign(pawn, false); return "void"; },
                (_, after, _) => !after.Roles.Any(item => (string?)item.GetType().GetProperty("def")?.GetValue(item) == role.def.defName));
            _rolePawn = null; _role = null;
        }

        [DebugAction("RWSI Native Acceptance", "Control: repeat ideology role assignment", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void RepeatIdeologyRoleControl()
        {
            var pawn = _rolePawn;
            var role = _role;
            if (pawn == null || role == null || !role.IsAssigned(pawn)) { Log.Error("[RWSI Native Acceptance] Assign an ideology role first."); return; }
            Run("pawn_role_changed", "control.role.assign_same_formal_holder", "RimWorld.Precept_Role.Assign(Pawn, Boolean)", pawn,
                "same role remains assigned to the same pawn with no holder change", () => { role.Assign(pawn, false); return "void"; },
                (before, after, _) => before.Roles.SequenceEqual(after.Roles), true);
        }

        private static Pawn? LiveHuman() => Find.CurrentMap?.mapPawns.AllPawnsSpawned
            .Where(pawn => pawn.Faction == Faction.OfPlayer && pawn.RaceProps.Humanlike && !pawn.Dead)
            .OrderBy(pawn => pawn.ThingID, StringComparer.Ordinal).FirstOrDefault();

        private static bool GuestEquals(NativeGuestSnapshot left, NativeGuestSnapshot right) =>
            left.Status == right.Status && left.HostFactionId == right.HostFactionId && left.Released == right.Released &&
            left.PrisonerOfColony == right.PrisonerOfColony && left.SlaveOfColony == right.SlaveOfColony;

        private static void Run(string family, string actionKey, string nativeApi, Pawn? pawn, string expectation,
            Func<object?> action, Func<NativePawnSnapshot, NativePawnSnapshot, object?, bool> predicate, bool controlOnly = false)
        {
            if (pawn == null) { Log.Error("[RWSI Native Acceptance] " + actionKey + " has no eligible pawn; no native call was made."); return; }
            try
            {
                var result = AcceptanceTransitionReceipt.Run(family, actionKey, nativeApi, pawn, null, expectation, action, predicate, controlOnly);
                Log.Message("[RWSI Native Acceptance] " + actionKey + " action_id=" + result.ActionId + " completed=" + result.Completed +
                    " expected_events=" + result.ExpectedEventCount + " control_passed=" + result.ControlPassed +
                    " before=" + result.BeforePath + " action=" + result.ActionPath + " after=" + result.AfterPath);
            }
            catch (Exception exception) { Log.Error("[RWSI Native Acceptance] " + actionKey + " failed: " + exception); }
        }
    }
}
