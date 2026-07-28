# LoRa the Explorer

A location-based exploration RPG played over **LoRa mesh radio**.

You carry a handheld LoRa device (a "spyglass"), walk out into the real world, and radio survey
commands back to a game server you run yourself. The server tracks the territories you discover,
the Survey Posts you charter, and — if you opt in — pits your outposts against other explorers'.

No cell service required. No app store. No account with anyone. Just a radio, a hex grid, and
somewhere to walk.

> **Theme:** the World's End Society — a society of surveyors mapping what's left.

---

## How it works

```
Your spyglass ──LoRa radio──> companion node ──TCP──> game server ──> web dashboard
   (in the field)                (at home)          (your Docker container)
```

You send `/lora survey` from the field. The server requests your GPS position over radio telemetry,
works out which H3 hex you're standing in, and pays out XP, provisions, and survey marks. Everything
else — maps, upgrades, the merchant, PvP — happens on the web dashboard, because LoRa bandwidth is
precious and only commands that prove location go over the air.

## Features

- **Survey the real world.** H3 resolution-8 hex grid (~0.31 sq mi per territory). Discover new
  territory for bonus rewards.
- **Charter Survey Posts.** Permanent outposts at real locations ≥3 miles from base camp. Level
  them up, keep them tended, or watch them fall into ruin.
- **Progress systems.** Ranks, momentum streaks, postcards (achievements), weekly Expedition
  Contracts, a rotating Frontier Merchant, and randomly dropped relics.
- **A dashboard that looks the part.** A CRT-and-bronze handheld radio UI with a live event feed,
  a Leaflet map of your discovered hexes, and repeater positions pulled from the mesh.
