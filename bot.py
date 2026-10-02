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
    print(f"[WARN] OWNER_ID invalid: '{_owner_id_raw}' — check your .env!")

def is_owner(user) -> bool:
    if OWNER_ID == 0:
        print("[WARN] OWNER_ID not set! Add your Discord ID to .env")
        return False
    return user.id == OWNER_ID

# ─── Helpers ──────────────────────────────────────────────────────────────────
SUB_KEYWORDS     = ["subscribe", "subscribed", "abone ol", "abone olundu"]
LIKE_KEYWORDS    = ["like", "liked", "beğen"]
# Real comment indicators: Reply button only appears under actual posted comments
# "Yorum ekleyin" is just a placeholder — we ignore it
COMMENT_KEYWORDS = ["yanıtla", "reply", "yanıtlar", "replies"]

def analyse_image_ocr(image_bytes: bytes, channel_name: str) -> dict:
    """Run OCR and check for channel name + subscribe + like + real comment."""
    if not OCR_AVAILABLE:
        return {"ocr": False, "channel": False, "subscribe": False, "like": False, "comment": False}
    try:
        img = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(img, lang="tur+eng").lower()

        # Real comment = "yanıtla" or "reply" button visible (only appears under posted comments)
        # "Yorum ekleyin" is just the input placeholder — ignored
        comment_ok = any(k in text for k in COMMENT_KEYWORDS)

        return {
            "ocr": True,
            "text": text[:500],
            "channel":   channel_name.lower() in text if channel_name else True,
            "subscribe": any(k in text for k in SUB_KEYWORDS),
            "like":      any(k in text for k in LIKE_KEYWORDS),
            "comment":   comment_ok,
        }
    except Exception as e:
        print(f"[OCR ERROR] {e}")
        return {"ocr": False, "channel": False, "subscribe": False, "like": False, "comment": False}

def is_image(attachment: discord.Attachment) -> bool:
    return attachment.content_type is not None and attachment.content_type.startswith("image/")

# ─── Events ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[BOT] Logged in as {bot.user}")
    print(f"[BOT] Owner ID: {OWNER_ID}")
    print(f"[BOT] OCR: {'Enabled' if OCR_AVAILABLE else 'Disabled'}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.guild is None:
        await bot.process_commands(message)
        return

    print(f"[MSG] #{message.channel.name} | {message.author}: {message.content[:50]}")

    cfg = load_config()
    guild_cfg = cfg.get(str(message.guild.id), {})
    submit_channel_id = guild_cfg.get("submit_channel_id")

    if (
        submit_channel_id
        and message.channel.id == int(submit_channel_id)
        and message.attachments
    ):
        images = [a for a in message.attachments if is_image(a)]
        if images:
            await process_submission(message, images[0], guild_cfg)
            return  # don't process commands for image submissions

    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        return
    print(f"[CMD ERROR] {ctx.command} — {error}")

# ─── Submission Handler ───────────────────────────────────────────────────────
async def process_submission(message: discord.Message, attachment: discord.Attachment, guild_cfg: dict):
    guild = message.guild
    member = message.author

    role_id = guild_cfg.get("role_id")
    channel_name = guild_cfg.get("channel_name", "")
    log_channel_id = guild_cfg.get("log_channel_id")

    # Add processing reaction
    await message.add_reaction("⏳")

    # Read and analyse image
    image_bytes = await attachment.read()
    result = analyse_image_ocr(image_bytes, channel_name)

    if not result["ocr"]:
        # OCR unavailable — auto-approve without analysis
        await _approve(message, member, guild, role_id, log_channel_id, channel_name, result)
        return

    # Check results
    passed = result["subscribe"] and result["like"] and result["comment"]
    if channel_name:
        passed = passed and result["channel"]

    if passed:
        await _approve(message, member, guild, role_id, log_channel_id, channel_name, result)
    else:
        await _reject(message, member, guild, log_channel_id, channel_name, result)

async def _approve(message, member, guild, role_id, log_channel_id, channel_name, result):
    """Give role, react ✅, notify user, log."""
    # Remove ⏳, add ✅
    try:
        await message.remove_reaction("⏳", bot.user)
    except Exception:
        pass
    await message.add_reaction("✅")

    # Give role
    role = guild.get_role(int(role_id)) if role_id else None
    if role:
        try:
            await member.add_roles(role, reason="Subscription screenshot approved")
        except discord.Forbidden:
            print(f"[WARN] Can't assign role to {member}")

    # DM user
    try:
        await member.send(
            f"✅ **Your subscription screenshot was approved!**\n"
            f"Server: **{guild.name}**\n"
            + (f"Role assigned: **{role.name}**" if role else "")
        )
    except discord.Forbidden:
        pass

    # Log
    await _send_log(message, member, guild, log_channel_id, channel_name, result, approved=True, role=role)

async def _reject(message, member, guild, log_channel_id, channel_name, result):
    """React ❌, DM user with missing items, log."""
    try:
        await message.remove_reaction("⏳", bot.user)
    except Exception:
        pass
    await message.add_reaction("❌")

    # Build missing list
    missing = []
    if channel_name and not result.get("channel"):
        missing.append(f"Channel name not found: **{channel_name}**")
    if not result.get("subscribe"):
        missing.append("Subscribe not detected")
    if not result.get("like"):
        missing.append("Like not detected")
    if not result.get("comment"):
        missing.append("Comment not detected")

    try:
        await member.send(
            f"❌ **Your subscription screenshot was rejected.**\n"
            f"Server: **{guild.name}**\n\n"
            f"**Missing:**\n" + "\n".join(f"• {m}" for m in missing) + "\n\n"
            f"Please send a new screenshot with all items visible."
        )
    except discord.Forbidden:
        pass

    # Log
    await _send_log(message, member, guild, log_channel_id, channel_name, result, approved=False, role=None)

async def _send_log(message, member, guild, log_channel_id, channel_name, result, approved, role):
    """Send result embed to log channel."""
    if not log_channel_id:
        return
    log_channel = guild.get_channel(int(log_channel_id))
    if not log_channel:
        return

    color = discord.Color.green() if approved else discord.Color.red()
    status = "✅ Approved" if approved else "❌ Rejected"

    embed = discord.Embed(
        title=f"📋 Subscription Screenshot — {status}",
        color=color,
        timestamp=datetime.utcnow()
    )
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)
    embed.add_field(name="👤 User", value=member.mention, inline=True)
    embed.add_field(name="🆔 ID", value=str(member.id), inline=True)

    if result.get("ocr"):
        ch_val = ("✅" if result.get("channel") else "❌") if channel_name else "—"
        checks = (
            f"{'✅' if result.get('channel') else '❌'} Channel: {channel_name}\n" if channel_name else ""
        ) + (
            f"{'✅' if result.get('subscribe') else '❌'} Subscribe\n"
            f"{'✅' if result.get('like') else '❌'} Like\n"
            f"{'✅' if result.get('comment') else '❌'} Comment"
        )
        embed.add_field(name="🔍 OCR Results", value=checks, inline=False)
    else:
        embed.add_field(name="🔍 OCR", value="⚠️ OCR unavailable — auto-approved", inline=False)

    if role:
        embed.add_field(name="🎖️ Role", value=role.mention, inline=False)

    embed.set_image(url=message.attachments[0].url)
    embed.set_footer(text=f"#{message.channel.name}")
    await log_channel.send(embed=embed)

