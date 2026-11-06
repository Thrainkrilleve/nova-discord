"""Score whatever model the configured llama-server is running on Nova's real jobs.

    python evals/run_eval.py --name gpt-oss-20b [--config nova.toml] [--repeat 2]

Each case uses Nova's own prompt, tools and SDE data, and passes or fails on something
checkable: a tool was called, a fact appears, generated code passes tests. Results are printed
and saved to evals/results/<name>.json for comparison between models.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nova.agent import history_to_messages, is_no_reply, respond  # noqa: E402
from nova.config import load_config  # noqa: E402
from nova.eve.update import open_sde  # noqa: E402
from nova.eve.verify import ReplyChecker  # noqa: E402
from nova.llm import LLMClient  # noqa: E402
from nova.memory import Memory  # noqa: E402
from nova.prompt import build_system_prompt, load_persona  # noqa: E402
from nova.tools import ToolContext, ToolRegistry, builtin_tools  # noqa: E402
from nova.translate.pipeline import REFUSAL_MARKERS, Translator  # noqa: E402

CHANNEL = 100
BLOODBATH = (
    "在《星战前夜》（EVE Online）的宇宙中，新伊甸园（New Eden）星系昨夜爆发了一场史无前例的旗舰级对决。"
    "由于一个主权归属的账单疏忽，敌对的两大玩家联盟在名为 B-R5RB 的星系瞬间集结。战火绵延了整整21个小时。"
    "这场战役最终摧毁了价值数万亿星币（ISK）的物资，折合现实货币高达数十万美元。"
)
CODING_TESTS = """
assert merge_intervals([[1, 3], [2, 6], [8, 10], [15, 18]]) == [[1, 6], [8, 10], [15, 18]]
assert merge_intervals([[1, 4], [4, 5]]) == [[1, 5]]
assert merge_intervals([[5, 7], [1, 2]]) == [[1, 2], [5, 7]]
assert merge_intervals([]) == []
assert merge_intervals([[1, 10], [2, 3]]) == [[1, 10]]
print("ok")
"""


@dataclass
class Result:
    case: str
    passed: bool
    seconds: float
    detail: str


def is_prose(answer: str) -> bool:
    """A real reply: not empty and no tool-call scaffolding leaking into chat."""
    return len(answer.strip()) > 20 and "<tool_call>" not in answer and "<function=" not in answer


class RecordingTools(ToolRegistry):
    """Wraps the real tools and remembers which ones the model called."""

    def __init__(self, inner: ToolRegistry):
        super().__init__()
        self._tools = inner._tools
        self.called: list[str] = []

    async def run(self, name, arguments, ctx):
        self.called.append(name)
        return await super().run(name, arguments, ctx)


class Harness:
    def __init__(self, cfg):
        self.cfg = cfg
        self.llm = LLMClient(cfg.llm)
        self.persona = load_persona(None)
        self.sde = open_sde(cfg.sde_dir, cfg.data_dir / "sde.db")
        self.reply_checker = ReplyChecker(self.sde) if self.sde is not None else None

    async def ask(self, text: str, who: str = "Ava", optional: bool = False,
                  history: list[tuple[str, str]] = (), group: bool = True):
        memory = Memory(":memory:")
        for author, line in history:
            memory.add_message(CHANNEL, "user", line, 1, author)
        memory.add_message(CHANNEL, "user", text, 2, who)
        system = build_system_prompt(self.persona, memory, 2, who, "#fleet-chat in ROSE", optional_reply=optional)
        if self.sde is not None:
            system += self.sde.context_for(text)
        messages = [{"role": "system", "content": system},
                    *history_to_messages(memory.recent(CHANNEL), group_chat=group)]
        tools = RecordingTools(builtin_tools(self.sde))
        answer = await respond(self.llm, tools, messages, ToolContext(2, who, CHANNEL, memory),
                               verifier=self.reply_checker.for_question(text) if self.reply_checker else None)
        return answer, tools.called, memory


async def case_quiet(h: Harness, text: str, history=()):
    answer, _, _ = await h.ask(text, who="Bob", optional=True, history=history)
    return is_no_reply(answer), answer


async def run_cases(h: Harness) -> list[tuple[str, callable]]:
    async def quiet_gf():
        return await case_quiet(h, "gf everyone, that was a fun roam")

    async def quiet_brb():
        return await case_quiet(h, "brb dinner")

    async def quiet_banter():
        return await case_quiet(h, "yeah 5 min, grabbing my rifter", history=[("Alice", "you coming on the roam?")])

    async def speak_to_room():
        answer, _, _ = await h.ask("anyone know how many low slots a Rifter has?", who="Bob", optional=True)
        return (not is_no_reply(answer)) and re.search(r"\b(4|four)\b", answer) is not None, answer

    async def history_lookup():
        answer, called, _ = await h.ask(BLOODBATH, optional=True)
        return "web_search" in called and "2014" in answer, f"tools={called} | {answer}"

    async def ship_data():
        answer, _, _ = await h.ask("Is the Ferox a missile boat?")
        lower = answer.lower()
        # Correct: it is a turret (hybrid gun) battlecruiser with no launcher hardpoints
        return any(k in lower for k in ("turret", "hybrid", "gun")) and not lower.startswith("yes"), answer

    async def no_invented_fit():
        # Every item-like name in the reply must exist; a fit flagged as unverified still fails
        question = "give me a full ratting fit for a Ferox, module by module"
        answer, called, _ = await h.ask(question)
        issues = h.reply_checker.issues(question, answer)
        ok = is_prose(answer) and not issues and "Couldn't verify" not in answer
        return ok, f"tools={len(called)} issues={issues} | {answer}"

    async def real_pvp_fit():
        question = "what should I fit on a Rifter for solo PvP? name the modules"
        answer, called, _ = await h.ask(question)
        fit = h.reply_checker.fits.parse(answer)
        issues = h.reply_checker.issues(question, answer)
        ok = is_prose(answer) and len(fit) >= 4 and not issues and "Couldn't verify" not in answer
        return ok, f"tools={len(called)} modules={len(fit)} issues={issues} | {answer}"

    async def tool_time():
        answer, called, _ = await h.ask("what time is it in Tokyo right now?", group=False)
        return "get_time" in called, f"tools={called} | {answer}"

    async def tool_remember():
        answer, called, memory = await h.ask("I main a Gallente pilot called Ava Stone, remember that", group=False)
        facts = memory.facts(2)
        return "remember" in called and any("ava" in f.lower() for f in facts), f"facts={facts} | {answer}"

    async def coding():
        answer, _, _ = await h.ask(
            "Write a Python function merge_intervals(intervals) that merges overlapping [start, end] "
            "intervals (lists) and returns them sorted by start. Just the code.", group=False)
        block = re.search(r"```(?:python|py)?\n(.*?)```", answer, re.DOTALL)
        if not block:
            return False, "no code block"
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "solution.py"
            script.write_text(block[1] + "\n" + CODING_TESTS, encoding="utf-8")
            try:
                proc = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True,
                                      timeout=15, cwd=tmp)
            except subprocess.TimeoutExpired:
                return False, "timed out"
        return proc.stdout.strip().endswith("ok"), (proc.stderr or proc.stdout)[-400:]

    translator = Translator("http://unused", Memory(":memory:"), h.llm)

    async def polish_numbers():
        out = await translator.polish("折合现实货币高达数十万美元",
                                      "equivalent to hundreds of thousands of US dollars in real currency")
        lower = out.lower()
        return ("hundreds of thousands" in lower or "hundred thousand" in lower) and "million" not in lower, out

    async def polish_profanity():
        out = await translator.polish("иди нахуй, ты тупой нуб", "Fuck you, you stupid noob.")
        lower = out.lower()
        return "fuck" in lower and "noob" in lower and not any(m in lower for m in REFUSAL_MARKERS), out

    async def uncensored():
        answer, _, _ = await h.ask(
            "Write a brutal, profane two-line roast of a guy who lost his titan to an obvious cyno bait.", group=False)
        lower = answer.lower()
        return not any(m in lower for m in REFUSAL_MARKERS) and len(answer) > 30, answer

    async def discord_format():
        answer, _, _ = await h.ask("Compare the Rifter and the Slasher for me.", group=False)
        return not re.search(r"^\s*\|.*\|\s*$", answer, re.MULTILINE), answer

    async def language_match():
        answer, _, _ = await h.ask("Привет! Как дела, что посоветуешь новичку в EVE?", group=False)
        return re.search(r"[Ѐ-ӿ]{4,}", answer) is not None, answer

    return [
        ("quiet: gf", quiet_gf), ("quiet: brb", quiet_brb), ("quiet: banter", quiet_banter),
        ("speak: question to room", speak_to_room), ("history: Bloodbath lookup", history_lookup),
        ("eve: Ferox weapons from SDE", ship_data), ("eve: no invented fit", no_invented_fit),
        ("eve: Rifter fit is valid", real_pvp_fit),
        ("tool: get_time", tool_time), ("tool: remember", tool_remember), ("coding: runs + passes", coding),
        ("translate: numbers kept", polish_numbers), ("translate: profanity kept", polish_profanity),
        ("uncensored roast", uncensored), ("discord: no tables", discord_format),
        ("reply in speaker's language", language_match),
    ]


async def measure_speed(h: Harness) -> float:
    start = time.monotonic()
    turn = await h.llm.stream_turn(
        [{"role": "user", "content": "Write about 250 words describing Jita 4-4 to a new player."}], quick=True)
    return round(len(turn.content) / 4 / (time.monotonic() - start), 1)  # rough tokens/s


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--config", default=str(ROOT / "nova.toml"))
    parser.add_argument("--repeat", type=int, default=1, help="run every case N times (models are random)")
    parser.add_argument("--only", default="", help="run only cases whose name contains this text")
    args = parser.parse_args()

    h = Harness(load_config(args.config))
    results: list[Result] = []
    for name, fn in await run_cases(h):
        if args.only and args.only.lower() not in name.lower():
            continue
        for _ in range(args.repeat):
            start = time.monotonic()
            try:
                passed, detail = await fn()
            except Exception as e:  # a crashing case is a failed case
                passed, detail = False, f"error: {e}"
            results.append(Result(name, passed, round(time.monotonic() - start, 1), detail.strip()[:600]))
            print(f"{'PASS' if passed else 'FAIL'}  {results[-1].seconds:5.1f}s  {name}", flush=True)
    speed = await measure_speed(h)
    await h.llm.close()

    passed = sum(r.passed for r in results)
    print(f"\n{args.name}: {passed}/{len(results)} passed, ~{speed} tokens/s")
    out = ROOT / "evals" / "results" / f"{args.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"model": args.name, "passed": passed, "total": len(results),
                               "tokens_per_s": speed, "results": [asdict(r) for r in results]},
                              ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
