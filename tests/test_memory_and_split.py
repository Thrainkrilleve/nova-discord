from datetime import datetime, timezone

from nova.discord_reply import split_message
from nova.prompt import DEFAULT_PERSONA, build_system_prompt


def test_reset_hides_history_but_keeps_the_log(memory):
    memory.add_message(1, "user", "old", 5, "A")
    memory.reset_channel(1)
    memory.add_message(1, "user", "new", 5, "A")
    assert [m.content for m in memory.recent(1)] == ["new"]
    assert memory.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_recent_is_oldest_first_and_respects_budget(memory):
    for i in range(5):
        memory.add_message(1, "user", f"m{i}" + "x" * 98)
    picked = memory.recent(1, limit=10, char_budget=250)
    assert [m.content[:2] for m in picked] == ["m3", "m4"]


def test_channels_are_independent(memory):
    memory.add_message(1, "user", "one")
    memory.add_message(2, "user", "two")
    memory.set_listening(2, True)
    assert [m.content for m in memory.recent(1)] == ["one"]
    assert memory.is_listening(2) and not memory.is_listening(1)


def test_facts_dedupe_and_forget(memory):
    assert memory.add_fact(7, "Flies a Rifter")
    assert not memory.add_fact(7, "flies a rifter")
    memory.update_profile(7, timezone="Europe/London")
    assert memory.forget_user(7) == 1
    assert memory.facts(7) == [] and memory.profile(7).timezone is None


def test_system_prompt_includes_profile_and_facts(memory):
    memory.update_profile(7, timezone="America/New_York", character_name="Ava Stone")
    memory.add_fact(7, "Runs an AzerothCore server")
    now = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
    prompt = build_system_prompt(DEFAULT_PERSONA, memory, 7, "Ava", "#general", now)
    assert "14:00 (America/New_York)" in prompt
    assert "Today in EVE time (UTC): Thursday 15 October 2026, 18:00" in prompt
    assert "Ava Stone" in prompt and "AzerothCore" in prompt


def test_split_short_text_is_one_chunk():
    assert split_message("hello") == ["hello"]


def test_split_respects_limit_and_reopens_code_fences():
    code = "```python\n" + "\n".join(f"x{i} = {i}" for i in range(60)) + "\n```"
    chunks = split_message("Here you go:\n" + code, limit=200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    for c in chunks:
        assert c.count("```") % 2 == 0, c  # every chunk renders as balanced markdown
    assert chunks[1].startswith("```python")


def test_split_hard_wraps_a_giant_line():
    chunks = split_message("a" * 5000, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks) == "a" * 5000


def test_recent_can_drop_stale_messages(memory):
    memory.add_message(1, "user", "old topic")
    memory.db.execute("UPDATE messages SET created_at = '2000-01-01T00:00:00+00:00'")
    memory.add_message(1, "user", "new question")
    assert [m.content for m in memory.recent(1, max_age_minutes=120)] == ["new question"]
    assert len(memory.recent(1)) == 2
