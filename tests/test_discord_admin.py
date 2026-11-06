"""discord_admin: free-post channels act at once, everything else waits for an approver's click."""

import itertools
from types import SimpleNamespace

import pytest

from nova.config import Config
from nova.discord_admin import ApprovalView, Proposal, ServerControl, add_admin_tools, channel_slug, is_approver
from nova.security import TRIPWIRES
from nova.tools import ToolContext, ToolRegistry

_ids = itertools.count(5000)
NOVA = SimpleNamespace(id=1, display_name="Nova", name="nova", bot=True)
FREE, LOCKED, NOTIFY = 101, 102, 103


class Perms(SimpleNamespace):
    view_channel = True
    send_messages = True


class Message:
    def __init__(self, channel, author, content):
        self.id, self.channel, self.author, self.content = next(_ids), channel, author, content
        self.pinned = False
        self.jump_url = f"https://discord.com/channels/{channel.guild.id}/{channel.id}/{self.id}"

    async def edit(self, content=None, suppress=None, view=None):
        self.content = content

    async def pin(self, reason=None):
        self.pinned = True

    async def unpin(self, reason=None):
        self.pinned = False


class Channel:
    def __init__(self, guild, cid, name, topic=""):
        self.guild, self.id, self.name, self.topic = guild, cid, name, topic
        self.sent: list[Message] = []
        self.views: list = []
        self.hidden_from: set[int] = set()
        self.muted: set[int] = set()

    def permissions_for(self, member):
        return Perms(view_channel=member.id not in self.hidden_from, send_messages=member.id not in self.muted)

    async def send(self, content, suppress_embeds=False, view=None):
        msg = Message(self, NOVA, content)
        self.sent.append(msg)
        self.views.append(view)
        return msg

    async def edit(self, topic=None, reason=None):
        self.topic = topic

    async def fetch_message(self, mid):
        return next(m for m in self.sent if m.id == mid)

    async def history(self, limit=50):
        for m in reversed(self.sent[-limit:]):
            yield m


class Category(SimpleNamespace):
    def permissions_for(self, member):
        return Perms()


class Guild:
    def __init__(self):
        self.id, self.name = 900, "Nova Support"
        self.members: dict[int, SimpleNamespace] = {}
        self.text_channels = [Channel(self, FREE, "nova-chat"), Channel(self, LOCKED, "announcements"),
                              Channel(self, NOTIFY, "server-notifications")]
        self.categories = [Category(id=200, name="Projects"), Category(id=201, name="Staff")]
        self.created: list[tuple] = []

    def get_member(self, uid):
        return self.members.get(uid)

    def get_channel(self, cid):
        return next((c for c in [*self.text_channels, *self.categories] if c.id == cid), None)

    async def create_text_channel(self, name, category=None, topic=None, reason=None):
        made = Channel(self, next(_ids), name, topic or "")
        self.text_channels.append(made)
        self.created.append((name, category.id, topic, reason))
        return made


def member(guild, uid, name, *roles):
    m = SimpleNamespace(id=uid, display_name=name, name=name.lower(), bot=False, guild=guild,
                        roles=[SimpleNamespace(id=700 + i, name=r) for i, r in enumerate(roles)])
    guild.members[uid] = m
    return m


@pytest.fixture
def server(memory):
    guild = Guild()
    cfg = Config(free_post_channels={FREE})
    cfg.admin.audit_channel = str(NOTIFY)
    cfg.admin.servers = "900"
    cfg.admin.categories = "Projects"
    bot = SimpleNamespace(cfg=cfg, user=NOVA, memory=memory, get_guild=lambda gid: guild if gid == guild.id else None,
                          get_channel=guild.get_channel)
    reg = ToolRegistry()
    control = add_admin_tools(reg, bot)
    people = SimpleNamespace(anyone=member(guild, 10, "Rando", "Clocked In"),
                             root=member(guild, 11, "Ava", "Root Privileges"),
                             logi=member(guild, 12, "Helper", "Logi Bros", "Clocked In"),
                             nova_user=member(guild, 13, "Fan", "Nova User"))
    return SimpleNamespace(guild=guild, reg=reg, control=control, people=people, memory=memory,
                           chan={c.id: c for c in guild.text_channels})


def ctx(server, who, channel_id=FREE, **kw):
    return ToolContext(who.id, who.display_name, channel_id, server.memory, **kw)


async def call(server, who, tool, channel_id=FREE, **args):
    import json
    return await server.reg.run(tool, json.dumps(args), ctx(server, who, channel_id))


# -- routing ---------------------------------------------------------------------------------------

async def test_anyone_posts_in_a_free_channel_at_once(server):
    out = await call(server, server.people.anyone, "post_message", channel="#nova-chat", text="o7 everyone")
    assert out.startswith("Posted in <#101>")
    free = server.chan[FREE]
    assert free.sent[-1].content == "o7 everyone" and free.views[-1] is None
    audit = server.chan[NOTIFY].sent[-1].content
    assert "asked by Rando (`10`)" in audit and "approved" not in audit and "o7 everyone" in audit


