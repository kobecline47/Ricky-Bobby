"""
Ricky Bobby - Disboard Bump Bot
Automatically bumps the server to Disboard every 2 hours
"""

import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
from datetime import datetime, timedelta
import aiohttp
import json

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = int(os.getenv('GUILD_ID', 0))
DISBOARD_WEBHOOK_URL = os.getenv('DISBOARD_WEBHOOK_URL')
LOG_CHANNEL_ID = int(os.getenv('LOG_CHANNEL_ID', 0)) if os.getenv('LOG_CHANNEL_ID') else None
BUMP_INTERVAL_HOURS = int(os.getenv('BUMP_INTERVAL_HOURS', 2))
DISBOARD_BOT_ID = int(os.getenv('DISBOARD_BOT_ID', 302050872383242240))

# Validate required environment variables
if not TOKEN:
    raise ValueError("❌ DISCORD_TOKEN not set in environment variables!")
if not GUILD_ID or GUILD_ID == 0:
    raise ValueError("❌ GUILD_ID not set in environment variables!")
print(f"✅ Configuration loaded:")
print(f"   Guild ID: {GUILD_ID}")
print(f"   Bump interval: {BUMP_INTERVAL_HOURS} hours")
print(f"   Disboard bot id: {DISBOARD_BOT_ID}")
print(f"   Log channel: {LOG_CHANNEL_ID if LOG_CHANNEL_ID else 'Not set'}")

# Intents
intents = discord.Intents.default()
# We need message content to read Disboard confirmation messages.
intents.message_content = True
intents.members = False

# Bot setup
class RickyBobbyBot(commands.Bot):
    async def setup_hook(self):
        """Sync slash commands early so they appear quickly in the target guild."""
        try:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"✅ setup_hook: synced {len(synced)} command(s) to guild {GUILD_ID}", flush=True)
        except Exception as e:
            print(f"❌ setup_hook sync failed: {e}", flush=True)


bot = RickyBobbyBot(command_prefix='!', intents=intents)

# Bump tracking
bump_data = {
    'last_bump': None,
    'bump_count': 0,
    'failed_bumps': 0,
    'bump_channel_id': None,
}

# Save file for persistence
BUMP_DATA_FILE = 'bump_data.json'

def load_bump_data():
    """Load bump data from file"""
    global bump_data
    if os.path.exists(BUMP_DATA_FILE):
        try:
            with open(BUMP_DATA_FILE, 'r') as f:
                bump_data = json.load(f)
            # Backfill keys when loading older save files.
            bump_data.setdefault('last_bump', None)
            bump_data.setdefault('bump_count', 0)
            bump_data.setdefault('failed_bumps', 0)
            bump_data.setdefault('bump_channel_id', None)
        except:
            bump_data = {
                'last_bump': None,
                'bump_count': 0,
                'failed_bumps': 0,
                'bump_channel_id': None,
            }
    return bump_data

def save_bump_data():
    """Save bump data to file"""
    with open(BUMP_DATA_FILE, 'w') as f:
        json.dump(bump_data, f, indent=2)

async def bump_server():
    """Disboard bump cannot be executed by a bot; send instructions instead."""
    return True, "Run Disboard /bump now in this channel. I will track the confirmation and schedule reminders."


def get_notification_channel() -> discord.abc.Messageable | None:
    """Resolve where bump notifications should be posted."""
    channel_id = bump_data.get('bump_channel_id') or LOG_CHANNEL_ID
    if not channel_id:
        return None
    return bot.get_channel(int(channel_id))

@bot.event
async def on_ready():
    """Bot startup"""
    print(f"✅ {bot.user} is online!", flush=True)
    print(f"Logged in as {bot.user} (id={bot.user.id})", flush=True)
    print(
        "Guilds visible: "
        + ", ".join([f"{g.name}({g.id})" for g in bot.guilds])
        if bot.guilds
        else "Guilds visible: none",
        flush=True,
    )
    
    # Load bump data
    load_bump_data()
    print(f"📍 Bump channel: {bump_data.get('bump_channel_id') or 'Not set'}", flush=True)
    
    # Sync commands with Discord (guild-scoped)
    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"✅ on_ready: synced {len(synced)} command(s) to guild {GUILD_ID}", flush=True)
    except Exception as e:
        print(f"⚠️  on_ready sync failed: {e}", flush=True)
        try:
            synced = await bot.tree.sync()
            print(f"✅ on_ready: synced {len(synced)} command(s) globally (may take up to 1 hour)", flush=True)
        except Exception as e2:
            print(f"❌ Global sync also failed: {e2}", flush=True)
    
    # Start background tasks
    auto_bump.start()
    print(f"🚀 Auto-bump task started (every {BUMP_INTERVAL_HOURS} hours)", flush=True)
    
    # Set bot status
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"for bumps every {BUMP_INTERVAL_HOURS} hours"
        )
    )


@bot.event
async def on_message(message: discord.Message):
    """If the bot is mentioned in a channel, save that channel for bump notices."""
    if message.author.bot or not message.guild:
        return

    if bot.user and bot.user.mentioned_in(message):
        bump_data['bump_channel_id'] = message.channel.id
        save_bump_data()
        await message.channel.send(
            f"✅ Bump channel set to {message.channel.mention}. "
            f"I'll post Disboard /bump reminders here every {BUMP_INTERVAL_HOURS} hours."
        )

    # Track confirmed bumps from Disboard's bot response.
    if message.author.id == DISBOARD_BOT_ID:
        content = (message.content or "").lower()
        embed_text = ""
        if message.embeds:
            parts = []
            for emb in message.embeds:
                if emb.title:
                    parts.append(emb.title)
                if emb.description:
                    parts.append(emb.description)
            embed_text = " ".join(parts).lower()

        if "bump done" in content or "bump done" in embed_text or "please wait another" in content:
            bump_data['last_bump'] = datetime.now().isoformat()
            bump_data['bump_count'] += 1
            save_bump_data()
            await message.channel.send(
                f"✅ Bump confirmed. I will remind this channel again in {BUMP_INTERVAL_HOURS} hours."
            )

    await bot.process_commands(message)

