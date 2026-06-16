using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using Microsoft.Xna.Framework.Input;
using StardewValley;
using StardewValley.Menus;

namespace PuPuStardewNpcMod;

public sealed class PuPuChatMenu : IClickableMenu
{
    private readonly TextBox input;
    private readonly Func<string, Task> submit;
    private readonly string npcName;
    private readonly Texture2D? portrait;

    public PuPuChatMenu(string npcName, Texture2D? portrait, Func<string, Task> submit)
        : base(
            Math.Max(64, (Game1.uiViewport.Width - Math.Min(900, Game1.uiViewport.Width - 128)) / 2),
            Math.Max(64, Game1.uiViewport.Height - 260),
            Math.Min(900, Game1.uiViewport.Width - 128),
            180,
            true
        )
    {
        this.npcName = npcName;
        this.portrait = portrait;
        this.submit = submit;
        this.input = new TextBox(
            Game1.content.Load<Texture2D>("LooseSprites\\textBox"),
            null,
            Game1.smallFont,
            Game1.textColor
        )
        {
            X = this.xPositionOnScreen + (this.portrait is null ? 32 : 156),
            Y = this.yPositionOnScreen + 78,
            Width = this.width - (this.portrait is null ? 64 : 188),
            Height = 64,
            Selected = true,
        };
        Game1.keyboardDispatcher.Subscriber = this.input;
    }

    public override void receiveKeyPress(Keys key)
    {
        if (key == Keys.Escape)
        {
            this.exitThisMenu();
            return;
        }

        if (key == Keys.Enter)
        {
            this.SubmitAndClose();
            return;
        }

        base.receiveKeyPress(key);
    }

    public override void receiveGamePadButton(Buttons button)
    {
        if (button == Buttons.B)
        {
            this.exitThisMenu();
            return;
        }

        if (button is Buttons.A or Buttons.Start)
        {
            this.SubmitAndClose();
            return;
        }

        base.receiveGamePadButton(button);
    }

    public override void draw(SpriteBatch b)
    {
        b.Draw(Game1.fadeToBlackRect, Game1.graphics.GraphicsDevice.Viewport.Bounds, Color.Black * 0.35f);
        b.Draw(
            Game1.staminaRect,
            new Rectangle(this.xPositionOnScreen, this.yPositionOnScreen, this.width, this.height),
            Color.Black * 0.86f
        );
        this.DrawBorder(b);

        Utility.drawTextWithShadow(
            b,
            $"和{this.npcName}说：",
            Game1.smallFont,
            new Vector2(this.xPositionOnScreen + (this.portrait is null ? 32 : 156), this.yPositionOnScreen + 28),
            Color.White
        );
        if (this.portrait is not null)
        {
            Rectangle source = new(
                0,
                0,
                Math.Min(64, this.portrait.Width),
                Math.Min(64, this.portrait.Height)
            );
            b.Draw(
                this.portrait,
                new Rectangle(this.xPositionOnScreen + 28, this.yPositionOnScreen + 24, 96, 96),
                source,
                Color.White
            );
        }
        this.input.Draw(b);
        Utility.drawTextWithShadow(
            b,
            "Enter 发送  /  Esc 取消",
            Game1.tinyFont,
            new Vector2(this.xPositionOnScreen + (this.portrait is null ? 32 : 156), this.yPositionOnScreen + 145),
            Color.LightGray
        );
        this.drawMouse(b);
    }

    public override void emergencyShutDown()
    {
        this.ClearKeyboardSubscriber();
        base.emergencyShutDown();
    }

    public new void exitThisMenu(bool playSound = true)
    {
        this.ClearKeyboardSubscriber();
        base.exitThisMenu(playSound);
    }

    private void SubmitAndClose()
    {
        string text = this.input.Text?.Trim() ?? "";
        if (!string.IsNullOrWhiteSpace(text))
        {
            _ = this.submit(text);
        }
        this.exitThisMenu();
    }

    private void ClearKeyboardSubscriber()
    {
        if (Game1.keyboardDispatcher.Subscriber == this.input)
        {
            Game1.keyboardDispatcher.Subscriber = null;
        }
    }

    private void DrawBorder(SpriteBatch b)
    {
        Rectangle top = new(this.xPositionOnScreen, this.yPositionOnScreen, this.width, 4);
        Rectangle bottom = new(this.xPositionOnScreen, this.yPositionOnScreen + this.height - 4, this.width, 4);
        Rectangle left = new(this.xPositionOnScreen, this.yPositionOnScreen, 4, this.height);
        Rectangle right = new(this.xPositionOnScreen + this.width - 4, this.yPositionOnScreen, 4, this.height);
        b.Draw(Game1.staminaRect, top, Color.White * 0.75f);
        b.Draw(Game1.staminaRect, bottom, Color.White * 0.75f);
        b.Draw(Game1.staminaRect, left, Color.White * 0.75f);
        b.Draw(Game1.staminaRect, right, Color.White * 0.75f);
    }
}
