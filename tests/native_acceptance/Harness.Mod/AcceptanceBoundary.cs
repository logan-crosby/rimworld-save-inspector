using System;
using System.IO;

namespace Rwsi.NativeAcceptance
{
    public sealed class AcceptanceMarker
    {
        public string ExpectedRoot { get; set; } = "";
        public string AuthorThread { get; set; } = "";
        public bool SyntheticOnly { get; set; }
        public string ExpectedSeed { get; set; } = "";
        public long ExpectedWorldId { get; set; }
        public string ExpectedBuild { get; set; } = "";
    }

    public static class AcceptanceBoundary
    {
        public static bool Allows(string root, string seed, long worldId, string build,
            AcceptanceMarker? marker)
        {
            if (marker == null || !marker.SyntheticOnly ||
                marker.AuthorThread != "01a14622-b914-7c81-b9ff-0e08d6436c93" ||
                string.IsNullOrWhiteSpace(root) || string.IsNullOrWhiteSpace(marker.ExpectedRoot) ||
                !Path.IsPathRooted(root) || !Path.IsPathRooted(marker.ExpectedRoot) ||
                string.IsNullOrWhiteSpace(marker.ExpectedSeed) ||
                string.IsNullOrWhiteSpace(marker.ExpectedBuild) ||
                !string.Equals(seed, marker.ExpectedSeed, StringComparison.Ordinal) ||
                worldId != marker.ExpectedWorldId ||
                !string.Equals(build, marker.ExpectedBuild, StringComparison.Ordinal)) return false;
            try
            {
                return string.Equals(Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar),
                    Path.GetFullPath(marker.ExpectedRoot).TrimEnd(Path.DirectorySeparatorChar),
                    StringComparison.Ordinal);
            }
            catch (ArgumentException) { return false; }
            catch (NotSupportedException) { return false; }
        }
    }
}
