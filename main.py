import os
import re
import json
import asyncio
import aiohttp
import logging
import urllib.parse
from pathlib import Path
from typing import Optional

from aiohttp import web

import discord
from discord.ext import commands
import wavelink

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------
TOKEN         = os.environ["DISCORD_TOKEN"]
GUILD_ID      = int(os.environ["GUILD_ID"])
LAVALINK_HOST = os.environ["LAVALINK_HOST"]
LAVALINK_PORT = int(os.environ.get("LAVALINK_PORT", 2333))
LAVALINK_PASS = os.environ["LAVALINK_PASSWORD"]
LAVALINK_SEC  = os.environ.get("LAVALINK_SECURE", "false").lower() == "true"

SLSKD_URL     = os.environ.get("SLSKD_URL", "http://127.0.0.1:5030")
SLSKD_KEY     = os.environ["SLSKD_API_KEY"]

DOWNLOAD_DIR  = Path(os.environ.get("DOWNLOAD_DIR", "/data/downloads"))
HTTP_PORT     = int(os.environ.get("HTTP_PORT", 8080))
PUBLIC_HOST   = os.environ["PUBLIC_HOST"]
STATE_FILE    = Path(os.environ.get("STATE_FILE", "/data/state.json"))

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("music-bot")

# ------------------------------------------------------------------
# State persistence
# ------------------------------------------------------------------
_state_lock = asyncio.Lock()

async def load_state() -> dict:
    async with _state_lock:
        if not STATE_FILE.exists():
            return {}
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            log.warning("state file corrupt, starting fresh")
            return {}

async def save_state(state: dict):
    async with _state_lock:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(STATE_FILE)

# ------------------------------------------------------------------
# HTTP file server (serves downloaded audio to Lavalink)
# ------------------------------------------------------------------
async def serve_audio(request):
    fname = os.path.basename(request.match_info["fname"])
    path = DOWNLOAD_DIR / fname
    if not path.exists():
        return web.Response(status=404, text="not found")
    return web.FileResponse(path)

async def start_http_server():
    app = web.Application()
    app.router.add_get("/audio/{fname}", serve_audio)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()
    log.info("HTTP file server on :%d", HTTP_PORT)

# ------------------------------------------------------------------
# slskd helpers
# ------------------------------------------------------------------
HEADERS = {"X-API-Key": SLSKD_KEY}

async def slskd_search(query: str, timeout: int = 15) -> list:
    async with aiohttp.ClientSession(headers=HEADERS) as s:
        try:
            r = await s.post(f"{SLSKD_URL}/api/v0/searches", json={"searchText": query})
            r.raise_for_status()
            search_id = (await r.json())["id"]
        except Exception as e:
            log.error("slskd search start failed: %s", e)
            return []

        deadline = asyncio.get_event_loop().time() + timeout
        responses = []
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
            try:
                r = await s.get(f"{SLSKD_URL}/api/v0/searches/{search_id}")
                data = await r.json()
                responses = data.get("responses", [])
                if data.get("isComplete") or len(responses) >= 30:
                    break
            except Exception as e:
                log.error("slskd poll failed: %s", e)
                break

        try:
            await s.delete(f"{SLSKD_URL}/api/v0/searches/{search_id}")
        except Exception:
            pass

        log.info("slskd search '%s' returned %d responses", query, len(responses))
        return responses

def score_file(filename: str, filesize: int, bitrate: int) -> float:
    ext = Path(filename).suffix.lower()
    ext_score = {".flac": 100, ".wav": 90, ".m4a": 70, ".mp3": 60, ".ogg": 55}.get(ext, 0)
    bit_score = min(bitrate or 0, 1411) / 20.0
    size_score = min(filesize / (1024 * 1024), 100) / 5
    return ext_score + bit_score + size_score

