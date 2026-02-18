"""
Music Cog for Buddy
Music player with YouTube support.

YouTube cookies setup (required for authenticated access):
1. Export cookies on a machine where you are logged into YouTube:
   yt-dlp --cookies-from-browser chrome --cookies cookies.txt
2. Upload/place `cookies.txt` in the bot project root directory (same level as `main.py`) on the VPS.
3. Refresh cookies regularly (recommended every 7-14 days, or immediately if YouTube starts failing auth checks).
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path
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

PRIMARY_AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"
FALLBACK_AUDIO_FORMAT = "bestaudio/best"

YTDL_OPTIONS = {
    "format": PRIMARY_AUDIO_FORMAT,
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "skip_download": True,
    "cookiefile": "cookies.txt",
    "extractor_args": {"youtube": {"player_client": ["android"]}},
    "sleep_interval_requests": 1,
    "retries": 2,
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

COOKIES_FILE_NAME = "cookies.txt"
EXTRACTION_RETRIES = 2
EXTRACTION_RETRY_DELAY_SECONDS = 1.0
YTDLP_DEBUG_ENV = "YTDLP_DEBUG"


class TrackExtractionError(Exception):
    """Raised when a track cannot be extracted from YouTube."""

    def __init__(self, user_message: str, detail: Optional[str] = None, retryable: bool = True):
        self.user_message = user_message
        self.retryable = retryable
        super().__init__(detail or user_message)


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
        self.cookies_path = Path(__file__).resolve().parent.parent / COOKIES_FILE_NAME

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

        if not self.cookies_path.is_file():
            raise TrackExtractionError(
                "Missing cookies.txt. Export cookies and place the file in the bot root directory.",
                f"cookies file not found at: {self.cookies_path}",
                retryable=False,
            )

        logger.info(f"Using YouTube cookies from: {self.cookies_path}")

        last_error: Optional[Exception] = None
        for attempt in range(1, EXTRACTION_RETRIES + 1):
            try:
                return await asyncio.to_thread(self._extract_track_sync_with_fallback, query, str(self.cookies_path))
            except TrackExtractionError as e:
                last_error = e
                if attempt < EXTRACTION_RETRIES and e.retryable:
                    logger.warning(
                        f"Retrying extraction ({attempt}/{EXTRACTION_RETRIES}) for query '{query}': {e}"
                    )
                    await asyncio.sleep(EXTRACTION_RETRY_DELAY_SECONDS)
                    continue
                logger.error(f"Extraction failed for query '{query}': {e}")
                raise
            except Exception as e:
                last_error = e
                if attempt < EXTRACTION_RETRIES:
                    logger.warning(
                        f"Retrying extraction ({attempt}/{EXTRACTION_RETRIES}) for query '{query}': {e}"
                    )
                    await asyncio.sleep(EXTRACTION_RETRY_DELAY_SECONDS)
                    continue
                logger.error(f"Extraction failed for query '{query}': {e}", exc_info=True)
                raise TrackExtractionError("Could not extract this track from YouTube.", str(e)) from e

        raise TrackExtractionError("Could not extract this track from YouTube.", str(last_error))

    @staticmethod
    def _is_requested_format_unavailable(raw_error: Exception) -> bool:
        return "requested format is not available" in str(raw_error).lower()

    @classmethod
    def _extract_track_sync_with_fallback(cls, query: str, cookies_path: str) -> Dict[str, Any]:
        requested_formats = [PRIMARY_AUDIO_FORMAT, FALLBACK_AUDIO_FORMAT]

        for idx, format_string in enumerate(requested_formats):
            try:
                return cls._extract_track_sync(query, cookies_path, format_override=format_string)
            except TrackExtractionError as format_error:
                if not cls._is_requested_format_unavailable(format_error):
                    raise

                if idx < len(requested_formats) - 1:
                    logger.warning(
                        "yt-dlp format '%s' unavailable for query '%s'. Retrying with '%s'.",
                        format_string,
                        query,
                        requested_formats[idx + 1],
                    )
                    continue

                logger.warning(
                    "yt-dlp selector formats failed for query '%s'. Falling back to direct audio format selection.",
                    query,
                )
                return cls._extract_track_sync(query, cookies_path, allow_selector=False)

        raise TrackExtractionError("Could not extract this track from YouTube.", retryable=False)

    @staticmethod
    def _classify_extraction_error(raw_error: Exception) -> TrackExtractionError:
        message = str(raw_error)
        lowered = message.lower()

        if "sign in to confirm you're not a bot" in lowered:
            return TrackExtractionError(
                "YouTube requires sign-in verification. Refresh cookies.txt and try again.",
                message,
                retryable=False,
            )
        if "http error 429" in lowered or "too many requests" in lowered:
            return TrackExtractionError(
                "YouTube rate-limited this request (HTTP 429). Please wait a bit and retry.",
                message,
            )
        if "video unavailable" in lowered:
            return TrackExtractionError(
                "This video is unavailable on YouTube.",
                message,
                retryable=False,
            )
        if "private video" in lowered:
            return TrackExtractionError(
                "This is a private video and cannot be played.",
                message,
                retryable=False,
            )
        if "requested format is not available" in lowered:
            return TrackExtractionError(
                "No playable audio format is available for this video.",
                message,
                retryable=False,
            )
        return TrackExtractionError("Could not extract this track from YouTube.", message)

    @staticmethod
    def _pick_best_audio_format(formats: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        for fmt in formats:
            if not fmt.get("url"):
                continue
            if fmt.get("acodec") in (None, "none"):
                continue
            candidates.append(fmt)

        if not candidates:
            return None

        def score(fmt: Dict[str, Any]) -> tuple:
            ext = fmt.get("ext") or ""
            ext_score = 2 if ext == "m4a" else 1 if ext == "webm" else 0
            audio_only_score = 1 if (fmt.get("vcodec") in (None, "none")) else 0
            bitrate_score = float(fmt.get("abr") or fmt.get("tbr") or 0)
            return (audio_only_score, ext_score, bitrate_score)

        return max(candidates, key=score)

    @staticmethod
    def _format_label(format_info: Optional[Dict[str, Any]]) -> str:
        if not format_info:
            return "unknown"
        format_id = format_info.get("format_id", "unknown")
        ext = format_info.get("ext", "unknown")
        abr = format_info.get("abr") or format_info.get("tbr") or "?"
        return f"id={format_id}, ext={ext}, abr={abr}"

    @classmethod
    def _normalize_track_info(
        cls,
        info: Dict[str, Any],
        query: str,
        requested_format: str,
        allow_selector: bool,
    ) -> Dict[str, Any]:
        if not info:
            raise TrackExtractionError("No results found for this query.", "No extraction info returned", retryable=False)

        if "entries" in info:
            info = next((entry for entry in info["entries"] if entry), None)
            if not info:
                raise TrackExtractionError("No results found for this query.", "No playable entry in results", retryable=False)

        selected_format: Optional[Dict[str, Any]] = None
        stream_url = info.get("url")
        if not stream_url or not allow_selector:
            selected_format = cls._pick_best_audio_format(info.get("formats") or [])
            if not selected_format:
                raise TrackExtractionError(
                    "No playable audio format is available for this video.",
                    "No audio format with stream URL found",
                    retryable=False,
                )
            stream_url = selected_format.get("url")

        if not stream_url:
            raise TrackExtractionError(
                "Could not create a playable stream URL for this track.",
                "No playable stream URL found",
                retryable=False,
            )

        chosen_format = selected_format or {
            "format_id": info.get("format_id"),
            "ext": info.get("ext"),
            "abr": info.get("abr") or info.get("tbr"),
        }
        logger.info(
            "yt-dlp extraction succeeded for query '%s'. requested_format='%s', chosen_format='%s'",
            query,
            requested_format,
            cls._format_label(chosen_format),
        )

        return {
            "title": info.get("title") or query,
            "webpage_url": info.get("webpage_url") or query,
            "stream_url": stream_url,
            "duration": info.get("duration"),
        }

    @classmethod
    def _extract_track_sync(
        cls,
        query: str,
        cookies_path: str,
        format_override: Optional[str] = None,
        allow_selector: bool = True,
    ) -> Dict[str, Any]:
        ydl_options = dict(YTDL_OPTIONS)
        ydl_options["cookiefile"] = cookies_path
        if allow_selector:
            ydl_options["format"] = format_override or PRIMARY_AUDIO_FORMAT
        else:
            ydl_options.pop("format", None)

        logger.info(
            "Attempting yt-dlp extraction for query '%s' with format='%s'",
            query,
            ydl_options.get("format", "<auto-audio-pick>"),
        )

        with yt_dlp.YoutubeDL(ydl_options) as ydl:
            try:
                info = ydl.extract_info(query, download=False)
            except Exception as e:
                raise cls._classify_extraction_error(e) from e

            return cls._normalize_track_info(
                info,
                query,
                requested_format=ydl_options.get("format", "<auto-audio-pick>"),
                allow_selector=allow_selector,
            )

    @classmethod
    def _list_available_formats_sync(cls, query: str, cookies_path: str) -> Dict[str, Any]:
        ydl_options = dict(YTDL_OPTIONS)
        ydl_options["cookiefile"] = cookies_path
        ydl_options.pop("format", None)

        with yt_dlp.YoutubeDL(ydl_options) as ydl:
            info = ydl.extract_info(query, download=False)

        if not info:
            raise TrackExtractionError("No results found for this query.", "No extraction info returned", retryable=False)

        if "entries" in info:
            info = next((entry for entry in info["entries"] if entry), None)
            if not info:
                raise TrackExtractionError("No results found for this query.", "No playable entry in results", retryable=False)

        formats = info.get("formats") or []

        def sort_key(fmt: Dict[str, Any]) -> tuple:
            has_audio = 1 if fmt.get("acodec") not in (None, "none") else 0
            abr = float(fmt.get("abr") or fmt.get("tbr") or 0)
            return (has_audio, abr)

        sorted_formats = sorted(formats, key=sort_key, reverse=True)
        lines: List[str] = []
        for fmt in sorted_formats:
            fmt_id = str(fmt.get("format_id", "?"))
            ext = str(fmt.get("ext", "?"))
            acodec = str(fmt.get("acodec", "?"))
            vcodec = str(fmt.get("vcodec", "?"))
            abr = fmt.get("abr") or fmt.get("tbr") or "?"
            lines.append(f"{fmt_id:>6} ext={ext:<5} acodec={acodec:<12} vcodec={vcodec:<12} br={abr}")

        logger.info("yt-dlp formats for '%s':\n%s", query, "\n".join(lines[:30]))
        return {
            "title": info.get("title") or query,
            "formats": lines,
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
        except TrackExtractionError as e:
            logger.error(f"Failed to resolve track '{query}': {e}")
            await interaction.followup.send(
                embed=EmbedFactory.error("Track Error", e.user_message),
                ephemeral=True,
            )
            return
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

    @app_commands.command(name="ytdlp_formats", description="Admin debug: list yt-dlp formats for a YouTube URL")
    @app_commands.describe(url="YouTube URL")
    @is_admin()
    async def ytdlp_formats(self, interaction: discord.Interaction, url: str):
        """List available yt-dlp formats for debugging on VPS."""
        if yt_dlp is None:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Dependency Missing", "yt-dlp is not installed. Run: pip install yt-dlp"),
                ephemeral=True,
            )
            return

        if os.getenv(YTDLP_DEBUG_ENV, "0") != "1":
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Debug Disabled",
                    f"Set {YTDLP_DEBUG_ENV}=1 in environment and restart the bot to use this command.",
                ),
                ephemeral=True,
            )
            return

        if not self.cookies_path.is_file():
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Missing cookies.txt",
                    "cookies.txt was not found in the bot root directory.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            format_data = await asyncio.to_thread(self._list_available_formats_sync, url, str(self.cookies_path))
        except TrackExtractionError as e:
            await interaction.followup.send(embed=EmbedFactory.error("Format Debug Error", e.user_message), ephemeral=True)
            return
        except Exception as e:
            logger.error(f"Failed to list yt-dlp formats for '{url}': {e}", exc_info=True)
            await interaction.followup.send(
                embed=EmbedFactory.error("Format Debug Error", "Could not list formats for this URL."),
                ephemeral=True,
            )
            return

        max_block_chars = 1400
        selected_lines: List[str] = []
        used = 0
        for line in format_data["formats"]:
            next_size = len(line) + 1
            if used + next_size > max_block_chars:
                break
            selected_lines.append(line)
            used += next_size

        list_text = "\n".join(selected_lines) if selected_lines else "No formats found."
        summary = (
            f"Title: {format_data['title']}\n"
            f"Showing {len(selected_lines)} of {len(format_data['formats'])} formats."
        )
        await interaction.followup.send(f"{summary}\n```text\n{list_text}\n```", ephemeral=True)

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

