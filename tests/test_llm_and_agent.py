from nova.agent import history_to_messages, respond
from nova.llm import strip_thinking
from nova.memory import StoredMessage
from nova.tools import ToolContext, builtin_tools


def test_strip_thinking_removes_closed_and_unclosed_blocks():
    assert strip_thinking("<think>plan</think>\nHello") == "Hello"
    assert strip_thinking("<think>still going") == ""
    assert strip_thinking("no tags") == "no tags"


async def test_stream_turn_hides_thinking_and_reports_progress(fake_llm):
    fake, llm = fake_llm
    fake.reply({"content": "<think>hmm"}, {"content": "</think>"}, {"content": "Hel"}, {"content": "lo"})
    seen = []

    async def on_text(text):
        seen.append(text)

    turn = await llm.stream_turn([{"role": "user", "content": "hi"}], on_text=on_text)
    assert turn.content == "Hello"
    assert seen[-1] == "Hello"
    assert all("think" not in s for s in seen)
    assert fake.requests[0]["repeat_penalty"] == 1.05


async def test_model_names(fake_llm):
    _, llm = fake_llm
    assert await llm.model_names() == ["fake-model"]


async def test_respond_runs_tool_then_answers(fake_llm, memory):
    fake, llm = fake_llm
    # Turn 1: the model asks to remember something, arguments streamed in two pieces
    fake.reply(
        {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "remember", "arguments": '{"fact": '}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": '"Likes Gallente"}'}}]},
    )
    # Turn 2: final answer after seeing the tool result
    fake.reply({"content": "Noted."})

    ctx = ToolContext(user_id=42, user_name="Ava", channel_id=1, memory=memory)
    messages = [{"role": "user", "content": "I like Gallente ships"}]
    answer = await respond(llm, builtin_tools(), messages, ctx)

    assert answer == "Noted."
    assert memory.facts(42) == ["Likes Gallente"]
    second = fake.requests[1]["messages"]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "remember"
    assert second[-1] == {"role": "tool", "tool_call_id": "c1", "content": "Saved: Likes Gallente"}
    assert "tools" in fake.requests[0]


async def test_unknown_tool_is_reported_not_raised(memory):
    ctx = ToolContext(1, "x", 1, memory)
    result = await builtin_tools().run("launch_doomsday", "{}", ctx)
    assert "no tool" in result


def test_group_history_is_prefixed_with_names():
    history = [
        StoredMessage("user", 1, "Alice", "hi"),
        StoredMessage("assistant", 9, "Nova", "hey"),
    ]
    assert history_to_messages(history, group_chat=True) == [
        {"role": "user", "content": "Alice: hi"},
        {"role": "assistant", "content": "hey"},
    ]
    assert history_to_messages(history, group_chat=False)[0]["content"] == "hi"


def test_no_reply_marker_detection():
    from nova.agent import is_no_reply, may_become_no_reply
    for text in ("[no reply]", "[No reply]", "NO_REPLY", " [no reply]. "):
        assert is_no_reply(text)
    assert not is_no_reply("No reply needed, but nice fight")
    assert may_become_no_reply("") and may_become_no_reply("[no") and may_become_no_reply("[no reply]")
    assert not may_become_no_reply("Nice") and not may_become_no_reply("[note]")


def test_optional_reply_section_only_when_listening(memory):
    from nova.prompt import DEFAULT_PERSONA, build_system_prompt
    quiet = build_system_prompt(DEFAULT_PERSONA, memory, 1, "A", "#general", optional_reply=True)
    loud = build_system_prompt(DEFAULT_PERSONA, memory, 1, "A", "#general")
    assert "[no reply]" in quiet and "You may stay quiet" in quiet
    assert "You may stay quiet" not in loud
    assert "web_search to confirm" in loud


async def test_empty_reply_is_retried_without_thinking(fake_llm, memory):
    fake, llm = fake_llm
    fake.reply({"content": "<think>pondering forever"})  # budget ran out mid-think: nothing visible
    fake.reply({"content": "Short answer."})
    answer = await respond(llm, builtin_tools(), [{"role": "user", "content": "hi"}], ToolContext(1, "x", 1, memory))
    assert answer == "Short answer."
    assert fake.requests[1]["chat_template_kwargs"]["enable_thinking"] is False


async def test_never_an_empty_reply(fake_llm, memory):
    from nova.agent import EMPTY_FALLBACK

    fake, llm = fake_llm
    fake.reply({"content": "<think>pondering forever"})
    fake.reply({"content": ""})  # the retry comes back empty too (context too small under load)
    answer = await respond(llm, builtin_tools(), [{"role": "user", "content": "hi"}], ToolContext(1, "x", 1, memory))
    assert answer == EMPTY_FALLBACK


def test_textual_tool_calls_never_reach_chat():
    text = "Here goes.\n<tool_call>\n<function=eve_find_items>\n<parameter=keywords>x</parameter>\n</function>\n</tool_call>\nDone."
    assert strip_thinking(text) == "Here goes.\nDone."
    assert strip_thinking("<tool_call>\n<function=eve_find_items>") == ""


async def test_running_out_of_tool_rounds_forces_a_written_answer(fake_llm, memory):
    fake, llm = fake_llm
    from nova.agent import MAX_TOOL_ROUNDS
    for i in range(MAX_TOOL_ROUNDS):
        fake.reply({"tool_calls": [{"index": 0, "id": f"c{i}", "function": {"name": "get_time", "arguments": "{}"}}]})
    fake.reply({"content": "It's late."})
    answer = await respond(llm, builtin_tools(), [{"role": "user", "content": "time?"}], ToolContext(1, "x", 1, memory))
    assert answer == "It's late."
    last = fake.requests[-1]
    assert "tools" not in last and "No more lookups" in last["messages"][-1]["content"]