# ─── Owner Commands ───────────────────────────────────────────────────────────
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

@bot.command(name="setchannel")
@owner_only()
async def set_channel(ctx, *, channel_name: str = None):
    if channel_name is None:
        await ctx.send("❌ Usage: `!setchannel ChannelName`")
        return
    cfg = load_config()
    cfg.setdefault(str(ctx.guild.id), {})["channel_name"] = channel_name
    save_config(cfg)
    await ctx.send(f"✅ Channel name set to **{channel_name}** — bot will look for this in screenshots.")

@bot.command(name="setyoutubename")
@owner_only()
async def set_youtube_name(ctx, *, name: str = None):
    if name is None:
        await ctx.send("❌ Usage: `!setyoutubename YouTubeKullanıcıAdı`")
        return
    cfg = load_config()
    cfg.setdefault(str(ctx.guild.id), {})["youtube_name"] = name
    save_config(cfg)
    await ctx.send(f"✅ YouTube username set to **{name}** — bot will check this name appears next to comments.")

@bot.command(name="settings")
@owner_only()
async def show_settings(ctx):
    cfg = load_config()
    guild_cfg = cfg.get(str(ctx.guild.id), {})

    submit_ch = ctx.guild.get_channel(int(guild_cfg["submit_channel_id"])) if guild_cfg.get("submit_channel_id") else None
    log_ch    = ctx.guild.get_channel(int(guild_cfg["log_channel_id"]))    if guild_cfg.get("log_channel_id")    else None
    role      = ctx.guild.get_role(int(guild_cfg["role_id"]))              if guild_cfg.get("role_id")           else None
    ch_name   = guild_cfg.get("channel_name", "Not set")

    embed = discord.Embed(title="⚙️ Bot Settings", color=discord.Color.blurple())
    embed.add_field(name="📥 Submit Channel",   value=submit_ch.mention if submit_ch else "❌ Not set", inline=False)
    embed.add_field(name="📋 Log Channel",      value=log_ch.mention    if log_ch    else "❌ Not set", inline=False)
    embed.add_field(name="🎖️ Subscriber Role",  value=role.mention      if role      else "❌ Not set", inline=False)
    embed.add_field(name="📺 Channel Name",     value=f"**{ch_name}**",                                inline=False)
    embed.add_field(name="🔍 OCR",              value="✅ Enabled"      if OCR_AVAILABLE else "❌ Disabled", inline=False)
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
            "2. Bot automatically checks for **subscribe + like + comment**\n"
            "3. ✅ = approved + role given | ❌ = rejected + DM sent"
        ),
        inline=False
    )
    if is_owner(ctx.author):
        embed.add_field(
            name="⚙️ Owner Commands",
            value=(
                "`!setsubmit #channel` — Set screenshot submission channel\n"
                "`!setlog #channel` — Set log channel\n"
                "`!setrole @Role` — Set role given on approval\n"
                "`!setchannel Name` — Set YouTube channel name to look for\n"
                "`!settings` — View current settings\n"
                "`!otogecayarla #channel` — Set secret bypass channel\n"
                "`!help` — This menu"
            ),
            inline=False
        )
        embed.add_field(
            name="🔓 Gizli Komutlar",
            value=(
                "`!otogec` — Ayarlanan kanalda kullanıldığında direkt rol verir\n"
                "`!otogecayarla #kanal` — Gizli geçiş kanalını ayarla"
            ),
            inline=False
        )
    embed.set_footer(text="Owner commands are only visible to the bot owner.")
    await ctx.send(embed=embed)

