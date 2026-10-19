using System;
using System.IO;
using Newtonsoft.Json;
using RimWorld;
using Verse;

namespace Rwsi.NativeAcceptance
{
    internal static class AcceptanceRuntime
    {
        internal const string MarkerFileName = "RwsiNativeAcceptance.json";

        internal static bool TryAuthorize(out string error)
        {
            error = "unknown guard failure";
            try
            {
                var root = GenFilePaths.SaveDataFolderPath;
                var markerPath = Path.Combine(root, MarkerFileName);
                if (!File.Exists(markerPath)) { error = "root marker is missing"; return false; }
                var marker = JsonConvert.DeserializeObject<AcceptanceMarker>(File.ReadAllText(markerPath));
                var world = Find.World;
                if (world?.info == null) { error = "active world identity is unavailable"; return false; }
                var seed = world.info.seedString;
                var persistentRandomValue = world.info.persistentRandomValue;
                var build = VersionControl.CurrentVersionStringWithRev;
                if (!AcceptanceBoundary.Allows(root, seed, persistentRandomValue, build, marker))
                {
                    var mismatches = new System.Collections.Generic.List<string>();
                    if (marker == null) mismatches.Add("marker_missing_or_invalid");
                    else
                    {
                        if (string.IsNullOrWhiteSpace(root) || string.IsNullOrWhiteSpace(marker.ExpectedRoot) ||
                            !string.Equals(Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar), Path.GetFullPath(marker.ExpectedRoot).TrimEnd(Path.DirectorySeparatorChar), StringComparison.Ordinal)) mismatches.Add("save_root");
                        if (!string.Equals(marker.AuthorThread, "01a14622-b914-7c81-b9ff-0e08d6436c93", StringComparison.Ordinal)) mismatches.Add("author_thread");
                        if (!marker.SyntheticOnly) mismatches.Add("synthetic_only");
                        if (!string.Equals(seed, marker.ExpectedSeed, StringComparison.Ordinal)) mismatches.Add("world_seed");
                        if (persistentRandomValue != marker.ExpectedWorldId) mismatches.Add("persistent_random_value");
                        if (!string.Equals(build, marker.ExpectedBuild, StringComparison.Ordinal)) mismatches.Add("native_build");
                    }
                    error = "marker rejected; mismatch_fields=" + (mismatches.Count == 0 ? "boundary_or_path_validation" : string.Join(",", mismatches)) +
                        "; observed_build=" + build + "; expected_build=" + (marker?.ExpectedBuild ?? "<missing>");
                    return false;
                }
                error = "authorized";
                return true;
            }
            catch (Exception ex)
            {
                error = ex.GetType().Name + ": " + ex.Message;
                return false;
            }
        }
    }
}
