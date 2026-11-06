# Moving Nova to the server

Ubuntu with Docker Desktop. About an hour, most of it waiting. Commands are copy-paste; run them one
at a time.

**How it fits together:** the language model runs directly on Ubuntu (Docker Desktop can't reach the
GPU), and everything else runs in Docker: Nova, translation, voice and search. Cloudflare sends
`nova.example.com` to Nova on port 8740.

**Rule of the night:** only one Nova may run on the bot token. The desktop Nova stays off once you
start the server's. To roll back, stop the server's and start the desktop's again: her memory there
is untouched.

---

## Part 1: on the desktop (10 minutes plus copying)

**1. Stop Nova.** In PowerShell:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*nova.exe*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

**2. Plug in the flash drive and pack everything onto it.** Replace `E:` with the drive's letter:

```powershell
cd $env:USERPROFILE\OneDrive\Documents\claude\nova
powershell -ExecutionPolicy Bypass -File scripts\export-for-server.ps1 -Dest E:\nova-transfer
```

It copies her memory, the model (11 GB) and the prebuilt Docker images, then writes `MANIFEST.txt`.
It refuses to run while Nova is still up. You need about 25 GB free on the drive.

**3. Copy your `.env` too.** It holds the bot token and keys, so it isn't in git. Copy
`nova\.env` to `E:\nova-transfer\desktop.env` by hand.

---

## Part 2: on the server

**1. Copy the transfer folder into your home folder.** Docker Desktop only shares your home folder
with containers. Then put the models where the model server expects them:

```bash
cp -r /media/$USER/*/nova-transfer ~/nova-transfer      # adjust if the drive mounts elsewhere
sudo mkdir -p /opt/nova/models && sudo cp ~/nova-transfer/models/* /opt/nova/models/
```

**2. Get the code.**

```bash
git clone -b rebuild https://git.example.com/you/Nova.git ~/nova
cd ~/nova
```

**3. Install and start the model server.** It downloads a pinned llama.cpp (Vulkan) build, installs
the GPU drivers it needs, and starts the service `nova-llm`:

```bash
sudo deploy/install-llm.sh
```

Wait for `OK: nova-llm is up` and a line naming the GPU (`Radeon RX 7800 XT`). If it says the service
didn't come up and the log mentions `spec` or `mtp`, run:

```bash
sudo sed -i 's/^SPEC=.*/SPEC=none/' /etc/nova-llm.env && sudo systemctl restart nova-llm
```

**4. Settings.**

```bash
cp deploy/nova.docker.toml deploy/nova.toml
cp ~/nova-transfer/desktop.env .env
cat .env.example | grep -E "^(SEARXNG_SECRET|NOVA_CPUS|NOVA_MEM|NOVA_DISCORD_CLIENT_SECRET)=" >> .env
nano .env
```

In `.env`, check or fill:

| Setting | Value |
|---|---|
| `DISCORD_TOKEN`, `NOVA_OWNER_ID`, `NOVA_AA_TOKEN` | already there from the desktop |
| `NOVA_LLM_BASE_URL` | leave it; the stack uses its own |
| `NOVA_API_KEY` | needed now that Nova is public. Make one: `openssl rand -hex 32` |
| `SEARXNG_SECRET` | `openssl rand -hex 32` |
| `NOVA_DISCORD_CLIENT_SECRET` | from step 9 (you can fill it in later) |

Remove any duplicate lines, then save (Ctrl+O, Enter, Ctrl+X).

**5. Docker Desktop settings.** Settings > Resources: give it at least **12 GB of memory**, and check
that File sharing includes `/home/<you>`. Apply & restart.

**6. Load the images.** This is the fast path, using the ones built on the desktop:

```bash
docker load -i ~/nova-transfer/images/nova-images.tar.gz
```

If that file isn't there, build them instead (10 to 20 minutes; versions are locked, so it builds the
same thing):

```bash
docker compose --env-file .env -f deploy/docker-compose.yml build
```

**7. Check Docker can reach the model.**

```bash
docker compose --env-file .env -f deploy/docker-compose.yml run --rm --no-deps --entrypoint python nova -c "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:8780/health').read())"
```

