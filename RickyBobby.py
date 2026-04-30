"""
RickyBobby - GzVibe Music Bot
Ported from GamingZone Main1.py — full GzVibe Control Deck with autoplay engine.
"""

import asyncio
import base64
import collections
import json
import os
import platform as _platform
import re
import shutil as _shutil
import time
import traceback
import urllib.parse
import urllib.request

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID_STR = os.getenv("GUILD_ID", "").strip()
GUILD_ID = discord.Object(id=int(GUILD_ID_STR)) if GUILD_ID_STR else None

if not TOKEN:
    raise ValueError("DISCORD_TOKEN environment variable not set")

STARTUP_MARKER = f"boot-{int(time.time())}-{os.getpid()}"

# ---------------------------------------------------------------------------
# FFmpeg resolution
# ---------------------------------------------------------------------------

def _is_usable_ffmpeg(path: str | None) -> bool:
    if not path:
        return False
    if not os.path.exists(path):
        return False
    try:
        import subprocess
        subprocess.run([path, "-version"], capture_output=True, text=True, timeout=8, check=False)
        return True
    except Exception:
        return False


def _resolve_ffmpeg_executable() -> str:
    for candidate in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/bin/ffmpeg"):
        if _is_usable_ffmpeg(candidate):
            return candidate
    system_ffmpeg = _shutil.which("ffmpeg")
    if _is_usable_ffmpeg(system_ffmpeg):
        return system_ffmpeg
    try:
        import static_ffmpeg
        static_ffmpeg.add_paths()
        bundled = _shutil.which("ffmpeg")
        if _is_usable_ffmpeg(bundled):
            return bundled
    except Exception:
        pass
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if _is_usable_ffmpeg(exe):
            return exe
    except Exception:
        pass
    env_ffmpeg = os.getenv("FFMPEG_PATH", "").strip()
    if _is_usable_ffmpeg(env_ffmpeg):
        return env_ffmpeg
    return "ffmpeg"


FFMPEG_EXE = _resolve_ffmpeg_executable()
print(f"[Music] Using FFmpeg: {FFMPEG_EXE}")

FFMPEG_OPTS = {
    "executable": FFMPEG_EXE,
    "before_options": "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 -reconnect_delay_max 10 -rw_timeout 15000000",
    "options": "-vn -sn -dn -bufsize 256k",
}

YTDL_STREAM_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "format": "bestaudio[ext=m4a]/bestaudio/best",
    "source_address": "0.0.0.0",
    "extractor_args": {"youtube": {"player_client": ["android", "web", "tv_embedded"]}},
}

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class SongEntry:
    def __init__(self, title: str, url: str, webpage_url: str, duration: int,
                 requester: discord.Member, local_path: str | None = None):
        self.title = title
        self.url = url
        self.webpage_url = webpage_url
        try:
            self.duration = int(float(duration or 0))
        except (TypeError, ValueError):
            self.duration = 0
        self.requester = requester
        self.local_path = local_path

    def format_duration(self) -> str:
        m, s = divmod(int(self.duration or 0), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class GuildMusicState:
    def __init__(self):
        self.queue: collections.deque[SongEntry] = collections.deque()
        self.current: SongEntry | None = None
        self.last_finished: SongEntry | None = None
        self.recent_track_ids: collections.deque[str] = collections.deque(maxlen=20)
        self.recent_title_keys: collections.deque[str] = collections.deque(maxlen=20)
        self.recent_artist_keys: collections.deque[str] = collections.deque(maxlen=12)
        self.voice_client: discord.VoiceClient | None = None
        self.volume: float = 0.5
        self.now_playing_msg: discord.Message | None = None
        self.source_transformer: discord.PCMVolumeTransformer | None = None
        self.autoplay: bool = False
        self.autoplay_mode: str = "gzvibe"
        self.last_autoplay_debug: list[dict] = []
        self.retry_attempts: dict[str, int] = {}


music_states: dict[int, GuildMusicState] = {}


def get_music_state(guild_id: int) -> GuildMusicState:
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState()
    return music_states[guild_id]


# ---------------------------------------------------------------------------
# Playlist persistence
# ---------------------------------------------------------------------------

def _music_data_dir() -> str:
    explicit = os.getenv("MUSIC_DATA_DIR", "").strip()
    if explicit:
        os.makedirs(explicit, exist_ok=True)
        return explicit
    railway_mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if railway_mount:
        os.makedirs(railway_mount, exist_ok=True)
        return railway_mount
    if os.path.isdir("/data"):
        return "/data"
    return os.path.dirname(os.path.abspath(__file__))


_PLAYLISTS_PATH = os.path.join(_music_data_dir(), "music_playlists.json")
MUSIC_PLAYLISTS: dict[str, dict] = {"guilds": {}}


def _load_music_playlists() -> None:
    global MUSIC_PLAYLISTS
    if not os.path.exists(_PLAYLISTS_PATH):
        return
    try:
        with open(_PLAYLISTS_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict) and isinstance(loaded.get("guilds", {}), dict):
            MUSIC_PLAYLISTS = loaded
    except Exception as e:
        print(f"[Playlist] Failed to load: {e}")


def _save_music_playlists() -> None:
    try:
        os.makedirs(os.path.dirname(_PLAYLISTS_PATH), exist_ok=True)
        tmp = _PLAYLISTS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(MUSIC_PLAYLISTS, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _PLAYLISTS_PATH)
    except Exception as e:
        print(f"[Playlist] Failed to save: {e}")


def _playlist_bucket(guild_id: int, user_id: int) -> dict[str, list[dict]]:
    guilds = MUSIC_PLAYLISTS.setdefault("guilds", {})
    g = guilds.setdefault(str(guild_id), {})
    return g.setdefault(str(user_id), {})


def _playlist_track(song: SongEntry) -> dict:
    return {"title": song.title, "webpage_url": song.webpage_url or "", "duration": int(song.duration or 0)}


def _playlist_has_track(tracks: list[dict], track: dict) -> bool:
    tu = (track.get("webpage_url") or "").strip()
    tt = (track.get("title") or "").strip().casefold()
    for item in tracks:
        if tu and (item.get("webpage_url") or "").strip() == tu:
            return True
        if tt and (item.get("title") or "").strip().casefold() == tt:
            return True
    return False


def _gzvibe_playlist_embed(title: str, description: str, color: int = 0x1DB954) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text="GzVibe Playlist • RickyBobby")
    return embed


_load_music_playlists()

# ---------------------------------------------------------------------------
# YouTube helpers
# ---------------------------------------------------------------------------

def _youtube_video_id(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.netloc or "").lower()
        if "youtu.be" in host:
            return (parsed.path or "").lstrip("/").split("/")[0]
        if "youtube.com" in host or "youtube-nocookie.com" in host:
            q = urllib.parse.parse_qs(parsed.query)
            if q.get("v"):
                return q["v"][0]
            if q.get("id"):
                return q["id"][0]
            parts = [p for p in (parsed.path or "").split("/") if p]
            if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
                return parts[1]
        if "googlevideo.com" in host:
            q = urllib.parse.parse_qs(parsed.query)
            if q.get("id"):
                return q["id"][0]
    except Exception:
        pass
    return ""


def _youtube_thumbnail(url: str) -> str:
    vid = _youtube_video_id(url)
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ""


