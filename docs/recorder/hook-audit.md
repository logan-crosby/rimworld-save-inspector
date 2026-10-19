# Life Recorder hook feasibility audit

**CONTRADICTED — follow-up2026-10-17:** the initial claims that guest, relation, gene, ideology/royalty and save-loader types were absent or not found were based on incomplete method-table inspection. Root independently confirmed `RimWorld.Pawn_GuestTracker`, `Pawn_RelationsTracker`, `Pawn_GeneTracker`, `Pawn_IdeoTracker`, `Pawn_RoyaltyTracker` and `Verse.GameDataSaveLoader` in the exact cached1.6.4535 Assembly-CSharp using `monodis --typedef`. The current recorder worker is auditing signatures and bodies in1.6 and local1.7. This corrects type-discovery conclusions only; native hook behavior remains unverified. Retain the original report below as failed/incomplete evidence, not current proof of missing APIs. Author: Codex thread01a14622-b914-7c81-b9ff-0e08d6436c93.

**Status:** reference inspection only; no native runtime verification.
**Reference profile inspected:** Krafs.Rimworld.Ref 1.6.4535, ref/net472/Assembly-CSharp.dll.
**Assembly SHA-256:** 8963b06f44903b4cab020c9a7b4b8f26b304c349d585993dc8fbe161059cda04.
**Metadata extraction:** Mono monodis --method; the cached reference is metadata, not a game runtime.
**Current in-repository comparison:** RimAI.Agent/Source/Perception/Events/PerceptionHarmonyHooks.cs; its own header describes the examples as compile-only and awaiting runtime validation.

“Reference signature observed” means the method and displayed signature exist in that exact cached assembly. It does not establish Harmony binding, successful event semantics, compatibility with another game build, or behavior with other mods. No native assembly was inspected or game launched in this audit.

| Required recorder family | Reference evidence / candidate | Feasibility finding |
| --- | --- | --- |
| pawn_died | Verse.Pawn.Kill(Nullable<Verse.DamageInfo> dinfo, Verse.Hediff exactCulprit = null) | Signature observed. A postfix still must verify the dead-state transition and capture only supplied cause data. Existing perception example patches Pawn.Kill; it does not prove native behavior. |
| pawn_resurrected | Verse.Pawn_HealthTracker.Notify_Resurrected(bool restoreMissingParts = ..., float gettingScarsChance = ...); also Verse.HediffSet.Notify_Resurrected() appears in metadata | Candidate notification signatures observed. Exact success timing and coverage of all resurrection paths require runtime verification. Do not infer success from a generic debug utility or invocation name. |
| pawn_born | Verse.GenSpawn.Spawn(Verse.Thing, Verse.IntVec3, Verse.Map, Verse.Rot4 = ..., Verse.WipeMode = ..., bool respawningAfterLoad = ..., bool forbidLeavings = ...) | Spawn overload observed; it is not a birth-specific method. Filtering for a spawned Pawn alone would include non-birth spawns and load respawns. A validated newborn predicate/source remains necessary. |
| pawn_faction_changed | Verse.Pawn.SetFaction(RimWorld.Faction newFaction, Verse.Pawn recruiter = null); same-shaped methods also exist on other types | Signature observed on Verse.Pawn. Must compare before/after saved faction IDs and avoid treating a call as a successful transition when unchanged or failed. |
| pawn_guest_status_changed | Verse.Pawn.GuestStatus getter exists as Nullable<RimWorld.GuestStatus> | No SetGuestStatus method or guest-tracker type was found in the inspected Assembly-CSharp method table. Mutator/notification hook and success predicate remain unverified; do not substitute a property-read hook. |
| pawn_relation_changed | Notify_RelationAdded(Pawn, PawnRelationDef) occurs on observed relation-aware types, but no Pawn_RelationsTracker type or corresponding public add/remove pair was found in the inspected table | Candidate notification is not a complete bidirectional add/remove contract. Need an exact owner/mutator pair, counterpart direction rules, and runtime confirmation before this family is certified. |
| pawn_health_changed | Verse.Pawn_HealthTracker.AddHediff(HediffDef, BodyPartRecord = null, Nullable<DamageInfo> = null, DamageResult = null), AddHediff(Hediff, ...), RemoveHediff(Hediff), and Notify_Resurrected(...) | Signatures observed. These can support added/removed condition candidates; successful post-state and operation/implant semantics need separate coverage. Do not hook every damage/tick as an equivalent family. |
| pawn_genes_changed | PawnGenerator.GenerateGenes(Pawn, XenotypeDef, PawnGenerationRequest) is present; no Pawn_GeneTracker type or obvious runtime add/remove mutation pair was found in this assembly method listing | Generation method is not a complete post-generation change hook. Required mutation signatures remain unresolved in the available reference output; report this family as unverified. |
| pawn_role_changed | Royal-title/ideology utilities exist, including a debug SetIdeoRole(Pawn) method; no canonical pawn-role setter was established | “Role” is not sufficiently represented by one verified native target in this reference. The product meaning and mutation family remain unresolved; do not treat debug helpers as production hooks. |
| pawn_holder_changed | Verse.ThingOwner.TryTransferToContainer(Thing, ThingOwner, int, out Thing, bool = ...), plus TryAdd, Take, TryDrop overloads | Container methods observed, but no single operation covers all pawn holder transitions (map, caravan, transport, corpse, carry, world). Enumerate required holder contexts and prove identity-preserving success predicates in runtime tests. |

## Existing perception hook cross-check

The current source explicitly says its hook bodies are compile-only. It contains Pawn.Kill, Pawn_HealthTracker.MakeDowned, GenSpawn.Spawn, and MentalStateHandler.TryStartMentalState examples, which are not substitutes for the recorder's required family coverage. Its GenSpawn.Spawn attribute names the six-argument overload (Thing, IntVec3, Map, Rot4, WipeMode, bool); the inspected 1.6.4535 metadata also has a seven-parameter overload with optional forbidLeavings. The death postfix accepts the supplied nullable damage info. These observations are source/reference comparisons only, not a compile or runtime result for the recorder.

## Gate disposition

- Reference metadata: observed for the listed candidates only in cached 1.6.4535 Assembly-CSharp.
- Native game signatures: not inspected.
- Harmony patch installation / success predicates: not runtime-tested.
- Required-family coverage: guest status, relations, genes, role semantics, and complete holder transitions remain materially unverified; birth requires a distinct verified predicate.
- Release effect: do not mark these families installed or fully covered based on method-name matches or reference compilation. L2/L5 must resolve them against the target runtime or explicitly remain blocked/unverified.
