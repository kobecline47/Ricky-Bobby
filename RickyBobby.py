import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import asyncio
import os
import random
from collections import deque
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = os.getenv('GUILD_ID')

if not TOKEN:
    raise ValueError("DISCORD_TOKEN environment variable not set")

YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'default_search': 'ytsearch',
    'quiet': True,
    'no_warnings': True,
    'source_address': '0.0.0.0',
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


class Song:
    def __init__(self, data, requester=None):
        self.title = data.get('title', 'Unknown')
        self.url = data.get('url')
        self.webpage_url = data.get('webpage_url') or data.get('url', '')
        self.duration = data.get('duration', 0)
        self.thumbnail = data.get('thumbnail')
        self.requester = requester

    def duration_str(self):
        if not self.duration:
            return 'Live'
        m, s = divmod(int(self.duration), 60)
        h, m = divmod(m, 60)
        if h:
            return f'{h}:{m:02d}:{s:02d}'
        return f'{m}:{s:02d}'


class GuildMusicState:
    def __init__(self):
        self.queue = deque()
        self.current = None
        self.voice_client = None
        self.loop = False


guild_states = {}


def get_state(guild_id):
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildMusicState()
    return guild_states[guild_id]


async def fetch_song(query, requester=None):
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
        if 'entries' in data:
            data = data['entries'][0]
        return Song(data, requester)
    except Exception as e:
        print(f"yt-dlp error: {e}")
        return None


async def play_next(guild_id):
    state = get_state(guild_id)
    if not state.voice_client or not state.voice_client.is_connected():
        return

    next_song = None
    if state.loop and state.current:
        next_song = await fetch_song(state.current.webpage_url, state.current.requester)
    elif state.queue:
        queued = state.queue.popleft()
        next_song = await fetch_song(queued.webpage_url, queued.requester)

    if not next_song:
        state.current = None
        return

    state.current = next_song
    try:
        source = discord.FFmpegOpusAudio(next_song.url, **FFMPEG_OPTIONS)
        state.voice_client.play(
            source,
            after=lambda e: asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)
        )
    except Exception as e:
        print(f"Playback error: {e}")
        state.current = None


intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)


@bot.event
async def setup_hook():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"Synced {len(synced)} commands to guild {GUILD_ID}")
    else:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands globally")


@bot.event
async def on_ready():
    print(f'{bot.user} is online!')
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening,
        name='/play to start music'
    ))


async def ensure_voice(interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("You need to be in a voice channel first.", ephemeral=True)
        return None
    state = get_state(interaction.guild.id)
    channel = interaction.user.voice.channel
    if state.voice_client and state.voice_client.is_connected():
        if state.voice_client.channel != channel:
            await state.voice_client.move_to(channel)
    else:
        state.voice_client = await channel.connect()
    return state


@bot.tree.command(name="play", description="Play a song from YouTube")
@app_commands.describe(query="YouTube URL or search query")
async def play(interaction, query: str):
    await interaction.response.defer()
    state = await ensure_voice(interaction)
    if not state:
        return
    song = await fetch_song(query, interaction.user)
    if not song:
        await interaction.followup.send("Could not find or load that song.", ephemeral=True)
        return
    if state.voice_client.is_playing() or state.voice_client.is_paused():
        state.queue.append(song)
        embed = discord.Embed(
            title="Added to Queue",
            description=f"[{song.title}]({song.webpage_url})",
            color=discord.Color.blue()
        )
        embed.add_field(name="Duration", value=song.duration_str())
        embed.add_field(name="Position in Queue", value=str(len(state.queue)))
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)
    else:
        state.current = song
        try:
            source = discord.FFmpegOpusAudio(song.url, **FFMPEG_OPTIONS)
            state.voice_client.play(
                source,
                after=lambda e: asyncio.run_coroutine_threadsafe(play_next(interaction.guild.id), bot.loop)
            )
        except Exception as e:
            await interaction.followup.send(f"Error playing song: {e}", ephemeral=True)
            return
        embed = discord.Embed(
            title="Now Playing",
            description=f"[{song.title}]({song.webpage_url})",
            color=discord.Color.green()
        )
        embed.add_field(name="Duration", value=song.duration_str())
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction):
    state = get_state(interaction.guild.id)
    if not state.voice_client or not state.voice_client.is_playing():
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    state.voice_client.stop()
    await interaction.response.send_message("Skipped!")


