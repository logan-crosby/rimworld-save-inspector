# Versioning and release features

Version: **1.1.0b1**. Status: beta prerelease.

This is a minor release because it adds history and discovery alongside the
existing inspection interface. The beta suffix records the narrower validation
scope; it does not turn deferred acceptance checks into passes.

## Included features

| Capability | Interface | Release status |
| --- | --- | --- |
| Colony summaries, pawn details, health, genes, equipment, inventory, research and saved mods | Python, JSON CLI, single-save MCP | Existing 1.0 interface retained |
| Local save collection, compatible campaigns and immutable catalog generations | Catalog CLI, catalog inspection MCP | New beta |
| One-pawn timeline across compatible saves, snapshot dossiers and historical aliases | JSON CLI, history MCP | New beta |
| Chronology uncertainty, conflicts, shared context and source provenance | History responses | New beta |
| Exact XML evidence and full normalized saved-event occurrences | JSON CLI, history MCP | New beta |
| Offline discovery, bounded highlights and follow-up selectors | JSON CLI, history MCP | New beta |
| Read-only inspection of supplied Life Recorder journal files | Journal CLI and MCP | Experimental utility; no complete-recording claim |

The C# Life Recorder mod is excluded from the Python wheel and source archive.
Its experimental source is retained in the repository. Native logging remains
unaccepted because holder-transition handling has unresolved failures.

## Version rules

- Package versions use `MAJOR.MINOR.PATCH`, with Python prerelease suffixes such
  as `b1` and `rc1`. Git release tags, if separately authorized, use a `v` prefix.
- A patch fixes behavior within the documented contract. A minor version adds
  compatible capabilities. A major version is required for incompatible changes
  to an established CLI, Python API or public JSON contract.
- The package, CLI and MCP server identities share the package version.
- JSON schema versions are independent: the single-save and history envelopes
  remain `1.0`; discovery remains `history-discovery/1.0`. A package bump alone
  does not change their shapes or meanings.
- Single-save parser provenance reports the package version. Its interpretation
  identity includes that version, so interpretation IDs and cursors from older
  package versions are not reused. Save-byte snapshot IDs remain unchanged.
  History interpretation uses extraction version `7`; its fingerprint invalidates
  catalogs interpreted by older extractors.
- Existing versioned artifacts and verification receipts are immutable. A new
  package gets new hashes and fresh affected checks; old results retain their
  original candidate identity.

## Validation and promotion

The reviewed functional baseline passed the installed synthetic and regression
suite and a private canonical-save check. That evidence covers the documented
bounded delivery, not every compatible save, pawn or game runtime.

The full private-corpus campaign, original performance limits, native recorder
acceptance and paid semantic evaluation remain deferred. MCP cold startup can
take approximately 10–20 seconds on a busy host. Other game versions remain
best effort; Windows and runtime mod compatibility are not certified.

The original `tools/verify.py` acceptance runner is pinned to the 1.0.0 contract
and rejects other release identities. It is retained with its original assertions;
it does not certify this beta. Updating that acceptance campaign for a later
release requires its own versioned scope and evidence.

Do not remove the beta suffix by renaming artifacts. A stable 1.1 candidate needs
an explicit release-scope decision about these limitations and fresh validation
of that candidate against the resulting acceptance scope. Native recorder
acceptance is required only if a later release includes the recorder.

GitHub releases distribute the Python wheel and source archive with checksums.
Installing the package does not register MCP servers in a host or install the recorder.

Author: Codex thread `01a14622-b914-7c81-b9ff-0e08d6436c93`, 2026-10-18.
