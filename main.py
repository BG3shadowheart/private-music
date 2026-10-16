import os
import re
import asyncio
import aiohttp
import logging
import urllib.parse
from pathlib import Path
from aiohttp import web

import discord
from discord.ext import commands
import wavelink

# ---------- Config ----------
TOKEN         = os.environ["DISCORD_TOKEN"]
GUILD_ID      = int(os.environ["GUILD_ID"])
LAVALINK_HOST = os.environ["LAVALINK_HOST"]
LAVALINK_PORT = int(os.environ.get("LAVALINK_PORT", 443))
LAVALINK_PASS = os.environ["LAVALINK_PASSWORD"]
LAVALINK_SEC  = os.environ.get("LAVALINK_SECURE", "true").lower() == "true"

SLSKD_URL     = os.environ.get("SLSKD_URL", "http://127.0.0.1:5030")
SLSKD_KEY     = os.environ["SLSKD_API_KEY"]

DOWNLOAD_DIR  = Path(os.environ.get("DOWNLOAD_DIR", "/data/downloads"))
HTTP_PORT     = int(os.environ.get("HTTP_PORT", 8080))
# The Lavalink service fetches completed files over Railway private networking.
# Example: http://${{private-music.RAILWAY_PRIVATE_DOMAIN}}:8080
AUDIO_BASE_URL = os.environ.get("AUDIO_BASE_URL", "").rstrip("/")

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("soulseek-bot")

# ---------- HTTP file server ----------
async def serve_audio(request):
    fname = request.match_info["fname"]
    fname = os.path.basename(fname)
    path = DOWNLOAD_DIR / fname
    if not path.exists():
        return web.Response(status=404, text="not found")
    return web.FileResponse(path)

async def start_http_server():
    app = web.Application()
    app.router.add_get("/audio/{fname}", serve_audio)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    log.info("HTTP file server on :%d", HTTP_PORT)

# ---------- slskd helpers ----------
HEADERS = {"X-API-Key": SLSKD_KEY}

async def slskd_search(query: str, timeout: int = 12):
    async with aiohttp.ClientSession(headers=HEADERS) as s:
        r = await s.post(f"{SLSKD_URL}/api/v0/searches", json={"searchText": query})
        r.raise_for_status()
        search_id = (await r.json())["id"]

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
            r = await s.get(f"{SLSKD_URL}/api/v0/searches/{search_id}")
            data = await r.json()
            count = len(data.get("responses", []))
            if count >= 20:
                break

        r = await s.get(f"{SLSKD_URL}/api/v0/searches/{search_id}")
        data = await r.json()

        try:
            await s.delete(f"{SLSKD_URL}/api/v0/searches/{search_id}")
        except Exception:
            pass

        return data.get("responses", [])

def score_file(filename: str, filesize: int, bitrate: int) -> float:
    ext = Path(filename).suffix.lower()
    ext_score = {".flac": 100, ".wav": 90, ".m4a": 70, ".mp3": 60, ".ogg": 55}.get(ext, 0)
    bit_score = min(bitrate or 0, 1411) / 20.0
    size_score = min(filesize / (1024 * 1024), 100) / 5
    return ext_score + bit_score + size_score

def pick_best(responses, query: str):
    candidates = []
    q_tokens = set(re.findall(r"\w+", query.lower()))

    for resp in responses:
        if not resp.get("hasFreeUploadSlot"):
            continue
        for f in resp.get("files", []):
            fname = f.get("filename", "")
            if f.get("isLocked"):
                continue
            ext = Path(fname).suffix.lower()
            if ext not in {".flac", ".mp3", ".m4a", ".wav", ".ogg"}:
                continue
            fname_tokens = set(re.findall(r"\w+", fname.lower()))
            if not (q_tokens & fname_tokens):
                continue
            score = score_file(fname, f.get("size", 0), f.get("bitRate", 0))
            queue = resp.get("queueLength", 0)
            score -= queue * 0.5
            candidates.append((score, resp["username"], f))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1], candidates[0][2]