# ---------------------------------------------------------------------------
# Title/artist key helpers
# ---------------------------------------------------------------------------

def _normalized_title_key(title: str) -> str:
    t = (title or "").lower()
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"\[[^\]]*\]", " ", t)
    for n in ["official audio", "official video", "official music video", "music video",
              "lyrics", "lyric video", "audio", "video", "remaster", "remastered", "hq", "hd", "topic"]:
        t = t.replace(n, " ")
    t = re.sub(r"\b(?:19|20)\d{2}\b", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _song_core_key(title: str) -> str:
    raw = (title or "").lower()
    raw = re.sub(r"\([^)]*\)", " ", raw)
    raw = re.sub(r"\[[^\]]*\]", " ", raw)
    if " - " in raw:
        rhs = raw.split(" - ", 1)[1].strip()
        rhs_key = _normalized_title_key(rhs)
        if rhs_key:
            return rhs_key
    if " by " in raw:
        lhs = raw.split(" by ", 1)[0].strip()
        lhs_key = _normalized_title_key(lhs)
        if lhs_key:
            return lhs_key
    return _normalized_title_key(raw)


def _same_song_key(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    shorter = min(len(a), len(b))
    return shorter >= 6 and (a in b or b in a)


def _titles_too_similar(seed: str, candidate: str) -> bool:
    a = _song_core_key(seed)
    b = _song_core_key(candidate)
    if not a or not b:
        return False
    if _same_song_key(a, b):
        return True
    ta = [t for t in a.split() if t]
    tb = [t for t in b.split() if t]
    if not ta or not tb:
        return False
    overlap = len(set(ta).intersection(tb))
    min_len = max(1, min(len(ta), len(tb)))
    if overlap >= 2 and overlap / min_len >= 0.75:
        return True
    lead_a = " ".join(ta[:2])
    lead_b = " ".join(tb[:2])
    if len(lead_a) >= 6 and lead_a == lead_b:
        return True
    return False


def _song_signature_tokens(title: str) -> list[str]:
    core = _song_core_key(title)
    if not core:
        return []
    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with"}
    return [t for t in core.split() if len(t) >= 3 and t not in stop][:4]


def _autoplay_query_seed(song: SongEntry) -> str:
    artist = _song_artist_key(song)
    core = _song_core_key(song.title)
    if artist and core:
        return f"{artist} {core}"
    return core or artist or song.title


def _autoplay_title_tokens(title: str) -> set[str]:
    core = _song_core_key(title)
    if not core:
        return set()
    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with"}
    return {t for t in core.split() if len(t) >= 3 and t not in stop}


def _artist_key_from_title(title: str) -> str:
    raw = (title or "").lower()
    raw = re.sub(r"\([^)]*\)", " ", raw)
    raw = re.sub(r"\[[^\]]*\]", " ", raw)
    artist_part = ""
    if " - " in raw:
        artist_part = raw.split(" - ", 1)[0].strip()
    elif " by " in raw:
        artist_part = raw.split(" by ", 1)[1].strip()
    artist_part = artist_part.split("|")[0].split("/")[0].split(",")[0].strip()
    artist_part = re.sub(r"\b(feat|ft|featuring|official|topic|vevo)\b.*$", "", artist_part).strip()
    artist_part = re.sub(r"[^a-z0-9]+", " ", artist_part)
    return re.sub(r"\s+", " ", artist_part).strip()


def _song_artist_key(song: SongEntry | None) -> str:
    return _artist_key_from_title(song.title) if song else ""


def _entry_artist_key(entry: dict) -> str:
    return _artist_key_from_title(entry.get("title", ""))


def _song_identity(song: SongEntry | None) -> str:
    if not song:
        return ""
    return _youtube_video_id(song.webpage_url) or _youtube_video_id(song.url)


def _entry_identity(entry: dict) -> str:
    return _youtube_video_id(entry.get("webpage_url", "")) or _youtube_video_id(entry.get("url", ""))


def _remember_finished_song(state: GuildMusicState, song: SongEntry | None) -> None:
    if not song:
        return
    state.last_finished = song
    sid = _song_identity(song)
    if sid:
        state.recent_track_ids.append(sid)
    tkey = _song_core_key(song.title)
    if tkey:
        state.recent_title_keys.append(tkey)
    akey = _song_artist_key(song)
    if akey:
        state.recent_artist_keys.append(akey)


# ---------------------------------------------------------------------------
# Autoplay scoring
# ---------------------------------------------------------------------------

def _autoplay_noise_penalty(title: str) -> int:
    lowered = (title or "").lower()
    penalty = 0
    noisy_terms = {
        "slowed": -5, "reverb": -4, "sped up": -5, "nightcore": -6,
        "8d": -6, "bass boosted": -4, "full album": -8, "playlist": -8,
        "karaoke": -7, "reaction": -7, "compilation": -6,
    }
    for term, pts in noisy_terms.items():
        if term in lowered:
            penalty += pts
    return penalty


def _autoplay_candidate_score(entry: dict, *, current: SongEntry,
                               recent_title_keys: collections.deque,
                               recent_artist_keys: collections.deque,
                               queue_artist_keys: set,
                               current_artist_key: str,
                               autoplay_mode: str,
                               source_bias: int) -> int:
    score = source_bias
    title = entry.get("title", "")
    candidate_tokens = _autoplay_title_tokens(title)
    current_tokens = set(_song_signature_tokens(current.title))
    maki = autoplay_mode == "gzvibe"

    if current_tokens and candidate_tokens:
        matches = len(candidate_tokens.intersection(current_tokens))
        if matches == 1:
            score += 2 if maki else 1
        elif matches >= 2:
            score -= min(10 if maki else 8, matches * (4 if maki else 3))

    for recent_key in list(recent_title_keys)[-3:]:
        recent_tokens = {t for t in recent_key.split() if len(t) >= 3}
        if recent_tokens:
            overlap = len(candidate_tokens.intersection(recent_tokens))
            if overlap:
                score -= min(6 if maki else 4, overlap * (2 if maki else 1))

    candidate_artist = _entry_artist_key(entry)
    if candidate_artist:
        repeat_count = sum(1 for a in recent_artist_keys if a == candidate_artist)
        if current_artist_key and candidate_artist == current_artist_key:
            if maki:
                score += 20
            else:
                score += 6 if repeat_count == 0 else max(0, 6 - repeat_count * 3)
        elif candidate_artist in queue_artist_keys:
            score += 8 if maki else 5
        if maki:
            if candidate_artist not in queue_artist_keys and candidate_artist != current_artist_key:
                score -= 5
            if repeat_count >= 3:
                score -= min(8, repeat_count * 2)
            elif repeat_count == 1:
                score += 1
        else:
            if repeat_count == 0:
                score += 5
            elif repeat_count == 1:
                score += 2
            elif repeat_count >= 2:
                score -= min(10, repeat_count * 3)
            if candidate_artist not in queue_artist_keys and candidate_artist != current_artist_key and repeat_count == 0:
                score += 4
    else:
        score -= 4 if maki else 2

    candidate_dur = int(entry.get("duration") or 0)
    current_dur = int(current.duration or 0)
    if candidate_dur and current_dur:
        longer = max(candidate_dur, current_dur)
        shorter = max(1, min(candidate_dur, current_dur))
        ratio = longer / shorter
        if ratio <= 1.35:
            score += 4
        elif ratio <= 2.0:
            score += 2
        elif ratio >= 4.0:
            score -= 6 if maki else 4

    score += _autoplay_noise_penalty(title)
    if maki and source_bias < 12:
        score -= 3
    return score


def _format_autoplay_mode(mode: str) -> str:
    return "GzVibe" if mode == "gzvibe" else "Balanced"


def _autoplay_mode_button_style(mode: str) -> discord.ButtonStyle:
    return discord.ButtonStyle.success if mode == "gzvibe" else discord.ButtonStyle.secondary


def _autoplay_mode_embed(mode: str) -> discord.Embed:
    is_gzvibe = mode == "gzvibe"
    embed = discord.Embed(
        title="🎚️ Autoplay Mode",
        color=0xF1C40F if is_gzvibe else 0x5865F2,
        description=(
            "**GzVibe** keeps the queue in the same lane with tighter artist/style continuity."
            if is_gzvibe else
            "**Balanced** explores broader recommendations while still avoiding repeats."
        ),
    )
    embed.add_field(name="Current", value=f"**{_format_autoplay_mode(mode)}**", inline=True)
    embed.add_field(name="Switch", value="Use `/autoplaymode` or the mode button on the panel.", inline=True)
    embed.add_field(name="Visual Preset",
                    value="`High Cohesion` for GzVibe" if is_gzvibe else "`Discovery Mix` for Balanced",
                    inline=False)
    embed.set_footer(text="GzVibe Engine • Music UX")
    return embed


def _summarize_autoplay_debug(entry: dict) -> dict:
    dur = int(entry.get("duration") or 0)
    m, s = divmod(dur, 60)
    h, m = divmod(m, 60)
    return {
        "title": entry.get("title", "Unknown title"),
        "source": entry.get("source", "unknown"),
        "score": entry.get("score", 0),
        "artist": entry.get("artist", "Unknown"),
        "duration": f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}",
    }


# ---------------------------------------------------------------------------
# Cookie / search helpers
# ---------------------------------------------------------------------------

_resolved_cookiefile_cache: str | None = None
_resolved_cookiefile_checked: bool = False


def _resolve_yt_cookiefile() -> str | None:
    global _resolved_cookiefile_cache, _resolved_cookiefile_checked
    if _resolved_cookiefile_checked:
        return _resolved_cookiefile_cache
    _resolved_cookiefile_checked = True
    cookiefile = os.getenv("YTDLP_COOKIES_PATH", "").strip()
    if cookiefile and os.path.exists(cookiefile):
        _resolved_cookiefile_cache = cookiefile
        return _resolved_cookiefile_cache
    b64_blob = os.getenv("YTDLP_COOKIES_B64", "").strip()
    if not b64_blob:
        return None
    try:
        data = base64.b64decode(b64_blob)
        if not data:
            return None
        target = os.path.join(_music_data_dir(), "yt_cookies.txt")
        with open(target, "wb") as f:
            f.write(data)
        if os.path.exists(target):
            print(f"[yt-dlp] Loaded cookies from YTDLP_COOKIES_B64 into {target}")
            _resolved_cookiefile_cache = target
            return _resolved_cookiefile_cache
    except Exception as e:
        print(f"[yt-dlp] Failed to decode YTDLP_COOKIES_B64: {e}")
    return None


def _normalize_search_query(query: str) -> str:
    parts = re.split(r"\s+", (query or "").strip())
    out: list[str] = []
    seen: set[str] = set()
    for token in parts:
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return " ".join(out)


def _looks_like_url(text: str) -> bool:
    t = (text or "").strip().lower()
    return t.startswith("http://") or t.startswith("https://")


def _yt_suggestions(query: str) -> list[str]:
    url = ("https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&q="
           + urllib.parse.quote(query))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=3) as resp:
        data = json.loads(resp.read().decode())
    return [item for item in data[1] if isinstance(item, str)][:8]


