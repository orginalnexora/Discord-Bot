import discord
from discord.ext import commands
import json
import os
import io
from datetime import datetime
from dotenv import load_dotenv

# Optional OCR support
try:
    from PIL import Image
    import pytesseract
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────
CONFIG_FILE = "config.json"

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_config(data: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ─── Bot setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ─── Owner check ──────────────────────────────────────────────────────────────
_owner_id_raw = os.getenv("OWNER_ID", "0").strip()
try:
    OWNER_ID = int(_owner_id_raw)
except ValueError:
    OWNER_ID = 0
    print(f"[WARN] OWNER_ID geçersiz: '{_owner_id_raw}' — .env dosyasını kontrol et!")

def is_owner(user) -> bool:
    if OWNER_ID == 0:
        print("[WARN] OWNER_ID ayarlanmamış! .env'e Discord ID'ni ekle.")
        return False
    return user.id == OWNER_ID

# ─── Helpers ──────────────────────────────────────────────────────────────────
SUB_KEYWORDS = [
    "subscribe", "subscribed", "abone",
    "like", "liked",
    "comment", "commented", "yorum",
]

def analyse_image_ocr(image_bytes: bytes) -> dict:
    if not OCR_AVAILABLE:
        return {"ocr": False, "found": [], "text": ""}
    try:
        img = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(img, lang="tur+eng").lower()
        found = [kw for kw in SUB_KEYWORDS if kw in text]
        return {"ocr": True, "found": found, "text": text[:500]}
    except Exception as e:
        print(f"[OCR ERROR] {e}")
        return {"ocr": False, "found": [], "text": ""}

def is_image(attachment: discord.Attachment) -> bool:
    return attachment.content_type is not None and attachment.content_type.startswith("image/")

# ─── Approval View ────────────────────────────────────────────────────────────
class ApprovalView(discord.ui.View):
    def __init__(self, submitter_id: int, guild_id: int):
        super().__init__(timeout=None)
        self.submitter_id = submitter_id
        self.guild_id = guild_id

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success, custom_id="approve_btn")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction.user):
            await interaction.response.send_message("❌ Only the bot owner can use this.", ephemeral=True)
            return

        cfg = load_config()
        role_id = cfg.get(str(self.guild_id), {}).get("role_id")
        guild = interaction.guild
        role = guild.get_role(int(role_id)) if role_id else None

        member = guild.get_member(self.submitter_id)
        if member is None:
            try:
                member = await guild.fetch_member(self.submitter_id)
            except Exception:
                await interaction.response.send_message("⚠️ Member not found.", ephemeral=True)
                return

        if role:
            try:
                await member.add_roles(role, reason="Subscription screenshot approved")
            except discord.Forbidden:
                await interaction.response.send_message("❌ Missing permissions to assign role.", ephemeral=True)
                return

        try:
            await member.send(
                f"✅ **Your subscription screenshot has been approved!**\n"
                f"Server: **{guild.name}**\n"
                + (f"Role assigned: **{role.name}**" if role else "")
            )
        except discord.Forbidden:
            pass

        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.color = discord.Color.green()
        embed.set_footer(text=f"✅ Approved by {interaction.user} • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        for child in self.children:
            child.disabled = True

        await interaction.message.edit(embed=embed, view=self)
        await interaction.response.send_message(f"✅ {member.mention} approved and notified.", ephemeral=True)

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger, custom_id="reject_btn")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction.user):
            await interaction.response.send_message("❌ Only the bot owner can use this.", ephemeral=True)
            return

        guild = interaction.guild
        member = guild.get_member(self.submitter_id)
        if member is None:
            try:
                member = await guild.fetch_member(self.submitter_id)
            except Exception:
                member = None

        if member:
            try:
                await member.send(
                    f"❌ **Your subscription screenshot was rejected.**\n"
                    f"Server: **{guild.name}**\n"
                    f"Please send a valid screenshot showing: subscribe + like + comment."
                )
            except discord.Forbidden:
                pass

        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.color = discord.Color.red()
        embed.set_footer(text=f"❌ Rejected by {interaction.user} • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        for child in self.children:
            child.disabled = True

        await interaction.message.edit(embed=embed, view=self)
        await interaction.response.send_message(
            f"❌ {member.mention if member else 'User'} rejected and notified.", ephemeral=True
        )

# ─── Events ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[BOT] Logged in as {bot.user}")
    print(f"[BOT] Owner ID: {OWNER_ID}")
    print(f"[BOT] OCR: {'Enabled' if OCR_AVAILABLE else 'Disabled'}")
    print(f"[BOT] Prefix: !")
    bot.add_view(ApprovalView(submitter_id=0, guild_id=0))

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # DM guard
    if message.guild is None:
        await bot.process_commands(message)
        return

    print(f"[MSG] #{message.channel.name} | {message.author}: {message.content[:50]}")

    cfg = load_config()
    guild_cfg = cfg.get(str(message.guild.id), {})
    submit_channel_id = guild_cfg.get("submit_channel_id")
    log_channel_id = guild_cfg.get("log_channel_id")

    if (
        submit_channel_id
        and message.channel.id == int(submit_channel_id)
        and message.attachments
    ):
        images = [a for a in message.attachments if is_image(a)]
        if images:
            await process_submission(message, images[0], log_channel_id)

    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    """Catch and display command errors in console."""
    if isinstance(error, commands.CheckFailure):
        return  # Silently ignore access denied
    print(f"[CMD ERROR] {ctx.command} — {error}")
    await ctx.send(f"❌ Error: `{error}`", delete_after=8)

async def process_submission(message: discord.Message, attachment: discord.Attachment, log_channel_id):
    await message.reply(
        "📸 **Screenshot received!** You'll be notified once it's reviewed.",
        delete_after=15
    )

    image_bytes = await attachment.read()
    ocr_result = analyse_image_ocr(image_bytes)

    embed = discord.Embed(
        title="📋 New Subscription Screenshot",
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow()
    )
    embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
    embed.add_field(name="👤 User", value=message.author.mention, inline=True)
    embed.add_field(name="🆔 ID", value=str(message.author.id), inline=True)
    embed.add_field(name="📅 Date", value=discord.utils.format_dt(message.created_at, style="f"), inline=False)

    if ocr_result["ocr"]:
        found = ocr_result["found"]
        checks = {
            "Subscribe": any(k in found for k in ["subscribe", "subscribed", "abone"]),
            "Like":      any(k in found for k in ["like", "liked"]),
            "Comment":   any(k in found for k in ["comment", "commented", "yorum"]),
        }
        check_text = "\n".join(f"{'✅' if v else '❓'} {k}" for k, v in checks.items())
        embed.add_field(name="🔍 OCR Analysis", value=check_text, inline=False)
    else:
        embed.add_field(
            name="🔍 OCR Analysis",
            value="⚠️ OCR unavailable — manual review required",
            inline=False
        )

    embed.set_image(url=attachment.url)
    embed.set_footer(text="Awaiting manual approval...")

    if log_channel_id:
        log_channel = message.guild.get_channel(int(log_channel_id))
        if log_channel:
            view = ApprovalView(submitter_id=message.author.id, guild_id=message.guild.id)
            await log_channel.send(embed=embed, view=view)
        else:
            print(f"[WARN] Log channel {log_channel_id} not found!")
    else:
        print("[WARN] Log channel not set! Use !setlog #channel")

# ─── Commands ─────────────────────────────────────────────────────────────────
def owner_only():
    async def predicate(ctx):
        if not is_owner(ctx.author):
            await ctx.send("❌ Only the bot owner can use this command.", delete_after=5)
            return False
        return True
    return commands.check(predicate)

@bot.command(name="setsubmit")
@owner_only()
async def set_submit(ctx, channel: discord.TextChannel = None):
    if channel is None:
        await ctx.send("❌ Usage: `!setsubmit #channel`")
        return
    cfg = load_config()
    cfg.setdefault(str(ctx.guild.id), {})["submit_channel_id"] = str(channel.id)
    save_config(cfg)
    await ctx.send(f"✅ Submit channel set to {channel.mention}")

@bot.command(name="setlog")
@owner_only()
async def set_log(ctx, channel: discord.TextChannel = None):
    if channel is None:
        await ctx.send("❌ Usage: `!setlog #channel`")
        return
    cfg = load_config()
    cfg.setdefault(str(ctx.guild.id), {})["log_channel_id"] = str(channel.id)
    save_config(cfg)
    await ctx.send(f"✅ Log channel set to {channel.mention}")

@bot.command(name="setrole")
@owner_only()
async def set_role(ctx, role: discord.Role = None):
    if role is None:
        await ctx.send("❌ Usage: `!setrole @Role`")
        return
    cfg = load_config()
    cfg.setdefault(str(ctx.guild.id), {})["role_id"] = str(role.id)
    save_config(cfg)
    await ctx.send(f"✅ Subscriber role set to {role.mention}")

@bot.command(name="settings")
@owner_only()
async def show_settings(ctx):
    cfg = load_config()
    guild_cfg = cfg.get(str(ctx.guild.id), {})

    submit_ch = ctx.guild.get_channel(int(guild_cfg["submit_channel_id"])) if guild_cfg.get("submit_channel_id") else None
    log_ch    = ctx.guild.get_channel(int(guild_cfg["log_channel_id"]))    if guild_cfg.get("log_channel_id")    else None
    role      = ctx.guild.get_role(int(guild_cfg["role_id"]))              if guild_cfg.get("role_id")           else None

    embed = discord.Embed(title="⚙️ Bot Settings", color=discord.Color.blurple())
    embed.add_field(name="📥 Submit Channel",  value=submit_ch.mention if submit_ch else "❌ Not set", inline=False)
    embed.add_field(name="📋 Log Channel",     value=log_ch.mention    if log_ch    else "❌ Not set", inline=False)
    embed.add_field(name="🎖️ Subscriber Role", value=role.mention      if role      else "❌ Not set", inline=False)
    embed.add_field(name="🔍 OCR",             value="✅ Enabled"      if OCR_AVAILABLE else "❌ Disabled", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(
        title="📖 Subscription SS Bot — Help",
        description="Subscription screenshot verification bot.",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="📥 How to use",
        value=(
            "1. Send your screenshot to the submission channel\n"
            "2. Make sure **subscribe + like + comment** are visible\n"
            "3. Wait for the owner to approve — you'll get a DM"
        ),
        inline=False
    )
    if is_owner(ctx.author):
        embed.add_field(
            name="⚙️ Owner Commands",
            value=(
                "`!setsubmit #channel` — Set the screenshot submission channel\n"
                "`!setlog #channel` — Set the mod/log channel\n"
                "`!setrole @Role` — Set the role given after approval\n"
                "`!settings` — View current settings\n"
                "`!help` — This menu"
            ),
            inline=False
        )
    embed.set_footer(text="Owner commands are only visible to the bot owner.")
    await ctx.send(embed=embed)

# ─── Run ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        print("ERROR: Add your DISCORD_TOKEN to the .env file!")
        exit(1)
    bot.run(token)
