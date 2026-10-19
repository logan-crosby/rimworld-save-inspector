using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;

namespace RimWorld.LifeRecorder.Core
{
    /// <summary>Maps an untrusted logical campaign identifier to a safe directory below the active save root.</summary>
    public static class RecorderStoragePaths
    {
        public static string GetCampaignStorageKey(string campaignId)
        {
            if (campaignId == null) throw new ArgumentNullException(nameof(campaignId));
            using (var sha = SHA256.Create())
            {
                var hash = sha.ComputeHash(Encoding.UTF8.GetBytes(campaignId));
                return BitConverter.ToString(hash).Replace("-", "").ToLowerInvariant();
            }
        }

        public static string ResolveCampaignDirectory(string saveDataFolderPath, string campaignId)
        {
            if (string.IsNullOrWhiteSpace(saveDataFolderPath))
                throw new ArgumentException("The active save-data folder is required.", nameof(saveDataFolderPath));

            var saveRoot = Path.GetFullPath(saveDataFolderPath);
            var recorderRoot = Path.Combine(saveRoot, "LifeRecorder");
            return Path.GetFullPath(Path.Combine(recorderRoot, GetCampaignStorageKey(campaignId)));
        }
    }
}