def _piped_search(query: str, max_results: int) -> list[dict]:
    instances = ["https://piped.video", "https://piped.adminforge.de", "https://piped.projectsegfau.lt"]
    for base in instances:
        try:
            search_url = f"{base}/api/v1/search?q={urllib.parse.quote(query)}&filter=videos"
            req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode(errors="ignore"))
            results = []
            for item in data[:max_results * 4]:
                vid = (item.get("url") or item.get("id") or "").replace("/watch?v=", "").replace("https://www.youtube.com/watch?v=", "").strip("/ ")
                if not vid:
                    continue
                req2 = urllib.request.Request(f"{base}/api/v1/streams/{vid}", headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
                with urllib.request.urlopen(req2, timeout=6) as resp2:
                    details = json.loads(resp2.read().decode(errors="ignore"))
                audio_streams = details.get("audioStreams", [])
                if not audio_streams:
                    continue
                best = max(audio_streams, key=lambda s: int(s.get("bitrate") or 0))
                stream_url = best.get("url")
                if not stream_url:
                    continue
                results.append({"title": item.get("title", "Unknown title"), "url": stream_url,
                                 "webpage_url": f"https://www.youtube.com/watch?v={vid}",
                                 "duration": int(item.get("duration") or 0)})
                if len(results) >= max_results:
                    break
            if results:
                return results
        except Exception as e:
            print(f"[Piped] {base}: {e}")
    return []


def _invidious_search(query: str, max_results: int) -> list[dict]:
    instances = ["https://inv.nadeko.net", "https://invidious.privacyredirect.com",
                 "https://invidious.fdn.fr", "https://invidious.projectsegfau.lt", "https://yewtu.be"]
    for base in instances:
        try:
            req = urllib.request.Request(
                f"{base}/api/v1/search?q={urllib.parse.quote(query)}&type=video&sort_by=relevance",
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode(errors="ignore"))
            entries = []
            for item in data:
                if item.get("type") != "video":
                    continue
                vid = item.get("videoId")
                if not vid:
                    continue
                try:
                    req2 = urllib.request.Request(f"{base}/api/v1/videos/{vid}",
                                                  headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
                    with urllib.request.urlopen(req2, timeout=6) as resp2:
                        details = json.loads(resp2.read().decode(errors="ignore"))
                    audio_formats = [f for f in details.get("adaptiveFormats", [])
                                     if "audio" in str(f.get("type", "")).lower() and f.get("url")]
                    if not audio_formats:
                        audio_formats = [f for f in details.get("formatStreams", [])
                                         if "audio" in str(f.get("type", "")).lower() and f.get("url")]
                    if not audio_formats:
                        continue
                    best_audio = max(audio_formats, key=lambda f: int(f.get("bitrate", 0) or 0))
                    entries.append({"title": item.get("title", "Unknown title"), "url": best_audio["url"],
                                    "webpage_url": f"https://www.youtube.com/watch?v={vid}",
                                    "duration": int(item.get("lengthSeconds") or 0)})
                    if len(entries) >= max_results:
                        break
                except Exception:
                    continue
            if entries:
                return entries
        except Exception as e:
            print(f"[Invidious] {base}: {e}")
    return []


def _ytdlp_search(query: str, max_results: int) -> list[dict]:
    try:
        import yt_dlp
        query = _normalize_search_query(query)
        cookiefile = _resolve_yt_cookiefile()
        base_opts = {
            "quiet": True, "no_warnings": True, "default_search": "ytsearch",
            "extract_flat": "in_playlist", "ignoreerrors": True, "noplaylist": True,
            "http_headers": {"User-Agent": "Mozilla/5.0"}, "source_address": "0.0.0.0",
        }
        if cookiefile and os.path.exists(cookiefile):
            base_opts["cookiefile"] = cookiefile
        attempts = [
            {**base_opts, "extractor_args": {"youtube": {"player_client": ["android", "web"]}}},
            {**base_opts, "extractor_args": {"youtube": {"player_client": ["ios", "android"]}}},
            {**base_opts, "extractor_args": {"youtube": {"player_client": ["tv_embedded", "web"]}}},
        ]
        queries = [f"ytsearch{max_results}:{query}", f"ytsearch{max_results}:{query} audio"]
        for ydl_opts in attempts:
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    for q in queries:
                        try:
                            info = ydl.extract_info(q, download=False)
                            entries = []
                            for entry in info.get("entries", [])[:max_results]:
                                if not entry:
                                    continue
                                try:
                                    webpage_url = entry.get("webpage_url") or f"https://www.youtube.com/watch?v={entry.get('id', '')}"
                                    stream_url = entry.get("url")
                                    if not stream_url or "youtube.com/watch" in str(stream_url):
                                        stream_url = webpage_url
                                    if not stream_url:
                                        continue
                                    entries.append({"title": entry.get("title", "Unknown title"),
                                                    "url": stream_url, "webpage_url": webpage_url,
                                                    "duration": entry.get("duration", 0)})
                                except Exception:
                                    continue
                            if entries:
                                return entries
                        except Exception:
                            continue
            except Exception:
                continue
        piped = _piped_search(query, max_results)
        if piped:
            return piped
        return _invidious_search(query, max_results)
    except Exception as e:
        print(f"[yt-dlp] search failed: {e}")
        piped = _piped_search(query, max_results)
        if piped:
            return piped
        return _invidious_search(query, max_results)


def _ytdlp_resolve_url(url: str) -> list[dict]:
    try:
        import yt_dlp
        cookiefile = _resolve_yt_cookiefile()
        base_opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
                     "http_headers": {"User-Agent": "Mozilla/5.0"}, "source_address": "0.0.0.0"}
        if cookiefile and os.path.exists(cookiefile):
            base_opts["cookiefile"] = cookiefile
        attempts = [
            {**base_opts, "format": "bestaudio[ext=m4a]/bestaudio/best"},
            {**base_opts, "format": "bestaudio/best"},
            base_opts,
        ]
        for ydl_opts in attempts:
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                if info and info.get("entries"):
                    info = next((e for e in info.get("entries", []) if e), None)
                if not info:
                    continue
                stream_url = info.get("url")
                if not stream_url:
                    formats = info.get("formats") or []
                    audio_formats = [f for f in formats if f.get("url") and str(f.get("acodec", "none")) != "none"]
                    if audio_formats:
                        best_audio = max(audio_formats, key=lambda f: int(f.get("abr") or f.get("tbr") or 0))
                        stream_url = best_audio.get("url")
                if not stream_url:
                    stream_url = info.get("webpage_url") or url
                if not stream_url:
                    continue
                return [{"title": info.get("title", "Unknown title"), "url": stream_url,
                          "webpage_url": info.get("webpage_url") or url, "duration": info.get("duration", 0) or 0}]
            except Exception as e:
                print(f"[yt-dlp] url attempt failed: {e}")
    except Exception as e:
        print(f"[yt-dlp] url resolve failed: {e}")
    return []


async def search_youtube(query: str, max_results: int = 1) -> list[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _ytdlp_search(query, max_results))


async def search_youtube_resilient(query: str, max_results: int = 1) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    loop = asyncio.get_running_loop()
    if _looks_like_url(q):
        normalized = q.replace("music.youtube.com", "www.youtube.com")
        direct = await loop.run_in_executor(None, lambda: _ytdlp_resolve_url(normalized))
        if direct:
            return direct
    attempts: list[str] = [q]
    if not _looks_like_url(q):
        lowered = q.lower()
        attempts.extend([
            f"{q} official audio" if "official audio" not in lowered else q,
            f"{q} topic" if "topic" not in lowered else q,
            f"{q} lyrics" if "lyrics" not in lowered else q,
        ])
    seen: set[str] = set()
    for attempt in attempts:
        key = _normalize_search_query(attempt).lower().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        results = await search_youtube(_normalize_search_query(attempt), max_results=max_results)
        if results:
            return results
    return []


def _fetch_related_yt_dlp(webpage_url: str, exclude_url: str) -> list[dict]:
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "extract_flat": True, "ignoreerrors": True, "noplaylist": True}
        cookiefile = _resolve_yt_cookiefile()
        if cookiefile and os.path.isfile(cookiefile):
            opts["cookiefile"] = cookiefile
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
        related = info.get("related_videos") or []
        results = []
        for v in related:
            vid_id = v.get("id") or v.get("url", "")
            if not vid_id:
                continue
            wurl = f"https://www.youtube.com/watch?v={vid_id}"
            if wurl == exclude_url or exclude_url.endswith(vid_id):
                continue
            results.append({"title": v.get("title") or v.get("id", "Unknown"),
                             "url": wurl, "webpage_url": wurl, "duration": v.get("duration") or 0})
            if len(results) >= 12:
                break
        return results
    except Exception as e:
        print(f"[Autoplay] related fetch failed: {e}")
        return []