def pick_best(responses: list, query: str):
    """Pick best (username, file_info) or None. Loose token match so we're not too strict."""
    q_tokens = set(re.findall(r"\w+", query.lower()))
    q_tokens = {t for t in q_tokens if len(t) > 2}  # ignore short words

    candidates = []
    for resp in responses:
        for f in resp.get("files", []):
            if f.get("isLocked"):
                continue
            fname = f.get("filename", "")
            ext = Path(fname).suffix.lower()
            if ext not in {".flac", ".mp3", ".m4a", ".wav", ".ogg"}:
                continue
            fname_tokens = set(re.findall(r"\w+", fname.lower()))
            overlap = len(q_tokens & fname_tokens)
            # Require at least half the meaningful tokens match
            if q_tokens and overlap < max(1, len(q_tokens) // 2):
                continue
            score = score_file(fname, f.get("size", 0), f.get("bitRate", 0))
            score += overlap * 10
            score -= resp.get("queueLength", 0) * 0.5
            if not resp.get("hasFreeUploadSlot"):
                score -= 20
            candidates.append((score, resp["username"], f))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1], candidates[0][2]

async def slskd_download(username: str, file_info: dict, timeout: int = 240) -> Optional[Path]:
    async with aiohttp.ClientSession(headers=HEADERS) as s:
        payload = [{"filename": file_info["filename"], "size": file_info["size"]}]
        try:
            r = await s.post(f"{SLSKD_URL}/api/v0/transfers/downloads/{username}", json=payload)
            r.raise_for_status()
        except Exception as e:
            log.error("slskd enqueue failed: %s", e)
            return None

        deadline = asyncio.get_event_loop().time() + timeout
        target_name = Path(file_info["filename"]).name

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(3)
            try:
                r = await s.get(f"{SLSKD_URL}/api/v0/transfers/downloads/{username}")
                data = await r.json()
            except Exception:
                continue
            for d in data.get("directories", []):
                for f in d.get("files", []):
                    if Path(f["filename"]).name != target_name:
                        continue
                    state = f.get("state", "")
                    if "Completed" in state and "Succeeded" in state:
                        local = DOWNLOAD_DIR / target_name
                        if local.exists():
                            return local
                    if "Failed" in state or "Cancelled" in state or "Rejected" in state:
                        log.warning("slskd download failed: %s", state)
                        return None
        log.warning("slskd download timed out for %s", target_name)
        return None

