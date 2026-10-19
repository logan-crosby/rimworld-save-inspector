using System;
using System.IO;
using System.Security.Cryptography;
using System.Xml;

namespace RimWorld.LifeRecorder.Core
{
    public sealed class SaveOutputVerification
    {
        public bool Associated { get; }
        public long ByteCount { get; }
        public string? Sha256 { get; }
        public string? FailureReason { get; }

        internal SaveOutputVerification(bool associated, long byteCount, string? sha256, string? failureReason)
        {
            Associated = associated;
            ByteCount = byteCount;
            Sha256 = sha256;
            FailureReason = failureReason;
        }
    }

    /// <summary>Read-only bounded verifier for the three checkpoint markers persisted by the mod.</summary>
    public static class SaveOutputVerifier
    {
        public const long DefaultMaxBytes = 2L * 1024 * 1024 * 1024;

        public static SaveOutputVerification Verify(string path, string campaignId, string checkpointId, string producerSessionId,
            long maxBytes = DefaultMaxBytes)
        {
            if (String.IsNullOrWhiteSpace(path) || String.IsNullOrWhiteSpace(campaignId) ||
                String.IsNullOrWhiteSpace(checkpointId) || String.IsNullOrWhiteSpace(producerSessionId) || maxBytes <= 0)
                return Fail(0, "invalid_verification_arguments");

            try
            {
                var before = new FileInfo(path);
                if (!before.Exists) return Fail(0, "output_missing");
                if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0) return Fail(0, "output_is_reparse_point");
                if (before.Length <= 0 || before.Length > maxBytes) return Fail(before.Length, "output_outside_byte_limit");
                var initialLength = before.Length;
                var initialWriteTime = before.LastWriteTimeUtc;
                byte[] digest;
                var foundCampaign = 0;
                var foundCheckpoint = 0;
                var foundSession = 0;
                var campaign = (string?)null;
                var checkpoint = (string?)null;
                var session = (string?)null;

                using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
                {
                    if (stream.Length != initialLength) return Fail(stream.Length, "output_changed_before_read");
                    using (var sha = SHA256.Create()) digest = sha.ComputeHash(stream);
                    stream.Position = 0;
                    var settings = new XmlReaderSettings
                    {
                        DtdProcessing = DtdProcessing.Prohibit,
                        XmlResolver = null,
                        IgnoreComments = true,
                        IgnoreWhitespace = true,
                        CloseInput = false,
                        MaxCharactersInDocument = maxBytes
                    };
                    using (var reader = XmlReader.Create(stream, settings))
                    {
                        var advance = true;
                        while (true)
                        {
                            if (advance && !reader.Read()) break;
                            advance = true;
                            if (reader.NodeType != XmlNodeType.Element) continue;
                            if (reader.LocalName == "lifeRecorderCampaignId")
                            {
                                campaign = reader.ReadElementContentAsString();
                                foundCampaign++;
                                advance = false;
                            }
                            else if (reader.LocalName == "lifeRecorderCheckpointId")
                            {
                                checkpoint = reader.ReadElementContentAsString();
                                foundCheckpoint++;
                                advance = false;
                            }
                            else if (reader.LocalName == "lifeRecorderCheckpointSession")
                            {
                                session = reader.ReadElementContentAsString();
                                foundSession++;
                                advance = false;
                            }
                        }
                    }
                    if (stream.Length != initialLength) return Fail(stream.Length, "output_changed_during_read");
                }

                var after = new FileInfo(path);
                if (!after.Exists || after.Length != initialLength || after.LastWriteTimeUtc != initialWriteTime ||
                    (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
                    return Fail(after.Exists ? after.Length : 0, "output_replaced_or_changed_during_read");

                var hash = ToHex(digest);
                var associated = foundCampaign == 1 && foundCheckpoint == 1 && foundSession == 1 &&
                    campaign == campaignId && checkpoint == checkpointId && session == producerSessionId;
                return new SaveOutputVerification(associated, initialLength, hash,
                    associated ? null : "checkpoint_markers_missing_duplicated_or_mismatched");
            }
            catch (XmlException) { return Fail(0, "output_xml_invalid"); }
            catch (IOException) { return Fail(0, "output_io_error"); }
            catch (UnauthorizedAccessException) { return Fail(0, "output_access_denied"); }
            catch (Exception ex) { return Fail(0, "verification_error_" + ex.GetType().Name); }
        }

        private static SaveOutputVerification Fail(long bytes, string reason) => new SaveOutputVerification(false, bytes, null, reason);
        private static string ToHex(byte[] bytes) => BitConverter.ToString(bytes).Replace("-", "").ToLowerInvariant();
    }
}
