"""
Ricky Bobby - Disboard Bump Bot
Automatically bumps the server to Disboard every 2 hours
"""

import os
import discord
from discord.ext import commands, tasks
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

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Bot setup
bot = commands.Bot(command_prefix='!', intents=intents)

# Bump tracking
bump_data = {
    'last_bump': None,
    'bump_count': 0,
    'failed_bumps': 0,
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
        except:
            bump_data = {
                'last_bump': None,
                'bump_count': 0,
                'failed_bumps': 0,
            }
    return bump_data

def save_bump_data():
    """Save bump data to file"""
    with open(BUMP_DATA_FILE, 'w') as f:
        json.dump(bump_data, f, indent=2)

async def bump_server():
    """Execute a bump to Disboard"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(DISBOARD_WEBHOOK_URL) as resp:
                if resp.status == 200:
                    bump_data['last_bump'] = datetime.now().isoformat()
                    bump_data['bump_count'] += 1
                    save_bump_data()
                    return True, "✅ Server bumped successfully!"
                else:
                    bump_data['failed_bumps'] += 1
                    save_bump_data()
                    return False, f"❌ Bump failed with status {resp.status}"
    except Exception as e:
        bump_data['failed_bumps'] += 1
        save_bump_data()
        return False, f"❌ Bump error: {str(e)}"

@bot.event
async def on_ready():
    """Bot startup"""
    print(f"✅ {bot.user} is online!")
    print(f"Logged in as {bot.user.name}#{bot.user.discriminator}")
    
    # Load bump data
    load_bump_data()
    
    # Start background tasks
    auto_bump.start()
    
    # Set bot status
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="for bumps every 2 hours"
        )
    )

@bot.slash_command(name="bump", description="Manually bump the server to Disboard")
async def bump_command(interaction: discord.Interaction):
    """Manual bump command"""
    await interaction.response.defer(ephemeral=True)
    
    if not DISBOARD_WEBHOOK_URL:
        await interaction.followup.send(
            "❌ Disboard webhook not configured. Ask admin to set DISBOARD_WEBHOOK_URL.",
            ephemeral=True
        )
        return
    
    success, message = await bump_server()
    
    # Log the bump
    if LOG_CHANNEL_ID:
        try:
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                embed = discord.Embed(
                    title="🚀 Manual Bump Executed",
                    description=message,
                    color=discord.Color.green() if success else discord.Color.red(),
                    timestamp=datetime.now()
                )
                embed.add_field(name="Bumped by", value=interaction.user.mention)
                embed.add_field(name="Total Bumps", value=str(bump_data['bump_count']))
                embed.add_field(name="Failed Bumps", value=str(bump_data['failed_bumps']))
                await log_channel.send(embed=embed)
        except:
            pass
    
    await interaction.followup.send(message, ephemeral=True)

@bot.slash_command(name="bumpstats", description="View bump statistics")
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

@bot.slash_command(name="nextbump", description="Check when the next automatic bump is scheduled")
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
    """Automatic bump task that runs every N hours"""
    if not DISBOARD_WEBHOOK_URL:
        print("⚠️  Disboard webhook not configured, skipping auto-bump")
        return
    
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Running automatic bump...")
    success, message = await bump_server()
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")
    
    # Log the bump
    if LOG_CHANNEL_ID:
        try:
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                embed = discord.Embed(
                    title="🚀 Automatic Bump Executed",
                    description=message,
                    color=discord.Color.green() if success else discord.Color.red(),
                    timestamp=datetime.now()
                )
                embed.add_field(name="Total Bumps", value=str(bump_data['bump_count']))
                embed.add_field(name="Failed Bumps", value=str(bump_data['failed_bumps']))
                embed.add_field(name="Next Bump", value=f"in {BUMP_INTERVAL_HOURS} hours")
                await log_channel.send(embed=embed)
        except:
            pass

@auto_bump.before_loop
async def before_auto_bump():
    """Wait for bot to be ready before starting auto-bump task"""
    await bot.wait_until_ready()

# Run the bot
if __name__ == "__main__":
    bot.run(TOKEN)