async def test_posts_elsewhere_wait_for_approval(server):
    out = await call(server, server.people.anyone, "post_message", channel="announcements", text="Big news")
    assert out.startswith("Proposed, not done")
    assert server.chan[LOCKED].sent == []  # nothing in the target yet
    preview, view = server.chan[FREE].sent[-1], server.chan[FREE].views[-1]
    assert "Big news" in preview.content and isinstance(view, ApprovalView)
    assert {c.label for c in view.children} == {"Confirm", "Cancel"}
    assert server.chan[NOTIFY].sent == []  # proposed isn't executed


async def test_topics_and_new_channels_need_approval_even_in_free_channels(server):
    out = await call(server, server.people.root, "set_channel_topic", channel="nova-chat", topic="Ask Nova things")
    assert out.startswith("Proposed")
    assert server.chan[FREE].topic == ""
    out = await call(server, server.people.root, "create_channel", name="Fleet Ops!", category="Projects")
    assert out.startswith("Proposed") and server.guild.created == []
    assert server.control.needs_approval("topic", FREE) and server.control.needs_approval("create", 0)
    assert not server.control.needs_approval("post", FREE) and server.control.needs_approval("post", LOCKED)


async def test_edits_and_pins_of_her_own_messages_in_free_channels(server):
    await call(server, server.people.anyone, "post_message", channel="nova-chat", text="first draft")
    mine = server.chan[FREE].sent[-1]
    out = await call(server, server.people.anyone, "edit_message", channel="nova-chat", text="final")
    assert out.startswith("Edited") and mine.content == "final"
    out = await call(server, server.people.anyone, "pin_message", channel="nova-chat", message=str(mine.id))
    assert out.startswith("Pinned") and mine.pinned
    out = await call(server, server.people.anyone, "unpin_message", channel="nova-chat", message=mine.jump_url)
    assert out.startswith("Unpinned") and not mine.pinned


async def test_she_never_edits_other_peoples_messages_and_pinning_them_needs_approval(server):
    free = server.chan[FREE]
    theirs = Message(free, server.people.anyone, "my message")
    free.sent.append(theirs)
    out = await call(server, server.people.root, "edit_message", channel="nova-chat", text="x", message=str(theirs.id))
    assert out == "Refused: I only edit my own messages."
    out = await call(server, server.people.anyone, "pin_message", channel="nova-chat", message=str(theirs.id))
    assert out.startswith("Proposed") and not theirs.pinned


async def test_channel_creation_only_in_allowed_categories(server):
    out = await call(server, server.people.root, "create_channel", name="secret", category="Staff")
    assert out.startswith("Refused: I can only create channels in these categories")
    assert TRIPWIRES.events[-1].kind == "admin_refused"
    assert channel_slug("Fleet Ops!") == "fleet-ops"


async def test_requests_from_dms_or_hidden_or_muted_channels_are_refused(server):
    anyone = server.people.anyone
    out = await server.reg.run("post_message", '{"channel": "nova-chat", "text": "hi"}',
                               ctx(server, anyone, FREE, private=True))
    assert out.startswith("Refused: server changes can only be asked for in a channel")
    server.chan[LOCKED].hidden_from.add(anyone.id)
    out = await call(server, anyone, "post_message", channel="announcements", text="hi")
    assert out.startswith("Refused: you can't see that channel")
    server.chan[FREE].muted.add(anyone.id)
    out = await call(server, anyone, "post_message", channel="nova-chat", text="hi")
    assert out.startswith("Refused: you can't post in that channel yourself")


async def test_free_channels_have_their_own_rate_limit(server):
    server.control.free_per_window = 2
    for _ in range(2):
        assert (await call(server, server.people.anyone, "post_message", channel="nova-chat", text="x")).startswith("Posted")
    out = await call(server, server.people.anyone, "post_message", channel="nova-chat", text="x")
    assert out.startswith("Refused: that channel has had enough posts")


# -- approval ----------------------------------------------------------------------------------------

def pending(server) -> Proposal:
    return server.chan[FREE].views[-1].proposal


async def test_only_root_privileges_or_logi_bros_can_confirm(server):
    await call(server, server.people.anyone, "post_message", channel="announcements", text="Maintenance at 20:00")
    proposal = pending(server)
    for nope in (server.people.anyone, server.people.nova_user):
        assert (await server.control.confirm(proposal, nope)).startswith("Only members with Root Privileges or Logi Bros")
        assert TRIPWIRES.events[-1].kind == "approval_refused"
    assert server.chan[LOCKED].sent == []
    out = await server.control.confirm(proposal, server.people.logi)
    assert out.startswith("Posted in <#102>") and server.chan[LOCKED].sent[-1].content == "Maintenance at 20:00"
    audit = server.chan[NOTIFY].sent[-1].content
    assert "asked by Rando (`10`), approved by Helper (`12`)" in audit
    assert (await server.control.confirm(proposal, server.people.root)) == "This proposal is done."  # once only


