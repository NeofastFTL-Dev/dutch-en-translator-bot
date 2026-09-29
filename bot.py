"""
Dutch → English live translator bot for Discord.

Watches Dutch text, posts English in a role-locked channel,
and can speak the translation in a voice channel with
Microsoft neural TTS (free) or ElevenLabs (custom AI voice).
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from langdetect import DetectorFactory, LangDetectException, detect

load_dotenv()
DetectorFactory.seed = 0

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", "").strip()
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "").strip()

GUILD_ID = int(os.getenv("GUILD_ID") or 0)
SOURCE_CHANNEL_IDS = {
    int(x) for x in os.getenv("SOURCE_CHANNEL_IDS", "").replace(" ", "").split(",") if x
}
OUTPUT_CHANNEL_ID = int(os.getenv("OUTPUT_CHANNEL_ID") or 0)
ENGLISH_ROLE_ID = int(os.getenv("ENGLISH_ROLE_ID") or 0)
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID") or 0)

TARGET_LANG = os.getenv("TARGET_LANG", "EN-US")
SOURCE_LANG = os.getenv("SOURCE_LANG", "NL")
AUTO_TRANSLATE = os.getenv("AUTO_TRANSLATE", "true").lower() == "true"
PING_ENGLISH_ROLE = os.getenv("PING_ENGLISH_ROLE", "false").lower() == "true"
SPEAK_TRANSLATIONS = os.getenv("SPEAK_TRANSLATIONS", "true").lower() == "true"
MIN_CHARS = int(os.getenv("MIN_CHARS") or 3)
COOLDOWN_SECONDS = float(os.getenv("COOLDOWN_SECONDS") or 1.5)
EDGE_TTS_VOICE = os.getenv("EDGE_TTS_VOICE", "en-US-JennyNeural")

URL_RE = re.compile(r"https?://\S+")
MENTION_RE = re.compile(r"<@!?\d+>|<@&\d+>|<#\d+>")
EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
SKIP_PREFIXES = ("!", "/", ".", "?", "$")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tts_lock = asyncio.Lock()
last_spoken: dict[int, float] = {}


def _looks_like_dutch(text: str) -> bool:
    cleaned = EMOJI_RE.sub("", MENTION_RE.sub("", URL_RE.sub("", text))).strip()
    if len(cleaned) < MIN_CHARS:
        return False
    try:
        lang = detect(cleaned)
    except LangDetectException:
        dutchish = re.search(
            r"\b(het|een|de|ik|jij|je|wij|we|zij|ze|niet|wel|ook|maar|want|dat|dit|die|van|voor|met|naar|als|er|is|zijn|was|heb|hebt|heeft|gaan|gaat|doe|doen)\b",
            cleaned,
            re.IGNORECASE,
        )
        return bool(dutchish)
    return lang in {"nl", "af"}


async def translate_nl_en(text: str) -> tuple[str, str]:
    cleaned = text.strip()
    if DEEPL_API_KEY:
        try:
            import deepl

            translator = deepl.DeepLClient(DEEPL_API_KEY)
            result = await asyncio.to_thread(
                translator.translate_text,
                cleaned,
                source_lang="NL",
                target_lang=TARGET_LANG,
            )
            return result.text, "DeepL"
        except Exception as exc:  # noqa: BLE001
            print(f"[translate] DeepL failed, falling back: {exc}")

    try:
        from deep_translator import GoogleTranslator

        out = await asyncio.to_thread(
            GoogleTranslator(source="nl", target="en").translate, cleaned
        )
        return out, "Google"
    except Exception as exc:  # noqa: BLE001
        print(f"[translate] Google failed: {exc}")
        return cleaned, "none"


async def synthesize_speech(text: str) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()
    path = Path(tmp.name)

    if ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID:
        import aiohttp

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
        headers = {
            "xi-api-key": ELEVENLABS_API_KEY,
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
        }
        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.4, "similarity_boost": 0.8},
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"ElevenLabs {resp.status}: {body[:200]}")
                path.write_bytes(await resp.read())
        return path

    import edge_tts

    communicate = edge_tts.Communicate(text, EDGE_TTS_VOICE)
    await communicate.save(str(path))
    return path


async def speak_in_guild(guild: discord.Guild, text: str) -> str | None:
    if not SPEAK_TRANSLATIONS:
        return None

    vc: discord.VoiceClient | None = guild.voice_client
    if vc is None or not vc.is_connected():
        channel = guild.get_channel(VOICE_CHANNEL_ID) if VOICE_CHANNEL_ID else None
        if not isinstance(channel, discord.VoiceChannel):
            return "not-connected"
        vc = await channel.connect()

    audio_path = await synthesize_speech(text)
    try:
        async with tts_lock:
            while vc.is_playing():
                await asyncio.sleep(0.15)
            source = discord.FFmpegPCMAudio(str(audio_path), options="-vn")
            done = asyncio.Event()

            def _after(err: Exception | None) -> None:
                if err:
                    print(f"[tts] playback error: {err}")
                bot.loop.call_soon_threadsafe(done.set)

            vc.play(source, after=_after)
            await done.wait()
    finally:
        try:
            audio_path.unlink(missing_ok=True)
        except OSError:
            pass
    return "ok"


def should_watch(message: discord.Message) -> bool:
    if message.author.bot:
        return False
    if not message.guild:
        return False
    if GUILD_ID and message.guild.id != GUILD_ID:
        return False
    if SOURCE_CHANNEL_IDS and message.channel.id not in SOURCE_CHANNEL_IDS:
        return False
    content = message.content.strip()
    if len(content) < MIN_CHARS:
        return False
    if content.startswith(SKIP_PREFIXES):
        return False
    if content.startswith(bot.user.mention if bot.user else "@"):
        return False
    return True


def translation_embed(
    original: str,
    translated: str,
    author: discord.Member | discord.User,
    engine: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="Dutch → English",
        color=discord.Color.from_rgb(244, 167, 185),
        description=translated,
    )
    embed.add_field(
        name="Original",
        value=original[:1024] if original else "—",
        inline=False,
    )
    embed.set_author(name=str(author), icon_url=author.display_avatar.url)
    embed.set_footer(text=f"via {engine} · for English native speakers")
    return embed


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} ({bot.user.id if bot.user else '?'})")
    try:
        if GUILD_ID:
            bot.tree.copy_global_to(guild=discord.Object(id=GUILD_ID))
            synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        else:
            synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands")
    except Exception as exc:  # noqa: BLE001
        print(f"Command sync failed: {exc}")


@bot.event
async def on_message(message: discord.Message) -> None:
    await bot.process_commands(message)
    if not AUTO_TRANSLATE or not should_watch(message):
        return
    if not _looks_like_dutch(message.content):
        return

    translated, engine = await translate_nl_en(message.content)
    if not translated or translated.strip() == message.content.strip():
        return

    dest = message.guild.get_channel(OUTPUT_CHANNEL_ID) if message.guild else None
    if not isinstance(dest, discord.TextChannel):
        dest = message.channel

    mention = f"<@&{ENGLISH_ROLE_ID}> " if PING_ENGLISH_ROLE and ENGLISH_ROLE_ID else ""
    embed = translation_embed(message.content, translated, message.author, engine)
    jump = f"[Jump to original]({message.jump_url})"
    await dest.send(
        content=f"{mention}{jump}",
        embed=embed,
        allowed_mentions=discord.AllowedMentions(roles=bool(PING_ENGLISH_ROLE)),
    )

    if SPEAK_TRANSLATIONS and message.guild:
        now = asyncio.get_event_loop().time()
        last = last_spoken.get(message.guild.id, 0)
        if now - last >= COOLDOWN_SECONDS:
            last_spoken[message.guild.id] = now
            spoken_as = f"{message.author.display_name} said: {translated}"
            await speak_in_guild(message.guild, spoken_as)


@bot.tree.command(name="translate", description="Translate Dutch text to English right now")
@app_commands.describe(text="Dutch text to translate")
async def translate_cmd(interaction: discord.Interaction, text: str) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)
    translated, engine = await translate_nl_en(text)
    embed = translation_embed(text, translated, interaction.user, engine)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="say", description="Translate Dutch and speak English in the voice channel")
@app_commands.describe(text="Dutch text to speak in English")
async def say_cmd(interaction: discord.Interaction, text: str) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    translated, engine = await translate_nl_en(text)
    result = await speak_in_guild(interaction.guild, translated)
    if result == "not-connected":
        await interaction.followup.send(
            "I'm not in a voice channel. Use `/join` first, cutie.",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        embed=translation_embed(text, translated, interaction.user, engine)
    )


@bot.tree.command(name="join", description="Join a voice channel and start speaking translations")
@app_commands.describe(channel="Voice channel (defaults to yours, or VOICE_CHANNEL_ID)")
async def join_cmd(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel | None = None,
) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return

    target = channel
    if target is None and interaction.user.voice:
        target = interaction.user.voice.channel  # type: ignore[assignment]
    if target is None and VOICE_CHANNEL_ID:
        maybe = interaction.guild.get_channel(VOICE_CHANNEL_ID)
        if isinstance(maybe, discord.VoiceChannel):
            target = maybe
    if target is None:
        await interaction.response.send_message(
            "Hop into a voice channel first, or pass one to `/join`.",
            ephemeral=True,
        )
        return

    vc = interaction.guild.voice_client
    if vc and vc.is_connected():
        await vc.move_to(target)
    else:
        await target.connect()
    await interaction.response.send_message(
        f"Joined **{target.name}**. I'll speak English translations there ✨"
    )


@bot.tree.command(name="leave", description="Leave the voice channel")
async def leave_cmd(interaction: discord.Interaction) -> None:
    if not interaction.guild or not interaction.guild.voice_client:
        await interaction.response.send_message("I'm not in voice.", ephemeral=True)
        return
    await interaction.guild.voice_client.disconnect()
    await interaction.response.send_message("Left voice. Bye for now 💕")


@bot.tree.command(name="voice", description="Set the Microsoft neural TTS voice (edge-tts)")
@app_commands.describe(name="e.g. en-US-JennyNeural, en-GB-SoniaNeural, en-US-AriaNeural")
async def voice_cmd(interaction: discord.Interaction, name: str) -> None:
    global EDGE_TTS_VOICE
    EDGE_TTS_VOICE = name.strip()
    await interaction.response.send_message(
        f"Speaking with **{EDGE_TTS_VOICE}** from now on. "
        "This does not persist across restarts — put it in `.env` to keep it.",
        ephemeral=True,
    )


@bot.tree.command(name="status", description="Show translator settings")
async def status_cmd(interaction: discord.Interaction) -> None:
    engine = "DeepL" if DEEPL_API_KEY else "Google (fallback)"
    tts = (
        f"ElevenLabs ({ELEVENLABS_VOICE_ID})"
        if ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID
        else f"edge-tts ({EDGE_TTS_VOICE})"
    )
    msg = (
        f"**Engine:** {engine}\n"
        f"**TTS:** {tts}\n"
        f"**Auto-translate:** {AUTO_TRANSLATE}\n"
        f"**Speak translations:** {SPEAK_TRANSLATIONS}\n"
        f"**Source channels:** {', '.join(map(str, SOURCE_CHANNEL_IDS)) or 'all'}\n"
        f"**Output channel:** {OUTPUT_CHANNEL_ID or 'same channel'}\n"
        f"**English role:** {ENGLISH_ROLE_ID or 'not set'}\n"
    )
    await interaction.response.send_message(msg, ephemeral=True)


def main() -> None:
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in .env")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
