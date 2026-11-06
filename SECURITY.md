# Security Policy

Nova is a Discord assistant with long-term memory, file access, and tools that can search the web, talk to EVE services, and (on servers you allow) post or create channels. Treat anything that lets someone reach another person's private data, or act with Nova's permissions, as a security issue.

## Supported versions

Only the latest commit on `main` is supported. If you are not running that, upgrade before reporting unless the issue clearly still applies.

## Reporting a vulnerability

**Do not open a public issue, pull request, or Discord post.**

Use GitHub's private reporting form:

https://github.com/Thrainkrilleve/nova-discord/security/advisories/new

Please include:

- A description of the issue and its impact
- Steps to reproduce, or a proof of concept
- The commit hash or release you tested
- Whether you have already shared it with anyone else

You should get an acknowledgement within 7 days. Please give us a chance to fix and release a patch before any public disclosure. If we confirm the report, we will credit you unless you ask us not to.

If private reporting is unavailable, open a [blank issue](https://github.com/Thrainkrilleve/nova-discord/issues/new) titled "Security: please contact me" with no technical details, and we will follow up privately.

## In scope

- Reading or leaking another person's DMs, memories, files, or profile without authorization
- Prompt injection or jailbreaks that bypass owner, channel, or tool restrictions
- Getting Nova to post, edit, pin, set topics, or create channels without the documented approval rules
- Using recruiter, Alliance Auth, or other privileged tools from a place they should refuse
- SSRF or local-network access through search, page reading, or other outbound requests
- Path traversal or escape from the per-channel file workspace
- Stealing, logging, or exposing `DISCORD_TOKEN`, API keys, or other secrets
- Authentication bypass on the web app or chat-app API

## Out of scope

- The model being rude, wrong, or uncensored when that does not leak private data or bypass permissions
- Self-hosting mistakes, such as publishing `.env`, exposing the API without `NOVA_API_KEY`, or granting the bot Administrator
- Discord platform bugs, rate limits, or API outages
- Third-party model servers, search engines, or EVE APIs behaving as those services document

## Handling secrets and reports

Never commit `.env`, `nova.toml` with real IDs, bot tokens, `var/nova.db`, `var/web.db`, or uploaded user files. If a token may have leaked, reset it in the [Discord Developer Portal](https://discord.com/developers/applications) immediately.

When you report a problem, redact tokens, API keys, and other people's message content, memories, or personal data.