# ------------------------------------------------------------------
# URL resolvers (extract song name from Spotify / YouTube links)
# ------------------------------------------------------------------
async def resolve_url_title(url: str) -> Optional[str]:
    """Use public oEmbed endpoints (no API key needed)."""
    try:
        async with aiohttp.ClientSession() as s:
            if "open.spotify.com" in url:
                api = f"https://open.spotify.com/oembed?url={urllib.parse.quote(url)}"
            elif "youtube.com" in url or "youtu.be" in url:
                api = f"https://www.youtube.com/oembed?url={urllib.parse.quote(url)}&format=json"
            else:
                return None
            async with s.get(api, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                return data.get("title")
    except Exception as e:
        log.warning("URL resolve failed: %s", e)
        return None

def simplify_query(q: str) -> str:
    """Strip brackets, feat., etc. for a broader Soulseek search."""
    q = re.sub(r"\(.*?\)|\[.*?\]", "", q)
    q = re.sub(r"\b(feat|ft|featuring)\.?\s+\S+", "", q, flags=re.IGNORECASE)
    q = re.sub(r"[^\w\s\-']", " ", q)
    return re.sub(r"\s+", " ", q).strip()

# ------------------------------------------------------------------
# Discord bot
# ------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)

# Per-guild playback state
players_state: dict[int, dict] = {}
# Last voice channel per guild, for reconnection
voice_persist: dict[int, int] = {}


def now_playing_embed(track, player, loop_mode: str) -> discord.Embed:
    title = track.title or "Unknown"
    e = discord.Embed(
        title="🎵 Now Playing",
        description=f"**{title}**\nby `{track.author or 'Unknown'}`",
        color=0xff8fd0,
    )
    if track.artwork:
        e.set_thumbnail(url=track.artwork)
    status = "⏸ Paused" if player.paused else "▶️ Playing"
    e.add_field(name="Status", value=status, inline=True)
    e.add_field(name="Loop", value=loop_mode, inline=True)
    e.add_field(name="Volume", value=f"{player.volume}%", inline=True)
    e.set_footer(text="Use the buttons below ✨")
    return e


class PlayerPanel(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary)
    async def pause_resume(self, itx: discord.Interaction, _):
        player = itx.guild.voice_client
        if not player:
            return await itx.response.send_message("Not connected.", ephemeral=True)
        if player.paused:
            await player.pause(False)
        else:
            await player.pause(True)
        await self._refresh(itx)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.primary)
    async def next_track(self, itx: discord.Interaction, _):
        player = itx.guild.voice_client
        if not player:
            return await itx.response.send_message("Not connected.", ephemeral=True)
        await player.stop()
        await itx.response.defer()

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop(self, itx: discord.Interaction, _):
        state = players_state.setdefault(self.guild_id, {})
        modes = ["off", "track", "queue"]
        current = state.get("loop", "off")
        new = modes[(modes.index(current) + 1) % len(modes)]
        state["loop"] = new
        await itx.response.send_message(f"Loop: **{new}**", ephemeral=True)

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary)
    async def vol_down(self, itx: discord.Interaction, _):
        player = itx.guild.voice_client
        if not player:
            return await itx.response.send_message("Not connected.", ephemeral=True)
        new = max(0, player.volume - 10)
        await player.set_volume(new)
        await self._refresh(itx)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary)
    async def vol_up(self, itx: discord.Interaction, _):
        player = itx.guild.voice_client
        if not player:
            return await itx.response.send_message("Not connected.", ephemeral=True)
        new = min(200, player.volume + 10)
        await player.set_volume(new)
        await self._refresh(itx)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle(self, itx: discord.Interaction, _):
        state = players_state.setdefault(self.guild_id, {})
        queue = state.get("queue", [])
        import random
        random.shuffle(queue)
        state["queue"] = queue
        await itx.response.send_message("Queue shuffled ✨", ephemeral=True)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary)
    async def queue(self, itx: discord.Interaction, _):
        state = players_state.setdefault(self.guild_id, {})
        queue = state.get("queue", [])
        if not queue:
            return await itx.response.send_message("Queue is empty.", ephemeral=True)
        lines = [f"`{i+1}.` {t[1]}" for i, t in enumerate(queue[:15])]
        await itx.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop(self, itx: discord.Interaction, _):
        player = itx.guild.voice_client
        if player:
            state = players_state.setdefault(self.guild_id, {})
            state["queue"] = []
            await player.stop()
        await itx.response.defer()

    async def _refresh(self, itx: discord.Interaction):
        try:
            player = itx.guild.voice_client
            if player and player.current:
                await itx.response.edit_message(
                    embed=now_playing_embed(player.current, player,
                                            players_state.get(self.guild_id, {}).get("loop", "off")),
                    view=self,
                )
            else:
                await itx.response.defer()
        except discord.HTTPException:
            pass


