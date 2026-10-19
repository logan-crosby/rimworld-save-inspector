# Beta delivery scope

Version `1.1.0b1`, 2026-10-19.

The beta provides Python save inspection, JSON CLI, configured-source read-only
MCP tools, local history catalogs, pawn dossiers and timelines, exact evidence
retrieval, saved-event queries and offline discovery. Sources remain read-only;
catalog refreshes may write only application-owned derived cache generations.

Existing 1.0 inspection interfaces remain available. New history and discovery
interfaces are beta; see [versioning and features](versioning.md). Missing clocks,
uncertain identity, conflicting observations and unsupported fields stay explicit.

Read-only journal inspection is an experimental compatibility utility. The
Life Recorder mod has unresolved native holder-transition failures and is not
ready for dependable logging. Its development source remains in the repository;
it is excluded from Python distributions and no installable mod is released.

The complete private-corpus campaign, original performance and aggregate G/H/L/A
acceptance gates, native recorder lifecycle/performance and paid semantic
evaluation remain deferred. Functional checks do not certify those workloads.
Earlier failed evidence remains retained privately under its original candidate.

Independent review observed cold MCP startup of approximately 9–17 seconds on a
busy host. Handshake, tool queries and clean shutdown work, but original startup
and throughput targets are not certified. Configure the client connection timeout
to allow for cold startup separately from warm query execution.

Distribution is through GitHub prereleases, not PyPI. Publishing this beta does
not register MCP servers, modify a game profile, install a mod or certify runtime
game/mod compatibility. The stable 1.0 release remains available.

Author: Codex thread `01a14622-b914-7c81-b9ff-0e08d6436c93`.
