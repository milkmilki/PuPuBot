using System.Text.Json.Serialization;

namespace PuPuStardewNpcMod;

public sealed class BridgeRequest
{
    [JsonPropertyName("text")]
    public string Text { get; set; } = "";

    [JsonPropertyName("session_id")]
    public string SessionId { get; set; } = "owner";

    [JsonPropertyName("source")]
    public string Source { get; set; } = "stardew_npc";

    [JsonPropertyName("context")]
    public Dictionary<string, object?> Context { get; set; } = new();
}

public sealed class BridgeResponse
{
    [JsonPropertyName("ok")]
    public bool Ok { get; set; }

    [JsonPropertyName("reply")]
    public string Reply { get; set; } = "";

    [JsonPropertyName("error")]
    public string Error { get; set; } = "";
}