async def search_autocomplete(interaction: discord.Interaction, current: str) -> list[discord.app_commands.Choice[str]]:
    if not current or len(current) < 2:
        return []
    try:
        loop = asyncio.get_running_loop()
        suggestions = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _yt_suggestions(current)), timeout=2.5)
        return [discord.app_commands.Choice(name=s[:100], value=s[:100]) for s in suggestions][:25]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Autoplay engine
# ---------------------------------------------------------------------------

async def _rank_autoplay_candidates(state: GuildMusicState, current: SongEntry) -> list[tuple[int, dict, str]]:
    loop = asyncio.get_running_loop()
    exclude = current.webpage_url or current.url
    query_seed = _autoplay_query_seed(current)
    blocked_ids: set[str] = set(state.recent_track_ids)
    blocked_title_keys: set[str] = set(state.recent_title_keys)
    current_id = _song_identity(current)
    if current_id:
        blocked_ids.add(current_id)
    current_title_key = _song_core_key(current.title)
    if current_title_key:
        blocked_title_keys.add(current_title_key)
    current_artist_key = _song_artist_key(current)
    seed_tokens = _song_signature_tokens(current.title)
    recent_seed_titles = [current.title]
    if state.last_finished and state.last_finished.title:
        recent_seed_titles.append(state.last_finished.title)
    for q_item in list(state.queue)[:2]:
        if q_item and q_item.title:
            recent_seed_titles.append(q_item.title)
    queue_artist_keys: set[str] = set()
    for q_item in state.queue:
        qid = _song_identity(q_item)
        if qid:
            blocked_ids.add(qid)
        qkey = _song_core_key(q_item.title)
        if qkey:
            blocked_title_keys.add(qkey)
        qartist = _song_artist_key(q_item)
        if qartist:
            queue_artist_keys.add(qartist)

    def _candidate_allowed(entry: dict) -> bool:
        rid = _entry_identity(entry)
        if rid and rid in blocked_ids:
            return False
        rtitle = entry.get("title", "")
        rkey = _song_core_key(rtitle)
        if rkey and any(_same_song_key(rkey, bk) for bk in blocked_title_keys):
            return False
        if _titles_too_similar(current.title, rtitle):
            return False
        if any(_titles_too_similar(s, rtitle) for s in recent_seed_titles if s):
            return False
        if seed_tokens:
            rtokens = set(t for t in _song_core_key(rtitle).split() if t)
            if len(seed_tokens) >= 4 and sum(1 for t in seed_tokens[:4] if t in rtokens) >= 3:
                return False
        if rtitle.lower() == current.title.lower():
            return False
        return True

    seen_candidates: set[str] = set()
    scored: list[tuple[int, dict, str]] = []

    def _consider(entries: list[dict], *, source_bias: int, source_name: str) -> None:
        for entry in entries:
            if not _candidate_allowed(entry):
                continue
            key = _entry_identity(entry) or entry.get("webpage_url") or entry.get("title", "").lower()
            if not key or key in seen_candidates:
                continue
            seen_candidates.add(key)
            score = _autoplay_candidate_score(
                entry, current=current, recent_title_keys=state.recent_title_keys,
                recent_artist_keys=state.recent_artist_keys, queue_artist_keys=queue_artist_keys,
                current_artist_key=current_artist_key, autoplay_mode=state.autoplay_mode,
                source_bias=source_bias)
            scored.append((score, entry, source_name))

    if exclude:
        related = await loop.run_in_executor(None, lambda: _fetch_related_yt_dlp(exclude, exclude))
        _consider(related, source_bias=38 if state.autoplay_mode == "gzvibe" else 30, source_name="related")

    try:
        if state.autoplay_mode == "gzvibe":
            radio_queries = [
                f"{current_artist_key} radio" if current_artist_key else f"{query_seed} radio",
                f"{current_artist_key} best songs" if current_artist_key else f"{query_seed} radio",
            ]
            if current_artist_key and current_artist_key not in query_seed.lower():
                radio_queries.append(f"{current_artist_key} {query_seed}")
            broad_queries = [
                f"artists similar to {current_artist_key} best songs" if current_artist_key else f"songs like {query_seed}",
                f"mix similar to {query_seed}",
            ]
            artist_queries = ([f"artists like {current_artist_key}", f"{current_artist_key} greatest hits"]
                               if current_artist_key else [])
        else:
            radio_queries = [f"mix similar to {query_seed}", f"songs like {query_seed}"]
            if current_artist_key and current_artist_key not in query_seed.lower():
                radio_queries.append(f"songs like {current_artist_key} {query_seed}")
            broad_queries = [f"{query_seed} radio", f"music recommendations similar to {query_seed}"]
            if current_artist_key:
                broad_queries.append(f"artists similar to {current_artist_key} best songs")
            artist_queries = [f"{current_artist_key} best songs"] if current_artist_key else []

        async def _safe_search(q: str, max_r: int) -> list[dict]:
            try:
                return await asyncio.wait_for(search_youtube(_normalize_search_query(q), max_results=max_r), timeout=8.0)
            except Exception:
                return []

        radio_results, broad_results, artist_results = await asyncio.gather(
            asyncio.gather(*[_safe_search(q, 12) for q in radio_queries]),
            asyncio.gather(*[_safe_search(q, 12) for q in broad_queries]),
            asyncio.gather(*[_safe_search(q, 12) for q in artist_queries]) if artist_queries else asyncio.gather(),
        )

        for q, results in zip(radio_queries, radio_results):
            _consider(results, source_bias=28 if state.autoplay_mode == "gzvibe" else 22, source_name=q)
        for q, picks in zip(broad_queries, broad_results):
            _consider(picks, source_bias=16 if state.autoplay_mode == "gzvibe" else 14, source_name=q)
        for q, picks in zip(artist_queries, artist_results):
            _consider(picks, source_bias=13 if state.autoplay_mode == "gzvibe" else 7, source_name=q)
    except Exception as e:
        print(f"[Autoplay] Fallback search failed: {e}")

    return sorted(scored, key=lambda item: item[0], reverse=True) if scored else []


