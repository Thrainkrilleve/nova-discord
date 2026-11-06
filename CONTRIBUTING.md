# Contributing to Nova

Thanks for helping. Nova stores conversations and can act in Discord servers, so changes are reviewed with extra care around memory, permissions, and tool access.

## Before you start

- Use your own Discord application and a test server. Do not point a development build at someone else's production bot token.
- Never commit `.env`, tokens, `var/`, databases, or real user files.
- Read [SECURITY.md](SECURITY.md) if your change touches memory, tools, Discord admin actions, search, files, or the web API.
- Be kind in issues and reviews. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Development setup

You need Python 3.11 or newer.

```
git clone https://github.com/Thrainkrilleve/nova-discord.git
cd nova-discord
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
copy .env.example .env          # Windows; on Unix: cp .env.example .env
# add DISCORD_TOKEN and NOVA_OWNER_ID if you are running the bot
pytest
```

The test suite uses a fake model server, so you do not need llama-server or a GPU to run `pytest`. `nova chat` and `nova run` do need a running OpenAI-compatible model server; see the README.

## Pull requests

1. Fork the repo and create a branch from `main`.
2. Keep the change focused. A permission or memory change is easier to review on its own than mixed with refactors.
3. Add or update tests when behavior changes. The suite already covers memory isolation, tool guards, search URL rules, and Discord admin approvals.
4. Run `pytest` before you open the pull request.
5. Explain what changed and how you tested it.

Please do not submit generated code dumps with no explanation, or changes whose purpose is to exfiltrate user data or bypass Nova's permission rules.
