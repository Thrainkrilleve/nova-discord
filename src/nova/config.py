"""Configuration: secrets from .env, everything else from nova.toml (both optional)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class LLMConfig:
    base_url: str = "http://127.0.0.1:8080/v1"
    api_key: str = ""
    model: str = "local"
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 4096  # room for reasoning models to think and still answer
    repeat_penalty: float = 1.05
    timeout: float = 300.0
    # Reasoning models (Qwen3.x, gpt-oss) think before answering: deeper but much slower, and a
    # long think can use up max_tokens and leave an empty reply. false = answer directly.
    thinking: bool = True
    # The model can read images (llama-server --mmproj, or LM Studio with the mmproj file beside
    # the model). Attached images are then passed to it; otherwise Nova says she can't see them.
    vision: bool = False
    # Write her reasoning (the hidden "thinking") to the log after each model turn. It can repeat private
    # messages, so it's off unless you're debugging.
    log_thinking: bool = False


@dataclass
class BotConfig:
    owner_id: int = 0
    dev_guild_id: int = 0
    open_dms: bool = False
    # Write the text of each message she acts on to the log (who/where/length is always logged). Off by
    # default: DMs are private.
    log_messages: bool = False
    history_messages: int = 30
    history_chars: int = 12000
    # Server channels only: forget messages older than this, so yesterday's topic doesn't leak in
    history_minutes: int = 120


@dataclass
class TranslateConfig:
    enabled: bool = True
    nllb_url: str = "http://127.0.0.1:8001/translate"
    polish: bool = True
    # "channels": only where /autotranslate is on; "all": every channel Nova can read; "off"
    auto: str = "channels"


@dataclass
class SearchConfig:
    # Providers in order; ones without settings are skipped (google: NOVA_GOOGLE_API_KEY +
    # NOVA_GOOGLE_CX in .env, brave: NOVA_BRAVE_API_KEY). ddg needs nothing.
    order: str = "searxng,google,brave,ddg"
    searxng_url: str = ""  # e.g. http://127.0.0.1:8888 (services/searxng)
    results: int = 6


@dataclass
class VoiceConfig:
    # Empty URL = that half is off. See scripts/start-voice.ps1 / .sh.
    whisper_url: str = ""   # e.g. http://127.0.0.1:8003 (speech to text)
    tts_url: str = ""       # e.g. http://127.0.0.1:8002 (text to speech)
    tts_voice: str = "alba"


@dataclass
class AAConfig:
    # aa-reporter on your Alliance Auth, e.g. "https://auth.example.com/reporter/"; key in .env as
    # NOVA_AA_TOKEN (Reporter > Api tokens in the AA admin, tied to a recruiter-level account)
    reporter_url: str = ""
    # Channel IDs where recruitment tools work (plus the owner's DMs). Comma-separated.
    channels: str = ""
    # "Our corp": the corporation ID (or ticker) corp lookups use when nobody names one
    home_corp: str = ""


@dataclass
class ServeConfig:
    # `nova serve`, or alongside the bot when enabled: an OpenAI-compatible API in front of Nova, so any
    # chat app (Open WebUI, phone apps, editors) talks to the same Nova, memory and tools as Discord.
    enabled: bool = False
    host: str = "127.0.0.1"  # anything else needs NOVA_API_KEY
    port: int = 8740
    public_url: str = ""     # base for file links in replies; default http://host:port
    # The web app at / (webapp.py): sign in with Discord. The secret is NOVA_DISCORD_CLIENT_SECRET in .env
    web: bool = True
    discord_client_id: str = ""  # the Discord application's ID (the bot's own application works)
    web_users: str = ""          # Discord IDs besides the owner who may sign in, comma-separated


@dataclass
class AdminConfig:
    # Server control (discord_admin.py). Free-post channels are NOVA_FREE_POST_CHANNELS in .env.
    # The only servers (IDs, comma-separated) where Nova may post, pin, set topics or create channels at
    # all; empty = server control is off everywhere. Chatting, AA and the web app don't depend on this.
    servers: str = ""
    # Who may confirm Nova's proposals: Discord role IDs or names, comma-separated
    approver_roles: str = "Root Privileges,Logi Bros"
    # Categories (IDs or names) create_channel may create in; empty = no channel creation
    categories: str = ""
    audit_channel: str = ""   # channel ID every executed action is logged to (the mod log)
    approval_minutes: int = 30  # unconfirmed proposals expire after this
    free_posts_per_10min: int = 6  # per free-post channel, on top of the per-person rate limit


@dataclass
class AutonomyConfig:
    # "Free will": Nova looks in on these channels now and then and may post unprompted. Only
    # free-post channels count; any other ID here is ignored.
    enabled: bool = False
    channels: str = ""   # channel IDs, comma-separated
    interval_minutes: int = 45
    jitter_minutes: int = 15
    max_posts_per_day: int = 3      # per channel, rolling 24 hours
    min_gap_minutes: int = 180      # between two unprompted posts in a channel
    lookback_messages: int = 25
    wake_on_messages: int = 0       # also look in after this many new messages (0 = schedule only)
    mark_posts: bool = True         # a small "unprompted" line under each post


@dataclass
class Config:
    discord_token: str = ""
    llm: LLMConfig = field(default_factory=LLMConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    translate: TranslateConfig = field(default_factory=TranslateConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    aa: AAConfig = field(default_factory=AAConfig)
    aa_token: str = ""
    serve: ServeConfig = field(default_factory=ServeConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)
    autonomy: AutonomyConfig = field(default_factory=AutonomyConfig)
    free_post_channels: set[int] = field(default_factory=set)  # NOVA_FREE_POST_CHANNELS
    api_key: str = ""
    api_users: dict[int, str] = field(default_factory=dict)  # Discord ID -> API key, for people besides the owner
    discord_client_secret: str = ""  # Discord login for the web app
    persona_file: Path | None = None
    data_dir: Path = Path("var")
    sde_path: Path | None = None  # unzipped JSONL export; None means <data_dir>/sde

    @property
    def sde_dir(self) -> Path:
        return self.sde_path or self.data_dir / "sde"


def _apply(target: object, values: dict) -> None:
    """Copy known keys from a TOML table onto a dataclass, coercing to the field's type."""
    for key, value in values.items():
        if hasattr(target, key):
            setattr(target, key, type(getattr(target, key))(value))