async def fetch_related_song(state: GuildMusicState, current: SongEntry) -> dict | None:
    ranked = await _rank_autoplay_candidates(state, current)
    state.last_autoplay_debug = [
        _summarize_autoplay_debug({**entry, "source": sn, "score": score, "artist": _entry_artist_key(entry) or "Unknown"})
        for score, entry, sn in ranked[:5]
    ]
    if ranked:
        recent_ids = set(state.recent_track_ids)
        current_id = _song_identity(current)
        if current_id:
            recent_ids.add(current_id)
        recent_title_keys = set(state.recent_title_keys)
        current_key = _song_core_key(current.title)
        if current_key:
            recent_title_keys.add(current_key)
        seed_titles = [current.title]
        if state.last_finished and state.last_finished.title:
            seed_titles.append(state.last_finished.title)
        for score, entry, sn in ranked:
            cid = _entry_identity(entry)
            if cid and cid in recent_ids:
                continue
            ct = entry.get("title", "")
            if not ct:
                continue
            ck = _song_core_key(ct)
            if ck and any(_same_song_key(ck, k) for k in recent_title_keys):
                continue
            if any(_titles_too_similar(s, ct) for s in seed_titles if s):
                continue
            curl = entry.get("webpage_url") or ""
            if curl and current.webpage_url and curl == current.webpage_url:
                continue
            print(f"[Autoplay] Picked {sn} ({score}, mode={state.autoplay_mode}): {ct}")
            return entry
    return None


# ---------------------------------------------------------------------------
# Stream resolution & playback
# ---------------------------------------------------------------------------

def _extract_stream_url(song: SongEntry) -> str | None:
    target = song.webpage_url or song.url
    if not target:
        return None
    try:
        import yt_dlp
        base_opts = dict(YTDL_STREAM_OPTS)
        cookiefile = _resolve_yt_cookiefile()
        if cookiefile:
            base_opts["cookiefile"] = cookiefile
        attempts = [base_opts, {**base_opts, "format": "bestaudio/best"},
                    {k: v for k, v in base_opts.items() if k != "format"}]
        for opts in attempts:
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(target, download=False)
                if not info:
                    continue
                url = info.get("url")
                if not url:
                    formats = info.get("formats") or []
                    audio_formats = [f for f in formats if f.get("url") and str(f.get("acodec", "none")) != "none"]
                    if audio_formats:
                        best = max(audio_formats, key=lambda f: int(f.get("abr") or f.get("tbr") or 0))
                        url = best.get("url")
                if isinstance(url, str) and url.strip() and url.startswith("http"):
                    return url
            except Exception as e:
                print(f"[Music] stream resolve attempt failed: {e}")
    except Exception as e:
        print(f"[Music] stream resolve failed: {e}")
    if song.url and isinstance(song.url, str) and song.url.startswith("http"):
        return song.url
    return None


