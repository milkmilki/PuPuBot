using StardewModdingAPI;

namespace PuPuStardewNpcMod;

public sealed class ModConfig
{
    public string BridgeUrl { get; set; } = "http://127.0.0.1:18787/chat";

    public string Token { get; set; } = "";

    public string SessionId { get; set; } = "owner";

    public string NpcInternalName { get; set; } = "PuPuBot_PuPu";

    public string NpcName { get; set; } = "仆仆";

    public string PortraitAssetName { get; set; } = "Portraits/PuPuBot_PuPu";

    public SButton TalkButton { get; set; } = SButton.MouseRight;

    public float InteractDistanceTiles { get; set; } = 2.0f;

    public int RequestTimeoutSeconds { get; set; } = 180;

    public bool IncludeGameContext { get; set; } = true;

    public bool ShowDialogueBox { get; set; } = true;

    public bool ShowHudNotification { get; set; } = false;
}