async def test_approver_roles_come_from_discord_not_from_names_typed(server):
    roles = server.control.approver_roles
    assert is_approver(server.people.root, roles) and is_approver(server.people.logi, roles)
    impostor = SimpleNamespace(id=66, display_name="Root Privileges", name="Logi Bros", roles=[], guild=server.guild)
    assert not is_approver(impostor, roles)
    # Role IDs work too, and a member of another server never counts
    server.control.approver_roles = {"700"}
    assert is_approver(server.people.anyone, server.control.approver_roles)
    await call(server, server.people.anyone, "post_message", channel="announcements", text="x")
    elsewhere = SimpleNamespace(**{**vars(server.people.root), "guild": SimpleNamespace(id=1)})
    assert server.control.may_confirm(pending(server), elsewhere).startswith("Only members")


async def test_stale_proposals_expire(server):
    await call(server, server.people.anyone, "set_channel_topic", channel="announcements", topic="new")
    proposal = pending(server)
    assert server.control.may_confirm(proposal, server.people.root, now=proposal.expires + 1) == "This proposal is expired."
    assert server.chan[LOCKED].topic == ""
    assert server.control.proposals.expire() == [] and proposal.id not in server.control.proposals.pending


async def test_cancel_by_requester_or_approver(server):
    await call(server, server.people.anyone, "set_channel_topic", channel="announcements", topic="new")
    proposal = pending(server)
    assert server.control.may_cancel(proposal, server.people.nova_user).startswith("Only whoever asked")
    assert (await server.control.cancel(proposal, server.people.anyone)) == "Cancelled by Rando."
    assert (await server.control.confirm(proposal, server.people.root)) == "This proposal is cancelled."


async def test_approved_topic_and_channel(server):
    await call(server, server.people.anyone, "set_channel_topic", channel="announcements", topic="Read me")
    assert (await server.control.confirm(pending(server), server.people.root)).startswith("Topic of <#102> set")
    assert server.chan[LOCKED].topic == "Read me"
    await call(server, server.people.anyone, "create_channel", name="Fleet Ops", category="projects", topic="ops")
    out = await server.control.confirm(pending(server), server.people.root)
    assert out.startswith("Created <#") and server.guild.created[0][:3] == ("fleet-ops", 200, "ops")
    assert "approved by Ava (11)" in server.guild.created[0][3]  # reaches Discord's own audit log


# -- guards ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("tool,args", [
    ("post_message", {"channel": "nova-chat", "text": "hi"}),
    ("edit_message", {"channel": "nova-chat", "text": "hi"}),
    ("pin_message", {"channel": "nova-chat"}),
    ("unpin_message", {"channel": "nova-chat", "message": "1"}),
    ("set_channel_topic", {"channel": "nova-chat", "topic": "x"}),
    ("create_channel", {"name": "x", "category": "Projects"}),
])
async def test_tainted_turns_change_nothing(server, tool, args):
    import json
    out = await server.reg.run(tool, json.dumps(args), ctx(server, server.people.root, tainted=True))
    assert out.startswith("Refused: I read a web page or file")
    assert TRIPWIRES.events[-1].kind == "admin_tainted"
    assert server.chan[FREE].sent == [] and server.guild.created == []


async def test_other_servers_are_never_managed(server):
    server.control.servers = {123}  # e.g. the corp server: chat and AA there, no server control
    for tool, args in [("post_message", {"channel": "nova-chat", "text": "hi"}),
                       ("set_channel_topic", {"channel": "announcements", "topic": "x"}),
                       ("create_channel", {"name": "x", "category": "Projects"})]:
        out = await call(server, server.people.root, tool, **args)
        assert out.startswith("Refused: I don't manage this server")
    assert server.chan[FREE].sent == [] and server.guild.created == []
    assert await server.control.post_autonomous(server.chan[FREE], "hello") is None
    server.control.servers = set()  # the default: off everywhere
    out = await call(server, server.people.root, "post_message", channel="nova-chat", text="hi")
    assert out.startswith("Refused: I don't manage this server")


def test_there_are_no_member_control_tools(server):
    names = set(server.reg.names())
    assert names == {"post_message", "edit_message", "pin_message", "unpin_message", "set_channel_topic",
                     "create_channel"}
    banned = ("role", "kick", "ban", "timeout", "mute", "member", "delete", "purge", "permission")
    assert not [n for n in names if any(b in n for b in banned)]
    assert not [m for m in dir(ServerControl) if any(b in m for b in ("kick", "ban", "timeout", "add_role"))]


async def test_private_text_never_goes_out_in_a_post(server):
    secret = "my alt is parked in the Jita trade hub with forty billion isk worth of plex"
    server.memory.set_place(555, "Discord DMs", private=True)
    server.memory.add_message(555, "user", secret, 10, "Rando")
    out = await call(server, server.people.anyone, "post_message", channel="nova-chat", text=secret)
    assert out == "Refused: that text came from a private conversation."
    assert server.chan[FREE].sent == []


async def test_autonomous_posts_only_in_free_channels(server):
    assert await server.control.post_autonomous(server.chan[LOCKED], "hello") is None
    sent = await server.control.post_autonomous(server.chan[FREE], "hello")
    assert sent.content == "hello" and "unprompted" in server.chan[NOTIFY].sent[-1].content