def id_list(text: str) -> set[int]:
    """"123, 456" -> {123, 456}; anything that isn't a number is skipped."""
    return {int(c) for c in text.replace(" ", "").split(",") if c.isdigit()}


def load_config(toml_path: str | Path = "nova.toml", env_path: str | Path = ".env") -> Config:
    load_dotenv(env_path)
    cfg = Config()

    path = Path(toml_path)
    if path.exists():
        # utf-8-sig: Windows PowerShell 5.1 and some editors write a BOM, which tomllib rejects
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        _apply(cfg.llm, data.get("llm", {}))
        _apply(cfg.bot, data.get("bot", {}))
        _apply(cfg.translate, data.get("translate", {}))
        _apply(cfg.search, data.get("search", {}))
        _apply(cfg.voice, data.get("voice", {}))
        _apply(cfg.aa, data.get("aa", {}))
        _apply(cfg.serve, data.get("serve", {}))
        _apply(cfg.admin, data.get("admin", {}))
        _apply(cfg.autonomy, data.get("autonomy", {}))
        if persona := data.get("persona", {}).get("file"):
            cfg.persona_file = Path(persona)
        if data_dir := data.get("storage", {}).get("data_dir"):
            cfg.data_dir = Path(data_dir)
        if sde_path := data.get("eve", {}).get("sde_path"):
            cfg.sde_path = Path(sde_path)

    cfg.discord_token = os.getenv("DISCORD_TOKEN", "").strip()
    cfg.llm.base_url = os.getenv("NOVA_LLM_BASE_URL", cfg.llm.base_url).rstrip("/")
    cfg.llm.api_key = os.getenv("NOVA_LLM_API_KEY", cfg.llm.api_key)
    cfg.aa_token = os.getenv("NOVA_AA_TOKEN", "").strip()
    cfg.api_key = os.getenv("NOVA_API_KEY", "").strip()
    cfg.discord_client_secret = os.getenv("NOVA_DISCORD_CLIENT_SECRET", "").strip()
    # NOVA_API_USERS="discord_id:key,discord_id:key": other people's own keys for the chat-app API
    for pair in os.getenv("NOVA_API_USERS", "").split(","):
        user, _, key = pair.strip().partition(":")
        if user.strip().isdigit() and key.strip():
            cfg.api_users[int(user)] = key.strip()
    cfg.free_post_channels = id_list(os.getenv("NOVA_FREE_POST_CHANNELS", ""))
    if owner := os.getenv("NOVA_OWNER_ID", "").strip():
        cfg.bot.owner_id = int(owner)
    return cfg