- **Optional multiplayer.** Register with the war ledger to appear on a leaderboard, scout rivals,
  and dispatch raids against their outposts. Entirely opt-in — see [Privacy](#privacy--your-data).
- **Runs on a Pi.** Lightweight Python container, SQLite, no external services required.

## Requirements

- A **MeshCore** companion node reachable over TCP, serial, or BLE (a Heltec V3 or similar)
- A **LoRa handheld with GPS** as your spyglass (e.g. ThinkNode M1) with location telemetry enabled
- Docker, or Python 3.12+

> Built for MeshCore. Meshtastic is not supported today; the radio layer is behind an adapter
> interface (`radio/adapter.py`) so it can be added without a rewrite.

## Quick start

```bash
docker run -d --name lora-the-explorer -p 1492:1492 -e TZ=America/Phoenix -v /path/to/data:/app/data ghcr.io/hornofabraxas/lora-the-explorer:latest
```

Set `TZ` to your own [IANA timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) — it
sets the clock used for the timestamps in radio replies, so you can tell a fresh reply from a stale
one. Everything the game resets on a schedule (the daily survey limit, the dispatch) is UTC by
design and is unaffected by this.

Then open `http://localhost:1492` and follow the setup wizard — it will walk you through choosing a
password (or OIDC SSO), connecting your companion radio, and dropping a pin for your base camp.

<details>
<summary>Running from source</summary>

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m lora_explorer
```

For UI work without any radio hardware, there's a mock-data preview server on port 1493:

```bash
.venv/bin/python preview_server.py
```
</details>

## Commands

Sent from your spyglass over the mesh:

| Command | What it does |
|---|---|
| `/lora survey` | Survey the territory you're standing in |
| `/lora charter` | Begin chartering a Survey Post here |
| `/lora <name>` | Name the post, completing the charter |
| `/lora upkeep` | Tend a Survey Post, resetting its ruin timer |

## Privacy & your data

**Please read this before you deploy it.**

Your install's database contains **a detailed history of where you have physically been** — your
base camp coordinates, every survey's latitude and longitude, and every hex you've discovered.

- **You are the data controller for your own instance.** If you host it for other people, the legal
  responsibility for their data is yours.
- **Don't expose the dashboard to the open internet** without authentication, and treat backup
  files as sensitive — a backup is your full location history.
- **Single-player sends nothing anywhere.** No telemetry, no analytics, no phone-home.
- **Multiplayer is opt-in** and sends only a ~11 km-rounded centroid plus opaque post tokens —
  never your precise GPS, survey history, or real hex IDs.
- **The optional update check is opt-in and off by default** — a plain request to GitHub's public
  release list, no player data attached, never automatic unless you turn it on in Settings.
- **The hosted multiplayer service is 18+.**

Full detail: **[PRIVACY.md](PRIVACY.md)** · **[TERMS.md](TERMS.md)** · **[SECURITY.md](SECURITY.md)**

## Play safely

This game asks you to walk around real places. **Never play while driving.** Watch your
surroundings, don't trespass, and obey local law — including the radio regulations that apply to
LoRa in your country. Your safety and your compliance are your own responsibility.

## Multiplayer service

The hosted war ledger lives at `lora.nukeradio.net` and is currently **invite-only** while the game
is in alpha. The server is open source too — see the companion repo,
[`lora-worker`](https://github.com/hornofabraxas/lora-worker). You can run your own.

## Windows

The [releases page](https://github.com/hornofabraxas/lora-the-explorer/releases) has a
`LoRaTheExplorer-Setup-X.Y.Z.exe` installer for each tagged version — Start Menu shortcut, optional
desktop icon, no Python or Docker needed. It runs as a system-tray app: right-click the tray icon
for **Open Dashboard**, **Open Log Folder**, or **Quit**. Data (including your survey history —
treat it as sensitive, see [Privacy](#privacy--your-data)) lives in `%LOCALAPPDATA%\LoRaTheExplorer\`
and is **not** removed by uninstalling.

The installer is unsigned, so Windows SmartScreen will warn on first run — this is a solo hobby
project without a code-signing certificate, not a false-positive detector working correctly. Click
"More info" → "Run anyway" if you trust the source, or build it yourself from
`packaging/windows/` if you'd rather not.

The Windows build listens on all interfaces by default (matching the Docker image), so it's reachable
from other devices on your LAN or over VPN without extra setup. The dashboard is auth-gated, but
that gate isn't up yet on a brand-new install — **set a password (or OIDC) on first launch, from the
same machine, before exposing this to a network anyone else can reach**, including the open internet
via port forwarding. Until you do, anyone who reaches `/setup` first claims the password. If you want
loopback-only instead, set `HOST=127.0.0.1` yourself before launching.

## Updating

**Docker:** the `:latest` tag on `ghcr.io/hornofabraxas/lora-the-explorer` tracks every push to
`main` — Force Update (Unraid) or `docker compose pull && docker compose up -d` picks it up.
Tagged releases (`v0.2.0`, etc.) are published separately and pinned — use one of those instead if
you want a stable version rather than the rolling edge.

**Windows:** download and run the newer installer from the releases page; it installs over the old
version in place.

**Checking for updates:** Settings → Updates has a manual "Check now" button (always available) and
an opt-in daily automatic check (off by default) — both a plain, unauthenticated request to GitHub's
public release list, no player data attached. See [Privacy](#privacy--your-data). If you're
registered for multiplayer and the war ledger has moved past what your version can talk to, you'll
see an "Update required" banner — local play keeps working either way.

## Releasing (maintainers)

```bash
git tag v0.2.0
git push origin v0.2.0
```

CI builds and pushes `ghcr.io/hornofabraxas/lora-the-explorer:v0.2.0`, publishes a GitHub Release
with auto-generated notes, and attaches the Windows installer built from that same commit. It does
not touch `:latest`. Version numbers follow [SemVer](https://semver.org/) loosely — this is
pre-1.0, so breaking wire/schema changes bump the minor version, ideally alongside raising
`MIN_CLIENT_VERSION` on the Worker (see the `lora-worker` repo) so old clients get a clear "please
update" instead of silently desyncing.

## Contributing

Issues and pull requests are welcome. Please run the tests first:

```bash
.venv/bin/python -m pytest
```

If you're changing anything that touches location data or what crosses to the multiplayer service,
say so explicitly in the PR — that boundary is the project's most important invariant.

## Community

[Discord](https://discord.gg/EHXemsA2SS) — support, privacy requests, invite codes, and mesh talk.

## License

[MIT](LICENSE) © 2026 Justin Walls. Third-party credits in [NOTICE](NOTICE).

**LoRa®** is a registered trademark of Semtech Corporation. This project is independent and
unaffiliated — not endorsed by or associated with Semtech, the LoRa Alliance, or MeshCore. The name
describes the radio technology the game runs on.
