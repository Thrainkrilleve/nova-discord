# Nova roadmap

## Done (October 2026, `rebuild` branch)

- Combined rewrite of EVE Nova and NovaAI; local model behind an OpenAI-compatible server
- Model pick by eval: Qwen3.5-9B Defiant Fable Heretic MTP Q8_0, thinking on, vision via mmproj
- Translation (NLLB + model polish), stay-quiet listening, history lookup, timezone handling
- EVE: current SDE index, fit validator, real fits from zKillboard, reply fact-checking
- Killmail links, pasted fits (EFT and in-game copy) and wallet journals read automatically
- File services: per-channel workspace, read/search/write, CSV as read-only SQL
- Search: SearXNG / Google / Brave / DuckDuckGo chain, safe page reader
- Vision (image recognition), voice messages transcribed (whisper.cpp), speech out (pocket-tts, `/say`)
- Video editing with ffmpeg: trim, compress, gif, audio, mute, resize, frame
- Alliance Auth: aa-reporter `nova-api` with per-token keys; recruiter tools gated by channel

## Next update

Ordered by value for effort. Each item should land with tests and, where it changes replies, an eval case.

1. **Go live on the server.** Step by step in `deploy/DEPLOY.md`: llama-server on the host (Vulkan, systemd `nova-llm`; Docker Desktop can't reach the GPU), everything else in Docker, the web app behind Cloudflare. Later: `nova sde` on a daily timer. Retire the old translator bot once `[translate] auto = "all"` has run for a week.
2. **aa-reporter release.** Run its tests and `makemigrations --check` in the dev container, merge `nova-api`, create Nova's account and token, and point a recruiter channel at it.
3. **New-applicant watch.** A background task that polls `aa_applicants` / `aa_recent` and posts a short heads-up in the recruiter channel when a new applicant's report has high findings. Opt-in per channel; never posts report text anywhere else.
4. **Eval coverage for the new tools.** Cases for csv_query arithmetic, killmail summaries, pasted-fit analysis, search-then-read, and AA refusals outside recruiter channels.
5. **Voice in voice channels.** Join a channel, listen with whisper, answer with pocket-tts. Needs discord.py voice receive (an extension) and push-to-talk style turn taking; start with "answer when named".
6. **Voice fine-tuning.** Clone a Nova voice for pocket-tts from a short reference clip (`voice_url`), then compare against stock voices.
7. **Memory upkeep.** Summarise old channel history into facts, prune stale ones, and add `/forget` for a single fact.

## Back burner

- Image generation (recruitment material) and image editing: needs a diffusion model that fits alongside the LLM in 16 GB, or a second GPU/time slot. ComfyUI behind a small API would slot in like the voice services.
- Video generation: same constraint, much heavier.
- Sound and music generation.
- Fine-tuning Nova's own model on the logged conversations (Unsloth); only once the log holds enough good examples.