@bot.tree.command(name="stop", description="Stop music and clear the queue")
async def stop(interaction):
    state = get_state(interaction.guild.id)
    state.queue.clear()
    state.loop = False
    state.current = None
    if state.voice_client:
        state.voice_client.stop()
    await interaction.response.send_message("Stopped and queue cleared.")


@bot.tree.command(name="pause", description="Pause the current song")
async def pause(interaction):
    state = get_state(interaction.guild.id)
    if state.voice_client and state.voice_client.is_playing():
        state.voice_client.pause()
        await interaction.response.send_message("Paused.")
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume the paused song")
async def resume(interaction):
    state = get_state(interaction.guild.id)
    if state.voice_client and state.voice_client.is_paused():
        state.voice_client.resume()
        await interaction.response.send_message("Resumed.")
    else:
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)


@bot.tree.command(name="queue", description="Show the current queue")
async def queue_cmd(interaction):
    state = get_state(interaction.guild.id)
    if not state.current and not state.queue:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return
    embed = discord.Embed(title="Music Queue", color=discord.Color.purple())
    if state.current:
        loop_tag = " (looping)" if state.loop else ""
        embed.add_field(
            name=f"Now Playing{loop_tag}",
            value=f"[{state.current.title}]({state.current.webpage_url}) `{state.current.duration_str()}`",
            inline=False
        )
    if state.queue:
        lines = []
        for i, s in enumerate(list(state.queue)[:10], 1):
            lines.append(f"`{i}.` [{s.title}]({s.webpage_url}) `{s.duration_str()}`")
        if len(state.queue) > 10:
            lines.append(f"... and {len(state.queue) - 10} more")
        embed.add_field(name="Up Next", value='\n'.join(lines), inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nowplaying", description="Show the currently playing song")
async def nowplaying(interaction):
    state = get_state(interaction.guild.id)
    if not state.current:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    embed = discord.Embed(
        title="Now Playing",
        description=f"[{state.current.title}]({state.current.webpage_url})",
        color=discord.Color.green()
    )
    embed.add_field(name="Duration", value=state.current.duration_str())
    if state.current.requester:
        embed.set_footer(text=f"Requested by {state.current.requester.display_name}")
    if state.current.thumbnail:
        embed.set_thumbnail(url=state.current.thumbnail)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="loop", description="Toggle loop for the current song")
async def loop_cmd(interaction):
    state = get_state(interaction.guild.id)
    state.loop = not state.loop
    status = "enabled" if state.loop else "disabled"
    await interaction.response.send_message(f"Loop {status}.")


@bot.tree.command(name="shuffle", description="Shuffle the queue")
async def shuffle(interaction):
    state = get_state(interaction.guild.id)
    if len(state.queue) < 2:
        await interaction.response.send_message("Not enough songs in queue to shuffle.", ephemeral=True)
        return
    q_list = list(state.queue)
    random.shuffle(q_list)
    state.queue = deque(q_list)
    await interaction.response.send_message(f"Shuffled {len(q_list)} songs.")


@bot.tree.command(name="remove", description="Remove a song from the queue by position")
@app_commands.describe(position="Position in queue (1 = next song)")
async def remove(interaction, position: int):
    state = get_state(interaction.guild.id)
    if position < 1 or position > len(state.queue):
        await interaction.response.send_message(
            f"Invalid position. Queue has {len(state.queue)} song(s).", ephemeral=True
        )
        return
    q_list = list(state.queue)
    removed = q_list.pop(position - 1)
    state.queue = deque(q_list)
    await interaction.response.send_message(f"Removed **{removed.title}** from the queue.")


@bot.tree.command(name="leave", description="Disconnect the bot from voice")
async def leave(interaction):
    state = get_state(interaction.guild.id)
    state.queue.clear()
    state.current = None
    state.loop = False
    if state.voice_client:
        await state.voice_client.disconnect()
        state.voice_client = None
    await interaction.response.send_message("Disconnected.")


bot.run(TOKEN)
