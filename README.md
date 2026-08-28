# LoRa the Explorer

A location-based exploration game you play over long-range **LoRa radio**, with no cell service or
internet needed out in the field.

You carry a small handheld LoRa device (your "spyglass"), walk out into the real world, and radio
survey commands back to a game server you run yourself at home. The server tracks the territory you
discover, the outposts you build, and, if you choose, pits your outposts against other explorers'.

No cell signal. No app store. No account with anyone. Just a radio, a map, and somewhere to walk.

> **The setting:** the World's End Society, a guild of surveyors mapping what is left of the world.

## A look around

The game is played from a phone-friendly web dashboard on your own network. The tabs run left to
right along the bottom: Briefing, Radio, Outposts, Ledger, and Multiplayer.

<table>
  <tr>
    <td width="25%"><img src="docs/screenshots/briefing.png" alt="Briefing tab"><br><sub><b>Briefing.</b> Your home screen: currencies, the daily dispatch, your rank, the merchant, and contracts.</sub></td>
    <td width="25%"><img src="docs/screenshots/radio1.png" alt="Radio console"><br><sub><b>Radio.</b> The live feed and the Survey, Charter, and Upkeep commands you send from the field.</sub></td>
    <td width="25%"><img src="docs/screenshots/radio2.png" alt="Live map"><br><sub><b>Map.</b> The territory you have discovered and the mesh repeaters around you.</sub></td>
    <td width="25%"><img src="docs/screenshots/outpost1.png" alt="Outposts base camp"><br><sub><b>Outposts.</b> Grow your base camp to unlock perks, slots, and bigger rewards.</sub></td>
  </tr>
  <tr>
    <td width="25%"><img src="docs/screenshots/outpost2.png" alt="Survey posts"><br><sub><b>Survey Posts.</b> Outposts that earn for you passively, if you keep them maintained.</sub></td>
    <td width="25%"><img src="docs/screenshots/ledger.png" alt="Ledger"><br><sub><b>Ledger.</b> Your expedition stats and the achievements you have unlocked.</sub></td>
    <td width="25%"><img src="docs/screenshots/multiplayer1.png" alt="Multiplayer hub"><br><sub><b>Multiplayer.</b> Your standing on the shared war ledger and your outpost defenses.</sub></td>
    <td width="25%"><img src="docs/screenshots/multiplayer2.png" alt="Warfront"><br><sub><b>Warfront.</b> Scout rival explorers and raid their outposts. Distances stay deliberately coarse.</sub></td>
  </tr>
</table>

## How it works

```
Your spyglass ──LoRa radio──> companion node ──> game server ──> web dashboard
   (in the field)               (at home)        (your computer or Pi)
```

You send `/lora survey` from the field. The server figures out where you are from your device's GPS,
works out which territory you are standing in, and pays out rewards. Everything else, like maps,
upgrades, the merchant, and multiplayer, happens on the web dashboard, because radio bandwidth is
precious and only the commands that need to prove your location go over the air.

## Features

- **Survey the real world.** Walk somewhere, survey it, and claim that territory. Every survey earns
  rewards, and reaching somewhere new pays a bonus on top. Each territory is about a third of a
  square mile.
- **Build outposts.** Set up permanent Survey Posts at real locations away from home. Level them up
  and keep them tended, or watch them fall into ruin.
- **Progress systems.** Ranks, daily streaks, achievements, weekly expedition contracts, a rotating
  merchant, and rare relics you dig up while surveying.
- **A dashboard that looks the part.** A retro radio-set interface with a live event feed, a map of
  the ground you have covered, and the mesh repeaters near you.
