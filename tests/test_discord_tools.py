"""discord_members: Discord roles and members, scoped to the servers the asker can see."""

from datetime import datetime, timezone
from types import SimpleNamespace

from nova.discord_tools import _guilds_for, describe
from nova.tools import ToolContext


class Role(SimpleNamespace):
    def is_default(self):
        return self.name == "@everyone"


def server(name, people, roles):
    """people: {display name: [role names]}; roles: role names, lowest first."""
    everyone = Role(name="@everyone", managed=False, members=[])
    by_name = {r: Role(name=r, managed=False, members=[]) for r in roles}
    members = []
    for i, (display, has) in enumerate(people.items()):
        m = SimpleNamespace(id=1000 + i, display_name=display, name=display.lower().replace(" ", "_"),
                            joined_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
                            roles=[everyone, *(by_name[r] for r in has)])
        members.append(m)
        for r in has:
            by_name[r].members.append(m)
    g = SimpleNamespace(name=name, chunked=True, members=members, member_count=len(members),
                        roles=[everyone, *by_name.values()])
    g.get_member = lambda uid: next((m for m in members if m.id == uid), None)
    return g


ROSE = server("ROSE", {"Ava Stone": ["Member", "Director"], "PilotTwoMain": ["Member", "Director"],
                       "Norkk": ["Member"]}, ["Member", "Recruiter", "Director"])
FRIENDS = server("Friends", {"Ava Stone": ["Admin"], "Someone": []}, ["Admin"])


def test_who_has_a_role():
    out = describe([ROSE], role="director")
    assert out == "ROSE, @Director (2): Ava Stone, PilotTwoMain"
    assert "@Recruiter (0): nobody" in describe([ROSE], role="@Recruiter")
    assert "no role like 'Pope'" in describe([ROSE], role="Pope")


def test_a_members_roles_and_the_role_list():
    assert describe([ROSE], member="norkk") == "ROSE: Norkk (norkk, joined 2025-01-02): Member"
    assert "Ava Stone (ava_stone, joined 2025-01-02): Director, Member" in describe([ROSE], member="ava")
    assert describe([ROSE]) == "ROSE, 3 members, roles: Director (2), Recruiter (0), Member (3)"


def test_dms_cover_only_servers_the_asker_shares():
    bot = SimpleNamespace(guilds=[ROSE, FRIENDS], get_channel=lambda cid: None,
                          is_owner_id=lambda uid: uid == 1)
    norkk = ROSE.members[2].id
    assert _guilds_for(bot, ToolContext(norkk, "Norkk", 300, None), "") == [ROSE]       # not his: Friends
    assert _guilds_for(bot, ToolContext(1, "Ava", 300, None), "") == [ROSE, FRIENDS]  # the owner: all
    assert _guilds_for(bot, ToolContext(1, "Ava", 300, None), "friend") == [FRIENDS]
    assert describe([]) == "I'm not in a server with you that matches that."
