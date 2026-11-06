"""Entry point.

    nova run     start the Discord bot (default)
    nova chat    talk to Nova in the terminal, no Discord needed
    nova check   test the connection to the model server
    nova sde     update the EVE static data (download if CCP has a newer build) and its index
    nova translate "text"   translate one message the way Discord would (needs the NLLB service)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .agent import history_to_messages, respond
from .config import Config, load_config
from .eve.update import open_sde, update as update_sde
from .eve.verify import ReplyChecker
from .files import Workspace
from .inbound import Attachment
from .llm import LLMClient, LLMError
from .memory import Memory
from .wiring import make_aa, make_inbound, make_searcher, make_speaker
from .prompt import build_system_prompt, continuity_note, load_persona
from .tools import ToolContext, builtin_tools
from .translate.pipeline import TranslationError, Translator

TERMINAL_CHANNEL = 0  # conversation log key for `nova chat`
TERMINAL_NAME = "Owner"  # "you" as a speaker name made the model mix itself up with the user


def owner_identity(cfg: Config, memory: Memory) -> tuple[int, str]:
    """The terminal and chat apps talk as the owner, so Nova's facts and recall are shared with Discord.
    The name is whatever the owner last went by on Discord ("you" confused the model)."""
    user_id = cfg.bot.owner_id or 0
    return user_id, memory.name_of(user_id) or TERMINAL_NAME


async def check(cfg: Config) -> int:
    llm = LLMClient(cfg.llm)
    try:
        print(f"Model server {cfg.llm.base_url}: models {await llm.model_names()}")
        turn = await llm.stream_turn([{"role": "user", "content": "Reply with just the word: pong"}])
        print(f"Test reply: {turn.content!r}")
        return 0
    except LLMError as e:
        print(f"FAILED: {e}")
        return 1
    finally:
        await llm.close()


async def chat(cfg: Config) -> int:
    memory = Memory(cfg.data_dir / "nova.db")
    sde = open_sde(cfg.sde_dir, cfg.data_dir / "sde.db")
    llm, tools = LLMClient(cfg.llm), builtin_tools(sde, make_searcher(cfg), make_speaker(cfg), make_aa(cfg))
    reply_checker = ReplyChecker(sde) if sde is not None else None
    persona = load_persona(cfg.persona_file)
    inbound = make_inbound(cfg, sde)
    workspace = Workspace(cfg.data_dir / "files" / str(TERMINAL_CHANNEL))
    user_id, user_name = owner_identity(cfg, memory)
    memory.set_place(TERMINAL_CHANNEL, "the terminal (nova chat)", private=True)
    pending: list[Attachment] = []
    print("Chatting with Nova. /file <path> attaches a file, /reset clears history, /quit exits.\n")
    try:
        while True:
            try:
                text = (await asyncio.to_thread(input, "you> ")).strip()
            except EOFError:
                break
            if text in ("/quit", "/exit"):
                break
            if text == "/reset":
                memory.reset_channel(TERMINAL_CHANNEL)
                print("(history cleared)\n")
                continue
            if text.startswith("/file "):
                path = Path(text[6:].strip().strip('"'))
                if path.is_file():
                    pending.append(Attachment(path.name, path.read_bytes()))
                    print(f"(attached {path.name}; it goes with your next message)\n")
                else:
                    print(f"(no such file: {path})\n")
                continue
            if not text:
                continue
            ctx = ToolContext(user_id, user_name, TERMINAL_CHANNEL, memory, workspace=workspace, private=True, is_owner=True,
                              owner_name=user_name)
            prepared = await inbound.prepare(text, pending, workspace)
            pending = []
            memory.add_message(TERMINAL_CHANNEL, "user", prepared.text, user_id, user_name)
            history = memory.recent(TERMINAL_CHANNEL, cfg.bot.history_messages, cfg.bot.history_chars)
            system = (build_system_prompt(persona, memory, user_id, user_name, "a terminal chat with your owner")
                      + continuity_note(memory, user_id, TERMINAL_CHANNEL) + prepared.context)
            messages = [{"role": "system", "content": system}, *history_to_messages(history, group_chat=False)]
            if prepared.images:
                messages[-1]["content"] = prepared.user_content()
            text = prepared.text

            printed = ""

            def emit(full: str) -> None:
                # Text normally only grows; a new model turn after a tool call starts over.
                nonlocal printed
                if full.startswith(printed):
                    print(full[len(printed):], end="", flush=True)
                else:
                    print("\nnova> " + full, end="", flush=True)
                printed = full

            async def show(full: str) -> None:
                emit(full)

            print("nova> ", end="", flush=True)
            try:
                answer = await respond(llm, tools, messages, ctx, on_text=show,
                                       verifier=reply_checker.for_question(text) if reply_checker else None)
            except LLMError as e:
                print(f"\n[error: {e}]\n")
                continue
            emit(answer)
            print("\n")
            for path in ctx.outputs:
                print(f"(wrote {path})")
            memory.add_message(TERMINAL_CHANNEL, "assistant", answer, None, "Nova")
    finally:
        await llm.close()
        memory.close()
        if sde is not None:
            sde.close()
    return 0


def sde_command(cfg: Config) -> int:
    try:
        print(update_sde(cfg.sde_dir, cfg.data_dir / "sde.db"))
    except OSError as e:
        print(f"SDE update failed: {e}")
        return 1
    return 0


def make_translator(cfg: Config, memory: Memory, llm: LLMClient) -> Translator | None:
    if not cfg.translate.enabled:
        return None
    return Translator(cfg.translate.nllb_url, memory, llm, polish=cfg.translate.polish)


async def translate_once(cfg: Config, text: str) -> int:
    memory = Memory(cfg.data_dir / "nova.db")
    llm = LLMClient(cfg.llm)
    translator = make_translator(cfg, memory, llm)
    try:
        if translator is None:
            print("Translation is disabled in nova.toml.")
            return 1
        print(await translator.translate(text))
        return 0
    except TranslationError as e:
        print(f"FAILED: {e}")
        return 1
    finally:
        await llm.close()
        memory.close()


def run_bot(cfg: Config) -> int:
    from .bot import NovaBot  # imported here so `nova chat` works without a Discord token

    if not cfg.discord_token:
        print("DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token.")
        return 1
    memory = Memory(cfg.data_dir / "nova.db")
    sde = open_sde(cfg.sde_dir, cfg.data_dir / "sde.db")
    llm = LLMClient(cfg.llm)
    tools = builtin_tools(sde, make_searcher(cfg), make_speaker(cfg), make_aa(cfg))
    inbound = make_inbound(cfg, sde)
    server = None
    if cfg.serve.enabled:
        from .serve import NovaServer

        server = NovaServer(cfg, memory, llm, tools, sde, inbound)
        server.check_safe()
    bot = NovaBot(cfg, memory, llm, tools, sde, make_translator(cfg, memory, llm), inbound=inbound, server=server)
    try:
        bot.run(cfg.discord_token, log_handler=None)
    finally:
        memory.close()
        if sde is not None:
            sde.close()
    return 0


async def serve(cfg: Config) -> int:
    """Only the chat-app API, without Discord (when the bot runs elsewhere or isn't needed)."""
    from .serve import NovaServer

    memory = Memory(cfg.data_dir / "nova.db")
    sde = open_sde(cfg.sde_dir, cfg.data_dir / "sde.db")
    llm = LLMClient(cfg.llm)
    server = NovaServer(cfg, memory, llm, builtin_tools(sde, make_searcher(cfg), make_speaker(cfg), make_aa(cfg)),
                        sde, make_inbound(cfg, sde))
    server.check_safe()
    await llm.start()
    await server.start()
    try:
        await asyncio.Event().wait()  # until Ctrl-C
    finally:
        await server.stop()
        await llm.close()
        memory.close()
        if sde is not None:
            sde.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="nova", description="Nova Discord assistant")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "chat", "check", "sde", "translate", "serve"])
    parser.add_argument("text", nargs="?", default="", help="text for `nova translate`")
    parser.add_argument("--config", default="nova.toml", help="path to nova.toml (optional)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "chat":
        logging.getLogger().setLevel(logging.WARNING)
        logging.getLogger("nova.agent").setLevel(logging.INFO)  # still show tool calls
    cfg = load_config(args.config)

    if args.command == "check":
        sys.exit(asyncio.run(check(cfg)))
    if args.command == "chat":
        sys.exit(asyncio.run(chat(cfg)))
    if args.command == "sde":
        sys.exit(sde_command(cfg))
    if args.command == "serve":
        try:
            sys.exit(asyncio.run(serve(cfg)))
        except KeyboardInterrupt:
            sys.exit(0)
    if args.command == "translate":
        sys.exit(asyncio.run(translate_once(cfg, args.text)))
    sys.exit(run_bot(cfg))


if __name__ == "__main__":
    main()