# ------------------------------------------------------------------
# Music cog
# ------------------------------------------------------------------
class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._advancing: set[int] = set()  # guild IDs currently advancing

    async def ensure_voice(self, ctx) -> Optional[wavelink.Player]:
        if ctx.voice_client:
            return ctx.voice_client
        if not ctx.author.voice:
            await ctx.send("❌ Join a voice channel first.")
            return None
        try:
            player: wavelink.Player = await ctx.author.voice.channel.connect(
                cls=wavelink.Player, self_deaf=True, reconnect=True, timeout=30.0,
            )
        except Exception as e:
            log.exception("voice connect failed")
            await ctx.send(f"❌ Could not join: `{e}`")
            return None

        # Persist voice channel
        voice_persist[ctx.guild.id] = ctx.author.voice.channel.id
        state = await load_state()
        state.setdefault("guilds", {})[str(ctx.guild.id)] = {"voice_channel_id": ctx.author.voice.channel.id}
        await save_state(state)

        # Set region (best-effort)
        try:
            await ctx.author.voice.channel.edit(rtc_region="singapore")
        except (discord.Forbidden, discord.HTTPException):
            log.info("could not set voice region (no perms)")

        # Set bitrate (best-effort)
        try:
            await ctx.author.voice.channel.edit(bitrate=384000)
        except (discord.Forbidden, discord.HTTPException):
            log.info("could not set bitrate 384k (limit or perms)")

        return player

    async def _search_and_play(self, ctx, query: str):
        """Resolve → search Soulseek → download → play."""
        player = await self.ensure_voice(ctx)
        if not player:
            return

        state = players_state.setdefault(ctx.guild.id, {"queue": [], "loop": "off"})

        # Detect URL
        original_query = query
        if query.startswith(("http://", "https://")):
            await ctx.send(f"🔗 Resolving URL…")
            title = await resolve_url_title(query)
            if title:
                query = title
                log.info("URL resolved to: %s", title)
            else:
                await ctx.send("⚠️ Couldn't read that link. Paste the song name instead.")
                return

        search_queries = [query, simplify_query(query)]
        responses = []
        used_query = query
        for q in search_queries:
            if not q:
                continue
            await ctx.send(f"🔎 Searching Soulseek for **{q}**…")
            responses = await slskd_search(q)
            if responses:
                used_query = q
                break

        best = pick_best(responses, used_query) if responses else None

        # Fallback to SoundCloud via Lavalink if Soulseek has nothing
        if not best:
            log.warning("Soulseek failed, trying SoundCloud for: %s", query)
            await ctx.send("🔄 Not on Soulseek — trying SoundCloud…")
            try:
                sc = await wavelink.Playable.search(f"scsearch:{query}")
                if sc:
                    track = sc[0] if not isinstance(sc, wavelink.Playlist) else sc.tracks[0]
                    await self._enqueue_and_play(ctx, player, track, state)
                    return
            except Exception as e:
                log.exception("SoundCloud fallback failed")

            await ctx.send("❌ Couldn't find that song anywhere. Try a different name.")
            return

        username, file_info = best
        fname = Path(file_info["filename"]).name
        status = await ctx.send(f"⬇️ Downloading `{fname}` from **{username}** "
                                f"({file_info.get('bitRate','?')} kbps)…")

        local_path = await slskd_download(username, file_info)
        if not local_path:
            await status.edit(content="❌ Download failed (uploader offline or queue too long).")
            return

        url = f"http://{PUBLIC_HOST}:{HTTP_PORT}/audio/{urllib.parse.quote(local_path.name)}"
        try:
            tracks = await wavelink.Playable.search(url)
            if not tracks:
                await status.edit(content="❌ Lavalink couldn't load the file.")
                return
            track = tracks[0] if not isinstance(tracks, wavelink.Playlist) else tracks.tracks[0]
        except Exception as e:
            log.exception("lavalink load failed")
            await status.edit(content=f"❌ Lavalink error: `{e}`")
            return

        await status.delete()
        await self._enqueue_and_play(ctx, player, track, state)

    async def _enqueue_and_play(self, ctx, player: wavelink.Player, track, state: dict):
        if player.playing or player.paused:
            # Queue it
            state["queue"].append((track, track.title))
            msg = await ctx.send(f"➕ Added to queue: **{track.title}**")
            # We'll delete this when the track starts (see on_wavelink_track_start)
            state.setdefault("pending_queue_msgs", []).append((msg.channel.id, msg.id))
            return

        await player.play(track)
        # Panel will be posted by on_wavelink_track_start

    # -------- Commands --------
    @commands.command()
    async def join(self, ctx):
        player = await self.ensure_voice(ctx)
        if player:
            await ctx.send(f"✅ Joined **{player.channel.name}** (Singapore)")

    @commands.command()
    async def play(self, ctx, *, query: str = None):
        if not query:
            return await ctx.send("Usage: `.play <song name or URL>`")
        try:
            await self._search_and_play(ctx, query)
        except Exception as e:
            log.exception(".play failed")
            await ctx.send(f"❌ Error: `{e}`")

    @commands.command()
    async def stop(self, ctx):
        state = players_state.setdefault(ctx.guild.id, {"queue": [], "loop": "off"})
        state["queue"] = []
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
            await ctx.send("⏹️ Stopped and disconnected.")
        else:
            await ctx.send("Not connected.")

    @commands.command()
    async def help(self, ctx):
        e = discord.Embed(
            title="🎀 ABN Music Bot — Commands",
            color=0xff8fd0,
            description="Your kawaii Soulseek music bot ✨",
        )
        e.add_field(name="`.join`", value="Join your voice channel", inline=False)
        e.add_field(name="`.play <song or URL>`", value="Search & play from Soulseek (or Spotify/YouTube links)", inline=False)
        e.add_field(name="`.stop`", value="Stop and disconnect", inline=False)
        e.add_field(name="`.help`", value="Show this panel", inline=False)
        e.set_footer(text="Use the buttons on the player panel to control playback 💖")
        await ctx.send(embed=e)


