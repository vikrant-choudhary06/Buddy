"""
Music Cog for Buddy
Music player with YouTube support
"""

import asyncio
import logging
import shutil
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from database.db_manager import DatabaseManager
from utils.embeds import EmbedColor, EmbedFactory
from utils.permissions import is_admin

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None

try:
    import imageio_ffmpeg
except ImportError:  # pragma: no cover
    imageio_ffmpeg = None

logger = logging.getLogger(__name__)

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch1",
    "skip_download": True,
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


class MusicQueue:
    """Music queue manager."""

    def __init__(self):
        self.queue: List[Dict[str, Any]] = []
        self.current: Optional[Dict[str, Any]] = None
        self.loop = False

    def add(self, track: Dict[str, Any]):
        self.queue.append(track)

    def next(self) -> Optional[Dict[str, Any]]:
        if self.loop and self.current:
            return self.current
        if self.queue:
            self.current = self.queue.pop(0)
            return self.current
        self.current = None
        return None

    def clear(self):
        self.queue = []
        self.current = None


class Music(commands.Cog):
    """Music player cog."""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get("modules", {}).get("music", {})
        self.queues: Dict[int, MusicQueue] = {}
        self.play_locks: Dict[int, asyncio.Lock] = {}
        self.guild_volumes: Dict[int, float] = {}

    def get_queue(self, guild_id: int) -> MusicQueue:
        if guild_id not in self.queues:
            self.queues[guild_id] = MusicQueue()
        return self.queues[guild_id]

    def get_play_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self.play_locks:
            self.play_locks[guild_id] = asyncio.Lock()
        return self.play_locks[guild_id]

    @staticmethod
    def _resolve_ffmpeg_executable() -> Optional[str]:
        """Resolve FFmpeg executable path from system PATH or bundled package."""
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path:
            return ffmpeg_path

        if imageio_ffmpeg is not None:
            try:
                return imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                return None

        return None

    async def _extract_track(self, query: str) -> Dict[str, Any]:
        """Resolve a query/URL to a playable audio stream."""
        if yt_dlp is None:
            raise RuntimeError("yt-dlp is not installed.")

        return await asyncio.to_thread(self._extract_track_sync, query)

    @staticmethod
    def _extract_track_sync(query: str) -> Dict[str, Any]:
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            info = ydl.extract_info(query, download=False)

            if not info:
                raise ValueError("No results found")

            if "entries" in info:
                info = next((entry for entry in info["entries"] if entry), None)
                if not info:
                    raise ValueError("No results found")

            stream_url = info.get("url")
            if not stream_url:
                raise ValueError("No playable stream URL found")

            return {
                "title": info.get("title") or query,
                "webpage_url": info.get("webpage_url") or query,
                "stream_url": stream_url,
                "duration": info.get("duration"),
            }

    async def _refresh_stream_url(self, track: Dict[str, Any]) -> None:
        """Refresh stream URL for expired links."""
        lookup = track.get("webpage_url") or track.get("query")
        if not lookup:
            return

        refreshed = await self._extract_track(lookup)
        track["title"] = refreshed.get("title", track.get("title"))
        track["webpage_url"] = refreshed.get("webpage_url", track.get("webpage_url"))
        track["stream_url"] = refreshed.get("stream_url", track.get("stream_url"))

    async def _build_audio_source(self, track: Dict[str, Any], guild_id: int) -> Optional[discord.AudioSource]:
        ffmpeg_path = self._resolve_ffmpeg_executable()
        if not ffmpeg_path:
            return None

        for attempt in range(2):
            stream_url = track.get("stream_url")
            if not stream_url:
                await self._refresh_stream_url(track)
                stream_url = track.get("stream_url")

            if not stream_url:
                continue

            try:
                audio = discord.FFmpegPCMAudio(
                    stream_url,
                    executable=ffmpeg_path,
                    **FFMPEG_OPTIONS,
                )
                volume = self.guild_volumes.get(guild_id, 0.5)
                return discord.PCMVolumeTransformer(audio, volume=volume)
            except Exception as e:
                if attempt == 0:
                    await self._refresh_stream_url(track)
                    continue
                logger.error(f"Failed to create audio source: {e}", exc_info=True)
                return None

        return None

    async def _announce_now_playing(self, guild: discord.Guild, track: Dict[str, Any]):
        channel_id = track.get("text_channel_id")
        if not channel_id:
            return

        channel = guild.get_channel(channel_id)
        if not channel:
            return

        title = track.get("title", "Unknown Track")
        url = track.get("webpage_url")
        requester_id = track.get("requested_by")

        description = f"[{title}]({url})" if url else title
        if requester_id:
            description += f"\nRequested by: <@{requester_id}>"

        embed = EmbedFactory.create(
            title="Now Playing",
            description=description,
            color=EmbedColor.INFO,
        )
        await channel.send(embed=embed)

    async def _play_next(self, guild_id: int) -> bool:
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return False

        vc = guild.voice_client
        if not vc:
            self.get_queue(guild_id).current = None
            return False

        queue = self.get_queue(guild_id)

        while True:
            track = queue.next()
            if not track:
                return False

            source = await self._build_audio_source(track, guild_id)
            if source:
                break

            logger.warning(f"Skipping unplayable track: {track.get('title', 'unknown')}")

        def after_playback(error):
            if error:
                logger.error(f"Playback error in guild {guild_id}: {error}")
            self.bot.loop.call_soon_threadsafe(asyncio.create_task, self._ensure_playing(guild_id))

        try:
            vc.play(source, after=after_playback)
        except Exception as e:
            logger.error(f"Error starting playback: {e}", exc_info=True)
            return False

        try:
            await self._announce_now_playing(guild, track)
        except Exception as e:
            logger.warning(f"Failed to send now playing message: {e}")

        return True

    async def _ensure_playing(self, guild_id: int) -> bool:
        """Start playback if idle and queue has tracks."""
        lock = self.get_play_lock(guild_id)
        async with lock:
            guild = self.bot.get_guild(guild_id)
            if not guild or not guild.voice_client:
                return False

            vc = guild.voice_client
            if vc.is_playing() or vc.is_paused():
                return False

            return await self._play_next(guild_id)

    @app_commands.command(name="play", description="Play music from YouTube")
    @app_commands.describe(query="Song name or YouTube URL")
    async def play(self, interaction: discord.Interaction, query: str):
        """Play music from YouTube."""
        if yt_dlp is None:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Dependency Missing", "yt-dlp is not installed. Run: pip install yt-dlp"),
                ephemeral=True,
            )
            return

        if not self._resolve_ffmpeg_executable():
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Dependency Missing",
                    "FFmpeg executable not found. Install with: pip install imageio-ffmpeg",
                ),
                ephemeral=True,
            )
            return

        if not interaction.user.voice:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not in Voice", "You must be in a voice channel to use this command"),
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        user_channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client

        try:
            if vc and vc.channel != user_channel:
                await vc.move_to(user_channel)
            elif not vc:
                vc = await user_channel.connect()
        except Exception as e:
            await interaction.followup.send(
                embed=EmbedFactory.error("Connection Failed", f"Could not join voice channel: {e}"),
                ephemeral=True,
            )
            return

        try:
            track = await self._extract_track(query)
        except Exception as e:
            logger.error(f"Failed to resolve track '{query}': {e}", exc_info=True)
            await interaction.followup.send(
                embed=EmbedFactory.error("Track Error", "Could not fetch this track. Try a different query or URL."),
                ephemeral=True,
            )
            return

        track["query"] = query
        track["requested_by"] = interaction.user.id
        track["text_channel_id"] = interaction.channel.id

        guild_id = interaction.guild.id
        queue = self.get_queue(guild_id)
        queue.add(track)
        position = len(queue.queue) if queue.current else 1

        started = await self._ensure_playing(guild_id)
        started_now = started and queue.current is track

        title = track.get("title", query)
        url = track.get("webpage_url")
        track_text = f"[{title}]({url})" if url else title

        status_line = "Playback started." if started_now else f"Position in queue: {position}"
        embed = EmbedFactory.success(
            "Added to Queue",
            f"Track: {track_text}\nRequested by: {interaction.user.mention}\n{status_line}",
        )
        await interaction.followup.send(embed=embed)
        logger.info(f"Added to queue by {interaction.user}: {title}")

    @app_commands.command(name="join", description="Join your voice channel")
    async def join(self, interaction: discord.Interaction):
        """Join voice channel."""
        if not interaction.user.voice:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not in Voice", "You must be in a voice channel"),
                ephemeral=True,
            )
            return

        channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client

        if vc:
            await vc.move_to(channel)
        else:
            await channel.connect()

        embed = EmbedFactory.success("Joined", f"Joined {channel.mention}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leave", description="Leave voice channel")
    async def leave(self, interaction: discord.Interaction):
        """Leave voice channel."""
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Connected", "I'm not in a voice channel"),
                ephemeral=True,
            )
            return

        guild_id = interaction.guild.id
        queue = self.get_queue(guild_id)
        queue.clear()

        if vc.is_playing() or vc.is_paused():
            vc.stop()

        await vc.disconnect()
        await interaction.response.send_message(embed=EmbedFactory.success("Disconnected", "Left voice channel"))

    @app_commands.command(name="queue", description="View music queue")
    async def view_queue(self, interaction: discord.Interaction):
        """View music queue."""
        guild_id = interaction.guild.id
        queue = self.get_queue(guild_id)

        if not queue.current and not queue.queue:
            await interaction.response.send_message(
                embed=EmbedFactory.info("Empty Queue", "The music queue is empty"),
                ephemeral=True,
            )
            return

        lines: List[str] = []
        if queue.current:
            lines.append(f"Now Playing: {queue.current.get('title', 'Unknown')}")

        if queue.queue:
            lines.append("")
            lines.append("Up Next:")
            for i, track in enumerate(queue.queue[:10], 1):
                lines.append(f"{i}. {track.get('title', 'Unknown')}")

        embed = EmbedFactory.create(
            title="Music Queue",
            description="\n".join(lines),
            color=EmbedColor.INFO,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="skip", description="Skip current track")
    async def skip(self, interaction: discord.Interaction):
        """Skip current track."""
        vc = interaction.guild.voice_client
        if not vc or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True,
            )
            return

        vc.stop()
        await interaction.response.send_message(embed=EmbedFactory.success("Skipped", "Skipped current track"))

    @app_commands.command(name="pause", description="Pause music")
    async def pause(self, interaction: discord.Interaction):
        """Pause music."""
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True,
            )
            return

        vc.pause()
        await interaction.response.send_message(embed=EmbedFactory.success("Paused", "Music paused"))

    @app_commands.command(name="resume", description="Resume music")
    async def resume(self, interaction: discord.Interaction):
        """Resume music."""
        vc = interaction.guild.voice_client
        if not vc or not vc.is_paused():
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Paused", "Music is not paused"),
                ephemeral=True,
            )
            return

        vc.resume()
        await interaction.response.send_message(embed=EmbedFactory.success("Resumed", "Music resumed"))

    @app_commands.command(name="volume", description="Set volume (Admin)")
    @app_commands.describe(volume="Volume level (0-100)")
    @is_admin()
    async def volume(self, interaction: discord.Interaction, volume: int):
        """Set volume."""
        if volume < 0 or volume > 100:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Volume", "Volume must be between 0 and 100"),
                ephemeral=True,
            )
            return

        guild_id = interaction.guild.id
        self.guild_volumes[guild_id] = volume / 100

        vc = interaction.guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = self.guild_volumes[guild_id]

        await interaction.response.send_message(embed=EmbedFactory.success("Volume", f"Volume set to {volume}%"))

    @app_commands.command(name="nowplaying", description="Show currently playing track")
    async def nowplaying(self, interaction: discord.Interaction):
        """Show currently playing track."""
        queue = self.get_queue(interaction.guild.id)
        if not queue.current:
            await interaction.response.send_message(
                embed=EmbedFactory.info("Nothing Playing", "No music is currently playing"),
                ephemeral=True,
            )
            return

        track = queue.current
        title = track.get("title", "Unknown Track")
        url = track.get("webpage_url")
        description = f"[{title}]({url})" if url else title

        embed = EmbedFactory.create(
            title="Now Playing",
            description=description,
            color=EmbedColor.INFO,
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    """Setup function for cog loading."""
    await bot.add_cog(Music(bot, bot.db, bot.config))