- **Optional multiplayer.** Join the shared war ledger to appear on a leaderboard, scout rivals, and
  raid their outposts. Entirely opt-in. See [Privacy](#privacy--your-data).
- **Light to run.** A small Python app with a local database and no external services. It is happy on
  a Raspberry Pi, and runs just as well on any computer.

## Getting started

### What you'll need

- **A companion radio node running MeshCore.** This is the base station at home that receives your
  radio messages. It connects to the game server over Wi-Fi, USB, or Bluetooth. A Heltec V3 or
  similar works well.
- **A handheld LoRa device with GPS** to carry in the field (your "spyglass"), such as a ThinkNode
  M1, with location sharing turned on.
- **Somewhere to run the game server.** Any always-on computer or a Raspberry Pi. On Windows you do
  not need anything extra (see the Windows option below).

> Built for MeshCore. Meshtastic is not supported yet. The radio layer is designed so support could
> be added later without a rewrite.

### Install

Pick the option that matches where you want to run the server.

**Docker (Linux, macOS, Raspberry Pi, or Windows).** The recommended way:

```bash
docker run -d --name lora-the-explorer -p 1492:1492 -e TZ=America/Phoenix -v /path/to/data:/app/data ghcr.io/hornofabraxas/lora-the-explorer:latest
```

Set `TZ` to your own [timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones). It
sets the clock shown in radio replies, so you can tell a fresh reply from a stale one. Anything the
game resets on a schedule (the daily survey limit, the dispatch) runs on UTC and is unaffected.

**Windows, without Docker or Python.** Download the `LoRaTheExplorer-Setup-X.Y.Z.exe` installer from
the [releases page](https://github.com/hornofabraxas/lora-the-explorer/releases). It adds a Start
Menu shortcut and runs as a system-tray app: right-click the tray icon for **Open Dashboard**, **Open
Log Folder**, or **Quit**. Your data (including your survey history, so treat it as sensitive) lives
in `%LOCALAPPDATA%\LoRaTheExplorer\` and is **not** removed by uninstalling.

The installer is unsigned, so Windows SmartScreen will warn you on first run. This is a solo hobby
project without a code-signing certificate, not a real threat. Click **More info**, then **Run
anyway** if you trust the source, or build it yourself from `packaging/windows/`.

**From source (Python 3.12 or newer):**

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m lora_explorer
```

### First run

Open `http://localhost:1492` and follow the setup wizard. It walks you through choosing a password
(or single sign-on), connecting your companion radio, and dropping a pin for your home base. After
that, head outside and send your first `/lora survey`.

## Commands

Sent from your spyglass over the radio:

| Command | What it does |
|---|---|
| `/lora survey` | Survey the territory you are standing in |
| `/lora charter` | Begin building a Survey Post here |
| `/lora <name>` | Name the post, completing the charter |
| `/lora upkeep` | Tend a Survey Post, resetting its ruin timer |

## Privacy & your data

**Please read this before you deploy it.**

Your install's database holds **a detailed history of where you have physically been**: your home
coordinates, every survey's location, and every place you have discovered.

- **You are responsible for your own instance.** If you host it for other people, the responsibility
  for their data is yours.
- **Do not expose the dashboard to the open internet** without a password, and treat backup files as
  sensitive. A backup is your full location history.
- **Single-player sends nothing anywhere.** No telemetry, no analytics, no phone-home.
- **Multiplayer is opt-in.** It sends only a coarse location snapped to a roughly 50-mile grid, plus
  anonymous outpost tokens. It never sends your precise location or your survey history.
- **The update check is opt-in and off by default.** When on, it is a plain request to GitHub's
  public release list with no personal data attached.
- **The hosted multiplayer service is 18+.**

Full detail: **[PRIVACY.md](PRIVACY.md)** · **[TERMS.md](TERMS.md)** · **[SECURITY.md](SECURITY.md)**

## Play safely

This game asks you to walk around real places. **Never play while driving.** Watch your
surroundings, do not trespass, and follow local law, including the radio regulations that apply to
LoRa in your country. Your safety and your compliance are your own responsibility.

## Multiplayer service

The hosted war ledger lives at `lora.nukeradio.net` and is **invite-only** while the game is in
alpha. The service is open source too, so you can read exactly what it does with the coarse data you
send it, or run your own. See the companion repo,
[`lora-worker`](https://github.com/hornofabraxas/lora-worker).

## Updating

**Docker:** the `:latest` tag tracks the newest build. Pull it again (`docker compose pull && docker
compose up -d`, or Force Update on Unraid) to update. Tagged versions like `v0.3.0` are pinned and
published separately if you prefer a fixed version over the rolling edge.

**Windows:** download and run the newer installer from the releases page. It installs over the old
version in place.

**Checking for updates:** Settings has a manual "Check now" button, plus an opt-in daily automatic
check that is off by default. Both are a plain request to GitHub's public release list with no
personal data attached. If you play multiplayer and the service has moved past what your version can
talk to, you will see an "Update required" banner. Local play keeps working either way.

## Community

[Discord](https://discord.gg/EHXemsA2SS) for support, privacy requests, invite codes, and mesh talk.

## License

[MIT](LICENSE) © 2026 hornofabraxas. Third-party credits in [NOTICE](NOTICE).

**LoRa®** is a registered trademark of Semtech Corporation. This project is independent and
unaffiliated, and is not endorsed by or associated with Semtech, the LoRa Alliance, or MeshCore. The
name describes the radio technology the game runs on.
