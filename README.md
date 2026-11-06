# Nova

A self-hosted Discord assistant: frank conversation, light coding help, EVE Online know-how, and memory of the people it talks to. The model runs locally behind any OpenAI-compatible server (llama.cpp's `llama-server` by default), so it can be swapped without touching the bot.

This is a combined rewrite of two earlier projects: an EVE Online Discord bot and an Ollama-based general assistant.


## Layout

```
src/nova/
  bot.py            Discord bot: when to answer, streaming replies, slash commands
  agent.py          reply loop: model <-> tool calls
  llm.py            streaming OpenAI-compatible client (hides <think> blocks)
  memory.py         SQLite: conversation log, facts, profiles, channel settings
  prompt.py         persona + per-message context
  tools.py          tool registry and built-ins (time, remember, search, files, EVE, voice, video, AA)
  inbound.py        attachments -> prompt (images, voice transcripts, file previews, EVE data blocks)
  wiring.py         builds the optional services from config
  files.py          per-channel file workspace, CSV-as-SQL
  search.py         SearXNG / Google / Brave / DuckDuckGo search, safe page reader
  voice.py          whisper.cpp transcription, pocket-tts speech, ffmpeg helpers
  video.py          ffmpeg trims, compression, gifs, frames
  aa.py             aa-reporter API client (recruiter background checks)
  serve.py          OpenAI-compatible API for chat apps (same Nova, same memory)
  fleet.py          fleet-post times worked out in EVE time
  eve/              SDE index, fit validator, real fits, killmails, pasted fits, wallets
  discord_reply.py  message splitting and live-edit streaming
  discord_admin.py  limited server control: posts, pins, topics, channels, with approval buttons
  autonomy.py       optional unprompted posts in free-post channels, on a budget
scripts/start-llm.ps1 / .sh     launch llama-server with a GGUF model (-Mtp, -Mmproj)
scripts/start-voice.ps1 / .sh   launch the speech-to-text and text-to-speech services
services/searxng/               docker compose for a private SearXNG
tests/              pytest suite, uses a fake model server
```

## Setup

```powershell
py -3.13 -m venv $env:USERPROFILE\.venvs\nova
& $env:USERPROFILE\.venvs\nova\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env          # add DISCORD_TOKEN and NOVA_OWNER_ID
copy nova.example.toml nova.toml  # optional
```

The bot needs the **Message Content** intent enabled in the Discord Developer Portal (Bot -> Privileged Gateway Intents).

## Running

```powershell
.\scripts\start-llm.ps1 -Model C:\models\your-model.gguf   # terminal 1
nova check    # is the model server answering?
nova chat     # talk to Nova in the terminal, no Discord needed
nova run      # start the Discord bot
nova sde      # fetch CCP's latest static data if newer, rebuild the item index
nova serve    # only the chat-app API (see "Talking to Nova outside Discord")
pytest        # tests
```

## Choosing a model

`evals/run_eval.py` scores the running model on Nova's actual jobs (staying quiet, EVE lookups, tools, coding, translation polish, uncensored replies, formatting); `evals/eval-model.ps1 -Model x.gguf -Name x [-Mtp]` starts llama-server, runs it and stops it. Results land in `evals/results/` (gitignored).

Current pick (October 2026, 16 GB cards): **Qwen3.5-9B Defiant Fable Heretic, plusIQ-TOOLS MTP Q8_0** (10.7 GB, DavidAU) with thinking on and `-Mtp`: 29/32, ~2.8 s median reply, ~63 tokens/s. gpt-oss-20b Derestricted is the fast fallback (23/30, under 1 s). Qwen3.6-35B-A3B, Gemma 4 26B-A4B and Mistral-Nemo-12B scored lower and were slower.

## Translation

Merged from the old EVE translator bot. NLLB-200 (`services/nllb/translator_api.py`, port 8001) makes a literal translation, sentence by sentence, after a slang glossary swaps terms it garbles (`буль` -> bubble, `нуб` -> noob). Nova's own model then polishes it for fluency; the polish is dropped if it fails, refuses or drifts in length.

- Right-click a message -> Apps -> **Translate**, or `/translate text:...`: private reply
- `/autotranslate enabled:True`: translate non-English messages in this channel publicly
- `[translate] auto = "all"` in `nova.toml` translates everywhere, like the old bot did
- `nova translate "текст"` tests the pipeline from the terminal

Run the NLLB service from its own venv (CPU is fast enough, ~2 s per message, and leaves VRAM to the main model):

```powershell
py -3.13 -m venv $env:USERPROFILE\.venvs\nllb
& $env:USERPROFILE\.venvs\nllb\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cpu
& $env:USERPROFILE\.venvs\nllb\Scripts\pip install -r services\nllb\requirements.txt
$env:REQUIRE_GPU = "false"; & $env:USERPROFILE\.venvs\nllb\Scripts\python services\nllb\translator_api.py
```

## EVE data

Nova reads CCP's static data export (JSONL) from `[eve] sde_path` in `nova.toml`, or from `var/sde` if unset. `nova sde` checks `https://developers.eveonline.com/static-data/tranquility/latest.jsonl`, downloads a newer export into `var/sde` when one exists, and rebuilds the 20 MB SQLite index (about a second). Run it daily from a timer on the server; the bot also rebuilds the index at startup if the export changed.

Ships, modules and drones named in a message are looked up automatically and added to the prompt; the model can also call `eve_item_info` itself.

- Fits: `eve_real_fits` pulls what pilots actually flew from zKillboard; drafts go through `eve_check_fit` (slots, hardpoints, CPU/PG, sizes), and replies are fact-checked against the SDE before they are sent.
- zKillboard kill links and pasted fits (EFT or copied from the game) are read before the model sees the message and arrive as data blocks; `eve_killmail` reads any other kill.
- Wallet journal exports: `eve_wallet_report` totals per character and type, checks the balance chain and flags bursts and transfers.

## Files, search, vision, voice and video

- **Files**: attachments are saved per channel under `var/files/<channel>/`. Small text files go straight into the prompt; big ones get a preview and are read with `file_read`, `file_search` and, for CSVs, `csv_query` (read-only SQL over a `data` table). `file_write` sends files back. `nova chat` takes `/file <path>`.
- **Search**: `[search] order` tries SearXNG, Google (`NOVA_GOOGLE_API_KEY`, `NOVA_GOOGLE_CX`), Brave (`NOVA_BRAVE_API_KEY`) and DuckDuckGo in turn, skipping unconfigured ones. A private SearXNG is the best option: `docker compose -f services/searxng/docker-compose.yml up -d`, then `searxng_url = "http://127.0.0.1:8888"`. `read_page` refuses private and local addresses. Set `NOVA_CONTACT` (a URL or address) in `.env`; Wikipedia rejects bots without one.
- **Vision**: `[llm] vision = true` sends attached images to the model (needs the model's mmproj: `start-llm.ps1 -Mmproj`, or the file beside the model in LM Studio).
- **Voice**: `scripts/start-voice.ps1` starts whisper.cpp (port 8003) and pocket-tts (port 8002). With `[voice]` URLs set, voice messages and audio files are transcribed, and Nova gets the `speak` tool and `/say`.
- **Video**: attached clips can be trimmed, compressed to fit Discord, turned into gifs, muted, resized, or have audio or a frame pulled out (`video_info`, `video_edit`, ffmpeg bundled via imageio-ffmpeg).

## Alliance Auth (recruiting)

With aa-reporter's API (its `nova-api` release), Nova can read background-check reports in recruiter channels: `aa_find_member`, `aa_report`, `aa_recent`, `aa_applicants`.

1. In the auth admin, create an account for Nova with `aa_reporter.basic_access` and `view_findings` only, then an **API token** for it (Reporter -> API tokens; the key is shown once).
2. `.env`: `NOVA_AA_TOKEN=rpt_...`; `nova.toml`: `[aa] reporter_url` and the recruiter `channels`.

The tools only work in those channels and the owner's DMs. Every read is logged in Reporter as a report view, under Nova's account.

## Talking to Nova outside Discord

Nova is the program, not the model: LM Studio (or llama-server) only runs the model, while her persona, tools, fact-checking and memory live in `nova`. Talking to the model directly gets you none of that. To use the real Nova from another app, switch on her API:

```toml
[serve]
enabled = true      # starts with `nova run`; or run `nova serve` on its own
host = "127.0.0.1"
port = 8740
```

Open `http://127.0.0.1:8740/` for Nova's own web app, or point any OpenAI-compatible chat app at `http://127.0.0.1:8740/v1`, model `nova` (Jan, Chatbox, phone apps, editor plugins). To reach it from another machine, set `host = "0.0.0.0"` and `NOVA_API_KEY` in `.env`, and give the app that key.

One memory everywhere:

- The terminal and chat apps talk as the owner (`NOVA_OWNER_ID`), so facts Nova learns in Discord, the terminal or an app are the same facts.
- In private conversations (DMs, the terminal, apps) she gets a short note of your most recent conversation elsewhere, so you can pick up where you left off.
- `recall` searches earlier conversations: everywhere you've talked in private places, only the current channel in public ones (nothing from a DM surfaces in a group channel).
- Files she writes come back as signed download links.

### The web app

`nova serve` (and `nova run` with `[serve] enabled`) also serves a chat page at `/`. On localhost it opens straight away as you, the owner. Behind a public URL people sign in with Discord, so Nova knows them by their real Discord ID and every owner-only guard holds as it does in Discord:

1. In the Discord developer portal, open the application (the bot's own is fine), add `<public_url>/auth/callback` under OAuth2 > Redirects, and copy its client secret.
2. Set `discord_client_id` (and `web_users` for anyone besides you) under `[serve]`, and `NOVA_DISCORD_CLIENT_SECRET` in `.env`.
3. Point the tunnel at port 8740: the page, the API and file links all live there.

Sessions are kept on the server (`var/web.db`, tokens stored hashed) and last 14 days. The page loads nothing from other sites, runs no inline script, and renders replies as text. Every change needs the session's CSRF token from the same origin, and failed sign-ins count toward the same lockout as wrong API keys.

The app's own conversation is used as context, and replies arrive whole once ready: a tool call or the EVE fact-check can replace a draft, which can't be taken back once streamed.

## Server control

Nova can post, edit and pin her own messages, set channel topics and create channels when someone asks her. She can't touch members at all: there are no tools for roles, kicks, bans or timeouts (Overlord handles those). The rules live in code (`discord_admin.py`), not in the prompt:

- **Only on the servers in `[admin] servers`** (server IDs; empty = off everywhere). In any other server she still chats, answers mentions and replies, and does Alliance Auth lookups as before, but these tools refuse. The web app and chat-app API never get them either.
- **Free-post channels** (`NOVA_FREE_POST_CHANNELS` in `.env`, channel IDs): anyone talking to her gets posts, edits and pins of her own messages right away, as long as they can see and post in that channel themselves. A channel gets at most `free_posts_per_10min` of these, on top of the usual per-person rate limit.
- **Everywhere else** she only proposes: a preview with **Confirm** / **Cancel** buttons appears in the channel where she was asked. Only a member holding one of `[admin] approver_roles` (default Root Privileges, Logi Bros) can confirm; the requester or an approver can cancel. Proposals expire after `approval_minutes`. The model can propose but never approve.
- **Topics and new channels always need approval**, free-post channels included. Channels are only created inside `[admin] categories` and inherit that category's permissions.
- Requests must come from a channel of that server (not DMs or chat apps), from someone who can see the target channel.
- If she read a web page or a file earlier in the same turn, every one of these tools refuses (a page can't talk her into posting). Refusals and clicks on Confirm by people without the role trip the security report to the owner.
- Text from private conversations is blocked here just like in her replies.
- Every executed action is posted to `[admin] audit_channel` (use #server-notifications) with who asked and who approved, and the same goes into Discord's own audit log as the reason.

```toml
[admin]
servers = "123456789012345678"                 # the server she lives on
approver_roles = "Root Privileges,Logi Bros"   # role IDs are safer than names
categories = "123456789012345678"              # where create_channel may create; empty = never
audit_channel = "123456789012345678"           # #server-notifications
approval_minutes = 30
free_posts_per_10min = 6
```

Buttons on proposals made before a restart stop working; ask her again.

### Discord permissions for Nova's role

Remove **Administrator**. Grant only:

- View Channels, Send Messages, Embed Links, Read Message History
- Manage Messages (pins; on servers that show a separate **Pin Messages** permission, grant that instead)
- Manage Channels (topics and creating channels)

Do **not** grant Manage Roles, Kick Members, Ban Members, Moderate Members (timeouts), Manage Server or Manage Webhooks. Manage Channels at server level also allows deleting channels, which Nova has no tool for; to narrow it further, grant Manage Channels only on the categories she may work in (channel permission overrides) instead of on her role. Without Read Message History she can't find her own latest message to edit or pin, or look in for autonomous mode.

### Autonomous mode (optional)

Off by default. When enabled, Nova looks in on some free-post channels every `interval_minutes` (± `jitter_minutes`), reads the recent messages and decides whether she has anything worth saying. Staying quiet is the default:

- Only channels in both `[autonomy] channels` and `NOVA_FREE_POST_CHANNELS`; anything else is ignored with a warning.
- The model isn't even asked if nothing was said since her last look, or if her message is the latest.
- A hard budget per channel: at most `max_posts_per_day` in any 24 hours and `min_gap_minutes` between posts, kept in `var/autonomy.json` so restarts don't reset it.
- She gets no tools while deciding: she can talk, but not change anything. Server changes still go through the approval flow when a person asks.
- Each post ends with a small "unprompted" line (`mark_posts`) and is logged to the audit channel and Nova's log as unprompted.

```toml
[autonomy]
enabled = true
channels = "123456789012345678"
interval_minutes = 45
jitter_minutes = 15
max_posts_per_day = 3
min_gap_minutes = 180
lookback_messages = 25
wake_on_messages = 0    # e.g. 15: also look in after 15 new messages
mark_posts = true
```

## How Nova decides to answer

- DMs: from the owner, or anyone if `open_dms = true`
- Servers: when mentioned, when someone replies to her, or everything in channels switched on with `/listen`

Slash commands: `/reset`, `/listen`, `/remember`, `/memories`, `/forgetme`, `/timezone`, `/character`, `/status`, `/translate`, `/autotranslate`, `/say` (with voice on).

In server channels Nova only sees the last `history_minutes` (default 120) of chat, so old topics don't bleed into new answers.

Every message is logged in `var/nova.db` (gitignored). `/reset` only hides earlier history from the model; the log stays as future fine-tuning material.

## License

MIT. See [LICENSE](LICENSE).
