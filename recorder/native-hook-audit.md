# Native hook audit (development candidate)

This audit records the binding basis for the current candidate. A successful compile proves that the code binds to the referenced metadata; it does not prove that Harmony installed a patch or that a gameplay action reached it.

## Assembly and build identity

| Profile | Version / reference | SHA-256 | Use |
|---|---|---|---|
| Reference API | `Krafs.Rimworld.Ref 1.6.4535` | `8963b06f44903b4cab020c9a7b4b8f26b304c349d585993dc8fbe161059cda04` | API-reference build only; no runtime claim |
| Installed native target | RimWorld `1.7.4980 rev60`, `Assembly-CSharp.dll` | `eff7d97949f919fb2b151ebfb906e9be749b63410b7dbda78eeebb7972b245c3` | Explicit local native compile profile |
| Installed native Unity API | `UnityStubs.dll` | `cd95a2d1a2e0e52745f1b4562fc0cbf66b526cd942015a75da63d2cf8770d535` | Unity and first-pass metadata reference for the installed native profile |

The installed 1.7 runtime configuration maps Unity assemblies and `Assembly-CSharp-firstpass` to `UnityStubs.dll`. The build keeps the project at `net472`; it does not infer a new target framework from the host runtime. Native references are resolved from the explicit `RimWorldNativeManagedDir` only. `obj/native-1.7.4980` and `bin/native-1.7.4980` are isolated from the `reference-1.6.4535` assets and outputs. The native assets contain Harmony and Newtonsoft but not `Krafs.Rimworld.Ref`.

The current native profile compile succeeded with zero warnings and zero errors. That remains compile-only. Runtime patch installation, event delivery, save association, performance, and gameplay outcomes are unverified pending the separately controlled native-profile run.

## Audited targets and predicates

Signatures below are exact overload selectors used by `RecorderBootstrap`; they were checked against the pinned 1.6 API reference and the installed 1.7 metadata/decompilation where noted. Hook success is still gated by the runtime predicates below.

| Family | Target signature | Record predicate and limits |
|---|---|---|
| Birth notification | `QuestManager.Notify_PawnBorn(Thing, Thing, Pawn, Pawn)` and `TaleRecorder.RecordTale(TaleDef, object[])` | Quest callback stages `biological_birth` only when the actual birther Thing equals the genetic mother; other birther kinds remain `unknown`. Tale callback accepts only `TaleDefOf.GaveBirth` with validated mother/newborn arguments. Both feed one per-session coalescer; no direct-spawn-as-birth rule. |
| Hatching | `CompHatcher.Hatch()` and `PawnUtility.TrySpawnHatchedOrBornPawn(Pawn, Thing)` | 1.7 decompilation shows `Hatch` invokes the pawn utility for each hatch and the call returns a bool. A thread-local egg scope associates only a `true` utility result with that egg; generic spawn or gestator/mech creation is not called biological birth. Hatching is a separate birth kind and shares the session coalescer. `Hatch` itself returns void. |
| Guest status | `Pawn_GuestTracker.SetGuestStatus(Faction, GuestStatus)` and `Pawn_GuestTracker.CapturedBy(Faction, Pawn)` | Emits only if effective host/status/released/prisoner/slave state changes. Nested duplicate callbacks collapse by before/after equality. Other direct-field or mod-only writes are not exact-event coverage. |
| Relations | `Pawn_RelationsTracker.AddDirectRelation(PawnRelationDef, Pawn)`, `RemoveDirectRelation(PawnRelationDef, Pawn)`, `RemoveDirectRelation(DirectPawnRelation)`, `RemoveRelation(RelationshipRecord)`, `TryRemoveDirectRelation(PawnRelationDef, Pawn)` | Snapshots directed direct relations around the mutator and emits changed edges only. Indirect relation consequences are not claimed as directly mutated events. |
| Genes | `Pawn_GeneTracker.AddGene(GeneDef, bool)`, `RemoveGene(Gene)`, `ClearXenogenes()`, `SetXenotype(XenotypeDef)`, `SetXenotypeDirect(XenotypeDef)` | Compares effective genes and xenotype before/after. Direct field writes and custom-mod mutations remain gaps. |
| Ideology role | `Precept_Role.Assign(Pawn, bool)` and `Unassign(Pawn, bool)` | Emits only when `IsAssigned(Pawn)` changes. |
| Royal title | `Pawn_RoyaltyTracker.SetTitle(Faction, RoyalTitleDef, bool, bool, bool)` | Emits only when the effective title changes. Mod-specific role fields remain outside exact-event coverage. |
| Save checkpoint | `SafeSaver.Save(string, string, Action, bool)` | Prefix records an attempt; a successful outcome requires the bounded read-only output verifier to find matching campaign/checkpoint/session markers and complete the output hash. Exceptions are rethrown unchanged and record failure. Attempted checkpoint state is not treated as a verified parent. No actual save path has been runtime-validated yet. |
| Holder transitions | `Pawn.SpawnSetup(Map, bool)`, `Thing.DeSpawn(DestroyMode)`, and loaded concrete `ThingOwner.TryAdd`/`Remove` overrides | Emits only observed before/after holder-chain changes for the same pawn identity. Override discovery is limited to concrete implementations loaded at initialization; later third-party overrides and non-ThingOwner world-pawn transitions are not covered. |
| Clocks | `Find.TickManager.TicksGame`, `TicksAbs`, `gameStartAbsTick`, and `StartingYear` | Captures values on the game thread. When all tick values are available, enforces `ticks_abs = ticks_game + game_start_abs_tick`; unavailable values remain null rather than inferred. |