# ------------------------------------------------------------------
# Events
# ------------------------------------------------------------------
@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)
    # Try to restore voice session
    state = await load_state()
    for guild_id_str, data in state.get("guilds", {}).items():
        voice_persist[int(guild_id_str)] = data.get("voice_channel_id")
        log.info("Restored voice channel for guild %s: %s",
                 guild_id_str, data.get("voice_channel_id"))


@bot.event
async def setup_hook():
    uri = f"{'https' if LAVALINK_SEC else 'http'}://{LAVALINK_HOST}:{LAVALINK_PORT}"
    node = wavelink.Node(uri=uri, password=LAVALINK_PASS)
    try:
        await wavelink.Pool.connect(nodes=[node], client=bot, cache_capacity=100)
        log.info("Connected to Lavalink at %s", uri)
    except Exception as e:
        log.error("Lavalink connection failed: %s", e)


@bot.event
async def on_wavelink_track_start(payload: wavelink.TrackStartEventPayload):
    player = payload.player
    guild = player.guild
    state = players_state.setdefault(guild.id, {"queue": [], "loop": "off"})

    # Clean up any pending "added to queue" messages
    for chan_id, msg_id in state.pop("pending_queue_msgs", []):
        try:
            chan = guild.get_channel(chan_id)
            if chan:
                msg = await chan.fetch_message(msg_id)
                await msg.delete()
        except Exception:
            pass

    # Send/refresh the player panel
    try:
        embed = now_playing_embed(payload.track, player, state.get("loop", "off"))
        panel_msg = await player.channel.send(embed=embed, view=PlayerPanel(guild.id))
        state["panel_msg_id"] = panel_msg.id
        state["panel_chan_id"] = player.channel.id
    except Exception:
        log.exception("failed to send panel")


@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    player = payload.player
    guild = player.guild
    state = players_state.setdefault(guild.id, {"queue": [], "loop": "off"})

    if guild.id in self_advance_guard:
        return

    loop = state.get("loop", "off")

    # Loop current track
    if loop == "track" and payload.reason != "stopped":
        await player.play(payload.track)
        return

    # Advance queue
    queue = state.get("queue", [])
    if queue:
        next_track, _ = queue.pop(0)
        state["queue"] = queue
        if loop == "queue":
            queue.append((payload.track, payload.track.title))
        await player.play(next_track)
        return

    # Queue empty → restore bitrate, disconnect if 24/7 not on
    await self_cleanup_voice(player)


self_advance_guard: set[int] = set()


async def self_cleanup_voice(player: wavelink.Player):
    try:
        ch = player.channel
        if ch and ch.bitrate != 64000:
            await ch.edit(bitrate=64000)
    except Exception:
        pass


@bot.event
async def on_voice_state_update(member, before, after):
    """Reconnect if our bot gets disconnected from voice."""
    if member.id != bot.user.id:
        return
    if before.channel and not after.channel:
        # We were disconnected
        guild_id = member.guild.id
        persisted = voice_persist.get(guild_id)
        if not persisted:
            return
        log.warning("voice disconnected, attempting reconnect in 5s…")
        await asyncio.sleep(5)
        try:
            channel = member.guild.get_channel(persisted)
            if channel:
                await channel.connect(cls=wavelink.Player, self_deaf=True, reconnect=True)
                log.info("reconnected to %s", channel.name)
        except Exception as e:
            log.error("reconnect failed: %s", e)


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------
async def main():
    await start_http_server()
    await bot.add_cog(Music(bot))
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