async def test_one_round_cannot_run_dozens_of_tools(fake_llm, memory):
    from nova.agent import MAX_TOOL_CALLS, TOO_MANY_CALLS

    fake, llm = fake_llm
    fake.reply(*[{"tool_calls": [{"index": i, "id": f"c{i}", "function": {"name": "get_time", "arguments": "{}"}}]}
                 for i in range(25)])
    fake.reply({"content": "Done."})
    tools = builtin_tools()
    ran = []
    original = tools.run

    async def counting(name, arguments, ctx):
        ran.append(name)
        return await original(name, arguments, ctx)

    tools.run = counting
    answer = await respond(llm, tools, [{"role": "user", "content": "time x25"}], ToolContext(1, "x", 1, memory))
    assert answer == "Done." and len(ran) == MAX_TOOL_CALLS
    results = [m for m in fake.requests[1]["messages"] if m["role"] == "tool"]
    assert len(results) == 25   # every call answered, the extras with a refusal
    assert sum(m["content"] == TOO_MANY_CALLS for m in results) == 25 - MAX_TOOL_CALLS


async def test_thinking_reaches_the_log_only_when_asked(fake_llm, caplog):
    import logging

    fake, llm = fake_llm
    caplog.set_level(logging.INFO, logger="nova.llm")
    fake.reply({"reasoning_content": "They want the time; "}, {"reasoning_content": "use get_time."}, {"content": "Hi."})
    turn = await llm.stream_turn([{"role": "user", "content": "hi"}])
    assert turn.content == "Hi." and "thinking" not in caplog.text            # off by default
    llm.cfg.log_thinking = True
    fake.reply({"reasoning_content": "They want the time; use get_time."}, {"content": "Hi."})
    fake.reply({"content": "<think>inline reasoning here</think>Hello."})
    assert (await llm.stream_turn([{"role": "user", "content": "hi"}])).content == "Hi."
    assert (await llm.stream_turn([{"role": "user", "content": "hi"}])).content == "Hello."
    assert "thinking (33 chars): They want the time; use get_time." in caplog.text
    assert "inline reasoning here" in caplog.text


class _OverflowingLLM:
    """Says the context is full while any tool result is long, like llama-server does."""

    def __init__(self, ever_fits=True):
        self.ever_fits, self.calls = ever_fits, 0

    async def stream_turn(self, messages, tools=None, on_text=None, temperature=None, quick=False):
        from nova.llm import LLMError, ToolCall, Turn
        self.calls += 1
        if self.calls == 1:
            return Turn(content="", tool_calls=[ToolCall(id="c1", name="big", arguments="{}")])
        if not self.ever_fits or any(m.get("role") == "tool" and len(m["content"]) > 2000 for m in messages):
            raise LLMError('model server returned 400: {"error":{"type":"exceed_context_size_error"}}')
        return Turn(content="Here's the short version.")


def _big_tool_registry():
    from nova.tools import ToolRegistry
    reg = ToolRegistry()

    @reg.tool("big", "returns a lot", {"type": "object", "properties": {}})
    async def big(ctx):
        return "x" * 5000
    return reg


async def test_a_full_context_trims_tool_results_and_retries(memory):
    llm = _OverflowingLLM()
    answer = await respond(llm, _big_tool_registry(), [{"role": "user", "content": "everything"}],
                           ToolContext(1, "x", 1, memory))
    assert answer == "Here's the short version."


async def test_a_context_that_never_fits_asks_to_narrow_it_down(memory):
    from nova.agent import CONTEXT_FULL
    answer = await respond(_OverflowingLLM(ever_fits=False), _big_tool_registry(),
                           [{"role": "user", "content": "everything"}], ToolContext(1, "x", 1, memory))
    assert answer == CONTEXT_FULL


async def test_the_fit_checker_leaves_auth_data_alone(fake_llm, memory):
    fake, llm = fake_llm
    fake.reply({"content": "Hound with a Capital Remote Repair Augmentor I in the hangar."})
    ctx = ToolContext(1, "x", 1, memory)
    ctx.source_texts.append("assets from auth")
    answer = await respond(llm, builtin_tools(), [{"role": "user", "content": "my assets"}], ctx,
                           verifier=lambda text: ["can't fit that"])
    assert answer == "Hound with a Capital Remote Repair Augmentor I in the hangar." and len(fake.requests) == 1


async def test_an_empty_reply_after_long_lookups_is_retried_with_them_shortened(fake_llm, memory):
    from nova.tools import ToolRegistry

    fake, llm = fake_llm
    reg = ToolRegistry()

    @reg.tool("big_lookup", "returns a lot", {"type": "object", "properties": {}})
    async def big_lookup(ctx):
        return "row\n" * 2000

    fake.reply({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "big_lookup", "arguments": "{}"}}]})
    fake.reply({"content": "<think>too much to read"})
    fake.reply({"content": ""})                      # the quick retry is empty too: the context is full
    fake.reply({"content": "Here's the short version."})
    answer = await respond(llm, reg, [{"role": "user", "content": "corp history?"}], ToolContext(1, "x", 1, memory))
    assert answer == "Here's the short version."
    last = fake.requests[-1]["messages"]
    assert len(next(m for m in last if m["role"] == "tool")["content"]) < 2000      # trimmed before the last try