Birth coalescing is scoped to a `BirthObservationBuffer` owned by one loaded recorder session, not a process-static pawn-ID cache. It keeps the first callback's exact clock basis and every source/parent/birther observation. An unknown observation can be refined by a positive birth-specific signal; conflicting positive kinds yield `birth_kind: unknown` and `classification_conflict: true`, while conflicting parent facts are retained in the observation list and flagged. A completed subject remains in the bounded 100,000-ID session dedupe set; capacity exhaustion records coverage and suppresses further completed births rather than risking duplicates. Late same-session notifications become coverage observations, never a second `pawn_born` event. A new session (including replay) can record the same saved pawn ID independently.

Pending birth journal envelopes reserve their serialized max-sequence byte size in `RecorderQueue`; ordinary events and gap materialization account for those reservations. Flushing converts a reservation to actual queued bytes atomically. Pending births flush at `GameComponentUpdate` (including paused-safe updates), before save attempt/serialization, before session close and before component reinitialization. If pending observations exceed the subject or shared 16 MiB budget, the collector writes explicit coverage diagnostics when queue capacity permits. This is implementation evidence only; runtime cadence/order remains unvalidated in native gameplay.

All collectors report install status separately from observed event counts. A missing or failed patch remains explicit in coverage metadata. Runtime status and observed gameplay-action counts are not yet available.

## Discovery failures preserved

- Initial source search targeted `Verse.PawnUtility`; the actual type resolved in the audited API is `PawnUtility`. The selector was corrected after metadata inspection.
- The first explicit 1.7 profile pointed Unity and first-pass references at `UnityEngine*.dll` and `Assembly-CSharp-firstpass.dll`, which do not exist in the installed app assembly directory. The attempted build failed with `CS0246` for `UnityEngine` at `RecorderSessionComponent.cs:12`. The installed runtime config and metadata show `UnityStubs.dll`; the profile was corrected to that verified binding.
- An earlier `dotnet test --no-restore` invocation returned no output before test assets existed. It is invalid verification evidence. After restoring from the local package cache, the testhost required scoped local loopback permission; the actual xUnit run later passed all 13 tests.

## Validation commands

Reference API build:

```sh
dotnet restore recorder/Recorder.Mod/Recorder.Mod.csproj \
  -p:RimWorldGameProfile=reference-1.6.4535 \
  --source /path/to/nuget-cache
dotnet build recorder/Recorder.Mod/Recorder.Mod.csproj --no-restore \
  -p:RimWorldGameProfile=reference-1.6.4535 --verbosity minimal
```

Native 1.7 compile:

```sh
dotnet restore recorder/Recorder.Mod/Recorder.Mod.csproj \
  -p:RimWorldGameProfile=native-1.7.4980 \
  -p:RimWorldNativeManagedDir=/path/to/isolated/RimWorld.app/Contents/MacOS \
  --source /path/to/nuget-cache
dotnet build recorder/Recorder.Mod/Recorder.Mod.csproj --no-restore \
  -p:RimWorldGameProfile=native-1.7.4980 \
  -p:RimWorldNativeManagedDir=/path/to/isolated/RimWorld.app/Contents/MacOS \
  --verbosity minimal
```

The diagnostic native build log is retained at `.superpowers/sdd/2026-10-17-complete-pawn-history/recorder-native-build-diag.txt`.