You want `b'{"status":"ok"}'`. If it fails, see *Problems* below.

**8. Load her memory, then start everything.**

```bash
deploy/import-data.sh ~/nova-transfer
docker compose --env-file .env -f deploy/docker-compose.yml up -d
docker compose --env-file .env -f deploy/docker-compose.yml logs -f nova
```

Wait for `logged in as Nova#5785`, then press Ctrl+C (Nova keeps running). Say hi to her in Discord.
Download the EVE data once:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml exec nova nova sde
```

**9. The web app (Discord sign-in).** At https://discord.com/developers/applications, open Nova's
application:

- OAuth2 > Redirects > add `https://nova.example.com/auth/callback` and save.
- OAuth2 > Client Secret > Reset > copy it into `.env` as `NOVA_DISCORD_CLIENT_SECRET`.

Then restart Nova:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml up -d nova
```

**10. Cloudflare.** In Zero Trust > Networks > Tunnels, open the server's tunnel > Public hostname >
Add:

- Subdomain `nova`, domain `example.com`
- Service `HTTP` and `localhost:8740`. If cloudflared itself runs in Docker Desktop, use
  `host.docker.internal:8740` instead.

Open https://nova.example.com and sign in with Discord. **Done.**

---

## Problems

| What you see | Fix |
|---|---|
| `install-llm.sh`: no GPU line, or slow replies | `vulkaninfo --summary` should list the 7800 XT. If not: `sudo apt install mesa-vulkan-drivers`, reboot. |
| Step 7 fails (can't reach the model) | In `/etc/nova-llm.env` set `HOST=0.0.0.0`, then `sudo systemctl restart nova-llm` and `sudo ufw deny 8780` so only this machine uses it. |
| Compose says a variable is missing | You left out `--env-file .env`, or that line is empty in `.env`. |
| `import-data.sh` says memory already there | Expected on a second run. `FORCE=1 deploy/import-data.sh ~/nova-transfer` replaces it. |
| A mount error mentioning `/home` or `/media` | Docker Desktop > Settings > Resources > File sharing: add your home folder. |
| Nova answers twice in Discord | The desktop Nova is still running. Stop it (Part 1, step 1). |
| Web page says "closed: sign-in isn't set up" | `NOVA_DISCORD_CLIENT_SECRET` is empty or Nova wasn't restarted (step 9). |
| Sign-in says "Not on the list" | Only you get in. Add others' Discord IDs to `web_users` in `deploy/nova.toml`, then restart Nova. |

**Roll back:** `docker compose --env-file .env -f deploy/docker-compose.yml stop nova` on the server,
then start Nova on the desktop as before.

## Everyday commands (from `~/nova`)

```bash
# once: a permanent shorthand (not "dc": that name is Ubuntu's calculator)
echo "novac() { docker compose --env-file ~/nova/.env -f ~/nova/deploy/docker-compose.yml \"\$@\"; }" >> ~/.bashrc && source ~/.bashrc
novac ps                       # what's running
novac logs -f nova             # Nova's log
novac restart nova             # after changing deploy/nova.toml or .env
novac kill nova                # kill switch
git pull && novac build nova && novac up -d --no-deps nova   # update Nova only (never "up --build": it rebuilds every image at once and can starve the server)
sudo systemctl status nova-llm         # the model server
journalctl -u nova-llm -f              # its log
```

## Pinned versions

Builds are repeatable: Python packages come from `deploy/locks/*.txt` (prebuilt wheels only, nothing
compiles), whisper.cpp is pinned in `whisper.Dockerfile`, SearXNG in the compose file, and llama.cpp in
`install-llm.sh` (with its checksum). To move to newer versions on purpose: loosen the line in the lock
file (or delete the file's contents except torch's), rebuild, then write the lock again from the image:

```bash
docker run --rm --entrypoint pip nova-nllb:latest freeze > deploy/locks/nllb.txt   # likewise nova, tts
```

Back up her memory now and then: `docker run --rm -v nova_nova-data:/v:ro -v ~/backups:/b alpine tar czf /b/nova-$(date +%F).tgz -C /v .`
