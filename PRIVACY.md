# Privacy Notice — LoRa the Explorer

**Effective:** 2026-07-27
**Contact:** [Discord](https://discord.gg/EHXemsA2SS) — `#privacy` or a DM to the operator.

LoRa the Explorer is a location-based game. Location data is inherently sensitive, so this notice
is specific about what is stored, where it goes, and what other people can see.

---

## 1. Two very different situations

Read the section that applies to you. They have different data controllers.

| | **Your own install** | **The multiplayer service ("the war ledger")** |
|---|---|---|
| What | The Docker container you run | `lora.nukeradio.net`, operated by the project maintainer |
| Who controls the data | **You do** | **The maintainer does** |
| Precise GPS stored? | **Yes** | **No — never sent** |
| Opt-in? | It's your server | Yes — multiplayer is off until you register |

If you self-host and never register for multiplayer, **no data from your install ever leaves your
machine.** The game makes no analytics, telemetry, or "phone home" requests. Single-player is
fully self-contained.

---

## 2. What your own install stores (you are the controller)

Everything below lives in the SQLite database on **your** machine (`/app/data/explorer.db` in
Docker, `%LOCALAPPDATA%\LoRaTheExplorer\explorer.db` on the Windows installer) and is never
transmitted anywhere except as described in §3.

- **Precise GPS coordinates.** Your base camp (`players.home_lat` / `home_lon`), your most recent
  survey position (`last_survey_lat` / `last_survey_lon`), and **the latitude/longitude of every
  survey you have ever made** (`surveys.lat` / `surveys.lon`).
- **H3 hex IDs** of every territory you have discovered or chartered. A resolution-8 hex ID
  decodes directly to a location roughly 460 m across — treat it as location data.
- **Radio identifiers.** The public key of your companion and spyglass devices, and cached
  details of other MeshCore nodes and repeaters your radio has heard, including repeater GPS
  positions where broadcast.
- **Account credentials.** Your dashboard password (hashed) or your OIDC subject identifier,
  plus session cookies.
- **Gameplay records.** XP, currencies, Survey Posts, relics, postcards, activity log.
- **Log output.** Some log lines include a truncated (8-character) H3 hex prefix at charter time.
  On Docker/Linux this goes to stderr, captured by your container runtime's own log driver until it
  rotates it out. On the **Windows installer**, it's written to a rotating file at
  `%LOCALAPPDATA%\LoRaTheExplorer\lora-explorer.log` — local only, never transmitted, but treat it
  with the same care as a backup if you ever share it for support.

**This means your database is a detailed map of where you physically go.** If you run this on a
machine other people can reach, secure it. Do not expose the dashboard to the public internet
without authentication, and be careful who you give backups to — a backup file contains your full
location history.

If you are self-hosting for other people, **you** are the data controller for that instance and
any legal obligations that come with it are yours, not the project's.

## 3. What leaves your install

Nothing leaves unless you turn it on. There are exactly four outbound paths:

### 3a. The multiplayer service (only after you register)

Registration is **opt-in** and currently **invite-only**. Once registered, your server periodically
pushes a signed bundle. The complete contents are (see `src/lora_explorer/multiplayer/bundle.py`):

| Field | What it is |
|---|---|
| `display_name` | The name you chose at registration (sent once, at registration) |
| `coarse_centroid` | Your base camp snapped to a **~0.75° grid (~50 mile / ~80 km cells)** — the **only** geography ever sent. Your true home is somewhere inside that ~50-mile cell; the reported point never reveals where |
| `post_summaries` | Per Survey Post: an **opaque random token**, level, your custom name, charter time, ward/upkeep timestamps |
| `survey_count`, `discoveries` | Counts only |
| `active_title` | A label from a fixed in-game list |
| `timestamp` | When the bundle was built |

**What is deliberately never sent:** your precise GPS, your survey history, the H3 hex IDs of your
territories, your XP, your provisions, your password, or your radio keys.

Your Survey Posts are identified to the outside world by a **random token**, not their real hex ID,
so a post's identifier cannot be decoded back into a place. This is true of every post without
exception — a post's token is generated randomly when it is chartered and is never derived from
its location. Auto-generated post names are also withheld, because they are derived from the real
hex.

### 3b. Notification webhook (optional, you configure it)

If you set a Discord or Ntfy webhook URL in Settings, PvP alert text is POSTed to **that URL of
your choosing**. That destination is a third party the project has no relationship with, and their
privacy practices are their own. Leave it blank to disable.

### 3c. Your LoRa mesh

Game commands and replies travel over LoRa radio. **LoRa mesh traffic is not private** — it is
unencrypted at the transport level in the sense that anyone with a receiver in range can observe
mesh activity, and MeshCore routing is public by design. Do not put anything sensitive in a
command or a Survey Post name.

### 3d. Update check (off by default, you turn it on)

Settings → Updates has a **manual "Check now" button**, always available, and a separate
**"check automatically" toggle, off by default**. Either one sends a single plain, unauthenticated
`GET` to GitHub's public releases API (`api.github.com/repos/hornofabraxas/lora-the-explorer/releases/latest`)
with a generic User-Agent — no player ID, no location, no version-mismatch fingerprinting beyond
"what's the newest tag." When on, the automatic check runs at most once a day. GitHub's own privacy
policy governs what GitHub does with that request. See `src/lora_explorer/update_check.py`.

This is unrelated to, and does not require, multiplayer registration — it works (or stays off) the
same way whether or not you've ever registered for the war ledger.

### Bug reports (not an outbound path — you send them yourself)

Help → **Report a Problem** assembles a short, non-sensitive diagnostic snapshot alongside whatever
you type. **The app never transmits it** — that is why it is not counted among the outbound paths
above. You press **Copy report** (to paste wherever you like, e.g. our Discord) or **Open GitHub
issue** (which opens a pre-filled issue in *your* browser, under *your* account). Nothing is sent
until you choose to send it, and you can see the whole payload first.

The diagnostic snapshot contains only: app version, install method, OS and architecture, Python
version, connection *type* (wifi/usb/ble — never the host/address), whether the companion is
connected, and whether multiplayer is registered/enabled. **If (and only if) you have joined the
war ledger**, it also includes your **multiplayer username and player ID** — both already
assigned by / shared with the multiplayer service — so the maintainer can look up your account to
help. It **never** includes your location, home coordinates, H3 hex IDs, node or radio keys, logs,
or webhook URLs. An optional contact field is yours to fill in (or leave blank) if you want a reply.

---

## 4. What the multiplayer service stores (maintainer is the controller)

Stored in a Cloudflare Durable Object:

- A **random 128-bit player ID** (not derived from you or your hardware)
- Your **display name** and chosen title
- Your **coarse centroid** (~50 mile grid) and when it last moved
- Your Survey Posts as **opaque tokens**, plus level, name, and timing fields
- Your multiplayer **item inventory, raid records, scout results, defence state, notifications**
- A **shared secret** used to verify your server's requests
- Anti-abuse counters (rejected-signature counts, first-seen timestamps)

**No IP addresses are logged by the application.** There is no application-level IP capture and no
analytics. However, Cloudflare — as the hosting provider — necessarily processes the IP address of
each request to route and protect it, under Cloudflare's own policies and its role as a processor.

**Lawful basis (GDPR/UK GDPR):** legitimate interests — operating a game leaderboard and
competitive service that you opted into, using the minimum data needed. You can object at any time
by deleting your registration.

## 5. What other players can see about you

Any **registered** player can see, on the leaderboard: your display name, title, renown, post count,
your posts' opaque tokens and names. The leaderboard requires authentication — it is not public to
the internet.

A player who spends a **probe** to scout you additionally sees: your posts' levels, ages, HP,
damage reduction, active boost count, and a straight-line distance to you **fuzzed to the nearest
50 miles**. Exact distance is never revealed.

Your display name and post names are chosen by you and visible to others — **do not use your real
name, address, or anything identifying.**

## 6. Retention and deletion

- **Your own install:** you control it. Delete the database or the container.
- **The multiplayer service:** ask on [Discord](https://discord.gg/EHXemsA2SS) and the maintainer
  will delete your registration. This is a **complete erasure**, not just a profile removal. It
  deletes your profile, Survey Posts, inventory, defence state, raid records, scout results,
  notifications, cooldowns, anti-abuse counters and your leaderboard entry.

  It also removes you from records that belong to *other* players. Where a rival's raid history
  names you, your player ID and display name are replaced with "A departed explorer" — they keep
  their own history, but nothing identifying you remains in it. Scout reports involving you are
  deleted outright.

- There is no automated retention schedule; data persists while the service runs.

## 7. Your rights

If you are in the UK, EU, or a US state with a privacy law, you may have rights to access, correct,
delete, or object to processing of your data. **Exercise any of them by contacting the maintainer
on [Discord](https://discord.gg/EHXemsA2SS).** Requests are handled personally, usually quickly —
this is a hobby project run by one person, so please be patient and be aware there is no 24/7 desk.

Because the service holds so little (a random ID, a name you invented, and a ~50 mile-grid centroid),
most requests can be satisfied by simply deleting your registration.

## 8. Age requirement

**The multiplayer service is for adults aged 18 or over.** See the [Terms](TERMS.md). Accounts
believed to belong to minors will be removed. The service is not directed to children and does not
knowingly collect data from anyone under 18.

## 9. International transfers

The multiplayer service runs on Cloudflare's global network and is operated from the **United
States**. If you use it from outside the US, your data is processed in the US and wherever
Cloudflare's edge handles your request.

## 10. Changes

Material changes will be announced on Discord and reflected in the effective date above. The
history of this file is public in Git.

---

*This notice describes the software as published. If someone else is hosting an instance for you,
ask them what they do with your data — this project cannot speak for them.*