def _ffmpeg_candidate_paths() -> list[str]:
    candidates: list[str] = []
    env_ffmpeg = os.getenv("FFMPEG_PATH", "").strip()
    if env_ffmpeg:
        candidates.append(env_ffmpeg)
    if FFMPEG_EXE:
        candidates.append(FFMPEG_EXE)
    for path in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/bin/ffmpeg"):
        candidates.append(path)
    wh = _shutil.which("ffmpeg")
    if wh:
        candidates.append(wh)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item and item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


async def play_next(guild_id: int, loop: asyncio.AbstractEventLoop):
    state = get_music_state(guild_id)
    if not (state.queue and state.voice_client and state.voice_client.is_connected()):
        _remember_finished_song(state, state.current)
        state.current = None
        return
    state.current = state.queue.popleft()
    song = state.current
    retry_key = _song_identity(song) or (song.webpage_url or song.url or song.title).strip().lower()
    try:
        try:
            stream_url = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _extract_stream_url(song)), timeout=20.0)
        except asyncio.TimeoutError:
            raise RuntimeError("Stream URL resolution timed out")
        if not stream_url:
            raise RuntimeError("No playable stream URL could be resolved")
        audio = None
        last_error: Exception | None = None
        for ffmpeg_exe in _ffmpeg_candidate_paths():
            if ffmpeg_exe != "ffmpeg" and not os.path.exists(ffmpeg_exe):
                continue
            try:
                audio = discord.FFmpegPCMAudio(stream_url, executable=ffmpeg_exe,
                                               before_options=FFMPEG_OPTS["before_options"],
                                               options=FFMPEG_OPTS["options"])
                break
            except Exception as e:
                last_error = e
        if audio is None:
            raise RuntimeError(f"No working ffmpeg found. Last error: {last_error}")
        volume_factor = max(0.1, min(2.0, state.volume))
        source = discord.PCMVolumeTransformer(audio, volume=volume_factor)
        state.source_transformer = source

        def after_play(error):
            try:
                state.source_transformer = None
                if error:
                    print(f"[Music] Player error: {error}")
                    attempts = state.retry_attempts.get(retry_key, 0)
                    if attempts < 1:
                        state.retry_attempts[retry_key] = attempts + 1
                        state.queue.appendleft(song)
                        state.current = None
                        asyncio.run_coroutine_threadsafe(play_next_async(guild_id, loop), loop)
                        return
                else:
                    state.retry_attempts.pop(retry_key, None)
                finished = state.current
                _remember_finished_song(state, finished)
                state.current = None
                asyncio.run_coroutine_threadsafe(play_next_async(guild_id, loop), loop)
            except Exception as e:
                print(f"[Music] after_play error: {e}")

        print(f"[Music] Now playing: {song.title}")
        state.voice_client.play(source, after=after_play)
    except Exception as e:
        print(f"[Music] Failed to play {song.title}: {e}")
        _remember_finished_song(state, state.current)
        state.current = None
        asyncio.run_coroutine_threadsafe(play_next_async(guild_id, loop), loop)


async def play_next_async(guild_id: int, loop: asyncio.AbstractEventLoop):
    state = get_music_state(guild_id)
    seed_song = state.current or state.last_finished
    if not state.queue and state.autoplay and seed_song:
        try:
            r = await fetch_related_song(state, seed_song)
            if r:
                state.queue.append(SongEntry(
                    title=r["title"], url=r["url"], webpage_url=r["webpage_url"],
                    duration=r.get("duration") or 0, requester=seed_song.requester))
        except Exception as e:
            print(f"[Autoplay] Failed: {e}")
    _loop = asyncio.get_running_loop()
    await play_next(guild_id, _loop)
    try:
        await _post_music_panel(guild_id)
    except Exception as e:
        print(f"[Music] Panel update failed: {e}")


# ---------------------------------------------------------------------------
# GzVibe Control Deck
# ---------------------------------------------------------------------------

