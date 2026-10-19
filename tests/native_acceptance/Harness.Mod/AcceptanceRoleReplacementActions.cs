using System;
using System.Linq;
using LudeonTK;
using RimWorld;
using Verse;

namespace Rwsi.NativeAcceptance
{
    /// <summary>Independent native membership oracles for replacement and nested null assignment.</summary>
    public static class AcceptanceRoleReplacementActions
    {
        private static Precept_RoleSingle? _role;
        private static Pawn? _first;
        private static Pawn? _second;

        [DebugAction("RWSI Native Acceptance", "Transition: prepare singleton role holder", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void Prepare()
        {
            if (!AcceptanceRuntime.TryAuthorize(out var error)) { Log.Error("[RWSI Native Acceptance] Role preparation rejected: " + error); return; }
            var pawns = Find.CurrentMap?.mapPawns.AllPawnsSpawned
                .Where(p => p.Faction == Faction.OfPlayer && p.RaceProps.Humanlike && !p.Dead && p.ideo?.Ideo != null)
                .OrderBy(p => p.ThingID, StringComparer.Ordinal).ToArray() ?? Array.Empty<Pawn>();
            _first = pawns.FirstOrDefault();
            _second = pawns.FirstOrDefault(p => p != _first && p.ideo.Ideo == _first?.ideo.Ideo);
            _role = _first?.ideo.Ideo.PreceptsListForReading.OfType<Precept_RoleSingle>()
                .OrderBy(r => r.def.defName, StringComparer.Ordinal).FirstOrDefault();
            if (_first == null || _second == null || _role == null)
            { Log.Error("[RWSI Native Acceptance] Two same-ideology synthetic humans and a native singleton role are required."); return; }
            RunMembership("role.prepare_singleton", _first, () => _role.Assign(_first, false), null,
                new[] { _first.GetUniqueLoadID() }, () => _role.IsAssigned(_first) && !_role.IsAssigned(_second));
        }

        [DebugAction("RWSI Native Acceptance", "Transition: replace singleton role holder", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void Replace()
        {
            if (_role == null || _first == null || _second == null || !_role.IsAssigned(_first))
            { Log.Error("[RWSI Native Acceptance] Prepare singleton role holder before replacement."); return; }
            RunMembership("role.replace_singleton", _second, () => _role.Assign(_second, false),
                new[] { _first.GetUniqueLoadID() }, new[] { _second.GetUniqueLoadID() },
                () => !_role.IsAssigned(_first) && _role.IsAssigned(_second));
        }

        [DebugAction("RWSI Native Acceptance", "Transition: clear singleton role holder", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void Clear()
        {
            if (_role == null || _second == null || !_role.IsAssigned(_second))
            { Log.Error("[RWSI Native Acceptance] Replace singleton role holder before clearing."); return; }
            RunMembership("role.clear_singleton_nested_null", _second, () => _role.Unassign(_second, false),
                new[] { _second.GetUniqueLoadID() }, Array.Empty<string>(),
                () => !_role.IsAssigned(_second) && (_first == null || !_role.IsAssigned(_first)));
        }

        [DebugAction("RWSI Native Acceptance", "Control: repeat cleared singleton unassignment", allowedGameStates = AllowedGameStates.PlayingOnMap)]
        public static void RepeatClear()
        {
            if (_role == null || _second == null || _role.ChosenPawns().Any())
            { Log.Error("[RWSI Native Acceptance] Clear singleton role before the no-op control."); return; }
            var result = AcceptanceTransitionReceipt.Run("pawn_role_changed", "control.role.repeat_cleared_unassign",
                "Precept_RoleSingle.Unassign -> Assign(null)", _second, null,
                "the native role remains empty with unchanged pawn role snapshot", () => { _role.Unassign(_second, false); return "void"; },
                (before, after, _) => !_role.ChosenPawns().Any() && before.Roles.SequenceEqual(after.Roles), controlOnly: true);
            Log.Message("[RWSI Native Acceptance] Role no-op control action_id=" + result.ActionId + " control_passed=" + result.ControlPassed);
        }

        private static void RunMembership(string key, Pawn subject, Action mutation, string[]? expectedBefore,
            string[] expectedAfter, Func<bool> nativeAssignmentPredicate)
        {
            var role = _role!;
            NativeSubjectReference[] Capture() => role.ChosenPawns().Where(p => p != null)
                .OrderBy(p => p.ThingID, StringComparer.Ordinal)
                .Select(p => new NativeSubjectReference { ThingId = p.ThingID, UniqueLoadId = p.GetUniqueLoadID() }).ToArray();
            var result = AcceptanceTransitionReceipt.RunExternal("pawn_role_changed", key,
                "Precept_RoleSingle native Assign/Unassign membership", subject.ThingID, subject.GetUniqueLoadID(),
                "native role membership equals declared before/after IDs and IsAssigned agrees; every added or removed pawn is an affected subject",
                () => Capture(), () => { mutation(); return "void"; }, () => Capture(),
                (before, after, _) => TransitionOracle.ExactMembershipTransition(
                    ((NativeSubjectReference[])before!).Select(p => p.UniqueLoadId).ToArray(),
                    ((NativeSubjectReference[])after!).Select(p => p.UniqueLoadId).ToArray(), expectedBefore, expectedAfter)
                    && nativeAssignmentPredicate(),
                (before, after) =>
                {
                    var a = (NativeSubjectReference[])before!; var b = (NativeSubjectReference[])after!;
                    return a.Where(p => !b.Any(q => q.UniqueLoadId == p.UniqueLoadId))
                        .Concat(b.Where(p => !a.Any(q => q.UniqueLoadId == p.UniqueLoadId))).ToArray();
                });
            Log.Message("[RWSI Native Acceptance] " + key + " role_def=" + role.def.defName + " action_id=" + result.ActionId +
                " completed=" + result.Completed + " expected_events=" + result.ExpectedEventCount);
        }
    }
}
