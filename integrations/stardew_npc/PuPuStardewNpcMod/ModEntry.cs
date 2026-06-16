using System.Collections.Concurrent;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using StardewModdingAPI;
using StardewModdingAPI.Events;
using StardewValley;
using StardewValley.Menus;

namespace PuPuStardewNpcMod;

public sealed class ModEntry : Mod
{
    private readonly ConcurrentQueue<string> pendingReplies = new();
    private HttpClient httpClient = new();
    private ModConfig config = new();
    private NPC? activeNpc;
    private Texture2D? portraitTexture;

    public override void Entry(IModHelper helper)
    {
        this.config = helper.ReadConfig<ModConfig>();
        this.httpClient = new HttpClient
        {
            Timeout = TimeSpan.FromSeconds(Math.Max(5, this.config.RequestTimeoutSeconds)),
        };

        helper.Events.Input.ButtonPressed += this.OnButtonPressed;
        helper.Events.GameLoop.UpdateTicked += this.OnUpdateTicked;
        helper.ConsoleCommands.Add(
            "pupu",
            "Send a message to the PuPu NPC. Usage: pupu 仆仆你在吗",
            this.OnConsoleCommand
        );
    }

    private void OnButtonPressed(object? sender, ButtonPressedEventArgs e)
    {
        if (!Context.IsWorldReady || Game1.activeClickableMenu is not null)
        {
            return;
        }

        if (e.Button != this.config.TalkButton)
        {
            return;
        }

        NPC? npc = this.FindPupuNpcAtCursor(e.Cursor.GrabTile) ?? this.FindNearbyPupuNpc();
        if (npc is null)
        {
            return;
        }

        if (!this.IsPlayerCloseEnough(npc))
        {
            return;
        }

        this.Helper.Input.Suppress(e.Button);
        this.OpenChatMenu(npc);
    }

    private void OnConsoleCommand(string command, string[] args)
    {
        string text = string.Join(" ", args).Trim();
        if (string.IsNullOrWhiteSpace(text))
        {
            this.Monitor.Log("Usage: pupu 仆仆你在吗", LogLevel.Info);
            return;
        }

        this.activeNpc = this.FindPupuNpcInCurrentLocation();
        _ = this.SendMessageAsync(text);
    }

    private void OpenChatMenu(NPC npc)
    {
        this.activeNpc = npc;
        Game1.activeClickableMenu = new PuPuChatMenu(
            this.config.NpcName,
            this.GetPortraitTexture(),
            this.SendMessageAsync
        );
    }

    private async Task SendMessageAsync(string text)
    {
        text = text.Trim();
        if (string.IsNullOrWhiteSpace(text))
        {
            return;
        }

        try
        {
            BridgeRequest payload = new()
            {
                Text = text,
                SessionId = this.config.SessionId,
                Context = this.config.IncludeGameContext
                    ? this.BuildGameContext(this.activeNpc)
                    : new Dictionary<string, object?>(),
            };

            using HttpRequestMessage request = new(HttpMethod.Post, this.config.BridgeUrl)
            {
                Content = new StringContent(
                    JsonSerializer.Serialize(payload),
                    Encoding.UTF8,
                    "application/json"
                ),
            };

            if (!string.IsNullOrWhiteSpace(this.config.Token))
            {
                request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", this.config.Token);
                request.Headers.TryAddWithoutValidation("X-PuPu-Token", this.config.Token);
            }

            using HttpResponseMessage response = await this.httpClient.SendAsync(request);
            string body = await response.Content.ReadAsStringAsync();
            if (!response.IsSuccessStatusCode)
            {
                this.pendingReplies.Enqueue($"PuPu bridge error: HTTP {(int)response.StatusCode} {body}");
                return;
            }

            BridgeResponse? bridgeResponse = JsonSerializer.Deserialize<BridgeResponse>(
                body,
                new JsonSerializerOptions { PropertyNameCaseInsensitive = true }
            );
            string reply = bridgeResponse?.Reply?.Trim() ?? "";
            if (string.IsNullOrWhiteSpace(reply))
            {
                reply = bridgeResponse?.Error?.Trim() ?? "PuPu bridge returned an empty reply.";
            }
            this.pendingReplies.Enqueue(reply);
        }
        catch (Exception ex)
        {
            this.Monitor.Log($"Failed to talk to PuPu bridge: {ex}", LogLevel.Warn);
            this.pendingReplies.Enqueue("连不上 PuPu bridge。先确认 scripts/run_stardew_npc_bridge.bat 开着。");
        }
    }