class MusicControlView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(emoji="⏯️", label="Pause / Resume", style=discord.ButtonStyle.primary, row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("Not connected to voice.", ephemeral=True)
            return
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Paused.", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", label="Skip Track", style=discord.ButtonStyle.secondary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message("Nothing to skip.", ephemeral=True)
            return
        vc.stop()
        await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", label="Stop Session", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("Not connected to voice.", ephemeral=True)
            return
        state.queue.clear()
        state.current = None
        vc.stop()
        if state.now_playing_msg:
            try:
                await state.now_playing_msg.delete()
            except Exception:
                pass
            state.now_playing_msg = None
        await interaction.response.send_message("⏹️ Stopped and queue cleared.", ephemeral=True)

    @discord.ui.button(emoji="📋", label="Queue Snapshot", style=discord.ButtonStyle.secondary, row=1)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        if not state.current and not state.queue:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return
        desc = ""
        if state.current:
            desc += f"**Now Playing:** [{state.current.title}]({state.current.webpage_url}) `{state.current.format_duration()}`\n\n"
        for i, entry in enumerate(state.queue, 1):
            desc += f"`{i}.` [{entry.title}]({entry.webpage_url}) `{entry.format_duration()}`\n"
            if i >= 10:
                remaining = len(state.queue) - 10
                if remaining:
                    desc += f"*...and {remaining} more*"
                break
        embed = discord.Embed(title="📋 Queue Snapshot", description=desc, color=0x9B59B6)
        embed.set_footer(text="GzVibe Panel • Live Queue")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(emoji="🔉", label="Vol -10%", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        state.volume = max(0.0, round(state.volume - 0.1, 2))
        if state.source_transformer:
            state.source_transformer.volume = state.volume
        await interaction.response.send_message(f"🔉 Volume: {int(state.volume * 100)}%", ephemeral=True)

    @discord.ui.button(emoji="🔊", label="Vol +10%", style=discord.ButtonStyle.secondary, row=1)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        state.volume = min(1.0, round(state.volume + 0.1, 2))
        if state.source_transformer:
            state.source_transformer.volume = state.volume
        await interaction.response.send_message(f"🔊 Volume: {int(state.volume * 100)}%", ephemeral=True)

    @discord.ui.button(emoji="🔁", label="Autoplay: OFF", style=discord.ButtonStyle.secondary, row=2, custom_id="autoplay_toggle")
    async def autoplay_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        state.autoplay = not state.autoplay
        button.label = f"Autoplay: {'ON' if state.autoplay else 'OFF'}"
        button.style = discord.ButtonStyle.success if state.autoplay else discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)
        note = (f"I will queue related songs automatically in **{_format_autoplay_mode(state.autoplay_mode)}** mode!"
                if state.autoplay else "")
        await interaction.followup.send(f"🔁 Autoplay is now **{'ON' if state.autoplay else 'OFF'}**. {note}", ephemeral=True)

    @discord.ui.button(emoji="🎚️", label="Mode: GzVibe", style=discord.ButtonStyle.success, row=2, custom_id="autoplay_mode_toggle")
    async def autoplay_mode_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        state.autoplay_mode = "balanced" if state.autoplay_mode == "gzvibe" else "gzvibe"
        button.label = f"Mode: {_format_autoplay_mode(state.autoplay_mode)}"
        button.style = _autoplay_mode_button_style(state.autoplay_mode)
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(embed=_autoplay_mode_embed(state.autoplay_mode), ephemeral=True)

    @discord.ui.button(emoji="✨", label="Save To Playlist", style=discord.ButtonStyle.primary, row=2, custom_id="playlist_quick_add")
    async def playlist_quick_add(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_music_state(self.guild_id)
        song = state.current or (state.queue[0] if state.queue else None)
        if not song:
            await interaction.response.send_message("No song available to save.", ephemeral=True)
            return
        playlist_name = "GzVibe Favorites"
        user_playlists = _playlist_bucket(interaction.guild.id, interaction.user.id)
        tracks = user_playlists.setdefault(playlist_name, [])
        track = _playlist_track(song)
        if _playlist_has_track(tracks, track):
            embed = _gzvibe_playlist_embed("🎵 Already In Playlist",
                                           f"[{song.title}]({song.webpage_url}) is already in **{playlist_name}**.", color=0xF39C12)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        tracks.append(track)
        _save_music_playlists()
        embed = _gzvibe_playlist_embed("✅ Added To Playlist",
                                        f"Saved [{song.title}]({song.webpage_url}) to **{playlist_name}**.\n"
                                        f"You now have `{len(tracks)}` songs in that playlist.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def _post_music_panel(guild_id: int, force_new: bool = False):
    state = get_music_state(guild_id)
    guild = bot.get_guild(guild_id)
    if not guild or not state.current:
        return
    # Find the music channel: look for a channel named "music-commands", "music", or any text channel
    music_ch = None
    for name in ("🎵┃music-commands", "music-commands", "music"):
        music_ch = discord.utils.get(guild.text_channels, name=name)
        if music_ch:
            break
    if not music_ch:
        # Fall back to first text channel
        if guild.text_channels:
            music_ch = guild.text_channels[0]
    if not music_ch:
        return

    embed = discord.Embed(
        title="✦ GzVibe Control Deck ✦",
        description=(
            f"### [{state.current.title}]({state.current.webpage_url})\n"
            "`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
            f"**Vibe Mode:** `{_format_autoplay_mode(state.autoplay_mode)}`  •  "
            f"**Autoplay:** `{'ON' if state.autoplay else 'OFF'}`"
        ),
        color=0x00D1B2,
    )
    thumb = _youtube_thumbnail(state.current.webpage_url or state.current.url)
    if thumb:
        embed.set_thumbnail(url=thumb)
    embed.add_field(name="⏱️ Duration", value=f"`{state.current.format_duration()}`", inline=True)
    embed.add_field(name="🎧 Requested By", value=state.current.requester.mention, inline=True)
    embed.add_field(name="🔊 Volume", value=f"`{int(state.volume * 100)}%`", inline=True)
    embed.add_field(name="📦 Queue Depth", value=f"`{len(state.queue)}` tracks waiting", inline=False)
    embed.add_field(name="🧩 Build", value=f"`{STARTUP_MARKER}`", inline=False)
    q = len(state.queue)
    footer_parts = []
    if q:
        footer_parts.append(f"{q} song{'s' if q != 1 else ''} in queue")
    if state.autoplay:
        footer_parts.append(f"🔁 Autoplay ON • {_format_autoplay_mode(state.autoplay_mode)}")
    embed.set_footer(text="  ✦  ".join(footer_parts) if footer_parts else "GzVibe Deck • Controls Live")

    view = MusicControlView(guild_id)
    for child in view.children:
        if getattr(child, "custom_id", None) == "autoplay_toggle":
            child.label = f"Autoplay: {'ON' if state.autoplay else 'OFF'}"
            child.style = discord.ButtonStyle.success if state.autoplay else discord.ButtonStyle.secondary
        if getattr(child, "custom_id", None) == "autoplay_mode_toggle":
            child.label = f"Mode: {_format_autoplay_mode(state.autoplay_mode)}"
            child.style = _autoplay_mode_button_style(state.autoplay_mode)

    if force_new and state.now_playing_msg:
        try:
            await state.now_playing_msg.delete()
        except Exception:
            pass
        state.now_playing_msg = None

    if state.now_playing_msg:
        try:
            await state.now_playing_msg.edit(embed=embed, view=view)
            return
        except Exception:
            state.now_playing_msg = None

    # Try to recover the last panel after restart
    if state.now_playing_msg is None:
        try:
            async for msg in music_ch.history(limit=20):
                if msg.author.id != bot.user.id or not msg.embeds:
                    continue
                if (msg.embeds[0].title or "").strip() == "✦ GzVibe Control Deck ✦":
                    state.now_playing_msg = msg
                    break
        except Exception:
            pass

    if state.now_playing_msg:
        try:
            await state.now_playing_msg.edit(embed=embed, view=view)
            return
        except Exception:
            state.now_playing_msg = None

    state.now_playing_msg = await music_ch.send(embed=embed, view=view)


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"[GzVibe] {bot.user} is online! Build: {STARTUP_MARKER}", flush=True)
    if GUILD_ID:
        bot.tree.copy_global_to(guild=GUILD_ID)
        synced = await bot.tree.sync(guild=GUILD_ID)
        print(f"[GzVibe] Synced {len(synced)} commands to guild {GUILD_ID.id}", flush=True)
    else:
        synced = await bot.tree.sync()
        print(f"[GzVibe] Synced {len(synced)} commands globally", flush=True)
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening, name="music • /play"))


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="play", description="Search and play a song in your voice channel")
@app_commands.autocomplete(query=search_autocomplete)
@app_commands.describe(query="YouTube URL or search terms")
async def play(interaction: discord.Interaction, query: str):
    member = interaction.guild.get_member(interaction.user.id)
    if not member or not member.voice or not member.voice.channel:
        await interaction.response.send_message("You need to be in a voice channel first.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        state = get_music_state(interaction.guild.id)
        vc = interaction.guild.voice_client
        if vc is None:
            vc = await member.voice.channel.connect(self_deaf=True)
            state.voice_client = vc
        elif vc.channel != member.voice.channel:
            await vc.move_to(member.voice.channel)
            state.voice_client = vc
        else:
            state.voice_client = vc

        try:
            results = await asyncio.wait_for(search_youtube_resilient(query, max_results=1), timeout=45.0)
        except asyncio.TimeoutError:
            await interaction.followup.send("Search timed out — try a direct YouTube URL.")
            return

        if not results:
            await interaction.followup.send("No results found. Try a more specific query or paste a YouTube URL directly.")
            return

        r = results[0]
        entry = SongEntry(title=r.get("title", "Unknown"), url=r["url"],
                          webpage_url=r.get("webpage_url", ""), duration=r.get("duration", 0),
                          requester=interaction.user)
        state.queue.append(entry)

        if not vc.is_playing() and not vc.is_paused():
            _loop = asyncio.get_running_loop()
            await play_next(interaction.guild.id, _loop)
            await interaction.followup.send(f"▶️ Starting **{entry.title}** — see the player below!", ephemeral=True)
            await _post_music_panel(interaction.guild.id, force_new=True)
        else:
            embed = discord.Embed(title="Added to Queue",
                                   description=f"[{entry.title}]({entry.webpage_url})", color=0x3498DB)
            embed.add_field(name="Position", value=str(len(state.queue)))
            embed.add_field(name="Duration", value=entry.format_duration())
            await interaction.followup.send(embed=embed)
            await _post_music_panel(interaction.guild.id)
    except Exception as e:
        print(f"[Play] Error: {traceback.format_exc()}")
        try:
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}")
        except Exception:
            pass