@bot.command(name="abonessnasıl")
async def abone_ss_nasil(ctx):
    cfg = load_config()
    guild_cfg = cfg.get(str(ctx.guild.id), {}) if ctx.guild else {}
    submit_ch_id = guild_cfg.get("submit_channel_id")
    submit_ch = ctx.guild.get_channel(int(submit_ch_id)) if submit_ch_id and ctx.guild else None
    ch_name = guild_cfg.get("channel_name", "belirlenen YouTube kanalı")

    embed = discord.Embed(
        title="📋 Abone SS Nasıl Gönderilir?",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="📺 Adım 1 — Abone ol",
        value=f"**{ch_name}** YouTube kanalına abone ol.",
        inline=False
    )
    embed.add_field(
        name="👍 Adım 2 — Like at",
        value="Kanalın herhangi bir videosunu beğen.",
        inline=False
    )
    embed.add_field(
        name="💬 Adım 3 — Yorum yap",
        value="Videoya bir yorum bırak. Yorumun **görünür** olduğundan emin ol.",
        inline=False
    )
    embed.add_field(
        name="📸 Adım 4 — SS al",
        value=(
            "Ekran görüntüsünde şunların **hepsi görünür** olmalı:\n"
            "✅ Abone ol butonu (abone olundu hali)\n"
            "✅ Like butonu\n"
            "✅ Yorumun ve altındaki **Yanıtla** butonu"
        ),
        inline=False
    )
    embed.add_field(
        name=f"📤 Adım 5 — SS gönder",
        value=f"SS'yi {submit_ch.mention if submit_ch else '**#ss kanalına**'} gönder. Bot otomatik kontrol edecek.",
        inline=False
    )
    embed.set_footer(text="✅ Onaylanırsa rol verilir | ❌ Reddedilirse DM ile bildirim gelir")
    await ctx.send(embed=embed)

# ─── Gizli Bypass Komutları ───────────────────────────────────────────────────
@bot.command(name="otogecayarla")
@owner_only()
async def otogec_ayarla(ctx, channel: discord.TextChannel = None):
    """Sadece owner: !otogec komutunun çalışacağı kanalı ayarla."""
    if channel is None:
        await ctx.send("❌ Kullanım: `!otogecayarla #kanal`", delete_after=5)
        return
    cfg = load_config()
    cfg.setdefault(str(ctx.guild.id), {})["otogec_channel_id"] = str(channel.id)
    save_config(cfg)
    await ctx.send(f"✅ Gizli geçiş kanalı **{channel.mention}** olarak ayarlandı.", delete_after=5)

@bot.command(name="otogec")
async def otogec(ctx):
    """Gizli bypass: ayarlanan kanalda kullanıldığında direkt rol verir."""
    cfg = load_config()
    guild_cfg = cfg.get(str(ctx.guild.id), {}) if ctx.guild else {}
    otogec_ch_id = guild_cfg.get("otogec_channel_id")

    if not otogec_ch_id:
        return

    if ctx.channel.id != int(otogec_ch_id):
        return

    role_id = guild_cfg.get("role_id")
    role = ctx.guild.get_role(int(role_id)) if role_id else None

    if not role:
        return

    member = ctx.author
    if role in member.roles:
        await ctx.send(f"{member.mention} zaten role sahip.", delete_after=3)
        return

    try:
        await member.add_roles(role, reason="Gizli geçiş kodu kullanıldı")
        await ctx.send(f"✅ {member.mention}", delete_after=3)
    except discord.Forbidden:
        pass

# ─── Run ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        print("ERROR: Add your DISCORD_TOKEN to the .env file!")
        exit(1)
    bot.run(token)