async def slskd_download(username: str, file_info: dict, timeout: int = 180):
    async with aiohttp.ClientSession(headers=HEADERS) as s:
        payload = [{"filename": file_info["filename"], "size": file_info["size"]}]
        r = await s.post(f"{SLSKD_URL}/api/v0/transfers/downloads/{username}", json=payload)
        r.raise_for_status()

        deadline = asyncio.get_event_loop().time() + timeout
        target_name = Path(file_info["filename"]).name

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(3)
            r = await s.get(f"{SLSKD_URL}/api/v0/transfers/downloads/{username}")
            data = await r.json()
            for d in data.get("directories", []):
                for f in d.get("files", []):
                    if Path(f["filename"]).name != target_name:
                        continue
                    state = f.get("state", "")
                    if state == "Completed, Succeeded":
                        local = DOWNLOAD_DIR / target_name
                        if local.exists():
                            return local
                    if "Failed" in state or "Cancelled" in state:
                        raise RuntimeError(f"download failed: {state}")
        raise TimeoutError("soulseek download timed out")

# ---------- Discord bot ----------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=".", intents=intents)

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def get_player(self, ctx):
        if not ctx.author.voice:
            await ctx.send("Join a voice channel first.")
            return None
        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                await ctx.voice_client.move_to(ctx.author.voice.channel)
            return ctx.voice_client
        try:
            return await ctx.author.voice.channel.connect(
                cls=wavelink.Player, self_deaf=False, reconnect=True, timeout=30.0)
        except Exception as e:
            log.exception("voice connect failed")
            await ctx.send(f"Could not join: {e}")
            return None

    @commands.command()
    async def join(self, ctx):
        p = await self.get_player(ctx)
        if p:
            await ctx.send(f"Joined **{p.channel.name}**")

    @commands.command()
    async def play(self, ctx, *, query: str):
        player = await self.get_player(ctx)
        if not player:
            return
        await ctx.send(f"Searching Soulseek for **{query}**…")

        try:
            responses = await slskd_search(query)
            if not responses:
                await ctx.send("No Soulseek results.")
                return
            best = pick_best(responses, query)
            if not best:
                await ctx.send("No usable results (all locked or queued).")
                return
            username, file_info = best
            fname = Path(file_info["filename"]).name
            await ctx.send(f"Downloading `{fname}` from **{username}** ({file_info.get('bitRate','?')} kbps)…")

            local_path = await slskd_download(username, file_info)
            if not AUDIO_BASE_URL:
                await ctx.send("Audio server is not configured. Set AUDIO_BASE_URL on Railway.")
                return
            url = f"{AUDIO_BASE_URL}/audio/{urllib.parse.quote(local_path.name)}"

            tracks = await wavelink.Playable.search(url)
            if not tracks:
                await ctx.send("Lavalink couldn't load the file.")
                return
            track = tracks[0] if not isinstance(tracks, wavelink.Playlist) else tracks.tracks[0]
            if player.playing or player.paused:
                await player.queue.put_wait(track)
                await ctx.send(f"➕ Added to queue: **{track.title}**")
            else:
                await player.play(track)
                await ctx.send(f"▶️ Now playing: **{track.title}**")

        except Exception as e:
            log.exception(".play failed")
            await ctx.send(f"Play failed: `{e}`")

    @commands.command()
    async def stop(self, ctx):
        if ctx.voice_client:
            ctx.voice_client.queue.clear()
            await ctx.voice_client.skip(force=True)
            await ctx.send("Stopped and cleared the queue. I will stay in voice.")

    @commands.command(name="help")
    async def help_command(self, ctx):
        embed = discord.Embed(title="Private Music", description="Your 24/7 private library player", color=0xF4A7C3)
        embed.add_field(name="`.join`", value="Join your voice channel and remain connected.", inline=False)
        embed.add_field(name="`.play <song name>`", value="Find, download, and play a high-quality audio file.", inline=False)
        embed.add_field(name="`.stop`", value="Stop playback and clear the queue while staying in voice.", inline=False)
        await ctx.send(embed=embed)

@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)

@bot.event
async def setup_hook():
    node = wavelink.Node(
        uri=f"{'https' if LAVALINK_SEC else 'http'}://{LAVALINK_HOST}:{LAVALINK_PORT}",
        password=LAVALINK_PASS,
    )
    await wavelink.Pool.connect(nodes=[node], client=bot, cache_capacity=100)
    log.info("Connected to Lavalink")

@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    """Advance one track at a time; never overlap audio streams."""
    player = payload.player
    if player.queue.is_empty:
        return
    next_track = player.queue.get()
    await player.play(next_track)

async def main():
    await start_http_server()
    await bot.add_cog(Music(bot))
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