@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    vc.stop()
    await interaction.response.send_message("⏭️ Skipped.")


@bot.tree.command(name="pause", description="Pause the current song")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    vc.pause()
    await interaction.response.send_message("⏸️ Paused.")


@bot.tree.command(name="resume", description="Resume the paused song")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_paused():
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)
        return
    vc.resume()
    await interaction.response.send_message("▶️ Resumed.")


@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def stop(interaction: discord.Interaction):
    state = get_music_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    if not vc:
        await interaction.response.send_message("Not connected to a voice channel.", ephemeral=True)
        return
    state.queue.clear()
    state.current = None
    vc.stop()
    if state.now_playing_msg:
        try:
            await state.now_playing_msg.delete()
        except Exception:
            pass
        state.now_playing_msg = None
    await interaction.response.send_message("⏹️ Stopped and queue cleared.")


@bot.tree.command(name="leave", description="Disconnect the bot from voice")
async def leave(interaction: discord.Interaction):
    state = get_music_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    if not vc:
        await interaction.response.send_message("Not connected to a voice channel.", ephemeral=True)
        return
    state.queue.clear()
    state.current = None
    await vc.disconnect()
    if state.now_playing_msg:
        try:
            await state.now_playing_msg.delete()
        except Exception:
            pass
        state.now_playing_msg = None
    await interaction.response.send_message("👋 Disconnected.")


@bot.tree.command(name="queue", description="Show the current music queue")
async def queue_cmd(interaction: discord.Interaction):
    state = get_music_state(interaction.guild.id)
    if not state.current and not state.queue:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return
    desc = ""
    if state.current:
        desc += f"**Now Playing:** [{state.current.title}]({state.current.webpage_url}) `{state.current.format_duration()}` — {state.current.requester.mention}\n\n"
    for i, entry in enumerate(state.queue, 1):
        desc += f"`{i}.` [{entry.title}]({entry.webpage_url}) `{entry.format_duration()}` — {entry.requester.mention}\n"
        if i >= 10:
            remaining = len(state.queue) - 10
            if remaining > 0:
                desc += f"*...and {remaining} more*"
            break
    embed = discord.Embed(title="📋 GzVibe Queue", description=desc, color=0x9B59B6)
    if state.current:
        thumb = _youtube_thumbnail(state.current.webpage_url or state.current.url)
        if thumb:
            embed.set_thumbnail(url=thumb)
    embed.set_footer(text="Queue Snapshot • GzVibe")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nowplaying", description="Show what's currently playing")
async def nowplaying(interaction: discord.Interaction):
    state = get_music_state(interaction.guild.id)
    if not state.current:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    embed = discord.Embed(
        title="✦ GzVibe Live Track ✦",
        description=(f"## [{state.current.title}]({state.current.webpage_url})\n"
                     "`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`"),
        color=0x00D1B2,
    )
    thumb = _youtube_thumbnail(state.current.webpage_url or state.current.url)
    if thumb:
        embed.set_thumbnail(url=thumb)
    embed.add_field(name="⏱️ Duration", value=f"`{state.current.format_duration()}`", inline=True)
    embed.add_field(name="🎧 Requested By", value=state.current.requester.mention, inline=True)
    embed.add_field(name="🔊 Volume", value=f"`{int(state.volume * 100)}%`", inline=True)
    embed.add_field(name="🔁 Autoplay",
                    value=f"`{'ON' if state.autoplay else 'OFF'}` in `{_format_autoplay_mode(state.autoplay_mode)}`",
                    inline=False)
    embed.set_footer(text="GzVibe Live View")
    await interaction.response.send_message(embed=embed)
    await _post_music_panel(interaction.guild.id, force_new=True)


@bot.tree.command(name="volume", description="Set the playback volume (0-100)")
@app_commands.describe(level="Volume level (0-100)")
async def volume(interaction: discord.Interaction, level: int):
    if not 0 <= level <= 100:
        await interaction.response.send_message("Volume must be between 0 and 100.", ephemeral=True)
        return
    state = get_music_state(interaction.guild.id)
    state.volume = level / 100
    if state.source_transformer:
        state.source_transformer.volume = state.volume
    await interaction.response.send_message(f"🔊 Volume set to {level}%.")


@bot.tree.command(name="shuffle", description="Shuffle the queue")
async def shuffle(interaction: discord.Interaction):
    import random
    state = get_music_state(interaction.guild.id)
    if len(state.queue) < 2:
        await interaction.response.send_message("Not enough songs in queue to shuffle.", ephemeral=True)
        return
    q_list = list(state.queue)
    random.shuffle(q_list)
    state.queue = collections.deque(q_list)
    await interaction.response.send_message(f"🔀 Shuffled {len(q_list)} songs.")


@bot.tree.command(name="remove", description="Remove a song from the queue by position")
@app_commands.describe(position="Position in queue (1 = next song)")
async def remove(interaction: discord.Interaction, position: int):
    state = get_music_state(interaction.guild.id)
    if position < 1 or position > len(state.queue):
        await interaction.response.send_message(f"Invalid position. Queue has {len(state.queue)} song(s).", ephemeral=True)
        return
    q_list = list(state.queue)
    removed = q_list.pop(position - 1)
    state.queue = collections.deque(q_list)
    await interaction.response.send_message(f"🗑️ Removed **{removed.title}** from the queue.")


@bot.tree.command(name="autoplaymode", description="Set how strict autoplay should be")
@app_commands.choices(mode=[
    app_commands.Choice(name="GzVibe", value="gzvibe"),
    app_commands.Choice(name="Balanced", value="balanced"),
])
@app_commands.describe(mode="GzVibe = tight artist continuity; Balanced = broader discovery")
async def autoplaymode(interaction: discord.Interaction, mode: str):
    state = get_music_state(interaction.guild.id)
    state.autoplay_mode = mode if mode in {"gzvibe", "balanced"} else "gzvibe"
    await interaction.response.send_message(embed=_autoplay_mode_embed(state.autoplay_mode), ephemeral=True)
    await _post_music_panel(interaction.guild.id)


@bot.tree.command(name="playlist", description="View your GzVibe Favorites playlist")
async def playlist(interaction: discord.Interaction):
    user_playlists = _playlist_bucket(interaction.guild.id, interaction.user.id)
    tracks = user_playlists.get("GzVibe Favorites", [])
    if not tracks:
        await interaction.response.send_message("Your GzVibe Favorites playlist is empty. Use Save To Playlist on the deck to add songs.", ephemeral=True)
        return
    lines = []
    for i, t in enumerate(tracks[:15], 1):
        url = t.get("webpage_url", "")
        title = t.get("title", "Unknown")
        dur = int(t.get("duration") or 0)
        m, s = divmod(dur, 60)
        lines.append(f"`{i}.` [{title}]({url}) `{m}:{s:02d}`")
    if len(tracks) > 15:
        lines.append(f"*...and {len(tracks) - 15} more*")
    embed = _gzvibe_playlist_embed("🎵 GzVibe Favorites", "\n".join(lines))
    embed.add_field(name="Total", value=f"{len(tracks)} songs", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.run(TOKEN)