@bot.tree.command(name="bump", description="Manually bump the server to Disboard")
async def bump_command(interaction: discord.Interaction):
    """Manual helper command for Disboard bumping"""
    await interaction.response.defer(ephemeral=True)
    success, message = await bump_server()
    
    # Log the bump
    notify_channel = get_notification_channel()
    if notify_channel:
        try:
            embed = discord.Embed(
                title="🚀 Disboard Bump Reminder",
                description=message,
                color=discord.Color.green() if success else discord.Color.red(),
                timestamp=datetime.now()
            )
            embed.add_field(name="Requested by", value=interaction.user.mention)
            embed.add_field(name="Total Bumps", value=str(bump_data['bump_count']))
            embed.add_field(name="Failed Bumps", value=str(bump_data['failed_bumps']))
            await notify_channel.send(embed=embed)
        except:
            pass
    
    await interaction.followup.send(
        "Use Disboard's /bump command now. I will detect the confirmation and track it automatically.",
        ephemeral=True
    )

@bot.tree.command(name="bumpstats", description="View bump statistics")
async def bumpstats_command(interaction: discord.Interaction):
    """Show bump statistics"""
    await interaction.response.defer(ephemeral=True)
    
    last_bump = bump_data.get('last_bump')
    if last_bump:
        last_bump_time = datetime.fromisoformat(last_bump)
        time_since = datetime.now() - last_bump_time
        last_bump_str = f"{last_bump_time.strftime('%Y-%m-%d %H:%M:%S')} ({time_since.days}d {time_since.seconds//3600}h ago)"
    else:
        last_bump_str = "Never"
    
    embed = discord.Embed(
        title="📊 Bump Statistics",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    embed.add_field(name="Total Bumps", value=str(bump_data['bump_count']), inline=False)
    embed.add_field(name="Failed Bumps", value=str(bump_data['failed_bumps']), inline=False)
    embed.add_field(name="Last Bump", value=last_bump_str, inline=False)
    embed.add_field(name="Bump Interval", value=f"Every {BUMP_INTERVAL_HOURS} hours", inline=False)
    embed.set_footer(text="Ricky Bobby Bump Bot")
    
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="setbumpchannel", description="Set the channel for auto bump notifications")
@app_commands.describe(channel="Channel where Ricky Bobby should post bump updates")
async def setbumpchannel_command(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    """Set or update the bump notification channel."""
    await interaction.response.defer(ephemeral=True)

    target_channel = channel or interaction.channel
    if target_channel is None:
        await interaction.followup.send("❌ Could not determine a channel.", ephemeral=True)
        return

    bump_data['bump_channel_id'] = target_channel.id
    save_bump_data()

    await interaction.followup.send(
        f"✅ Bump channel set to {target_channel.mention}.",
        ephemeral=True
    )

@bot.tree.command(name="nextbump", description="Check when the next automatic bump is scheduled")
async def nextbump_command(interaction: discord.Interaction):
    """Show next scheduled bump time"""
    await interaction.response.defer(ephemeral=True)
    
    last_bump = bump_data.get('last_bump')
    if last_bump:
        last_bump_time = datetime.fromisoformat(last_bump)
        next_bump = last_bump_time + timedelta(hours=BUMP_INTERVAL_HOURS)
        time_until = next_bump - datetime.now()
        hours = time_until.total_seconds() // 3600
        minutes = (time_until.total_seconds() % 3600) // 60
        next_bump_str = f"{next_bump.strftime('%Y-%m-%d %H:%M:%S')} (in {int(hours)}h {int(minutes)}m)"
    else:
        next_bump_str = "Next scheduled bump will run soon"
    
    embed = discord.Embed(
        title="⏰ Next Scheduled Bump",
        description=next_bump_str,
        color=discord.Color.gold(),
        timestamp=datetime.now()
    )
    embed.set_footer(text="Ricky Bobby Bump Bot")
    
    await interaction.followup.send(embed=embed, ephemeral=True)

@tasks.loop(hours=BUMP_INTERVAL_HOURS)
async def auto_bump():
    """Automatic reminder task that runs every N hours"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Running automatic bump reminder...")
    
    # Log the bump
    notify_channel = get_notification_channel()
    if notify_channel:
        try:
            embed = discord.Embed(
                title="⏰ Time To Bump On Disboard",
                description="Run Disboard's /bump command in this channel now.",
                color=discord.Color.blurple(),
                timestamp=datetime.now()
            )
            embed.add_field(name="Total Bumps", value=str(bump_data['bump_count']))
            embed.add_field(name="Failed Bumps", value=str(bump_data['failed_bumps']))
            embed.add_field(name="Next Bump", value=f"in {BUMP_INTERVAL_HOURS} hours")
            await notify_channel.send(embed=embed)
        except:
            pass

@auto_bump.before_loop
async def before_auto_bump():
    """Wait for bot to be ready before starting auto-bump task"""
    await bot.wait_until_ready()

# Run the bot
if __name__ == "__main__":
    bot.run(TOKEN)
