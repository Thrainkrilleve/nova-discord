"""Assemble the optional services (EVE data, vision, voice, ...) from config, for bot and terminal chat alike."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .aa import AAAccess, ReporterClient
from .config import Config
from .inbound import Inbound
from .search import Searcher
from .voice import Speaker, Transcriber

if TYPE_CHECKING:
    from .eve.sde import SDE


def make_searcher(cfg: Config) -> Searcher:
    order = [p.strip().lower() for p in cfg.search.order.split(",") if p.strip()]
    return Searcher(order, cfg.search.searxng_url, cfg.search.results)


def make_aa(cfg: Config) -> AAAccess | None:
    if not (cfg.aa.reporter_url and cfg.aa_token):
        return None
    channels = {int(c) for c in cfg.aa.channels.replace(" ", "").split(",") if c.isdigit()}
    return AAAccess(ReporterClient(cfg.aa.reporter_url, cfg.aa_token), channels, cfg.bot.owner_id,
                    home_corp=cfg.aa.home_corp)


def make_speaker(cfg: Config) -> Speaker | None:
    return Speaker(cfg.voice.tts_url, cfg.voice.tts_voice) if cfg.voice.tts_url else None


def make_inbound(cfg: Config, sde: "SDE | None") -> Inbound:
    from . import fleet
    from .translate.glossary import slang_context

    from .eve.pilot import PilotStats

    providers = [fleet.context_for, slang_context, PilotStats().context_for]
    if sde is not None:
        from .eve.fits import FitValidator
        from .eve.killmail import KillmailReader
        from .eve.paste import PasteReader

        validator = FitValidator(sde)

        async def sde_context(text: str) -> str:
            return sde.context_for(text)

        # Order matters only for readability of the prompt: items, then pasted fits, then killmails
        providers += [sde_context, PasteReader(validator).context_for, KillmailReader(validator).context_for]
    transcriber = Transcriber(cfg.voice.whisper_url) if cfg.voice.whisper_url else None
    return Inbound(vision=cfg.llm.vision, transcriber=transcriber, context_providers=providers)
