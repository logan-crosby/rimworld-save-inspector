# Disposable native acceptance harness

Owner: root; Beads `raf-vqw`, parent `raf-801`; author thread
`01a14622-b914-7c81-b9ff-0e08d6436c93`; policy `v2026.09.26.1`.

This development-only harness is excluded from the Python and recorder release
packages. It drives real native game APIs through explicit debug actions, never
the recorder's event-emission API. It must reject an absent or mismatching marker,
active savedata root, synthetic world identity, author thread, or native build.
All output stays inside the marked disposable profile. It has no startup gameplay
mutation and no control over another process.

The native mod project is `Harness.Mod/Harness.Mod.csproj`. Its entry points are
the debug actions in `Harness.Mod/AcceptanceActions.cs` and
`Harness.Mod/AcceptanceTransitionActions.cs`:

- `RWSI Native Acceptance > Prepare fixture` generates missing player pawns with
  `PawnGenerator.GeneratePawn` and spawns them with `GenSpawn.Spawn` until the
  current map reaches at least 10 humanlikes, 10 animals, and 5 mechanoids.
  Existing and generated world pawns are retained. Missing native pawn kinds,
  spawn failures, and short populations are logged as incomplete; no category is
  substituted. The selected animal/mechanoid kind and all materialized pawn IDs
  and kinds are included in the next receipt.
- `RWSI Native Acceptance > Write state receipt` writes a unique immutable JSON
  file under `<active-save-root>/NativeAcceptance/`. The envelope contains a
  SHA-256 of canonical serialized payload JSON; it records game/absolute ticks,
  world seed and persistent random value, native build, map identity and size,
  every loaded map and its map/world pawn census, nested holder paths, world-pool
  membership, faction unique load ID/name/def, state, hediffs, direct relations,
  xenotype, and genes. Holder traversal is bounded to depth 64 and 100,000 visits;
  cycles, limits, and read failures are explicit diagnostics and set
  `census_complete` to false. Native `GetDirectlyHeldThings()` null means the
  holder has no direct owner; `GetChildHolders()` is still traversed.
- Transition actions and their acceptance scope are listed in
  `action-procedure.json`, frozen before native transition actions. Each
  invocation creates three create-new files under
  `<active-save-root>/NativeAcceptance/transitions/`: `*.before.json`,
  `*.action.json`, and `*.after.json`. Each is an envelope with a SHA-256 over
  the serialized stage payload; action IDs are UUIDs, so same-tick invocations
  remain distinct. The action stage records clocks, unique subject/related IDs,
  native API, return value, exception details, expected edge, independent state
  observations, and the completion-derived expected event count.
- Current native transition menu paths (compile-bound; runtime behavior remains
  unverified):
  - `Transition: kill live human` calls `Pawn.Kill` on the selected live player
    human and requires that same pawn to change alive-to-dead.
  - `Transition: resurrect dead pawn` calls `ResurrectionUtility.TryResurrect`
    on a pawn from a current-map corpse and requires dead-to-alive plus `true`.
  - `Control: resurrect living pawn` requires `false` and unchanged live state.
  - `Control: injected nested health failure then success` verifies the exact
    native `AddHediff(HediffDef,...)` to `AddHediff(Hediff,...)` call chain in
    installed IL, checks the recorder's prefix/finalizer patches and priority,
    then installs a one-shot harness-owned Harmony prefix after the recorder
    inner prefix. The injected exception is raised before the native inner
    method mutates the selected pawn. The receipt requires both native methods
    entered, retains the exact transient Hediff identity and exception, and
    separately counts harness and recorder prefix/finalizer entries, requires
    one fault consumption and the exact injected exception type, and compares
    the full before/after Hediff membership/part/severity state. The receipt
    declares zero expected events; this is not an observed journal count. Only a passed fault control runs a separate real injury
    addition, whose new receipt must link the failed action ID and expect one
    event. Missing recorder patch/order evidence refuses injection entirely.
  - `Transition: add nonfatal injury` adds a newly created native injury
    Hediff instance; `Transition: remove exact added injury` removes that exact
    instance. `Control: remove absent injury` records a no-op control.
  - `Transition: add direct acquaintance`, `Transition: remove direct
    acquaintance`, and `Control: remove absent acquaintance` exercise the
    directed native relation APIs and verify the exact pawn pair.
  - `Transition: change faction`, `Transition: restore faction`, and
    `Control: set same faction` exercise a real faction edge and unchanged
    setter control.
  - `Transition: set guest status`, `Transition: toggle guest host`, and
    `Control: repeat guest status` record the complete guest/host tuple.
  - `Transition: add gene` and `Transition: remove exact gene` retain and
    verify the exact native Gene instance; `Control: remove absent gene`
    attempts to remove a separately constructed gene absent from membership.
  - `Transition: assign ideology role` and `Transition: unassign ideology
    role` exercise a formal `Precept_Role` when the synthetic Ideo supplies one;
    `Control: repeat ideology role assignment` is the same-holder control.
  - `Transition: map pawn to caravan` and `Transition: caravan pawn to map`
    use `CaravanMaker.MakeCaravan`, `Caravan.RemovePawn`, and `GenSpawn.Spawn`.
  - `Transition: carry pawn` and `Transition: drop carried pawn` use the native
    `Pawn_CarryTracker` holder APIs.
  - `Transition: load pawn to transporter` and `Transition: unload pawn from
    transporter` use an existing compatible native `CompTransporter` and its
    `ThingOwner`.
  - `Transition: map pawn to world pool` and `Transition: world pool pawn to
    map` are an additional holder edge outside the batch.
  - `Control: add already-held pawn to transporter` checks that duplicating a
    loaded pawn is a no-op with zero expected events.
  - `Transition: store corpse in pawn inventory` and `Transition: remove corpse
    from pawn inventory` move a real dead pawn inside its native Corpse holder
    through the living player's inventory ThingOwner.
  - `Transition: place pawn in cryptosleep casket` and `Transition: eject pawn
    from cryptosleep casket` exercise a temporary native casket.
  - `Transition: place child pawn in growth vat` and `Transition: remove child
    pawn from growth vat` require an existing vat and eligible child pawn.
  - These cases are source/build only; each may report unavailable preconditions
    and needs native before/after plus journal verification.
  - `Transition: hatch fertilized egg` invokes native `CompHatcher.Hatch()`;
    it records every newly spawned pawn ThingID and unique load ID, and expects
    one event per materialized subject. If the native call yields no pawn, the
    receipt records an incomplete attempt and zero expected events; this is a
    failed-hatch observation, not a passing negative control by itself.