    private Dictionary<string, object?> BuildGameContext(NPC? npc)
    {
        Farmer? player = Game1.player;
        int hearts = 0;
        try
        {
            if (player is not null && player.friendshipData.TryGetValue(this.config.NpcInternalName, out Friendship friendship))
            {
                hearts = Math.Max(0, friendship.Points / 250);
            }
        }
        catch
        {
            hearts = 0;
        }

        Vector2? npcTile = npc is null ? null : TileFromPosition(npc.Position);
        return new Dictionary<string, object?>
        {
            ["npc_name"] = this.config.NpcName,
            ["npc_internal_name"] = this.config.NpcInternalName,
            ["npc_tile"] = npcTile is null ? "" : $"{npcTile.Value.X:0},{npcTile.Value.Y:0}",
            ["hearts"] = hearts,
            ["player"] = player?.Name ?? "",
            ["farm"] = player?.farmName.Value ?? "",
            ["location"] = player?.currentLocation?.NameOrUniqueName ?? "",
            ["season"] = Game1.currentSeason,
            ["day"] = Game1.dayOfMonth,
            ["year"] = Game1.year,
            ["time"] = Game1.timeOfDay,
            ["weather"] = Game1.isRaining ? "rain" : "clear",
            ["money"] = player?.Money ?? 0,
        };
    }

    private void OnUpdateTicked(object? sender, UpdateTickedEventArgs e)
    {
        while (this.pendingReplies.TryDequeue(out string? reply))
        {
            this.DisplayReply(reply);
        }
    }

    private void DisplayReply(string reply)
    {
        reply = reply.Trim();
        if (string.IsNullOrWhiteSpace(reply))
        {
            return;
        }

        if (this.config.ShowDialogueBox && Context.IsWorldReady)
        {
            Game1.drawObjectDialogue(reply);
            return;
        }

        if (this.config.ShowHudNotification && Context.IsWorldReady)
        {
            Game1.addHUDMessage(new HUDMessage(reply, HUDMessage.newQuest_type));
            return;
        }

        this.Monitor.Log($"{this.config.NpcName}: {reply}", LogLevel.Info);
    }

    private NPC? FindPupuNpcAtCursor(Vector2 cursorTile)
    {
        if (Game1.currentLocation is null)
        {
            return null;
        }

        foreach (NPC npc in Game1.currentLocation.characters)
        {
            if (!this.IsPupuNpc(npc))
            {
                continue;
            }

            if (Vector2.Distance(TileFromPosition(npc.Position), cursorTile) <= 1.25f)
            {
                return npc;
            }
        }
        return null;
    }

    private NPC? FindNearbyPupuNpc()
    {
        if (Game1.currentLocation is null)
        {
            return null;
        }

        foreach (NPC npc in Game1.currentLocation.characters)
        {
            if (this.IsPupuNpc(npc) && this.IsPlayerCloseEnough(npc))
            {
                return npc;
            }
        }
        return null;
    }

    private NPC? FindPupuNpcInCurrentLocation()
    {
        if (Game1.currentLocation is null)
        {
            return null;
        }

        foreach (NPC npc in Game1.currentLocation.characters)
        {
            if (this.IsPupuNpc(npc))
            {
                return npc;
            }
        }
        return null;
    }

    private bool IsPupuNpc(NPC npc)
    {
        return string.Equals(npc.Name, this.config.NpcInternalName, StringComparison.OrdinalIgnoreCase);
    }

    private bool IsPlayerCloseEnough(NPC npc)
    {
        Vector2 playerTile = TileFromPosition(Game1.player.Position);
        return Vector2.Distance(playerTile, TileFromPosition(npc.Position)) <= Math.Max(0.5f, this.config.InteractDistanceTiles);
    }

    private static Vector2 TileFromPosition(Vector2 position)
    {
        return new Vector2(
            (float)Math.Floor(position.X / Game1.tileSize),
            (float)Math.Floor(position.Y / Game1.tileSize)
        );
    }

    private Texture2D? GetPortraitTexture()
    {
        if (this.portraitTexture is not null)
        {
            return this.portraitTexture;
        }

        string assetName = string.IsNullOrWhiteSpace(this.config.PortraitAssetName)
            ? $"Portraits/{this.config.NpcInternalName}"
            : this.config.PortraitAssetName;
        try
        {
            this.portraitTexture = Game1.content.Load<Texture2D>(assetName.Replace("/", "\\"));
        }
        catch (Exception ex)
        {
            this.Monitor.Log(
                $"Could not load PuPu portrait asset '{assetName}'. {ex.Message}",
                LogLevel.Warn
            );
        }
        return this.portraitTexture;
    }
}