- `Batch: pin baseline and reset progress` writes a complete immutable native
  state receipt, hashes it, and binds the batch to that baseline, marked profile,
  world, native build, and loaded-game session. The lifecycle component clears
  in-memory progress on every game load or new game. Then
  `Batch: continue bounded 10-family run` performs up to two rounds per family
  per invocation, checking a 15-second wall-time budget between native calls.
  It counts only completed state transitions, records unique transition action
  IDs by family, counts each materialized newborn separately, and stops a family
  on its first missing or failed transition. Invoke again to resume. The batch
  receipt binds progress to the pinned baseline hash and game session. These are
  native action counts only; the root separately reconciles recorder journal
  results. The batch has not been run, and holder family coverage remains
  unaccepted until native outcomes and frozen holder targets are verified.
- `Metrics: enable`, `Metrics: disable`, `Metrics: reset`, and
  `Metrics: write snapshot receipt` use reflection to access the optional
  `RimWorld.LifeRecorder.RecorderDeveloperMetrics` API. They never emit events;
  their data is diagnostic only, not a transition oracle.
- `Benchmark: start 2500+10000 actual DoSingleTick samples` and
  `Benchmark: cancel DoSingleTick interval` arm the same independent Harmony
  prefix/postfix probe in either metrics condition. It measures each real
  `Verse.TickManager.DoSingleTick()` call with `Stopwatch.GetTimestamp()` before
  and after the native method; the first 2,500 game ticks are warmup and the
  next 10,000 raw duration samples are written with the Stopwatch frequency.
  This measures native method elapsed time, not frame, IPC, or wall-interval
  timing. Root owns paired-run controls; start does not unpause or mutate game
  state. Cancelled/partial runs are invalid for performance comparison.

The harness declares Harmony as a mod dependency because the independent fault
control and tick probe use Harmony-owned patches. The patches are armed only by
guarded debug actions and unpatched on completion, cancellation, or failure.

The state census also records per-Hediff load ID, def, body part/index, and
severity; directed relation target unique IDs; gene load ID, def, and
endogenous/xenogene membership; xenotype; guest status/host/released/prisoner/
slave state; and ideology-role/royal-title holders. These fields are observed
through the game’s native pawn state and do not consult recorder output.

Both actions load no code at startup and check
`<active-save-root>/RwsiNativeAcceptance.json` immediately before acting. The
marker is created by the fixture owner. Its exact schema is
`AcceptanceMarker` in `Harness.Mod/AcceptanceBoundary.cs`; current seed,
persistent random value, and native build are compared with that marker through
`AcceptanceBoundary.Allows`.

All mutating transition actions perform the same guard before the native call.
They take their target only from the active marked synthetic world and never
accept a path, save ID, pawn ID, or API name from input. A thrown call is kept in
the action-stage receipt and cannot satisfy a completion predicate. The first
M2 source has entry points for the ten frozen event families, but source presence
is not native acceptance. Holder coverage is partial as described above. Game
execution, batch completeness, metrics API availability, and tick-component
registration/runtime behavior remain unverified.

Build against the installed native game metadata with isolated restore assets:

```sh
dotnet restore tests/native_acceptance/Harness.Mod/Harness.Mod.csproj \
  -p:RimWorldNativeManagedDir="/path/to/isolated/RimWorld.app/Contents/MacOS" \
  -p:RestorePackagesPath=".superpowers/sdd/2026-10-17-complete-pawn-history/recorder-nuget-packages" \
  --source .superpowers/sdd/2026-10-17-complete-pawn-history/recorder-nuget-feed
dotnet build tests/native_acceptance/Harness.Mod/Harness.Mod.csproj --no-restore \
  -p:RimWorldNativeManagedDir="/path/to/isolated/RimWorld.app/Contents/MacOS" \
  --verbosity minimal
```

The DLL is emitted under `Harness.Mod/bin/native-1.7.4980/Debug/net472/`; build
outputs are not source artifacts. A successful compile proves native metadata
binding only. The debug actions have not been run in RimWorld, and no gameplay,
population, receipt completeness, or recorder-delivery result is claimed.

Before/after native state, immutable saved XML, and independently read journal
records must reconcile for every required event family and failed/no-op control.
Compilation and guard unit tests alone do not establish gameplay success.

The first quick-colony preparation generated seed `corporation` rather than the
procedure's desired seed. Its initial save and screenshots remain preserved as
preparation; no performance results exist. Pin the final fixture identity, adapter
hashes and census before measurement, and keep the same adapter in all on/off runs.
